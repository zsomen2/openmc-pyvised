import json

import httpx
import numpy as np
import pytest
import respx

from pyvised.backend.client import ApiClient, BackendError, Connection
from pyvised_protocol import API_PREFIX, frames
from pyvised_protocol.schemas import BoundingBox, ModelInfo, OpenModelRequest, SliceRequest
from pyvised_protocol.slices import encode_slice

BASE = "http://127.0.0.1:50000"
URL = BASE + API_PREFIX

INFO = ModelInfo(
    model_id="m1",
    source="hw/model.py",
    kind="python",
    bounding_box=BoundingBox(lower_left=(-1, -1, -1), upper_right=(1, 1, 1)),
    root_universe=0,
)


@pytest.fixture
def api():
    with ApiClient(Connection(BASE, "secret")) as client:
        yield client


@respx.mock
def test_open_model_sends_the_request_and_token(api: ApiClient):
    route = respx.post(f"{URL}/models/open").mock(
        return_value=httpx.Response(200, json=INFO.model_dump(mode="json"))
    )
    info = api.open_model(OpenModelRequest(path="hw/model.py", params={"rods": 2}))
    assert info == INFO
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"
    assert json.loads(sent.content) == {
        "path": "hw/model.py",
        "library": None,
        "params": {"rods": 2},
    }


@respx.mock
def test_slice_decodes_the_frame(api: ApiClient):
    ids = np.zeros((3, 4, 3), dtype=np.int32)
    ids[..., 0], ids[..., 1], ids[..., 2] = 5, 6, 7
    respx.post(f"{URL}/models/m1/slice").mock(
        return_value=httpx.Response(
            200,
            content=encode_slice(ids, None, {"render_seconds": 0.01}),
            headers={"content-type": frames.CONTENT_TYPE},
        )
    )
    request = SliceRequest(origin=(0, 0, 0), width=(1, 1), pixels=(4, 3))
    data = api.slice("m1", request)
    assert data.shape == (3, 4)
    assert (int(data.cells[0, 0]), int(data.instances[1, 1]), int(data.materials[2, 3])) == (
        5,
        6,
        7,
    )
    assert data.temperature is None and data.meta["render_seconds"] == 0.01


@respx.mock
def test_error_info_becomes_a_backend_error(api: ApiClient):
    respx.post(f"{URL}/models/open").mock(
        return_value=httpx.Response(
            422, json={"error": "build_failed", "detail": "Traceback ...\nNameError: x"}
        )
    )
    with pytest.raises(BackendError) as raised:
        api.open_model(OpenModelRequest(path="bad.py"))
    assert raised.value.code == "build_failed"
    assert raised.value.detail.endswith("NameError: x")


@respx.mock
def test_other_failures_are_backend_errors_too(api: ApiClient):
    respx.post(f"{URL}/models/m1/slice").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(BackendError, match="500"):
        api.slice("m1", SliceRequest(origin=(0, 0, 0), width=(1, 1), pixels=(1, 1)))
    respx.get(f"{URL}/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BackendError, match="no answer"):
        api.health()
