"""Tally and k-eff arithmetic of the Simulation tab (no Qt)."""

import csv
import io
import math

import numpy as np
import pytest

from pyvised.render import tallies as tm
from pyvised_protocol.runs import Estimate, MeshInfo, TallyFilterResult, TallyResult
from pyvised_protocol.sidecar import ReferenceResult, TallyNormalisation

SURFACE = TallyFilterResult(kind="surface", bins=1, ids=[7])
ENERGY = TallyFilterResult(kind="energy", bins=3, edges=[0.0, 0.5, 5e5, 2e7])
DOSE = TallyFilterResult(kind="energyfunction", bins=1, labels=["f(E)"])
MESH = TallyFilterResult(
    kind="mesh",
    bins=6,
    mesh=MeshInfo(
        id=3,
        kind="regular",
        dimension=[3, 2, 1],
        lower_left=[0, 0, 0],
        upper_right=[3, 4, 1],
        grids=[[0, 1, 2, 3], [0, 2, 4], [0, 1]],
    ),
)


def dose_tally() -> tuple[TallyResult, np.ndarray, np.ndarray]:
    tally = TallyResult(
        id=2,
        name="dose",
        scores=["flux"],
        nuclides=["total"],
        filters=[SURFACE, ENERGY, DOSE],
        shape=[1, 3, 1, 1, 1],
        realizations=200,
        totals=[Estimate(value=6.0, std=math.sqrt(0.01 + 0.04 + 0.09))],
    )
    mean = np.array([1.0, 2.0, 3.0]).reshape(tally.shape)
    std = np.array([0.1, 0.2, 0.3]).reshape(tally.shape)
    return tally, mean, std


def test_quality_follows_mcnps_guideline():
    assert tm.quality(0.01) is tm.Quality.GOOD
    assert tm.quality(0.05) is tm.Quality.FAIR
    assert tm.quality(0.0999) is tm.Quality.FAIR
    assert tm.quality(0.1) is tm.Quality.POOR
    assert tm.quality(None) is tm.Quality.NONE
    assert tm.rel_err(0.0, 1.0) is None and tm.rel_err(-2.0, 0.5) == 0.25


def test_figure_of_merit_uses_minutes():
    assert tm.figure_of_merit(0.1, 60.0) == pytest.approx(100.0)
    assert tm.figure_of_merit(0.1, 120.0) == pytest.approx(50.0)
    assert tm.figure_of_merit(None, 60.0) is None
    assert tm.figure_of_merit(0.1, 0.0) is None


def test_normalisation_factor():
    assert tm.factor(None) == 1.0
    norm = TallyNormalisation(
        source_strength=1.0e15, divide_by="area", divisor=1.25e5, multiplier=3.6e-3
    )
    assert tm.factor(norm) == pytest.approx(1.0e15 * 3.6e-3 / 1.25e5)
    no_divisor = TallyNormalisation(source_strength=2.0, divide_by="volume", multiplier=3.0)
    assert tm.factor(no_divisor) == 6.0


def test_bin_labels():
    assert tm.bin_label(SURFACE, 0) == "7"
    assert tm.bin_label(ENERGY, 1) == "0.5 – 5e+05"
    assert tm.bin_label(ENERGY, 2, "MeV") == "0.5 – 20"
    assert tm.bin_label(DOSE, 0) == "f(E)"
    # mesh bins run with the first index fastest
    assert [tm.bin_label(MESH, b) for b in range(4)] == [
        "(1, 1, 1)",
        "(2, 1, 1)",
        "(3, 1, 1)",
        "(1, 2, 1)",
    ]
    assert tm.filter_summary(SURFACE) == "surface 7"


def test_rows_and_csv():
    tally, mean, std = dose_tally()
    rows = tm.rows(tally, mean, std)
    assert [(r.bins, r.mean) for r in rows] == [((0, b, 0), b + 1.0) for b in range(3)]
    assert rows[0].rel_err == pytest.approx(0.1)
    norm = TallyNormalisation(source_strength=10.0, unit="µSv/h")
    text = tm.csv_text(tally, mean, std, norm, seconds=60.0)
    table = list(csv.reader(io.StringIO(text)))
    assert table[0] == [
        "surface",
        "energy low [eV]",
        "energy high [eV]",
        "energy function",
        "nuclide",
        "score",
        "mean",
        "std. dev.",
        "rel. err.",
        "FOM",
        "normalised [µSv/h]",
        "normalised std. dev.",
    ]
    assert table[2][:3] == ["7", "0.5", "500000.0"]
    assert float(table[2][6]) == 2.0 and float(table[2][9]) == pytest.approx(100.0)
    assert float(table[3][10]) == 30.0
    assert tm.totals(tally) == [("total", "flux", tally.totals[0])]


