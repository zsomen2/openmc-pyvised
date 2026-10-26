import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile


def run_self_test(tmp_path: Path) -> tuple[int, str]:
    report = tmp_path / "report.txt"
    env = dict(
        os.environ,
        QT_QPA_PLATFORM="offscreen",
        LOCALAPPDATA=str(tmp_path / "local"),  # lock file and log away from a running PyVised
        PYVISED_SETTINGS=str(tmp_path / "PyVised.ini"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pyvised", "--self-test", "--no-backend", "--report", str(report)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return proc.returncode, report.read_text(encoding="utf-8")


def test_self_test_without_the_backend_passes(tmp_path: Path):
    code, report = run_self_test(tmp_path)
    assert code == 0, report
    assert "translations: pyvised_hu.qm loaded, qtbase_hu.qm loaded" in report
    assert "[ OK ] examples: " in report
    assert "main window built in" in report
    assert "[SKIP] backend (--no-backend)" in report
    assert report.rstrip().endswith("RESULT: PASSED")


def test_self_test_refuses_while_pyvised_runs(tmp_path: Path):
    folder = tmp_path / "local" / "PyVised"
    folder.mkdir(parents=True)
    lock = QLockFile(str(folder / "pyvised.lock"))
    assert lock.tryLock(0)
    try:
        code, report = run_self_test(tmp_path)
    finally:
        lock.unlock()
    assert code == 2
    assert "already running" in report and "RESULT: FAILED" in report
