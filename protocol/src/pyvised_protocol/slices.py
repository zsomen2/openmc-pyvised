"""Geometry slices as binary frames (the answer of `POST /models/{id}/slice`).

A slice holds three int32 maps of shape (vertical, horizontal) taken from
``openmc.lib.slice_data``: cell id, cell instance and material id per pixel,
always for the deepest cells. Row 0 is the top edge (+v). Each map is a
separate array in the frame, which compresses better than the interleaved
channels. Optional layers follow when the request asks for them:

- ``level_cells``, ``level_instances``: the cells at one universe level
  (`OUTSIDE` where the geometry is not nested that deep);
- ``temperature`` [K] (float32; plot mode loads no nuclear data, so its density
  channel is not usable and is not sent);
- ``ww_lower``: the weight-window lower bound (float32, NaN without a window).
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import frames

OUTSIDE = -2
"""Every channel outside the geometry (no cell found)."""
VOID = -1
"""Material channel of a void cell."""
OVERLAP = -3
"""Instance and material channel where cells overlap (with ``show_overlaps``); the cell
channel then holds -(number of overlapping cells + 2)."""


@dataclass
class SliceData:
    cells: np.ndarray
    instances: np.ndarray
    materials: np.ndarray
    temperature: np.ndarray | None = None
    level_cells: np.ndarray | None = None
    level_instances: np.ndarray | None = None
    ww_lower: np.ndarray | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        """(vertical, horizontal) pixels."""
        return self.cells.shape[0], self.cells.shape[1]


def encode_slice(
    ids: np.ndarray,
    properties: np.ndarray | None = None,
    meta: dict[str, Any] | None = None,
    *,
    level_ids: np.ndarray | None = None,
    ww_lower: np.ndarray | None = None,
) -> bytes:
    """Frame from `slice_data` output: `ids` (v, h, 3) int32, `properties` (v, h, 2) or None,
    `level_ids` (v, h, 3) of a slice at one universe level, `ww_lower` (v, h)."""
    arrays: dict[str, np.ndarray] = {
        "cells": ids[..., 0].astype(np.int32),
        "instances": ids[..., 1].astype(np.int32),
        "materials": ids[..., 2].astype(np.int32),
    }
    if properties is not None:
        arrays["temperature"] = properties[..., 0].astype(np.float32)
    if level_ids is not None:
        arrays["level_cells"] = level_ids[..., 0].astype(np.int32)
        arrays["level_instances"] = level_ids[..., 1].astype(np.int32)
    if ww_lower is not None:
        arrays["ww_lower"] = ww_lower.astype(np.float32)
    return frames.encode(arrays, meta)


def decode_slice(data: bytes) -> SliceData:
    frame = frames.decode(data)
    a = frame.arrays
    return SliceData(
        cells=a["cells"],
        instances=a["instances"],
        materials=a["materials"],
        temperature=a.get("temperature"),
        level_cells=a.get("level_cells"),
        level_instances=a.get("level_instances"),
        ww_lower=a.get("ww_lower"),
        meta=frame.meta,
    )
