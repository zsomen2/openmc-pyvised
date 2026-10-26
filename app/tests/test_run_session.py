"""The run session: following a run from events, missed events, history, start failures."""

from datetime import datetime
from typing import ClassVar

import pytest

from pyvised.backend.client import BackendError, Connection
from pyvised.core import run_session
from pyvised.core.run_session import RunSession, merge_progress
from pyvised.core.tasks import TaskRunner
from pyvised_protocol.runs import (
    Event,
    LogChunk,
    RunInfo,
    RunPlan,
    RunProgress,
    RunRequest,
    RunStatus,
)


def run_info(run_id: str = "r1", state: str = "running", **kwargs) -> RunInfo:
    return RunInfo(
        id=run_id,
        path=f"hw/runs/{run_id}",
        model="hw/model.py",
        state=state,  # type: ignore[arg-type]
        created=datetime(2026, 10, 26, 12, 0),
        plan=RunPlan(run_mode="eigenvalue", particles=100, batches=10, inactive=2, threads=4),
        **kwargs,
    )


class FakeRunApi:
    instances: ClassVar[list["FakeRunApi"]] = []

    def __init__(self, connection) -> None:
        self.connection = connection
        self.started: list[RunRequest] = []
        self.stopped: list[str] = []
        self.status_calls: list[str] = []
        self.active: RunStatus | None = None
        self.fail_start: BackendError | None = None
        self.listed = [run_info("old", "finished", k_eff=1.0, k_std=0.001, has_results=True)]
        self.log = "line 1\nline 2\n"
        self.log_offsets: list[int] = []
        self.active_calls = 0
        FakeRunApi.instances.append(self)

    def start_run(self, request: RunRequest) -> RunInfo:
        if self.fail_start is not None:
            raise self.fail_start
        self.started.append(request)
        return run_info()

    def stop_run(self, run_id: str) -> RunInfo:
        self.stopped.append(run_id)
        return run_info()

    def runs(self, model: str) -> list[RunInfo]:
        return list(self.listed)

    def active_run(self) -> RunStatus | None:
        self.active_calls += 1
        return self.active

    def run_status(self, run_id: str) -> RunStatus:
        self.status_calls.append(run_id)
        return RunStatus(run=run_info(run_id), progress=RunProgress(k=[1.0, 1.1, 1.2], batch=3))

    def run_log(self, run_id: str, offset: int = 0) -> LogChunk:
        self.log_offsets.append(offset)
        data = self.log.encode()[offset:]
        return LogChunk(
            offset=offset,
            text=data.decode(),
            next=len(self.log.encode()),
            size=len(self.log.encode()),
        )

    def close(self) -> None:
        pass


@pytest.fixture
def session(qtbot, monkeypatch):
    FakeRunApi.instances.clear()
    monkeypatch.setattr(run_session, "ApiClient", FakeRunApi)
    monkeypatch.setattr(run_session.EventStream, "start", lambda self, connection: None)
    tasks = TaskRunner()
    s = RunSession(tasks)
    s.set_connection(Connection("http://127.0.0.1:1", "t"))
    yield s
    s.shutdown()
    tasks.shutdown()


def api() -> FakeRunApi:
    return FakeRunApi.instances[-1]


def progress_event(run_id: str, first: int, k: list[float], **kwargs) -> Event:
    update = RunProgress(
        first=first,
        k=k,
        entropy=[3.0] * len(k),
        mean=[None] * len(k),
        std=[None] * len(k),
        **kwargs,
    )
    return Event(type="run.progress", run_id=run_id, data=update.model_dump(mode="json"))


def test_merge_progress():
    progress = RunProgress()
    assert merge_progress(
        progress, RunProgress(first=0, k=[1.0, 1.1], mean=[None, None], std=[None, None], batch=2)
    )
    assert merge_progress(
        progress, RunProgress(first=1, k=[1.15, 1.2], mean=[None, 1.1], std=[None, 0.1], batch=3)
    )
    assert progress.k == [1.0, 1.15, 1.2] and progress.mean == [None, None, 1.1]
    assert progress.batch == 3
    assert not merge_progress(progress, RunProgress(first=7, k=[1.0])), "a gap"


def test_start_follow_and_finish(qtbot, session: RunSession):
    session.set_model("hw/model.py")
    qtbot.waitUntil(lambda: [r.id for r in session.history] == ["old"], timeout=3000)
    request = RunRequest(path="hw/model.py", params={"rod": 1.0})
    with qtbot.waitSignal(session.currentChanged, timeout=3000):
        assert session.start(request)
    assert session.running and api().started == [request]
    assert session.current is not None and session.current.id == "r1"
    qtbot.waitUntil(lambda: session.current_log == "line 1\nline 2\n", timeout=3000)

    session.events.received.emit(progress_event("r1", 0, [1.0, 1.1], batch=2))
    session.events.received.emit(progress_event("r1", 2, [1.2], batch=3))
    assert session.progress.k == [1.0, 1.1, 1.2] and session.progress.batch == 3

    offset = len(b"line 1\nline 2\n")
    appended: list[str] = []
    session.logAppended.connect(appended.append)
    session.events.received.emit(
        Event(type="run.log", run_id="r1", data={"text": "line 3\n", "offset": offset})
    )
    assert appended == ["line 3\n"] and session.current_log.endswith("line 3\n")

    assert session.stop()
    qtbot.waitUntil(lambda: api().stopped == ["r1"], timeout=3000)

    done = run_info("r1", "finished", stop_reason="completed", has_results=True)
    with qtbot.waitSignal(session.finished, timeout=3000) as blocker:
        session.events.received.emit(
            Event(type="run.finished", run_id="r1", data=done.model_dump(mode="json"))
        )
    assert blocker.args[0].state == "finished"
    assert not session.running


