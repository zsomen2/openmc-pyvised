import os

# headless Qt for the tests; must be set before the QApplication exists
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

import pytest
from PySide6.QtCore import QSettings

from pyvised.core.settings import AppSettings


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    """Settings in a throw-away INI file (never the user's real settings)."""
    store = QSettings(str(tmp_path / "PyVised.ini"), QSettings.Format.IniFormat)
    return AppSettings(store)
