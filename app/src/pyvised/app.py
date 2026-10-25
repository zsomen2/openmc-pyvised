"""Start-up: logging, Qt, settings, theme, translations, the backend and the main window."""

import argparse
import contextlib
import ctypes
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from PySide6.QtCore import QCoreApplication, QLockFile, QSettings, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from . import __version__
from .backend.manager import BackendConfig, BackendManager, BackendState
from .core.i18n import install_translators
from .core.logging_setup import configure_logging
from .core.paths import app_icon_path, local_data_dir
from .core.settings import LANGUAGES, AppSettings, ThemeMode
from .core.tasks import TaskRunner
from .core.theme import apply_theme, choose_style
from .ui.main_window import MainWindow

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "PyVised.Desktop"
SETTINGS_ENV = "PYVISED_SETTINGS"
"""Path of an INI file to use instead of the user's settings (development, tests)."""
SETTLED_STATES = {
    BackendState.READY,
    BackendState.ERROR,
    BackendState.DOCKER_MISSING,
    BackendState.DOCKER_UNAVAILABLE,
    BackendState.IMAGE_MISSING,
}


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="pyvised", description="PyVised, a GUI for OpenMC")
    parser.add_argument("--no-backend", action="store_true", help="do not start the OpenMC backend")
    parser.add_argument(
        "--theme", choices=[mode.value for mode in ThemeMode], help="theme for this run only"
    )
    parser.add_argument("--lang", choices=list(LANGUAGES), help="language for this run only")
    parser.add_argument(
        "--screenshot",
        type=Path,
        metavar="PNG",
        help="save the window once the backend has settled, then quit",
    )
    return parser.parse_known_args(argv)


def _set_app_user_model_id() -> None:
    """Group the taskbar button under PyVised's own icon instead of python.exe."""
    if sys.platform == "win32":
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def _schedule_screenshot(
    app: QApplication,
    window: MainWindow,
    backend: BackendManager,
    path: Path,
    *,
    wait_for_backend: bool,
    timeout_s: int = 240,
) -> None:
    def capture() -> None:
        window.grab().save(str(path))
        log.info("screenshot saved to %s", path)
        app.quit()

    if not wait_for_backend:
        QTimer.singleShot(800, capture)
        return

    def on_state(state: str, _detail: str) -> None:
        if BackendState(state) in SETTLED_STATES:
            QTimer.singleShot(500, capture)

    backend.stateChanged.connect(on_state)
    QTimer.singleShot(timeout_s * 1000, capture)


def main(argv: list[str] | None = None) -> int:
    args, qt_args = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging()
    log.info("PyVised %s starting", __version__)
    _set_app_user_model_id()
    QApplication.setOrganizationName("PyVised")
    QApplication.setApplicationName("PyVised")
    QApplication.setApplicationVersion(__version__)
    app = QApplication([sys.argv[0], *qt_args])
    app.setStyle(choose_style())

    settings_file = os.environ.get(SETTINGS_ENV)
    settings = AppSettings(
        QSettings(settings_file, QSettings.Format.IniFormat) if settings_file else None
    )
    apply_theme(ThemeMode(args.theme) if args.theme else settings.theme)
    translators = install_translators(app, args.lang or settings.language)
    app.setWindowIcon(QIcon(str(app_icon_path())))

    lock: QLockFile | None = None
    if args.screenshot is None:
        local_data_dir().mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(local_data_dir() / "pyvised.lock"))
        if not lock.tryLock(100):
            QMessageBox.information(
                None, "PyVised", QCoreApplication.translate("main", "PyVised is already running.")
            )
            return 1
        if settings.ask_workspace_at_startup:
            folder = QFileDialog.getExistingDirectory(
                None,
                QCoreApplication.translate("main", "Choose the workspace folder"),
                str(settings.workspace),
            )
            if folder:
                settings.workspace = Path(folder)
                settings.sync()

    tasks = TaskRunner(app)
    backend = BackendManager(BackendConfig.from_settings(settings), parent=app)
    window = MainWindow(settings, backend, tasks)
    if args.screenshot is not None:
        # real platform rendering (fonts, style) without a window popping up
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    if not args.no_backend:
        QTimer.singleShot(0, backend.start)
    if args.screenshot is not None:
        _schedule_screenshot(
            app, window, backend, args.screenshot, wait_for_backend=not args.no_backend
        )

    code = app.exec()
    tasks.shutdown()
    if not args.no_backend:
        backend.shutdown()
    if lock is not None:
        lock.unlock()
    del translators
    return code
