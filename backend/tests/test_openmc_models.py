"""Model loading and slices with real OpenMC: only inside the backend container."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openmc")

from fastapi.testclient import TestClient

from pyvised_backend import server
from pyvised_backend.config import Config
from pyvised_backend.errors import ServiceError
from pyvised_backend.models import ModelService
from pyvised_backend.render import RenderError, RenderPool
from pyvised_protocol import API_PREFIX
from pyvised_protocol.schemas import ModelInfo, OpenModelRequest, SliceRequest
from pyvised_protocol.slices import OUTSIDE, VOID, decode_slice

pytestmark = pytest.mark.openmc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_ids():
    """Tests build models with the same ids; OpenMC would warn about each reuse."""
    import openmc

    openmc.reset_auto_ids()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name in ("pins.py", "sphere.ipynb"):
        shutil.copy(FIXTURES / name, ws / name)
    return ws


@pytest.fixture
def config(tmp_path: Path, workspace: Path) -> Config:
    xs = os.environ.get("OPENMC_CROSS_SECTIONS")
    return Config(
        token="t",
        workspace=workspace,
        cross_sections=Path(xs) if xs else None,
        custom_data=tmp_path / "custom",
        cache_dir=tmp_path / "cache",
        render_workers=1,
    )


@pytest.fixture(scope="module")
def pool(tmp_path_factory: pytest.TempPathFactory):
    pool = RenderPool(1, tmp_path_factory.mktemp("logs"))
    yield pool
    pool.shutdown()


def test_builder_is_exported_with_parameters_and_described(config: Config):
    opened = ModelService(config).open(OpenModelRequest(path="pins.py", params={"pitch": 2.0}))
    info = opened.info
    assert info.kind == "python" and info.library == "builtin"
    assert "Model.run() skipped" in info.builder_output
    assert not list(opened.directory.glob("statepoint*")), "the simulation must not run"

    cells = {c.id: c for c in info.cells}
    assert (cells[10].fill_type, cells[10].fill) == ("lattice", 5)
    assert cells[11].fill_type == "void"
    assert (cells[1].fill_type, cells[1].fill, cells[1].universe) == ("material", 1, 1)
    assert cells[1].region == "-1"
    (lattice,) = info.lattices
    assert (lattice.id, lattice.pitch, lattice.shape) == (5, [2.0, 2.0], [2, 2])
    assert (lattice.universes, lattice.parent_cells) == ([1], [10])
    pin = next(u for u in info.universes if u.id == 1)
    assert (pin.cells, pin.parent_lattices) == ([1, 2], [5])  # names do not survive the XML
    assert info.root_universe not in (1, 5)
    box = info.bounding_box
    assert box.lower_left == (-2.0, -2.0, -10.0) and box.upper_right == (3.0, 2.0, 10.0)

    water = next(m for m in info.materials if m.id == 2)
    assert water.name == "water" and water.mass_density == pytest.approx(1.0)
    assert [(n.name, n.fraction, n.kind) for n in water.nuclides] == [
        ("H1", 2.0, "ao"),
        ("O16", 1.0, "ao"),
    ]
    assert water.atom_density == pytest.approx(0.1003, rel=1e-3)
    surfaces = {s.id: s for s in info.surfaces}
    assert surfaces[1].type == "z-cylinder" and surfaces[1].coefficients["r"] == pytest.approx(0.6)
    assert surfaces[10].boundary == "vacuum" and surfaces[11].boundary == "transmission"
    assert (info.settings.run_mode, info.settings.particles) == ("eigenvalue", 100)
    assert info.settings.sources[0].space == "Point (0, 0, 0)"
    (tally,) = info.tallies
    assert (tally.id, tally.name, tally.scores) == (4, "fuel flux", ["flux"])
    assert tally.filters[0].kind == "CellFilter" and tally.filters[0].summary == "1"


def test_notebook_is_exported(config: Config):
    info = ModelService(config).open(OpenModelRequest(path="sphere.ipynb")).info
    assert info.kind == "notebook"
    assert [(c.id, c.name) for c in info.cells] == [(7, "water sphere")]
    assert "Model.run() skipped" in info.builder_output


def test_xml_models_as_one_file_and_as_separate_files(config: Config, workspace: Path):
    import openmc

    water = openmc.Material(material_id=1)
    water.add_nuclide("H1", 2.0)
    water.set_density("g/cm3", 1.0)
    sphere = openmc.Sphere(r=3.0, boundary_type="vacuum")
    model = openmc.Model(
        geometry=openmc.Geometry([openmc.Cell(cell_id=3, fill=water, region=-sphere)]),
        materials=openmc.Materials([water]),
    )
    model.settings.particles = 10
    model.settings.batches = 1
    model.export_to_model_xml(workspace / "one" / "model.xml")
    model.export_to_xml(workspace / "separate")
    service = ModelService(config)
    for path in ("one/model.xml", "separate/geometry.xml"):
        info = service.open(OpenModelRequest(path=path)).info
        assert info.kind == "xml" and [c.id for c in info.cells] == [3], path


def test_failing_builders_report_why(config: Config, workspace: Path):
    (workspace / "bad.py").write_text("import openmc\nraise RuntimeError('bad geometry')\n")
    (workspace / "silent.py").write_text("import openmc\nopenmc.Model()\n")
    service = ModelService(config)
    with pytest.raises(ServiceError) as raised:
        service.open(OpenModelRequest(path="bad.py"))
    assert raised.value.error == "build_failed" and "bad geometry" in raised.value.detail
    with pytest.raises(ServiceError) as raised:
        service.open(OpenModelRequest(path="silent.py"))
    assert raised.value.error == "no_export"


def test_slice_orientation_special_values_and_instances(config: Config, pool: RenderPool):
    opened = ModelService(config).open(OpenModelRequest(path="pins.py"))  # pitch 1
    # x from -1.5 to 2.5 and y from -2 to 2, 0.1 cm per pixel; row 0 is the top (y = +2)
    request = SliceRequest(basis="xy", origin=(0.5, 0.0, 0.0), width=(4.0, 4.0), pixels=(40, 40))
    data = decode_slice(pool.render(opened.directory, request, {"tag": "xy"}))
    assert data.shape == (40, 40) and data.meta["tag"] == "xy"
    assert data.meta["render_seconds"] > 0

    def at(x: float, y: float) -> tuple[int, int, int]:
        row, col = int((2.0 - y) / 0.1), int((x + 1.5) / 0.1)
        return (
            int(data.cells[row, col]),
            int(data.instances[row, col]),
            int(data.materials[row, col]),
        )

    assert at(-0.5, 0.5)[0] == 1 and at(-0.5, 0.5)[2] == 1  # fuel of the top-left pin
    assert at(-0.12, 0.88)[0] == 2  # water in the corner of that pin
    assert at(1.5, 0.0)[0] == 11 and at(1.5, 0.0)[2] == VOID
    assert at(0.0, 1.5) == (OUTSIDE, OUTSIDE, OUTSIDE)
    assert at(-1.3, 0.0) == (OUTSIDE, OUTSIDE, OUTSIDE)
    pins = {at(x, y)[1] for x in (-0.5, 0.5) for y in (-0.5, 0.5)}
    assert len(pins) == 4, "every lattice element is its own instance"

    # xz: row 0 is the top (+z); the model spans -10 < z < 10
    side = SliceRequest(basis="xz", origin=(0.0, 0.5, 0.0), width=(2.0, 24.0), pixels=(20, 24))
    data = decode_slice(pool.render(opened.directory, side, {}))
    assert data.cells[0, 10] == OUTSIDE and data.cells[-1, 10] == OUTSIDE
    assert data.cells[12, 5] == 1  # x = -0.45, y = 0.5: the fuel of a pin


def test_worker_survives_an_openmc_abort(config: Config, pool: RenderPool, tmp_path: Path):
    good = ModelService(config).open(OpenModelRequest(path="pins.py"))
    bad = tmp_path / "bad"
    bad.mkdir()
    text = (good.directory / "model.xml").read_text(encoding="utf-8")
    (bad / "model.xml").write_text(text.replace('material="1"', 'material="99"'), "utf-8")
    with pytest.raises(RenderError) as raised:
        pool.preload(bad)
    assert "99" in raised.value.detail
    request = SliceRequest(basis="xy", origin=(0.0, 0.0, 0.0), width=(2.0, 2.0), pixels=(8, 8))
    data = decode_slice(pool.render(good.directory, request, {}))
    assert set(np.unique(data.cells)) <= {1, 2}


def test_api_end_to_end(config: Config):
    headers = {"Authorization": "Bearer t"}
    with TestClient(server.create_app(config)) as client:
        response = client.post(
            f"{API_PREFIX}/models/open", headers=headers, json={"path": "pins.py"}
        )
        assert response.status_code == 200, response.text
        info = ModelInfo.model_validate(response.json())
        body = {"basis": "yz", "origin": [0, 0, 0], "width": [2, 2], "pixels": [16, 16]}
        response = client.post(
            f"{API_PREFIX}/models/{info.model_id}/slice", headers=headers, json=body
        )
        assert response.status_code == 200
        assert decode_slice(response.content).shape == (16, 16)


def test_universe_levels_and_unbounded_axes(config: Config, workspace: Path):
    import openmc

    info = ModelService(config).open(OpenModelRequest(path="pins.py")).info
    assert info.max_level == 1, "root cells, then the pins of lattice 5"

    # a tube along (1, 1, 0) is a general Cylinder without a bounding box: OpenMC's box is
    # infinite in z, so the extent comes from the vacuum planes
    water = openmc.Material(material_id=1)
    water.add_nuclide("H1", 2.0)
    water.set_density("g/cm3", 1.0)
    wall = openmc.ZCylinder(r=10.0, boundary_type="vacuum")
    top = openmc.ZPlane(z0=5.0, boundary_type="vacuum")
    bottom = openmc.ZPlane(z0=-7.0, boundary_type="vacuum")
    tube = openmc.Cylinder(x0=0.0, y0=0.0, z0=0.0, r=1.0, dx=1.0, dy=1.0, dz=0.0)
    inner = openmc.Cell(cell_id=2, region=-wall & -tube)
    outer = openmc.Cell(cell_id=1, fill=water, region=-wall & -top & +bottom & ~inner.region)
    model = openmc.Model(geometry=openmc.Geometry([outer, inner]), materials=[water])
    model.export_to_model_xml(workspace / "tube" / "model.xml")
    info = ModelService(config).open(OpenModelRequest(path="tube/model.xml")).info
    box = info.bounding_box
    assert box.lower_left == pytest.approx((-10.0, -10.0, -7.0))
    assert box.upper_right == pytest.approx((10.0, 10.0, 5.0))
    assert box.clipped == (False, False, False)


def test_level_layer_and_inspect(config: Config, pool: RenderPool):
    opened = ModelService(config).open(OpenModelRequest(path="pins.py"))  # pitch 1
    request = SliceRequest(
        basis="xy", origin=(0.5, 0.0, 0.0), width=(4.0, 4.0), pixels=(40, 40), level=0
    )
    data = decode_slice(pool.render(opened.directory, request, {}))
    assert data.level_cells is not None and data.level_instances is not None
    assert set(np.unique(data.cells)) == {OUTSIDE, 1, 2, 11}
    assert set(np.unique(data.level_cells)) == {OUTSIDE, 10, 11}, "lattice cell 10 at level 0"

    result = pool.inspect(opened.directory, (-0.5, 0.5, 0.0))  # centre of the top-left pin
    assert result.found and [(s.level, s.cell) for s in result.path] == [(0, 10), (1, 1)]
    assert result.path[0].lattice == 5 and result.path[0].lattice_index == [0, 1]
    assert result.path[1].local == pytest.approx((0.0, 0.0, 0.0))
    assert result.path[1].universe == 1 and result.path[1].cell_name == "fuel"
    assert (result.material, result.material_name) == (1, "fuel")
    assert result.density == pytest.approx(10.0) and result.instance is not None
    void = pool.inspect(opened.directory, (1.5, 0.0, 0.0))
    assert void.found and void.material is None and void.path[-1].cell == 11
    outside = pool.inspect(opened.directory, (0.0, 50.0, 0.0))
    assert not outside.found and outside.path == []


def test_weight_window_layer(config: Config, workspace: Path, pool: RenderPool):
    import openmc

    water = openmc.Material(material_id=1)
    water.add_nuclide("H1", 2.0)
    water.set_density("g/cm3", 1.0)
    sphere = openmc.Sphere(r=4.0, boundary_type="vacuum")
    model = openmc.Model(
        geometry=openmc.Geometry([openmc.Cell(cell_id=1, fill=water, region=-sphere)]),
        materials=[water],
    )
    mesh = openmc.RegularMesh(mesh_id=3)
    mesh.lower_left, mesh.upper_right, mesh.dimension = (-2, -2, -2), (2, 2, 2), (2, 1, 1)
    model.settings.weight_windows = [
        openmc.WeightWindows(mesh, [0.5, 0.125], upper_bound_ratio=5.0, energy_bounds=[0, 2e7])
    ]
    model.export_to_model_xml(workspace / "ww" / "model.xml")
    opened = ModelService(config).open(OpenModelRequest(path="ww/model.xml"))
    assert [w.mesh_id for w in opened.info.weight_windows] == [3]
    request = SliceRequest(
        basis="xy",
        origin=(0.0, 0.0, 0.0),
        width=(8.0, 8.0),
        pixels=(8, 8),
        weight_windows={"particle": "neutron"},
    )
    data = decode_slice(pool.render(opened.directory, request, {}))
    assert data.ww_lower is not None
    assert data.ww_lower[3, 2] == pytest.approx(0.5) and data.ww_lower[4, 5] == pytest.approx(0.125)
    assert np.isnan(data.ww_lower[0, 0]), "outside the mesh"
