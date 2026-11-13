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
  walk        - optional structure walker (see formats.py): walk(read, avail) -> None
                (reject) or (end, complete). When set, the walked end decides the
                file size instead of footer / next-file / max_size.
  align       - only accept headers whose file start is a multiple of this (0 = any);
                used for weak magics (bare MP3 frame sync) since files start on sectors
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .formats import (
    MOV_BRANDS,
    gif_walk,
    id3v2_size,
    jpeg_header_ok,
    jpeg_scan_start,
    mp3_frames_ok,
    mp3_walk,
    mp4_ftyp_brand,
    mp4_walk,
)


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
    walk: Optional[Callable[[Callable[[int, int], bytes], int], Optional[tuple[int, bool]]]] = None
    align: int = 0


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
    """ID3v2 header: major version 2-4, revision 0 and a syncsafe size.

    If the tag ends inside `head`, the first audio frame after it (allowing some
    zero padding) must carry a valid frame sync too.
    """
    if not (len(head) >= 6 and head[3] in (2, 3, 4) and head[4] == 0):
        return False
    tag = id3v2_size(head)
    if tag is None:
        return False
    if tag + 4 > len(head):
        return True  # large tag (cover art): the walker checks the frames
    rel = tag
    while rel + 4 <= len(head) and head[rel] == 0:
        rel += 1
    if rel + 4 > len(head):
        return True
    return mp3_frames_ok(head, rel, need=2)


def _mp3_frame(head: bytes) -> bool:
    """No ID3 tag: the first audio frame must have the 11-bit sync and a valid
    Layer III header, followed by consistent frames."""
    return mp3_frames_ok(head, 0, need=3)


def _jpeg(head: bytes) -> bool:
    return jpeg_header_ok(head)


def _mp4(head: bytes) -> bool:
    brand = mp4_ftyp_brand(head)
    return brand is not None and brand not in MOV_BRANDS


def _mov(head: bytes) -> bool:
    return mp4_ftyp_brand(head) in MOV_BRANDS


def _pe(head: bytes) -> bool:
    """MZ alone is too common in random data; require a valid PE header."""
    if len(head) < 0x40:
        return False
    lfanew = int.from_bytes(head[0x3C:0x40], "little")
    if not (0 < lfanew < 0x10000) or len(head) < lfanew + 4:
        return False
    return head[lfanew:lfanew + 4] == b"PE\x00\x00"


SIGNATURES: list[Signature] = [
    # SOI + marker; _jpeg validates the marker after SOI (APPn/DQT/DHT/SOFn/DRI/COM)
    # and the segment chain, so an embedded EXIF thumbnail SOI is also accepted only if valid.
    Signature("jpeg", "jpg", b"\xff\xd8\xff", b"\xff\xd9", 30 * MB, check=_jpeg, scan_start=jpeg_scan_start),
    Signature("png", "png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 30 * MB),
    # GIF: the 00 3B byte pair also occurs inside LZW data, so the end is found by
    # walking the block structure to the 0x3B trailer instead of a footer search.
    Signature("gif", "gif", b"GIF87a", None, 20 * MB, walk=gif_walk),
    Signature("gif", "gif", b"GIF89a", None, 20 * MB, walk=gif_walk),
    Signature("pdf", "pdf", b"%PDF-", b"%%EOF", 500 * MB, footer_last=True),
    # ZIP-based formats (DOCX, XLSX, PPTX, APK, JAR): end at the End Of Central Directory record.
    Signature("zip", "zip", b"PK\x03\x04", b"PK\x05\x06", 2000 * MB, footer_extra=18),
    Signature("rar", "rar", b"Rar!\x1a\x07", None, 2000 * MB),
    Signature("7z", "7z", b"7z\xbc\xaf\x27\x1c", None, 2000 * MB),
    Signature("gzip", "gz", b"\x1f\x8b\x08", None, 2000 * MB, check=_gzip),
    Signature("sqlite", "sqlite", b"SQLite format 3\x00", None, 2000 * MB),
    # ISO-BMFF: "ftyp" at offset 4 alone matches random data; the check validates the
    # ftyp box + brand and the walker follows top-level boxes to find the real end.
    Signature("mp4", "mp4", b"ftyp", None, 4000 * MB, offset=4, check=_mp4, walk=mp4_walk),
    Signature("mov", "mov", b"ftyp", None, 4000 * MB, offset=4, check=_mov, walk=mp4_walk),
    Signature("avi", "avi", b"RIFF", None, 4000 * MB, check=_riff_avi),
    Signature("wav", "wav", b"RIFF", None, 1000 * MB, check=_riff_wav),
    Signature("mp3", "mp3", b"ID3", None, 50 * MB, check=_mp3_id3, walk=mp3_walk),
    # MP3 without an ID3 tag: Layer III frame sync (MPEG-1, 2, 2.5; with/without CRC).
    # Two-byte magic is weak, so only sector-aligned starts are considered.
    *[Signature("mp3", "mp3", magic, None, 50 * MB, check=_mp3_frame, walk=mp3_walk, align=512)
      for magic in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3", b"\xff\xe2")],
    Signature("exe", "exe", b"MZ", None, 200 * MB, check=_pe),
]


MAX_HEADER_LEN = max(len(s.header) + s.offset for s in SIGNATURES)


def by_extension(ext: str) -> list[Signature]:
    ext = ext.lower().lstrip(".")
    return [s for s in SIGNATURES if s.extension == ext]
