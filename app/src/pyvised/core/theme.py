"""Light/dark handling: follow Windows (Auto) or force a colour scheme."""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QStyleFactory

from .settings import ThemeMode

ACCENT = "#1f6feb"
"""Logo blue, for accents drawn by the app itself."""


def windows_build() -> int:
    return sys.getwindowsversion().build if sys.platform == "win32" else 0


def choose_style() -> str:
    """`windows11` on Windows 11 (native look, dark mode), `Fusion` elsewhere (Windows 10)."""
    available = {key.lower(): key for key in QStyleFactory.keys()}  # noqa: SIM118 (a list)
    if windows_build() >= 22000 and "windows11" in available:
        return available["windows11"]
    return "Fusion"


def apply_theme(mode: ThemeMode) -> None:
    hints = QGuiApplication.styleHints()
    if mode is ThemeMode.AUTO:
        hints.unsetColorScheme()
    elif mode is ThemeMode.DARK:
        hints.setColorScheme(Qt.ColorScheme.Dark)
    else:
        hints.setColorScheme(Qt.ColorScheme.Light)


def is_dark() -> bool:
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Unknown:
        return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
    return scheme == Qt.ColorScheme.Dark
