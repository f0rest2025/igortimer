import os
import sys

from PySide6.QtGui import QIcon


def resource_path(relative_path):
    """Return an absolute path that works both in source mode and PyInstaller bundles."""
    candidates = []

    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, relative_path))

    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path))

    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), relative_path))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return candidates[0]


def app_icon():
    return QIcon(resource_path("logo.png"))
