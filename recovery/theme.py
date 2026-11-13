"""Visual theme for the GUI: light, calm, one accent colour, readable fonts."""

from __future__ import annotations

import sys
from tkinter import ttk

COLORS = {
    "bg": "#f4f6fa",
    "card": "#ffffff",
    "text": "#1f2937",
    "muted": "#6b7280",
    "accent": "#2563eb",
    "accent_soft": "#dbeafe",
    "danger": "#dc2626",
    "success": "#16a34a",
    "border": "#e5e7eb",
}


def apply_theme(root) -> None:
    """Apply colours and fonts to ttk widgets. Falls back to the default look if styling fails."""
    font = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
    root.configure(bg=COLORS["bg"])
    root.option_add("*Font", f"{{{font}}} 10")
    style = ttk.Style(root)
    for theme in ("vista", "clam", "aqua", "default"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except Exception:  # noqa: BLE001
                continue
    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=(font, 10))
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["card"], relief="flat")
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
    style.configure("CardMuted.TLabel", background=COLORS["card"], foreground=COLORS["muted"])
    style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(font, 16, "bold"))
    style.configure("Sub.TLabel", background=COLORS["bg"], foreground=COLORS["muted"], font=(font, 10))
    style.configure("TLabelframe", background=COLORS["card"], bordercolor=COLORS["border"])
    style.configure("TLabelframe.Label", background=COLORS["card"], foreground=COLORS["accent"],
                    font=(font, 10, "bold"))
    style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 8), font=(font, 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", COLORS["card"])],
              foreground=[("selected", COLORS["accent"])])
    style.configure("TButton", padding=(12, 6), background=COLORS["card"], foreground=COLORS["text"],
                    bordercolor=COLORS["border"])
    style.map("TButton", background=[("active", COLORS["accent_soft"]), ("disabled", "#f3f4f6")],
              foreground=[("disabled", "#9ca3af")])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="#ffffff",
                    bordercolor=COLORS["accent"], font=(font, 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#93c5fd")],
              foreground=[("disabled", "#ffffff")])
    style.configure("Danger.TButton", background=COLORS["danger"], foreground="#ffffff",
                    bordercolor=COLORS["danger"])
    style.map("Danger.TButton", background=[("active", "#b91c1c"), ("disabled", "#fca5a5")])
    style.configure("Icon.TButton", padding=(4, 2))
    style.configure("TEntry", fieldbackground=COLORS["card"], bordercolor=COLORS["border"], padding=4)
    style.configure("TCombobox", fieldbackground=COLORS["card"], bordercolor=COLORS["border"], padding=4)
    style.configure("Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
                    rowheight=26, bordercolor=COLORS["border"])
    style.configure("Treeview.Heading", background=COLORS["bg"], foreground=COLORS["muted"],
                    font=(font, 9, "bold"), relief="flat")
    style.map("Treeview", background=[("selected", COLORS["accent_soft"])],
              foreground=[("selected", COLORS["text"])])
    style.configure("Horizontal.TProgressbar", background=COLORS["accent"], troughcolor=COLORS["border"],
                    bordercolor=COLORS["border"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"])
    style.configure("Status.TLabel", background="#e5e7eb", foreground=COLORS["muted"], padding=(8, 4))
