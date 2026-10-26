"""Where to put cell, material or universe numbers on a slice, as Vised's labels do.

A region is a cell instance (cell id and instance), so every lattice element
gets its own label. Each label goes to the region's most interior pixel: the
distance to the nearest boundary is found by repeated erosion on a coarse copy
of the slice (pure numpy), and regions too small for text get no label.
"""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from pyvised_protocol.slices import SliceData

from .colorize import ColorTables, lookup

GRID = 360
"""Longest side of the coarse grid the distances are measured on."""
MAX_DEPTH = 40
"""Erosion steps: regions deeper than this all count as roomy."""


class LabelKind(StrEnum):
    NONE = "none"
    CELL = "cell"
    MATERIAL = "material"
    UNIVERSE = "universe"


@dataclass(frozen=True)
class Label:
    row: float
    col: float
    """Pixel position in the slice (row 0 = top), at the pixel's centre."""
    text: str
    room: float
    """Distance from the position to the region's boundary [slice pixels]."""


def _depth(keys: np.ndarray) -> np.ndarray:
    """Erosion depth of every pixel: 0 on a boundary, k when k steps keep it inside."""
    inside = np.ones(keys.shape, dtype=bool)
    same_h = keys[:, 1:] == keys[:, :-1]
    same_v = keys[1:, :] == keys[:-1, :]
    inside[:, 1:] &= same_h
    inside[:, :-1] &= same_h
    inside[1:, :] &= same_v
    inside[:-1, :] &= same_v
    inside[0, :] = inside[-1, :] = False
    inside[:, 0] = inside[:, -1] = False
    depth = inside.astype(np.int32)
    for _ in range(MAX_DEPTH - 1):
        eroded = inside.copy()
        eroded[1:, :] &= inside[:-1, :]
        eroded[:-1, :] &= inside[1:, :]
        eroded[:, 1:] &= inside[:, :-1]
        eroded[:, :-1] &= inside[:, 1:]
        if not eroded.any():
            break
        depth += eroded
        inside = eroded
    return depth


def place_labels(
    data: SliceData, kind: LabelKind, tables: ColorTables, limit: int = 400
) -> list[Label]:
    """Labels for the regions of a slice, roomiest first, at most `limit`."""
    if kind is LabelKind.NONE:
        return []
    rows, cols = data.shape
    step = max(1, int(np.ceil(max(rows, cols) / GRID)))
    cells = data.cells[::step, ::step].astype(np.int64)
    instances = data.instances[::step, ::step].astype(np.int64)
    materials = data.materials[::step, ::step]
    if kind is LabelKind.UNIVERSE and data.level_cells is not None:
        # universes at the slice's level: one label per cell instance of that level
        assert data.level_instances is not None
        level_cells = data.level_cells[::step, ::step].astype(np.int64)
        level_instances = data.level_instances[::step, ::step].astype(np.int64)
        nested = level_cells >= 0
        cells = np.where(nested, level_cells, cells)
        instances = np.where(nested, level_instances, instances)
    keys = (cells << 32) | (instances & 0xFFFFFFFF)
    valid = cells >= 0
    if kind is LabelKind.MATERIAL:
        valid &= materials >= 0
    depth = np.where(valid, _depth(keys), 0)
    flat_keys, flat_depth = keys.ravel(), depth.ravel()
    candidates = np.flatnonzero(flat_depth > 0)
    if candidates.size == 0:
        return []
    # among a region's deepest pixels, prefer the one nearest its centroid (long strips)
    _, group = np.unique(flat_keys[candidates], return_inverse=True)
    r_all, c_all = np.divmod(candidates, cells.shape[1])
    counts = np.bincount(group)
    r_mean = np.bincount(group, weights=r_all) / counts
    c_mean = np.bincount(group, weights=c_all) / counts
    distance = np.hypot(r_all - r_mean[group], c_all - c_mean[group])
    score = flat_depth[candidates] - 1e-4 * distance
    order = candidates[np.lexsort((score, group))]
    sorted_groups = np.sort(group)
    last = np.flatnonzero(np.append(sorted_groups[1:] != sorted_groups[:-1], True))
    best = order[last]  # the best pixel of each region
    best = best[np.argsort(-flat_depth[best], kind="stable")][:limit]
    r, c = np.divmod(best, cells.shape[1])
    cell_ids = cells.ravel()[best]
    if kind is LabelKind.CELL:
        values = cell_ids
    elif kind is LabelKind.MATERIAL:
        values = materials.ravel()[best]
    else:
        values = lookup(cell_ids, tables.cell_universe, -1, np.int64)
    return [
        Label(
            row=min(rows - 1, rr * step + (step - 1) / 2),
            col=min(cols - 1, cc * step + (step - 1) / 2),
            text=str(v),
            room=float(d * step),
        )
        for rr, cc, v, d in zip(
            r.tolist(), c.tolist(), values.tolist(), flat_depth[best].tolist(), strict=True
        )
        if v >= 0
    ]
