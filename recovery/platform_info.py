"""Platform detection and privilege checks."""

from __future__ import annotations

import ctypes
import os
import platform
import sys


def current_os() -> str:
    """Return 'windows', 'macos' or 'linux'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def is_admin() -> bool:
    """True when running with the privileges needed for raw disk access."""
    if current_os() == "windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def describe() -> str:
    return (
        f"OS={current_os()} ({platform.system()} {platform.release()}), "
        f"Python={platform.python_version()}, admin/root={is_admin()}"
    )


def raw_device_hint(device: str) -> str:
    """Example device path to show users for raw scanning on this OS."""
    os_name = current_os()
    if os_name == "windows":
        return device if device.startswith("\\\\.\\") else f"\\\\.\\{device}"
    if os_name == "macos":
        return device if device.startswith("/dev/") else f"/dev/{device}"
    return device if device.startswith("/dev/") else f"/dev/{device}"
