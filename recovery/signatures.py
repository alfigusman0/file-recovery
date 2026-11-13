"""File signatures used by the carving engine.

Each signature describes:
  header      - magic bytes that start the file
  offset      - position of the magic bytes inside the file (usually 0)
  footer      - bytes that mark the end of the file (None = use max_size or next file)
  footer_extra- bytes that follow the footer (e.g. the 18 fixed bytes after a ZIP EOCD)
  footer_last - use the LAST footer before the next file of the same type (PDF incremental updates)
  scan_start  - function(head_bytes) -> offset where footer search may start (JPEG: after SOS)
  max_size    - upper bound used when no footer is found
  check       - optional validation of the first bytes (called with 4096 bytes)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .formats import jpeg_scan_start


@dataclass(frozen=True)
class Signature:
    name: str
    extension: str
    header: bytes
    footer: Optional[bytes]
    max_size: int
    offset: int = 0
    check: Optional[Callable[[bytes], bool]] = None
    footer_extra: int = 0
    footer_last: bool = False
    scan_start: Optional[Callable[[bytes], Optional[int]]] = None


MB = 1024 * 1024


def _riff_avi(head: bytes) -> bool:
    return len(head) >= 12 and head[8:12] == b"AVI "


def _riff_wav(head: bytes) -> bool:
    return len(head) >= 12 and head[8:12] == b"WAVE"


def _gzip(head: bytes) -> bool:
    """3-byte magic alone matches random data often; check the flag and OS bytes too."""
    if len(head) < 10:
        return False
    flags, os_byte = head[3], head[9]
    return flags & 0xE0 == 0 and os_byte <= 13


def _mp3_id3(head: bytes) -> bool:
    """ID3v2 header: major version 2-4 and revision 0."""
    return len(head) >= 6 and head[3] in (2, 3, 4) and head[4] == 0


def _pe(head: bytes) -> bool:
    """MZ alone is too common in random data; require a valid PE header."""
    if len(head) < 0x40:
        return False
    lfanew = int.from_bytes(head[0x3C:0x40], "little")
    if not (0 < lfanew < 0x10000) or len(head) < lfanew + 4:
        return False
    return head[lfanew:lfanew + 4] == b"PE\x00\x00"


SIGNATURES: list[Signature] = [
    Signature("jpeg", "jpg", b"\xff\xd8\xff\xe0", b"\xff\xd9", 30 * MB, scan_start=jpeg_scan_start),
    Signature("jpeg", "jpg", b"\xff\xd8\xff\xe1", b"\xff\xd9", 30 * MB, scan_start=jpeg_scan_start),
    Signature("png", "png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 30 * MB),
    Signature("gif", "gif", b"GIF87a", b"\x00\x3b", 20 * MB),
    Signature("gif", "gif", b"GIF89a", b"\x00\x3b", 20 * MB),
    Signature("pdf", "pdf", b"%PDF-", b"%%EOF", 500 * MB, footer_last=True),
    # ZIP-based formats (DOCX, XLSX, PPTX, APK, JAR): end at the End Of Central Directory record.
    Signature("zip", "zip", b"PK\x03\x04", b"PK\x05\x06", 2000 * MB, footer_extra=18),
    Signature("rar", "rar", b"Rar!\x1a\x07", None, 2000 * MB),
    Signature("7z", "7z", b"7z\xbc\xaf\x27\x1c", None, 2000 * MB),
    Signature("gzip", "gz", b"\x1f\x8b\x08", None, 2000 * MB, check=_gzip),
    Signature("sqlite", "sqlite", b"SQLite format 3\x00", None, 2000 * MB),
    Signature("mp4", "mp4", b"ftyp", None, 4000 * MB, offset=4),
    Signature("avi", "avi", b"RIFF", None, 4000 * MB, check=_riff_avi),
    Signature("wav", "wav", b"RIFF", None, 1000 * MB, check=_riff_wav),
    Signature("mp3", "mp3", b"ID3", None, 50 * MB, check=_mp3_id3),
    Signature("exe", "exe", b"MZ", None, 200 * MB, check=_pe),
]


MAX_HEADER_LEN = max(len(s.header) + s.offset for s in SIGNATURES)


def by_extension(ext: str) -> list[Signature]:
    ext = ext.lower().lstrip(".")
    return [s for s in SIGNATURES if s.extension == ext]
