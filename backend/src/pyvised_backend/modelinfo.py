"""Summarise an exported model: ``python -m pyvised_backend.modelinfo MODEL_XML``.

Runs in its own process: the OpenMC Python API takes a second to import and
must stay out of the API process. Prints one line after `RESULT_MARKER` with
the JSON of the model part of `ModelInfo` (bounding box, materials, cells,
universes, lattices, surfaces, settings, tallies, weight windows, warnings).
"""

import json
import math
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pyvised_protocol.schemas import (
    BoundingBox,
    CellInfo,
    FilterInfo,
    LatticeInfo,
    MaterialInfo,
    NuclideFraction,
    SettingsInfo,
    SourceInfo,
    SurfaceInfo,
    TallyInfo,
    UniverseInfo,
    WeightWindowInfo,
)

RESULT_MARKER = "PYVISED_RESULT "
UNBOUNDED_EXTENT = 100.0
"""Half-extent [cm] used on axes along which the geometry is unbounded."""


BOUNDARIES = ("vacuum", "reflective", "white", "periodic")


def boundary_extent(geometry: Any) -> tuple[list[float], list[float]]:
    """Per axis, the lowest and highest finite coordinate of the boundary surfaces.

    OpenMC cannot bound some regions (a rotated cylinder is a general `Cylinder`
    without a bounding box), so a geometry whose particles all leave through
    vacuum planes may still report an infinite box; the boundary surfaces give
    its extent. Axes without such a coordinate stay infinite.
    """
    lower, upper = [math.inf] * 3, [-math.inf] * 3
    for surface in geometry.get_all_surfaces().values():
        if surface.boundary_type not in BOUNDARIES:
            continue
        for side in "-+":
            try:
                box = surface.bounding_box(side)
            except Exception:
                continue
            for axis in range(3):
                for value in (float(box.lower_left[axis]), float(box.upper_right[axis])):
                    if math.isfinite(value):
                        lower[axis] = min(lower[axis], value)
                        upper[axis] = max(upper[axis], value)
    return (
        [lo if lo < hi else -math.inf for lo, hi in zip(lower, upper, strict=True)],
        [hi if lo < hi else math.inf for lo, hi in zip(lower, upper, strict=True)],
    )


def geometry_bounds(geometry: Any) -> tuple[list[float], list[float]]:
    """The geometry's bounding box; infinite axes fall back on the boundary surfaces."""
    box = geometry.bounding_box
    lower = [float(x) for x in box.lower_left]
    upper = [float(x) for x in box.upper_right]
    if all(math.isfinite(v) for v in lower + upper):
        return lower, upper
    fallback_lower, fallback_upper = boundary_extent(geometry)
    for axis in range(3):
        unbounded = not (math.isfinite(lower[axis]) and math.isfinite(upper[axis]))
        if unbounded and math.isfinite(fallback_lower[axis] + fallback_upper[axis]):
            lower[axis], upper[axis] = fallback_lower[axis], fallback_upper[axis]
    return lower, upper


def bounding_box(lower: Any, upper: Any) -> BoundingBox:
    ll, ur, clipped = [], [], []
    for lo, hi in zip(lower, upper, strict=True):
        lo, hi = float(lo), float(hi)
        lo_ok, hi_ok = math.isfinite(lo), math.isfinite(hi)
        if not lo_ok and not hi_ok:
            lo, hi = -UNBOUNDED_EXTENT, UNBOUNDED_EXTENT
        elif not lo_ok:
            lo = hi - 2 * UNBOUNDED_EXTENT
        elif not hi_ok:
            hi = lo + 2 * UNBOUNDED_EXTENT
        ll.append(lo)
        ur.append(hi)
        clipped.append(not (lo_ok and hi_ok))
    return BoundingBox(lower_left=tuple(ll), upper_right=tuple(ur), clipped=tuple(clipped))


def _flatten(obj: Any) -> Iterator[Any]:
    if isinstance(obj, (list, tuple)) or getattr(obj, "ndim", 0) > 0:
        for item in obj:
            yield from _flatten(item)
    elif obj is not None:
        yield obj


def _ids_text(ids: list[int], limit: int = 8) -> str:
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, … ({len(ids)})"