def test_a_missed_event_fetches_the_whole_state(qtbot, session: RunSession):
    session.events.received.emit(
        Event(type="run.state", run_id="r1", data=run_info().model_dump(mode="json"))
    )
    session.events.received.emit(progress_event("r1", 5, [1.3]))
    qtbot.waitUntil(lambda: api().status_calls == ["r1"], timeout=3000)
    qtbot.waitUntil(lambda: session.progress.k == [1.0, 1.1, 1.2], timeout=3000)


def test_status_requests_are_not_repeated_while_one_is_open(qtbot, session: RunSession):
    session.events.received.emit(
        Event(type="run.state", run_id="r1", data=run_info().model_dump(mode="json"))
    )
    session.events.received.emit(progress_event("r1", 5, [1.3]))
    session.events.received.emit(progress_event("r1", 6, [1.4]))
    qtbot.waitUntil(lambda: session.progress.k == [1.0, 1.1, 1.2], timeout=3000)
    assert api().status_calls == ["r1"]


def test_a_late_answer_does_not_revive_an_ended_run(qtbot, session: RunSession):
    session.events.received.emit(
        Event(type="run.state", run_id="r1", data=run_info().model_dump(mode="json"))
    )
    done = run_info("r1", "stopped", stop_reason="user", has_results=True)
    session.events.received.emit(
        Event(type="run.finished", run_id="r1", data=done.model_dump(mode="json"))
    )
    api().active = RunStatus(run=run_info("r1"), progress=RunProgress(k=[0.9], batch=1))
    session.events.connected.emit(True)  # the answer was made before the run ended
    qtbot.waitUntil(lambda: api().active_calls == 1, timeout=3000)
    qtbot.wait(200)
    assert session.current is not None and session.current.state == "stopped"
    assert not session.running


def test_the_log_is_read_on_from_where_it_stops(qtbot, session: RunSession):
    session.events.received.emit(
        Event(type="run.state", run_id="r1", data=run_info().model_dump(mode="json"))
    )
    qtbot.waitUntil(lambda: session.current_log == "line 1\nline 2\n", timeout=3000)
    start = len(b"line 1\nline 2\n")
    api().log += "line 3\nline 4\n"
    session.events.received.emit(  # "line 3" was missed
        Event(type="run.log", run_id="r1", data={"text": "line 4\n", "offset": start + 7})
    )
    qtbot.waitUntil(lambda: session.current_log.endswith("line 3\nline 4\n"), timeout=3000)
    assert api().log_offsets[-1] == start, "only the missing part is read"
    appended: list[str] = []
    session.logAppended.connect(appended.append)
    session.events.received.emit(  # overlaps what the session has
        Event(type="run.log", run_id="r1", data={"text": "line 4\nline 5\n", "offset": start + 7})
    )
    assert appended == ["line 5\n"]
    assert session.current_log == "line 1\nline 2\nline 3\nline 4\nline 5\n"


def test_the_history_is_polled_while_another_backend_runs(qtbot, session: RunSession):
    api().listed = [run_info("x1", "running")]  # nobody sends events about it
    session.set_model("hw/model.py")
    qtbot.waitUntil(lambda: [r.id for r in session.history] == ["x1"], timeout=3000)
    assert session._history_timer.isActive()
    api().listed = [run_info("x1", "finished")]
    session.refresh_history()
    qtbot.waitUntil(lambda: not session.history[0].active, timeout=3000)
    assert not session._history_timer.isActive()


def test_reconnecting_follows_the_active_run(qtbot, session: RunSession):
    api().active = RunStatus(run=run_info("r9"), progress=RunProgress(k=[0.9], batch=1))
    with qtbot.waitSignal(session.currentChanged, timeout=3000):
        session.events.connected.emit(True)
    assert session.current is not None and session.current.id == "r9"
    assert session.progress.k == [0.9]


def test_start_failures_are_explained(qtbot, session: RunSession):
    api().fail_start = BackendError("run_active: busy", "run_active", "Another run")
    with qtbot.waitSignal(session.startFailed, timeout=3000) as blocker:
        session.start(RunRequest(path="hw/model.py"))
    assert blocker.args == ["Another simulation is running.", "Another run"]
    assert not session.running


def test_progress_from_results_rebuilds_the_running_mean():
    from pyvised.core.run_session import progress_from_results
    from pyvised_protocol.runs import RunResults

    results = RunResults(
        run_id="r",
        statepoint="statepoint.6.h5",
        run_mode="eigenvalue",
        batches=6,
        inactive=2,
        k_generation=[0.9, 1.1, 1.0, 1.2, 1.1, 1.3],
        entropy=[3.0] * 6,
        runtime={"total": 12.0},
        rate_active=1000.0,
    )
    progress = progress_from_results(results)
    assert progress.mean[:3] == [None, None, None], "no σ from a single active generation"
    assert progress.mean[3] == pytest.approx(1.1)
    assert progress.std[3] == pytest.approx(0.1)
    assert progress.mean[5] == pytest.approx(1.15)
    assert progress.std[5] == pytest.approx(0.0645497, rel=1e-5)
    assert (progress.batch, progress.inactive, progress.statepoint) == (6, 2, 6)
