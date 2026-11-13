"""Progress tracking: percent, speed and estimated time left."""

from __future__ import annotations

import time
from typing import Callable, Optional


class ProgressTracker:
    """Turns (done, total) updates into a percent, a speed and an ETA.

    Speed is measured over a sliding window so it reflects the current pace,
    not the average since the start.
    """

    def __init__(self, total: int, clock: Callable[[], float] = time.monotonic, window: float = 10.0,
                 unit: str = "bytes"):
        self.total = total
        self.unit = unit  # "bytes" or "items" (folders, files)
        self._clock = clock
        self._window = window
        self._start = clock()
        self._samples: list[tuple[float, int]] = [(self._start, 0)]
        self.done = 0

    def update(self, done: int) -> None:
        now = self._clock()
        self.done = done
        self._samples.append((now, done))
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.pop(0)

    @property
    def percent(self) -> Optional[float]:
        if not self.total:
            return None
        return min(100.0, self.done * 100.0 / self.total)

    @property
    def speed(self) -> float:
        """Bytes per second over the recent window (0 when unknown)."""
        if len(self._samples) < 2:
            return 0.0
        t0, d0 = self._samples[0]
        t1, d1 = self._samples[-1]
        if t1 - t0 < 0.5:  # too short to measure a real pace
            return 0.0
        return (d1 - d0) / (t1 - t0)

    @property
    def eta_seconds(self) -> Optional[float]:
        speed = self.speed
        if not self.total or speed <= 0:
            return None
        return max(0.0, (self.total - self.done) / speed)

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    def status_text(self) -> str:
        parts = []  # separators use plain ASCII so every console shows them correctly
        if self.percent is not None:
            parts.append(f"{self.percent:5.1f}%")
        if self.unit == "items":
            parts.append(f"{self.done} dari {self.total}" if self.total else f"{self.done} dibaca")
            if self.speed > 0:
                parts.append(f"{self.speed:.1f} item/dtk")
        else:
            parts.append(f"{format_bytes(self.done)} dari {format_bytes(self.total)}" if self.total
                         else f"{format_bytes(self.done)} dipindai")
            if self.speed > 0:
                parts.append(f"{format_bytes(self.speed)}/dtk")
        eta = self.eta_seconds
        if eta is not None:
            parts.append(f"sisa {format_duration(eta)}")
        return "  |  ".join(parts)


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} dtk"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} mnt {sec:02d} dtk"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} jam {minutes:02d} mnt"
