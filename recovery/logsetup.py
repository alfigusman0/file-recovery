"""Log file setup shared by the GUI and CLI.

Logs go to <home>/FileRecovery/logs/recovery.log so a failed scan can be
investigated later. Logging never stops the app: if the folder cannot be
created, logs go to stderr only.
"""

from __future__ import annotations

import logging
import threading
import os
import sys

LOG_DIR = os.path.join(os.path.expanduser("~"), "FileRecovery", "logs")
_configured = False


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log records in memory so the UI can show them live.

    Each record gets an increasing sequence number; the UI asks for "everything after N".
    """

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.capacity = capacity
        self.records: list[dict] = []
        self.seq = 0
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + self.formatter.formatException(record.exc_info) if self.formatter else ""
        except Exception:  # noqa: BLE001 - logging must never break the app
            text = str(record.msg)
        with self._lock:
            self.seq += 1
            self.records.append({
                "seq": self.seq,
                "time": self.formatter.formatTime(record, "%H:%M:%S") if self.formatter else "",
                "level": record.levelname,
                "logger": record.name.replace("recovery.", ""),
                "message": text,
            })
            if len(self.records) > self.capacity:
                del self.records[: len(self.records) - self.capacity]

    def since(self, seq: int) -> tuple[list[dict], int]:
        with self._lock:
            return [r for r in self.records if r["seq"] > seq], self.seq


RING = RingBufferHandler()


def set_debug(enabled: bool) -> None:
    """Debug mode shows every step (DEBUG level) in the live log; normal mode shows key events."""
    logging.getLogger("recovery").setLevel(logging.DEBUG if enabled else logging.INFO)
    RING.setLevel(logging.DEBUG if enabled else logging.INFO)


def setup_logging() -> str:
    """Configure logging once. Returns the log file path (or '' if unavailable)."""
    global _configured
    logger = logging.getLogger("recovery")
    if _configured:
        return os.path.join(LOG_DIR, "recovery.log")
    _configured = True
    logger.setLevel(logging.INFO)
    logger.addHandler(RING)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.path.join(LOG_DIR, "recovery.log")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        path = ""
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.info("session started")
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"recovery.{name}")
