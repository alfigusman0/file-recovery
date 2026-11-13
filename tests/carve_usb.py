"""Carve recoverable files from the freshly quick-formatted USB drives into a folder on C:."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from recovery.carver import carve_summary  # noqa: E402
from recovery.safety import check_destination  # noqa: E402

BASE = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "usb_carve")
EXTS = ["jpg", "png", "pdf", "zip", "mp4", "mov", "gif", "mp3"]

for letter in ("F", "H"):
    source = rf"\\.\{letter}:"
    out = os.path.join(BASE, letter)
    check_destination(source, out)  # refuses if the output is on the same drive
    summary = carve_summary(source, out, extensions=EXTS)
    kinds = {}
    for r in summary.recovered:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    print(f"{source}: {len(summary.recovered)} file ({sum(1 for r in summary.recovered if r.complete)} utuh) -> {out}")
    print(f"    per jenis: {kinds}")
    print(f"    dilewati di dalam file lain: {summary.skipped_inside_other}")
