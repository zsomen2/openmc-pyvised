"""The API client's run calls and the event stream, against a mocked backend (respx)."""

import json
from datetime import datetime

import httpx
import numpy as np
import pytest
import respx

from pyvised.backend.client import ApiClient, BackendError, Connection
from pyvised.backend.events import EventStream
from pyvised_protocol import API_PREFIX, frames
from pyvised_protocol.runs import (
    Event,
    RunInfo,
    RunRequest,
    RunResults,
    RunSettings,
    TallyResult,
    format_sse,
)

BASE = "http://127.0.0.1:50001"
URL = BASE + API_PREFIX
INFO = RunInfo(
    id="aHcvcnVucy94",
    path="hw/runs/x",
    model="hw/model.py",
    state="running",
    created=datetime(2026, 10, 26, 12, 0),
)


@pytest.fixture
def api():
    with ApiClient(Connection(BASE, "secret")) as client:
        yield client


@respx.mock
def test_start_list_stop_and_delete(api: ApiClient):
    start = respx.post(f"{URL}/runs").mock(
        return_value=httpx.Response(201, json=INFO.model_dump(mode="json"))
    )
    request = RunRequest(path="hw/model.py", params={"rod": 1}, settings=RunSettings(batches=10))
    assert api.start_run(request) == INFO
    body = json.loads(start.calls.last.request.content)
    assert body["settings"]["batches"] == 10 and body["params"] == {"rod": 1}
    listed = respx.get(f"{URL}/runs").mock(
        return_value=httpx.Response(200, json=[INFO.model_dump(mode="json")])
    )
    assert api.runs("hw/model.py") == [INFO]
    assert listed.calls.last.request.url.params["model"] == "hw/model.py"
    respx.post(f"{URL}/runs/{INFO.id}/stop").mock(
        return_value=httpx.Response(200, json=INFO.model_dump(mode="json"))
    )
    assert api.stop_run(INFO.id).id == INFO.id
    respx.delete(f"{URL}/runs/{INFO.id}").mock(return_value=httpx.Response(204))
    api.delete_run(INFO.id)
    respx.get(f"{URL}/runs/active").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    assert api.active_run() is None


@respx.mock
def test_a_busy_backend_is_reported(api: ApiClient):
    respx.post(f"{URL}/runs").mock(
        return_value=httpx.Response(409, json={"error": "run_active", "detail": "stop it first"})
    )
    with pytest.raises(BackendError) as caught:
        api.start_run(RunRequest(path="hw/model.py"))
    assert caught.value.code == "run_active" and caught.value.detail == "stop it first"


@respx.mock
def test_results_and_tally_arrays(api: ApiClient):
    results = RunResults(
        run_id=INFO.id, statepoint="statepoint.10.h5", run_mode="eigenvalue", batches=10
    )
    respx.get(f"{URL}/runs/{INFO.id}/results").mock(
        return_value=httpx.Response(200, json=results.model_dump(mode="json"))
    )
    assert api.run_results(INFO.id).statepoint == "statepoint.10.h5"
    tally = TallyResult(id=4, scores=["flux"], nuclides=["total"], shape=[2, 1, 1])
    frame = frames.encode(
        {"mean": np.ones((2, 1, 1)), "std": np.full((2, 1, 1), 0.1)},
        {"tally": tally.model_dump(), "statepoint": "statepoint.10.h5"},
    )
    respx.get(f"{URL}/runs/{INFO.id}/tallies/4").mock(
        return_value=httpx.Response(200, content=frame)
    )
    data = api.tally_data(INFO.id, 4)
    assert data.tally == tally and data.mean.shape == (2, 1, 1) and data.std[1, 0, 0] == 0.1


@respx.mock
def test_the_event_stream_delivers_events(qtbot):
    events = [
        Event(type="run.state", run_id="r1", data={"x": 1}),
        Event(type="run.log", run_id="r1", data={"text": "a\nb\n", "offset": 0}),
    ]
    body = b": connected\n\n" + b"".join(format_sse(e) for e in events)
    respx.get(f"{URL}/events").mock(
        return_value=httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )
    )
    stream = EventStream()
    received: list[Event] = []
    stream.received.connect(received.append)
    with qtbot.waitSignal(stream.connected, timeout=3000) as connected:
        stream.start(Connection(BASE, "secret"))
    assert connected.args == [True]
    qtbot.waitUntil(lambda: len(received) == 2, timeout=3000)
    stream.stop()
    assert received == events
    assert not stream.running
