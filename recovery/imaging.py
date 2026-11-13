"""Disk imaging and result export.

Imaging copies a disk or partition into a single image file before recovery.
Recovering from the image instead of the live disk is the safest workflow:
the original is only read, never written, and the image can be reused.

Bad sectors are zero-filled and logged instead of stopping the copy.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .carver import read_at, source_size
from .fsmeta import DeletedEntry

BLOCK = 1024 * 1024
SUBBLOCK = 4096
ProgressFn = Callable[[int, int], None]


@dataclass
class ImageResult:
    image_path: str
    bytes_copied: int
    total_bytes: int
    sha256: str
    bad_ranges: list[tuple[int, int]] = field(default_factory=list)  # (offset, length) zero-filled
    cancelled: bool = False


def image_source(
    source: str,
    dest_path: str,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> ImageResult:
    """Copy source (file or raw device) to dest_path, zero-filling unreadable blocks."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    digest = hashlib.sha256()
    bad: list[tuple[int, int]] = []
    copied = 0
    cancelled = False
    with open(source, "rb", buffering=0) as src, open(dest_path, "wb") as out:
        total = source_size(src)
        pos = 0
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            want = min(BLOCK, total - pos) if total else BLOCK
            if want <= 0:
                break
            try:
                block = read_at(src, pos, want)
            except OSError:
                block = _read_failed_block(src, pos, want, total, bad)
            if not block:
                break  # end of readable data
            out.write(block)
            digest.update(block)
            pos += len(block)
            copied += len(block)
            if progress:
                progress(copied, total)
        out.flush()
    return ImageResult(dest_path, copied, total, digest.hexdigest(), bad, cancelled)


def _read_failed_block(src, pos: int, size: int, total: int, bad: list) -> bytes:
    """Re-read a failed block sub-block by sub-block; zero-fill sectors that cannot be read.

    Returns b"" when nothing at all is readable and the size is unknown (end of device).
    """
    out = bytearray()
    readable_any = False
    for off in range(0, size, SUBBLOCK):
        length = min(SUBBLOCK, size - off)
        try:
            part = read_at(src, pos + off, length)
        except OSError:
            part = b""
        if part:
            readable_any = True
        else:
            if total == 0 and not readable_any:
                return b""  # unknown size and nothing readable from here: end of device
            part = b"\x00" * length
            bad.append((pos + off, length))
        out += part
    return bytes(out)


def verify_image(image_path: str, expected_sha256: str) -> bool:
    """Re-hash an image file and compare with the hash recorded at imaging time."""
    digest = hashlib.sha256()
    with open(image_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BLOCK), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


# ---------- export ----------

EXPORT_FIELDS = ["path", "name", "size_bytes", "file_system", "partition", "inode",
                 "created", "modified", "deleted_approx"]


def export_entries(entries: list[DeletedEntry], path: str) -> str:
    """Write the listing to CSV or JSON depending on the file extension."""
    rows = [{
        "path": e.path,
        "name": e.name,
        "size_bytes": e.size,
        "file_system": e.fs_type,
        "partition": e.partition,
        "inode": e.inode,
        "created": e.crtime.isoformat() if e.crtime else "",
        "modified": e.mtime.isoformat() if e.mtime else "",
        "deleted_approx": e.deleted_at.isoformat() if e.deleted_at else "",
    } for e in entries]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith(".json"):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return path
