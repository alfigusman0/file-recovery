"""Regression tests for the second review's confirmed bugs.

Run: python -m unittest discover -s tests -v
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import types
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery import carver, fsmeta, repair, trash  # noqa: E402
from recovery.carver import carve_summary  # noqa: E402
from recovery.formats import gif_walk, mp3_walk, mp4_walk  # noqa: E402
from tests.test_recovery import make_png  # noqa: E402
from tests.test_regressions import _zip_with_descriptors  # noqa: E402
from tests.test_validators import box, make_mp3_frames, make_mp4, reader, write_image  # noqa: E402


def make_gif(lzw_len: int = 5000) -> bytes:
    """GIF89a with a global color table, a GCE extension and one image whose LZW data contains 00 3B."""
    header = b"GIF89a" + struct.pack("<HH", 64, 64) + bytes([0x80 | 0x01, 0, 0])  # GCT of 4 entries
    gct = b"\x00\x00\x00\xff\xff\xff\x10\x20\x30\x40\x50\x60"
    gce = b"\x21\xf9\x04\x00\x00\x00\x00\x00"
    descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, 64, 64) + b"\x00"
    lzw = bytearray(os.urandom(lzw_len))
    for i in range(100, lzw_len - 2, 700):
        lzw[i:i + 2] = b"\x00\x3b"  # the false "footer" inside image data
    blocks = b"".join(bytes([len(lzw[i:i + 255])]) + bytes(lzw[i:i + 255]) for i in range(0, lzw_len, 255))
    return header + gct + gce + descriptor + b"\x02" + blocks + b"\x00" + b"\x3b"


def gzip_header() -> bytes:
    return b"\x1f\x8b\x08\x00" + b"\x00" * 4 + b"\x00\x03"  # flags 0, OS = Unix


def files_in(folder: str) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n != "recovery_report.json")


# 1 ------------------------------------------------------------------------
class GuessedEndDoesNotHideFiles(unittest.TestCase):
    def test_gzip_guess_does_not_swallow_png_and_gif(self):
        png, gif = make_png(), make_gif()
        image = gzip_header() + os.urandom(20_000) + png + os.urandom(3000) + gif + os.urandom(250_000)
        with tempfile.TemporaryDirectory() as tmp:
            s = carve_summary(write_image(tmp, image), os.path.join(tmp, "o"), extensions=["gz", "png", "gif"])
            by_kind = {r.kind: r for r in s.recovered}
            self.assertIn("png", by_kind)
            self.assertIn("gif", by_kind)
            self.assertEqual(by_kind["png"].size, len(png))
            self.assertEqual(by_kind["gif"].size, len(gif))
            self.assertEqual(by_kind["gzip"].confidence, "low")


# 2 ------------------------------------------------------------------------
class _BadSectorFile:
    """BytesIO that raises OSError for any read touching [bad, bad + 4096)."""

    def __init__(self, data: bytes, bad: int) -> None:
        self._io = io.BytesIO(data)
        self.bad = bad

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._io.close()

    def seek(self, pos, whence=0):
        return self._io.seek(pos, whence)

    def read(self, n):
        pos = self._io.tell()
        if pos < self.bad + 4096 and pos + n > self.bad:
            raise OSError(23, "Data error (cyclic redundancy check)")
        return self._io.read(n)


class UnreadableSectorTests(unittest.TestCase):
    def test_bad_sector_is_zero_filled_and_report_written(self):
        png = make_png()
        image = os.urandom(8192) + b"\x00" * 8192 + os.urandom(4096) + png + os.urandom(8192)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            with mock.patch.object(carver, "_open_source", lambda _src: _BadSectorFile(image, 8192)):
                s = carve_summary("fake-source", out, extensions=["png"])
            self.assertFalse(s.cancelled)
            self.assertEqual([r.size for r in s.recovered], [len(png)])
            with open(os.path.join(out, "recovery_report.json"), encoding="utf-8") as fh:
                self.assertEqual(len(json.load(fh)), 1)

    def test_report_written_on_unexpected_error(self):
        png = make_png()
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, os.urandom(4096) + png + os.urandom(4096))
            out = os.path.join(tmp, "o")
            with mock.patch.object(carver, "extract_file", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    carve_summary(src, out, extensions=["png"])
            self.assertTrue(os.path.exists(os.path.join(out, "recovery_report.json")))


# 3 ------------------------------------------------------------------------
class Mp3LimitTests(unittest.TestCase):
    def test_stream_cut_by_limit_is_incomplete(self):
        frames = make_mp3_frames(20)
        avail = len(frames) - 100  # limit falls inside the last frame
        end, complete = mp3_walk(reader(frames), avail)
        self.assertFalse(complete)
        self.assertLessEqual(end, avail)

    def test_stream_ending_before_limit_is_complete(self):
        frames = make_mp3_frames(20)
        data = frames + b"\x00" * 1000
        self.assertEqual(mp3_walk(reader(data), len(data)), (len(frames), True))


# 4 ------------------------------------------------------------------------
class Mp4LimitTests(unittest.TestCase):
    def test_box_past_limit_makes_file_incomplete(self):
        mp4 = make_mp4()
        tail = struct.pack(">I", 100_000) + b"free" + b"\x00" * 1000  # box claims more than is there
        data = mp4 + tail
        end, complete = mp4_walk(reader(data), len(data))
        self.assertEqual(end, len(mp4))
        self.assertFalse(complete)

    def test_moov_then_end_is_complete(self):
        mp4 = make_mp4() + box(b"free", b"\x00" * 10)
        self.assertEqual(mp4_walk(reader(mp4), len(mp4)), (len(mp4), True))


# 5 ------------------------------------------------------------------------
class GifWalkTests(unittest.TestCase):
    def test_walk_ignores_00_3b_inside_lzw(self):
        gif = make_gif()
        self.assertIn(b"\x00\x3b", gif[:-2])
        data = gif + os.urandom(2000)
        self.assertEqual(gif_walk(reader(data), len(data)), (len(gif), True))

    def test_truncated_gif_is_incomplete(self):
        gif = make_gif()
        end, complete = gif_walk(reader(gif[:3000]), 3000)
        self.assertFalse(complete)
        self.assertLessEqual(end, 3000)

    def test_bad_header_rejected(self):
        self.assertIsNone(gif_walk(reader(b"GIF89a" + b"\x00" * 3), 9))

    def test_carved_gif_is_whole(self):
        gif = make_gif()
        with tempfile.TemporaryDirectory() as tmp:
            s = carve_summary(write_image(tmp, os.urandom(4000) + gif + os.urandom(4000)),
                              os.path.join(tmp, "o"), extensions=["gif"])
            self.assertEqual(len(s.recovered), 1)
            rec = s.recovered[0]
            self.assertEqual(rec.size, len(gif))
            self.assertTrue(rec.complete)
            with open(rec.path, "rb") as fh:
                self.assertEqual(fh.read(), gif)


# 6 ------------------------------------------------------------------------
class _FakeTskFile:
    def __init__(self, data: bytes, cancel: threading.Event) -> None:
        self.data, self.cancel = data, cancel

    def read_random(self, offset, n):
        self.cancel.set()  # user cancels while the first block is being read
        return self.data[offset:offset + n]


class FsCancelTests(unittest.TestCase):
    def test_cancel_mid_file_is_not_success(self):
        cancel = threading.Event()
        size = fsmeta.READ_BLOCK * 3
        handle = types.SimpleNamespace(open_meta=lambda inode: _FakeTskFile(os.urandom(size), cancel))
        entry = fsmeta.DeletedEntry("Docs/big.bin", "big.bin", size, 5, "FAT32", None, None, None,
                                    fs_handle=handle)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fsmeta, "_require_tsk", lambda: None):
                results = fsmeta.recover_entries("fake", [entry], tmp, cancel=cancel)
            self.assertEqual(len(results), 1)
            dest, written, err = results[0]
            self.assertIsNotNone(err)
            self.assertEqual(written, 0)
            self.assertFalse(os.path.exists(dest))


# 7 ------------------------------------------------------------------------
class CarveCancelCleanupTests(unittest.TestCase):
    def _cancel_during_write(self, image: bytes, ext: str):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o")
            s = carve_summary(write_image(tmp, image), out, extensions=[ext], cancel=cancel,
                              phase=lambda *_a: cancel.set())
            self.assertTrue(s.cancelled)
            self.assertEqual(s.recovered, [])
            self.assertEqual(files_in(out), [])

    def test_footer_format(self):
        self._cancel_during_write(os.urandom(4096) + make_png() + os.urandom(4096), "png")

    def test_walked_format(self):
        self._cancel_during_write(os.urandom(4096) + make_gif() + os.urandom(4096), "gif")

    def test_copy_range_removes_partial_file(self):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            src = write_image(tmp, os.urandom(carver.EXTRACT_BLOCK * 2))
            out_path = os.path.join(tmp, "part.bin")
            real_read_at = carver.read_at

            def read_then_cancel(f, pos, size):
                data = real_read_at(f, pos, size)
                cancel.set()
                return data

            with open(src, "rb") as f, mock.patch.object(carver, "read_at", read_then_cancel):
                with self.assertRaises(carver.CancelledError):
                    carver._copy_range(f, 0, carver.EXTRACT_BLOCK * 2, out_path, cancel)
            self.assertFalse(os.path.exists(out_path))


# 8 ------------------------------------------------------------------------
class SqliteOriginalUntouchedTests(unittest.TestCase):
    def test_hot_journal_original_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live.db")
            con = sqlite3.connect(live, isolation_level=None)
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
            con.execute("PRAGMA synchronous=OFF")
            con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 200) for i in range(300)])
            con.execute("PRAGMA cache_size=2")  # force dirty pages to spill into the db file
            con.execute("BEGIN")
            con.execute("UPDATE t SET b = ?", ("y" * 200,))
            snap = os.path.join(tmp, "snap.db")
            shutil.copyfile(live, snap)
            journal = live + "-journal"
            has_journal = os.path.exists(journal) and os.path.getsize(journal) > 0
            if has_journal:
                shutil.copyfile(journal, snap + "-journal")
            con.execute("ROLLBACK")
            con.close()
            if not has_journal:
                self.skipTest("SQLite did not leave a journal to test with")

            def digest(p):
                with open(p, "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()

            before = (digest(snap), digest(snap + "-journal"))
            result = repair.repair_file(snap, os.path.join(tmp, "out"))
            self.assertEqual((digest(snap), digest(snap + "-journal")), before)
            self.assertTrue(result.ok, result.actions)


# 9 ------------------------------------------------------------------------
class TrashDirectoryRestoreTests(unittest.TestCase):
    def test_restore_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "$RABC123")
            os.makedirs(os.path.join(folder, "sub"))
            with open(os.path.join(folder, "a.txt"), "wb") as fh:
                fh.write(b"aaa")
            with open(os.path.join(folder, "sub", "b.txt"), "wb") as fh:
                fh.write(b"bbb")
            item = trash.TrashItem("Project", folder, r"C:\Users\x\Project", None, 0)
            dest = os.path.join(tmp, "dest")
            first = trash.restore(item, dest)
            second = trash.restore(item, dest)
            self.assertNotEqual(first, second)
            with open(os.path.join(first, "sub", "b.txt"), "rb") as fh:
                self.assertEqual(fh.read(), b"bbb")
            self.assertTrue(os.path.isfile(os.path.join(second, "a.txt")))

    def test_per_file_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Trash", "files", "dir")
            os.makedirs(folder)
            item = trash.TrashItem("dir", folder, None, None, 0)
            err = shutil.Error([(os.path.join(folder, "x"), "dst", "Permission denied")])
            with mock.patch.object(trash.shutil, "copytree", side_effect=err):
                with self.assertRaises(OSError) as ctx:
                    trash.restore(item, os.path.join(tmp, "dest"))
            self.assertIsInstance(ctx.exception, trash.RestoreError)
            self.assertEqual(len(ctx.exception.failures), 1)


# 10 -----------------------------------------------------------------------
def _jpeg_with_big_header(app_segments: int) -> bytes:
    thumb = b"\xff\xd8\xff\xd9"
    exif = b"Exif\x00\x00" + b"\x00" * 8 + thumb
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    big = b"".join(b"\xff\xe2" + struct.pack(">H", 65535) + b"\x00" * 65533 for _ in range(app_segments))
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" * 65
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    body = os.urandom(30_000).replace(b"\xff", b"\xfe")
    return b"\xff\xd8" + app1 + big + dqt + sos + body + b"\xff\xd9"


class JpegLongHeaderTests(unittest.TestCase):
    def test_header_longer_than_scan_head_is_read_further(self):
        jpeg = _jpeg_with_big_header(5)  # ~320 KB of header segments
        self.assertGreater(jpeg.index(b"\xff\xda"), carver.SCAN_HEAD)
        with tempfile.TemporaryDirectory() as tmp:
            s = carve_summary(write_image(tmp, os.urandom(4096) + jpeg + os.urandom(4096)),
                              os.path.join(tmp, "o"), extensions=["jpg"])
            jpgs = [r for r in s.recovered if r.source_offset == 4096]
            self.assertEqual(len(jpgs), 1)
            self.assertEqual(jpgs[0].size, len(jpeg))
            self.assertEqual(jpgs[0].confidence, "high")

    def test_unknown_scan_start_is_low_confidence(self):
        jpeg = _jpeg_with_big_header(5)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(carver, "SCAN_HEAD_MAX", carver.SCAN_HEAD):
            s = carve_summary(write_image(tmp, os.urandom(4096) + jpeg + os.urandom(4096)),
                              os.path.join(tmp, "o"), extensions=["jpg"])
            first = [r for r in s.recovered if r.source_offset == 4096][0]
            self.assertLess(first.size, len(jpeg))  # cut at the thumbnail's EOI
            self.assertEqual(first.confidence, "low")


# 11 -----------------------------------------------------------------------
class InflateTests(unittest.TestCase):
    def test_inflate_exact_with_memoryview_and_trailing_data(self):
        payload = os.urandom(300_000)
        comp = zlib.compressobj(6, zlib.DEFLATED, -15)
        raw = comp.compress(payload) + comp.flush()
        buf = raw + b"PK\x07\x08" + os.urandom(500_000)
        content, consumed = repair._inflate_exact(memoryview(buf))
        self.assertEqual(content, payload)
        self.assertEqual(consumed, len(raw))
        self.assertEqual(repair._inflate_exact(raw[:-10]), (None, 0))

    def test_descriptor_archive_still_repairs(self):
        files = {f"f{i}.bin": os.urandom(100_000) for i in range(4)}
        archive = _zip_with_descriptors(files)
        broken = archive[: archive.index(b"PK\x01\x02")]
        fixed, _actions = repair.repair_zip(broken)
        import zipfile
        with zipfile.ZipFile(io.BytesIO(fixed)) as zf:
            self.assertEqual({n: zf.read(n) for n in zf.namelist()}, files)


# 13 -----------------------------------------------------------------------
class ZipOver4GBTests(unittest.TestCase):
    def test_struct_error_is_a_clean_failure(self):
        archive = _zip_with_descriptors({"a.txt": b"hello" * 100})
        broken = archive[: archive.index(b"PK\x01\x02")]
        with mock.patch.object(repair.struct, "pack_into", side_effect=struct.error("out of range")):
            fixed, actions = repair.repair_zip(broken)
        self.assertIsNone(fixed)
        self.assertTrue(any("ZIP64" in a for a in actions), actions)


if __name__ == "__main__":
    unittest.main()
