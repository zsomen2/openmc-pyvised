"""What lies at a point: the nesting path from the root universe down (``inspect``).

Runs in a render worker next to ``openmc.lib``, but walks the geometry with the
OpenMC Python API: at every level it lists the cells of the universe that
contain the point (more than one is an overlap), follows the fill (translation,
rotation, lattice element) and keeps the point's local coordinates.
"""

from pathlib import Path
from typing import Any

import numpy as np

from pyvised_protocol.schemas import InspectResult, PathStep

MAX_LEVELS = 64


def load_model(model_xml: Path) -> Any:
    """The model, also when OpenMC cannot read its weight windows back (see modelinfo)."""
    from .modelinfo import _load_model

    return _load_model(model_xml, [])


def _first(value: Any) -> float | None:
    values = np.ravel(np.asarray(value, dtype=float)) if value is not None else []
    return float(values[0]) if len(values) else None


def inspect_point(
    model: Any, point: tuple[float, float, float], instance: int | None
) -> InspectResult:
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    p = np.asarray(point, dtype=float)
    universe = model.geometry.root_universe
    path: list[PathStep] = []
    cell = None
    for level in range(MAX_LEVELS):
        inside = [c for c in universe.cells.values() if c.region is None or tuple(p) in c.region]
        if not inside:
            cell = None
            break
        cell = inside[0]
        step = PathStep(
            level=level,
            universe=universe.id,
            cell=cell.id,
            cell_name=cell.name or "",
            local=(float(p[0]), float(p[1]), float(p[2])),
            overlapping=[c.id for c in inside[1:]],
        )
        path.append(step)
        fill = cell.fill
        if not isinstance(fill, (openmc.UniverseBase, openmc.Lattice)):
            break
        if cell.translation is not None:
            p = p - np.asarray(cell.translation, dtype=float)
        if cell.rotation is not None:
            p = np.asarray(cell.rotation_matrix) @ p
        if isinstance(fill, openmc.Lattice):
            index, p = fill.find_element(p)
            step.lattice, step.lattice_index = fill.id, [int(i) for i in index]
            universe = fill.get_universe(index) if fill.is_valid_index(index) else fill.outer
            if universe is None:
                cell = None
                break
        else:
            universe = fill
    result = InspectResult(point=point, found=cell is not None, path=path, instance=instance)
    if cell is None:
        return result
    material: Any = cell.fill
    if isinstance(material, (list, tuple)):  # distributed materials
        material = material[instance] if instance is not None and instance < len(material) else None
    if isinstance(material, openmc.Material):
        result.material = material.id
        result.material_name = material.name or ""
        density = _first(cell.density)
        if density is None:
            try:
                density = float(material.get_mass_density())
            except Exception:
                density = None
        result.density = density
        result.temperature = _first(cell.temperature)
        if result.temperature is None and material.temperature is not None:
            result.temperature = float(material.temperature)
    elif cell.temperature is not None:
        result.temperature = _first(cell.temperature)
    return result
