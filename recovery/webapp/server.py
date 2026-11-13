"""Local web server for the recovery app.

Serves the modern UI and a small JSON API on 127.0.0.1 only.

Protection:
  - Bound to the loopback address; other computers cannot connect.
  - Host header must be 127.0.0.1 or localhost (blocks DNS-rebinding tricks).
  - Every /api request needs a random token that only the served page knows
    (blocks other websites from driving the API with a browser).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import string
import sys
import tempfile
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..carver import carve_summary, source_size
from ..dates import make_range
from ..disks import list_disks_info
from ..flashcheck import (PROBE_TIMEOUT, STUCK, DeviceNotResponding, capacity_test, check_capacity_target,
                          device_key, run_with_timeout)
from ..flashrepair import diagnose, repair_command, run_repair, surface_scan
from ..fsmeta import recover_entries, scan_deleted
from ..imaging import export_entries, image_source
from ..logsetup import RING, get_logger, set_debug, setup_logging
from ..platform_info import current_os, is_admin
from ..repair import repair_file
from ..safety import UnsafeDestinationError, check_destination, is_device_path
from ..trash import filter_by_deletion, list_trash, restore
from .jobs import JobBusy, JobManager

log = get_logger("server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PREVIEW_EXT = {".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp"}
TEXT_EXT = {".txt", ".csv", ".log", ".xml", ".json", ".md", ".html", ".py", ".ini", ".cfg"}
MAX_BROWSE = 800


class AppState:
    """Everything the handlers share. Protected by the JobManager for long operations."""

    def __init__(self, port: int = 0) -> None:
        self.token = secrets.token_urlsafe(24)
        self.port = port
        self.jobs = JobManager()
        self.fs_entries: list = []
        self.fs_source = ""
        self.trash_items: list = []
        self.preview_dir = tempfile.mkdtemp(prefix="filerecovery_preview_")
        self.log_path = setup_logging()
        self.debug = False


def _json_dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")


def _entry_dict(i: int, e) -> dict:
    return {
        "id": i,
        "path": e.path,
        "name": e.name,
        "size": e.size,
        "fs": e.fs_type,
        "partition": e.partition,
        "created": e.crtime.isoformat() if e.crtime else None,
        "modified": e.mtime.isoformat() if e.mtime else None,
        "deleted": e.deleted_at.isoformat() if e.deleted_at else None,
    }


def _trash_dict(i: int, item) -> dict:
    return {
        "id": i,
        "name": item.name,
        "size": item.size,
        "deleted": item.deleted_at.isoformat() if item.deleted_at else None,
        "original": item.original_path,
    }


def list_roots() -> list[dict]:
    roots = []
    if current_os() == "windows":
        for letter in string.ascii_uppercase:
            path = f"{letter}:\\"
            if os.path.exists(path):
                roots.append({"name": f"{letter}:", "path": path})
    else:
        roots.append({"name": "/", "path": "/"})
        home = os.path.expanduser("~")
        roots.append({"name": "Home", "path": home})
    return roots


def browse(path: str) -> dict:
    """Directory listing for the built-in file picker. Folders first, then files."""
    if not path:
        return {"path": "", "parent": None, "entries": [], "roots": list_roots()}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Folder tidak ditemukan: {path}")
    entries = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                entries.append({"name": entry.name, "path": entry.path, "is_dir": is_dir, "size": size})
                if len(entries) >= MAX_BROWSE:
                    break
    except PermissionError as exc:
        raise PermissionError(f"Akses ditolak ke {path}: {exc}") from exc
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None, "entries": entries,
            "roots": list_roots()}


class Handler(BaseHTTPRequestHandler):
    server_version = "FileRecovery/" + __version__

    @property
    def app(self) -> AppState:
        return self.server.app  # type: ignore[attr-defined]

    # ---------- plumbing ----------

    def log_message(self, fmt, *args):  # route HTTP access log into our log file
        # Drop query strings: preview URLs carry the session token in ?token=
        log.debug("http %s", (fmt % args).split("?", 1)[0])

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              headers: Optional[dict] = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            # The browser closed the request (page reload, polling timeout). Not an error.
            self.close_connection = True
            log.debug("client closed the connection before %s was sent", self.path)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, _json_dump(value))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.app.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("Content-Length tidak valid")
        if length > 1_000_000:
            raise ValueError("permintaan terlalu besar")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ValueError("body harus JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("body harus objek JSON")
        return data

    def _gate(self) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Host tidak diizinkan")
            return False
        if not self._token_ok():
            self._error(HTTPStatus.FORBIDDEN, "Token tidak valid. Muat ulang halaman.")
            return False
        return True

    # ---------- GET ----------

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Host tidak diizinkan")
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._serve_index()
            if url.path.startswith("/static/"):
                return self._serve_static(url.path[len("/static/"):])
            if url.path.startswith("/api/preview/"):
                # <img> tags cannot send headers, so previews carry the token in the query string
                return self._serve_preview(unquote(url.path[len("/api/preview/"):]), query.get("token", ""))
            if not url.path.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "tidak ditemukan")
            if not self._token_ok():
                return self._error(HTTPStatus.FORBIDDEN, "Token tidak valid. Muat ulang halaman.")
            return self._api_get(url.path, query)
        except (ValueError, FileNotFoundError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s failed", url.path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _serve_index(self) -> None:
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
            html = fh.read().replace("__TOKEN__", self.app.token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, name: str) -> None:
        name = unquote(name)
        # ":" must be refused too: on Windows "D:file" makes os.path.join jump to drive D:.
        if any(c in name for c in "/\\:") or name.startswith("."):
            return self._error(HTTPStatus.NOT_FOUND, "tidak ditemukan")
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            return self._error(HTTPStatus.NOT_FOUND, "tidak ditemukan")
        ctype = {".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}.get(
            os.path.splitext(name)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(HTTPStatus.OK, fh.read(), ctype + "; charset=utf-8")

    def _api_get(self, path: str, q: dict) -> None:
        app = self.app
        if path == "/api/info":
            return self._json(200, {"version": __version__, "os": current_os(), "admin": is_admin(),
                                    "log": app.log_path})
        if path == "/api/disks":
            return self._json(200, list_disks_info())
        if path == "/api/browse":
            return self._json(200, browse(q.get("path", "")))
        if path == "/api/job":
            return self._json(200, app.jobs.snapshot())
        if path == "/api/logs":
            since = int(q.get("since", "0") or 0)
            rows, last = RING.since(since)
            return self._json(200, {"lines": rows[-500:], "last": last, "debug": app.debug})
        if path == "/api/debug":
            return self._json(200, {"debug": app.debug, "job": app.jobs.snapshot(),
                                    "threads": threading.active_count(), "log": app.log_path})
        if path == "/api/trash":
            date_range = make_range(q.get("from", ""), q.get("to", ""))
            items, skipped = filter_by_deletion(list_trash(), date_range)
            app.trash_items = items
            return self._json(200, {"items": [_trash_dict(i, it) for i, it in enumerate(items)],
                                    "skipped": skipped})
        if path == "/api/fs/entries":
            date_range = make_range(q.get("from", ""), q.get("to", ""))
            needle = q.get("match", "").strip().lower()
            rows = []
            for i, e in enumerate(app.fs_entries):
                if needle and needle not in e.path.lower():
                    continue
                if date_range.active and not date_range.contains(e.deleted_at):
                    continue
                rows.append(_entry_dict(i, e))
            return self._json(200, {"source": app.fs_source, "total": len(app.fs_entries), "items": rows})
        if path == "/api/fs/export":
            fmt = q.get("format", "csv")
            name = "daftar-file-dihapus." + ("json" if fmt == "json" else "csv")
            out = os.path.join(app.preview_dir, name)
            export_entries(app.fs_entries, out)
            with open(out, "rb") as fh:
                body = fh.read()
            ctype = "application/json" if fmt == "json" else "text/csv"
            return self._send(200, body, ctype + "; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        return self._error(HTTPStatus.NOT_FOUND, "endpoint tidak ada")

    def _serve_preview(self, name: str, token: str) -> None:
        if not secrets.compare_digest(token, self.app.token):
            return self._error(HTTPStatus.FORBIDDEN, "token diperlukan")
        if any(c in name for c in "/\\:") or name.startswith("."):
            return self._error(HTTPStatus.BAD_REQUEST, "nama tidak valid")
        path = os.path.join(self.app.preview_dir, name)
        if not os.path.isfile(path):
            return self._error(HTTPStatus.NOT_FOUND, "pratinjau tidak ada")
        ext = os.path.splitext(name)[1].lower()
        if ext in PREVIEW_EXT:
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, f"image/{ext[1:]}")
        elif ext in TEXT_EXT:
            ctype = "text/plain"
        else:
            ctype = "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read(2 * 1024 * 1024)
        return self._send(200, data, ctype + "; charset=utf-8" if ctype.startswith("text") else ctype)

    # ---------- POST ----------

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
            return self._api_post(path, body)
        except JobBusy as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        except DeviceNotResponding as exc:
            retry = max(1, int(STUCK.remaining(exc.key)) + 1) if exc.key else 30
            return self._send(HTTPStatus.SERVICE_UNAVAILABLE, _json_dump({"error": str(exc), "stuck": True}),
                              headers={"Retry-After": str(retry)})
        except UnsafeDestinationError as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, FileNotFoundError, KeyError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except PermissionError as exc:
            return self._error(HTTPStatus.FORBIDDEN, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _need(self, body: dict, key: str) -> str:
        value = str(body.get(key, "")).strip()
        if not value:
            raise ValueError(f"'{key}' wajib diisi")
        return value

    @staticmethod
    def _not_stuck(device: str, whole_drive: bool = False) -> None:
        key = device_key(device, whole_drive=whole_drive)
        if STUCK.is_stuck(key):
            raise DeviceNotResponding(STUCK.message(key), key)

    def _api_post(self, path: str, body: dict) -> None:
        app = self.app
        jobs = app.jobs

        if path == "/api/debug":
            app.debug = bool(body.get("enabled", False))
            set_debug(app.debug)
            log.info("debug mode %s", "ON" if app.debug else "OFF")
            return self._json(200, {"debug": app.debug})

        if path == "/api/check":
            source = self._need(body, "source")
            dest = str(body.get("dest", ""))
            check_destination(source, dest)
            return self._json(200, {"ok": True})

        if path == "/api/job/cancel":
            return self._json(200, {"cancelled": jobs.cancel()})

        if path == "/api/trash/restore":
            ids = [int(i) for i in body.get("ids", [])]
            dest = self._need(body, "dest")
            items = [app.trash_items[i] for i in ids if 0 <= i < len(app.trash_items)]
            if not items:
                raise ValueError("pilih item trash dulu")

            def run(job):
                ok, errors = 0, []
                for n, item in enumerate(items, 1):
                    if job.cancelled:
                        break
                    job.set_phase("Menyalin", item.name)
                    try:
                        restore(item, dest)
                        ok += 1
                    except OSError as exc:
                        errors.append(f"{item.name}: {exc}")
                    job.progress(n, len(items))
                return {"ok": ok, "total": len(items), "dest": dest, "errors": errors}

            job = jobs.start("trash_restore", "Pulihkan dari trash", run, unit="items")
            return self._json(202, {"started": job.name})

        if path == "/api/fs/scan":
            source = self._need(body, "source")
            if not is_device_path(source) and not os.path.exists(source):
                raise FileNotFoundError(f"Sumber tidak ditemukan: {source}")
            date_range = make_range(body.get("from", ""), body.get("to", ""))

            def run(job):
                job.set_phase("Menghitung folder", source)

                def item_cb(done, total, text):
                    job.set_total(total)
                    job.progress(done, total)
                    job.set_phase("Membaca folder", text)

                entries = scan_deleted(source, date_range=date_range, cancel=job.cancel_event,
                                       progress=lambda msg: job.set_phase("Membaca", msg),
                                       item_progress=item_cb)
                app.fs_entries = entries
                app.fs_source = source
                return {"count": len(entries), "source": source}

            job = jobs.start("fs_scan", "Cari file dihapus", run, unit="items")
            return self._json(202, {"started": job.name})

        if path == "/api/fs/recover":
            ids = [int(i) for i in body.get("ids", [])]
            dest = self._need(body, "dest")
            chosen = [app.fs_entries[i] for i in ids if 0 <= i < len(app.fs_entries)]
            if not chosen:
                raise ValueError("pilih file dulu")
            check_destination(app.fs_source, dest)
            src = app.fs_source

            def run(job):
                job.set_phase("Memulihkan", f"{len(chosen)} file")
                results = recover_entries(src, chosen, dest, cancel=job.cancel_event)
                ok = sum(1 for _d, _n, err in results if err is None)
                job.progress(len(results), len(chosen))
                return {"ok": ok, "total": len(results), "dest": dest,
                        "problems": [f"{d}: {e}" for d, _n, e in results if e]}

            job = jobs.start("fs_recover", "Pulihkan file dihapus", run, unit="items")
            return self._json(202, {"started": job.name})

        if path == "/api/fs/preview":
            idx = int(body.get("id", -1))
            if not 0 <= idx < len(app.fs_entries):
                raise ValueError("file tidak ditemukan")
            entry = app.fs_entries[idx]
            src = app.fs_source

            def run(job):
                job.set_phase("Menyiapkan pratinjau", entry.name)
                scratch = tempfile.mkdtemp(dir=app.preview_dir)
                results = recover_entries(src, [entry], scratch, cancel=job.cancel_event)
                dest, written, err = results[0]
                if err and not os.path.exists(dest):
                    shutil.rmtree(scratch, ignore_errors=True)
                    raise RuntimeError(err)
                # Flatten into the preview folder under a unique name (the endpoint serves flat names only).
                name = f"{secrets.token_hex(6)}_{os.path.basename(dest)}"
                os.replace(dest, os.path.join(app.preview_dir, name))
                shutil.rmtree(scratch, ignore_errors=True)
                return {"name": entry.name, "file": name, "size": entry.size, "partial": bool(err),
                        "kind": os.path.splitext(name)[1].lower(),
                        "url": f"/api/preview/{name}?token={app.token}"}

            job = jobs.start("fs_preview", "Pratinjau", run, unit="items")
            return self._json(202, {"started": job.name})

        if path == "/api/carve":
            source = self._need(body, "source")
            out = self._need(body, "out")
            exts = [str(e) for e in body.get("exts", [])] or None
            date_range = make_range(body.get("from", ""), body.get("to", ""))
            check_destination(source, out)
            if not is_device_path(source) and not os.path.exists(source):
                raise FileNotFoundError(f"Sumber tidak ditemukan: {source}")

            def run(job):
                try:
                    with open(source, "rb", buffering=0) as fh:
                        job.set_total(source_size(fh))
                except OSError:
                    pass

                def scan_progress(done, total):
                    job.set_total(total)
                    job.progress(done, total)
                    job.set_phase("Memindai", source)

                def phase(text, done, total):
                    job.set_phase_progress(text, done, total)

                summary = carve_summary(source, out, extensions=exts, progress=scan_progress,
                                        date_range=date_range, cancel=job.cancel_event, phase=phase)
                return {"recovered": len(summary.recovered),
                        "complete": sum(1 for r in summary.recovered if r.complete),
                        "skipped_by_date": summary.skipped_by_date,
                        "skipped_inside": summary.skipped_inside_other,
                        "out": out, "cancelled": summary.cancelled}

            job = jobs.start("carve", "Pemindaian carving", run, unit="bytes")
            return self._json(202, {"started": job.name})

        if path == "/api/image":
            source = self._need(body, "source")
            out = self._need(body, "out")
            check_destination(source, out)   # the image file itself, never a device or its parent folder

            def run(job):
                def track(done, total):
                    job.progress(done, total)
                res = image_source(source, out, progress=track, cancel=job.cancel_event)
                return {"image": res.image_path, "bytes": res.bytes_copied, "total": res.total_bytes,
                        "sha256": res.sha256, "bad_blocks": len(res.bad_ranges), "cancelled": res.cancelled}

            job = jobs.start("image", "Salin disk", run, unit="bytes")
            return self._json(202, {"started": job.name})

        if path == "/api/repair":
            src = self._need(body, "source")
            out = self._need(body, "out")
            if not os.path.exists(src):
                raise FileNotFoundError(f"Tidak ditemukan: {src}")

            def run(job):
                paths = ([os.path.join(r, f) for r, _d, files in os.walk(src) for f in files]
                         if os.path.isdir(src) else [src])
                results = []
                for n, p in enumerate(paths, 1):
                    if job.cancelled:
                        break
                    job.set_phase("Memperbaiki", os.path.basename(p))
                    res = repair_file(p, out)
                    results.append({"file": os.path.basename(p), "ok": res.ok, "output": res.output,
                                    "actions": res.actions})
                    job.progress(n, len(paths))
                return {"results": results, "ok": sum(1 for r in results if r["ok"]), "total": len(results)}

            job = jobs.start("repair", "Perbaiki file", run, unit="items")
            return self._json(202, {"started": job.name})

        if path == "/api/flash/diagnose":
            dev = self._need(body, "device")
            self._not_stuck(dev)

            def run(job):
                job.set_phase("Membaca sektor awal")
                d = diagnose(dev)
                return asdict(d)

            job = jobs.start("flash_diag", "Diagnosa perangkat", run)
            return self._json(202, {"started": job.name})

        if path == "/api/flash/surface":
            dev = self._need(body, "device")
            self._not_stuck(dev)

            def run(job):
                def track(done, total):
                    job.progress(done, total)
                res = surface_scan(dev, progress=track, cancel=job.cancel_event)
                if res.stuck:
                    job.set_phase("Perangkat tidak merespons", res.message)
                out = asdict(res)
                out["healthy"] = res.healthy
                out["bad_bytes"] = res.bad_bytes
                out["bad_count"] = len(res.bad_ranges)
                out["bad_ranges"] = res.bad_ranges[:200]
                return out

            job = jobs.start("flash_surface", "Uji permukaan", run, unit="bytes")
            return self._json(202, {"started": job.name})

        if path == "/api/flash/repair":
            dev = self._need(body, "device")
            mode = str(body.get("mode", "check"))
            cmd = repair_command(dev, mode)  # raises ValueError for refused devices

            def run(job):
                job.set_phase("Menjalankan " + cmd[0], " ".join(cmd))
                code, output = run_repair(cmd)
                return {"code": code, "output": output, "command": " ".join(cmd)}

            job = jobs.start("flash_repair", "Perbaikan sistem file", run)
            return self._json(202, {"started": job.name})

        if path == "/api/capacity":
            target = self._need(body, "path")
            try:
                size_mb = int(body.get("size_mb", 0) or 0)
                file_mb = int(body.get("file_mb", 1024) or 1024)
            except (TypeError, ValueError) as exc:
                raise ValueError("'size_mb' dan 'file_mb' harus angka") from exc
            if size_mb < 0 or not 1 <= file_mb <= 4096:
                raise ValueError("'size_mb' minimal 0 (0 = semua ruang kosong), 'file_mb' 1..4096")
            key = device_key(target, whole_drive=True)
            self._not_stuck(target, whole_drive=True)
            # The folder check touches the device, so it is guarded like any other probe.
            probe = run_with_timeout(check_capacity_target, target, timeout=PROBE_TIMEOUT, key=key)
            if probe.timed_out:
                raise DeviceNotResponding(probe.message, key)
            if not probe.ok:
                raise probe.exception  # CapacityRefused is a ValueError -> 400
            folder = probe.value

            def run(job):
                stage = {"name": None}

                def track(phase, done, total):
                    if phase != stage["name"]:
                        stage["name"] = phase
                        job.new_stage("Menulis data uji" if phase == "write" else "Memverifikasi data",
                                      total, folder)
                    job.progress(done, total)

                job.set_phase("Menyiapkan", folder)
                res = capacity_test(folder, size_mb, cancel=job.cancel_event, progress=track, file_mb=file_mb)
                return asdict(res)

            job = jobs.start("capacity", "Uji kapasitas", run, unit="bytes")
            return self._json(202, {"started": job.name})

        return self._error(HTTPStatus.NOT_FOUND, "endpoint tidak ada")


class _ExclusiveServer(ThreadingHTTPServer):
    """HTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind the same
    port too. Turning it off makes a busy port fail loudly instead of being shared."""

    allow_reuse_address = False


def create_server(port: int = 0) -> tuple[ThreadingHTTPServer, AppState]:
    """Create the server bound to 127.0.0.1. Port 0 picks a free port."""
    state = AppState()
    httpd = _ExclusiveServer(("127.0.0.1", port), Handler)
    state.port = httpd.server_address[1]
    httpd.app = state  # type: ignore[attr-defined]
    return httpd, state


def serve_in_thread(port: int = 0) -> tuple[ThreadingHTTPServer, AppState, str]:
    httpd, state = create_server(port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http-server")
    thread.start()
    return httpd, state, f"http://127.0.0.1:{state.port}/"


def main(port: int = 8765, open_browser: bool = True) -> int:
    import webbrowser
    try:
        httpd, state = create_server(port)
    except OSError:
        httpd, state = create_server(0)  # the port is busy: use any free port
    url = f"http://127.0.0.1:{state.port}/"
    print(f"File Recovery berjalan di {url}")
    print("Tekan Ctrl+C untuk berhenti.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        shutil.rmtree(state.preview_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
