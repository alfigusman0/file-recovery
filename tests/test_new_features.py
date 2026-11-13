"""Tests for progress tracking, flash-drive diagnosis and repair rules. Run: python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.flashrepair import diagnose, repair_command, surface_scan  # noqa: E402
from recovery.progress import ProgressTracker, format_bytes, format_duration  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fat32_deleted.img")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def test_percent_and_eta(self):
        clock = FakeClock()
        t = ProgressTracker(1000, clock=clock)
        clock.now = 10.0
        t.update(250)
        self.assertAlmostEqual(t.percent, 25.0)
        self.assertAlmostEqual(t.speed, 25.0)           # 250 bytes in 10 s
        self.assertAlmostEqual(t.eta_seconds, 30.0)     # 750 bytes left at 25 B/s
        self.assertIn("25.0%", t.status_text())

    def test_no_speed_or_eta_for_very_short_window(self):
        clock = FakeClock()
        t = ProgressTracker(1000, clock=clock)
        clock.now = 0.1
        t.update(500)
        self.assertEqual(t.speed, 0.0)
        self.assertIsNone(t.eta_seconds)

    def test_unknown_total_has_no_percent(self):
        clock = FakeClock()
        t = ProgressTracker(0, clock=clock)
        clock.now = 5.0
        t.update(10)
        self.assertIsNone(t.percent)

    def test_percent_never_exceeds_100(self):
        clock = FakeClock()
        t = ProgressTracker(100, clock=clock)
        clock.now = 1.0
        t.update(250)
        self.assertEqual(t.percent, 100.0)

    def test_formatting(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(3 * 1024 * 1024), "3.0 MB")
        self.assertEqual(format_duration(45), "45 dtk")
        self.assertEqual(format_duration(125), "2 mnt 05 dtk")
        self.assertEqual(format_duration(3700), "1 jam 01 mnt")
        self.assertEqual(format_duration(3720), "1 jam 02 mnt")


@unittest.skipUnless(os.path.exists(FIXTURE), "fixture missing")
class FlashDiagnosisTests(unittest.TestCase):
    def test_fat32_image_is_ok(self):
        d = diagnose(FIXTURE)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.file_system, "FAT32")

    def test_all_zero_device_is_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zero.img")
            with open(path, "wb") as fh:
                fh.write(bytes(2 * 1024 * 1024))
            self.assertEqual(diagnose(path).status, "raw")

    def test_random_device_is_unreadable_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rnd.img")
            with open(path, "wb") as fh:
                fh.write(os.urandom(2 * 1024 * 1024))
            self.assertIn(diagnose(path).status, ("unreadable", "unknown"))

    def test_missing_device_reports_unreadable(self):
        d = diagnose(os.path.join(tempfile.gettempdir(), "does-not-exist.img"))
        self.assertEqual(d.status, "unreadable")

    def test_surface_scan_of_healthy_image(self):
        result = surface_scan(FIXTURE)
        self.assertTrue(result.healthy)
        self.assertEqual(result.bytes_read, result.total_bytes)

    def test_surface_scan_can_be_cancelled(self):
        import threading
        cancel = threading.Event()
        cancel.set()
        result = surface_scan(FIXTURE, cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.healthy)


class RepairCommandTests(unittest.TestCase):
    def test_windows_check_and_fix(self):
        if os.name != "nt":
            self.skipTest("Windows rules")
        self.assertEqual(repair_command("E:", "check"), ["chkdsk", "E:"])
        self.assertEqual(repair_command("E:", "fix"), ["chkdsk", "E:", "/f"])
        self.assertEqual(repair_command("\\\\.\\F:", "check"), ["chkdsk", "F:"])

    def test_system_drive_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows rules")
        system = os.environ.get("SystemDrive", "C:")
        with self.assertRaises(ValueError):
            repair_command(system + "\\", "fix")

    def test_file_path_is_refused_for_windows_repair(self):
        if os.name != "nt":
            self.skipTest("Windows rules")
        with self.assertRaises(ValueError):
            repair_command(FIXTURE, "check")

    def test_unknown_mode_refused(self):
        with self.assertRaises(ValueError):
            repair_command("E:", "format")


if __name__ == "__main__":
    unittest.main()


class SurfaceEndOfDeviceTests(unittest.TestCase):
    """Windows volumes report size 0; reading past the end must not count as bad sectors."""

    @unittest.skipUnless(os.path.exists(FIXTURE), "fixture missing")
    def test_unknown_size_does_not_report_end_as_bad(self):
        from unittest import mock
        import recovery.flashrepair as fr
        with mock.patch.object(fr, "source_size", return_value=0):
            result = surface_scan(FIXTURE)
        self.assertEqual(result.bad_ranges, [])
        self.assertEqual(result.bytes_read, os.path.getsize(FIXTURE))
