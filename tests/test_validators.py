"""Format validators used by the carver (MP4/MOV, JPEG, MP3) and the confidence field.

Run: python -m unittest discover -s tests -v
"""

import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.carver import carve_summary  # noqa: E402
from recovery.formats import (  # noqa: E402
    jpeg_header_ok,
    mp3_frame_info,
    mp3_walk,
    mp4_ftyp_brand,
    mp4_walk,
)
from recovery.signatures import SIGNATURES  # noqa: E402


def box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + btype + payload


def make_mp4(brand: bytes = b"isom", mdat_len: int = 6000, with_moov: bool = True) -> bytes:
    ftyp = box(b"ftyp", brand + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    mdat = box(b"mdat", os.urandom(mdat_len))
    moov = box(b"moov", box(b"mvhd", b"\x00" * 100)) if with_moov else b""
    return ftyp + mdat + moov


def reader(data: bytes):
    return lambda off, n: data[off:off + n] if off >= 0 else b""


# MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding -> 417-byte frames.
MP3_HDR = b"\xff\xfb\x90\x64"
MP3_FRAME_LEN = 417


def make_mp3_frames(count: int) -> bytes:
    return b"".join(MP3_HDR + os.urandom(MP3_FRAME_LEN - 4).replace(b"\xff", b"\x00") for _ in range(count))


def make_jpeg(body_len: int = 5000, with_eoi: bool = True) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" * 65
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    body = os.urandom(body_len).replace(b"\xff", b"\xfe")
    return b"\xff\xd8" + app0 + dqt + sos + body + (b"\xff\xd9" if with_eoi else b"")


def write_image(tmp: str, data: bytes) -> str:
    src = os.path.join(tmp, "disk.img")
    with open(src, "wb") as fh:
        fh.write(data)
    return src


class Mp4ValidatorTests(unittest.TestCase):
    def test_valid_mp4_walks_to_end_and_is_complete(self):
        mp4 = make_mp4()
        data = mp4 + b"\x00" * 50_000
        self.assertEqual(mp4_walk(reader(data), len(data)), (len(mp4), True))

    def test_mp4_without_moov_is_incomplete(self):
        mp4 = make_mp4(with_moov=False)
        data = mp4 + b"\x00" * 4096
        end, complete = mp4_walk(reader(data), len(data))
        self.assertEqual(end, len(mp4))
        self.assertFalse(complete)

    def test_largesize_box(self):
        ftyp = box(b"ftyp", b"mp42" + b"\x00" * 4 + b"mp42isom")
        payload = os.urandom(3000)
        mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(payload)) + payload
        moov = box(b"moov", b"\x00" * 50)
        data = ftyp + mdat + moov + b"\x00" * 100
        self.assertEqual(mp4_walk(reader(data), len(data)), (len(data) - 100, True))

    def test_size_zero_box_extends_to_end(self):
        ftyp = box(b"ftyp", b"isom" + b"\x00" * 4 + b"isom")
        moov = box(b"moov", b"\x00" * 50)
        data = ftyp + moov + struct.pack(">I", 0) + b"mdat" + os.urandom(2000)
        self.assertEqual(mp4_walk(reader(data), len(data)), (len(data), True))

    def test_random_ftyp_at_offset_4_is_rejected(self):
        for _ in range(200):
            blob = os.urandom(4) + b"ftyp" + os.urandom(4088)
            if mp4_ftyp_brand(blob) is not None:  # astronomically unlikely
                continue
            self.assertIsNone(mp4_walk(reader(blob), len(blob)))

    def test_valid_ftyp_followed_by_garbage_is_truncated(self):
        ftyp = box(b"ftyp", b"isom" + b"\x00" * 4 + b"isom")
        data = ftyp + b"\x00\x00\x00\x05\x01\x02\x03\x04" + os.urandom(10_000)
        self.assertEqual(mp4_walk(reader(data), len(data)), (len(ftyp), False))

    def test_unknown_brand_and_bad_ftyp_size(self):
        self.assertIsNone(mp4_ftyp_brand(box(b"ftyp", b"zzzz" + b"\x00" * 8)))
        self.assertIsNone(mp4_ftyp_brand(box(b"ftyp", b"isom" + b"\x00" * 600)))
        self.assertIsNone(mp4_ftyp_brand(struct.pack(">I", 8) + b"ftypisom" + b"\x00" * 8))
        self.assertEqual(mp4_ftyp_brand(make_mp4()), b"isom")

    def test_box_beyond_available_data_stops_walk(self):
        mp4 = make_mp4()
        data = mp4[:-20]  # moov cut off
        end, complete = mp4_walk(reader(data), len(data))
        self.assertLess(end, len(data))
        self.assertFalse(complete)


