"""List disks and partitions so the user can pick a scan source without typing paths.

Each entry: (device_path, description). Listing needs no special rights on most
systems; reading the device later still requires Administrator/root.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

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


def list_disks() -> list[tuple[str, str]]:
    os_name = current_os()
    if os_name == "windows":
        return _windows()
    if os_name == "macos":
        return _macos()
    return _linux()
