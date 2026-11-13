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
import socket
import subprocess
from typing import Optional

from .platform_info import current_os


class UnsafeDestinationError(ValueError):
    pass


def is_device_path(source: str) -> bool:
    if current_os() == "windows":
        # Windows accepts forward slashes too: "//./G:" opens the raw volume just like "\\.\G:".
        upper = source.replace("/", "\\").upper()
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


def _windows_real_path(path: str) -> str:
    """Final, real path of a file or folder (resolves subst drives, junctions and symlinks).

    Uses the nearest existing parent when the folder does not exist yet. Returns a path that
    starts with \\\\?\\ (e.g. \\\\?\\C:\\data or \\\\?\\UNC\\server\\share\\...).
    Raises UnsafeDestinationError when the path cannot be resolved (fail closed).
    """
    import ctypes
    from ctypes import wintypes

    probe = os.path.abspath(path)
    if probe.startswith(_WIN_PREFIXES):
        probe = probe[4:]
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    # 0 access = only query metadata (never reads or writes the disk); backup semantics open folders.
    handle = k32.CreateFileW(probe, 0, 7, None, 3, 0x02000000, None)
    if not handle or handle == invalid:
        raise UnsafeDestinationError(f"Tidak dapat memastikan lokasi tujuan {path!r}. Pilih folder lain.")
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = k32.GetFinalPathNameByHandleW(handle, buf, 32768, 0)
        if not n:
            raise UnsafeDestinationError(f"Tidak dapat memastikan lokasi tujuan {path!r}. Pilih folder lain.")
        return buf.value
    finally:
        k32.CloseHandle(handle)


def _volume_guid(path: str) -> Optional[str]:
    """Unique volume GUID (\\\\?\\Volume{...}\\) of the volume that holds `path`. None if unknown."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumePathNameW.restype = wintypes.BOOL
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    mount = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(path, mount, 1024):
        return None
    guid = ctypes.create_unicode_buffer(100)
    if not k32.GetVolumeNameForVolumeMountPointW(mount.value, guid, 100):
        return None
    return guid.value.upper() or None


def _disk_of_volume(guid: str) -> Optional[int]:
    """Physical disk number that holds the volume with this GUID (None if unknown)."""
    if not re.match(r"^\\\\\?\\VOLUME\{[0-9A-F-]+\}\\$", guid):
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Volume -UniqueId '{guid}' | Get-Partition).DiskNumber"],
            capture_output=True, text=True, timeout=20, check=False)
        text = out.stdout.strip()
        return int(text) if out.returncode == 0 and text.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _is_local_host(host: str) -> bool:
    names = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    try:
        names.add(socket.gethostname().lower())
    except OSError:
        pass
    return host.lower() in names


def _check_windows(source: str, destination: str) -> None:
    src_letter = _win_letter_of_device(source)
    phys = re.match(r"^\\\\[.?]\\PhysicalDrive(\d+)$", source, re.IGNORECASE)
    if not src_letter and not phys:
        # Unknown device form on Windows: fail closed.
        raise UnsafeDestinationError(f"Tidak dapat memastikan disk sumber {source!r}. Gunakan file image.")

    # Resolve the destination to its real location: subst drives, junctions, mount points and
    # network names all end up as the true volume here.
    real = _windows_real_path(destination)
    if real.upper().startswith("\\\\?\\UNC\\"):
        host = real[8:].split("\\", 1)[0]
        if _is_local_host(host):
            raise UnsafeDestinationError(
                "Tujuan lewat nama jaringan ke komputer ini (mis. \\\\localhost\\G$) tidak diizinkan. "
                "Pilih folder lokal di drive lain.")
        return  # a different machine: its disks are not the disk being scanned

    dst_guid = _volume_guid(real)
    if dst_guid is None:
        raise UnsafeDestinationError(f"Tidak dapat memastikan volume tujuan {destination!r}. Pilih folder lain.")
    m = re.match(r"^\\\\\?\\([A-Za-z]):\\", real)
    dst_letter = m.group(1).upper() if m else None

    if src_letter:
        src_guid = _volume_guid(src_letter + ":\\")
        if src_guid is None:
            raise UnsafeDestinationError(f"Tidak dapat memastikan volume sumber {source!r}.")
        # The same volume (also when reached through subst, junction or mount point) is refused.
        # Other volumes stay allowed, as before, so a second partition on the same disk still works.
        if dst_guid == src_guid or dst_letter == src_letter:
            raise UnsafeDestinationError(
                f"Hasil tidak boleh disimpan ke drive {src_letter}: (drive yang sedang dipindai). "
                "Pilih folder di drive lain.")
        return

    # Physical disk source: the destination volume must sit on another disk.
    disk_no = _disk_of_volume(dst_guid)
    if disk_no is None:
        raise UnsafeDestinationError(
            f"Tidak dapat memastikan tujuan {destination!r} berada di disk mana. Pilih drive lain atau file image.")
    if disk_no == int(phys.group(1)):
        raise UnsafeDestinationError(
            f"Tujuan berada di disk fisik yang sama dengan sumber (PhysicalDrive{phys.group(1)}). "
            "Pilih drive di disk lain.")


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
    # Output must always be a file or folder, never a raw device (writing to \\.\PhysicalDriveN
    # would overwrite a whole disk, including the system disk).
    if is_device_path(destination) or destination.replace("/", "\\").upper().startswith(("\\\\.\\", "\\\\?\\")):
        raise UnsafeDestinationError("Tujuan tidak boleh berupa perangkat. Pilih folder atau file biasa.")
    if not is_device_path(source):
        return  # image file or folder: reading it never overwrites recovered data

    os_name = current_os()
    if os_name == "windows":
        source = source.replace("/", "\\")
        _check_windows(source, destination)
    else:
        _check_posix_device(source, destination, os_name)
