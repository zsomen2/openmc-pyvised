"""Tally and k-eff arithmetic for the Simulation tab, without Qt.

- normalisation (the sidecar's MCNP ``fm``): mean × source × multiplier ÷ volume or area,
  which may be a formula of the run's builder parameters;
- MCNP's guideline for relative errors (< 5 % good, 5–10 % fair, ≥ 10 % poor) and the
  figure of merit 1/(R²T), T in minutes of active batches;
- table rows and CSV, one column per filter;
- what a tally can be plotted as: an energy spectrum, a 1-D or 2-D mesh, bars over cells;
- reactivity in pcm and dollars, and how a result compares with a reference.

Tally arrays have the shape (bins of each filter …, nuclides, scores); the last filter
varies fastest, and mesh bins run with the first mesh index fastest.
"""

import csv
import io
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from pyvised_protocol.runs import Estimate, TallyFilterResult, TallyResult
from pyvised_protocol.sidecar import ReferenceResult, TallyNormalisation

GOOD_REL_ERR = 0.05
FAIR_REL_ERR = 0.10
ID_KINDS = {"cell", "surface", "material", "universe", "cellborn", "cellfrom", "cellinstance"}
EDGE_KINDS = {"energy", "energyout", "time", "mu", "polar", "azimuthal"}
MESH_KINDS = {"mesh", "meshborn"}