class JpegValidatorTests(unittest.TestCase):
    def test_marker_after_soi(self):
        self.assertTrue(jpeg_header_ok(make_jpeg()))
        self.assertFalse(jpeg_header_ok(b"\xff\xd8\xff\xd9" + b"\x00" * 100))  # EOI right after SOI
        self.assertFalse(jpeg_header_ok(b"\xff\xd8\xff\x00" + b"\x00" * 100))
        bad_chain = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"\x00" * 14 + b"\x12\x34"
        self.assertFalse(jpeg_header_ok(bad_chain + b"\x00" * 100))

    def test_carve_jpeg_with_and_without_eoi(self):
        good = make_jpeg(8000)
        cut = make_jpeg(8000, with_eoi=False)
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, os.urandom(3000) + good + b"\x00" * 4000)
            s = carve_summary(src, os.path.join(tmp, "a"), extensions=["jpg"])
            self.assertEqual(len(s.recovered), 1)
            rec = s.recovered[0]
            self.assertEqual(rec.size, len(good))
            self.assertTrue(rec.complete)
            self.assertEqual(rec.confidence, "high")

            src = write_image(tmp, os.urandom(3000) + cut + b"\x00" * 4000)
            s = carve_summary(src, os.path.join(tmp, "b"), extensions=["jpg"])
            self.assertEqual(len(s.recovered), 1)
            self.assertFalse(s.recovered[0].complete)
            self.assertEqual(s.recovered[0].confidence, "low")


class Mp3ValidatorTests(unittest.TestCase):
    def test_frame_header(self):
        self.assertEqual(mp3_frame_info(MP3_HDR), (MP3_FRAME_LEN, 3, 0))
        self.assertIsNone(mp3_frame_info(b"\xff\x1b\x90\x64"))  # sync bits missing
        self.assertIsNone(mp3_frame_info(b"\xff\xfb\xf0\x64"))  # bad bitrate index
        self.assertIsNone(mp3_frame_info(b"\xff\xfb\x9c\x64"))  # reserved sample rate

    def test_bare_stream_walk(self):
        frames = make_mp3_frames(20)
        data = frames + b"\x00" * 1000
        self.assertEqual(mp3_walk(reader(data), len(data)), (len(frames), True))
        short = make_mp3_frames(3)
        self.assertIsNone(mp3_walk(reader(short), len(short)))

    def test_id3_tag_then_frames_and_id3v1(self):
        tag = b"ID3\x03\x00\x00\x00\x00\x00\x14" + b"\x00" * 20
        frames = make_mp3_frames(5)
        trailer = b"TAG" + b"\x00" * 125
        data = tag + frames + trailer + os.urandom(500)
        self.assertEqual(mp3_walk(reader(data), len(data)), (len(tag) + len(frames) + 128, True))

    def test_carve_bare_mp3(self):
        frames = make_mp3_frames(30)
        sig_check = next(s for s in SIGNATURES if s.name == "mp3" and s.header == b"\xff\xfb").check
        self.assertTrue(sig_check(frames[:4096]))
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, b"\x00" * 4096 + frames + b"\x00" * 8192)
            s = carve_summary(src, os.path.join(tmp, "o"), extensions=["mp3"])
            self.assertEqual(len(s.recovered), 1)
            self.assertEqual(s.recovered[0].size, len(frames))
            self.assertEqual(s.recovered[0].confidence, "high")


class CarveMp4EndToEnd(unittest.TestCase):
    def test_fake_and_real_mp4(self):
        real = make_mp4(mdat_len=20_000)
        # 50 false hits: random 4 bytes + 'ftyp' + random data, like the reported false positives
        fakes = b"".join(os.urandom(4) + b"ftyp" + os.urandom(20_000) for _ in range(50))
        image = os.urandom(10_000) + fakes + b"\x00" * 1000 + real + os.urandom(200_000)
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, image)
            out = os.path.join(tmp, "o")
            s = carve_summary(src, out, extensions=["mp4", "mov"])
            self.assertEqual(len(s.recovered), 1, [r.size for r in s.recovered])
            rec = s.recovered[0]
            self.assertEqual(rec.kind, "mp4")
            self.assertEqual(rec.size, len(real))
            self.assertEqual(os.path.getsize(rec.path), len(real))
            self.assertTrue(rec.complete)
            self.assertEqual(rec.confidence, "high")
            with open(rec.path, "rb") as fh:
                self.assertEqual(fh.read(), real)
            with open(os.path.join(out, "recovery_report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report[0]["confidence"], "high")
            for key in ("path", "kind", "source_offset", "size", "complete"):
                self.assertIn(key, report[0])

    def test_mov_brand_and_truncated_mp4(self):
        mov = make_mp4(brand=b"qt  ", mdat_len=5000)
        cut = make_mp4(mdat_len=5000, with_moov=False)
        image = os.urandom(3000) + mov + b"\x00" * 3000 + cut + b"\x00" * 3000
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, image)
            s = carve_summary(src, os.path.join(tmp, "o"), extensions=["mp4", "mov"])
            by_kind = {r.kind: r for r in s.recovered}
            self.assertEqual(set(by_kind), {"mov", "mp4"})
            self.assertEqual(by_kind["mov"].size, len(mov))
            self.assertEqual(by_kind["mov"].confidence, "high")
            self.assertEqual(by_kind["mp4"].size, len(cut))
            self.assertFalse(by_kind["mp4"].complete)
            self.assertEqual(by_kind["mp4"].confidence, "low")


class ConfidenceTests(unittest.TestCase):
    def test_png_footer_without_deep_check_is_medium(self):
        from tests.test_recovery import make_png
        png = make_png(60, 60)
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, os.urandom(2000) + png + os.urandom(2000))
            s = carve_summary(src, os.path.join(tmp, "o"), extensions=["png"])
            self.assertEqual(len(s.recovered), 1)
            self.assertEqual(s.recovered[0].confidence, "medium")


if __name__ == "__main__":
    unittest.main()
