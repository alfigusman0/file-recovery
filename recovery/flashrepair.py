"""Diagnose and repair flash drives, SD cards and other memory devices.

Safe order of operations (the GUI follows the same order):
  1. diagnose()       read-only: can the file system be read?
  2. surface_scan()   read-only: read every sector and count unreadable ones
  3. image first      copy the whole device to an image file (see imaging.py)
  4. recover files    from the image, not from the device
  5. repair_command() asks the OS to repair the file system ON THE DEVICE.
                      This writes to the device, so it needs explicit confirmation
                      and is refused for the system drive.

Formatting is deliberately NOT offered: it erases the device.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .carver import read_at, source_size
from .platform_info import current_os
from .safety import is_device_path

try:
    import pytsk3  # type: ignore
    HAVE_TSK = True
except ImportError:  # pragma: no cover
    pytsk3 = None
    HAVE_TSK = False

SURFACE_BLOCK = 1024 * 1024
SUB_BLOCK = 4096


@dataclass
class Diagnosis:
    device: str
    size_bytes: int
    readable: bool
    file_system: str
    status: str                 # "ok", "raw", "unreadable", "unknown"
    message: str
    partitions: list[str] = field(default_factory=list)


@dataclass
class SurfaceResult:
    total_bytes: int
    bytes_read: int
    bad_ranges: list[tuple[int, int]]
    cancelled: bool

    @property
    def bad_bytes(self) -> int:
        return sum(length for _off, length in self.bad_ranges)

    @property
    def healthy(self) -> bool:
        return not self.bad_ranges and not self.cancelled


def _fs_name(ftype: int) -> str:
    names = {"TSK_FS_TYPE_FAT12": "FAT12", "TSK_FS_TYPE_FAT16": "FAT16", "TSK_FS_TYPE_FAT32": "FAT32",
             "TSK_FS_TYPE_FAT_DETECT": "FAT", "TSK_FS_TYPE_NTFS_DETECT": "NTFS",
             "TSK_FS_TYPE_EXT_DETECT": "ext", "TSK_FS_TYPE_HFS_DETECT": "HFS+"}
    for const, name in names.items():
        if hasattr(pytsk3, const) and getattr(pytsk3, const) == ftype:
            return name
    return "tidak dikenali"


def diagnose(device: str) -> Diagnosis:
    """Read-only check of the boot sector and file system (needs pytsk3 for the file system name)."""
    try:
        with open(device, "rb", buffering=0) as f:
            size = source_size(f)
            boot = read_at(f, 0, 512)
    except OSError as exc:
        return Diagnosis(device, 0, False, "-", "unreadable",
                         f"Perangkat tidak bisa dibaca: {exc}. Coba jalankan sebagai Administrator/root, "
                         "atau cabut dan pasang ulang perangkat.")

    if not HAVE_TSK:
        return Diagnosis(device, size, True, "-", "unknown",
                         "pytsk3 belum terpasang; hanya bisa membaca sektor. Jalankan: pip install pytsk3")

    partitions: list[str] = []
    try:
        img = pytsk3.Img_Info(device)
        try:
            volume = pytsk3.Volume_Info(img)
            for part in volume:
                if part.len > 0 and not part.flags & pytsk3.TSK_VS_PART_FLAG_UNALLOC:
                    partitions.append(f"{part.desc.decode(errors='replace')} ({part.len * volume.info.block_size // (1024 * 1024)} MB)")
        except OSError:
            partitions = []
        fs_names = []
        for offset in ([p for p in _partition_offsets(img)] or [0]):
            try:
                fs = pytsk3.FS_Info(img, offset=offset)
                fs_names.append(_fs_name(fs.info.ftype))
            except OSError:
                continue
    except OSError as exc:
        return Diagnosis(device, size, True, "-", "unknown", f"Gagal membuka sistem file: {exc}", partitions)

    if fs_names:
        name = fs_names[0]
        return Diagnosis(device, size, True, name, "ok",
                         f"Sistem file {name} terbaca. Data bisa dipindai dan dipulihkan.", partitions)

    # Boot sector hints: a formatted FAT/NTFS volume that TSK cannot open is damaged, and an
    # all-zero or unknown boot sector usually means RAW (needs format, which we never do here).
    if boot == b"\x00" * 512:
        status, msg = "raw", ("Sektor awal kosong (RAW). Sistem file hilang; data mungkin masih ada. "
                              "Pindai dengan carving atau salin ke image dulu.")
    else:
        status, msg = "unreadable", ("Sistem file tidak dikenali atau rusak. Salin ke image dulu, lalu "
                                     "jalankan perbaikan sistem file, atau pulihkan file dari image.")
    return Diagnosis(device, size, True, "tidak dikenali", status, msg, partitions)


def _partition_offsets(img) -> list[int]:
    offsets = []
    try:
        volume = pytsk3.Volume_Info(img)
        for part in volume:
            if part.len > 0 and not part.flags & pytsk3.TSK_VS_PART_FLAG_UNALLOC:
                offsets.append(part.start * volume.info.block_size)
    except OSError:
        pass
    return offsets


def surface_scan(
    device: str,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> SurfaceResult:
    """Read every sector without writing. Unreadable sectors are recorded, not fixed."""
    bad: list[tuple[int, int]] = []
    done = 0
    cancelled = False
    with open(device, "rb", buffering=0) as f:
        total = source_size(f)
        pos = 0
        while total == 0 or pos < total:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            want = min(SURFACE_BLOCK, total - pos) if total else SURFACE_BLOCK
            try:
                block = read_at(f, pos, want)
            except OSError:
                block = b""
            if not block:
                # Retry sub-block by sub-block to pinpoint the bad sectors.
                got_any = False
                block_bad = []
                for off in range(0, want, SUB_BLOCK):
                    length = min(SUB_BLOCK, want - off)
                    try:
                        part = read_at(f, pos + off, length)
                    except OSError:
                        part = b""
                    if part:
                        got_any = True
                    else:
                        block_bad.append((pos + off, length))
                if not got_any and total == 0:
                    # Size unknown (volumes on Windows report 0) and nothing readable from
                    # here on: we reached the end of the device, which is not a bad sector.
                    break
                bad.extend(block_bad)
                block_len = want
            else:
                block_len = len(block)
            pos += block_len
            done = pos
            if progress:
                progress(done, total)
    return SurfaceResult(total, done, bad, cancelled)


# ---------- OS repair (writes to the device) ----------

def system_drive_letter() -> str:
    return os.environ.get("SystemDrive", "C:").rstrip(":").upper()


def repair_command(device: str, mode: str = "check") -> list[str]:
    """Command that asks the OS to check (mode='check', read-only where possible) or repair ('fix').

    Raises ValueError for the system drive, unknown modes, or device forms without a repair tool.
    """
    if mode not in ("check", "fix"):
        raise ValueError("mode harus 'check' atau 'fix'")
    system = current_os()
    if system == "windows":
        letter = None
        clean = device.rstrip("\\")
        if clean.upper().startswith(("\\\\.\\", "\\\\?\\")):
            clean = clean[4:]
        if len(clean) == 2 and clean[1] == ":" and clean[0].isalpha():
            letter = clean[0].upper()
        if letter is None:
            raise ValueError("Perbaikan Windows membutuhkan huruf drive, contoh E:")
        if letter == system_drive_letter():
            raise ValueError(f"Drive {letter}: adalah drive sistem. Perbaikan tidak diizinkan di sini.")
        cmd = ["chkdsk", f"{letter}:"]
        if mode == "fix":
            cmd.append("/f")
        return cmd

    if system == "macos":
        if not device.startswith("/dev/disk"):
            raise ValueError("macOS membutuhkan /dev/diskNsM")
        if device.startswith("/dev/disk0"):
            raise ValueError("disk0 biasanya disk sistem; perbaikan tidak diizinkan.")
        if mode == "fix":
            return ["diskutil", "repairVolume", device]
        return ["diskutil", "verifyVolume", device]

    # Linux: fsck must run on an unmounted partition.
    if not device.startswith("/dev/"):
        raise ValueError("Linux membutuhkan path /dev/...")
    if device in ("/dev/sda",) or device.rstrip("0123456789") in ("/dev/sda",):
        raise ValueError("/dev/sda sering disk sistem; perbaikan tidak diizinkan di sini.")
    if mode == "fix":
        return ["fsck", "-y", device]
    return ["fsck", "-n", device]


def run_repair(cmd: list[str], timeout: int = 3600, runner: Optional[Callable] = None) -> tuple[int, str]:
    """Run the repair command and return (exit_code, output). `runner` is injectable for tests."""
    if runner is not None:
        return runner(cmd)
    if shutil.which(cmd[0]) is None:
        raise RuntimeError(f"Perintah {cmd[0]} tidak ditemukan di sistem ini")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_raw_device(device: str) -> bool:
    return is_device_path(device)
