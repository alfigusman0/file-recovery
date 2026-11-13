"""Safety checks that protect the disk being recovered from.

Writing recovered data onto the same disk you are scanning can overwrite the
very files you want back. These checks refuse that case.

The rule is FAIL CLOSED: when the source is a raw device and we cannot prove
the destination lives on a different disk, the destination is refused. Image
files as sources are not restricted, because reading an image never overwrites
recovered data.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from typing import Optional

from .platform_info import current_os


class UnsafeDestinationError(ValueError):
    pass


def is_device_path(source: str) -> bool:
    if current_os() == "windows":
        upper = source.upper()
        return (upper.startswith(("\\\\.\\", "\\\\?\\")) and not upper.startswith("\\\\?\\UNC\\")) \
            and not os.path.isfile(source)
    return source.startswith("/dev/")


# ---------- Windows ----------

_WIN_PREFIXES = ("\\\\?\\", "\\\\.\\")


def _win_letter_of_device(source: str) -> Optional[str]:
    """Drive letter for \\\\.\\X: or \\\\?\\X:, None for other device paths."""
    for pref in _WIN_PREFIXES:
        if source.startswith(pref):
            m = re.match(r"^([A-Za-z]):\\?$", source[len(pref):])
            return m.group(1).upper() if m else None
    return None


def _win_letter_of_path(path: str) -> Optional[str]:
    """Drive letter of a path, handling \\\\?\\ prefixes. None for UNC or unknown forms."""
    p = path
    for pref in _WIN_PREFIXES:
        if p.startswith(pref):
            p = p[len(pref):]
            break
    else:
        p = os.path.abspath(p)
    if p.upper().startswith("UNC\\"):
        return None
    m = re.match(r"^([A-Za-z]):", p)
    return m.group(1).upper() if m else None


def _windows_disk_number(letter: str) -> Optional[int]:
    """Physical disk number that holds a drive letter (None if unknown)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-Partition -DriveLetter {letter}).DiskNumber"],
            capture_output=True, text=True, timeout=20, check=False)
        text = out.stdout.strip()
        return int(text) if out.returncode == 0 and text.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _check_windows(source: str, destination: str) -> None:
    src_letter = _win_letter_of_device(source)
    phys = re.match(r"^\\\\[.?]\\PhysicalDrive(\d+)$", source, re.IGNORECASE)
    if not src_letter and not phys:
        # Unknown device form on Windows: fail closed.
        raise UnsafeDestinationError(f"Tidak dapat memastikan disk sumber {source!r}. Gunakan file image.")

    dst_letter = _win_letter_of_path(destination)
    if dst_letter is None:
        return  # UNC/network share: a different machine or disk
    if src_letter and dst_letter == src_letter:
        raise UnsafeDestinationError(
            f"Hasil tidak boleh disimpan ke drive {dst_letter}: (drive yang sedang dipindai). "
            "Pilih folder di drive lain.")
    if phys:
        disk_no = _windows_disk_number(dst_letter)
        if disk_no is None:
            raise UnsafeDestinationError(
                f"Tidak dapat memastikan drive {dst_letter}: berada di disk mana. Pilih drive lain atau file image.")
        if disk_no == int(phys.group(1)):
            raise UnsafeDestinationError(
                f"Drive {dst_letter}: berada di disk fisik yang sama dengan sumber "
                f"(PhysicalDrive{phys.group(1)}). Pilih drive di disk lain.")


# ---------- Linux ----------

def _linux_parent_disk(devno: int) -> Optional[str]:
    """Whole-disk name (e.g. 'sdb', 'nvme0n1') for a block device number, via sysfs."""
    major, minor = os.major(devno), os.minor(devno)
    link = f"/sys/dev/block/{major}:{minor}"
    try:
        real = os.path.realpath(link)
    except OSError:
        return None
    if not os.path.isdir(real):
        return None
    if os.path.exists(os.path.join(real, "partition")):
        return os.path.basename(os.path.dirname(real))
    return os.path.basename(real)


def _check_posix_device(source: str, destination: str, os_name: str) -> None:
    try:
        src_st = os.stat(source)
    except OSError:
        raise UnsafeDestinationError(f"Tidak dapat membaca sumber {source}.")
    import stat as _stat
    if not _stat.S_ISBLK(src_st.st_mode) and not _stat.S_ISCHR(src_st.st_mode):
        return  # regular file pretending to be /dev/...: not a device

    dest_probe = os.path.abspath(destination)
    while not os.path.exists(dest_probe):  # the folder may not exist yet
        parent = os.path.dirname(dest_probe)
        if parent == dest_probe:
            break
        dest_probe = parent
    try:
        dst_st = os.stat(dest_probe)
    except OSError:
        raise UnsafeDestinationError("Tidak dapat memastikan tujuan hasil. Pilih folder lain.")

    if os_name == "linux":
        src_disk = _linux_parent_disk(src_st.st_rdev)
        dst_disk = _linux_parent_disk(dst_st.st_dev)
        if src_disk is None or dst_disk is None:
            raise UnsafeDestinationError(
                "Tidak dapat memastikan tujuan berada di disk lain (sysfs tidak terbaca). "
                "Pilih file image sebagai sumber, atau pilih folder di disk lain.")
        if src_disk == dst_disk or dst_st.st_dev == src_st.st_rdev:
            raise UnsafeDestinationError(
                f"Hasil tidak boleh disimpan ke disk {src_disk} (sama dengan sumber {source}).")
        return

    # macOS: ask diskutil which whole disk holds the destination.
    src_whole = re.sub(r"^/dev/r?(disk\d+).*$", r"\1", source)
    try:
        out = subprocess.run(["diskutil", "info", "-plist", dest_probe], capture_output=True, timeout=20, check=False)
        info = plistlib.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        info = {}
    dst_whole = info.get("ParentWholeDisk")
    if not dst_whole:
        raise UnsafeDestinationError(
            "Tidak dapat memastikan tujuan berada di disk lain (diskutil gagal). Pilih file image sebagai sumber.")
    if dst_whole == src_whole:
        raise UnsafeDestinationError(f"Hasil tidak boleh disimpan ke {dst_whole} (sama dengan sumber {source}).")


# ---------- entry point ----------

def check_destination(source: str, destination: str) -> None:
    """Raise UnsafeDestinationError if destination may be on the same disk as a raw device source."""
    if not destination:
        raise UnsafeDestinationError("Folder tujuan belum dipilih")
    if not is_device_path(source):
        return  # image file or folder: reading it never overwrites recovered data

    os_name = current_os()
    if os_name == "windows":
        _check_windows(source, destination)
    else:
        _check_posix_device(source, destination, os_name)
