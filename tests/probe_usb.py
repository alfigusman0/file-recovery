"""Read-only probe of USB drives. Run as Administrator for raw access."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from recovery.flashrepair import diagnose, surface_scan  # noqa: E402

DEVICES = [r"\\.\F:", r"\\.\H:", r"\\.\PhysicalDrive1", r"\\.\PhysicalDrive2"]

for dev in DEVICES:
    d = diagnose(dev)
    print(f"{dev:<22} status={d.status:<11} fs={d.file_system:<16} size={d.size_bytes}")
    print(f"    {d.message[:160]}")
    if d.status != "unreadable" or d.size_bytes:
        try:
            r = surface_scan(dev)
            print(f"    surface: read={r.bytes_read} bad_ranges={len(r.bad_ranges)} healthy={r.healthy}")
        except OSError as exc:
            print(f"    surface error: {exc}")