class Quality(StrEnum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    NONE = "none"
    """No estimate: a zero bin, or too few realizations."""


def quality(rel_err: float | None) -> Quality:
    if rel_err is None or not math.isfinite(rel_err):
        return Quality.NONE
    if rel_err < GOOD_REL_ERR:
        return Quality.GOOD
    if rel_err < FAIR_REL_ERR:
        return Quality.FAIR
    return Quality.POOR


def rel_err(mean: float, std: float | None) -> float | None:
    if std is None or not math.isfinite(std) or mean == 0 or not math.isfinite(mean):
        return None
    return abs(std / mean)


def figure_of_merit(rel: float | None, seconds: float | None) -> float | None:
    """MCNP's FOM = 1 / (R² T) with T in minutes."""
    if rel is None or rel <= 0 or not seconds or seconds <= 0:
        return None
    return 1.0 / (rel * rel * seconds / 60.0)


def factor(norm: TallyNormalisation | None, params: Mapping[str, float] | None = None) -> float:
    """What the sidecar's normalisation multiplies a tally with (1 without one) in a run with
    the builder parameters `params`; NaN when its divisor formula needs a parameter the run
    does not have."""
    if norm is None:
        return 1.0
    value = norm.source_strength * norm.multiplier
    if norm.divide_by != "none":
        try:
            divisor = norm.divisor_value(params or {})
        except ValueError:
            return math.nan
        if divisor:
            value /= divisor
    return value


# -- bins ------------------------------------------------------------------------------------


def energy_text(ev: float, unit: str = "eV") -> str:
    if unit == "MeV":
        return f"{ev / 1e6:.4g}"
    return f"{ev:.4g}"


def filter_title(f: TallyFilterResult) -> str:
    names = {
        "energy": "energy",
        "energyout": "outgoing energy",
        "energyfunction": "energy function",
        "meshsurface": "mesh surface",
    }
    return names.get(f.kind, f.kind)


def mesh_index(f: TallyFilterResult, b: int) -> tuple[int, ...]:
    """1-based mesh indices (first index fastest) of bin `b` of a mesh filter."""
    dims = f.mesh.dimension if f.mesh is not None and f.mesh.dimension else [f.bins]
    index = np.unravel_index(b, tuple(dims), order="F")
    return tuple(int(i) + 1 for i in index)


def bin_label(f: TallyFilterResult, b: int, unit: str = "eV") -> str:
    """Short text for bin `b` of filter `f`."""
    if f.kind in ID_KINDS and b < len(f.ids):
        return str(f.ids[b])
    if f.kind in EDGE_KINDS and b + 1 < len(f.edges):
        low, high = f.edges[b], f.edges[b + 1]
        if f.kind in ("energy", "energyout"):
            return f"{energy_text(low, unit)} – {energy_text(high, unit)}"
        return f"{low:.4g} – {high:.4g}"
    if f.kind in MESH_KINDS:
        return "(" + ", ".join(str(i) for i in mesh_index(f, b)) + ")"
    if b < len(f.labels):
        return f.labels[b]
    return str(b + 1)


def filter_summary(f: TallyFilterResult) -> str:
    if f.kind in ID_KINDS and f.ids:
        shown = ", ".join(str(i) for i in f.ids[:6])
        return f"{filter_title(f)} {shown}" + (" …" if len(f.ids) > 6 else "")
    if f.summary:
        return f"{filter_title(f)}: {f.summary}"
    return f"{filter_title(f)} ({f.bins})"


@dataclass(frozen=True)
class Row:
    """One value of a tally: its bin indices per filter, nuclide and score."""

    bins: tuple[int, ...]
    nuclide: int
    score: int
    mean: float
    std: float | None

    @property
    def rel_err(self) -> float | None:
        return rel_err(self.mean, self.std)


def rows(tally: TallyResult, mean: np.ndarray, std: np.ndarray) -> list[Row]:
    out = []
    for index in np.ndindex(*mean.shape):
        *bins, nuclide, score = index
        s = float(std[index])
        out.append(
            Row(
                tuple(int(b) for b in bins),
                int(nuclide),
                int(score),
                float(mean[index]),
                s if math.isfinite(s) else None,
            )
        )
    return out


def totals(tally: TallyResult) -> list[tuple[str, str, Estimate]]:
    """(nuclide, score, total over the filter bins) of a tally."""
    out = []
    for index, total in enumerate(tally.totals):
        nuclide, score = divmod(index, max(1, len(tally.scores)))
        if nuclide < len(tally.nuclides) and score < len(tally.scores):
            out.append((tally.nuclides[nuclide], tally.scores[score], total))
    return out


def csv_text(
    tally: TallyResult,
    mean: np.ndarray,
    std: np.ndarray,
    norm: TallyNormalisation | None = None,
    seconds: float | None = None,
    params: Mapping[str, float] | None = None,
) -> str:
    """The tally as CSV: numeric bin columns (edges in eV, 1-based mesh indices), values."""
    header: list[str] = []
    for f in tally.filters:
        title = filter_title(f)
        if f.kind in EDGE_KINDS:
            unit = " [eV]" if f.kind in ("energy", "energyout") else ""
            header += [f"{title} low{unit}", f"{title} high{unit}"]
        elif f.kind in MESH_KINDS:
            dims = len(f.mesh.dimension) if f.mesh is not None and f.mesh.dimension else 1
            header += [f"{title} {axis}" for axis in "ijk"[:dims]]
        else:
            header.append(title)
    header += ["nuclide", "score", "mean", "std. dev.", "rel. err.", "FOM"]
    scale = factor(norm, params)
    if norm is not None:
        header += [f"normalised{f' [{norm.unit}]' if norm.unit else ''}", "normalised std. dev."]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows(tally, mean, std):
        cells: list[object] = []
        for f, b in zip(tally.filters, row.bins, strict=True):
            if f.kind in EDGE_KINDS:
                cells += [f.edges[b], f.edges[b + 1]] if b + 1 < len(f.edges) else ["", ""]
            elif f.kind in MESH_KINDS:
                cells += list(mesh_index(f, b))
            else:
                cells.append(bin_label(f, b))
        rel = row.rel_err
        fom = figure_of_merit(rel, seconds)
        cells += [
            tally.nuclides[row.nuclide],
            tally.scores[row.score],
            repr(row.mean),
            "" if row.std is None else repr(row.std),
            "" if rel is None else f"{rel:.6g}",
            "" if fom is None else f"{fom:.6g}",
        ]
        if norm is not None:
            cells += [repr(row.mean * scale), "" if row.std is None else repr(row.std * scale)]
        writer.writerow(cells)
    return out.getvalue()


# -- plots -------------------------------------------------------------------------------------


class PlotKind(StrEnum):
    SPECTRUM = "spectrum"
    MESH_1D = "mesh_1d"
    MESH_2D = "mesh_2d"
    BARS = "bars"
    CONVERGENCE = "convergence"


def mesh_axes(f: TallyFilterResult) -> list[int]:
    """Mesh axes with more than one bin."""
    if f.mesh is None or not f.mesh.dimension:
        return [0] if f.bins > 1 else []
    return [axis for axis, n in enumerate(f.mesh.dimension) if n > 1]


def plot_kinds(tally: TallyResult) -> list[tuple[PlotKind, int]]:
    """What the tally can be plotted as: (kind, index of the filter on the x axis)."""
    kinds: list[tuple[PlotKind, int]] = []
    for index, f in enumerate(tally.filters):
        if f.kind in ("energy", "energyout") and f.bins > 1:
            kinds.append((PlotKind.SPECTRUM, index))
        elif f.kind in MESH_KINDS:
            axes = mesh_axes(f)
            if len(axes) == 1:
                kinds.append((PlotKind.MESH_1D, index))
            elif len(axes) >= 2:
                kinds.append((PlotKind.MESH_2D, index))
                kinds.append((PlotKind.MESH_1D, index))
        elif f.kind in ID_KINDS and f.bins > 1:
            kinds.append((PlotKind.BARS, index))
    kinds.append((PlotKind.CONVERGENCE, -1))
    return kinds


def lethargy_widths(edges: Sequence[float] | np.ndarray) -> np.ndarray:
    """ln(E_high / E_low) per energy bin; NaN where the lower edge is 0."""
    e = np.asarray(edges, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        widths = np.log(e[1:] / e[:-1])
    widths[~np.isfinite(widths)] = np.nan
    return widths


def select(
    values: np.ndarray, axis: int, fixed: Sequence[int], nuclide: int, score: int
) -> np.ndarray:
    """1-D slice along filter `axis`; the other filters at their `fixed` bins."""
    index: list[int | slice] = []
    index.extend(int(b) for b in fixed)
    index += [nuclide, score]
    index[axis] = slice(None)
    return np.asarray(values[tuple(index)], dtype=np.float64)


def mesh_values(
    f: TallyFilterResult, line: np.ndarray, axes: Sequence[int], positions: Sequence[int] = ()
) -> np.ndarray:
    """A mesh filter's bins (`line`) as an array over `axes`; every other axis at its bin in
    `positions` (0-based; missing ones are 0, so 1-D and 2-D meshes take any positions)."""
    dims = tuple(f.mesh.dimension) if f.mesh is not None and f.mesh.dimension else (len(line),)
    grid = np.asarray(line).reshape(dims, order="F")
    index: list[int | slice] = [
        min(max(int(positions[axis]) if axis < len(positions) else 0, 0), n - 1)
        for axis, n in enumerate(dims)
    ]
    for axis in axes:
        index[axis] = slice(None)
    return grid[tuple(index)]


def mesh_edges(f: TallyFilterResult, axis: int) -> np.ndarray:
    """Bin edges of a mesh axis [cm] (indices when the mesh has no grid)."""
    if f.mesh is not None and axis < len(f.mesh.grids) and f.mesh.grids[axis]:
        return np.asarray(f.mesh.grids[axis], dtype=np.float64)
    n = f.mesh.dimension[axis] if f.mesh is not None and f.mesh.dimension else f.bins
    return np.arange(n + 1, dtype=np.float64)


# -- k-eff -------------------------------------------------------------------------------------


def reactivity(k: float) -> float:
    """ρ = (k − 1) / k."""
    return (k - 1.0) / k


def pcm(value: float) -> float:
    return value * 1e5


def reactivity_std(k: float, k_std: float) -> float:
    """σ(ρ) = σ(k) / k²."""
    return k_std / (k * k)


@dataclass(frozen=True)
class Comparison:
    reference: ReferenceResult
    value: float
    std: float | None
    difference: float
    """value − reference (k-eff: absolute; tallies: relative)."""
    sigmas: float | None
    """|difference| in combined standard deviations."""

    @property
    def agrees(self) -> bool | None:
        """Within the reference's tolerance, else within two combined standard deviations."""
        if self.reference.tolerance is not None:
            return abs(self.difference) <= self.reference.tolerance
        return None if self.sigmas is None else self.sigmas <= 2.0


def reference_std(reference: ReferenceResult) -> float | None:
    if reference.std is not None:
        return reference.std
    if reference.rel_err is not None:
        return abs(reference.rel_err * reference.value)
    return None


def compare(reference: ReferenceResult, value: float, std: float | None) -> Comparison:
    ref_std = reference_std(reference)
    combined = math.hypot(std or 0.0, ref_std or 0.0)
    difference = value - reference.value
    if reference.tally is not None and reference.value:
        difference /= reference.value
        combined /= abs(reference.value)
    return Comparison(
        reference=reference,
        value=value,
        std=std,
        difference=difference,
        sigmas=abs(difference) / combined if combined > 0 else None,
    )


def matching(
    references: Iterable[ReferenceResult], params: dict[str, float]
) -> list[ReferenceResult]:
    return [r for r in references if r.matches(params)]


@dataclass
class Series:
    """Points of a plot: x, y and σ (NaN where there is none)."""

    x: np.ndarray = field(default_factory=lambda: np.zeros(0))
    y: np.ndarray = field(default_factory=lambda: np.zeros(0))
    std: np.ndarray = field(default_factory=lambda: np.zeros(0))
