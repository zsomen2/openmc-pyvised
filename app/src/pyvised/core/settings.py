"""Persistent user settings: an INI file in `%APPDATA%\\PyVised` (QSettings)."""

from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from .paths import default_workspace

LANGUAGES = {"en": "English", "hu": "Magyar"}


class ThemeMode(StrEnum):
    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


class AppSettings:
    """Typed access to the settings; every property reads and writes the store directly."""

    def __init__(self, store: QSettings | None = None) -> None:
        self._store = store or QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "PyVised", "PyVised"
        )

    @property
    def file_path(self) -> str:
        return self._store.fileName()

    def sync(self) -> None:
        self._store.sync()

    # -- general -------------------------------------------------------------

    @property
    def language(self) -> str:
        value = str(self._store.value("general/language", "en"))
        return value if value in LANGUAGES else "en"

    @language.setter
    def language(self, value: str) -> None:
        self._store.setValue("general/language", value if value in LANGUAGES else "en")

    @property
    def theme(self) -> ThemeMode:
        value = str(self._store.value("general/theme", ThemeMode.AUTO.value))
        try:
            return ThemeMode(value)
        except ValueError:
            return ThemeMode.AUTO

    @theme.setter
    def theme(self, value: ThemeMode) -> None:
        self._store.setValue("general/theme", ThemeMode(value).value)

    @property
    def workspace(self) -> Path:
        value = self._store.value("general/workspace", "")
        return Path(str(value)) if value else default_workspace()

    @workspace.setter
    def workspace(self, value: Path) -> None:
        self._store.setValue("general/workspace", str(value))

    @property
    def ask_workspace_at_startup(self) -> bool:
        return self._bool("general/ask_workspace_at_startup", False)

    @ask_workspace_at_startup.setter
    def ask_workspace_at_startup(self, value: bool) -> None:
        self._store.setValue("general/ask_workspace_at_startup", bool(value))

    # -- backend -------------------------------------------------------------

    @property
    def backend_image(self) -> str:
        """Image reference chosen by the user; empty means the image matching this version."""
        return str(self._store.value("backend/image", "")).strip()

    @backend_image.setter
    def backend_image(self, value: str) -> None:
        self._store.setValue("backend/image", value.strip())

    @property
    def keep_backend_running(self) -> bool:
        return self._bool("backend/keep_running", False)

    @keep_backend_running.setter
    def keep_backend_running(self, value: bool) -> None:
        self._store.setValue("backend/keep_running", bool(value))

    @property
    def backend_cpus(self) -> float:
        """CPU limit for the container; 0 means no limit."""
        try:
            return max(0.0, float(self._store.value("backend/cpus", 0.0)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    @backend_cpus.setter
    def backend_cpus(self, value: float) -> None:
        self._store.setValue("backend/cpus", max(0.0, float(value)))

    # -- window --------------------------------------------------------------

    @property
    def window_geometry(self) -> QByteArray | None:
        value = self._store.value("window/geometry")
        return value if isinstance(value, QByteArray) else None

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._store.setValue("window/geometry", value)

    @property
    def window_state(self) -> QByteArray | None:
        value = self._store.value("window/state")
        return value if isinstance(value, QByteArray) else None

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._store.setValue("window/state", value)

    def _bool(self, key: str, default: bool) -> bool:
        value = self._store.value(key, default)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