def test_plot_kinds_and_selections():
    tally, mean, _std = dose_tally()
    assert tm.plot_kinds(tally) == [(tm.PlotKind.SPECTRUM, 1), (tm.PlotKind.CONVERGENCE, -1)]
    assert list(tm.select(mean, 1, [0, 0, 0], 0, 0)) == [1.0, 2.0, 3.0]
    cells = TallyResult(id=4, filters=[TallyFilterResult(kind="cell", bins=3, ids=[1, 2, 3])])
    assert tm.plot_kinds(cells)[0] == (tm.PlotKind.BARS, 0)
    mesh = TallyResult(id=5, filters=[MESH, ENERGY])
    assert tm.plot_kinds(mesh)[:3] == [
        (tm.PlotKind.MESH_2D, 0),
        (tm.PlotKind.MESH_1D, 0),
        (tm.PlotKind.SPECTRUM, 1),
    ]
    assert tm.mesh_axes(MESH) == [0, 1]


def test_mesh_values_are_fortran_ordered():
    line = np.arange(6.0)  # bins (1,1) (2,1) (3,1) (1,2) (2,2) (3,2)
    grid = tm.mesh_values(MESH, line, [0, 1], [0, 0, 0])
    assert grid.shape == (3, 2)
    assert grid[:, 0].tolist() == [0.0, 1.0, 2.0] and grid[:, 1].tolist() == [3.0, 4.0, 5.0]
    assert tm.mesh_values(MESH, line, [1], [2, 0, 0]).tolist() == [2.0, 5.0]
    assert tm.mesh_edges(MESH, 1).tolist() == [0, 2, 4]


def test_mesh_values_of_meshes_with_fewer_axes():
    flat = MESH.model_copy(
        update={"mesh": MESH.mesh.model_copy(update={"dimension": [3, 2]})}  # type: ignore[union-attr]
    )
    line = np.arange(6.0)
    assert tm.mesh_values(flat, line, [0], [0, 1, 0]).tolist() == [3.0, 4.0, 5.0]
    assert tm.mesh_values(flat, line, [0, 1]).shape == (3, 2)
    assert tm.mesh_values(flat, line, [1], [9]).tolist() == [2.0, 5.0], "clamped to the mesh"
    line_mesh = TallyFilterResult(kind="mesh", bins=4)  # no mesh details: one axis
    assert tm.mesh_values(line_mesh, np.arange(4.0), [0], [0, 0, 0]).tolist() == [0, 1, 2, 3]


def test_lethargy_widths():
    widths = tm.lethargy_widths([0.0, 1.0, math.e, 10.0])
    assert math.isnan(widths[0])
    assert widths[1] == pytest.approx(1.0)
    assert widths[2] == pytest.approx(math.log(10.0 / math.e))


def test_reactivity_and_references():
    assert tm.pcm(tm.reactivity(1.01)) == pytest.approx(990.1, abs=0.1)
    assert tm.reactivity_std(1.0, 0.0005) == pytest.approx(0.0005)
    mcnp = ReferenceResult(name="k-eff", value=1.0300, std=0.0012, params={"thermal_scattering": 0})
    demo = ReferenceResult(
        name="k-eff", value=1.04000, std=0.0005, params={"thermal_scattering": 1}
    )
    assert tm.matching([mcnp, demo], {"thermal_scattering": 0.0}) == [mcnp]
    assert tm.matching([mcnp, demo], {}) == []
    c = tm.compare(demo, 1.04017, 0.0004)
    assert c.difference == pytest.approx(0.00017)
    assert c.sigmas == pytest.approx(0.00017 / math.hypot(0.0004, 0.0005))
    assert c.agrees
    agreed = ReferenceResult(name="critical", value=1.0, tolerance=0.004)
    assert tm.compare(agreed, 1.0022, 0.00025).agrees, "judged by the tolerance, not by σ"
    assert not tm.compare(agreed, 1.0045, 0.00025).agrees
    dose = ReferenceResult(name="dose", value=4.0e5, rel_err=0.12, tally=2)
    d = tm.compare(dose, 5.0e5, 2.5e4)
    assert d.difference == pytest.approx(5.0e5 / 4.0e5 - 1)
    assert tm.reference_std(dose) == pytest.approx(0.12 * 4.0e5)
