"""Persistent user settings: an INI file in `%APPDATA%\\PyVised` (QSettings)."""

from enum import StrEnum
from pathlib import Path
from typing import cast

from PySide6.QtCore import QByteArray, QSettings

from pyvised_protocol.sidecar import Basis

from .paths import default_workspace

LANGUAGES = {"en": "English", "hu": "Magyar"}
MAX_RECENT = 8
BASES = ("xy", "xz", "yz")


class ThemeMode(StrEnum):
    AUTO = "auto"
    LIGHT = "light"
    DARK = "dark"


class ReloadPolicy(StrEnum):
    """What happens when the files of the open model change on disk."""

    ASK = "ask"
    ALWAYS = "always"
    NEVER = "never"


class PlotBackground(StrEnum):
    THEME = "theme"
    WHITE = "white"


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
        return self._enum("general/theme", ThemeMode, ThemeMode.AUTO)

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

    @property
    def reopen_last_model(self) -> bool:
        return self._bool("general/reopen_last_model", True)

    @reopen_last_model.setter
    def reopen_last_model(self, value: bool) -> None:
        self._store.setValue("general/reopen_last_model", bool(value))

    @property
    def reload_on_change(self) -> ReloadPolicy:
        return self._enum("general/reload_on_change", ReloadPolicy, ReloadPolicy.ASK)

    @reload_on_change.setter
    def reload_on_change(self, value: ReloadPolicy) -> None:
        self._store.setValue("general/reload_on_change", ReloadPolicy(value).value)

    # -- models --------------------------------------------------------------

    @property
    def recent_models(self) -> list[Path]:
        """Most recently opened models, newest first."""
        value = self._store.value("models/recent", [])
        if isinstance(value, str):  # QSettings returns a lone entry as a plain string
            value = [value]
        entries = value if isinstance(value, list) else []
        return [Path(str(v)) for v in entries if str(v)][:MAX_RECENT]

    def add_recent_model(self, path: Path) -> None:
        keep = [p for p in self.recent_models if p != path]
        self._store.setValue("models/recent", [str(p) for p in [path, *keep][:MAX_RECENT]])

    def clear_recent_models(self) -> None:
        self._store.remove("models/recent")

    # -- geometry ------------------------------------------------------------

    @property
    def render_quality(self) -> float:
        """Slice resolution relative to the device pixels of a view (0.5-1.0)."""
        try:
            value = float(self._store.value("geometry/render_quality", 1.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.5, value))

    @render_quality.setter
    def render_quality(self, value: float) -> None:
        self._store.setValue("geometry/render_quality", min(1.0, max(0.5, float(value))))

    @property
    def plot_background(self) -> PlotBackground:
        return self._enum("geometry/plot_background", PlotBackground, PlotBackground.THEME)

    @plot_background.setter
    def plot_background(self, value: PlotBackground) -> None:
        self._store.setValue("geometry/plot_background", PlotBackground(value).value)

    @property
    def color_mode(self) -> str:
        """Value of `render.colorize.ColorMode`."""
        value = str(self._store.value("geometry/color_mode", "material"))
        return value if value in ("material", "cell", "none") else "material"

    @color_mode.setter
    def color_mode(self, value: str) -> None:
        self._store.setValue("geometry/color_mode", value)

    @property
    def show_lines(self) -> bool:
        return self._bool("geometry/lines", True)

    @show_lines.setter
    def show_lines(self, value: bool) -> None:
        self._store.setValue("geometry/lines", bool(value))

    @property
    def line_mode(self) -> str:
        """Value of `render.colorize.LineMode`."""
        value = str(self._store.value("geometry/line_mode", "cell_instance"))
        return value if value in ("cell_instance", "cell", "material") else "cell_instance"

    @line_mode.setter
    def line_mode(self, value: str) -> None:
        self._store.setValue("geometry/line_mode", value)

    @property
    def line_width(self) -> int:
        try:
            value = int(self._store.value("geometry/line_width", 1))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1
        return 2 if value >= 2 else 1

    @line_width.setter
    def line_width(self, value: int) -> None:
        self._store.setValue("geometry/line_width", 2 if int(value) >= 2 else 1)

    @property
    def default_views(self) -> tuple[Basis, Basis]:
        """Bases of the left and right view (Front XZ + Top XY unless changed)."""
        value = str(self._store.value("geometry/default_views", "xz,xy")).split(",")
        if len(value) == 2 and all(v in BASES for v in value):
            return cast(Basis, value[0]), cast(Basis, value[1])
        return "xz", "xy"

    @default_views.setter
    def default_views(self, value: tuple[Basis, Basis]) -> None:
        self._store.setValue("geometry/default_views", ",".join(value))

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

    @property
    def geometry_splitter(self) -> QByteArray | None:
        value = self._store.value("window/geometry_splitter")
        return value if isinstance(value, QByteArray) else None

    @geometry_splitter.setter
    def geometry_splitter(self, value: QByteArray) -> None:
        self._store.setValue("window/geometry_splitter", value)

    def _enum[E: StrEnum](self, key: str, kind: type[E], default: E) -> E:
        try:
            return kind(str(self._store.value(key, default.value)))
        except ValueError:
            return default

    def _bool(self, key: str, default: bool) -> bool:
        value = self._store.value(key, default)
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
