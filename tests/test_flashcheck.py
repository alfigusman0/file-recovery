"""Tests for the capacity test and the hang protection. Uses temp folders only, never a real device.

Run: python -m unittest discover -s tests -v
"""

import errno
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import recovery.disks as disks  # noqa: E402
import recovery.flashcheck as fc  # noqa: E402
import recovery.flashrepair as fr  # noqa: E402
from recovery.flashcheck import (NOT_RESPONDING, STUCK, CapacityRefused, StuckRegistry,  # noqa: E402
                                 block_pattern, capacity_test, run_with_timeout)

KB = 1024
MB = 1024 * 1024
BLOCK = 64 * KB  # small blocks keep the tests fast


class _TempTarget(unittest.TestCase):
    """A temp folder that is allowed as a target (temp dirs live on the system drive)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="captest_")
        patcher = mock.patch.object(fc, "_is_on_system_drive", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def folder_gone(self):
        return not os.path.exists(os.path.join(self.dir, fc.FOLDER_NAME))


class CapacityTestTests(_TempTarget):
    def test_small_test_passes(self):
        seen = []
        r = capacity_test(self.dir, 3, progress=lambda p, d, t: seen.append(p), file_mb=1, block_size=BLOCK)
        self.assertTrue(r.ok, r.verdict)
        self.assertTrue(r.verdict.startswith("Kapasitas sesuai"), r.verdict)
        self.assertEqual(r.bytes_written, 3 * MB)
        self.assertEqual(r.bytes_verified_ok, 3 * MB)
        self.assertEqual((r.mismatched_blocks, r.unreadable_blocks), (0, 0))
        self.assertIsNone(r.first_bad_offset)
        self.assertFalse(r.cancelled)
        self.assertGreater(r.write_mb_per_s, 0)
        self.assertGreater(r.read_mb_per_s, 0)
        self.assertIn("write", seen)
        self.assertIn("verify", seen)
        self.assertTrue(r.cleaned_up)
        self.assertTrue(self.folder_gone())

    def test_fake_device_wraps_around(self):
        """Beyond 2 MB the 'device' hands back the data of block 0, like a fake stick."""
        real = fc._read_block
        limit = 2 * MB

        def fake_read(reader, offset, size):
            data = real(reader, offset, size)
            return block_pattern(0, size) if offset >= limit else data

        with mock.patch.object(fc, "_read_block", side_effect=fake_read):
            r = capacity_test(self.dir, 4, file_mb=1, block_size=BLOCK)
        self.assertFalse(r.ok)
        self.assertEqual(r.first_bad_offset, limit)
        self.assertEqual(r.mismatched_blocks, (4 * MB - limit) // BLOCK)
        self.assertEqual(r.bytes_verified_ok, limit)
        self.assertTrue(r.verdict.startswith("Kemungkinan flashdisk palsu: data mulai rusak sekitar"), r.verdict)
        self.assertIn("GB", r.verdict)
        self.assertTrue(self.folder_gone())

    def test_unreadable_blocks_are_counted(self):
        real = fc._read_block

        def failing(reader, offset, size):
            if 1 * MB <= offset < 1 * MB + 2 * BLOCK:
                raise OSError(errno.EIO, "I/O error")
            return real(reader, offset, size)

        with mock.patch.object(fc, "_read_block", side_effect=failing):
            r = capacity_test(self.dir, 2, file_mb=2, block_size=BLOCK)
        self.assertEqual(r.unreadable_blocks, 2)
        self.assertEqual(r.mismatched_blocks, 0)  # reading resumes correctly after the error
        self.assertEqual(r.first_bad_offset, 1 * MB)
        self.assertTrue(r.verdict.startswith("Kemungkinan flashdisk palsu"))

    def test_disk_full_stops_writing(self):
        real = fc._write_block
        full_at = 1 * MB + 3 * BLOCK

        def writer(fh, offset, data):
            if offset >= full_at:
                raise OSError(errno.ENOSPC, "No space left on device")
            real(fh, offset, data)

        with mock.patch.object(fc, "_write_block", side_effect=writer):
            r = capacity_test(self.dir, 4, file_mb=1, block_size=BLOCK)
        self.assertTrue(r.disk_full)
        self.assertEqual(r.bytes_written, full_at)
        self.assertEqual(r.bytes_verified_ok, full_at)
        self.assertTrue(r.ok)
        self.assertIn("Ruang kosong habis", r.verdict)
        self.assertTrue(self.folder_gone())

    def test_cancel_stops_early_and_cleans_up(self):
        cancel = threading.Event()

        def progress(phase, done, total):
            if phase == "write" and done >= 1 * MB:
                self.assertTrue(os.path.isdir(os.path.join(self.dir, fc.FOLDER_NAME)))
                cancel.set()

        r = capacity_test(self.dir, 8, cancel=cancel, progress=progress, file_mb=1, block_size=BLOCK)
        self.assertTrue(r.cancelled)
        self.assertFalse(r.ok)
        self.assertLess(r.bytes_written, 8 * MB)
        self.assertEqual(r.bytes_verified_ok, 0)
        self.assertTrue(r.verdict.startswith("Dibatalkan"), r.verdict)
        self.assertIn("sudah dihapus", r.verdict)
        self.assertTrue(r.cleaned_up)
        self.assertTrue(self.folder_gone())

    def test_cleanup_after_unexpected_error(self):
        with mock.patch.object(fc, "block_pattern", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                capacity_test(self.dir, 2, file_mb=1, block_size=BLOCK)
        self.assertTrue(self.folder_gone())

    def test_foreign_capacity_folder_is_not_deleted(self):
        folder = os.path.join(self.dir, fc.FOLDER_NAME)
        os.makedirs(folder)
        with open(os.path.join(folder, "penting.docx"), "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(CapacityRefused):
            capacity_test(self.dir, 1, block_size=BLOCK)
        self.assertTrue(os.path.exists(os.path.join(folder, "penting.docx")))


class CapacityRefusalTests(unittest.TestCase):
    def test_system_drive_refused(self):
        system_root = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
        with self.assertRaises(CapacityRefused) as ctx:
            capacity_test(system_root, 1)
        self.assertIn("sistem", str(ctx.exception))

    def test_missing_folder_and_file_refused(self):
        with mock.patch.object(fc, "_is_on_system_drive", return_value=False):
            with self.assertRaises(CapacityRefused):
                capacity_test(os.path.join(tempfile.gettempdir(), "tidak-ada-folder-xyz"), 1)
            with tempfile.NamedTemporaryFile(delete=False) as fh:
                path = fh.name
            try:
                with self.assertRaises(CapacityRefused):
                    capacity_test(path, 1)
            finally:
                os.remove(path)

    def test_raw_device_path_refused(self):
        with self.assertRaises(CapacityRefused):
            fc.check_capacity_target("\\\\.\\Q:")


class HangProtectionTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)  # let the blocked threads end

    def test_timeout_returns_not_responding_quickly(self):
        reg = StuckRegistry()
        calls = []

        def hangs():
            calls.append(1)
            self.release.wait(10)
            return "late"

        t0 = time.monotonic()
        res = run_with_timeout(hangs, timeout=0.2, key="TESTDEV:", registry=reg, cooldown=60)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(res.status, NOT_RESPONDING)
        self.assertTrue(res.timed_out)
        self.assertIn(NOT_RESPONDING, res.message)
        self.assertTrue(reg.is_stuck("TESTDEV:"))

        # During the cool-down the function is not started again.
        t0 = time.monotonic()
        res2 = run_with_timeout(hangs, timeout=0.2, key="TESTDEV:", registry=reg)
        self.assertLess(time.monotonic() - t0, 0.15)
        self.assertEqual(res2.status, NOT_RESPONDING)
        self.assertEqual(len(calls), 1)
        self.assertIn("coba lagi", reg.message("TESTDEV:"))

    def test_retry_allowed_after_cooldown(self):
        reg = StuckRegistry()
        gate = threading.Event()
        res = run_with_timeout(gate.wait, 5, timeout=0.1, key="K:", registry=reg, cooldown=0.1)
        self.assertTrue(res.timed_out)
        gate.set()  # the device answers again
        time.sleep(0.3)
        self.assertFalse(reg.is_stuck("K:"))
        res = run_with_timeout(lambda: 42, timeout=1, key="K:", registry=reg)
        self.assertTrue(res.ok)
        self.assertEqual(res.value, 42)

    def test_still_blocked_thread_keeps_device_stuck(self):
        reg = StuckRegistry()
        run_with_timeout(self.release.wait, 10, timeout=0.1, key="B:", registry=reg, cooldown=0.05)
        time.sleep(0.15)
        self.assertTrue(reg.is_stuck("B:"))  # cool-down over, but the old read is still blocked
        self.assertIn("cabut", reg.message("B:").lower())

    def test_errors_are_passed_back(self):
        res = run_with_timeout(lambda: 1 / 0, timeout=1)
        self.assertEqual(res.status, "error")
        self.assertIsInstance(res.exception, ZeroDivisionError)

    def test_device_key_matches_forms(self):
        self.assertEqual(fc.device_key("\\\\.\\q:"), "Q:")
        self.assertEqual(fc.device_key("Q:\\"), "Q:")
        self.assertEqual(fc.device_key("Q:"), "Q:")
        self.assertEqual(fc.device_key("Q:\\data", whole_drive=True), "Q:")
        self.assertEqual(fc.device_key("\\\\.\\PhysicalDrive3"), "PHYSICALDRIVE3")

    def test_diagnose_not_responding(self):
        dev = "\\\\.\\Y:"
        self.addCleanup(STUCK.clear, "Y:")
        with mock.patch.object(fr, "_diagnose", side_effect=lambda d: self.release.wait(10)):
            t0 = time.monotonic()
            d = fr.diagnose(dev, timeout=0.2)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(d.status, NOT_RESPONDING)
        self.assertFalse(d.readable)
        self.assertTrue(STUCK.is_stuck("Y:"))

    def test_surface_scan_stops_when_reads_hang(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"\0" * (3 * MB))
            path = fh.name
        key = fc.device_key(path)
        self.addCleanup(STUCK.clear, key)
        real = fr.read_at

        def slow_read(f, pos, n):
            if pos >= 1 * MB:
                self.release.wait(10)
            return real(f, pos, n)

        try:
            with mock.patch.object(fr, "read_at", side_effect=slow_read):
                t0 = time.monotonic()
                r = fr.surface_scan(path, read_timeout=0.2)
            self.assertLess(time.monotonic() - t0, 3.0)
            self.assertTrue(r.stuck)
            self.assertFalse(r.healthy)
            self.assertEqual(r.bytes_read, 1 * MB)
            self.assertIn(NOT_RESPONDING, r.message)
            with self.assertRaises(fc.DeviceNotResponding):
                fr.surface_scan(path)  # cool-down: no new probe on the stuck device
        finally:
            self.release.set()
            time.sleep(0.3)  # the blocked read returns and the handle gets closed
            try:
                os.remove(path)
            except OSError:
                pass

    def test_disk_list_does_not_block_and_flags_stuck(self):
        self.addCleanup(STUCK.clear, disks.LIST_KEY)
        self.addCleanup(STUCK.clear, "Q:")
        STUCK.clear(disks.LIST_KEY)
        with mock.patch.object(disks, "_raw_list", return_value=[("\\\\.\\Q:", "Volume Q:"),
                                                                 ("\\\\.\\R:", "Volume R:")]):
            STUCK.mark("Q:", cooldown=60)
            info = disks.list_disks_info(timeout=2)
        self.assertFalse(info["partial"])
        flags = {d["path"]: d["stuck"] for d in info["disks"]}
        self.assertEqual(flags, {"\\\\.\\Q:": True, "\\\\.\\R:": False})
        self.assertIn("tidak merespons", info["disks"][0]["desc"])

        # The OS query hangs: the cached list comes back fast, marked partial.
        with mock.patch.object(disks, "_raw_list", side_effect=lambda: self.release.wait(10) or []):
            t0 = time.monotonic()
            info = disks.list_disks_info(timeout=0.2)
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertTrue(info["partial"])
            self.assertIn(NOT_RESPONDING, info["message"])
            self.assertEqual(len(info["disks"]), 2)
            items = disks.list_disks(timeout=0.2)  # in cool-down: answers at once
        path, desc = items[0]  # still unpacks as (path, desc) for the old callers
        self.assertTrue(items[0].stuck)


class CapacityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from recovery.webapp.server import serve_in_thread
        from test_webapp import Client
        cls.httpd, cls.state, cls.url = serve_in_thread(0)
        cls.client = Client(cls.url, cls.state.token)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_needs_token(self):
        status, _ = self.client.request("POST", "/api/capacity", {"path": "Q:\\"}, token=False)
        self.assertEqual(status, 403)

    def test_system_drive_refused(self):
        system_root = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"
        status, body = self.client.request("POST", "/api/capacity", {"path": system_root, "size_mb": 1})
        self.assertEqual(status, 400)
        self.assertIn("sistem", body["error"])

    def test_capacity_job_runs_and_respects_one_job_rule(self):
        target = tempfile.mkdtemp(prefix="capapi_")
        self.addCleanup(shutil.rmtree, target, True)
        with mock.patch.object(fc, "_is_on_system_drive", return_value=False):
            gate = threading.Event()
            self.addCleanup(gate.set)
            self.state.jobs.start("blocker", "Blocker", lambda job: gate.wait(10))
            status, _ = self.client.request("POST", "/api/capacity", {"path": target, "size_mb": 2})
            self.assertEqual(status, 409)
            gate.set()
            self.client.wait_job()

            status, body = self.client.request("POST", "/api/capacity",
                                               {"path": target, "size_mb": 2, "file_mb": 1})
            self.assertEqual(status, 202, body)
            self.assertEqual(body["started"], "capacity")
            job = self.client.wait_job()
        self.assertEqual(job["name"], "capacity")
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["unit"], "bytes")
        res = job["result"]
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["bytes_written"], 2 * MB)
        self.assertTrue(res["verdict"].startswith("Kapasitas sesuai"))
        self.assertFalse(os.path.exists(os.path.join(target, fc.FOLDER_NAME)))


if __name__ == "__main__":
    unittest.main()
