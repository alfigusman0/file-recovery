"""Graphical interface (tkinter, included with Python on Windows, macOS and Linux).

Run with:  python -m recovery.gui   (or double-click run_gui.pyw on Windows)

Design:
  - One background job at a time. Every job has its own cancel event.
  - Worker threads never touch Tk widgets. They post messages to a queue that
    the UI thread drains in _poll(). Each message is handled inside try/except,
    so one failing handler cannot freeze the window.
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from . import __version__
from .carver import carve_summary
from .dates import make_range
from .disks import list_disks
from .fsmeta import TskUnavailable, recover_entries, scan_deleted
from .imaging import export_entries, image_source
from .logsetup import get_logger, setup_logging
from .platform_info import current_os, is_admin
from .repair import repair_file
from .safety import UnsafeDestinationError, check_destination, is_device_path
from .trash import filter_by_deletion, list_trash, restore
from .theme import COLORS, apply_theme
from .widgets import DateField, FlowFrame
from .progress import ProgressTracker
from .flashrepair import diagnose, repair_command, run_repair, surface_scan
from .carver import source_size

log = get_logger("gui")

PAD = 8
COMMON_EXTENSIONS = ["jpg", "png", "gif", "bmp", "pdf", "zip", "rar", "7z", "gz",
                     "sqlite", "mp4", "mov", "avi", "wav", "mp3", "exe"]
TEXT_PREVIEW_EXT = {".txt", ".csv", ".log", ".xml", ".json", ".md", ".html", ".py", ".ini", ".cfg"}
IMAGE_PREVIEW_EXT = {".png", ".gif"}  # native in Tk; JPEG needs Pillow (optional)

JOB_NAMES = {
    "trash_restore": "Pulihkan dari trash",
    "fs_scan": "Cari file dihapus",
    "fs_recover": "Pulihkan file dihapus",
    "fs_preview": "Pratinjau",
    "carve": "Pemindaian carving",
    "image": "Salin disk",
    "repair": "Perbaikan",
    "disks": "Muat daftar disk",
}


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def friendly_error(text: str) -> str:
    """Turn common OS errors into advice the user can act on."""
    low = text.lower()
    if "access denied" in low or "permission denied" in low or "akses ditolak" in low:
        return (text + "\n\nSaran: jalankan aplikasi sebagai Administrator (Windows) "
                "atau dengan sudo (Linux/macOS), lalu coba lagi.")
    if "no such file" in low or "cannot find" in low or "not found" in low:
        return text + "\n\nSaran: periksa lagi path sumber. Perangkat mungkin sudah dicabut."
    return text


def source_total(path: str) -> int:
    """Size in bytes of a file or raw device, 0 when unknown (progress then shows no percent)."""
    try:
        with open(path, "rb", buffering=0) as fh:
            return source_size(fh)
    except OSError:
        return 0


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        apply_theme(self)
        self.title(f"File Recovery {__version__}")
        self.geometry("960x640")
        self.minsize(800, 540)
        self.events: queue.Queue = queue.Queue()
        self.job: Optional[str] = None
        self.cancel_events: dict[str, threading.Event] = {}
        self.action_buttons: list[ttk.Button] = []
        self.cancel_buttons: dict[str, ttk.Button] = {}
        self.disk_maps: dict[str, dict[str, str]] = {}
        self.trash_items: list = []
        self.fs_entries: list = []
        self.fs_source = ""
        self.log_path = setup_logging()
        self.trackers: dict[str, ProgressTracker] = {}

        self._build_status_bar()
        notebook = ttk.Notebook(self)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.trash_tab = ttk.Frame(notebook, padding=PAD)
        self.fs_tab = ttk.Frame(notebook, padding=PAD)
        self.carve_tab = ttk.Frame(notebook, padding=PAD)
        self.image_tab = ttk.Frame(notebook, padding=PAD)
        self.repair_tab = ttk.Frame(notebook, padding=PAD)
        self.flash_tab = ttk.Frame(notebook, padding=PAD)
        notebook.add(self.trash_tab, text="  Recycle Bin  ")
        notebook.add(self.fs_tab, text="  Dihapus  ")
        notebook.add(self.carve_tab, text="  Carving  ")
        notebook.add(self.image_tab, text="  Salin disk  ")
        notebook.add(self.flash_tab, text="  Flashdisk  ")
        notebook.add(self.repair_tab, text="  Perbaiki  ")

        self._build_trash_tab()
        self._build_fs_tab()
        self._build_carve_tab()
        self._build_image_tab()
        self._build_flash_tab()
        self._build_repair_tab()
        self._update_controls()

        self.after(100, self._poll)
        self.after(200, self.refresh_trash)
        self.after(300, self.load_disks)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- common layout ----------

    def _build_status_bar(self) -> None:
        admin = "ya" if is_admin() else "tidak"
        text = f"Sistem: {current_os()}   |   Hak admin/root: {admin}"
        if not is_admin():
            text += "   (scan disk membutuhkan Administrator/root)"
        if self.log_path:
            text += "   |   Log: ~/FileRecovery/logs"
        self.status = ttk.Label(self, text=text, anchor="w", style="Status.TLabel")
        self.status.pack(fill="x", side="bottom")

    def _make_log(self, parent, row: int) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=4, sticky="nsew")
        text = tk.Text(frame, height=10, wrap="word")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set, state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text

    def _log(self, widget: tk.Text, msg: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", msg + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def _action_button(self, parent, text: str, command: Callable, primary: bool = False) -> ttk.Button:
        btn = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if primary else "TButton")
        self.action_buttons.append(btn)
        return btn

    def _cancel_button(self, parent, job: str) -> ttk.Button:
        btn = ttk.Button(parent, text="Batal", command=lambda: self.cancel_job(job), state="disabled")
        self.cancel_buttons[job] = btn
        return btn

    def _pick_file_into(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(title="Pilih file")
        if path:
            var.set(path)

    # ---------- job system ----------

    def _begin(self, name: str) -> Optional[threading.Event]:
        """Start a job. Returns its cancel event, or None if another job is running."""
        if self.job is not None:
            messagebox.showwarning(
                "Sedang berjalan",
                f"{JOB_NAMES.get(self.job, self.job)} masih berjalan. Tunggu atau batalkan dulu.")
            return None
        self.job = name
        event = threading.Event()
        self.cancel_events[name] = event
        log.info("job started: %s", name)
        self._update_controls()
        return event

    def _end(self, name: str) -> None:
        if name in ("fs_scan", "fs_recover", "fs_preview") and hasattr(self, "fs_bar"):
            self.fs_bar.stop()
        if self.job == name:
            self.job = None
        self.cancel_events.pop(name, None)
        log.info("job finished: %s", name)
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self.job is not None
        for btn in self.action_buttons:
            btn.configure(state="disabled" if busy else "normal")
        for name, btn in self.cancel_buttons.items():
            btn.configure(state="normal" if self.job == name else "disabled")

    def cancel_job(self, name: str) -> None:
        event = self.cancel_events.get(name)
        if event is not None:
            event.set()
            self.status.configure(text=f"Membatalkan {JOB_NAMES.get(name, name)} ...")
            log.info("job cancel requested: %s", name)

    def _spawn(self, name: str, cancel: threading.Event, fn: Callable) -> None:
        """Run fn(progress, cancel) in a worker thread and report the result through the queue."""
        def progress(done: int, total: int) -> None:
            self.events.put(("progress", name, done, total))

        def worker() -> None:
            try:
                payload = fn(progress, cancel)
                self.events.put(("done", name, payload))
            except Exception as exc:  # noqa: BLE001 - reported to the user below
                log.exception("job failed: %s", name)
                self.events.put(("error", name, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        """Drain the queue on the UI thread. Each message is isolated so one failure cannot stop polling."""
        try:
            while True:
                try:
                    msg = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except Exception as exc:  # noqa: BLE001
                    log.exception("UI handler failed for %r", msg[0] if msg else msg)
                    self.status.configure(text=f"Error internal: {exc}")
        finally:
            if self.winfo_exists():
                self.after(100, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            _, name, done, total = msg
            self._on_progress(name, done, total)
        elif kind == "done":
            _, name, payload = msg
            self._end(name)
            self._on_done(name, payload)
        elif kind == "error":
            _, name, text = msg
            self._end(name)
            self._on_error(name, text)
        elif kind == "items":
            _, name, done, total, text = msg
            tracker = self.trackers.get(name)
            if tracker is not None:
                tracker.total = total
                tracker.update(done)
                self.fs_bar.stop()
                self.fs_bar.configure(mode="determinate", value=tracker.percent or 0)
                self.fs_status.configure(text=f"{text}  |  {tracker.status_text()}")
        elif kind == "status":
            _, name, text = msg
            if name in ("fs_scan", "fs_recover", "fs_preview"):
                self.fs_status.configure(text=text)
        elif kind == "phase":
            _, name, text, done, total = msg
            if name == "carve":
                pct = done * 100 / total if total else 0
                self.carve_progress.configure(value=pct)
                self.carve_status.configure(text=f"{text}: {done} dari {total} ({pct:.0f}%)")

    def _on_progress(self, name: str, done: int, total: int) -> None:
        tracker = self.trackers.get(name)
        if name == "carve":
            if tracker is not None:
                if tracker.percent is not None:
                    self.carve_progress.configure(value=tracker.percent)
                self.carve_status.configure(text=tracker.status_text())
            else:
                self.carve_status.configure(text=f"{_human_size(done)} dipindai")
        elif name == "image":
            if tracker is not None:
                if tracker.percent is not None:
                    self.img_progress.configure(value=tracker.percent)
                self.img_status.configure(text=tracker.status_text())
        elif name == "repair":
            pct = done * 100 / total if total else 0
            self.repair_progress.configure(value=pct)
            self.repair_status.configure(text=f"{done} dari {total} file ({pct:.0f}%)")
        elif name == "flash_surface":
            if tracker is not None:
                if tracker.percent is not None:
                    self.flash_progress.configure(value=tracker.percent)
                self.flash_status.configure(text="Membaca semua sektor: " + tracker.status_text())

    def _on_error(self, name: str, text: str) -> None:
        text = friendly_error(text)
        if name.startswith("flash"):
            self.flash_status.configure(text="Error")
            self._log(self.flash_log, f"ERROR: {text}")
            log.error("job %s error: %s", name, text)
            return
        if name == "carve":
            self.carve_status.configure(text="Error")
            self._log(self.carve_log, f"ERROR: {text}")
        elif name == "image":
            self.img_status.configure(text="Error")
            self._log(self.img_log, f"ERROR: {text}")
        elif name == "repair":
            self.repair_status.configure(text="Error")
            self._log(self.repair_log, f"ERROR: {text}")
        elif name in ("fs_scan", "fs_recover", "fs_preview"):
            self.fs_status.configure(text=f"ERROR: {text}")
        else:
            messagebox.showerror(JOB_NAMES.get(name, name), text)
        log.error("job %s error: %s", name, text)

    def _on_done(self, name: str, payload) -> None:
        if name == "flash_diag":
            self.flash_status.configure(text="Diagnosa selesai")
            self._log(self.flash_log, f"[Diagnosa] {payload.device}")
            self._log(self.flash_log, f"  Ukuran: {_human_size(payload.size_bytes)}   Sistem file: {payload.file_system}")
            if payload.partitions:
                self._log(self.flash_log, "  Partisi: " + ", ".join(payload.partitions))
            self._log(self.flash_log, f"  Status: {payload.status.upper()} - {payload.message}")
        elif name == "flash_surface":
            self.flash_progress.configure(value=100)
            if payload.cancelled:
                self.flash_status.configure(text="Dibatalkan")
                self._log(self.flash_log, "Uji permukaan dibatalkan.")
            elif payload.healthy:
                self.flash_status.configure(text="Sehat")
                self._log(self.flash_log, f"[Uji permukaan] {_human_size(payload.bytes_read)} dibaca, "
                                          "tidak ada sektor rusak.")
            else:
                self.flash_status.configure(text=f"{len(payload.bad_ranges)} sektor rusak")
                self._log(self.flash_log, f"[Uji permukaan] {len(payload.bad_ranges)} rentang tidak terbaca "
                                          f"({_human_size(payload.bad_bytes)}). Salin ke image sebelum perbaikan.")
        elif name == "flash_repair":
            code, output = payload
            self.flash_progress.configure(value=100 if code == 0 else 0)
            self.flash_status.configure(text="Selesai" if code == 0 else f"Keluar dengan kode {code}")
            self._log(self.flash_log, output.strip() or "(tidak ada keluaran)")
            self._log(self.flash_log, f"Kode keluar: {code}")
        elif name == "disks":
            self._apply_disks(payload)
        elif name == "trash_restore":
            self.refresh_trash()
            messagebox.showinfo("Selesai", f"{payload['ok']} file dipulihkan ke:\n{payload['dest']}")
        elif name == "fs_scan":
            self._show_fs_entries(payload["source"], payload["entries"])
        elif name == "fs_recover":
            self.fs_status.configure(text=f"{payload['ok']}/{payload['total']} file dipulihkan")
            messagebox.showinfo("Selesai", f"{payload['ok']} dari {payload['total']} file dipulihkan ke:\n"
                                           f"{payload['dest']}\n\nDetail di log: {self.log_path or '-'}")
        elif name == "fs_preview":
            self.fs_status.configure(text="Pratinjau siap")
            self._open_preview(payload)
        elif name == "carve":
            summary = payload
            text = (f"{'Dibatalkan' if summary.cancelled else 'Selesai'}: "
                    f"{len(summary.recovered)} file ({sum(1 for r in summary.recovered if r.complete)} utuh)")
            if summary.skipped_by_date:
                text += f", {summary.skipped_by_date} di luar rentang tanggal"
            if summary.skipped_inside_other:
                text += f", {summary.skipped_inside_other} salinan di dalam file lain dilewati"
            self.carve_status.configure(text="Dibatalkan" if summary.cancelled else "Selesai")
            self._log(self.carve_log, f"{text}. Lokasi: {payload.out_dir}")
        elif name == "image":
            res = payload
            state = "Dibatalkan" if res.cancelled else "Selesai"
            self.img_status.configure(text=state)
            self._log(self.img_log, f"{state}: {_human_size(res.bytes_copied)} dari {_human_size(res.total_bytes)} "
                                    f"disalin ke {res.image_path}")
            self._log(self.img_log, f"SHA-256: {res.sha256}")
            if res.bad_ranges:
                self._log(self.img_log, f"Peringatan: {len(res.bad_ranges)} blok tidak terbaca, diisi nol.")
        elif name == "repair":
            ok = sum(1 for r in payload if r.ok)
            for r in payload:
                status = "OK   " if r.ok else "GAGAL"
                detail = "; ".join(r.actions)
                self._log(self.repair_log, f"{status} {os.path.basename(r.source)}  {detail}")
            self.repair_status.configure(text="Selesai")
            self._log(self.repair_log, f"Selesai: {ok}/{len(payload)} file berhasil diperbaiki.")

    # ---------- Trash ----------

    def _build_trash_tab(self) -> None:
        frm = self.trash_tab
        bar = FlowFrame(frm)
        bar.pack(fill="x")
        self._action_button(bar, "Muat ulang", self.refresh_trash).pack(side="left")
        self._action_button(bar, "Pulihkan yang dipilih...", self.restore_selected).pack(side="left", padx=PAD)
        self.trash_info = ttk.Label(bar, text="", style="Muted.TLabel")
        self.trash_info.pack(side="left", padx=PAD)

        dates = FlowFrame(frm)
        dates.pack(fill="x", pady=(PAD, 0))
        ttk.Label(dates, text="Dihapus dari").pack(side="left")
        self.trash_from_var = tk.StringVar()
        DateField(dates, self.trash_from_var).pack(side="left", padx=(4, 12))
        ttk.Label(dates, text="sampai").pack(side="left")
        self.trash_to_var = tk.StringVar()
        DateField(dates, self.trash_to_var).pack(side="left", padx=4)
        self._action_button(dates, "Terapkan filter", self.refresh_trash).pack(side="left", padx=PAD)
        ttk.Label(dates, text="(kosongkan = tampilkan semua)").pack(side="left")

        cols = ("name", "size", "deleted", "original")
        self.trash_tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="extended")
        for col, title, width in [("name", "Nama", 200), ("size", "Ukuran", 100),
                                  ("deleted", "Dihapus", 150), ("original", "Lokasi asal", 380)]:
            self.trash_tree.heading(col, text=title)
            self.trash_tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.trash_tree.yview)
        self.trash_tree.configure(yscrollcommand=scroll.set)
        self.trash_tree.pack(side="left", fill="both", expand=True, pady=PAD)
        scroll.pack(side="right", fill="y", pady=PAD)

    def refresh_trash(self) -> None:
        self.trash_tree.delete(*self.trash_tree.get_children())
        try:
            date_range = make_range(self.trash_from_var.get(), self.trash_to_var.get())
            all_items = list_trash()
        except ValueError as exc:
            messagebox.showerror("Tanggal", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - show OS errors instead of crashing
            log.exception("trash listing failed")
            self.trash_items = []
            self.trash_info.configure(text=f"Gagal membaca trash: {exc}")
            return
        self.trash_items, skipped = filter_by_deletion(all_items, date_range)
        for i, item in enumerate(self.trash_items):
            when = item.deleted_at.strftime("%Y-%m-%d %H:%M") if item.deleted_at else "-"
            self.trash_tree.insert("", "end", iid=str(i), values=(
                item.name, _human_size(item.size), when, item.original_path or "-"))
        info = f"{len(self.trash_items)} dari {len(all_items)} item"
        if skipped:
            info += f" ({skipped} tanpa tanggal hapus dilewati)"
        self.trash_info.configure(text=info)

    def restore_selected(self) -> None:
        selected = [int(i) for i in self.trash_tree.selection()]
        if not selected:
            messagebox.showinfo("Pulihkan", "Pilih satu atau lebih file dulu.")
            return
        dest = filedialog.askdirectory(title="Pilih folder tujuan")
        if not dest:
            return
        items = [self.trash_items[i] for i in selected]
        cancel = self._begin("trash_restore")
        if cancel is None:
            return

        def run(progress, cancel_event):
            ok = 0
            errors = []
            for item in items:
                try:
                    restore(item, dest)
                    ok += 1
                except OSError as exc:
                    errors.append(f"{item.name}: {exc}")
            if errors:
                log.warning("trash restore errors: %s", errors)
            return {"ok": ok, "dest": dest}

        self._spawn("trash_restore", cancel, run)

    # ---------- File system metadata (names and paths) ----------

    def _build_fs_tab(self) -> None:
        frm = self.fs_tab
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Sumber (disk / image):").grid(row=0, column=0, sticky="w")
        self.fs_source_var = tk.StringVar()
        self.fs_combo = ttk.Combobox(frm, textvariable=self.fs_source_var)
        self.fs_combo.grid(row=0, column=1, sticky="ew", padx=PAD)
        b = ttk.Frame(frm)
        b.grid(row=0, column=2)
        self._action_button(b, "File...", lambda: self._pick_file_into(self.fs_source_var)).pack(side="left")

        dates = FlowFrame(frm)
        dates.grid(row=1, column=0, columnspan=3, sticky="w", pady=PAD)
        ttk.Label(dates, text="Dihapus dari").pack(side="left")
        self.fs_from_var = tk.StringVar()
        DateField(dates, self.fs_from_var).pack(side="left", padx=(4, 12))
        ttk.Label(dates, text="sampai").pack(side="left")
        self.fs_to_var = tk.StringVar()
        DateField(dates, self.fs_to_var).pack(side="left", padx=4)
        ttk.Label(dates, text="Cari nama/path:").pack(side="left", padx=(PAD, 0))
        self.fs_match_var = tk.StringVar()
        ttk.Entry(dates, textvariable=self.fs_match_var, width=18).pack(side="left", padx=4)
        ttk.Button(dates, text="Tampilkan", command=self._refilter_fs).pack(side="left")

        bar = FlowFrame(frm)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._action_button(bar, "Cari file dihapus", self.start_fs_scan, primary=True).pack(side="left")
        self.cancel_buttons["fs_scan"] = self._cancel_button(bar, "fs_scan")
        self.cancel_buttons["fs_scan"].pack(side="left", padx=2)
        self._action_button(bar, "Pilih semua",
                            lambda: self.fs_tree.selection_set(self.fs_tree.get_children())).pack(side="left", padx=PAD)
        self._action_button(bar, "Pratinjau", self.start_preview).pack(side="left")
        self._action_button(bar, "Pulihkan yang dipilih...", self.recover_fs_selected).pack(side="left", padx=PAD)
        self.cancel_buttons["fs_recover"] = self._cancel_button(bar, "fs_recover")
        self.cancel_buttons["fs_recover"].pack(side="left")
        self._action_button(bar, "Ekspor daftar...", self.export_fs_list).pack(side="left", padx=PAD)
        self.fs_bar = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.fs_bar.pack(side="left", padx=PAD)
        self.fs_status = ttk.Label(bar, text="")
        self.fs_status.pack(side="left", padx=PAD)

        cols = ("path", "size", "fs", "deleted")
        self.fs_tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="extended")
        for col, title, width in [("path", "Path file", 420), ("size", "Ukuran", 100),
                                  ("fs", "Sistem file", 90), ("deleted", "Dihapus (perkiraan)", 160)]:
            self.fs_tree.heading(col, text=title)
            self.fs_tree.column(col, width=width, anchor="w")
        self.fs_tree.bind("<Double-1>", lambda _e: self.start_preview())
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.fs_tree.yview)
        self.fs_tree.configure(yscrollcommand=scroll.set)
        self.fs_tree.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=PAD)
        scroll.grid(row=3, column=2, sticky="ns", pady=PAD)
        frm.rowconfigure(3, weight=1)
        ttk.Label(frm, text="Butuh pytsk3 (pip install pytsk3). Nama, folder, dan waktu asli dibaca dari "
                            "tabel file sistem. Waktu hapus adalah perkiraan. Di ext4, file yang sudah dihapus "
                            "tidak punya nama lagi; gunakan carving.", foreground="gray", wraplength=860
                  ).grid(row=4, column=0, columnspan=3, sticky="w")

    def start_fs_scan(self) -> None:
        src = self.fs_source_var.get().strip()
        if not src:
            messagebox.showerror("Sumber", "Pilih disk atau file image.")
            return
        if not is_device_path(src) and not os.path.exists(src):
            messagebox.showerror("Sumber", f"Sumber tidak ditemukan: {src}")
            return
        try:
            date_range = make_range(self.fs_from_var.get(), self.fs_to_var.get())
        except ValueError as exc:
            messagebox.showerror("Tanggal", str(exc))
            return
        cancel = self._begin("fs_scan")
        if cancel is None:
            return
        self.fs_status.configure(text="Menghitung folder ...")
        self.fs_bar.configure(mode="indeterminate")
        self.fs_bar.start(12)
        self.trackers["fs_scan"] = ProgressTracker(0, unit="items")

        def run(progress, cancel_event):
            def item_cb(done, total, text):
                self.events.put(("items", "fs_scan", done, total, text))

            entries = scan_deleted(src, date_range=date_range, cancel=cancel_event,
                                   progress=lambda msg: self.events.put(("status", "fs_scan", msg)),
                                   item_progress=item_cb)
            return {"source": src, "entries": entries}

        self._spawn("fs_scan", cancel, run)

    def _show_fs_entries(self, src: str, entries) -> None:
        self.fs_entries = entries
        self.fs_source = src
        self._refilter_fs()

    def _refilter_fs(self) -> None:
        self.fs_tree.delete(*self.fs_tree.get_children())
        needle = self.fs_match_var.get().strip().lower()
        try:
            date_range = make_range(self.fs_from_var.get(), self.fs_to_var.get())
        except ValueError as exc:
            messagebox.showerror("Tanggal", str(exc))
            return
        shown = 0
        for i, e in enumerate(self.fs_entries):
            if needle and needle not in e.path.lower():
                continue
            if date_range.active and not date_range.contains(e.deleted_at):
                continue
            when = e.deleted_at.strftime("%Y-%m-%d %H:%M") if e.deleted_at else "-"
            self.fs_tree.insert("", "end", iid=str(i), values=(e.path, _human_size(e.size), e.fs_type, when))
            shown += 1
        self.fs_status.configure(text=f"{shown} dari {len(self.fs_entries)} file dihapus")

    def _selected_fs_entries(self):
        ids = [int(i) for i in self.fs_tree.selection()]
        return [self.fs_entries[i] for i in ids]

    def recover_fs_selected(self) -> None:
        chosen = self._selected_fs_entries()
        if not chosen:
            messagebox.showinfo("Pulihkan", "Pilih satu atau lebih file dulu (Ctrl/Shift untuk banyak).")
            return
        dest = filedialog.askdirectory(title="Pilih folder tujuan (bukan di disk yang dipindai)")
        if not dest:
            return
        try:
            check_destination(self.fs_source, dest)
        except UnsafeDestinationError as exc:
            messagebox.showerror("Tujuan tidak aman", str(exc))
            return
        cancel = self._begin("fs_recover")
        if cancel is None:
            return
        src = self.fs_source

        def run(progress, cancel_event):
            results = recover_entries(src, chosen, dest, cancel=cancel_event)
            ok = sum(1 for _d, _n, err in results if err is None)
            for dst, _n, err in results:
                if err:
                    log.warning("recover %s: %s", dst, err)
            return {"ok": ok, "total": len(results), "dest": dest}

        self._spawn("fs_recover", cancel, run)

    def start_preview(self) -> None:
        chosen = self._selected_fs_entries()
        if len(chosen) != 1:
            messagebox.showinfo("Pratinjau", "Pilih tepat satu file untuk pratinjau.")
            return
        entry = chosen[0]
        cancel = self._begin("fs_preview")
        if cancel is None:
            return
        src = self.fs_source
        cache = tempfile.mkdtemp(prefix="filerecovery_preview_")

        def run(progress, cancel_event):
            results = recover_entries(src, [entry], cache, cancel=cancel_event)
            dest, _written, err = results[0]
            if err and not os.path.exists(dest):
                raise RuntimeError(err)
            return {"path": dest, "name": entry.name, "size": entry.size, "partial": bool(err)}

        self._spawn("fs_preview", cancel, run)

    def _open_preview(self, info: dict) -> None:
        win = tk.Toplevel(self)
        win.title(f"Pratinjau: {info['name']}")
        win.geometry("720x520")
        path = info["path"]
        ext = os.path.splitext(path)[1].lower()
        note = "  (sebagian, data tidak lengkap)" if info["partial"] else ""
        ttk.Label(win, text=f"{info['name']}  -  {_human_size(info['size'])}{note}").pack(anchor="w", padx=PAD, pady=PAD)
        if ext in IMAGE_PREVIEW_EXT:
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError as exc:
                ttk.Label(win, text=f"Gambar tidak bisa ditampilkan: {exc}").pack(padx=PAD)
                return
            canvas = tk.Canvas(win, width=min(img.width(), 700), height=min(img.height(), 460))
            canvas.pack(fill="both", expand=True, padx=PAD)
            canvas.create_image(0, 0, anchor="nw", image=img)
            canvas.image = img  # keep a reference or Tk drops the image
        elif ext in TEXT_PREVIEW_EXT or ext == "":
            text = tk.Text(win, wrap="word")
            text.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
            try:
                with open(path, "rb") as fh:
                    raw = fh.read(64 * 1024)
                text.insert("1.0", raw.decode("utf-8", errors="replace"))
            except OSError as exc:
                text.insert("1.0", f"Tidak bisa membaca: {exc}")
            text.configure(state="disabled")
        else:
            try:
                with open(path, "rb") as fh:
                    head = fh.read(512)
            except OSError as exc:
                head = str(exc).encode()
            lines = []
            for off in range(0, len(head), 16):
                chunk = head[off:off + 16]
                hexpart = " ".join(f"{b:02x}" for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                lines.append(f"{off:08x}  {hexpart:<47}  {asc}")
            text = tk.Text(win, wrap="none", font=("Courier", 10))
            text.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
            text.insert("1.0", "Pratinjau heksadesimal (512 byte pertama). Simpan file untuk membukanya.\n\n"
                        + "\n".join(lines))
            text.configure(state="disabled")

    def export_fs_list(self) -> None:
        if not self.fs_entries:
            messagebox.showinfo("Ekspor", "Jalankan pencarian dulu.")
            return
        path = filedialog.asksaveasfilename(title="Simpan daftar", defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        try:
            export_entries(self.fs_entries, path)
        except OSError as exc:
            log.exception("export failed")
            messagebox.showerror("Ekspor gagal", str(exc))
            return
        messagebox.showinfo("Ekspor", f"Daftar disimpan ke:\n{path}")

    # ---------- Carving ----------

    def _build_carve_tab(self) -> None:
        frm = self.carve_tab
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Sumber (disk / file image):").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.disk_combo = ttk.Combobox(frm, textvariable=self.source_var)
        self.disk_combo.grid(row=0, column=1, sticky="ew", padx=PAD)
        btns = ttk.Frame(frm)
        btns.grid(row=0, column=2)
        self._action_button(btns, "Muat disk", self.load_disks).pack(side="left")
        self._action_button(btns, "File...", lambda: self._pick_file_into(self.source_var)).pack(side="left", padx=2)

        ttk.Label(frm, text="Simpan hasil ke folder:").grid(row=1, column=0, sticky="w", pady=PAD)
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "recovered"))
        ttk.Entry(frm, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=PAD)
        self._action_button(frm, "Pilih...", self.pick_out_dir).grid(row=1, column=2)

        dates = ttk.LabelFrame(frm, text="Rentang tanggal di dalam file (opsional)", padding=PAD)
        dates.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(dates, text="dari").pack(side="left")
        self.carve_from_var = tk.StringVar()
        DateField(dates, self.carve_from_var).pack(side="left", padx=(4, 12))
        ttk.Label(dates, text="sampai").pack(side="left")
        self.carve_to_var = tk.StringVar()
        DateField(dates, self.carve_to_var).pack(side="left", padx=4)
        ttk.Label(dates, text="Kosong = semua file. Tanggal diambil dari isi file (EXIF foto, "
                              "tanggal PDF, tanggal ZIP), bukan tanggal hapus.",
                  foreground="gray").pack(side="left", padx=PAD)

        ext_box = ttk.LabelFrame(frm, text="Jenis file yang dicari", padding=PAD)
        ext_box.grid(row=3, column=0, columnspan=3, sticky="ew", pady=PAD)
        self.ext_vars: dict[str, tk.BooleanVar] = {}
        for i, ext in enumerate(COMMON_EXTENSIONS):
            var = tk.BooleanVar(value=True)
            self.ext_vars[ext] = var
            ttk.Checkbutton(ext_box, text=ext.upper(), variable=var).grid(row=i // 8, column=i % 8, sticky="w", padx=4)
        sel = ttk.Frame(ext_box)
        sel.grid(row=2, column=0, columnspan=8, sticky="w", pady=(4, 0))
        self._action_button(sel, "Pilih semua", lambda: self._set_all_ext(True)).pack(side="left")
        self._action_button(sel, "Kosongkan", lambda: self._set_all_ext(False)).pack(side="left", padx=PAD)

        action = FlowFrame(frm)
        action.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.carve_btn = self._action_button(action, "Mulai pemindaian", self.start_carve, primary=True)
        self.carve_btn.pack(side="left")
        self.cancel_buttons["carve"] = self._cancel_button(action, "carve")
        self.cancel_buttons["carve"].pack(side="left", padx=PAD)
        self.carve_progress = ttk.Progressbar(action, mode="determinate", length=300)
        self.carve_progress.pack(side="left", padx=PAD, fill="x", expand=True)
        self.carve_status = ttk.Label(action, text="Siap")
        self.carve_status.pack(side="left")
        self.carve_log = self._make_log(frm, row=5)
        frm.rowconfigure(5, weight=1)

    def _set_all_ext(self, value: bool) -> None:
        for var in self.ext_vars.values():
            var.set(value)

    def pick_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Pilih folder hasil")
        if path:
            self.out_var.set(path)

    def load_disks(self) -> None:
        """List disks in the background (PowerShell/lsblk can take a moment)."""
        # Not a "job": listing must never block a scan, so it does not take the job slot.
        self._spawn("disks", threading.Event(), lambda progress, c: list_disks())

    def _apply_disks(self, choices) -> None:
        mapping = {desc: path for path, desc in choices}
        self.disk_maps = {"carve": mapping, "fs": mapping, "image": mapping}
        for combo, key in ((self.disk_combo, "carve"), (self.fs_combo, "fs"), (self.img_combo, "image")):
            combo.configure(values=list(mapping.keys()))
            combo.bind("<<ComboboxSelected>>", lambda _e, c=combo, k=key: self._apply_choice(c, k))
        if not choices:
            self.carve_status.configure(text="Daftar disk kosong")

    def _apply_choice(self, combo: ttk.Combobox, key: str) -> None:
        mapping = self.disk_maps.get(key, {})
        if combo.get() in mapping:
            combo.set(mapping[combo.get()])

    def start_carve(self) -> None:
        source = self.source_var.get().strip()
        out = self.out_var.get().strip()
        if not source or (not is_device_path(source) and not os.path.exists(source)):
            messagebox.showerror("Sumber", "Sumber tidak ditemukan. Pilih disk dari daftar atau pilih file image.")
            return
        if not out:
            messagebox.showerror("Folder", "Pilih folder hasil.")
            return
        try:
            check_destination(source, out)
        except UnsafeDestinationError as exc:
            messagebox.showerror("Tujuan tidak aman", str(exc))
            return
        extensions = [ext for ext, var in self.ext_vars.items() if var.get()]
        if not extensions:
            messagebox.showerror("Jenis file", "Pilih minimal satu jenis file.")
            return
        try:
            date_range = make_range(self.carve_from_var.get(), self.carve_to_var.get())
        except ValueError as exc:
            messagebox.showerror("Tanggal", str(exc))
            return
        cancel = self._begin("carve")
        if cancel is None:
            return
        self.carve_progress.configure(value=0)
        rng = f" (tanggal {date_range})" if date_range.active else ""
        self._log(self.carve_log, f"Mulai memindai {source}{rng} ...")
        log.info("carve source=%s out=%s exts=%s range=%s", source, out, extensions, date_range)

        tracker = ProgressTracker(source_total(source))
        self.trackers["carve"] = tracker

        def run(progress, cancel_event):
            def scan_progress(done, total):
                tracker.update(done)
                progress(done, total)

            def phase(text, done, total):
                self.events.put(("phase", "carve", text, done, total))

            summary = carve_summary(source, out, extensions=extensions, progress=scan_progress,
                                    date_range=date_range, cancel=cancel_event, phase=phase)
            summary.out_dir = out
            return summary

        self._spawn("carve", cancel, run)

    # ---------- Image ----------

    def _build_image_tab(self) -> None:
        frm = self.image_tab
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Disk / partisi yang disalin:").grid(row=0, column=0, sticky="w")
        self.img_src_var = tk.StringVar()
        self.img_combo = ttk.Combobox(frm, textvariable=self.img_src_var)
        self.img_combo.grid(row=0, column=1, sticky="ew", padx=PAD)
        self._action_button(frm, "File...", lambda: self._pick_file_into(self.img_src_var)).grid(row=0, column=2)
        ttk.Label(frm, text="File image tujuan:").grid(row=1, column=0, sticky="w", pady=PAD)
        self.img_out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "disk_backup.img"))
        ttk.Entry(frm, textvariable=self.img_out_var).grid(row=1, column=1, sticky="ew", padx=PAD)
        self._action_button(frm, "Simpan ke...", self._pick_image_out).grid(row=1, column=2)
        ttk.Label(frm, text="Salinan disk dibuat sebelum pemulihan agar disk asli tidak tertimpa. "
                            "Sektor rusak diisi nol dan dicatat. Hash SHA-256 disimpan untuk verifikasi.",
                  foreground="gray", wraplength=780).grid(row=2, column=0, columnspan=3, sticky="w")
        bar = FlowFrame(frm)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=PAD)
        self.img_btn = self._action_button(bar, "Mulai salin", self.start_image, primary=True)
        self.img_btn.pack(side="left")
        self.cancel_buttons["image"] = self._cancel_button(bar, "image")
        self.cancel_buttons["image"].pack(side="left", padx=PAD)
        self.img_progress = ttk.Progressbar(bar, mode="determinate", length=300)
        self.img_progress.pack(side="left", padx=PAD, fill="x", expand=True)
        self.img_status = ttk.Label(bar, text="Siap")
        self.img_status.pack(side="left")
        self.img_log = self._make_log(frm, row=4)
        frm.rowconfigure(4, weight=1)

    def _pick_image_out(self) -> None:
        path = filedialog.asksaveasfilename(title="Simpan image sebagai", defaultextension=".img")
        if path:
            self.img_out_var.set(path)

    def start_image(self) -> None:
        src = self.img_src_var.get().strip()
        out = self.img_out_var.get().strip()
        if not src or not out:
            messagebox.showerror("Image", "Isi sumber dan file tujuan.")
            return
        try:
            check_destination(src, os.path.dirname(os.path.abspath(out)))
        except UnsafeDestinationError as exc:
            messagebox.showerror("Tujuan tidak aman", str(exc))
            return
        cancel = self._begin("image")
        if cancel is None:
            return
        self.img_progress.configure(value=0)
        self._log(self.img_log, f"Menyalin {src} ke {out} ...")
        log.info("image source=%s out=%s", src, out)
        tracker = ProgressTracker(source_total(src))
        self.trackers["image"] = tracker

        def run(progress, c):
            def track(done, total):
                tracker.update(done)
                progress(done, total)
            return image_source(src, out, progress=track, cancel=c)

        self._spawn("image", cancel, run)

    # ---------- Repair ----------

    def _build_repair_tab(self) -> None:
        frm = self.repair_tab
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="File atau folder yang rusak:").grid(row=0, column=0, sticky="w")
        self.repair_src_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.repair_src_var).grid(row=0, column=1, sticky="ew", padx=PAD)
        b = ttk.Frame(frm)
        b.grid(row=0, column=2)
        self._action_button(b, "File...", self.pick_repair_file).pack(side="left")
        self._action_button(b, "Folder...", self.pick_repair_dir).pack(side="left", padx=2)

        ttk.Label(frm, text="Simpan hasil ke folder:").grid(row=1, column=0, sticky="w", pady=PAD)
        self.repair_out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "repaired"))
        ttk.Entry(frm, textvariable=self.repair_out_var).grid(row=1, column=1, sticky="ew", padx=PAD)
        self._action_button(frm, "Pilih...", self.pick_repair_out).grid(row=1, column=2)

        ttk.Label(frm, text="Didukung: PNG, JPG, PDF, ZIP/DOCX/XLSX/PPTX, SQLite. "
                            "File asli tidak akan diubah.").grid(row=2, column=0, columnspan=3, sticky="w")
        action = FlowFrame(frm)
        action.grid(row=3, column=0, columnspan=3, sticky="ew", pady=PAD)
        self.repair_btn = self._action_button(action, "Perbaiki", self.start_repair, primary=True)
        self.repair_btn.pack(side="left")
        self.repair_progress = ttk.Progressbar(action, mode="determinate", length=260)
        self.repair_progress.pack(side="left", padx=PAD)
        self.repair_status = ttk.Label(action, text="Siap")
        self.repair_status.pack(side="left", padx=PAD)
        self.repair_log = self._make_log(frm, row=4)
        frm.rowconfigure(4, weight=1)

    def pick_repair_file(self) -> None:
        path = filedialog.askopenfilename(title="Pilih file rusak")
        if path:
            self.repair_src_var.set(path)

    def pick_repair_dir(self) -> None:
        path = filedialog.askdirectory(title="Pilih folder berisi file rusak")
        if path:
            self.repair_src_var.set(path)

    def pick_repair_out(self) -> None:
        path = filedialog.askdirectory(title="Pilih folder hasil perbaikan")
        if path:
            self.repair_out_var.set(path)

    def start_repair(self) -> None:
        src = self.repair_src_var.get().strip()
        out = self.repair_out_var.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showerror("Sumber", "File atau folder tidak ditemukan.")
            return
        if not out:
            messagebox.showerror("Folder", "Pilih folder hasil.")
            return
        cancel = self._begin("repair")
        if cancel is None:
            return
        self.repair_status.configure(text="Memproses...")
        self.repair_progress.configure(value=0)

        def run(progress, cancel_event):
            if os.path.isdir(src):
                paths = [os.path.join(root, f) for root, _d, files in os.walk(src) for f in files]
            else:
                paths = [src]
            results = []
            for i, p in enumerate(paths, 1):
                if cancel_event.is_set():
                    break
                results.append(repair_file(p, out))
                progress(i, len(paths))
            return results

        self._spawn("repair", cancel, run)

    # ---------- Flashdisk / memory ----------

    def _build_flash_tab(self) -> None:
        frm = self.flash_tab
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(5, weight=1)
        ttk.Label(frm, text="Flashdisk, kartu memori, dan USB yang rusak, RAW, atau tidak terbaca",
                  style="Title.TLabel").grid(row=0, column=0, sticky="w")
        intro = ttk.Label(frm, style="Sub.TLabel", wraplength=600, text=(
            "Urutan aman: 1) Diagnosa  2) Uji permukaan  3) Salin ke image  4) Pulihkan file dari image  "
            "5) Baru perbaiki sistem file. Perbaikan menulis ke perangkat, jadi salin dulu. "
            "Format tidak disediakan karena menghapus semua data."))
        intro.grid(row=1, column=0, sticky="ew", pady=(2, PAD))
        # Keep the explanation inside the window when it is resized.
        frm.bind("<Configure>", lambda e: intro.configure(wraplength=max(200, e.width - 40)))

        pick = FlowFrame(frm)
        pick.grid(row=2, column=0, sticky="ew")
        ttk.Label(pick, text="Perangkat:").pack(side="left")
        self.flash_src_var = tk.StringVar()
        self.flash_combo = ttk.Combobox(pick, textvariable=self.flash_src_var, width=48)
        self.flash_combo.pack(side="left", padx=PAD)
        self._action_button(pick, "Muat ulang daftar", self.load_disks).pack(side="left")
        self._action_button(pick, "File image...",
                            lambda: self._pick_file_into(self.flash_src_var)).pack(side="left", padx=4)

        actions = FlowFrame(frm)
        actions.grid(row=3, column=0, sticky="ew", pady=PAD)
        self._action_button(actions, "1. Diagnosa", self.flash_diagnose, primary=True).pack(side="left")
        self._action_button(actions, "2. Uji permukaan", self.flash_surface).pack(side="left", padx=4)
        self._action_button(actions, "3. Salin ke image", self.flash_image).pack(side="left", padx=4)
        self._action_button(actions, "4. Pulihkan dari image", self.flash_recover_image).pack(side="left", padx=4)
        self._action_button(actions, "Cek sistem file (aman)",
                            lambda: self.flash_repair("check")).pack(side="left", padx=4)
        self._action_button(actions, "5. Perbaiki sistem file",
                            lambda: self.flash_repair("fix")).pack(side="left", padx=4)
        self.cancel_buttons["flash_surface"] = self._cancel_button(actions, "flash_surface")
        self.cancel_buttons["flash_surface"].pack(side="left", padx=4)

        status = FlowFrame(frm)
        status.grid(row=4, column=0, sticky="ew")
        self.flash_progress = ttk.Progressbar(status, mode="determinate", length=420)
        self.flash_progress.pack(side="left")
        self.flash_status = ttk.Label(status, text="Siap")
        self.flash_status.pack(side="left", padx=PAD)
        self.flash_log = self._make_log(frm, row=5)

    def _flash_device(self) -> Optional[str]:
        dev = self.flash_src_var.get().strip()
        if not dev:
            messagebox.showerror("Perangkat", "Pilih perangkat atau file image dulu.")
            return None
        if not is_device_path(dev) and not os.path.exists(dev):
            messagebox.showerror("Perangkat", f"Tidak ditemukan: {dev}")
            return None
        return dev

    def flash_diagnose(self) -> None:
        dev = self._flash_device()
        if not dev:
            return
        cancel = self._begin("flash_diag")
        if cancel is None:
            return
        self.flash_status.configure(text="Mendiagnosa ...")
        self._spawn("flash_diag", cancel, lambda progress, c: diagnose(dev))

    def flash_surface(self) -> None:
        dev = self._flash_device()
        if not dev:
            return
        cancel = self._begin("flash_surface")
        if cancel is None:
            return
        tracker = ProgressTracker(source_total(dev))
        self.trackers["flash_surface"] = tracker
        self.flash_progress.configure(value=0)
        self.flash_status.configure(text="Membaca semua sektor ...")
        self._log(self.flash_log, f"[Uji permukaan] {dev} (hanya membaca, tidak menulis)")

        def run(progress, c):
            def track(done, total):
                tracker.update(done)
                progress(done, total)
            return surface_scan(dev, progress=track, cancel=c)

        self._spawn("flash_surface", cancel, run)

    def flash_image(self) -> None:
        dev = self._flash_device()
        if not dev:
            return
        self.img_src_var.set(dev)
        self.notebook.select(self.image_tab)
        self._pick_image_out()
        if self.img_out_var.get().strip():
            self.start_image()

    def flash_recover_image(self) -> None:
        path = filedialog.askopenfilename(title="Pilih file image perangkat")
        if not path:
            return
        self.source_var.set(path)
        self.notebook.select(self.carve_tab)
        self.start_carve()

    def flash_repair(self, mode: str) -> None:
        dev = self._flash_device()
        if not dev:
            return
        try:
            cmd = repair_command(dev, mode)
        except ValueError as exc:
            messagebox.showerror("Tidak diizinkan", str(exc))
            return
        if mode == "fix":
            ok = messagebox.askyesno(
                "Perbaiki sistem file",
                "Perbaikan MENULIS ke perangkat dan bisa mengubah data yang belum disalin.\n\n"
                "Sudah menyalin ke image? Jika belum, batalkan dan jalankan langkah 3 dulu.\n\n"
                f"Perintah: {' '.join(cmd)}\n\nLanjutkan?")
            if not ok:
                return
        cancel = self._begin("flash_repair")
        if cancel is None:
            return
        self.flash_status.configure(text="Menjalankan " + cmd[0] + " ...")
        self._log(self.flash_log, f"$ {' '.join(cmd)}")
        log.info("flash repair mode=%s cmd=%s", mode, cmd)
        self._spawn("flash_repair", cancel, lambda progress, c: run_repair(cmd))

    # ---------- close ----------

    def _on_close(self) -> None:
        if self.job is not None and not messagebox.askyesno(
                "Keluar", "Ada proses yang berjalan. Hentikan dan keluar?"):
            return
        for event in self.cancel_events.values():
            event.set()
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