def _describe_distribution(dist: Any) -> str:
    if dist is None:
        return ""
    name = type(dist).__name__
    if name == "Point":
        return "Point ({:g}, {:g}, {:g})".format(*dist.xyz)
    if name == "PointCloud":
        points = list(dist.positions)
        if len(points) == 1:
            return "PointCloud ({:g}, {:g}, {:g})".format(*points[0])
        return f"PointCloud, {len(points)} points"
    if name == "Box":
        return "Box ({:g}, {:g}, {:g}) – ({:g}, {:g}, {:g})".format(
            *dist.lower_left, *dist.upper_right
        )
    if name == "Watt":
        return f"Watt (a = {dist.a:g}, b = {dist.b:g})"
    if name == "Maxwell":
        return f"Maxwell (θ = {dist.theta:g} eV)"
    if name == "Discrete":
        values = list(dist.x)
        if len(values) == 1:
            return f"Discrete ({values[0]:g})"
        return f"Discrete, {len(values)} values"
    if name == "Isotropic":
        return "isotropic"
    return name


def _filter_summary(f: Any) -> str:
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    if isinstance(f, openmc.EnergyFunctionFilter):
        return f"{len(f.energy)} points, {f.interpolation}"
    if isinstance(f, (openmc.EnergyFilter, openmc.EnergyoutFilter)):
        values = list(f.values)
        return f"{len(values) - 1} bins, {values[0]:g}–{values[-1]:g} eV"
    if isinstance(f, openmc.MeshFilter):
        mesh = f.mesh
        dims = "×".join(str(d) for d in getattr(mesh, "dimension", ()) or ())
        return f"mesh {mesh.id} ({type(mesh).__name__} {dims})".replace(" )", ")")
    bins = list(_flatten(getattr(f, "bins", [])))
    if bins and all(isinstance(b, (int,)) or hasattr(b, "__index__") for b in bins):
        return _ids_text([int(b) for b in bins])
    return ""


GRID_AXES = {
    "rectilinear": ("x_grid", "y_grid", "z_grid"),
    "cylindrical": ("r_grid", "phi_grid", "z_grid"),
    "spherical": ("r_grid", "theta_grid", "phi_grid"),
}


def _grid_dimension(mesh: ET.Element) -> list[int]:
    if mesh.findtext("dimension"):
        return [int(x) for x in mesh.findtext("dimension", "").split()]
    axes = GRID_AXES.get(mesh.get("type", ""), ())
    return [len((mesh.findtext(tag) or "").split()) - 1 for tag in axes]


def weight_windows_from_xml(root: ET.Element) -> list[WeightWindowInfo]:
    """Weight windows read from the XML itself (OpenMC 0.16.0 cannot read some back)."""
    settings = root.find("settings") if root.tag == "model" else root
    if settings is None:
        return []
    meshes = {m.get("id"): m for m in settings.iter("mesh")}
    found = []
    for ww in settings.iter("weight_windows"):
        mesh_id = (ww.findtext("mesh") or "").strip()
        mesh = meshes.get(mesh_id)
        lower = [float(x) for x in (ww.findtext("lower_ww_bounds") or "").split()]
        positive = [x for x in lower if x > 0]
        found.append(
            WeightWindowInfo(
                id=int(ww.get("id", "0")),
                mesh_id=int(mesh_id or 0),
                mesh_kind=mesh.get("type", "regular") if mesh is not None else "",
                dimension=_grid_dimension(mesh) if mesh is not None else [],
                particle=(ww.findtext("particle_type") or "neutron").strip(),
                energy_bounds=[float(x) for x in (ww.findtext("energy_bounds") or "").split()],
                lower_min=min(positive) if positive else None,
                lower_max=max(positive) if positive else None,
            )
        )
    return found


