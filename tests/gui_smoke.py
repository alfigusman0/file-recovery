"""Headless GUI smoke test (no real dialogs). Run: python tests/gui_smoke.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

from recovery import gui as gui_module  # noqa: E402
from recovery.gui import App  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "fat32_deleted.img")
messages = []
messagebox.showinfo = lambda *a, **k: messages.append(("info", a[1]))
messagebox.showerror = lambda *a, **k: messages.append(("error", a[1]))
messagebox.showwarning = lambda *a, **k: messages.append(("warn", a[1]))
messagebox.askyesno = lambda *a, **k: False  # never confirm a repair in the test


def wait(app, cond, limit=600):
    for _ in range(limit):
        app.update()
        time.sleep(0.02)
        if cond():
            return True
    return False


def main() -> int:
    app = App()
    app.update()
    app.geometry("1000x680+10+10")
    wait(app, lambda: True, 20)
    out = tempfile.mkdtemp()

    # Flash tab: diagnose, surface scan, image, recover, refused repair
    app.flash_src_var.set(FIXTURE)
    app.flash_diagnose()
    assert wait(app, lambda: app.job is None), "diagnose did not finish"
    assert "FAT32" in app.flash_log.get("1.0", "end"), app.flash_log.get("1.0", "end")

    app.flash_surface()
    assert wait(app, lambda: app.job is None), "surface scan did not finish"
    assert "Sehat" == app.flash_status.cget("text"), app.flash_status.cget("text")

    app.flash_repair("fix")  # refused or cancelled (askyesno returns False)
    assert app.job is None, "repair should not start without confirmation"

    # Carve tab: progress text with percent after a run
    app.source_var.set(FIXTURE)
    app.out_var.set(os.path.join(out, "carve"))
    app.start_carve()
    assert wait(app, lambda: app.job is None), "carve did not finish"
    assert app.carve_status.cget("text") in ("Selesai", "Dibatalkan"), app.carve_status.cget("text")

    # File-system scan: indeterminate bar stops at the end
    app.fs_source_var.set(FIXTURE)
    app.start_fs_scan()
    assert wait(app, lambda: app.job is None), "fs scan did not finish"
    assert len(app.fs_tree.get_children()) == 2, app.fs_tree.get_children()

    # Friendly error text
    text = gui_module.friendly_error("Img_Info_Con: access denied")
    assert "Administrator" in text

    # Responsive: narrow window must wrap toolbars without losing widgets
    app.geometry("640x560+10+10")
    for _ in range(40):
        app.update()
        time.sleep(0.02)
    for name in ("flash_tab", "carve_tab", "trash_tab"):
        frame = getattr(app, name)
        assert frame.winfo_width() > 0
    print("GUI SMOKE OK; messages:", [m[0] for m in messages])
    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
