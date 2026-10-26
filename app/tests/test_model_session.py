from pathlib import Path

import pytest
from fakes import FakeApi

from pyvised.backend.client import BackendError, Connection
from pyvised.core import model_session
from pyvised.core.model_session import ModelSession, folder_signature, relative_to_workspace
from pyvised.core.tasks import TaskRunner


@pytest.fixture
def session(qtbot, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(model_session, "ApiClient", FakeApi)
    tasks = TaskRunner()
    session = ModelSession(tasks)
    session.set_connection(Connection("http://127.0.0.1:1", "t"), tmp_path)
    yield session
    tasks.shutdown()


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_relative_to_workspace(tmp_path: Path):
    model = write(tmp_path / "ws" / "hw" / "model.py")
    assert relative_to_workspace(model, tmp_path / "ws") == "hw/model.py"
    assert relative_to_workspace(tmp_path / "elsewhere.py", tmp_path / "ws") is None


def test_folder_signature_ignores_the_sidecar_and_results(tmp_path: Path):
    model = write(tmp_path / "model.py", "1")
    before = folder_signature(model)
    write(tmp_path / "pyvised.json", "{}")
    (tmp_path / "statepoint.10.h5").write_bytes(b"\0")
    write(tmp_path / "runs" / "x" / "model.xml")
    assert folder_signature(model) == before
    write(tmp_path / "helper.py", "x = 1")
    assert folder_signature(model) != before


def test_open_reports_the_model_and_watches_its_files(qtbot, session: ModelSession, tmp_path):
    model = write(tmp_path / "hw" / "model.py", "a = 1")
    with qtbot.waitSignal(session.opened, timeout=5000) as opened:
        session.open(model)
    info, reloaded = opened.args
    assert info.source == "hw/model.py" and not reloaded
    assert session.path == model and session.generation == 1
    with qtbot.waitSignal(session.filesChanged, timeout=5000):
        model.write_text("a = 2", encoding="utf-8")
    with qtbot.waitSignal(session.opened, timeout=5000) as opened:
        session.reload()
    assert opened.args[1] is True, "the same model again"
    assert session.generation == 2


def test_models_outside_the_workspace_are_refused(qtbot, session: ModelSession, tmp_path):
    outside = write(tmp_path.parent / f"{tmp_path.name}-other" / "model.py")
    with qtbot.waitSignal(session.failed) as failed:
        session.open(outside)
    assert "workspace" in failed.args[0]
    assert session.path is None


def test_backend_errors_get_a_readable_message(qtbot, session: ModelSession, tmp_path):
    def broken(request):
        raise BackendError("build_failed: boom", "build_failed", "Traceback\nNameError: x")

    session._api.open_model = broken  # type: ignore[union-attr]
    with qtbot.waitSignal(session.failed, timeout=5000) as failed:
        session.open(write(tmp_path / "bad.py"))
    message, detail = failed.args
    assert message == "The model script stopped with an error."
    assert detail.endswith("NameError: x")
    assert not session.busy


def test_without_backend_nothing_is_sent(qtbot, tmp_path):
    session = ModelSession(TaskRunner())
    with qtbot.waitSignal(session.failed):
        session.open(write(tmp_path / "m.py"))
    assert not session.request_slice(None, print, print)  # type: ignore[arg-type]
