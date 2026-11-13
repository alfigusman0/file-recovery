"""List disks and partitions so the user can pick a scan source without typing paths.

Each entry: (device_path, description). Listing needs no special rights on most
systems; reading the device later still requires Administrator/root.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
from typing import Optional

from .flashcheck import NOT_RESPONDING, PROBE_TIMEOUT, STUCK, device_key, run_with_timeout
from .platform_info import current_os


def _run(args: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _windows() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    # Drive letters with their label and size (PowerShell is always present on Windows)
    script = ("Get-Volume | Where-Object DriveLetter | "
              "Select-Object DriveLetter,FileSystemLabel,FileSystem,Size | ConvertTo-Json")
    raw = _run(["powershell", "-NoProfile", "-Command", script])
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            for v in data:
                letter = v.get("DriveLetter")
                if not letter:
                    continue
                size_gb = (v.get("Size") or 0) / 1e9
                label = v.get("FileSystemLabel") or "tanpa label"
                result.append((f"\\\\.\\{letter}:",
                               f"Volume {letter}: {label}, {v.get('FileSystem') or '?'}, {size_gb:.1f} GB"))
        except (ValueError, TypeError):
            pass
    # Physical disks (needs Administrator for full info, but the list is usually visible)
    for n in range(4):
        path = f"\\\\.\\PhysicalDrive{n}"
        if os.path.exists(path):
            result.append((path, f"Disk fisik {n} (seluruh disk, butuh Administrator)"))
    return result


def _linux() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    raw = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT"])
    if not raw:
        return result
    try:
        data = json.loads(raw)
    except ValueError:
        return result

    def walk(devices):
        for d in devices or []:
            kind = d.get("type")
            if kind in ("disk", "part"):
                size_gb = int(d.get("size") or 0) / 1e9
                label = d.get("label") or d.get("fstype") or "-"
                mount = d.get("mountpoint") or "tidak di-mount"
                result.append((f"/dev/{d['name']}",
                               f"{kind} {d['name']}: {size_gb:.1f} GB, {label}, {mount}"))
            walk(d.get("children") or [])

    walk(data.get("blockdevices", []))
    return result


def _macos() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    raw = _run(["diskutil", "list"])
    if not raw:
        return result
    for line in raw.splitlines():
        if line.startswith("/dev/disk"):
            result.append((line.split()[0], line.strip()))
    return result


def _raw_list() -> list[tuple[str, str]]:
    os_name = current_os()
    if os_name == "windows":
        return _windows()
    if os_name == "macos":
        return _macos()
    return _linux()


# ---------- hang-protected listing ----------

LIST_KEY = "__disk_list__"
LIST_COOLDOWN = 30.0
STUCK_TAG = " [tidak merespons]"
_cache_lock = threading.Lock()
_cache: Optional[list[tuple[str, str]]] = None


class DiskInfo(tuple):
    """(path, desc) pair, so `for path, desc in list_disks()` keeps working, plus .stuck."""

    def __new__(cls, path: str, desc: str, stuck: bool = False):
        obj = super().__new__(cls, (path, desc))
        obj.stuck = stuck
        return obj

    @property
    def path(self) -> str:
        return self[0]

    @property
    def desc(self) -> str:
        return self[1]


def _refresh() -> list[tuple[str, str]]:
    global _cache
    disks = _raw_list()
    with _cache_lock:  # a late answer after a timeout still refreshes the cache
        _cache = disks
    return disks


def _fallback() -> list[tuple[str, str]]:
    """Drive letters without touching the devices (Windows only)."""
    if current_os() != "windows":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return []
    return [(f"\\\\.\\{chr(65 + i)}:", f"Volume {chr(65 + i)}: (detail belum tersedia)")
            for i in range(26) if mask & (1 << i)]


def list_disks_info(timeout: float = PROBE_TIMEOUT) -> dict:
    """Disk list that never blocks longer than `timeout` seconds.

    Returns {"disks": [{"path", "desc", "stuck"}], "partial": bool, "message": str,
    "stuck_devices": {...}}. When the OS query hangs, the last known list (or plain
    drive letters) comes back with partial=True.
    """
    res = run_with_timeout(_refresh, timeout=timeout, key=LIST_KEY, cooldown=LIST_COOLDOWN)
    partial, message = False, ""
    if res.ok:
        raw = res.value or []
    else:
        partial = True
        with _cache_lock:
            raw = list(_cache) if _cache is not None else _fallback()
        message = ("Daftar disk belum lengkap: sistem " + NOT_RESPONDING + " saat membaca daftar disk "
                   "(mungkin ada perangkat yang macet). Coba muat ulang sebentar lagi."
                   if res.timed_out else f"Gagal membaca daftar disk: {res.error}")
    disks = []
    for path, desc in raw:
        stuck = STUCK.is_stuck(device_key(path))
        disks.append({"path": path, "desc": desc + (STUCK_TAG if stuck else ""), "stuck": stuck})
    return {"disks": disks, "partial": partial, "message": message, "stuck_devices": STUCK.snapshot()}


def list_disks(timeout: float = PROBE_TIMEOUT) -> list[DiskInfo]:
    """List of DiskInfo (path, desc) pairs with a .stuck flag. Never blocks longer than `timeout`."""
    return [DiskInfo(d["path"], d["desc"], d["stuck"]) for d in list_disks_info(timeout)["disks"]]
