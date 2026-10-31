"""The run manager with a fake openmc executable (no OpenMC needed)."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from run_doubles import manager_for

from pyvised_backend import runs
from pyvised_backend.errors import ServiceError
from pyvised_backend.runs import RunManager, complete_utf8, copy_input, folder_name
from pyvised_protocol.runs import (
    FINAL_STATES,
    INPUT_FOLDER,
    LOG_FILE,
    RUN_FILE,
    RunInfo,
    RunRequest,
    RunSettings,
    run_id_for,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "hw").mkdir(parents=True)
    (ws / "hw" / "model.py").write_text("", encoding="utf-8")
    return ws


def wait_until_final(manager: RunManager, run_id: str, timeout: float = 30.0) -> RunInfo:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = manager.status(run_id).run
        run = manager._active
        if info.state in FINAL_STATES and (run is None or run.finished.wait(timeout)):
            return info
        time.sleep(0.05)
    raise AssertionError("the run did not end")


def wait_for_batch(manager: RunManager, run_id: str, batch: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        progress = manager.status(run_id).progress
        if progress is not None and progress.batch >= batch:
            return
        time.sleep(0.02)
    raise AssertionError(f"batch {batch} was not reached")


def request(**settings) -> RunRequest:
    created = datetime(2026, 10, 26, 14, 30, 5, tzinfo=timezone(timedelta(hours=1)))
    return RunRequest(
        path="hw/model.py",
        label="quick test",
        params={"rod": 2.0},
        settings=RunSettings(**settings),
        created=created,
    )


def test_folder_names_and_utf8_boundaries():
    when = datetime(2026, 1, 2, 3, 4, 5)
    assert folder_name(when, "") == "20260102-030405"
    assert folder_name(when, " rúd 30 cm / ki? ") == "20260102-030405_rúd-30-cm-ki"
    data = "kötegek".encode()
    assert complete_utf8(data) == len(data)
    assert complete_utf8(data[:2]) == 1  # "k" + half of "ö"
    assert complete_utf8("€".encode()[:2]) == 0


def test_a_run_finishes_and_keeps_its_files(workspace: Path, tmp_path: Path):
    manager, hub = manager_for(workspace, tmp_path)
    info = manager.start(request(statepoint_every=5))
    assert info.state == "running" and info.path == "hw/runs/20261026-143005_quick-test"
    assert info.params == {"rod": 2.0} and info.library == "builtin"
    assert info.plan is not None and info.plan.statepoints == [5, 10, 15, 20]
    assert info.plan.threads == 4 and info.versions["protocol"]
    final = wait_until_final(manager, info.id)
    assert final.state == "finished" and final.stop_reason == "completed"
    assert final.exit_code == 0 and final.batches_done == 20
    assert final.has_results and final.k_eff == 1.0
    folder = workspace / final.path
    stored = RunInfo.model_validate_json((folder / RUN_FILE).read_text(encoding="utf-8"))
    assert stored.state == "finished"
    assert "Bat./Gen." in (folder / LOG_FILE).read_text(encoding="utf-8")
    assert not (folder / "info.json").exists(), "the cache's own file is not copied"
    assert [p.name for p in folder.glob("statepoint.*.h5")] == ["statepoint.20.h5"]
    snapshotted = [name for names in manager.snapshotted for name in names]
    assert snapshotted == [f"statepoint.{b:02d}.h5" for b in (5, 10, 15, 20)]
    progress = manager.status(info.id).progress
    assert progress is not None and len(progress.k) == 20 and progress.mean[2] is None
    types = hub.types()
    assert types[0] == "run.state" and types[-1] == "run.finished"
    assert "run.progress" in types and "run.log" in types
    # the published increments add up to the whole history, in order
    ks: list[float] = []
    for event in hub.events:
        if event.type == "run.progress":
            assert event.data["first"] == len(ks)
            ks += event.data["k"]
    assert ks == progress.k
    text = "".join(e.data["text"] for e in hub.events if e.type == "run.log")
    assert text == (folder / LOG_FILE).read_text(encoding="utf-8")
    assert manager.results(info.id).statepoint == "statepoint.20.h5"


def test_stop_keeps_the_latest_statepoint(workspace: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENMC_DELAY", "0.05")
    manager, _ = manager_for(workspace, tmp_path, batches=400)
    info = manager.start(request(statepoint_every=5))
    wait_for_batch(manager, info.id, 12)
    assert manager.stop(info.id).id == info.id
    final = wait_until_final(manager, info.id)
    assert final.state == "stopped" and final.stop_reason == "user"
    assert final.has_results and 10 <= final.batches_done < 400
    assert manager.results(info.id).batches >= 10


def test_time_limit(workspace: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENMC_DELAY", "0.05")
    manager, _ = manager_for(workspace, tmp_path, batches=400)
    info = manager.start(request(time_limit=1.0))
    final = wait_until_final(manager, info.id)
    assert final.state == "stopped" and final.stop_reason == "time_limit"
    assert final.seconds < 8.0


def test_a_failed_run_reports_openmc_error(workspace: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENMC_FAIL", "1")
    manager, hub = manager_for(workspace, tmp_path)
    info = manager.start(request())
    final = wait_until_final(manager, info.id)
    assert final.state == "failed" and final.stop_reason == "error" and final.exit_code == 1
    assert final.error == "ERROR: Something went wrong in the fake transport code."
    assert not final.has_results
    assert hub.types()[-1] == "run.finished"
    with pytest.raises(ServiceError) as caught:
        manager.results(info.id)
    assert caught.value.error == "no_results"


def test_one_run_at_a_time(workspace: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENMC_DELAY", "0.05")
    manager, _ = manager_for(workspace, tmp_path, batches=400)
    info = manager.start(request())
    with pytest.raises(ServiceError) as caught:
        manager.start(request())
    assert caught.value.error == "run_active" and caught.value.status == 409
    with pytest.raises(ServiceError):
        manager.delete(info.id)
    manager.stop(info.id)
    wait_until_final(manager, info.id)
    second = manager.start(request())
    assert second.path.endswith("_quick-test-2"), "the same second gets a numbered folder"
    manager.stop(second.id)
    wait_until_final(manager, second.id)


def test_history_log_and_delete(workspace: Path, tmp_path: Path):
    manager, _ = manager_for(workspace, tmp_path)
    info = manager.start(request())
    wait_until_final(manager, info.id)
    runs = manager.list("hw/model.py")
    assert [r.id for r in runs] == [info.id]
    assert manager.list("hw/other.py") == []
    chunk = manager.log_text(info.id, 0, limit=40)
    assert chunk.offset == 0 and chunk.next == len(chunk.text.encode()) and chunk.size > 40
    rest = manager.log_text(info.id, chunk.next)
    assert chunk.text + rest.text == (workspace / info.path / LOG_FILE).read_text(encoding="utf-8")
    manager.delete(info.id)
    assert not (workspace / info.path).exists()
    with pytest.raises(ServiceError) as caught:
        manager.status(info.id)
    assert caught.value.status == 404


def test_a_run_keeps_the_model_code_it_started_with(workspace: Path, tmp_path: Path):
    folder = workspace / "hw"
    (folder / "model.py").write_text("import helper\n", encoding="utf-8")
    (folder / "helper.py").write_text("R = 3\n", encoding="utf-8")
    (folder / "notes.ipynb").write_text('{"cells": []}', encoding="utf-8")
    for name in ("pyvised.json", "data.json", "table.csv", "model.xml"):
        (folder / name).write_text("{}", encoding="utf-8")
    (folder / "lib").mkdir()
    (folder / "lib" / "deep.py").write_text("", encoding="utf-8")
    manager, _ = manager_for(workspace, tmp_path)
    info = manager.start(request())
    (folder / "helper.py").write_text("R = 4\n", encoding="utf-8")  # edited during the run
    wait_until_final(manager, info.id)
    copies = workspace / info.path / INPUT_FOLDER
    assert sorted(p.name for p in copies.iterdir()) == ["helper.py", "model.py", "notes.ipynb"]
    assert (copies / "helper.py").read_text(encoding="utf-8") == "R = 3\n"
    assert manager.list("hw/model.py")[0].id == info.id, "still listed as a run"

    (workspace / "xml").mkdir()
    (workspace / "xml" / "model.xml").write_text("<model/>", encoding="utf-8")
    (workspace / "xml" / "helper.py").write_text("", encoding="utf-8")
    xml = manager.start(request().model_copy(update={"path": "xml/model.xml", "label": "x"}))
    wait_until_final(manager, xml.id)
    assert not (workspace / xml.path / INPUT_FOLDER).exists(), "an XML model has no code"


def test_keeping_the_input_never_stops_a_run(tmp_path: Path, monkeypatch):
    model = tmp_path / "m" / "model.py"
    model.parent.mkdir()
    model.write_text("", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()

    def refuse(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(runs.shutil, "copy2", refuse)
    assert copy_input(model, run) == []


def test_run_ids_stay_in_the_workspace(workspace: Path, tmp_path: Path):
    manager, _ = manager_for(workspace, tmp_path)
    for bad in ("not-base64!", run_id_for("../outside/runs/x"), run_id_for("hw/model.py")):
        with pytest.raises(ServiceError) as caught:
            manager.status(bad)
        assert caught.value.status == 404


def test_runs_left_running_by_a_backend_restart(workspace: Path, tmp_path: Path):
    manager, _ = manager_for(workspace, tmp_path)
    folder = workspace / "hw" / "runs" / "20260101-000000"
    folder.mkdir(parents=True)
    (folder / "statepoint.10.h5").write_bytes(b"x")
    info = RunInfo(
        id=run_id_for("hw/runs/20260101-000000"),
        path="hw/runs/20260101-000000",
        model="hw/model.py",
        state="running",
        created=datetime(2026, 1, 1),
    )
    (folder / RUN_FILE).write_text(info.model_dump_json(), encoding="utf-8")
    (listed,) = manager.list("hw/model.py")
    assert listed.state == "stopped" and listed.stop_reason == "interrupted"
    assert listed.has_results
    stored = json.loads((folder / RUN_FILE).read_text(encoding="utf-8"))
    assert stored["state"] == "stopped"
    assert manager.results(info.id).statepoint == "statepoint.10.h5", "summarised on demand"


def test_a_run_of_another_backend_is_left_alone(workspace: Path, tmp_path: Path):
    import os

    from pyvised_backend.runs import HEARTBEAT_FILE, HEARTBEAT_STALE

    manager, _ = manager_for(workspace, tmp_path)
    folder = workspace / "hw" / "runs" / "20260102-000000"
    folder.mkdir(parents=True)
    info = RunInfo(
        id=run_id_for("hw/runs/20260102-000000"),
        path="hw/runs/20260102-000000",
        model="hw/model.py",
        state="running",
        created=datetime(2026, 1, 2),
    )
    (folder / RUN_FILE).write_text(info.model_dump_json(), encoding="utf-8")
    (folder / "statepoint.10.h5").write_bytes(b"x")
    heartbeat = folder / HEARTBEAT_FILE
    heartbeat.touch()
    (listed,) = manager.list("hw/model.py")
    assert listed.state == "running", "a fresh heartbeat: another backend runs it"
    with pytest.raises(ServiceError):
        manager.delete(info.id)
    with pytest.raises(ServiceError):
        manager.results(info.id)  # not summarised behind the other backend's back
    old = time.time() - HEARTBEAT_STALE - 5
    os.utime(heartbeat, (old, old))
    (listed,) = manager.list("hw/model.py")
    assert listed.state == "stopped" and listed.stop_reason == "interrupted"


def test_stop_while_summarising_keeps_a_completed_run(workspace: Path, tmp_path: Path):
    import threading

    manager, _hub = manager_for(workspace, tmp_path)
    summarising = threading.Event()
    release = threading.Event()
    original = manager._summarise

    def slow_summary(folder, run_id):
        summarising.set()
        release.wait(10)
        return original(folder, run_id)

    manager._summarise = slow_summary  # type: ignore[method-assign]
    info = manager.start(request())
    assert summarising.wait(20)
    assert manager.status(info.id).run.state == "summarising"
    manager.stop(info.id)
    manager.shutdown(timeout=0.1)
    release.set()
    final = wait_until_final(manager, info.id)
    assert final.state == "finished" and final.stop_reason == "completed"


def test_a_failure_while_finishing_still_ends_the_run(workspace: Path, tmp_path: Path):
    manager, hub = manager_for(workspace, tmp_path)

    def broken_summary(folder, run_id):
        raise RuntimeError("disk full")

    manager._summarise = broken_summary  # type: ignore[method-assign]
    info = manager.start(request())
    final = wait_until_final(manager, info.id)
    assert final.state == "failed" and "could not finish" in final.error
    assert hub.types()[-1] == "run.finished"
    assert not manager.status(info.id).run.active
    second = manager.start(request())  # the manager is free again
    wait_until_final(manager, second.id)
