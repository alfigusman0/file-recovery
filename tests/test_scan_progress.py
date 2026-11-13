"""Tests for folder pre-count and item progress with ETA. Run: python -m unittest discover -s tests -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery.fsmeta import HAVE_TSK, open_file_systems, count_directories, scan_deleted  # noqa: E402
from recovery.progress import ProgressTracker  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fat32_deleted.img")


class ItemProgressTests(unittest.TestCase):
    def test_items_tracker_text_and_eta(self):
        clock = [0.0]
        t = ProgressTracker(200, clock=lambda: clock[0], unit="items")
        clock[0] = 10.0
        t.update(50)
        self.assertAlmostEqual(t.percent, 25.0)
        self.assertIn("50 dari 200", t.status_text())
        self.assertAlmostEqual(t.eta_seconds, 30.0)  # 150 items left at 5 items/s

    @unittest.skipUnless(HAVE_TSK and os.path.exists(FIXTURE), "pytsk3 or fixture missing")
    def test_directory_count_matches_walk(self):
        seen = []
        scan_deleted(FIXTURE, item_progress=lambda d, t, _x: seen.append((d, t)))
        self.assertTrue(seen)
        done, total = seen[-1]
        self.assertEqual(done, total)
        self.assertGreater(total, 0)
        for fs_part in open_file_systems(FIXTURE):
            _idx, fs, _img = fs_part
            self.assertEqual(count_directories(fs), total)


if __name__ == "__main__":
    unittest.main()
