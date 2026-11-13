"""Format-aware helpers shared by carving and repair."""

from __future__ import annotations

import struct
from typing import Callable, Optional

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


# ---------------------------------------------------------------------------
# Structure validators used by the carver.
#
# A "walker" gets read(rel_offset, n) -> bytes (relative to the candidate start,
# short or empty past the available data) and `avail` (bytes that may belong to
# the file). It returns None to reject the candidate, or (end, complete) where
# `end` is the relative offset where the file really ends.
# ---------------------------------------------------------------------------

ReadFn = Callable[[int, int], bytes]

# Markers that may directly follow SOI (FF D8): APPn, DQT, DHT, SOF0-3, DRI, COM.
_JPEG_FIRST_MARKERS = frozenset(range(0xE0, 0xF0)) | {0xDB, 0xC4, 0xC0, 0xC1, 0xC2, 0xC3, 0xDD, 0xFE}
# Markers that must not appear between SOI and SOS.
_JPEG_BAD_BEFORE_SOS = frozenset({0x00, 0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8)))


def jpeg_header_ok(head: bytes) -> bool:
    """SOI followed by a plausible marker, and a consistent segment chain within `head`.

    Running out of `head` before SOS is accepted (EXIF/ICC segments can be large).
    """
    if len(head) < 4 or head[:3] != b"\xff\xd8\xff":
        return False
    if head[3] not in _JPEG_FIRST_MARKERS:
        return False
    pos = 2
    while pos + 4 <= len(head):
        if head[pos] != 0xFF:
            return False
        marker = head[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0xDA:  # SOS: header chain is consistent
            return True
        if marker in _JPEG_BAD_BEFORE_SOS:
            return False
        seg_len = struct.unpack(">H", head[pos + 2:pos + 4])[0]
        if seg_len < 2:
            return False
        pos += 2 + seg_len
    return True


# --- ISO-BMFF (MP4 / MOV / 3GP) -------------------------------------------

MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7", b"iso8", b"iso9",
    b"mp41", b"mp42", b"mp71", b"avc1", b"hvc1", b"dash", b"msnv", b"MSNV", b"mmp4",
    b"M4V ", b"M4VH", b"M4VP", b"M4A ", b"M4B ", b"M4P ", b"F4V ", b"F4P ", b"f4v ",
    b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3gp8", b"3gp9", b"3gg6", b"3g2a", b"3g2b", b"3g2c",
    b"XAVC", b"NDAS", b"ndas", b"kddi", b"KDDI", b"caqv", b"qt  ",
})
MOV_BRANDS = frozenset({b"qt  "})

# Top-level box types that may legitimately be large; unknown printable types
# are only followed while they stay small (text data often looks "printable").
_BMFF_KNOWN_TOP = frozenset({
    b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"udta", b"meta", b"uuid",
    b"moof", b"mfra", b"sidx", b"ssix", b"styp", b"emsg", b"prft", b"pdin", b"pnot",
    b"junk", b"bloc", b"PICT", b"Xtra",
})
_BMFF_UNKNOWN_MAX = 16 * 1024 * 1024


def _printable4(t: bytes) -> bool:
    return len(t) == 4 and all(0x20 <= c <= 0x7E for c in t)


def mp4_ftyp_brand(head: bytes) -> Optional[bytes]:
    """Major brand when `head` starts with a plausible ftyp box (16..512 bytes), else None."""
    if len(head) < 16 or head[4:8] != b"ftyp":
        return None
    size = struct.unpack(">I", head[0:4])[0]
    if not 16 <= size <= 512 or size % 4:
        return None
    brand = head[8:12]
    return brand if brand in MP4_BRANDS else None


def mp4_walk(read: ReadFn, avail: int) -> Optional[tuple[int, bool]]:
    """Walk top-level boxes from the start. Complete only when a 'moov' box was seen."""
    head = read(0, 16)
    if mp4_ftyp_brand(head) is None:
        return None
    pos = struct.unpack(">I", head[0:4])[0]
    if pos > avail:
        return None
    seen_moov = False
    cut_short = False  # True when a box runs past the available data
    while pos + 8 <= avail:
        h = read(pos, 16)
        if len(h) < 8:
            break
        size = struct.unpack(">I", h[0:4])[0]
        btype = h[4:8]
        if not _printable4(btype):
            break
        to_end = False
        if size == 1:  # 64-bit largesize follows the type
            if len(h) < 16:
                break
            size = struct.unpack(">Q", h[8:16])[0]
            if size < 16:
                break
        elif size == 0:  # box extends to the end of the file
            size = avail - pos
            to_end = True
        elif size < 8:
            break
        if btype not in _BMFF_KNOWN_TOP and size > _BMFF_UNKNOWN_MAX:
            break
        if pos + size > avail:  # box runs past the data we may use: stop at the last whole box
            cut_short = True
            break
        pos += size
        if btype == b"moov":
            seen_moov = True
        if to_end:
            break
    return pos, seen_moov and not cut_short


# --- MPEG audio (MP3) -----------------------------------------------------

