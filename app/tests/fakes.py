"""Test doubles for the geometry views: a small model and slices computed from the request.

The synthetic model fills -10 < x, y, z < 10 cm: cell 1 (fuel, material 1) for
x < 0, cell 2 (water, material 2) for x >= 0 and y < 5, and the void cell 3 for
x >= 0 and y >= 5. Everything else is outside the geometry. Its layers: the
cells at level 0 are the deepest cells, the temperature is 293.6 K, and the
weight-window lower bound is 0.5 for x < 0 and 0.125 for x >= 0.
"""

from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QTimer

from pyvised.render.views import BASES
from pyvised_protocol.schemas import (
    BoundingBox,
    CellInfo,
    InspectResult,
    MaterialInfo,
    ModelInfo,
    PathStep,
    SliceRequest,
    UniverseInfo,
    WeightWindowInfo,
)
from pyvised_protocol.sidecar import LocalizedText, Sidecar, update_sidecar
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
        weight_windows=[
            WeightWindowInfo(
                id=1,
                mesh_id=1,
                mesh_kind="regular",
                energy_bounds=[0.0, 2.0e7],
                lower_min=0.125,
                lower_max=0.5,
            )
        ],
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
    data = SliceData(cells=cells, instances=instances, materials=materials, meta={})
    if request.level is not None:
        data.level_cells, data.level_instances = cells.copy(), instances.copy()
    if request.properties:
        data.temperature = np.where(inside, 293.6, -2.0).astype(np.float32)
    if request.weight_windows is not None:
        data.ww_lower = np.where(inside, np.where(x < 0, 0.5, 0.125), np.nan).astype(np.float32)
    return data


class FakeSession(QObject):
    """Stands in for `ModelSession` in the views: answers slice requests asynchronously."""

    def __init__(self, folder: Path | None = None) -> None:
        super().__init__()
        self.generation = 1
        self.requests: list[SliceRequest] = []
        self.inspected: list[tuple[float, float, float]] = []
        self.fail = False
        self.info: ModelInfo | None = None
        self.folder = folder
        self.path = folder / "model.py" if folder is not None else None

    def request_slice(self, request, on_result, on_error) -> bool:
        self.requests.append(request)
        if self.fail:
            QTimer.singleShot(0, lambda: on_error(RuntimeError("worker died")))
        else:
            QTimer.singleShot(0, lambda: on_result(synthetic_slice(request)))
        return True

    def inspect(self, point, on_result, on_error) -> bool:
        self.inspected.append(point)
        result = InspectResult(
            point=point,
            found=True,
            path=[PathStep(level=0, universe=0, cell=1, cell_name="fuel", local=point)],
            material=1,
            material_name="fuel",
            density=10.0,
        )
        QTimer.singleShot(0, lambda: on_result(result))
        return True

    def update_sidecar(self, changes) -> None:
        assert self.folder is not None and self.info is not None
        self.info.sidecar = update_sidecar(self.folder, changes)


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
