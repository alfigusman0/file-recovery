"""Recover files from the operating system's trash / recycle bin.

  Windows: $Recycle.Bin\\<SID>\\$I* (metadata) + $R* (data)
  Linux:   ~/.local/share/Trash/files + info/*.trashinfo (freedesktop spec)
  macOS:   ~/.Trash (no metadata, original path is not stored)
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import struct
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from .platform_info import current_os


@dataclass
class TrashItem:
    name: str
    data_path: str
    original_path: Optional[str]
    deleted_at: Optional[dt.datetime]
    size: int


# ---------- Windows ----------

def _parse_windows_info(path: str) -> tuple[Optional[str], Optional[dt.datetime], int]:
    """Parse a $I file (Windows 10+ version 2 or Vista-era version 1).

    FILETIME is UTC; the result is converted to local time so it matches date filters.
    Raises ValueError for files that are not valid $I records (callers skip them).
    """
    with open(path, "rb") as fh:
        raw = fh.read(4096)
    if len(raw) < 24:
        raise ValueError("$I file too short")
    version = struct.unpack_from("<q", raw, 0)[0]
    size = struct.unpack_from("<q", raw, 8)[0]
    filetime = struct.unpack_from("<q", raw, 16)[0]
    deleted = None
    if 0 < filetime < 2 ** 63 // 10:
        try:
            utc = dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=filetime // 10)
            deleted = utc.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        except (OverflowError, ValueError):
            deleted = None
    if version == 2:
        if len(raw) < 28:
            raise ValueError("$I version 2 record too short")
        name_len = struct.unpack_from("<i", raw, 24)[0]
        if not 0 <= name_len <= 2048:
            raise ValueError("bad $I name length")
        name = raw[28:28 + name_len * 2].decode("utf-16-le", errors="replace").rstrip("\x00")
    else:
        name = raw[24:24 + 520].decode("utf-16-le", errors="replace").rstrip("\x00")
    return name or None, deleted, size


def _windows_items() -> list[TrashItem]:
    items: list[TrashItem] = []
    bin_root = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "$Recycle.Bin")
    if not os.path.isdir(bin_root):
        return items
    try:
        sids = os.listdir(bin_root)
    except OSError:
        return items
    for sid in sids:
        sid_dir = os.path.join(bin_root, sid)
        if not os.path.isdir(sid_dir):
            continue
        try:
            entries = os.listdir(sid_dir)
        except OSError:  # other users' folders need Administrator rights
            continue
        for entry in entries:
            if not entry.startswith("$I"):
                continue
            info_path = os.path.join(sid_dir, entry)
            data_path = os.path.join(sid_dir, "$R" + entry[2:])
            if not os.path.exists(data_path):
                continue
            try:
                original, deleted, size = _parse_windows_info(info_path)
            except (OSError, ValueError, struct.error):
                continue  # corrupt or unreadable metadata: skip this entry, keep the rest
            items.append(TrashItem(
                name=os.path.basename(original or data_path),
                data_path=data_path,
                original_path=original,
                deleted_at=deleted,
                size=size,
            ))
    return items


# ---------- Linux (freedesktop) ----------

def _linux_items() -> list[TrashItem]:
    base = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "Trash")
    files_dir = os.path.join(base, "files")
    info_dir = os.path.join(base, "info")
    items: list[TrashItem] = []
    if not os.path.isdir(files_dir):
        return items
    for name in os.listdir(files_dir):
        data_path = os.path.join(files_dir, name)
        original = None
        deleted = None
        info_path = os.path.join(info_dir, name + ".trashinfo")
        if os.path.exists(info_path):
            with open(info_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Path="):
                        original = urllib.parse.unquote(line[5:].strip())
                    elif line.startswith("DeletionDate="):
                        try:
                            deleted = dt.datetime.fromisoformat(line[13:].strip())
                        except ValueError:
                            pass
        items.append(TrashItem(
            name=name,
            data_path=data_path,
            original_path=original,
            deleted_at=deleted,
            size=os.path.getsize(data_path) if os.path.isfile(data_path) else 0,
        ))
    return items


# ---------- macOS ----------

def _macos_items() -> list[TrashItem]:
    trash = os.path.expanduser("~/.Trash")
    items: list[TrashItem] = []
    if not os.path.isdir(trash):
        return items
    for name in os.listdir(trash):
        path = os.path.join(trash, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        items.append(TrashItem(
            name=name,
            data_path=path,
            original_path=None,
            deleted_at=dt.datetime.fromtimestamp(stat.st_mtime),
            size=stat.st_size,
        ))
    return items


def list_trash() -> list[TrashItem]:
    os_name = current_os()
    if os_name == "windows":
        return _windows_items()
    if os_name == "macos":
        return _macos_items()
    return _linux_items()


def filter_by_deletion(items: list[TrashItem], date_range) -> tuple[list[TrashItem], int]:
    """Keep items deleted inside date_range. Returns (kept, skipped_without_date).

    Items with no deletion date are skipped when a range is set. On macOS the
    date is the file's modification time, because ~/.Trash keeps no deletion record.
    """
    if date_range is None or not date_range.active:
        return list(items), 0
    kept = [i for i in items if date_range.contains(i.deleted_at)]
    skipped = sum(1 for i in items if i.deleted_at is None)
    return kept, skipped


class RestoreError(OSError):
    """Some (or all) files of a trash item could not be copied.

    `target` is where the item was restored to; `failures` lists (path, reason)
    for each file that failed. Subclass of OSError, so callers that handle
    OSError per item keep working.
    """

    def __init__(self, target: str, failures: list[tuple[str, str]]) -> None:
        self.target = target
        self.failures = failures
        first = "; ".join(f"{p}: {r}" for p, r in failures[:3])
        more = f" (+{len(failures) - 3} lagi)" if len(failures) > 3 else ""
        super().__init__(f"{len(failures)} file gagal disalin ke {target}: {first}{more}")


def restore(item: TrashItem, destination_dir: str) -> str:
    """Copy a trash item (file or folder) to destination_dir (never moves/deletes the trash copy).

    A deleted folder ($R folder on Windows, a directory in Linux Trash/files) is
    copied with all its contents. Files inside it that cannot be copied do not
    stop the rest; they are reported together in a RestoreError afterwards.
    """
    os.makedirs(destination_dir, exist_ok=True)
    target = os.path.join(destination_dir, item.name)
    is_dir = os.path.isdir(item.data_path)
    base, ext = (target, "") if is_dir else os.path.splitext(target)
    n = 1
    while os.path.exists(target):
        target = f"{base}_{n}{ext}"
        n += 1
    if not is_dir:
        shutil.copy2(item.data_path, target)
        return target
    try:
        shutil.copytree(item.data_path, target, symlinks=True)
    except shutil.Error as exc:
        failures = [(str(e[0]), str(e[2])) for e in exc.args[0]] if exc.args and isinstance(exc.args[0], list) \
            else [(item.data_path, str(exc))]
        raise RestoreError(target, failures) from exc
    return target
