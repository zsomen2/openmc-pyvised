"""Test doubles for the geometry views: a small model and slices computed from the request.

The synthetic model fills -10 < x, y, z < 10 cm: cell 1 (fuel, material 1) for
x < 0, cell 2 (water, material 2) for x >= 0 and y < 5, and the void cell 3 for
x >= 0 and y >= 5. Everything else is outside the geometry.
"""

import numpy as np
from PySide6.QtCore import QObject, QTimer

from pyvised.render.views import BASES
from pyvised_protocol.schemas import (
    BoundingBox,
    CellInfo,
    MaterialInfo,
    ModelInfo,
    SliceRequest,
    UniverseInfo,
)
from pyvised_protocol.sidecar import LocalizedText, Sidecar
from pyvised_protocol.slices import OUTSIDE, VOID, SliceData


def model_info(source: str = "test/model.py", model_id: str = "fake1") -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        source=source,
        kind="python",
        sidecar=Sidecar(title=LocalizedText(en="Test model", hu="Tesztmodell")),
        bounding_box=BoundingBox(lower_left=(-10, -10, -10), upper_right=(10, 10, 10)),
        root_universe=0,
        materials=[
            MaterialInfo(id=1, name="fuel", mass_density=10.0),
            MaterialInfo(id=2, name="water", mass_density=1.0),
        ],
        cells=[
            CellInfo(id=1, name="fuel", universe=0, fill_type="material", fill=1),
            CellInfo(id=2, name="water", universe=0, fill_type="material", fill=2),
            CellInfo(id=3, name="gap", universe=0, fill_type="void"),
        ],
        universes=[UniverseInfo(id=0, cells=[1, 2, 3])],
        builder_output="exported\n",
    )


def synthetic_slice(request: SliceRequest) -> SliceData:
    h_axis, v_axis, _ = BASES[request.basis]
    cols, rows = request.pixels
    width, height = request.width
    u = request.origin[h_axis] - width / 2 + (np.arange(cols) + 0.5) * width / cols
    v = request.origin[v_axis] + height / 2 - (np.arange(rows) + 0.5) * height / rows
    point = np.zeros((3, rows, cols))
    point[h_axis], point[v_axis] = np.meshgrid(u, v)
    point[3 - h_axis - v_axis] = request.origin[3 - h_axis - v_axis]
    x, y = point[0], point[1]
    inside = (np.abs(point) < 10).all(axis=0)
    cells = np.where(x < 0, 1, np.where(y < 5, 2, 3))
    materials = np.where(x < 0, 1, np.where(y < 5, 2, VOID))
    cells = np.where(inside, cells, OUTSIDE).astype(np.int32)
    materials = np.where(inside, materials, OUTSIDE).astype(np.int32)
    instances = np.where(inside, 0, OUTSIDE).astype(np.int32)
    return SliceData(cells=cells, instances=instances, materials=materials, meta={})


class FakeSession(QObject):
    """Stands in for `ModelSession` in the views: answers slice requests asynchronously."""

    def __init__(self) -> None:
        super().__init__()
        self.generation = 1
        self.requests: list[SliceRequest] = []
        self.fail = False

    def request_slice(self, request, on_result, on_error) -> bool:
        self.requests.append(request)
        if self.fail:
            QTimer.singleShot(0, lambda: on_error(RuntimeError("worker died")))
        else:
            QTimer.singleShot(0, lambda: on_result(synthetic_slice(request)))
        return True


class FakeApi:
    """Stands in for `ApiClient` in `ModelSession`."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def open_model(self, request) -> ModelInfo:
        return model_info(source=request.path)

    def slice(self, model_id, request) -> SliceData:
        return synthetic_slice(request)

    def close(self) -> None:
        pass