def _load_model(path: Path, warnings: list[str]) -> Any:
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    try:
        return openmc.Model.from_model_xml(path)
    except Exception as exc:
        tree = ET.parse(path)
        settings = tree.getroot().find("settings")
        windows = [] if settings is None else settings.findall("weight_windows")
        if settings is None or not windows:
            raise
        # OpenMC 0.16.0 fails on weight windows without energy bounds: read the rest
        for element in windows:
            settings.remove(element)
        with tempfile.TemporaryDirectory() as tmp:
            stripped = Path(tmp) / "model.xml"
            tree.write(stripped, encoding="utf-8", xml_declaration=True)
            model = openmc.Model.from_model_xml(stripped)
        warnings.append(
            f"OpenMC's Python API could not read the weight windows ({type(exc).__name__}: "
            f"{exc}); they are listed from the XML."
        )
        return model


def _materials(model: Any) -> list[MaterialInfo]:
    out = []
    for m in model.materials:
        try:
            mass = float(m.get_mass_density())
        except Exception:
            mass = None
        try:
            atoms = float(sum(m.get_nuclide_atom_densities().values()))
        except Exception:
            atoms = None
        out.append(
            MaterialInfo(
                id=m.id,
                name=m.name or "",
                density=m.density,
                density_units=m.density_units or "",
                mass_density=mass,
                atom_density=atoms,
                temperature=m.temperature,
                nuclides=[
                    NuclideFraction(name=n.name, fraction=n.percent, kind=n.percent_type)
                    for n in m.nuclides
                ],
                sab=[name for name, *_ in getattr(m, "_sab", [])],
                depletable=bool(m.depletable),
            )
        )
    return out


def _first(value: Any) -> float | None:
    """A per-cell value, or the first one when it is given per instance."""
    values = list(_flatten(value))
    return float(values[0]) if values else None


def _max_level(geometry: Any) -> int:
    """Deepest universe level that holds cells (0: only the root universe's cells)."""
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    depth: dict[int, int] = {}

    def below(universe: Any) -> int:
        if universe.id in depth:
            return depth[universe.id]
        depth[universe.id] = 0  # a universe never contains itself; this ends any loop
        deepest = 0
        for cell in universe.cells.values():
            fill = cell.fill
            if isinstance(fill, openmc.Lattice):
                inner = {u.id: u for u in _flatten(fill.universes)}
                if fill.outer is not None:
                    inner[fill.outer.id] = fill.outer
                children = list(inner.values())
            elif isinstance(fill, openmc.UniverseBase):
                children = [fill]
            else:
                children = []
            for child in children:
                deepest = max(deepest, 1 + below(child))
        depth[universe.id] = deepest
        return deepest

    return below(geometry.root_universe)


