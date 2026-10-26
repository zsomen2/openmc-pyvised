"""Run endpoints and the event stream through the HTTP API, with a fake openmc."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from run_doubles import manager_for

from pyvised_backend import server
from pyvised_backend.events import EventHub
from pyvised_protocol import API_PREFIX
from pyvised_protocol.runs import (
    FINAL_STATES,
    RunInfo,
    RunResults,
    RunStatus,
    parse_sse,
)

AUTH = {"Authorization": "Bearer t"}


class Renderer:
    def start(self): ...
    def preload(self, folder): ...
    def render(self, folder, request, meta):
        return b""

    def inspect(self, folder, point): ...
    def shutdown(self): ...


@pytest.fixture
def client(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "hw").mkdir(parents=True)
    (workspace / "hw" / "model.py").write_text("", encoding="utf-8")
    hub = EventHub()
    manager, _ = manager_for(workspace, tmp_path, hub=hub)
    app = server.create_app(
        manager.config, models=manager.models, renderer=Renderer(), runs=manager, hub=hub
    )  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client


def url(path: str) -> str:
    return f"{API_PREFIX}{path}"


def wait_final(client: TestClient, run_id: str) -> RunInfo:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = RunStatus.model_validate(client.get(url(f"/runs/{run_id}"), headers=AUTH).json())
        if status.run.state in FINAL_STATES:
            return status.run
        time.sleep(0.05)
    raise AssertionError("the run did not end")


def test_runs_need_the_token(client: TestClient):
    assert client.get(url("/runs"), params={"model": "hw/model.py"}).status_code == 401
    assert client.get(url("/events")).status_code == 401


def test_start_follow_and_read_a_run(client: TestClient):
    body = {"path": "hw/model.py", "label": "api", "settings": {"batches": 8, "particles": 50}}
    response = client.post(url("/runs"), json=body, headers=AUTH)
    assert response.status_code == 201, response.text
    info = RunInfo.model_validate(response.json())
    active = client.get(url("/runs/active"), headers=AUTH).json()
    assert active is None or active["run"]["id"] == info.id
    final = wait_final(client, info.id)
    assert client.get(url("/runs/active"), headers=AUTH).json() is None
    assert final.state == "finished" and final.has_results
    listed = client.get(url("/runs"), params={"model": "hw/model.py"}, headers=AUTH).json()
    assert [r["id"] for r in listed] == [info.id]
    results = RunResults.model_validate(
        client.get(url(f"/runs/{info.id}/results"), headers=AUTH).json()
    )
    assert results.statepoint == "statepoint.8.h5"
    log = client.get(url(f"/runs/{info.id}/log"), params={"offset": 0}, headers=AUTH).json()
    assert "Bat./Gen." in log["text"] and log["next"] == log["size"]
    again = client.post(url(f"/runs/{info.id}/stop"), headers=AUTH)
    assert again.status_code == 200 and again.json()["state"] == "finished"
    assert client.get(url(f"/runs/{info.id}/tallies/4"), headers=AUTH).status_code == 404
    assert client.delete(url(f"/runs/{info.id}"), headers=AUTH).status_code == 204
    assert client.get(url(f"/runs/{info.id}"), headers=AUTH).status_code == 404


def test_errors_of_run_requests(client: TestClient):
    missing = client.post(url("/runs"), json={"path": "hw/none.py"}, headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    bad = client.post(
        url("/runs"), json={"path": "hw/model.py", "settings": {"batches": 0}}, headers=AUTH
    )
    assert bad.status_code == 422
    outside = client.get(url("/runs"), params={"model": "../x.py"}, headers=AUTH)
    assert outside.status_code == 403
    assert client.get(url("/runs/xyz"), headers=AUTH).status_code == 404


def test_events_stream_the_run(client: TestClient):
    seen: list[str] = []
    with client.stream("GET", url("/events"), headers=AUTH) as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        lines = stream.iter_lines()
        assert next(lines) == ": connected"
        body = {"path": "hw/model.py", "settings": {"batches": 6, "particles": 50}}
        run_id = client.post(url("/runs"), json=body, headers=AUTH).json()["id"]
        for event in parse_sse(lines):
            assert event.run_id == run_id
            seen.append(event.type)
            if event.type == "run.finished":
                assert event.data["state"] == "finished"
                break
    assert seen[0] == "run.state"
    assert {"run.progress", "run.log"} <= set(seen)


def test_health_reports_run_capabilities(client: TestClient):
    health = client.get(url("/health")).json()
    assert {"runs", "events"} <= set(health["capabilities"])
    assert health["cpu_count"] == 4
    assert client.get(url("/libraries"), headers=AUTH).json() == []
