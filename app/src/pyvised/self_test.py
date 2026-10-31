"""`pyvised --self-test`: check an installation without showing a window (CI, lab deployment).

It checks the app itself (Qt, resources, translations, building the main window) and,
unless `--no-backend` is given, everything the first start needs: Docker, the backend
image (downloaded like at a normal start when it is missing), the container and the
backend's own self-test. The report goes to `--report` (by default `selftest.txt` in the
log folder), the log and stdout when there is one; the exit code is 0 when all passed.
"""

import datetime
import logging
import platform
import sys
import time
from pathlib import Path

import PySide6
from PySide6.QtCore import QEventLoop, QLibraryInfo, QTimer, QTranslator, qVersion
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .backend.client import ApiClient
from .backend.manager import BackendConfig, BackendManager, BackendState, Transfer
from .core.examples import find_examples
from .core.paths import app_icon_path, examples_dir, i18n_dir, log_dir, logo_path
from .core.settings import AppSettings
from .core.tasks import TaskRunner

log = logging.getLogger(__name__)

BACKEND_TIMEOUT_S = 3600
"""A first start downloads ~1 GB."""
SETTLED = {
    BackendState.READY,
    BackendState.ERROR,
    BackendState.DOCKER_MISSING,
    BackendState.DOCKER_UNAVAILABLE,
    BackendState.IMAGE_MISSING,
}


def default_report() -> Path:
    return log_dir() / "selftest.txt"


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.ok = True

    def add(self, ok: bool | None, text: str) -> None:
        """A check: passed (True), failed (False) or skipped (None)."""
        mark = {True: "[ OK ]", False: "[FAIL]", None: "[SKIP]"}[ok]
        self.ok = self.ok and ok is not False
        self.say(f"{mark} {text}")

    def say(self, line: str) -> None:
        self.lines.append(line)
        log.info("self-test: %s", line)
        if sys.stdout is not None:  # None in the installed (windowed) app
            encoding = sys.stdout.encoding or "ascii"
            print(line.encode(encoding, "replace").decode(encoding), flush=True)


def run_self_test(
    app: QApplication, settings: AppSettings, *, report_path: Path | None, backend: bool
) -> int:
    report = _header()
    report.add(
        True,
        f"Qt {qVersion()}, PySide6 {PySide6.__version__}, Python {platform.python_version()}, "
        f"platform {app.platformName()}, style {app.style().name()}",
    )
    _check_resources(report)
    _check_examples(report)
    _check_translations(report)
    _check_window(settings, report)
    if backend:
        _check_backend(settings, report)
    else:
        report.add(None, "backend (--no-backend)")
    return _finish(report, report_path)


def blocked(report_path: Path | None) -> int:
    """The report when another PyVised runs (it would share the backend container)."""
    report = _header()
    report.add(False, "PyVised is already running; close it and run the self-test again")
    _finish(report, report_path)
    return 2


