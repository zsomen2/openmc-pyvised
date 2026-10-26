"""Theme-aware icons from qtawesome (Material Design Icons)."""

import os

os.environ.setdefault("QT_API", "pyside6")

import qtawesome as qta
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

_ARGB = QColor.NameFormat.HexArgb  # keeps the alpha the Windows 11 style uses for disabled text


def icon(name: str) -> QIcon:
    """Icon `name` (e.g. "mdi6.cog-outline") tinted with the current text colour."""
    palette = QApplication.palette()
    normal = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText)
    disabled = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    return qta.icon(name, color=normal.name(_ARGB), color_disabled=disabled.name(_ARGB))
