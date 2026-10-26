"""Building blocks for OpenMC models that mirror an MCNP deck.

A rebuild keeps the deck's cell, surface, material and universe numbers and
its order, with each card as a comment above its statement. Most cards map to
one OpenMC statement; these helpers cover the ones that do not:

- `parameter`: builder parameters set by PyVised (``PYVISED_PARAMS``);
- `Universes` (``u=n``) and `Cells` (``#n``, also for a cell further down);
- `density`: the density on a cell card (negative g/cm³, positive atom/b-cm);
- `Transform`: ``TRn``/``*TRn`` for transformed surfaces (``j n mnemonic …``)
  and, through `trcl`, for moved cells;
- `lat1`: a ``lat=1`` cell and its ``fill`` as an `openmc.RectLattice`;
- `weight_windows`: ``imp:n`` values as weight windows;
- `strict_ids`: a reused id becomes an error instead of OpenMC's warning.
"""

import contextlib
import json
import os
import warnings
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import openmc  # pyright: ignore[reportMissingImports]  (backend image only)
import openmc.data  # pyright: ignore[reportMissingImports]

PARAMS_ENV = "PYVISED_PARAMS"
BARN_CM = 1e-24
"""cm² per barn: atom/b-cm × 1e24 = atom/cm³."""


def parameter(name: str, default: Any) -> Any:
    """Builder parameter `name` as set by PyVised (JSON in ``PYVISED_PARAMS``), else `default`."""
    return json.loads(os.environ.get(PARAMS_ENV) or "{}").get(name, default)


@contextlib.contextmanager
def strict_ids() -> Iterator[None]:
    """Make OpenMC's warning about a reused id an error: in a hand rebuild it is a typo."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", openmc.IDWarning)
        yield


class Universes(dict):
    """``u[n]`` is universe n; it is created on first use, like MCNP's ``u=n``."""

    def __missing__(self, n: int) -> Any:
        self[n] = openmc.Universe(universe_id=n)
        return self[n]


class _Unresolved(openmc.Intersection):
    """Stands for the region of a cell that is defined further down the deck."""

    def __init__(self, cell_id: int) -> None:
        super().__init__([])
        self.cell_id = cell_id

    def __str__(self) -> str:
        raise RuntimeError(
            f"#{self.cell_id} was never resolved: call Cells.resolve() once cell "
            f"{self.cell_id} has its region"
        )


