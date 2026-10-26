"""pyvised_course: the MCNP-style rebuild helpers, checked on real OpenMC geometry."""

import json
import math

import numpy as np
import pytest

openmc = pytest.importorskip("openmc")

from pyvised_course import (  # noqa: E402  (after the importorskip)
    Cells,
    Transform,
    Universes,
    density,
    lat1,
    parameter,
    strict_ids,
    trcl,
    weight_windows,
)

pytestmark = pytest.mark.openmc


@pytest.fixture(autouse=True)
def fresh_ids():
    openmc.reset_auto_ids()
    yield
    openmc.reset_auto_ids()


def water(material_id: int = 3) -> "openmc.Material":
    mat = openmc.Material(material_id=material_id, name="water")
    mat.add_nuclide("H1", 2.0)
    mat.add_nuclide("O16", 1.0)
    mat.set_density("g/cm3", 1.0)
    return mat


def cell_path(geometry, point) -> list[int]:
    """Ids of the cells from the root down to the point."""
    return [item.id for item in geometry.find(point) if isinstance(item, openmc.Cell)]


def test_parameter_reads_pyvised_params(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYVISED_PARAMS", raising=False)
    assert parameter("rod", 30.0) == 30.0
    monkeypatch.setenv("PYVISED_PARAMS", json.dumps({"rod": -12.5}))
    assert parameter("rod", 30.0) == -12.5
    assert parameter("other", 1) == 1


def test_cell_card_densities():
    mat = water()
    cell = openmc.Cell(fill=mat)
    density(cell, -1.5)
    assert cell.density == pytest.approx(1.5)
    density(cell, 0.1)  # atom/b-cm
    check = water(4)
    check.set_density("g/cm3", cell.density)
    assert sum(check.get_nuclide_atom_densities().values()) == pytest.approx(0.1, rel=1e-12)


def test_complement_of_a_cell_further_down_the_deck():
    s1 = openmc.Sphere(r=5.0)
    s2 = openmc.Sphere(r=2.0)
    c = Cells()
    c[1] = openmc.Cell(cell_id=1, region=-s1 & c.complement(2))  # #2 before cell 2 exists
    c[2] = openmc.Cell(cell_id=2, region=-s2)
    with pytest.raises(RuntimeError, match="#2"):
        str(c[1].region)
    c.resolve()
    assert (0.0, 0.0, 1.0) not in c[1].region and (0.0, 0.0, 3.0) in c[1].region
    assert c.complement(2) is not None and (0.0, 0.0, 1.0) not in c.complement(2)
    c[3] = openmc.Cell(cell_id=3, region=c.complement(99))
    with pytest.raises(KeyError, match="99"):
        c.resolve()


def test_transform_cards():
    # *TR with angles: x' and y' turned by 30° about z
    tr = Transform.card(0, 0, 0, 30, 60, 90, 120, 30, 90, 90, 90, 0, degrees=True)
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    assert tr.apply((1.0, 0.0, 0.0)) == pytest.approx([c, s, 0.0])
    assert tr.apply((0.0, 1.0, 0.0)) == pytest.approx([-s, c, 0.0])
    moved = Transform.card(1, 2, 3)
    assert moved.rotation is None and moved.apply((1.0, 1.0, 1.0)) == pytest.approx([2, 3, 4])
    # M = -1: the displacement is the main origin seen from the auxiliary system
    inverse = Transform.card(1, 2, 3, 0, 1, 0, -1, 0, 0, 0, 0, 1, -1)
    assert inverse.apply((1.0, 2.0, 3.0)) == pytest.approx([0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="orthonormal"):
        Transform.card(0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1)
    with pytest.raises(ValueError, match="entries"):
        Transform.card(0, 0)


def test_transformed_surfaces_keep_their_number():
    tr = Transform.card(5, -2, 0, 30, 60, 90, 120, 30, 90, 90, 90, 0, degrees=True)
    aux = openmc.XCylinder(y0=7.0, z0=0.0, r=2.0)  # a tube along x' in auxiliary coordinates
    tube = tr.surface(aux, 101)
    assert tube.id == 101
    rng = np.random.default_rng(1)
    for point in rng.uniform(-30, 30, size=(20, 3)):
        assert tube.evaluate(tr.apply(point)) == pytest.approx(aux.evaluate(point), abs=1e-9)
    plane = tr.surface(openmc.Plane(a=1.0, b=2.0, c=3.0, d=4.0), 7)
    for point in rng.uniform(-30, 30, size=(5, 3)):
        sign = np.sign(openmc.Plane(a=1.0, b=2.0, c=3.0, d=4.0).evaluate(point))
        assert np.sign(plane.evaluate(tr.apply(point))) == sign


def test_trcl_moves_the_surfaces_and_the_fill():
    inner = openmc.Universe(universe_id=10)
    pin = openmc.ZCylinder(surface_id=1, x0=1.0, y0=0.0, r=0.5)
    inner.add_cell(openmc.Cell(cell_id=11, fill=water(), region=-pin))
    inner.add_cell(openmc.Cell(cell_id=12, region=+pin))
    box = openmc.model.RectangularParallelepiped(-2, 2, -2, 2, -2, 2)
    holder = openmc.Cell(cell_id=20, fill=inner, region=-box)
    # trcl: rotate by +90° about z (x' = y, y' = -x), then move by (10, 0, 0)
    tr = Transform.card(10, 0, 0, 0, 1, 0, -1, 0, 0, 0, 0, 1)
    trcl(holder, tr)
    ids = {s.id for s in holder.region.get_surfaces().values()}
    assert all(i // 1000 == 20 for i in ids), ids
    assert holder.translation == pytest.approx([10.0, 0.0, 0.0])
    outside = openmc.Sphere(r=50.0, boundary_type="vacuum")
    rest = openmc.Cell(cell_id=21, region=-outside & ~holder.region)
    geometry = openmc.Geometry([holder, rest])
    for local, expected in [((1.0, 0.0, 0.0), 11), ((-1.0, 0.0, 0.0), 12), ((1.0, 1.9, 0), 12)]:
        assert cell_path(geometry, tr.apply(local)) == [20, expected], local
    assert cell_path(geometry, (12.5, 0.0, 0.0)) == [21], "the box moved with the cell"


def test_lat1_off_origin_elements_and_the_lattice_cells_own_material():
    u = Universes()
    mat = water()
    pin = openmc.ZCylinder(surface_id=1, x0=1.0, y0=1.0, r=0.5)  # element (0,0) is 0 < x, y < 2
    u[3].add_cell(openmc.Cell(cell_id=31, fill=mat, region=-pin))
    u[3].add_cell(openmc.Cell(cell_id=32, region=+pin))
    planes = (
        openmc.XPlane(surface_id=11, x0=2.0),  # across it: element (1, 0)
        openmc.XPlane(surface_id=12, x0=0.0),
        openmc.YPlane(surface_id=13, y0=2.0),
        openmc.YPlane(surface_id=14, y0=0.0),
    )
    # j = 0: 3 2 / j = 1: 3 3; universe 2 is the lattice itself (its cell's material)
    lattice = lat1(10, 2, mat, -1.0, planes, ((0, 1), (0, 1)), [[3, 2], [3, 3]], u, "grid")
    assert lattice.id == 2 and lattice.pitch == pytest.approx([2.0, 2.0])
    assert lattice.lower_left == pytest.approx([0.0, 0.0])
    box = openmc.model.RectangularParallelepiped(0, 4, 0, 4, -1, 1, boundary_type="vacuum")
    geometry = openmc.Geometry([openmc.Cell(cell_id=1, fill=lattice, region=-box)])
    assert cell_path(geometry, (1.0, 1.0, 0.0)) == [1, 2003, 31], "pin of element (0,0)"
    assert cell_path(geometry, (1.9, 1.9, 0.0)) == [1, 2003, 32]
    assert cell_path(geometry, (3.0, 1.0, 0.0)) == [1, 10], "element (1,0): own material"
    assert cell_path(geometry, (3.0, 3.0, 0.0)) == [1, 2003, 31], "pin of element (1,1)"
    own = geometry.get_all_cells()[10]
    assert own.fill is mat and own.density == pytest.approx(1.0)


def test_lat1_reversed_indices_and_infinite_fill():
    u = Universes()
    pin = openmc.ZCylinder(surface_id=1, x0=1.0, y0=1.0, r=0.5)
    u[3].add_cell(openmc.Cell(cell_id=31, fill=water(), region=-pin))
    u[3].add_cell(openmc.Cell(cell_id=32, region=+pin))
    planes = (  # +i runs towards -x, +j towards -y
        openmc.XPlane(surface_id=12, x0=0.0),
        openmc.XPlane(surface_id=11, x0=2.0),
        openmc.YPlane(surface_id=14, y0=0.0),
        openmc.YPlane(surface_id=13, y0=2.0),
    )
    lattice = lat1(5, 2, None, None, planes, ((-1, 0), (-1, 0)), 3, u, number=10000)
    assert lattice.lower_left == pytest.approx([0.0, 0.0])
    assert lattice.outer is not None and lattice.outer.id == 20003
    box = openmc.model.RectangularParallelepiped(0, 8, 0, 4, -1, 1, boundary_type="vacuum")
    geometry = openmc.Geometry([openmc.Cell(cell_id=1, fill=lattice, region=-box)])
    assert cell_path(geometry, (3.0, 3.0, 0.0)) == [1, 20003, 31]
    assert cell_path(geometry, (7.0, 1.0, 0.0)) == [1, 20003, 31], "outside the array: outer"


def test_importances_become_weight_windows():
    mesh = openmc.RegularMesh(mesh_id=1)
    mesh.lower_left, mesh.upper_right, mesh.dimension = (0, 0, 0), (2, 1, 1), (2, 1, 1)
    ww = weight_windows(mesh, [1, 4])
    assert np.ravel(ww.lower_ww_bounds) == pytest.approx([0.5, 0.125])
    assert np.ravel(ww.upper_ww_bounds) == pytest.approx([2.5, 0.625])
    assert list(ww.energy_bounds) == [0.0, 20.0e6]
    with pytest.raises(ValueError, match="vacuum"):
        weight_windows(mesh, [1, 0])


def test_strict_ids_turn_reused_ids_into_errors():
    openmc.ZPlane(surface_id=4242)
    with strict_ids(), pytest.raises(openmc.IDWarning):
        openmc.ZPlane(surface_id=4242)
