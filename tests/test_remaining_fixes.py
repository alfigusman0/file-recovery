"""Tests for the fixes made after the code review: repair cancel, capacity test lock,
disk-list cache, job cancel refusal and Windows destination resolution."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery import disks, flashcheck, flashrepair, safety  # noqa: E402
from recovery.webapp.jobs import JobManager  # noqa: E402


class RepairCancelTests(unittest.TestCase):
    def test_read_only_check_detection(self):
        self.assertTrue(flashrepair.is_read_only_check(["chkdsk", "G:"]))
        self.assertTrue(flashrepair.is_read_only_check(["fsck", "-n", "/dev/sdb1"]))
        self.assertTrue(flashrepair.is_read_only_check(["diskutil", "verifyVolume", "/dev/disk2s1"]))
        self.assertFalse(flashrepair.is_read_only_check(["chkdsk", "G:", "/f"]))
        self.assertFalse(flashrepair.is_read_only_check(["fsck", "-y", "/dev/sdb1"]))
        self.assertFalse(flashrepair.is_read_only_check(["diskutil", "repairVolume", "/dev/disk2s1"]))

    def test_read_only_check_is_stopped_on_cancel(self):
        cancel = threading.Event()
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        code, output = flashrepair.run_repair(cmd, timeout=60, cancel=cancel)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertEqual(code, -1)
        self.assertIn("Dibatalkan", output)

    def test_normal_repair_runs_to_the_end(self):
        code, output = flashrepair.run_repair([sys.executable, "-c", "print('selesai')"], timeout=60)
        self.assertEqual(code, 0)
        self.assertIn("selesai", output)

    def test_repairing_command_is_not_stopped(self):
        # A write-capable command ignores cancel: here a fake one that finishes on its own.
        cancel = threading.Event()
        cancel.set()
        cmd = [sys.executable, "-c", "import time; time.sleep(1); print('fix')", "/f"]
        code, output = flashrepair.run_repair(cmd, timeout=60, cancel=cancel)
        self.assertEqual(code, 0)
        self.assertIn("fix", output)


class JobCancelTests(unittest.TestCase):
    def test_non_cancellable_job_refuses_cancel(self):
        jobs = JobManager()
        gate = threading.Event()
        job = jobs.start("flash_repair", "Perbaikan", lambda j: gate.wait(5) and None, cancellable=False)
        try:
            self.assertFalse(jobs.cancel())
            self.assertFalse(job.cancelled)
        finally:
            gate.set()

    def test_cancellable_job_accepts_cancel(self):
        jobs = JobManager()
        gate = threading.Event()
        job = jobs.start("capacity", "Uji", lambda j: gate.wait(5) and None)
        try:
            self.assertTrue(jobs.cancel())
            self.assertTrue(job.cancelled)
        finally:
            gate.set()


class CapacityLockTests(unittest.TestCase):
    def setUp(self):
        # Temp folders live on the system drive; allow them as targets in these tests.
        patcher = mock.patch.object(flashcheck, "_is_on_system_drive", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_test_on_same_folder_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="fr_cap_")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = flashcheck.check_capacity_target(tmp)
        with flashcheck._ACTIVE_LOCK:
            flashcheck._ACTIVE_TARGETS.add(target)
        self.addCleanup(lambda: flashcheck._ACTIVE_TARGETS.discard(target))
        with self.assertRaises(flashcheck.CapacityRefused):
            flashcheck.capacity_test(tmp, 1, file_mb=1)

    def test_lock_is_released_after_a_test(self):
        tmp = tempfile.mkdtemp(prefix="fr_cap_")
        self.addCleanup(shutil.rmtree, tmp, True)
        flashcheck.capacity_test(tmp, 1, file_mb=1)
        target = flashcheck.check_capacity_target(tmp)
        self.assertNotIn(target, flashcheck._ACTIVE_TARGETS)


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(disks.STUCK.clear, disks.LIST_KEY)
        disks.STUCK.clear(disks.LIST_KEY)
        disks._cache_at = 0.0

    def test_fresh_list_is_reused_without_a_new_query(self):
        with mock.patch.object(disks, "_raw_list", return_value=[("/dev/sdb", "disk")]) as raw:
            disks.list_disks_info(timeout=2)
            disks.list_disks_info(timeout=2)
            self.assertEqual(raw.call_count, 1)

    def test_stale_list_is_queried_again(self):
        with mock.patch.object(disks, "_raw_list", return_value=[("/dev/sdb", "disk")]) as raw:
            disks.list_disks_info(timeout=2)
            disks._cache_at = 0.0
            disks.list_disks_info(timeout=2)
            self.assertEqual(raw.call_count, 2)


@unittest.skipUnless(os.name == "nt", "Windows volume resolution")
class WindowsDestinationTests(unittest.TestCase):
    def test_real_path_has_long_prefix(self):
        real = safety._windows_real_path(os.environ.get("SystemRoot", r"C:\Windows"))
        self.assertTrue(real.startswith("\\\\?\\"), real)

    def test_local_unc_name_is_refused(self):
        with self.assertRaises(safety.UnsafeDestinationError):
            safety._check_windows(r"\\.\PhysicalDrive7", r"\\localhost\C$\tmp")

    def test_device_destination_is_refused(self):
        with self.assertRaises(safety.UnsafeDestinationError):
            safety.check_destination(r"\\.\PhysicalDrive7", r"\\.\PhysicalDrive0")

    def test_system_volume_is_refused_for_its_own_device(self):
        system = os.environ.get("SystemDrive", "C:")
        with self.assertRaises(safety.UnsafeDestinationError):
            safety._check_windows(f"\\\\.\\{system}", os.environ.get("TEMP", system + "\\"))


if __name__ == "__main__":
    unittest.main()