def _geometry(model: Any) -> dict[str, Any]:
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    geometry = model.geometry
    universes = geometry.get_all_universes()
    lattices = geometry.get_all_lattices()
    cells = geometry.get_all_cells()
    cell_universe = {cid: uid for uid, u in universes.items() for cid in u.cells}
    parent_cells: dict[int, list[int]] = defaultdict(list)
    parent_lattices: dict[int, list[int]] = defaultdict(list)

    cell_infos = []
    for cid in sorted(cells):
        cell = cells[cid]
        fill = cell.fill
        info: dict[str, Any] = {"fill": None, "fills": []}
        if fill is None:
            kind = "void"
        elif isinstance(fill, openmc.Material):
            kind, info["fill"] = "material", fill.id
        elif isinstance(fill, openmc.Lattice):
            kind, info["fill"] = "lattice", fill.id
            parent_cells[fill.id].append(cid)
        elif isinstance(fill, openmc.UniverseBase):
            kind, info["fill"] = "universe", fill.id
            parent_cells[fill.id].append(cid)
        else:
            kind = "distributed"
            info["fills"] = [None if m is None else m.id for m in _flatten(fill)]
        translation = getattr(cell, "translation", None)
        rotation = getattr(cell, "rotation", None)
        cell_infos.append(
            CellInfo(
                id=cid,
                name=cell.name or "",
                universe=cell_universe.get(cid, geometry.root_universe.id),
                fill_type=kind,
                density=_first(getattr(cell, "density", None)),
                temperature=_first(cell.temperature),
                region=str(cell.region) if cell.region is not None else "",
                translation=None
                if translation is None
                else (float(translation[0]), float(translation[1]), float(translation[2])),
                rotation=None if rotation is None else [float(x) for x in _flatten(rotation)],
                **info,
            )
        )

    lattice_infos = []
    for lid in sorted(lattices):
        lat = lattices[lid]
        members: list[int] = []
        for u in _flatten(lat.universes):
            if u.id not in members:
                members.append(u.id)
        for uid in members:
            parent_lattices[uid].append(lid)
        outer = lat.outer.id if lat.outer is not None else None
        if outer is not None and lid not in parent_lattices[outer]:
            parent_lattices[outer].append(lid)
        is_rect = isinstance(lat, openmc.RectLattice)
        if is_rect:
            shape = [int(x) for x in lat.shape]
        else:
            shape = [int(lat.num_rings)] + ([int(lat.num_axial)] if lat.num_axial else [])
        lattice_infos.append(
            LatticeInfo(
                id=lid,
                name=lat.name or "",
                kind="rect" if is_rect else "hex",
                pitch=[float(x) for x in _flatten(lat.pitch)],
                lower_left=[float(x) for x in lat.lower_left] if is_rect else None,
                center=None if is_rect else [float(x) for x in lat.center],
                shape=shape,
                outer=outer,
                universes=members,
                parent_cells=parent_cells.get(lid, []),
            )
        )

    universe_infos = [
        UniverseInfo(
            id=uid,
            name=universes[uid].name or "",
            cells=sorted(universes[uid].cells),
            parent_cells=parent_cells.get(uid, []),
            parent_lattices=parent_lattices.get(uid, []),
        )
        for uid in sorted(universes)
    ]

    surfaces = geometry.get_all_surfaces()
    surface_infos = [
        SurfaceInfo(
            id=sid,
            type=getattr(surf, "_type", type(surf).__name__),
            coefficients={k: float(v) for k, v in surf.coefficients.items()},
            boundary=surf.boundary_type,
            name=surf.name or "",
        )
        for sid, surf in sorted(surfaces.items())
    ]
    return {
        "bounding_box": bounding_box(*geometry_bounds(geometry)),
        "root_universe": geometry.root_universe.id,
        "max_level": _max_level(geometry),
        "cells": cell_infos,
        "universes": universe_infos,
        "lattices": lattice_infos,
        "surfaces": surface_infos,
    }


def _settings(model: Any) -> SettingsInfo:
    s = model.settings
    sources = s.source if isinstance(s.source, list) else [s.source]
    source_infos = []
    for src in sources:
        if src is None:
            continue
        source_infos.append(
            SourceInfo(
                kind=type(src).__name__,
                space=_describe_distribution(getattr(src, "space", None)),
                energy=_describe_distribution(getattr(src, "energy", None)),
                angle=_describe_distribution(getattr(src, "angle", None)),
                particle=str(getattr(src, "particle", "neutron")),
                strength=float(getattr(src, "strength", 1.0) or 1.0),
            )
        )
    return SettingsInfo(
        run_mode=str(s.run_mode or ""),
        particles=s.particles,
        batches=s.batches,
        inactive=s.inactive,
        generations_per_batch=s.generations_per_batch,
        photon_transport=bool(s.photon_transport),
        seed=s.seed,
        weight_windows_on=bool(s.weight_windows_on),
        sources=source_infos,
    )


def _tallies(model: Any) -> list[TallyInfo]:
    return [
        TallyInfo(
            id=t.id,
            name=t.name or "",
            scores=[str(score) for score in t.scores],
            filters=[
                FilterInfo(kind=type(f).__name__, bins=int(f.num_bins), summary=_filter_summary(f))
                for f in t.filters
            ],
            nuclides=[str(n) for n in t.nuclides],
            estimator=t.estimator,
        )
        for t in model.tallies
    ]


def describe(path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    model = _load_model(path, warnings)
    summary = _geometry(model)
    summary["materials"] = _materials(model)
    summary["settings"] = _settings(model)
    summary["tallies"] = _tallies(model)
    summary["weight_windows"] = weight_windows_from_xml(ET.parse(path).getroot())
    summary["warnings"] = warnings
    return summary


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m pyvised_backend.modelinfo MODEL_XML", file=sys.stderr)
        return 2
    try:
        summary = describe(Path(argv[0]))
    except Exception:
        traceback.print_exc()
        return 1
    print(RESULT_MARKER + json.dumps(summary, default=_jsonable), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
