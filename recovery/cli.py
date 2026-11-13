"""Command line interface. Works the same on Windows, macOS and Linux.

Examples:
  python -m recovery info
  python -m recovery trash list
  python -m recovery trash restore --index 3 --to ./restored
  python -m recovery carve --source disk.img --out ./carved --ext jpg png pdf
  python -m recovery carve --source /dev/sdb --out ./carved          (Linux/macOS, run as root)
  python -m recovery carve --source \\\\.\\E: --out .\\carved          (Windows, run as Administrator)
  python -m recovery repair --path ./carved --out ./repaired
  python -m recovery capacity E:\\ --mb 2048                           (fake-capacity test)
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .carver import carve_summary
from .dates import make_range
from .disks import list_disks
from .fsmeta import TskUnavailable, recover_entries, scan_deleted
from .imaging import export_entries, image_source
from .platform_info import describe, is_admin, raw_device_hint
from .safety import is_device_path
from .repair import repair_file
from .safety import UnsafeDestinationError, check_destination
from .trash import filter_by_deletion, list_trash, restore


def _progress(done: int, total: int) -> None:
    if total:
        pct = done * 100 // total
        print(f"\rscanning: {pct:3d}% ({done // (1024 * 1024)} MB)", end="", file=sys.stderr, flush=True)
    else:
        print(f"\rscanning: {done // (1024 * 1024)} MB", end="", file=sys.stderr, flush=True)


def cmd_info(_args) -> int:
    print(f"filerecovery {__version__}")
    print(describe())
    print("Raw disk scanning needs Administrator (Windows) or root (macOS/Linux).")
    print("Example device paths:")
    print("  Windows: \\\\.\\C:  or  \\\\.\\PhysicalDrive1")
    print("  Linux:   /dev/sdb1 or /dev/nvme0n1p1")
    print("  macOS:   /dev/disk2  (grant Full Disk Access to your terminal)")
    return 0


def cmd_trash_list(args) -> int:
    try:
        date_range = make_range(getattr(args, "date_from", "") or "", getattr(args, "date_to", "") or "")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    items, _skipped = filter_by_deletion(list_trash(), date_range)
    if not items:
        print("Trash is empty, not accessible, or no items in the date range.")
        return 0
    for i, item in enumerate(items):
        when = item.deleted_at.strftime("%Y-%m-%d %H:%M") if item.deleted_at else "unknown"
        print(f"[{i}] {item.name}  size={item.size}  deleted={when}  original={item.original_path or '-'}")
    return 0


def cmd_trash_restore(args) -> int:
    # Same filter and order as `trash list`, so the --index numbers match.
    try:
        date_range = make_range(args.date_from or "", args.date_to or "")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    items, _skipped = filter_by_deletion(list_trash(), date_range)
    if not 0 <= args.index < len(items):
        print(f"index out of range (0..{len(items) - 1})", file=sys.stderr)
        return 1
    try:
        target = restore(items[args.index], args.to)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"restored to {target}")
    return 0


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("harus lebih dari 0")
    return value


def _source_ok(source: str) -> bool:
    """A raw device path is accepted even when os.path.exists is False for it (Windows \\\\.\\C:)."""
    if is_device_path(source):
        return True
    return os.path.exists(source)


def cmd_carve(args) -> int:
    if not _source_ok(args.source):
        print(f"source not found: {args.source}", file=sys.stderr)
        print(f"hint: {raw_device_hint(args.source)}", file=sys.stderr)
        return 1
    if not is_admin() and is_device_path(args.source):
        print("warning: raw device access usually requires Administrator/root", file=sys.stderr)
    try:
        date_range = make_range(args.date_from or "", args.date_to or "")
        check_destination(args.source, args.out)
        summary = carve_summary(args.source, args.out, extensions=args.ext, progress=_progress,
                                max_files=args.max_files, date_range=date_range)
    except UnsafeDestinationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    found = summary.recovered
    print(file=sys.stderr)
    complete = sum(1 for r in found if r.complete)
    print(f"recovered {len(found)} file(s) ({complete} complete) into {args.out}")
    if summary.skipped_by_date:
        print(f"skipped {summary.skipped_by_date} file(s) outside the date range")
    print(f"report: {os.path.join(args.out, 'recovery_report.json')}")
    return 0


def cmd_repair(args) -> int:
    paths: list[str] = []
    if os.path.isdir(args.path):
        for root, _dirs, files in os.walk(args.path):
            paths += [os.path.join(root, f) for f in files if not f.startswith("repaired_")]
    else:
        paths = [args.path]
    ok = 0
    for p in paths:
        result = repair_file(p, args.out)
        status = "OK  " if result.ok else "FAIL"
        print(f"{status} {p}")
        for action in result.actions:
            print(f"     - {action}")
        ok += result.ok
    print(f"repaired {ok}/{len(paths)} file(s) into {args.out}")
    return 0 if ok else 2


def cmd_gui(_args) -> int:
    from .webapp.server import main as serve
    return serve()


def cmd_disks(_args) -> int:
    for path, desc in list_disks():  # desc ends with "[tidak merespons]" for a stuck device
        print(f"{path:<24} {desc}")
    return 0


def cmd_capacity(args) -> int:
    from .flashcheck import CapacityRefused, capacity_test

    print(f"PERINGATAN: uji ini mengisi ruang kosong di {args.folder} dengan data uji "
          "(folder capacity_test, dihapus lagi di akhir). Salin data penting dulu.", file=sys.stderr)

    def progress(phase: str, done: int, total: int) -> None:
        label = "menulis" if phase == "write" else "verifikasi"
        pct = f"{done * 100 // total:3d}%" if total else "    "
        print(f"\r{label}: {pct} ({done // (1024 * 1024)} MB)   ", end="", file=sys.stderr, flush=True)

    try:
        result = capacity_test(args.folder, args.mb, progress=progress, file_mb=args.file_mb)
    except CapacityRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(file=sys.stderr)
    mb = 1024 * 1024
    print(f"ditulis:       {result.bytes_written // mb} MB ({result.write_mb_per_s} MB/s)")
    print(f"terverifikasi: {result.bytes_verified_ok // mb} MB ({result.read_mb_per_s} MB/s)")
    print(f"blok berbeda:  {result.mismatched_blocks}, tidak terbaca: {result.unreadable_blocks}")
    if result.first_bad_offset is not None:
        print(f"rusak mulai:   offset {result.first_bad_offset} ({result.first_bad_offset // mb} MB)")
    print(result.verdict)
    if result.cancelled:
        return 130
    return 0 if result.ok else 2


def cmd_scan_deleted(args) -> int:
    try:
        date_range = make_range(args.date_from or "", args.date_to or "")
        entries = scan_deleted(args.source, date_range=date_range)
    except (ValueError, TskUnavailable, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.match:
        entries = [e for e in entries if args.match.lower() in e.path.lower()]
    for i, e in enumerate(entries):
        when = e.deleted_at.strftime("%Y-%m-%d %H:%M") if e.deleted_at else "-"
        print(f"[{i}] {e.path}  size={e.size}  fs={e.fs_type}  deleted~={when}")
    print(f"{len(entries)} deleted file(s) found")
    if args.export:
        print(f"exported to {export_entries(entries, args.export)}")
    return 0


def cmd_recover_deleted(args) -> int:
    try:
        date_range = make_range(args.date_from or "", args.date_to or "")
        check_destination(args.source, args.out)
        entries = scan_deleted(args.source, date_range=date_range)
    except UnsafeDestinationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (ValueError, TskUnavailable, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.match:
        entries = [e for e in entries if args.match.lower() in e.path.lower()]
    if not entries:
        print("nothing to recover")
        return 0
    ok = 0
    for dest, written, err in recover_entries(args.source, entries, args.out):
        if err:
            print(f"FAIL {dest}: {err}")
        else:
            ok += 1
            print(f"OK   {dest} ({written} bytes)")
    print(f"recovered {ok}/{len(entries)} file(s) into {args.out}")
    return 0 if ok == len(entries) else 2


def _copy_progress(done: int, total: int) -> None:
    pct = f"{done * 100 // total:3d}%" if total else "    "
    print(f"\rcopying: {pct} ({done // (1024 * 1024)} MB)", end="", file=sys.stderr, flush=True)


def cmd_image(args) -> int:
    try:
        check_destination(args.source, os.path.dirname(os.path.abspath(args.out)))
        result = image_source(args.source, args.out, progress=_copy_progress)
    except UnsafeDestinationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(file=sys.stderr)
    print(f"imaged {result.bytes_copied} of {result.total_bytes} bytes to {result.image_path}")
    print(f"sha256: {result.sha256}")
    if result.bad_ranges:
        print(f"warning: {len(result.bad_ranges)} unreadable block(s) were zero-filled")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="filerecovery", description="Cross-platform file recovery")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show platform and privilege info").set_defaults(func=cmd_info)

    trash = sub.add_parser("trash", help="work with the recycle bin / trash")
    tsub = trash.add_subparsers(dest="trash_cmd", required=True)
    tl = tsub.add_parser("list", help="list items in the trash")
    tl.add_argument("--from", dest="date_from", help="deletion date from, YYYY-MM-DD")
    tl.add_argument("--to", dest="date_to", help="deletion date to, YYYY-MM-DD")
    tl.set_defaults(func=cmd_trash_list)
    tr = tsub.add_parser("restore", help="copy a trash item to a folder")
    tr.add_argument("--index", type=int, required=True, help="index from 'trash list' with the same --from/--to")
    tr.add_argument("--to", required=True, help="destination folder")
    tr.add_argument("--from", dest="date_from", help="deletion date from, YYYY-MM-DD")
    tr.add_argument("--to-date", dest="date_to", help="deletion date to, YYYY-MM-DD")
    tr.set_defaults(func=cmd_trash_restore)

    sub.add_parser("disks", help="list disks and partitions").set_defaults(func=cmd_disks)
    sub.add_parser("gui", help="open the web interface in your browser").set_defaults(func=cmd_gui)

    c = sub.add_parser("carve", help="recover files from a disk, partition or image")
    c.add_argument("--source", required=True, help="image file or raw device path")
    c.add_argument("--out", required=True, help="output folder")
    c.add_argument("--ext", nargs="*", help="only these extensions, e.g. jpg png pdf")
    c.add_argument("--max-files", type=positive_int, default=None, help="stop after N files")
    c.add_argument("--from", dest="date_from", help="file date from, YYYY-MM-DD (embedded date)")
    c.add_argument("--to", dest="date_to", help="file date to, YYYY-MM-DD (embedded date)")
    c.set_defaults(func=cmd_carve)

    sd = sub.add_parser("scan-deleted", help="list deleted files with names and paths (needs pytsk3)")
    sd.add_argument("--source", required=True)
    sd.add_argument("--from", dest="date_from", help="deletion date from, YYYY-MM-DD")
    sd.add_argument("--to", dest="date_to", help="deletion date to, YYYY-MM-DD")
    sd.add_argument("--match", help="only paths containing this text")
    sd.add_argument("--export", help="write list to .csv or .json")
    sd.set_defaults(func=cmd_scan_deleted)

    rd = sub.add_parser("recover-deleted", help="recover deleted files keeping names and folders")
    rd.add_argument("--source", required=True)
    rd.add_argument("--out", required=True)
    rd.add_argument("--from", dest="date_from", help="deletion date from, YYYY-MM-DD")
    rd.add_argument("--to", dest="date_to", help="deletion date to, YYYY-MM-DD")
    rd.add_argument("--match", help="only paths containing this text (default: all)")
    rd.set_defaults(func=cmd_recover_deleted)

    im = sub.add_parser("image", help="copy a disk to an image file before recovery (safest)")
    im.add_argument("--source", required=True)
    im.add_argument("--out", required=True, help="image file to create, e.g. ./disk.img")
    im.set_defaults(func=cmd_image)

    cap = sub.add_parser("capacity", help="fake-capacity test: write test data to free space, read it back")
    cap.add_argument("folder", help="folder on the flash drive, e.g. E:\\ (not the system drive)")
    cap.add_argument("--mb", type=int, default=0, help="MB to test (default 0 = all free space)")
    cap.add_argument("--file-mb", type=positive_int, default=1024, help="size of each test file in MB")
    cap.set_defaults(func=cmd_capacity)

    r = sub.add_parser("repair", help="repair damaged files (file or folder)")
    r.add_argument("--path", required=True)
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_repair)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
