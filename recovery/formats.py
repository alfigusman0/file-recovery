"""Format-aware helpers shared by carving and repair."""

from __future__ import annotations

import struct
from typing import Optional

_NO_SEGMENT_LENGTH = (0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9)


def jpeg_scan_start(data: bytes) -> Optional[int]:
    """Offset where entropy-coded image data starts (just after SOS).

    Segments are skipped by their length field, so an EXIF thumbnail (which
    contains its own FFD9) is never mistaken for the end of the image.
    Returns None when the headers are incomplete within `data`.
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0xDA:  # SOS
            seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            return pos + 2 + seg_len
        if marker in _NO_SEGMENT_LENGTH:
            pos += 2
            continue
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if seg_len < 2:
            return None
        pos += 2 + seg_len
    return None