class Cells(dict):
    """``c[j]`` is cell j; ``c.complement(n)`` is MCNP's ``#n``.

    ``#n`` may name a cell further down the deck: its complement is filled in by
    `resolve`, which must run once every cell has its region.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[tuple[Any, int]] = []

    def complement(self, n: int) -> Any:
        cell = self.get(n)
        if cell is not None and cell.region is not None:
            return openmc.Complement(cell.region)
        hole = openmc.Complement(_Unresolved(n))
        self._pending.append((hole, n))
        return hole

    def resolve(self) -> None:
        for hole, n in self._pending:
            cell = self.get(n)
            if cell is None or cell.region is None:
                raise KeyError(f"#{n}: cell {n} does not exist or has no region")
            hole.node = cell.region
        self._pending.clear()


def density(cell: Any, value: float) -> None:
    """Cell-card density: negative = g/cm³, positive = atom/b-cm.

    `Cell.density` is in g/cm³, so atom/b-cm is converted with the average
    molar mass of the cell's material.
    """
    if value < 0:
        cell.density = -value
    else:
        cell.density = value * cell.fill.average_molar_mass / (openmc.data.AVOGADRO * BARN_CM)


# -- transformations -------------------------------------------------------------------


@dataclass(frozen=True)
class Transform:
    """An MCNP ``TRn`` card: main coordinates = displacement + Bᵀ · auxiliary coordinates.

    `rotation` is B as written on the card, row by row: its rows are the
    auxiliary x', y' and z' axes in main coordinates. None means a pure
    translation.
    """

    displacement: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[tuple[float, float, float], ...] | None = None

    @classmethod
    def card(cls, *values: float, degrees: bool = False) -> "Transform":
        """From the entries of a card: ``O1 O2 O3 [B1 … B9 [M]]``.

        ``*TRn`` gives B as angles in degrees (`degrees`). With M = -1 the
        displacement is the main origin in auxiliary coordinates. All nine B
        entries are needed (MCNP also accepts partial matrices).
        """
        if len(values) not in (3, 12, 13):
            raise ValueError(f"a TR card has 3, 12 or 13 entries, not {len(values)}")
        origin = np.array(values[:3], dtype=float)
        if len(values) == 3:
            return cls((float(origin[0]), float(origin[1]), float(origin[2])))
        b = np.array(values[3:12], dtype=float).reshape(3, 3)
        if degrees:
            b = np.cos(np.radians(b))
        if not np.allclose(b @ b.T, np.eye(3), atol=1e-3):
            raise ValueError(f"the rotation matrix is not orthonormal:\n{b}")
        if len(values) == 13 and values[12] not in (1, -1):
            raise ValueError(f"M must be 1 or -1, not {values[12]}")
        if len(values) == 13 and values[12] == -1:
            origin = -(b.T @ origin)
        return cls(
            (float(origin[0]), float(origin[1]), float(origin[2])),
            tuple((float(r[0]), float(r[1]), float(r[2])) for r in b),
        )

    @property
    def matrix(self) -> np.ndarray:
        """B (identity for a pure translation)."""
        return np.eye(3) if self.rotation is None else np.array(self.rotation)

    def apply(self, point: Sequence[float]) -> np.ndarray:
        """Auxiliary -> main coordinates of a point."""
        return np.asarray(self.displacement) + self.matrix.T @ np.asarray(point, dtype=float)

    def surface(self, surface: Any, surface_id: int) -> Any:
        """`surface`, given in auxiliary coordinates, moved to main coordinates as `surface_id`.

        Give `surface` without an id: the moved copy takes `surface_id`.
        """
        moved = surface
        if self.rotation is not None:
            moved = moved.rotate(self.matrix.T)  # an active rotation: x -> Bᵀ x
        moved = moved.translate(self.displacement)
        moved.id = surface_id
        return moved


def _map_surfaces(region: Any, move: Callable[[Any], Any]) -> Any:
    """A copy of `region` with every surface replaced by `move(surface)`."""
    if isinstance(region, _Unresolved):
        raise RuntimeError(f"#{region.cell_id} must be resolved before the cell is moved")
    if isinstance(region, openmc.Halfspace):
        return openmc.Halfspace(move(region.surface), region.side)
    if isinstance(region, openmc.Complement):
        return openmc.Complement(_map_surfaces(region.node, move))
    if isinstance(region, (openmc.Intersection, openmc.Union)):
        return type(region)([_map_surfaces(node, move) for node in region])
    raise TypeError(f"cannot move a {type(region).__name__} region")


def trcl(cell: Any, tr: Transform) -> Any:
    """MCNP ``trcl``: move `cell` and its fill by `tr`.

    The cell's surfaces become moved copies numbered 1000·cell + j, as MCNP
    numbers them. A filling universe or lattice moves with the cell through
    `Cell.translation` and `Cell.rotation` (OpenMC: local = R (r - t), so
    R = B and t = the displacement).
    """
    copies: dict[int, Any] = {}

    def move(surface: Any) -> Any:
        if surface.id not in copies:
            copies[surface.id] = tr.surface(surface, 1000 * cell.id + surface.id)
        return copies[surface.id]

    cell.region = _map_surfaces(cell.region, move)
    if isinstance(cell.fill, (openmc.UniverseBase, openmc.Lattice)):
        if any(tr.displacement):
            cell.translation = tr.displacement
        if tr.rotation is not None:
            cell.rotation = tr.matrix
    return cell


# -- lattices ------------------------------------------------------------------------------


def lat1(
    cell_id: int,
    universe_id: int,
    material: Any,
    rho: float | None,
    planes: Sequence[Any],
    fill_range: tuple[tuple[int, int], tuple[int, int]],
    fill: Sequence[Sequence[int]] | int,
    u: Universes,
    name: str = "",
    *,
    number: int = 1000,
) -> Any:
    """MCNP lattice cell (``lat=1``) -> `openmc.RectLattice` numbered like its universe.

    `planes` are the element's x and y planes in card order: across the first
    lies element (1,0), across the second (-1,0), across the third (0,1) and
    across the fourth (0,-1). `fill` is the fill array in deck order (i varies
    fastest, one row per j) over `fill_range` ((i1, i2), (j1, j2)). A single
    universe number (``fill=n``, an infinite lattice) fills every element of the
    range and also serves as the outer universe, so the lattice stays infinite.
    An entry naming the lattice's own universe means the lattice cell's
    `material` (density `rho` as on the card; None for a void lattice cell).

    MCNP puts a filling universe in the lattice cell's own coordinates for
    element (0,0); OpenMC centres it on each element. When element (0,0) is
    off the origin, each universe sits in a cell translated by -(element
    centre). That cell and its universe are numbered `number`·universe + n, and
    so is the universe holding the lattice cell's material (n = universe).
    """
    x_plus, x_minus, y_plus, y_minus = planes
    for plane, kind in ((x_plus, openmc.XPlane), (x_minus, openmc.XPlane)):
        if not isinstance(plane, kind):
            raise TypeError(f"surface {plane.id}: lat1 handles px/py element planes only")
    for plane in (y_plus, y_minus):
        if not isinstance(plane, openmc.YPlane):
            raise TypeError(f"surface {plane.id}: lat1 handles px/py element planes only")
    di = x_plus.x0 - x_minus.x0  # step from element i to i+1; its sign is the direction
    dj = y_plus.y0 - y_minus.y0
    cx = (x_plus.x0 + x_minus.x0) / 2  # centre of element (0,0)
    cy = (y_plus.y0 + y_minus.y0) / 2
    (i1, i2), (j1, j2) = fill_range
    ni, nj = i2 - i1 + 1, j2 - j1 + 1
    elements: dict[int, Any] = {}

    def element(n: int) -> Any:
        if n not in elements:
            if n == universe_id:
                own = openmc.Cell(cell_id=cell_id, name=name, fill=material)
                if material is not None and rho is not None:
                    density(own, rho)
                elements[n] = openmc.Universe(universe_id=number * universe_id + n, cells=[own])
            elif (cx, cy) != (0.0, 0.0):
                moved = openmc.Cell(
                    cell_id=number * universe_id + n, name=f"u={n} in lattice {universe_id}"
                )
                moved.fill = u[n]
                moved.translation = (-cx, -cy, 0.0)
                elements[n] = openmc.Universe(universe_id=number * universe_id + n, cells=[moved])
            else:
                elements[n] = u[n]
        return elements[n]

    lattice = openmc.RectLattice(lattice_id=universe_id, name=name)
    lattice.pitch = (abs(di), abs(dj))
    lattice.lower_left = (
        min(cx + i1 * di, cx + i2 * di) - abs(di) / 2,
        min(cy + j1 * dj, cy + j2 * dj) - abs(dj) / 2,
    )
    rows = []
    for r in range(nj):  # OpenMC rows run from +y down to -y
        k = nj - 1 - r
        j = j1 + k if dj > 0 else j2 - k
        row = []
        for col in range(ni):  # columns run from -x to +x
            i = i1 + col if di > 0 else i2 - col
            row.append(element(fill if isinstance(fill, int) else fill[j - j1][i - i1]))
        rows.append(row)
    lattice.universes = rows
    if isinstance(fill, int):
        lattice.outer = element(fill)
    return lattice


# -- importances ----------------------------------------------------------------------------


def weight_windows(
    mesh: Any,
    importances: Sequence[float],
    *,
    lower: float = 0.5,
    ratio: float = 5.0,
    survival: float = 3.0,
    energy_bounds: Sequence[float] = (0.0, 20.0e6),
    particle: str = "neutron",
) -> Any:
    """``imp:n`` values -> weight windows on a mesh that follows the importance regions.

    `importances` has one value per mesh element, in the mesh's order (the first
    index varies fastest). The lower bound is `lower`/imp; the upper/lower
    ratio 5 and the survival ratio 3 are the defaults of MCNP's WWG and WWP
    cards. One energy group covers `energy_bounds` [eV] (OpenMC 0.16.0 cannot
    read weight windows back without energy bounds). imp:n=0 has no window: use
    a vacuum boundary instead.
    """
    if any(imp <= 0 for imp in importances):
        raise ValueError("importances must be positive; imp:n=0 becomes a vacuum boundary")
    return openmc.WeightWindows(
        mesh,
        [lower / imp for imp in importances],
        upper_bound_ratio=ratio,
        survival_ratio=survival,
        energy_bounds=list(energy_bounds),
        particle_type=particle,
    )
