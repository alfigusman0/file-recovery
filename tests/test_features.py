"""Tests for dates, cancel, imaging, export, safety and file system recovery.

Run: python -m unittest discover -s tests -v
"""

import datetime as dt
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.carver import carve_summary  # noqa: E402
from recovery.dates import DateRange, make_range, parse_date  # noqa: E402
from recovery.fsmeta import HAVE_TSK, scan_deleted, recover_entries  # noqa: E402
from recovery.imaging import export_entries, image_source, verify_image  # noqa: E402
from recovery.safety import UnsafeDestinationError, check_destination  # noqa: E402
from recovery.trash import TrashItem, filter_by_deletion  # noqa: E402
from tests.test_recovery import make_png  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fat32_deleted.img")


class DateTests(unittest.TestCase):
    def test_empty_range_matches_everything(self):
        r = make_range("", "")
        self.assertFalse(r.active)
        self.assertTrue(r.contains(None))
        self.assertTrue(r.contains(dt.datetime(1990, 1, 1)))

    def test_inclusive_range(self):
        r = make_range("2026-01-01", "2026-11-30")
        self.assertTrue(r.contains(dt.datetime(2026, 1, 1, 0, 0)))
        self.assertTrue(r.contains(dt.datetime(2026, 11, 30, 23, 59)))
        self.assertFalse(r.contains(dt.datetime(2025, 12, 31, 23, 59)))
        self.assertFalse(r.contains(dt.datetime(2026, 12, 1)))
        self.assertFalse(r.contains(None))

    def test_bad_format_and_reversed_range(self):
        with self.assertRaises(ValueError):
            parse_date("01-01-2026")
        with self.assertRaises(ValueError):
            make_range("2026-11-30", "2026-01-01")

    def test_trash_filter_by_deletion_date(self):
        items = [
            TrashItem("a.txt", "x", None, dt.datetime(2026, 5, 1), 1),
            TrashItem("b.txt", "x", None, dt.datetime(2024, 5, 1), 1),
            TrashItem("c.txt", "x", None, None, 1),
        ]
        kept, skipped = filter_by_deletion(items, make_range("2026-01-01", "2026-11-30"))
        self.assertEqual([i.name for i in kept], ["a.txt"])
        self.assertEqual(skipped, 1)
        kept_all, _ = filter_by_deletion(items, DateRange())
        self.assertEqual(len(kept_all), 3)


class CarveOptionsTests(unittest.TestCase):
    def test_date_filter_skips_png_without_embedded_date(self):
        png = make_png()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(100_000) + png + os.urandom(10_000))
            summary = carve_summary(src, os.path.join(tmp, "o"), date_range=make_range("2026-01-01", "2026-11-30"))
            self.assertEqual(summary.recovered, [])
            self.assertGreaterEqual(summary.skipped_by_date, 1)  # random bytes may add other hits

    def test_cancel_stops_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(2_000_000))
            cancel = threading.Event()
            cancel.set()
            summary = carve_summary(src, os.path.join(tmp, "o"), cancel=cancel)
            self.assertTrue(summary.cancelled)


class ImagingTests(unittest.TestCase):
    def test_image_copy_matches_source_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.bin")
            data = os.urandom(3_000_000)
            with open(src, "wb") as fh:
                fh.write(data)
            dest = os.path.join(tmp, "img", "copy.img")
            result = image_source(src, dest)
            self.assertEqual(result.bytes_copied, len(data))
            self.assertEqual(result.bad_ranges, [])
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), data)
            self.assertTrue(verify_image(dest, result.sha256))

    def test_cancel_during_imaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.bin")
            with open(src, "wb") as fh:
                fh.write(os.urandom(3_000_000))
            cancel = threading.Event()
            cancel.set()
            result = image_source(src, os.path.join(tmp, "copy.img"), cancel=cancel)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.bytes_copied, 0)


class SafetyTests(unittest.TestCase):
    def test_same_drive_letter_is_refused_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        with self.assertRaises(UnsafeDestinationError):
            check_destination(r"\\.\C:", r"C:\recovered")

    def test_empty_destination_is_refused(self):
        with self.assertRaises(UnsafeDestinationError):
            check_destination(r"\\.\C:", "")

    def test_image_file_source_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "d.img")
            open(src, "wb").close()
            check_destination(src, tmp)  # must not raise


class ExportTests(unittest.TestCase):
    def test_export_csv_and_json(self):
        if not HAVE_TSK:
            self.skipTest("pytsk3 not installed")
        entries = scan_deleted(FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = export_entries(entries, os.path.join(tmp, "list.csv"))
            json_path = export_entries(entries, os.path.join(tmp, "list.json"))
            with open(csv_path, encoding="utf-8-sig") as fh:
                self.assertIn("path", fh.readline())
            with open(json_path, encoding="utf-8") as fh:
                self.assertIn("cv.txt", fh.read())


@unittest.skipUnless(HAVE_TSK, "pytsk3 not installed")
class FileSystemRecoveryTests(unittest.TestCase):
    def test_finds_deleted_files_with_names_and_paths(self):
        entries = scan_deleted(FIXTURE)
        paths = sorted(e.path for e in entries)
        self.assertIn("Docs/cv.txt", paths)
        self.assertIn("foto.bin", paths)
        cv = next(e for e in entries if e.name == "cv.txt")
        self.assertEqual(cv.size, len(b"CV isi dokumen penting\n"))
        self.assertEqual(cv.fs_type, "FAT32")
        self.assertIsNotNone(cv.deleted_at)

    def test_recovered_content_is_identical(self):
        entries = [e for e in scan_deleted(FIXTURE) if e.name == "cv.txt"]
        with tempfile.TemporaryDirectory() as tmp:
            results = recover_entries(FIXTURE, entries, tmp)
            dest, written, err = results[0]
            self.assertIsNone(err)
            self.assertEqual(written, len(b"CV isi dokumen penting\n"))
            with open(os.path.join(tmp, "Docs", "cv.txt"), "rb") as fh:
                self.assertEqual(fh.read(), b"CV isi dokumen penting\n")

    def test_date_range_on_deleted_scan(self):
        entries = scan_deleted(FIXTURE, date_range=make_range("2000-01-01", "2000-12-31"))
        self.assertEqual(entries, [])

    def test_random_image_has_no_file_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "r.img")
            with open(src, "wb") as fh:
                fh.write(os.urandom(2 * 1024 * 1024))
            self.assertEqual(scan_deleted(src), [])


if __name__ == "__main__":
    unittest.main()
