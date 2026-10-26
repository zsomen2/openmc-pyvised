"""Weight-window lower bounds at points: the importance layer of a slice.

The weight windows and their meshes are read from ``model.xml`` with
ElementTree and sampled with numpy, so neither ``openmc.lib`` nor the OpenMC
Python API is needed (OpenMC 0.16.0 cannot even read some weight windows back).
As in OpenMC, the first window set in model order that has a valid window at a
point wins: its particle matches, the point lies in its mesh, the energy lies
in its bounds and the lower bound is positive.

Lower bounds are stored mesh-major with the energy group slowest and the first
mesh index fastest (OpenMC writes ``lower_ww_bounds.ravel('F')``).
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TWO_PI = 2.0 * math.pi


def _floats(element: ET.Element | None, tag: str) -> list[float]:
    text = element.findtext(tag) if element is not None else None
    return [float(x) for x in (text or "").split()]


@dataclass
class Mesh:
    """A structured mesh: per axis the bin edges, in its own coordinates."""

    kind: str
    """"regular", "rectilinear", "cylindrical" or "spherical"."""
    edges: tuple[np.ndarray, ...]
    origin: np.ndarray

    @property
    def dimension(self) -> tuple[int, ...]:
        return tuple(len(e) - 1 for e in self.edges)

    @classmethod
    def from_xml(cls, element: ET.Element) -> "Mesh":
        kind = element.get("type", "regular")
        origin = np.array(_floats(element, "origin") or [0.0, 0.0, 0.0])
        if kind == "regular":
            dimension = [int(x) for x in (element.findtext("dimension") or "").split()]
            lower = _floats(element, "lower_left")
            upper = _floats(element, "upper_right")
            if not upper:
                width = _floats(element, "width")
                upper = [lo + n * w for lo, n, w in zip(lower, dimension, width, strict=True)]
            edges = tuple(
                np.linspace(lo, hi, n + 1)
                for lo, hi, n in zip(lower, upper, dimension, strict=True)
            )
            return cls(kind, edges, np.zeros(3))
        axes = {
            "rectilinear": ("x_grid", "y_grid", "z_grid"),
            "cylindrical": ("r_grid", "phi_grid", "z_grid"),
            "spherical": ("r_grid", "theta_grid", "phi_grid"),
        }.get(kind)
        if axes is None:
            raise ValueError(f"{kind} meshes are not supported")
        return cls(kind, tuple(np.array(_floats(element, a)) for a in axes), origin)

    def coordinates(self, points: np.ndarray) -> np.ndarray:
        """(N, 3) points -> (N, k) coordinates along the mesh axes."""
        if self.kind in ("regular", "rectilinear"):
            return points[:, : len(self.edges)]
        p = points - self.origin
        if self.kind == "cylindrical":
            r = np.hypot(p[:, 0], p[:, 1])
            phi = np.mod(np.arctan2(p[:, 1], p[:, 0]), TWO_PI)
            return np.stack([r, phi, p[:, 2]], axis=1)
        r = np.linalg.norm(p, axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            theta = np.arccos(np.clip(np.where(r > 0, p[:, 2] / r, 1.0), -1.0, 1.0))
        phi = np.mod(np.arctan2(p[:, 1], p[:, 0]), TWO_PI)
        return np.stack([r, theta, phi], axis=1)

    def bins(self, points: np.ndarray) -> np.ndarray:
        """Flat bin index (first axis fastest) per point; -1 outside the mesh.

        Bins are half-open, [low edge, high edge), as in OpenMC.
        """
        coords = self.coordinates(points)
        flat = np.zeros(len(points), dtype=np.int64)
        inside = np.ones(len(points), dtype=bool)
        stride = 1
        for axis, edges in enumerate(self.edges):
            index = np.searchsorted(edges, coords[:, axis], side="right") - 1
            inside &= (index >= 0) & (index < len(edges) - 1)
            flat += np.clip(index, 0, len(edges) - 2) * stride
            stride *= len(edges) - 1
        return np.where(inside, flat, -1)


@dataclass
class WindowSet:
    id: int
    particle: str
    energy_bounds: np.ndarray
    mesh: Mesh
    lower: np.ndarray
    """(energy groups, mesh bins) lower bounds."""

    def group(self, energy: float | None) -> int | None:
        """Energy group of `energy` [eV] (None: the first group); None outside the bounds."""
        groups = len(self.energy_bounds) - 1
        if energy is None or groups <= 0:
            return 0
        if not self.energy_bounds[0] <= energy <= self.energy_bounds[-1]:
            return None
        return int(min(np.searchsorted(self.energy_bounds, energy, side="right") - 1, groups - 1))


def read_weight_windows(model_xml: Path) -> list[WindowSet]:
    """The weight-window sets of a model, in model order; unsupported meshes are skipped."""
    root = ET.parse(model_xml).getroot()
    settings = root.find("settings") if root.tag == "model" else root
    if settings is None:
        return []
    meshes = {m.get("id"): m for m in root.iter("mesh")}
    found = []
    for element in settings.iter("weight_windows"):
        mesh_element = meshes.get((element.findtext("mesh") or "").strip())
        if mesh_element is None:
            continue
        try:
            mesh = Mesh.from_xml(mesh_element)
        except ValueError:
            continue
        bounds = np.array(_floats(element, "energy_bounds"))
        lower = np.array(_floats(element, "lower_ww_bounds"))
        bins = int(np.prod(mesh.dimension))
        groups = max(1, len(bounds) - 1)
        if lower.size != bins * groups:
            continue
        found.append(
            WindowSet(
                id=int(element.get("id", "0")),
                particle=(element.findtext("particle_type") or "neutron").strip(),
                energy_bounds=bounds,
                mesh=mesh,
                lower=lower.reshape(groups, bins),
            )
        )
    return found


def lower_bounds(
    sets: list[WindowSet],
    points: np.ndarray,
    particle: str = "neutron",
    energy: float | None = None,
) -> np.ndarray:
    """Lower weight bound per (N, 3) point; NaN where no window set has a valid window."""
    result = np.full(len(points), np.nan)
    todo = np.ones(len(points), dtype=bool)
    for ws in sets:
        if ws.particle != particle or not todo.any():
            continue
        group = ws.group(energy)
        if group is None:
            continue
        index = np.flatnonzero(todo)
        bins = ws.mesh.bins(points[index])
        inside = bins >= 0
        values = np.full(len(index), np.nan)
        values[inside] = ws.lower[group, bins[inside]]
        valid = inside & (values > 0)
        result[index[valid]] = values[valid]
        todo[index[valid]] = False
    return result


def slice_points(
    basis: str,
    origin: tuple[float, float, float],
    width: tuple[float, float],
    pixels: tuple[int, int],
) -> np.ndarray:
    """(rows · cols, 3) pixel centres of a slice; row 0 is the top edge, as in `slice_data`."""
    h, v, _ = {"xy": (0, 1, 2), "xz": (0, 2, 1), "yz": (1, 2, 0)}[basis]
    cols, rows = pixels
    u = origin[h] - width[0] / 2 + (np.arange(cols) + 0.5) * width[0] / cols
    w = origin[v] + width[1] / 2 - (np.arange(rows) + 0.5) * width[1] / rows
    points = np.empty((rows, cols, 3))
    points[...] = origin
    points[..., h] = u[np.newaxis, :]
    points[..., v] = w[:, np.newaxis]
    return points.reshape(-1, 3)
