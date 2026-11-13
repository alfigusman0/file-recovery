"""Build a standalone FileRecovery app with PyInstaller for the current OS.

  Windows : dist/FileRecovery.exe        (one file, windowed: no console window)
  macOS   : dist/FileRecovery.app        (app bundle, windowed)
  Linux   : dist/FileRecovery            (one file binary, run from a terminal)

PyInstaller cannot cross-compile: run this script on each OS you want to ship.
Run `python build_app.py --help` for details.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "FileRecovery"
ENTRY = os.path.join(HERE, "run_gui.pyw")
STATIC_REL = os.path.join("recovery", "webapp", "static")
STATIC_FILES = ("index.html", "app.css", "app.js")

HELP_EPILOG = f"""\
What it builds (on the OS you run it on):
  Windows  dist\\{APP_NAME}.exe     single windowed exe, double-click to start
  macOS    dist/{APP_NAME}.app     app bundle (open it from Finder)
  Linux    dist/{APP_NAME}         single binary, start it with ./{APP_NAME}

The app starts a local web server on 127.0.0.1 and opens the interface in the
browser. The web files in {STATIC_REL.replace(os.sep, '/')}/ ({', '.join(STATIC_FILES)})
are bundled as data files. pytsk3 is bundled when it is installed; without it the
deleted-file scan with original names is not available in the built app.

Steps:
  python -m pip install -r requirements.txt
  python -m pip install pyinstaller
  python build_app.py

Raw disk access still needs Administrator (Windows) or root/sudo (macOS, Linux)
when the built app runs. PyInstaller cannot cross-compile: build on each OS.
"""


def build_command(system: str | None = None) -> list[str]:
    """PyInstaller arguments for `system` ('windows', 'macos', 'linux'; default: current OS)."""
    if system is None:
        system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    static_src = os.path.join(HERE, STATIC_REL)
    # The server finds its web files next to recovery/webapp/server.py, so keep the same layout.
    static_dest = STATIC_REL.replace(os.sep, "/")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--add-data", f"{static_src}{os.pathsep}{static_dest}",
    ]
    if system == "windows":
        cmd += ["--onefile", "--windowed"]
    elif system == "macos":
        # A windowed build on macOS produces an .app bundle; onedir is the supported form for bundles.
        cmd += ["--onedir", "--windowed", "--osx-bundle-identifier", "id.ac.uinsgd.filerecovery"]
    else:
        cmd += ["--onefile"]
    if importlib.util.find_spec("pytsk3") is not None:
        cmd += ["--collect-all", "pytsk3"]
    cmd.append(ENTRY)
    return cmd


def _missing_inputs() -> list[str]:
    missing = [ENTRY] if not os.path.isfile(ENTRY) else []
    for name in STATIC_FILES:
        path = os.path.join(HERE, STATIC_REL, name)
        if not os.path.isfile(path):
            missing.append(path)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_app.py",
        description=f"Build a standalone {APP_NAME} app with PyInstaller for the current OS.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="only print the PyInstaller command, do not build")
    args = parser.parse_args(argv)

    missing = _missing_inputs()
    if missing:
        print("ERROR: required files are missing:", file=sys.stderr)
        for path in missing:
            print("  " + path, file=sys.stderr)
        return 2

    cmd = build_command()
    printable = subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)
    if args.dry_run:
        print(printable)
        return 0

    if importlib.util.find_spec("PyInstaller") is None:
        print("ERROR: PyInstaller is not installed for this Python:\n"
              f"  {sys.executable}\n"
              "Install it, then run this script again:\n"
              "  python -m pip install pyinstaller", file=sys.stderr)
        return 1
    if "--collect-all" not in cmd:
        print("WARNING: pytsk3 is not installed; the app will be built without the deleted-file "
              "scan. Install it with: python -m pip install -r requirements.txt", file=sys.stderr)

    print("Running:", printable)
    code = subprocess.call(cmd, cwd=HERE)
    if code == 0:
        out = {"win32": f"{APP_NAME}.exe", "darwin": f"{APP_NAME}.app"}.get(sys.platform, APP_NAME)
        print(f"\nDone: {os.path.join(HERE, 'dist', out)}")
    else:
        print(f"\nPyInstaller failed with exit code {code}. See the messages above.", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
