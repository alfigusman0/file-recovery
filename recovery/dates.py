"""Date range parsing and filtering.

A range is optional. Empty start or end means "no limit on that side";
both empty means "all files".
"""

from __future__ import annotations

import datetime as dt
import re
import struct
from typing import Optional

DATE_FORMAT = "%Y-%m-%d"


class DateRange:
    def __init__(self, start: Optional[dt.datetime] = None, end: Optional[dt.datetime] = None):
        if start and end and start > end:
            raise ValueError("Tanggal awal lebih besar dari tanggal akhir")
        self.start = start
        self.end = end

    @property
    def active(self) -> bool:
        return self.start is not None or self.end is not None

    def contains(self, when: Optional[dt.datetime]) -> bool:
        if not self.active:
            return True  # no range: everything matches, even unknown dates
        if when is None:
            return False
        if when.tzinfo is not None:
            # Convert aware times (UTC or offset) to local time before comparing.
            when = when.astimezone().replace(tzinfo=None)
        if self.start and when < self.start:
            return False
        if self.end and when > self.end:
            return False
        return True

    def __repr__(self) -> str:
        s = self.start.strftime(DATE_FORMAT) if self.start else "awal"
        e = self.end.strftime(DATE_FORMAT) if self.end else "akhir"
        return f"DateRange({s} .. {e})"


def parse_date(text: str, end_of_day: bool = False) -> Optional[dt.datetime]:
    """Parse YYYY-MM-DD. Returns None for empty input. end_of_day makes it inclusive."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        day = dt.datetime.strptime(text, DATE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"Format tanggal harus YYYY-MM-DD, contoh 2026-01-01: {text!r}") from exc
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day


def make_range(start_text: str = "", end_text: str = "") -> DateRange:
    return DateRange(parse_date(start_text), parse_date(end_text, end_of_day=True))


# ---------- embedded dates in carved files ----------

_PDF_DATE = re.compile(rb"/CreationDate\s*\(D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")


def _exif_datetime(data: bytes) -> Optional[dt.datetime]:
    """Read DateTimeOriginal (0x9003) or DateTime (0x0132) from a JPEG EXIF block."""
    idx = data.find(b"Exif\x00\x00")
    if idx == -1 or idx > 65536 or idx + 14 > len(data):
        return None
    tiff = idx + 6
    order = "<" if data[tiff:tiff + 2] == b"II" else ">"
    if data[tiff + 2:tiff + 4] != (b"*\x00" if order == "<" else b"\x00*"):
        return None  # not a TIFF header

    def read_ifd(offset: int) -> dict[int, tuple[int, int, int]]:
        entries = {}
        base = tiff + offset
        if base + 2 > len(data):
            return entries
        count = struct.unpack_from(order + "H", data, base)[0]
        for i in range(min(count, 200)):
            p = base + 2 + i * 12
            if p + 12 > len(data):
                break
            tag, typ, n = struct.unpack_from(order + "HHI", data, p)
            value_or_off = struct.unpack_from(order + "I", data, p + 8)[0]
            entries[tag] = (typ, n, value_or_off)
        return entries

    def ascii_value(entry) -> Optional[str]:
        typ, n, voff = entry
        if typ != 2 or n == 0:
            return None
        if n <= 4:
            raw = struct.pack(order + "I", voff)[:n]
        else:
            start = tiff + voff
            raw = data[start:start + n]
        return raw.split(b"\x00")[0].decode("ascii", "replace")

    if tiff + 8 > len(data):
        return None
    ifd0 = read_ifd(struct.unpack_from(order + "I", data, tiff + 4)[0])
    candidates = []
    if 0x8769 in ifd0:  # Exif sub-IFD
        sub = read_ifd(ifd0[0x8769][2])
        if 0x9003 in sub:
            candidates.append(ascii_value(sub[0x9003]))
    if 0x0132 in ifd0:
        candidates.append(ascii_value(ifd0[0x0132]))
    for text in candidates:
        if text:
            try:
                return dt.datetime.strptime(text.strip(), "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
    return None


def _pdf_datetime(data: bytes) -> Optional[dt.datetime]:
    m = _PDF_DATE.search(data[:200000] if len(data) > 200000 else data)
    if not m:
        return None
    parts = [int(g) if g else default for g, default in zip(m.groups(), (1, 1, 1, 0, 0, 0))]
    year, month, day, hour, minute, second = parts
    try:
        return dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _zip_datetime(data: bytes) -> Optional[dt.datetime]:
    """Date of the first local file header (DOS date/time)."""
    if not data.startswith(b"PK\x03\x04") or len(data) < 14:
        return None
    dos_time, dos_date = struct.unpack_from("<HH", data, 10)
    year = 1980 + (dos_date >> 9)
    month = (dos_date >> 5) & 0x0F
    day = dos_date & 0x1F
    hour = dos_time >> 11
    minute = (dos_time >> 5) & 0x3F
    second = (dos_time & 0x1F) * 2
    if year < 1981 or not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    try:
        return dt.datetime(year, month, day, min(hour, 23), min(minute, 59), min(second, 59))
    except ValueError:
        return None


def embedded_datetime(kind: str, head: bytes) -> Optional[dt.datetime]:
    """Best-effort creation date stored inside a carved file.

    `head` should contain the first part of the file (up to ~200 KB).
    This is the date the file was created or photographed, NOT the date it was deleted.
    """
    if kind == "jpeg":
        return _exif_datetime(head)
    if kind == "pdf":
        return _pdf_datetime(head)
    if kind == "zip":
        return _zip_datetime(head)
    return None
