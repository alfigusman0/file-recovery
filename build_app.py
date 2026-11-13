"""Build a standalone app for the current OS (Windows .exe, macOS .app, Linux binary).

Usage:
  python -m pip install pyinstaller pytsk3
  python build_app.py

PyInstaller cannot cross-compile: run this script on each OS you want to ship.
"""

import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    sep = ";" if platform.system() == "Windows" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--windowed", "--onefile",
        "--name", "FileRecovery",
        "--add-data", f"README.md{sep}.",
        "--collect-all", "pytsk3",
        os.path.join(HERE, "run_gui.pyw"),
    ]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=HERE)


if __name__ == "__main__":
    sys.exit(main())