_MP3_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mp3_frame_info(h: bytes) -> Optional[tuple[int, int, int]]:
    """Parse an MPEG audio Layer III frame header.

    Returns (frame_length, version_bits, sample_rate_index) or None. Requires the
    11-bit frame sync, a valid version, Layer III, a non-free/non-bad bitrate and
    a valid sample rate.
    """
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    version = (h[1] >> 3) & 0x03
    layer = (h[1] >> 1) & 0x03
    if version == 1 or layer != 1:  # reserved version / not Layer III
        return None
    br_idx = h[2] >> 4
    sr_idx = (h[2] >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = (_MP3_BITRATES_V1 if version == 3 else _MP3_BITRATES_V2)[br_idx] * 1000
    rate = _MP3_RATES[version][sr_idx]
    padding = (h[2] >> 1) & 0x01
    coef = 144 if version == 3 else 72
    return coef * bitrate // rate + padding, version, sr_idx


def mp3_frames_ok(data: bytes, pos: int = 0, need: int = 3) -> bool:
    """`need` consecutive, consistent frames starting at `pos` (chains running off the end of `data` pass)."""
    first = None
    for _ in range(need):
        if pos + 4 > len(data):
            return first is not None
        info = mp3_frame_info(data[pos:pos + 4])
        if info is None:
            return False
        if first is None:
            first = info[1:]
        elif info[1:] != first:
            return False
        pos += info[0]
    return True


def id3v2_size(head: bytes) -> Optional[int]:
    """Total ID3v2 tag size (header + body + optional footer), or None if invalid."""
    if len(head) < 10 or head[:3] != b"ID3" or head[3] not in (2, 3, 4) or head[4] != 0:
        return None
    if any(b & 0x80 for b in head[6:10]):  # syncsafe integer
        return None
    body = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
    return 10 + body + (10 if head[5] & 0x10 else 0)


MP3_MIN_FRAMES_BARE = 8   # bare frame-sync hit: demand a real stream
MP3_MAX_ID3_PADDING = 4096


def mp3_walk(read: ReadFn, avail: int) -> Optional[tuple[int, bool]]:
    """Skip an ID3v2 tag (if any), then follow MPEG frames to the end of the stream.

    An ID3v1 'TAG' trailer (128 bytes) right after the last frame is included.
    Bare streams (no ID3v2) need MP3_MIN_FRAMES_BARE frames, tagged ones 2.
    """
    head = read(0, 10)
    pos = 0
    min_frames = MP3_MIN_FRAMES_BARE
    if head[:3] == b"ID3":
        tag = id3v2_size(head)
        if tag is None or tag > avail:
            return None
        min_frames = 2
        # Encoders sometimes leave zero padding after the declared tag size.
        window = read(tag, MP3_MAX_ID3_PADDING + 4)
        rel = 0
        while rel + 4 <= len(window) and mp3_frame_info(window[rel:rel + 4]) is None:
            rel += 1
        if rel + 4 > len(window):
            return None
        pos = tag + rel
    first = None
    frames = 0
    hit_limit = False  # stopped because the available data ran out, not at the stream's end
    while True:
        if pos + 4 > avail:
            hit_limit = True
            break
        info = mp3_frame_info(read(pos, 4))
        if info is None:
            break
        if first is None:
            first = info[1:]
        elif info[1:] != first:
            break
        if pos + info[0] > avail:
            hit_limit = True
            break
        pos += info[0]
        frames += 1
    if frames < min_frames:
        return None
    if not hit_limit and pos + 128 <= avail and read(pos, 3) == b"TAG":
        pos += 128
    return pos, not hit_limit


# --- GIF ------------------------------------------------------------------

def _gif_skip_sub_blocks(read: ReadFn, pos: int, avail: int) -> Optional[int]:
    """Skip data sub-blocks (length byte + data) up to the 0x00 terminator.

    Returns the offset after the terminator, or None when the data runs out.
    """
    while True:
        if pos >= avail:
            return None
        n = read(pos, 1)
        if not n:
            return None
        length = n[0]
        pos += 1
        if length == 0:
            return pos
        pos += length


def gif_walk(read: ReadFn, avail: int) -> Optional[tuple[int, bool]]:
    """Walk the GIF block structure to the trailer (0x3B).

    The 00 3B byte pair also occurs inside LZW image data, so a footer search
    cuts files short; following the blocks finds the real end. Returns None
    for a bad header, (end, True) at the trailer, and (end_of_last_whole_block,
    False) when the structure breaks, or (avail, False) when the data runs out
    inside a block (a truncated GIF still shows its first part).
    """
    head = read(0, 13)
    if len(head) < 13 or head[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    packed = head[10]
    pos = 13
    if packed & 0x80:  # global color table
        pos += 3 * (1 << ((packed & 0x07) + 1))
    if pos > avail:
        return None
    last_good = pos
    images = 0
    while pos < avail:
        b = read(pos, 1)
        if not b:
            break
        block = b[0]
        if block == 0x3B:  # trailer
            return pos + 1, True
        if block == 0x21:  # extension: label byte + sub-blocks
            if pos + 2 > avail:
                break
            nxt = _gif_skip_sub_blocks(read, pos + 2, avail)
        elif block == 0x2C:  # image descriptor (9 bytes) [+ local color table] + LZW data
            desc = read(pos + 1, 9)
            if len(desc) < 9:
                break
            p = pos + 10
            if desc[8] & 0x80:
                p += 3 * (1 << ((desc[8] & 0x07) + 1))
            if p >= avail:
                break
            code_size = read(p, 1)
            if not code_size or not 1 <= code_size[0] <= 12:
                break  # not a valid LZW minimum code size: corrupt
            nxt = _gif_skip_sub_blocks(read, p + 1, avail)
            if nxt is not None:
                images += 1
        else:
            break  # unknown block: corrupt or overwritten
        if nxt is None or nxt > avail:
            # Data ran out inside a block (truncated file): keep what is there.
            return avail, False
        pos = last_good = nxt
    if images == 0:
        return None
    return last_good, False
