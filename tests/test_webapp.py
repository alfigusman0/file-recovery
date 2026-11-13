"""API tests for the web interface. Runs a real server on a free loopback port.

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from recovery.webapp.jobs import JobBusy, JobManager  # noqa: E402
from recovery.webapp.server import serve_in_thread  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fat32_deleted.img")


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method, path, body=None, token=True, host=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Token", self.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                ctype = res.headers.get("Content-Type", "")
                return res.status, (json.loads(raw) if ctype.startswith("application/json") else raw)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw

    def wait_job(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _status, job = self.request("GET", "/api/job")
            if job.get("state") not in ("running", None, "idle"):
                return job
            if job.get("state") == "idle":
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish in time")


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.state, cls.url = serve_in_thread(0)
        cls.client = Client(cls.url, cls.state.token)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_index_contains_token_and_app(self):
        with urllib.request.urlopen(self.url, timeout=10) as res:
            html = res.read().decode("utf-8")
        self.assertIn("File Recovery", html)
        self.assertIn(self.state.token, html)
        self.assertNotIn("__TOKEN__", html)

    def test_api_requires_token(self):
        status, body = self.client.request("GET", "/api/info", token=False)
        self.assertEqual(status, 403)

    def test_wrong_host_is_refused(self):
        status, _ = self.client.request("GET", "/api/info", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_info_and_disks(self):
        status, info = self.client.request("GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertIn("version", info)
        status, disks = self.client.request("GET", "/api/disks")
        self.assertEqual(status, 200)
        self.assertIn("disks", disks)

    def test_static_path_traversal_blocked(self):
        status, _ = self.client.request("GET", "/static/..%2Fjobs.py", token=False)
        self.assertEqual(status, 404)

    def test_browse_lists_folders(self):
        here = os.path.dirname(os.path.abspath(FIXTURE))
        status, data = self.client.request("GET", "/api/browse?path=" + urllib.parse.quote(here))
        self.assertEqual(status, 200)
        self.assertTrue(any(e["name"] == "fat32_deleted.img" for e in data["entries"]))

    @unittest.skipUnless(os.path.exists(FIXTURE), "fixture missing")
    def test_deleted_scan_recover_and_preview(self):
        status, _ = self.client.request("POST", "/api/fs/scan", {"source": FIXTURE})
        self.assertEqual(status, 202)
        job = self.client.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["count"], 2)
        self.assertEqual(job["unit"], "items")

        status, rows = self.client.request("GET", "/api/fs/entries")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows["items"]), 2)

        dest = tempfile.mkdtemp()
        status, _ = self.client.request("POST", "/api/fs/recover", {"ids": [0, 1], "dest": dest})
        self.assertEqual(status, 202)
        job = self.client.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["ok"], 2)

        status, _ = self.client.request("POST", "/api/fs/preview", {"id": 0})
        self.assertEqual(status, 202)
        job = self.client.wait_job()
        self.assertEqual(job["state"], "done", job)
        url = job["result"]["url"]
        status, body = self.client.request("GET", url, token=False)  # token travels in the query
        self.assertEqual(status, 200)
        self.assertIn(b"CV isi", body)

    def test_preview_rejects_wrong_token(self):
        status, _ = self.client.request("GET", "/api/preview/x.png?token=wrong", token=False)
        self.assertEqual(status, 403)

    @unittest.skipUnless(os.path.exists(FIXTURE), "fixture missing")
    def test_carve_and_flash_diagnose(self):
        out = tempfile.mkdtemp()
        status, _ = self.client.request("POST", "/api/carve", {"source": FIXTURE, "out": out})
        self.assertEqual(status, 202)
        job = self.client.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertIn("recovered", job["result"])

        status, _ = self.client.request("POST", "/api/flash/diagnose", {"device": FIXTURE})
        job = self.client.wait_job()
        self.assertEqual(job["result"]["status"], "ok")
        self.assertEqual(job["result"]["file_system"], "FAT32")

    def test_bad_input_gives_400(self):
        status, body = self.client.request("POST", "/api/carve", {"source": "", "out": ""})
        self.assertEqual(status, 400)
        self.assertIn("wajib", body["error"])

    def test_destination_on_source_device_is_refused(self):
        if os.name != "nt":
            self.skipTest("Windows device rule")
        status, body = self.client.request("POST", "/api/check", {"source": "\\\\.\\C:", "dest": "C:\\x"})
        self.assertEqual(status, 403)

    def test_repair_endpoint(self):
        src = tempfile.mkdtemp()
        out = tempfile.mkdtemp()
        with open(os.path.join(src, "bad.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nnot really")
        status, _ = self.client.request("POST", "/api/repair", {"source": src, "out": out})
        self.assertEqual(status, 202)
        job = self.client.wait_job()
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["result"]["total"], 1)

    def test_debug_mode_streams_logs(self):
        status, _ = self.client.request("POST", "/api/debug", {"enabled": True})
        self.assertEqual(status, 200)
        self.client.request("GET", "/api/trash")  # any action produces log lines
        status, logs = self.client.request("GET", "/api/logs?since=0")
        self.assertEqual(status, 200)
        self.assertGreater(logs["last"], 0)
        self.assertTrue(logs["debug"])
        status, dbg = self.client.request("GET", "/api/debug")
        self.assertIn("job", dbg)
        self.client.request("POST", "/api/debug", {"enabled": False})


class JobManagerTests(unittest.TestCase):
    def test_only_one_job_at_a_time(self):
        jobs = JobManager()
        gate = threading.Event()
        jobs.start("a", "A", lambda job: gate.wait(5) and {"ok": True})
        with self.assertRaises(JobBusy):
            jobs.start("b", "B", lambda job: None)
        gate.set()
        for _ in range(100):
            if jobs.snapshot()["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(jobs.snapshot()["state"], "done")

    def test_cancel_sets_state_cancelled(self):
        jobs = JobManager()

        def slow(job):
            for _ in range(200):
                if job.cancelled:
                    return {"partial": True}
                time.sleep(0.01)
            return {"partial": False}

        jobs.start("slow", "Slow", slow)
        self.assertTrue(jobs.cancel())
        for _ in range(200):
            if jobs.snapshot()["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(jobs.snapshot()["state"], "cancelled")

    def test_error_is_reported(self):
        jobs = JobManager()

        def boom(job):
            raise RuntimeError("akses ditolak")

        jobs.start("x", "X", boom)
        for _ in range(100):
            if jobs.snapshot()["state"] != "running":
                break
            time.sleep(0.02)
        snap = jobs.snapshot()
        self.assertEqual(snap["state"], "error")
        self.assertIn("akses ditolak", snap["error"])


if __name__ == "__main__":
    unittest.main()
