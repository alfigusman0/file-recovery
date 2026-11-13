"""Regression tests for bugs found in the deep review. Run: python -m unittest discover -s tests -v"""

import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.carver import carve_summary  # noqa: E402
from recovery.formats import jpeg_scan_start  # noqa: E402
from recovery.fsmeta import safe_relative_path  # noqa: E402
from recovery.imaging import EXPORT_FIELDS, export_entries, image_source  # noqa: E402
from recovery.repair import repair_zip  # noqa: E402
from recovery.safety import UnsafeDestinationError, check_destination  # noqa: E402
from tests.test_recovery import make_png  # noqa: E402


def _jpeg_with_exif_thumbnail(image_body: bytes) -> bytes:
    """SOI + APP1 with an embedded thumbnail that itself ends in FFD9, then DQT, SOS, scan data, EOI."""
    thumb = b"\xff\xd8\xff\xd9"  # tiny embedded JPEG (SOI..EOI)
    exif_payload = b"Exif\x00\x00" + b"\x00" * 8 + thumb
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" * 65
    sos_header = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + app1 + dqt + sos_header + image_body + b"\xff\xd9"


def _zip_with_descriptors(files: dict[str, bytes]) -> bytes:
    """Write a zip where each entry uses a data descriptor (bit 3), as streaming writers do."""
    out = io.BytesIO()
    central = []
    for name, data in files.items():
        name_b = name.encode()
        offset = out.tell()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = comp.compress(data) + comp.flush()
        flags = 0x08
        out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 8, 0, 0, 0, 0, 0, len(name_b), 0))
        out.write(name_b)
        out.write(payload)
        out.write(struct.pack("<IIII", 0x08074B50, crc, len(payload), len(data)))
        central.append((name_b, crc, len(payload), len(data), flags, offset))
    cd_start = out.tell()
    for name_b, crc, csize, usize, flags, offset in central:
        out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 8, 0, 0,
                              crc, csize, usize, len(name_b), 0, 0, 0, 0, 0, offset))
        out.write(name_b)
    cd_size = out.tell() - cd_start
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), cd_size, cd_start, 0))
    return out.getvalue()


class ZipRepairRegression(unittest.TestCase):
    def test_inner_zip_bytes_are_not_split(self):
        inner = make_zip_bytes({"x.txt": b"inner content " * 50})
        archive = _zip_with_descriptors({"inner.zip": inner, "b.txt": b"hello"})
        broken = archive[: archive.index(b"PK\x01\x02")]  # drop central directory
        fixed, actions = repair_zip(broken)
        self.assertIsNotNone(fixed)
        with zipfile.ZipFile(io.BytesIO(fixed)) as zf:
            self.assertEqual(sorted(zf.namelist()), ["b.txt", "inner.zip"])
            self.assertEqual(zf.read("inner.zip"), inner)

    def test_corrupt_stored_entry_is_dropped_not_repaired(self):
        content = b"A" * 3000
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("a.bin", content)
            zf.writestr("b.bin", b"B" * 100)
        raw = bytearray(z.getvalue())
        raw[60] ^= 0xFF  # corrupt a byte inside a.bin's data
        broken = bytes(raw[: raw.index(b"PK\x01\x02")])
        fixed, actions = repair_zip(broken)
        self.assertTrue(any("CRC mismatch" in a for a in actions), actions)
        with zipfile.ZipFile(io.BytesIO(fixed)) as zf:
            self.assertEqual(zf.namelist(), ["b.bin"])


def make_zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class JpegRegression(unittest.TestCase):
    def test_exif_thumbnail_eoi_does_not_end_image(self):
        body = os.urandom(40_000).replace(b"\xff", b"\xfe")  # entropy data never contains bare FF
        jpeg = _jpeg_with_exif_thumbnail(body)
        scan = jpeg_scan_start(jpeg[:256 * 1024])
        self.assertIsNotNone(scan)
        self.assertGreater(scan, jpeg.index(b"\xff\xd9") + 1)  # thumbnail EOI is before SOS

    def test_carver_keeps_whole_jpeg(self):
        body = os.urandom(40_000).replace(b"\xff", b"\xfe")
        jpeg = _jpeg_with_exif_thumbnail(body)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(5000) + jpeg + os.urandom(5000))
            summary = carve_summary(src, os.path.join(tmp, "o"), extensions=["jpg"])
            self.assertEqual(len(summary.recovered), 1)
            self.assertEqual(summary.recovered[0].size, len(jpeg))


class PdfRegression(unittest.TestCase):
    def test_incremental_update_keeps_last_eof(self):
        pdf = b"%PDF-1.4\n" + b"1" * 3000 + b"\n%%EOF\n" + b"2" * 2000 + b"\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(4000) + pdf + os.urandom(4000))
            summary = carve_summary(src, os.path.join(tmp, "o"), extensions=["pdf"])
            self.assertEqual(len(summary.recovered), 1)
            self.assertEqual(summary.recovered[0].size, len(pdf))


class ZipCarveRegression(unittest.TestCase):
    def test_zip_is_one_file_not_one_per_entry(self):
        archive = make_zip_bytes({f"f{i}.bin": os.urandom(3000) for i in range(5)})
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(2000) + archive + os.urandom(100_000))
            summary = carve_summary(src, os.path.join(tmp, "o"), extensions=["zip"])
            self.assertEqual(len(summary.recovered), 1)
            self.assertEqual(summary.recovered[0].size, len(archive))
            self.assertGreaterEqual(summary.skipped_inside_other, 4)


class SafetyRegression(unittest.TestCase):
    def test_image_file_source_is_always_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "d.img")
            open(img, "wb").close()
            check_destination(img, tmp)

    def test_empty_destination_refused(self):
        with self.assertRaises(UnsafeDestinationError):
            check_destination("/dev/sdb", "")

    def test_unknown_windows_device_form_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        with self.assertRaises(UnsafeDestinationError):
            check_destination(r"\\.\Volume{1234}", r"D:\out")


class NameSafetyRegression(unittest.TestCase):
    def test_traversal_and_drive_components_are_neutralised(self):
        parts = safe_relative_path("D:Windows/../evil.dll")
        self.assertTrue(all(p not in ("..", "") and ":" not in p for p in parts), parts)

    def test_reserved_windows_names_are_renamed(self):
        self.assertEqual(safe_relative_path("a/NUL")[-1], "_NUL")
        self.assertEqual(safe_relative_path("a/con.txt")[-1], "_con.txt")

    def test_trailing_dots_and_spaces_removed(self):
        self.assertEqual(safe_relative_path("a/b. ")[-1], "b")


class ImagingRegression(unittest.TestCase):
    def test_image_size_matches_source_for_odd_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "s.bin")
            data = os.urandom(3_146_728)
            with open(src, "wb") as fh:
                fh.write(data)
            res = image_source(src, os.path.join(tmp, "c.img"))
            self.assertEqual(res.bytes_copied, len(data))
            self.assertEqual(res.bad_ranges, [])

    def test_empty_export_has_full_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_entries([], os.path.join(tmp, "empty.csv"))
            with open(path, encoding="utf-8-sig") as fh:
                header = fh.readline().strip().split(",")
            self.assertEqual(header, EXPORT_FIELDS)


if __name__ == "__main__":
    unittest.main()
