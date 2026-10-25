from pathlib import Path

from PySide6.QtCore import QSettings

from pyvised.core.paths import default_workspace
from pyvised.core.settings import AppSettings, ThemeMode


def test_defaults(settings: AppSettings):
    assert settings.language == "en"
    assert settings.theme is ThemeMode.AUTO
    assert settings.workspace == default_workspace()
    assert settings.ask_workspace_at_startup is False
    assert settings.backend_image == ""
    assert settings.keep_backend_running is False
    assert settings.backend_cpus == 0.0


def test_values_survive_a_reload(tmp_path: Path):
    path = str(tmp_path / "PyVised.ini")
    first = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    first.language = "hu"
    first.theme = ThemeMode.DARK
    first.workspace = tmp_path / "work"
    first.ask_workspace_at_startup = True
    first.backend_image = " pyvised-backend:dev "
    first.keep_backend_running = True
    first.backend_cpus = 4
    first.sync()

    second = AppSettings(QSettings(path, QSettings.Format.IniFormat))
    assert second.language == "hu"
    assert second.theme is ThemeMode.DARK
    assert second.workspace == tmp_path / "work"
    assert second.ask_workspace_at_startup is True
    assert second.backend_image == "pyvised-backend:dev"
    assert second.keep_backend_running is True
    assert second.backend_cpus == 4.0


def test_invalid_values_fall_back(settings: AppSettings):
    settings.language = "de"
    assert settings.language == "en"
    settings._store.setValue("general/theme", "purple")
    assert settings.theme is ThemeMode.AUTO
    settings._store.setValue("backend/cpus", "many")
    assert settings.backend_cpus == 0.0
