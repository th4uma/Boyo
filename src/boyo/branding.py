"""Application name, window title and icon resources."""
from pathlib import Path

from . import __version__


APP_NAME = "Boyo"
APP_TITLE = f"{APP_NAME} · 本地照片整理 {__version__}"
ICON_PATH = Path(__file__).with_name("assets") / "boyo.svg"


def app_icon():
    # Keep the read-only CLI free of Qt imports. SVG stays sharp at every DPI.
    from PySide6.QtGui import QIcon
    return QIcon(str(ICON_PATH))