def _header() -> Report:
    report = Report()
    frozen = "installed" if getattr(sys, "frozen", False) else "source"
    report.say(f"PyVised self-test, {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    report.say(f"PyVised {__version__} ({frozen}) on {platform.platform()}")
    return report


def _finish(report: Report, report_path: Path | None) -> int:
    report.say("RESULT: " + ("PASSED" if report.ok else "FAILED"))
    path = report_path or default_report()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(report.lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("cannot write the self-test report %s: %s", path, exc)
        return 1
    return 0 if report.ok else 1


def _check_resources(report: Report) -> None:
    icon = app_icon_path()
    ok = icon.is_file() and logo_path().is_file() and not QIcon(str(icon)).isNull()
    report.add(ok, f"resources: app icon and logo in {icon.parent}")


def _check_examples(report: Report) -> None:
    folder = examples_dir()
    examples, problems = find_examples(folder)
    report.add(bool(examples) and not problems, f"examples: {len(examples)} in {folder}")
    for problem in problems:
        report.say(f"       {problem}")


def _check_translations(report: Report) -> None:
    own = QTranslator()
    own_ok = own.load("pyvised_hu", str(i18n_dir()))
    translated = own.translate("MainWindow", "Geometry") if own_ok else ""
    qt = QTranslator()
    qt_ok = qt.load("qtbase_hu", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    report.add(
        own_ok and translated == "Geometria" and qt_ok,
        f"translations: pyvised_hu.qm {'loaded' if own_ok else 'missing'}, "
        f"qtbase_hu.qm {'loaded' if qt_ok else 'missing'}",
    )


def _check_window(settings: AppSettings, report: Report) -> None:
    from .ui.main_window import MainWindow

    tasks = TaskRunner()
    began = time.perf_counter()
    try:
        window = MainWindow(settings, BackendManager(BackendConfig.from_settings(settings)), tasks)
    except Exception as exc:
        log.exception("building the main window failed")
        report.add(False, f"main window: {type(exc).__name__}: {exc}")
        return
    finally:
        tasks.shutdown()
    report.add(True, f"main window built in {time.perf_counter() - began:.2f} s")
    window.deleteLater()


def _check_backend(settings: AppSettings, report: Report) -> None:
    config = BackendConfig.from_settings(settings)
    manager = BackendManager(config)
    loop = QEventLoop()
    states: list[BackendState] = []
    transfers: list[Transfer] = []
    logged = [-1]

    def on_state(state: str, detail: str) -> None:
        states.append(BackendState(state))
        if BackendState(state) in SETTLED:
            loop.quit()

    def on_transfer(transfer: Transfer) -> None:
        transfers.append(transfer)
        fraction = transfer.fraction
        if fraction is not None and int(fraction * 10) > logged[0]:
            logged[0] = int(fraction * 10)
            log.info("self-test: %s %.0f %%", transfer.kind, fraction * 100)

    manager.stateChanged.connect(on_state)
    manager.transferChanged.connect(on_transfer)
    began = time.monotonic()
    QTimer.singleShot(0, manager.start)
    QTimer.singleShot(BACKEND_TIMEOUT_S * 1000, loop.quit)
    loop.exec()
    try:
        _report_backend(manager, config, report, states, transfers, time.monotonic() - began)
        if manager.state is BackendState.READY:
            _report_selftest(manager, settings, report)
    finally:
        manager.shutdown()


def _report_backend(
    manager: BackendManager,
    config: BackendConfig,
    report: Report,
    states: list[BackendState],
    transfers: list[Transfer],
    seconds: float,
) -> None:
    facts = manager.facts
    state = manager.state
    if facts.docker is None:
        report.add(False, "Docker: docker.exe not found (is Docker Desktop installed?)")
        return
    if facts.engine is None:
        report.add(False, f"Docker: the engine does not answer ({state.value})")
        return
    report.add(
        True,
        f"Docker: engine {facts.engine}, Docker Desktop {facts.desktop_version or '-'}, "
        f"{(facts.free_bytes or 0) / 1e9:.0f} GB free in {facts.data_dir}",
    )
    image = config.image
    if facts.image_id is None:
        report.add(False, f"backend image {image}: {_first_lines(manager.detail) or state.value}")
        return
    if BackendState.PULLING in states:
        size = max((transfer.done for transfer in transfers), default=0)
        how = f"{size / 1e6:.0f} MB downloaded" if size else "its layers were already here"
        report.add(True, f"backend image {image}: pulled ({how})")
    else:
        report.add(True, f"backend image {image}: present")
    health = manager.health
    if state is not BackendState.READY or health is None:
        report.add(False, f"backend: {_first_lines(manager.detail) or state.value}")
        return
    report.add(
        True,
        f"backend: OpenMC {health.openmc_version}, protocol {health.protocol_version}, "
        f"{health.cpu_count} CPUs, ready after {seconds:.0f} s",
    )


def _first_lines(detail: str, count: int = 3) -> str:
    """The failure and Docker's reason for it, on one line."""
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    return " | ".join(lines[:count])[:400]


def _report_selftest(manager: BackendManager, settings: AppSettings, report: Report) -> None:
    connection = manager.connection
    assert connection is not None
    try:
        with ApiClient(connection) as api:
            result = api.selftest()
    except Exception as exc:
        report.add(False, f"backend self-test: {exc}")
        return
    steps = ", ".join(
        f"{step.name} {step.seconds:.1f} s" + ("" if step.ok else f" ({step.detail})")
        for step in result.steps
    )
    if result.k_eff is not None and result.k_eff_std is not None:
        steps += f"; k-eff = {result.k_eff:.4f} +/- {result.k_eff_std:.4f}"
    report.add(result.ok, f"backend self-test: {steps}")
    if result.ok and manager.facts.image_id:
        settings.selftest_image = manager.facts.image_id
        settings.sync()
