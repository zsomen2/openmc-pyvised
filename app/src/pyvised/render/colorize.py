"""Turn slice ID maps into an RGBA image: colour mode, special values and boundary lines.

Everything works on the cached arrays, so switching the colour mode or the
lines needs no new slice from the backend.
"""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from pyvised_protocol.slices import OUTSIDE, OVERLAP, SliceData

from . import palette
from .palette import RGB

DENSE_LIMIT = 1 << 20
"""Ids below this are looked up in a dense table; larger ones go through np.unique."""


class ColorMode(StrEnum):
    MATERIAL = "material"
    CELL = "cell"
    NONE = "none"
    """Lines only, on the plot background (Vised's "no colour")."""


class LineMode(StrEnum):
    CELL_INSTANCE = "cell_instance"
    """Cell and instance change: outlines every lattice element, as Vised does."""
    CELL = "cell"
    MATERIAL = "material"


@dataclass(frozen=True)
class Style:
    mode: ColorMode = ColorMode.MATERIAL
    lines: bool = True
    line_mode: LineMode = LineMode.CELL_INSTANCE
    line_width: int = 1
    background: RGB = (255, 255, 255)
    line_color: RGB = palette.LINE_LIGHT
    void: RGB = palette.VOID_LIGHT
    outside: RGB = palette.OUTSIDE_LIGHT
    overlap: RGB = palette.OVERLAP

    @classmethod
    def for_background(
        cls, background: RGB, mode: ColorMode = ColorMode.MATERIAL, **kwargs
    ) -> "Style":
        """Colours for a light or dark plot background. Lines stay black over coloured
        regions (as in Vised); only lines on the bare background turn light on dark ones."""
        dark = sum(background) < 3 * 128
        light_lines = dark and mode is ColorMode.NONE
        return cls(
            mode=mode,
            background=background,
            line_color=palette.LINE_DARK if light_lines else palette.LINE_LIGHT,
            void=palette.VOID_DARK if dark else palette.VOID_LIGHT,
            outside=palette.OUTSIDE_DARK if dark else palette.OUTSIDE_LIGHT,
            **kwargs,
        )


def map_ids(ids: np.ndarray, colors: dict[int, RGB], default: RGB) -> np.ndarray:
    """(v, h) int ids -> (v, h, 3) uint8 colours; negative ids get `default`."""
    high = int(ids.max(initial=0))
    if high < DENSE_LIMIT:
        table = np.empty((high + 1, 3), dtype=np.uint8)
        table[:] = default
        for key, rgb in colors.items():
            if 0 <= key <= high:
                table[key] = rgb
        out = table[np.clip(ids, 0, high)]
        out[ids < 0] = default
        return out
    unique, inverse = np.unique(ids, return_inverse=True)
    table = np.array([colors.get(int(u), default) for u in unique], dtype=np.uint8)
    return table[inverse.reshape(ids.shape)]


def cell_colors(cell_ids: np.ndarray, overrides: dict[int, RGB] | None = None) -> dict[int, RGB]:
    colors = {int(c): palette.cell_color(int(c)) for c in np.unique(cell_ids) if c >= 0}
    colors.update(overrides or {})
    return colors


def boundaries(data: SliceData, line_mode: LineMode, width: int = 1) -> np.ndarray:
    """(v, h) bool mask of the pixels on a boundary between regions."""
    if line_mode is LineMode.MATERIAL:
        keys = [data.materials]
    elif line_mode is LineMode.CELL:
        keys = [data.cells]
    else:
        keys = [data.cells, data.instances]
    horizontal = np.zeros((data.shape[0], max(data.shape[1] - 1, 0)), dtype=bool)
    vertical = np.zeros((max(data.shape[0] - 1, 0), data.shape[1]), dtype=bool)
    for key in keys:
        horizontal |= key[:, 1:] != key[:, :-1]
        vertical |= key[1:, :] != key[:-1, :]
    edge = np.zeros(data.shape, dtype=bool)
    edge[:, 1:] |= horizontal
    edge[1:, :] |= vertical
    if width >= 2:
        edge[:, :-1] |= horizontal
        edge[:-1, :] |= vertical
    return edge


def colorize(
    data: SliceData,
    style: Style,
    material_colors: dict[int, RGB],
    cell_overrides: dict[int, RGB] | None = None,
) -> np.ndarray:
    """RGBA image (v, h, 4) of a slice; row 0 stays the top edge."""
    if style.mode is ColorMode.MATERIAL:
        rgb = map_ids(data.materials, material_colors, style.void)  # VOID gets the default
    elif style.mode is ColorMode.CELL:
        rgb = map_ids(data.cells, cell_colors(data.cells, cell_overrides), style.void)
    else:
        rgb = np.empty((*data.shape, 3), dtype=np.uint8)
        rgb[:] = style.background
    if style.mode is not ColorMode.NONE:
        rgb[data.materials == OUTSIDE] = style.outside
    rgb[data.materials == OVERLAP] = style.overlap
    if style.lines:
        rgb[boundaries(data, style.line_mode, style.line_width)] = style.line_color
    rgba = np.empty((*data.shape, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = 255
    return rgba
