"""File system metadata recovery using The Sleuth Kit (pytsk3).

Unlike carving, this keeps original file names, folder paths, sizes and
timestamps for files whose directory entry is still on the disk (deleted
entries). Requires the optional package:  pip install pytsk3

Notes on timestamps:
  - NTFS does not store a deletion time. `ctime` (MFT record change) is used
    as the approximate deletion time, because the record changes when deleted.
  - FAT/exFAT keep the last write time only; treated the same way.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .dates import DateRange

try:
    import pytsk3  # type: ignore
    HAVE_TSK = True
except ImportError:  # pragma: no cover - depends on environment
    pytsk3 = None
    HAVE_TSK = False

MAX_DEPTH = 64
READ_BLOCK = 4 * 1024 * 1024


class TskUnavailable(RuntimeError):
    pass


@dataclass
class DeletedEntry:
    path: str           # path inside the file system, e.g. "Docs/cv.pdf"
    name: str
    size: int
    inode: int          # meta address (needed to read contents)
    fs_type: str
    crtime: Optional[dt.datetime]
    mtime: Optional[dt.datetime]
    ctime: Optional[dt.datetime]   # approximate deletion time
    fs_handle: object = field(default=None, repr=False, compare=False)
    partition: int = 0

    @property
    def deleted_at(self) -> Optional[dt.datetime]:
        """Approximate deletion time: ctime when the file system has it, else mtime."""
        return self.ctime or self.mtime


def _to_dt(epoch: int) -> Optional[dt.datetime]:
    if not epoch:
        return None
    try:
        return dt.datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return None


def _require_tsk() -> None:
    if not HAVE_TSK:
        raise TskUnavailable("pytsk3 belum terpasang. Jalankan: pip install pytsk3")


def open_file_systems(source: str):
    """Yield (partition_index, fs_info, img) for every readable file system in source."""
    _require_tsk()
    img = pytsk3.Img_Info(source)
    found = False
    try:
        volume = pytsk3.Volume_Info(img)
        for i, part in enumerate(volume):
            if part.len <= 0 or part.flags & pytsk3.TSK_VS_PART_FLAG_UNALLOC:
                continue
            try:
                fs = pytsk3.FS_Info(img, offset=part.start * volume.info.block_size)
                found = True
                yield i, fs, img
            except OSError:
                continue
    except OSError:
        pass  # no partition table: try the whole source as one file system
    if not found:
        try:
            fs = pytsk3.FS_Info(img, offset=0)
            yield 0, fs, img
        except OSError:
            return


_FS_NAMES = {
    "TSK_FS_TYPE_NTFS_DETECT": "NTFS",
    "TSK_FS_TYPE_FAT12": "FAT12",
    "TSK_FS_TYPE_FAT16": "FAT16",
    "TSK_FS_TYPE_FAT32": "FAT32",
    "TSK_FS_TYPE_FAT_DETECT": "FAT",
    "TSK_FS_TYPE_EXFAT_DETECT": "exFAT",
    "TSK_FS_TYPE_EXT_DETECT": "ext",
    "TSK_FS_TYPE_HFS_DETECT": "HFS+",
}


def _fs_name(fs) -> str:
    """Human readable file system name. Constants missing in a pytsk3 build are skipped."""
    if not HAVE_TSK:
        return "unknown"
    try:
        ftype = fs.info.ftype
    except Exception:  # noqa: BLE001
        return "unknown"
    for const, name in _FS_NAMES.items():
        if hasattr(pytsk3, const) and getattr(pytsk3, const) == ftype:
            return name
    return "lain"


def count_directories(fs, cancel: Optional[threading.Event] = None) -> int:
    """Number of directories the walk will open (root included). Metadata only, no file data."""
    total = 0
    stack = [("/", 0)]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        path, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        total += 1
        try:
            directory = fs.open_dir(path=path)
        except OSError:
            continue
        for entry in directory:
            name_obj = entry.info.name
            if name_obj is None or name_obj.name in (b".", b".."):
                continue
            meta = entry.info.meta
            if name_obj.type == pytsk3.TSK_FS_NAME_TYPE_DIR and meta is not None                     and not name_obj.flags & pytsk3.TSK_FS_NAME_FLAG_UNALLOC:
                child = path.rstrip("/") + "/" + name_obj.name.decode("utf-8", errors="replace")
                stack.append((child, depth + 1))
    return total


def scan_deleted(
    source: str,
    date_range: Optional[DateRange] = None,
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[str], None]] = None,
    item_progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[DeletedEntry]:
    """List deleted files (directory entries marked unallocated) in source.

    date_range filters on the approximate deletion time (ctime). Entries
    without a time are skipped when a range is set.

    item_progress(done_dirs, total_dirs, text) is called while walking. The total
    comes from a quick directory count made first, so the GUI can show a percent
    and an estimated time left.
    """
    entries: list[DeletedEntry] = []
    for part_index, fs, _img in open_file_systems(source):
        fs_type = _fs_name(fs)
        if progress:
            progress(f"Menghitung folder partisi {part_index} ({fs_type}) ...")
        total_dirs = count_directories(fs, cancel)
        if progress:
            progress(f"Membaca partisi {part_index} ({fs_type}): {total_dirs} folder ...")
        root = fs.open_dir(path="/")
        counter = _Counter(progress, part_index, fs_type, item_progress, total_dirs)
        _walk(fs, root, "", fs_type, part_index, entries, cancel, depth=0, progress=counter)
    if date_range is not None and date_range.active:
        entries = [e for e in entries if date_range.contains(e.deleted_at)]
    return entries


class _Counter:
    """Reports how many directories and deleted files were visited so far."""

    def __init__(self, callback, part: int, fs_type: str, item_callback=None, total_dirs: int = 0):
        self.callback = callback
        self.item_callback = item_callback
        self.dirs = 0
        self.found = 0
        self.part = part
        self.fs_type = fs_type
        self.total_dirs = total_dirs

    def __call__(self, _unused=None):
        self.dirs += 1
        if self.item_callback and (self.dirs % 10 == 0 or self.dirs >= self.total_dirs):
            self.item_callback(min(self.dirs, self.total_dirs or self.dirs), self.total_dirs,
                               f"Partisi {self.part} ({self.fs_type})")
        if self.callback and self.dirs % 25 == 0:
            self.callback(f"Partisi {self.part} ({self.fs_type}): {self.dirs} dari {self.total_dirs} folder, "
                          f"{self.found} file dihapus ditemukan")


def _walk(fs, directory, prefix: str, fs_type: str, part: int, out: list[DeletedEntry],
          cancel, depth: int, progress) -> None:
    if depth > MAX_DEPTH:
        return
    if cancel is not None and cancel.is_set():
        return
    if progress is not None:
        progress()
    for entry in directory:
        name_obj = entry.info.name
        if name_obj is None:
            continue
        raw_name = name_obj.name
        if raw_name in (b".", b".."):
            continue
        name = raw_name.decode("utf-8", errors="replace")
        meta = entry.info.meta
        is_dir = name_obj.type == pytsk3.TSK_FS_NAME_TYPE_DIR
        deleted = bool(name_obj.flags & pytsk3.TSK_FS_NAME_FLAG_UNALLOC)
        path = f"{prefix}{name}"

        if is_dir:
            if meta is not None and not deleted:
                try:
                    _walk(fs, fs.open_dir(inode=meta.addr), path + "/", fs_type, part, out,
                          cancel, depth + 1, progress)
                except OSError:
                    pass
            continue
        # A deleted name whose metadata record is allocated again belongs to another file.
        reused = meta is not None and bool(meta.flags & pytsk3.TSK_FS_META_FLAG_ALLOC) if HAVE_TSK else False
        if deleted and meta is not None and meta.size >= 0 and not reused:
            if progress is not None and hasattr(progress, "found"):
                progress.found += 1
            out.append(DeletedEntry(
                path=path,
                name=name,
                size=int(meta.size),
                inode=int(meta.addr),
                fs_type=fs_type,
                crtime=_to_dt(meta.crtime),
                mtime=_to_dt(meta.mtime),
                ctime=_to_dt(meta.ctime),
                fs_handle=fs,
                partition=part,
            ))


class PartialRecoveryError(IOError):
    """Raised when fewer bytes than the recorded size could be read."""


def read_entry(entry: DeletedEntry, dest_path: str, cancel: Optional[threading.Event] = None) -> int:
    """Copy one deleted entry's data to dest_path. Returns bytes written.

    Raises PartialRecoveryError when the data cannot be read in full (the partial
    file is kept, so the user can inspect it). Cancelled copies are not errors.
    """
    _require_tsk()
    if entry.fs_handle is None:
        raise TskUnavailable("Entri tidak terhubung ke file system yang sudah dibuka")
    f = entry.fs_handle.open_meta(inode=entry.inode)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    written = 0
    with open(dest_path, "wb") as out:
        offset = 0
        while offset < entry.size:
            if cancel is not None and cancel.is_set():
                break
            want = min(READ_BLOCK, entry.size - offset)
            data = f.read_random(offset, want)
            if not data:
                break
            out.write(data)
            written += len(data)
            offset += len(data)
    if written < entry.size and not (cancel is not None and cancel.is_set()):
        raise PartialRecoveryError(f"hanya {written} dari {entry.size} byte yang bisa dibaca")
    return written


_RESERVED_WIN = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_INVALID_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')


def safe_component(name: str) -> str:
    """Make one path component safe on every OS (no traversal, no reserved names)."""
    cleaned = _INVALID_CHARS.sub("_", name).strip().rstrip(". ")
    if cleaned in ("", ".", ".."):
        cleaned = "_"
    if cleaned.split(".")[0].upper() in _RESERVED_WIN:
        cleaned = "_" + cleaned
    return cleaned


def safe_relative_path(path: str, keep_structure: bool = True) -> list[str]:
    """Split an entry path into safe components (folder names first, file name last)."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not keep_structure:
        parts = parts[-1:]
    return [safe_component(p) for p in parts] or ["_"]


def recover_entries(
    source: str,
    entries: list[DeletedEntry],
    out_dir: str,
    keep_structure: bool = True,
    cancel: Optional[threading.Event] = None,
) -> list[tuple[str, int, Optional[str]]]:
    """Recover selected entries. Returns (dest_path, bytes, error) per entry.

    Files keep their original name. With keep_structure, the folder layout from
    the disk is recreated under out_dir. Name clashes get a numeric suffix.
    """
    results = []
    root = os.path.realpath(out_dir)
    for e in entries:
        if cancel is not None and cancel.is_set():
            break
        dest = _unique_path(os.path.join(root, *safe_relative_path(e.path, keep_structure)))
        if os.path.commonpath([root, os.path.realpath(dest)]) != root:
            results.append((dest, 0, "path keluar dari folder tujuan, dilewati"))
            continue
        try:
            written = read_entry(e, dest, cancel)
            results.append((dest, written, None))
        except PartialRecoveryError as exc:
            partial = os.path.getsize(dest) if os.path.exists(dest) else 0
            results.append((dest, partial, f"sebagian: {exc}"))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the batch
            results.append((dest, 0, str(exc)))
    return results


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"
