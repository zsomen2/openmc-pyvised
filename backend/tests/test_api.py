from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pyvised_backend import server
from pyvised_backend.config import Config
from pyvised_backend.errors import ServiceError
from pyvised_backend.models import OpenedModel
from pyvised_protocol import API_PREFIX, PROTOCOL_VERSION, frames
from pyvised_protocol.schemas import (
    BoundingBox,
    HealthInfo,
    InspectResult,
    ModelInfo,
    SelfTestResult,
    SelfTestStep,
)
from pyvised_protocol.slices import decode_slice, encode_slice
from pyvised_protocol.tracks import TrackData, TrackSummary, decode_tracks, encode_tracks

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeModels:
    def __init__(self, folder: Path):
        info = ModelInfo(
            model_id="abc",
            source="m.py",
            kind="python",
            bounding_box=BoundingBox(lower_left=(-1, -1, -1), upper_right=(1, 1, 1)),
            root_universe=0,
        )
        self.model = OpenedModel(info, folder)
        self.requests = []

    def open(self, request):
        self.requests.append(request)
        if request.path == "broken.py":
            raise ServiceError("build_failed", "Traceback: boom")
        return self.model

    def get(self, model_id):
        if model_id != "abc":
            raise ServiceError("unknown_model", model_id, status=404)
        return self.model


class FakeRenderer:
    def __init__(self):
        self.preloaded = []
        self.inspected = []
        self.started = self.shut_down = False

    def start(self):
        self.started = True

    def preload(self, folder):
        self.preloaded.append(folder)

    def render(self, folder, request, meta):
        h, v = request.pixels
        ids = np.zeros((v, h, 3), dtype=np.int32)
        ids[..., 0] = 7
        return encode_slice(ids, None, meta)

    def inspect(self, folder, point):
        self.inspected.append(point)
        return InspectResult(point=point, found=False)

    def shutdown(self):
        self.shut_down = True


class FakeTracks:
    def __init__(self):
        self.requests = []
        self.cancelled = 0

    def generate(self, model_id, request):
        if model_id != "abc":
            raise ServiceError("unknown_model", model_id, status=404)
        self.requests.append(request)
        one = np.zeros(1)
        data = TrackData(
            position=np.zeros((1, 3)),
            energy=one,
            time=one,
            weight=one,
            cell=one,
            instance=one,
            material=one,
            event=one,
            step=one,
            track_start=np.array([0, 1]),
            track_history=np.zeros(1),
            track_particle=np.array([2112]),
            track_parent=np.array([-1]),
            history_id=np.array([1]),
            summary=TrackSummary(model_id=model_id, histories=1, seed=request.seed or 5),
        )
        return encode_tracks(data)

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def fakes(tmp_path: Path):
    return FakeModels(tmp_path), FakeRenderer()


@pytest.fixture
def tracks() -> FakeTracks:
    return FakeTracks()


@pytest.fixture
def client(tmp_path: Path, fakes, tracks) -> TestClient:
    config = Config(
        token=TOKEN, workspace=tmp_path, cross_sections=None, custom_data=tmp_path / "custom"
    )
    models, renderer = fakes
    app = server.create_app(config, models=models, renderer=renderer, tracks=tracks)
    return TestClient(app)


def test_health_needs_no_token(client: TestClient):
    response = client.get(f"{API_PREFIX}/health")
    assert response.status_code == 200
    health = HealthInfo.model_validate(response.json())
    assert health.protocol_version == PROTOCOL_VERSION
    assert {"selftest", "models", "slices", "inspect"} <= set(health.capabilities)


@pytest.mark.parametrize("header", [None, "Bearer wrong", TOKEN])
@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/selftest"), ("post", "/models/open"), ("get", "/models/abc")],
)
def test_private_endpoints_reject_missing_or_wrong_token(
    client: TestClient, header: str | None, method: str, path: str
):
    headers = {"Authorization": header} if header else {}
    response = client.request(method, f"{API_PREFIX}{path}", headers=headers, json={"path": "m"})
    assert response.status_code == 401


def test_selftest_with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    fake = SelfTestResult(ok=True, steps=[SelfTestStep(name="build", ok=True, seconds=0.1)])
    monkeypatch.setattr(server, "run_selftest_subprocess", lambda: fake)
    response = client.post(f"{API_PREFIX}/selftest", headers=AUTH)
    assert response.status_code == 200
    assert SelfTestResult.model_validate(response.json()) == fake


