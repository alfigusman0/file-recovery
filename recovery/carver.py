"""Signature-based file carving.

Reads a raw source (disk device, partition, or disk image file) sequentially,
finds known file headers, then extracts each file up to its footer (or a
maximum size). Works without any file system metadata, so it can recover files
after a quick format or a permanent delete as long as the data is not overwritten.

Raw device reads on Windows (\\\\.\\C:) and macOS/Linux (/dev/...) must be
sector aligned, so all reads go through `read_at`, which aligns automatically.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
from dataclasses import asdict, dataclass
from typing import BinaryIO, Callable, Iterable, Optional

from .dates import DateRange, embedded_datetime
from .signatures import MAX_HEADER_LEN, SIGNATURES, Signature

ALIGN = 4096
SCAN_CHUNK = 8 * 1024 * 1024
EXTRACT_BLOCK = 4 * 1024 * 1024
MIN_FILE_SIZE = 1024
CHECK_READ = 4096      # bytes read for Signature.check
DATE_READ = 200_000    # bytes read to find an embedded date
SCAN_HEAD = 256 * 1024  # bytes read to find where JPEG image data starts


@dataclass
class RecoveredFile:
    path: str
    kind: str
    source_offset: int
    size: int
    complete: bool  # True when a footer was found (or the format has none)


@dataclass
class CarveSummary:
    recovered: list[RecoveredFile]
    skipped_by_date: int
    cancelled: bool
    skipped_inside_other: int = 0


ProgressFn = Callable[[int, int], None]  # (bytes_scanned, total_bytes)


class CancelledError(Exception):
    """Raised internally when the user cancels a scan."""


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError()


def read_at(f: BinaryIO, pos: int, size: int) -> bytes:
    """Read `size` bytes from absolute position `pos`, aligned for raw devices."""
    if size <= 0:
        return b""
    aligned = pos - (pos % ALIGN)
    span = pos + size - aligned
    span = ((span + ALIGN - 1) // ALIGN) * ALIGN
    f.seek(aligned)
    data = f.read(span)
    start = pos - aligned
    return data[start:start + size]


def source_size(f: BinaryIO) -> int:
    """Total size of a file or raw device (0 if unknown)."""
    try:
        return f.seek(0, os.SEEK_END) or 0
    except OSError:
        return 0


def _find_headers(buf: bytes, base: int, sigs: Iterable[Signature], seen: set[tuple[str, int]]):
    """Yield (signature, file_start) for new header hits inside buf."""
    for sig in sigs:
        idx = buf.find(sig.header)
        while idx != -1:
            file_start = base + idx - sig.offset
            key = (sig.name + sig.extension, file_start)
            if file_start >= 0 and key not in seen:
                seen.add(key)
                yield sig, file_start
            idx = buf.find(sig.header, idx + 1)


def find_candidates(
    f: BinaryIO,
    sigs: list[Signature],
    total: int,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> list[tuple[Signature, int]]:
    """Pass 1: locate every header in the source. Result is sorted by offset."""
    tail_len = MAX_HEADER_LEN + 64
    results: list[tuple[Signature, int]] = []
    seen: set[tuple[str, int]] = set()
    pos = 0
    tail = b""
    while True:
        _check_cancel(cancel)
        chunk = read_at(f, pos, SCAN_CHUNK)
        if not chunk:
            break
        buf = tail + chunk
        base = pos - len(tail)
        for sig, start in _find_headers(buf, base, sigs, seen):
            if sig.check is not None:
                head = read_at(f, start, CHECK_READ)
                if not sig.check(head):
                    continue
            results.append((sig, start))
        pos += len(chunk)
        tail = buf[-tail_len:]
        if progress:
            progress(pos, total)
        if len(chunk) < SCAN_CHUNK:
            break
    results.sort(key=lambda item: item[1])
    return results


def extract_one(
    f: BinaryIO,
    sig: Signature,
    start: int,
    out_path: str,
    total: int,
    limit: Optional[int] = None,
    cancel: Optional[threading.Event] = None,
) -> tuple[int, bool]:
    """Write one carved file. Returns (size, complete).

    limit: absolute offset where this file must stop (start of the next file of
    the same type). Footers found beyond it are ignored.
    """
    footer = sig.footer
    extra = sig.footer_extra
    keep = len(footer) - 1 if footer else 0
    end_limit = start + sig.max_size
    if limit is not None:
        end_limit = min(end_limit, limit)
    if total:
        end_limit = min(end_limit, total)

    # Where footer search may begin (JPEG: after the header segments, so an EXIF thumbnail is skipped)
    footer_from = start
    if sig.scan_start is not None:
        head = read_at(f, start, SCAN_HEAD)
        rel = sig.scan_start(head)
        if rel is not None:
            footer_from = start + rel

    written = 0
    complete = False
    last_end: Optional[int] = None   # footer_last: absolute end of the last footer seen
    window = b""                     # bytes read but not yet written (footer may span blocks)
    window_base = start              # absolute offset of window[0]

    with open(out_path, "wb") as out:
        pos = start
        while pos < end_limit:
            _check_cancel(cancel)
            block = read_at(f, pos, min(EXTRACT_BLOCK, end_limit - pos))
            if not block:
                break
            pos += len(block)
            data = window + block
            base = window_base

            if not footer:
                out.write(data)
                written += len(data)
                window, window_base = b"", pos
                continue

            # Find footers whose end lies beyond what has already been decided.
            idx = data.find(footer, max(0, footer_from - base))
            if sig.footer_last:
                while idx != -1:
                    end_abs = base + idx + len(footer) + extra
                    # include a line break right after the footer (e.g. "%%EOF" + newline)
                    tail = data[idx + len(footer) + extra:idx + len(footer) + extra + 2]
                    end_abs += len(tail) - len(tail.lstrip(b"\r\n"))
                    if last_end is None or end_abs > last_end:
                        last_end = end_abs
                    idx = data.find(footer, idx + 1)
                emit = len(data) - keep
                if emit > 0:
                    out.write(data[:emit])
                    written += emit
                window, window_base = data[emit:], base + emit
                continue

            if idx != -1:
                end_abs = base + idx + len(footer) + extra
                if base + len(data) >= end_abs:  # whole footer (plus extra bytes) is in hand
                    end_rel = end_abs - base
                    out.write(data[:end_rel])
                    written += end_rel
                    complete = True
                    window = b""
                    break
                # Footer found but its trailing bytes are in the next block: keep everything.
                window, window_base = data, base
                continue

            emit = len(data) - keep
            if emit > 0:
                out.write(data[:emit])
                written += emit
            window, window_base = data[emit:], base + emit

        if footer and not complete:
            if window:
                out.write(window)
                written += len(window)
                window = b""
            if sig.footer_last and last_end is not None:
                # Keep everything up to the last footer; drop later incremental junk.
                out.truncate(last_end - start)
                written = last_end - start
                complete = True

    if written == 0 or (not complete and written < MIN_FILE_SIZE):
        os.remove(out_path)
        return 0, False
    if not footer:
        complete = True  # no footer defined; accept what was read
    return written, complete


def carve(
    source: str,
    out_dir: str,
    extensions: Optional[list[str]] = None,
    progress: Optional[ProgressFn] = None,
    max_files: Optional[int] = None,
    date_range: Optional[DateRange] = None,
    cancel: Optional[threading.Event] = None,
) -> list[RecoveredFile]:
    """Carve files from `source` (image file or raw device) into `out_dir`.

    date_range: keep only files whose date stored INSIDE the file (EXIF, PDF
        CreationDate, ZIP timestamp) falls in the range. Raw carving cannot know
        when a file was deleted, so this is a creation/capture date filter.
        Files without a readable date are skipped when a range is set.
    cancel: threading.Event; set it to stop early. Files already written are kept.
    """
    return carve_summary(source, out_dir, extensions, progress, max_files, date_range, cancel).recovered


def carve_summary(
    source: str,
    out_dir: str,
    extensions: Optional[list[str]] = None,
    progress: Optional[ProgressFn] = None,
    max_files: Optional[int] = None,
    date_range: Optional[DateRange] = None,
    cancel: Optional[threading.Event] = None,
    phase: Optional[Callable[[str, int, int], None]] = None,
) -> CarveSummary:
    """phase(text, done, total) reports the second stage (writing the found files)."""
    os.makedirs(out_dir, exist_ok=True)
    wanted = {e.lower().lstrip(".") for e in extensions} if extensions else None
    sigs = [s for s in SIGNATURES if wanted is None or s.extension in wanted]
    if not sigs:
        raise ValueError("No signatures match the requested extensions")

    recovered: list[RecoveredFile] = []
    skipped_by_date = 0
    skipped_inside = 0
    cancelled = False
    report = os.path.join(out_dir, "recovery_report.json")
    with open(source, "rb", buffering=0) as f:
        total = source_size(f)
        try:
            candidates = find_candidates(f, sigs, total, progress, cancel)
            # Start of the next candidate of the same type, used as an upper bound.
            by_name: dict[str, list[int]] = {}
            for sig, start in candidates:
                by_name.setdefault(sig.name, []).append(start)
            counters: dict[str, int] = {}
            covered_until = -1  # end of the last file written; later hits inside it are copies
            n_candidates = len(candidates)
            for index, (sig, start) in enumerate(candidates, 1):
                _check_cancel(cancel)
                if phase is not None:
                    phase("Menulis file", index, n_candidates)
                if max_files is not None and len(recovered) >= max_files:
                    break
                if start < covered_until:
                    skipped_inside += 1
                    continue
                # Only formats without a self-terminating footer need the next-file bound.
                # (A ZIP's local headers are all candidates; its EOCD ends it, so it must not be cut.)
                limit = None
                if sig.footer is None or sig.footer_last:
                    starts = by_name[sig.name]
                    nxt = bisect.bisect_right(starts, start)
                    limit = starts[nxt] if nxt < len(starts) else None
                if date_range is not None and date_range.active:
                    head = read_at(f, start, DATE_READ)
                    try:
                        when = embedded_datetime(sig.name, head)
                    except Exception:  # noqa: BLE001 - a broken date must not stop the scan
                        when = None
                    if not date_range.contains(when):
                        skipped_by_date += 1
                        continue
                counters[sig.extension] = counters.get(sig.extension, 0) + 1
                name = f"{sig.name}_{counters[sig.extension]:05d}_{start:x}.{sig.extension}"
                out_path = os.path.join(out_dir, name)
                size, complete = extract_one(f, sig, start, out_path, total, limit=limit, cancel=cancel)
                if size:
                    recovered.append(RecoveredFile(out_path, sig.name, start, size, complete))
                    covered_until = start + size
        except CancelledError:
            cancelled = True

    with open(report, "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in recovered], fh, indent=2)
    return CarveSummary(recovered, skipped_by_date, cancelled, skipped_inside)
