"""Tests with synthetic data. Run: python -m unittest discover -s tests -v"""

import io
import os
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.carver import carve  # noqa: E402
from recovery.repair import repair_file, repair_png, repair_zip  # noqa: E402
from recovery.trash import TrashItem, restore  # noqa: E402


def make_png(width=200, height=200) -> bytes:
    def chunk(ctype: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)

    # random pixels so the compressed file is larger than the 1 KB minimum
    raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class CarveTests(unittest.TestCase):
    def test_carve_finds_png_pdf_zip_inside_noise(self):
        png = make_png()
        pdf = b"%PDF-1.4\n" + b"x" * 5000 + b"\n%%EOF\n"
        zipped = make_zip({"a.bin": os.urandom(5000)})
        image = os.urandom(300_000) + png + os.urandom(70_000) + pdf + os.urandom(50_000) + zipped
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "disk.img")
            with open(src, "wb") as fh:
                fh.write(image)
            out = os.path.join(tmp, "out")
            found = carve(src, out)
            kinds = sorted(r.kind for r in found)
            self.assertIn("png", kinds)
            self.assertIn("pdf", kinds)
            self.assertIn("zip", kinds)
            png_rec = next(r for r in found if r.kind == "png")
            self.assertEqual(png_rec.size, len(png))
            with open(png_rec.path, "rb") as fh:
                self.assertEqual(fh.read(), png)
            self.assertTrue(os.path.exists(os.path.join(out, "recovery_report.json")))

    def test_extension_filter(self):
        image = os.urandom(10_000) + make_png() + os.urandom(10_000) + b"%PDF-1.4\n%%EOF\n" + os.urandom(2000)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(image)
            found = carve(src, os.path.join(tmp, "o"), extensions=["png"])
            self.assertTrue(found)
            self.assertTrue(all(r.kind == "png" for r in found))


class RepairTests(unittest.TestCase):
    def test_png_truncated_gets_iend_and_keeps_valid_chunks(self):
        png = make_png(8, 8)
        truncated = png[:-12]  # remove IEND chunk
        fixed, actions = repair_png(truncated)
        self.assertIsNotNone(fixed)
        self.assertTrue(fixed.endswith(b"IEND\xaeB`\x82"))
        self.assertTrue(any("IEND" in a for a in actions))

    def test_png_bad_crc_is_cut(self):
        png = bytearray(make_png(8, 8))
        png[-1] ^= 0xFF  # corrupt IEND CRC
        fixed, actions = repair_png(bytes(png))
        self.assertIsNotNone(fixed)
        self.assertTrue(any("bad CRC" in a for a in actions))

    def test_zip_without_central_directory_is_rebuilt(self):
        content = {"word/document.xml": b"<w:t>Hasil skripsi</w:t>" * 50, "readme.txt": b"hi"}
        z = make_zip(content)
        broken = z[: z.index(b"PK\x01\x02")]  # drop central dir + EOCD
        fixed, actions = repair_zip(broken)
        self.assertIsNotNone(fixed)
        with zipfile.ZipFile(io.BytesIO(fixed)) as zf:
            self.assertTrue(zf.testzip() is None)
            for name, data in content.items():
                self.assertEqual(zf.read(name), data)

    def test_repair_file_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "img.png")
            with open(src, "wb") as fh:
                fh.write(make_png(8, 8)[:-12])
            res = repair_file(src, os.path.join(tmp, "fixed"))
            self.assertTrue(res.ok)
            self.assertTrue(os.path.exists(res.output))

    def test_unknown_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "x.bin")
            with open(src, "wb") as fh:
                fh.write(b"abc")
            self.assertFalse(repair_file(src, tmp).ok)


class TrashTests(unittest.TestCase):
    def test_restore_copies_and_avoids_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = os.path.join(tmp, "trashed.txt")
            with open(data_path, "w") as fh:
                fh.write("isi")
            item = TrashItem("trashed.txt", data_path, None, None, 3)
            dest = os.path.join(tmp, "dest")
            first = restore(item, dest)
            second = restore(item, dest)
            self.assertNotEqual(first, second)
            self.assertTrue(os.path.exists(data_path))  # original trash copy untouched


if __name__ == "__main__":
    unittest.main()
