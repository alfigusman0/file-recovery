"""Reusable Tk widgets: a calendar date picker (no typing needed) and a theme."""

from __future__ import annotations

import calendar
import datetime as dt
import tkinter as tk
from tkinter import ttk
from typing import Optional

from .theme import COLORS

DATE_FORMAT = "%Y-%m-%d"
MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
             "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
DAYS_ID = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]


class FlowFrame(ttk.Frame):
    """A toolbar that wraps its children onto more rows when the window gets narrower.

    Children are placed with grid on every relayout, so widgets created with pack
    (the usual way in this app) are moved here automatically.
    """

    def __init__(self, parent, gap: int = 4, **kwargs):
        super().__init__(parent, **kwargs)
        self.gap = gap
        self._last_width = -1
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        if event.width == self._last_width:
            return
        self._last_width = event.width
        self.after_idle(self._relayout)

    def _relayout(self) -> None:
        width = self.winfo_width()
        children = [w for w in self.winfo_children() if w.winfo_ismapped() or w.winfo_manager() in ("pack", "grid", "")]
        for child in children:
            child.pack_forget()
            child.grid_forget()
        row = col = x = 0
        for child in children:
            need = child.winfo_reqwidth() + 2 * self.gap
            if x and width and x + need > width - 4:  # keep a small margin on the right
                row += 1
                col = 0
                x = 0
            child.grid(row=row, column=col, padx=self.gap, pady=self.gap, sticky="w")
            x += need
            col += 1


class DateField(ttk.Frame):
    """Read-only date display with a calendar button. The user picks a day; typing is not needed.

    `var` holds the date as YYYY-MM-DD, or "" when no date is set (no limit).
    """

    def __init__(self, parent, var: tk.StringVar, width: int = 12, allow_empty: bool = True):
        super().__init__(parent)
        self.var = var
        self.allow_empty = allow_empty
        self.entry = ttk.Entry(self, textvariable=var, width=width, state="readonly", justify="center")
        self.entry.pack(side="left")
        self.button = ttk.Button(self, text="📅", width=3, style="Icon.TButton", command=self.open_calendar)
        self.button.pack(side="left", padx=(4, 0))
        if allow_empty:
            self.clear = ttk.Button(self, text="✕", width=3, style="Icon.TButton", command=lambda: var.set(""))
            self.clear.pack(side="left", padx=(4, 0))

    def open_calendar(self) -> None:
        current = None
        try:
            current = dt.datetime.strptime(self.var.get(), DATE_FORMAT).date() if self.var.get() else None
        except ValueError:
            current = None
        CalendarPopup(self, current or dt.date.today(), on_pick=lambda d: self.var.set(d.strftime(DATE_FORMAT)))


class CalendarPopup(tk.Toplevel):
    """Month grid with previous/next buttons. Clicking a day selects it and closes the popup."""

    def __init__(self, anchor: tk.Misc, start: dt.date, on_pick):
        super().__init__(anchor)
        self.overrideredirect(False)
        self.title("Pilih tanggal")
        self.transient(anchor.winfo_toplevel())
        self.configure(bg=COLORS["card"])
        self.resizable(False, False)
        self.on_pick = on_pick
        self.year, self.month = start.year, start.month
        self.selected = start

        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 4
        self.geometry(f"+{x}+{y}")

        self.header = tk.Frame(self, bg=COLORS["card"])
        self.header.pack(fill="x", padx=12, pady=(10, 4))
        tk.Button(self.header, text="‹", width=3, relief="flat", bg=COLORS["card"], fg=COLORS["accent"],
                  font=("Segoe UI", 12, "bold"), command=lambda: self._shift(-1)).pack(side="left")
        self.title_label = tk.Label(self.header, bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left", expand=True)
        tk.Button(self.header, text="›", width=3, relief="flat", bg=COLORS["card"], fg=COLORS["accent"],
                  font=("Segoe UI", 12, "bold"), command=lambda: self._shift(1)).pack(side="right")

        self.grid_frame = tk.Frame(self, bg=COLORS["card"])
        self.grid_frame.pack(padx=12, pady=(0, 8))
        footer = tk.Frame(self, bg=COLORS["card"])
        footer.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(footer, text="Hari ini", relief="flat", bg=COLORS["card"], fg=COLORS["accent"],
                  command=lambda: self._pick(dt.date.today())).pack(side="left")
        tk.Button(footer, text="Tutup", relief="flat", bg=COLORS["card"], fg=COLORS["muted"],
                  command=self.destroy).pack(side="right")

        self._render()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_set()

    def _shift(self, delta: int) -> None:
        month = self.month + delta
        self.year += (month - 1) // 12
        self.month = (month - 1) % 12 + 1
        self._render()

    def _pick(self, day: dt.date) -> None:
        self.on_pick(day)
        self.destroy()

    def _render(self) -> None:
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.title_label.configure(text=f"{MONTHS_ID[self.month - 1]} {self.year}")
        for col, name in enumerate(DAYS_ID):
            tk.Label(self.grid_frame, text=name, bg=COLORS["card"], fg=COLORS["muted"],
                     width=4, font=("Segoe UI", 9)).grid(row=0, column=col)
        today = dt.date.today()
        for row, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(self.year, self.month), 1):
            for col, day in enumerate(week):
                if day == 0:
                    tk.Label(self.grid_frame, text="", bg=COLORS["card"], width=4).grid(row=row, column=col)
                    continue
                d = dt.date(self.year, self.month, day)
                is_sel = d == self.selected
                bg = COLORS["accent"] if is_sel else COLORS["card"]
                fg = "#ffffff" if is_sel else (COLORS["accent"] if d == today else COLORS["text"])
                tk.Button(self.grid_frame, text=str(day), width=4, relief="flat", bg=bg, fg=fg,
                          activebackground=COLORS["accent_soft"], font=("Segoe UI", 10),
                          command=lambda dd=d: self._pick(dd)).grid(row=row, column=col, padx=1, pady=1)
