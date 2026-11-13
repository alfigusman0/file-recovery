"""Background jobs for the web UI: one job at a time, live progress, cancellation."""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Optional

from ..logsetup import get_logger
from ..progress import ProgressTracker

log = get_logger("jobs")


class JobBusy(RuntimeError):
    pass


class Job:
    def __init__(self, name: str, label: str, unit: str = "bytes"):
        self.name = name
        self.label = label
        self.state = "running"          # running | done | error | cancelled
        self.phase = "Memulai"
        self.text = ""
        self.cancel_event = threading.Event()
        self.tracker = ProgressTracker(0, unit=unit)
        self.started = time.time()
        self.finished: Optional[float] = None
        self.result: Any = None
        self.error: Optional[str] = None
        self.phase_progress: Optional[tuple[int, int]] = None

    # called from the worker thread
    def set_total(self, total: int) -> None:
        self.tracker.total = total

    def progress(self, done: int, total: Optional[int] = None) -> None:
        if total is not None:
            self.tracker.total = total
        self.tracker.update(done)
        # Debug detail, throttled to about one line per 5 % (or per 1000 items when no total)
        step = max(1, (self.tracker.total or 1000) // 20)
        if done // step != getattr(self, "_last_step", -1):
            self._last_step = done // step
            log.debug("[%s] %s", self.name, self.tracker.status_text())

    def set_phase(self, phase: str, text: str = "") -> None:
        self.phase = phase
        self.text = text
        self.phase_progress = None
        log.debug("[%s] %s %s", self.name, phase, text)

    def set_phase_progress(self, phase: str, done: int, total: int) -> None:
        """Progress of a stage with its own counter (e.g. writing found files: 12 of 40)."""
        self.phase = phase
        self.phase_progress = (done, total)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def to_dict(self) -> dict:
        t = self.tracker
        eta = t.eta_seconds
        percent = round(t.percent, 1) if t.percent is not None else None
        done, total = t.done, t.total
        if self.phase_progress is not None and self.state == "running":
            done, total = self.phase_progress
            percent = round(done * 100 / total, 1) if total else None
            eta = None
        return {
            "name": self.name,
            "label": self.label,
            "state": self.state,
            "phase": self.phase,
            "text": self.text,
            "percent": percent,
            "done": done,
            "total": total,
            "unit": t.unit,
            "speed": t.speed,
            "eta_seconds": round(eta) if eta is not None else None,
            "status_line": t.status_text() if self.phase_progress is None else f"{done} dari {total}",
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "error": self.error,
            "result": self.result if self.state in ("done", "cancelled") else None,
        }


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Optional[Job] = None
        self.last: Optional[Job] = None

    def start(self, name: str, label: str, fn: Callable[[Job], Any], unit: str = "bytes") -> Job:
        with self._lock:
            if self.current is not None and self.current.state == "running":
                raise JobBusy(f"{self.current.label} masih berjalan. Tunggu atau batalkan dulu.")
            job = Job(name, label, unit)
            self.current = job
        log.info("job started: %s", name)

        def worker() -> None:
            try:
                job.result = fn(job)
                job.state = "cancelled" if job.cancelled else "done"
                if job.state == "done" and job.tracker.total:
                    job.tracker.done = job.tracker.total
            except Exception as exc:  # noqa: BLE001 - shown to the user through the API
                job.state = "error"
                job.error = str(exc)
                log.error("job %s failed: %s\n%s", name, exc, traceback.format_exc())
            finally:
                job.finished = time.time()
                log.info("job finished: %s (%s)", name, job.state)
                with self._lock:
                    self.last = job

        threading.Thread(target=worker, daemon=True, name=f"job-{name}").start()
        return job

    def cancel(self) -> bool:
        job = self.current
        if job is None or job.state != "running":
            return False
        job.cancel_event.set()
        job.set_phase("Membatalkan", "menunggu langkah berikutnya selesai")
        log.info("job cancel requested: %s", job.name)
        return True

    def snapshot(self) -> dict:
        job = self.current or self.last
        if job is None:
            return {"state": "idle"}
        return job.to_dict()