def test_open_model_preloads_it_and_returns_the_info(client: TestClient, fakes):
    models, renderer = fakes
    body = {"path": "m.py", "params": {"pitch": 1.5}}
    response = client.post(f"{API_PREFIX}/models/open", headers=AUTH, json=body)
    assert response.status_code == 200
    assert ModelInfo.model_validate(response.json()).model_id == "abc"
    assert models.requests[0].params == {"pitch": 1.5}
    assert renderer.preloaded == [models.model.directory]
    assert client.get(f"{API_PREFIX}/models/abc", headers=AUTH).json()["model_id"] == "abc"


def test_service_errors_become_error_info(client: TestClient):
    response = client.post(f"{API_PREFIX}/models/open", headers=AUTH, json={"path": "broken.py"})
    assert response.status_code == 422
    assert response.json() == {"error": "build_failed", "detail": "Traceback: boom"}
    response = client.get(f"{API_PREFIX}/models/nope", headers=AUTH)
    assert response.status_code == 404 and response.json()["error"] == "unknown_model"


def test_slice_answers_with_a_frame(client: TestClient):
    body = {"basis": "xz", "origin": [0, 0.5, 0], "width": [2, 1], "pixels": [40, 20]}
    response = client.post(f"{API_PREFIX}/models/abc/slice", headers=AUTH, json=body)
    assert response.status_code == 200
    assert response.headers["content-type"] == frames.CONTENT_TYPE
    data = decode_slice(response.content)
    assert data.shape == (20, 40) and int(data.cells[0, 0]) == 7
    assert data.meta["model_id"] == "abc" and data.meta["basis"] == "xz"


@pytest.mark.parametrize(
    "body",
    [
        {"origin": [0, 0, 0], "width": [1, 1], "pixels": [0, 10]},
        {"origin": [0, 0, 0], "width": [1, 1], "pixels": [10, 100_000]},
        {"origin": [0, 0, 0], "width": [-1, 1], "pixels": [10, 10]},
        {"origin": [0, 0, 0], "width": [1, 1], "pixels": [10, 10], "basis": "xx"},
    ],
)
def test_invalid_slice_requests_are_rejected(client: TestClient, body):
    response = client.post(f"{API_PREFIX}/models/abc/slice", headers=AUTH, json=body)
    assert response.status_code == 422


def test_renderer_starts_and_stops_with_the_app(tmp_path: Path, fakes):
    config = Config(token=TOKEN, workspace=tmp_path, cross_sections=None, custom_data=tmp_path)
    models, renderer = fakes
    with TestClient(server.create_app(config, models=models, renderer=renderer)):
        assert renderer.started
    assert renderer.shut_down


def test_inspect_asks_the_renderer(client: TestClient, fakes):
    _, renderer = fakes
    query = {"x": 1.5, "y": -2, "z": 0}
    response = client.get(f"{API_PREFIX}/models/abc/inspect", headers=AUTH, params=query)
    assert response.status_code == 200
    assert InspectResult.model_validate(response.json()).point == (1.5, -2.0, 0.0)
    assert renderer.inspected == [(1.5, -2.0, 0.0)]
    response = client.get(f"{API_PREFIX}/models/abc/inspect", headers=AUTH, params={"x": 1})
    assert response.status_code == 422


def test_tracks_answer_with_a_frame(client: TestClient, tracks: FakeTracks):
    body = {"histories": 20, "start": "point", "point": {"position": [1, 2, 3]}, "seed": 4}
    response = client.post(f"{API_PREFIX}/models/abc/tracks", headers=AUTH, json=body)
    assert response.status_code == 200
    assert response.headers["content-type"] == frames.CONTENT_TYPE
    data = decode_tracks(response.content)
    assert data.summary.model_id == "abc" and data.summary.seed == 4
    assert tracks.requests[0].point.position == (1.0, 2.0, 3.0)
    assert client.post(f"{API_PREFIX}/models/x/tracks", headers=AUTH, json={}).status_code == 404
    response = client.post(f"{API_PREFIX}/models/abc/tracks", headers=AUTH, json={"histories": 0})
    assert response.status_code == 422
    assert client.post(f"{API_PREFIX}/models/abc/tracks", json={}).status_code == 401


def test_tracks_cancel(client: TestClient, tracks: FakeTracks):
    response = client.delete(f"{API_PREFIX}/models/abc/tracks", headers=AUTH)
    assert response.status_code == 204 and tracks.cancelled == 1
    health = client.get(f"{API_PREFIX}/health").json()
    assert "tracks" in health["capabilities"]
