"""Turn slice ID maps into an RGBA image: colour mode, special values, highlight and lines.

Everything works on the cached arrays, so switching between the material, cell,
universe and density modes, the lines or the highlight needs no new slice from
the backend. The universe level, the temperature and the weight windows are
extra layers of a slice; their modes ask for them once.
"""

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from pyvised_protocol.schemas import ModelInfo
from pyvised_protocol.slices import OUTSIDE, OVERLAP, VOID, SliceData

from . import palette
from .palette import RGB
from .scales import Scale

DENSE_LIMIT = 1 << 20
"""Ids below this are looked up in a dense table; larger ones go through np.unique."""
DEFAULT_TEMPERATURE = 293.6
"""[K] OpenMC's default temperature, used where the model sets none."""


class ColorMode(StrEnum):
    MATERIAL = "material"
    CELL = "cell"
    UNIVERSE = "universe"
    DENSITY = "density"
    TEMPERATURE = "temperature"
    IMPORTANCE = "importance"
    """Weight windows: importance = lower bound of importance 1 / lower bound."""
    NONE = "none"
    """Lines only, on the plot background (Vised's "no colour")."""

    @property
    def continuous(self) -> bool:
        return self in (ColorMode.DENSITY, ColorMode.TEMPERATURE, ColorMode.IMPORTANCE)


COLOR_MAP_OF = {
    ColorMode.DENSITY: "viridis",
    ColorMode.TEMPERATURE: "thermal",
    ColorMode.IMPORTANCE: "viridis",
}


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
    no_data: RGB = palette.NO_DATA_LIGHT
    fade: float = 0.0
    """Share of the background mixed into everything (0 … 1), e.g. under particle tracks."""

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
            no_data=palette.NO_DATA_DARK if dark else palette.NO_DATA_LIGHT,
            **kwargs,
        )


@dataclass
class ColorTables:
    """Everything the colouriser needs to know about a model, built once per model."""

    materials: dict[int, RGB] = field(default_factory=dict)
    cell_overrides: dict[int, RGB] = field(default_factory=dict)
    universes: dict[int, RGB] = field(default_factory=dict)
    cell_universe: dict[int, int] = field(default_factory=dict)
    cell_density: dict[int, float] = field(default_factory=dict)
    """[g/cm³] of every material cell: its `Cell.density`, else its material's."""
    scales: dict[ColorMode, Scale] = field(default_factory=dict)
    importance_lower_bound: float = 0.5

    @classmethod
    def for_model(cls, info: ModelInfo) -> "ColorTables":
        sidecar = info.sidecar
        materials = palette.material_colors(
            [m.id for m in info.materials], sidecar.material_colors if sidecar else None
        )
        cell_overrides = palette.overridden({}, sidecar.cell_colors if sidecar else None)
        universes = palette.overridden(
            palette.universe_colors(u.id for u in info.universes),
            sidecar.universe_colors if sidecar else None,
        )
        mass = {m.id: m.mass_density for m in info.materials}
        cell_density: dict[int, float] = {}
        for cell in info.cells:
            fill = cell.fill if cell.fill_type == "material" else None
            if cell.fill_type == "distributed":
                fill = next((m for m in cell.fills if m is not None), None)
            if fill is None:
                continue
            value = cell.density if cell.density is not None else mass.get(fill)
            if value is not None:
                cell_density[cell.id] = value
        lower_bound = sidecar.importance_lower_bound if sidecar else 0.5
        temperatures = [DEFAULT_TEMPERATURE]
        temperatures += [m.temperature for m in info.materials if m.temperature is not None]
        temperatures += [c.temperature for c in info.cells if c.temperature is not None]
        scales: dict[ColorMode, Scale] = {}
        for mode, scale in (
            (ColorMode.DENSITY, Scale.fit(v for v in cell_density.values() if v > 0)),
            (ColorMode.TEMPERATURE, Scale.fit(temperatures, log=False)),
            (
                ColorMode.IMPORTANCE,
                Scale.fit(
                    [
                        lower_bound / w
                        for ww in info.weight_windows
                        for w in (ww.lower_min, ww.lower_max)
                        if w is not None and w > 0
                    ],
                    log=True,
                ),
            ),
        ):
            if scale is not None:
                scales[mode] = scale
        return cls(
            materials=materials,
            cell_overrides=cell_overrides,
            universes=universes,
            cell_universe={c.id: c.universe for c in info.cells},
            cell_density=cell_density,
            scales=scales,
            importance_lower_bound=lower_bound,
        )


@dataclass(frozen=True)
class Highlight:
    """Pixels to keep bright: deepest cells or materials; the rest is dimmed."""

    cells: frozenset[int] = frozenset()
    materials: frozenset[int] = frozenset()

    def mask(self, data: SliceData) -> np.ndarray:
        mask = np.zeros(data.shape, dtype=bool)
        if self.cells:
            mask |= np.isin(data.cells, np.fromiter(self.cells, dtype=np.int64))
        if self.materials:
            mask |= np.isin(data.materials, np.fromiter(self.materials, dtype=np.int64))
        return mask


def lookup(ids: np.ndarray, mapping: dict, default, dtype) -> np.ndarray:
    """Per-pixel `mapping[id]`; ids missing from it (and negative ids) get `default`."""
    high = int(ids.max(initial=0))
    if high < DENSE_LIMIT:
        table = np.full(high + 1, default, dtype=dtype)
        for key, value in mapping.items():
            if 0 <= key <= high:
                table[key] = value
        out = table[np.clip(ids, 0, high)]
        out[ids < 0] = default
        return out
    unique, inverse = np.unique(ids, return_inverse=True)
    table = np.array([mapping.get(int(u), default) for u in unique], dtype=dtype)
    return table[inverse.reshape(ids.shape)]


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


def universe_ids(data: SliceData, tables: ColorTables) -> np.ndarray:
    """Universe per pixel: of the cell at the slice's universe level where the geometry is
    nested that deep, else of the deepest cell; -1 outside and in overlaps."""
    cells = data.cells
    if data.level_cells is not None:
        cells = np.where(data.level_cells >= 0, data.level_cells, data.cells)
    return lookup(cells, tables.cell_universe, -1, np.int64)


def pixel_values(mode: ColorMode, data: SliceData, tables: ColorTables) -> np.ndarray | None:
    """The quantity a continuous mode shows (NaN: no value); None when the slice lacks it."""
    if mode is ColorMode.DENSITY:
        return lookup(data.cells, tables.cell_density, np.nan, np.float64)
    if mode is ColorMode.TEMPERATURE:
        if data.temperature is None:
            return None
        values = data.temperature.astype(np.float64)
        values[data.materials == OUTSIDE] = np.nan
        return values
    if mode is ColorMode.IMPORTANCE:
        if data.ww_lower is None:
            return None
        with np.errstate(divide="ignore", invalid="ignore"):
            return tables.importance_lower_bound / data.ww_lower.astype(np.float64)
    return None


def shade(values: np.ndarray, scale: Scale | None, colors: np.ndarray, no_data: RGB) -> np.ndarray:
    """Values -> colours of a colour map; NaN (and everything without a scale) -> `no_data`."""
    rgb = np.empty((*values.shape, 3), dtype=np.uint8)
    rgb[:] = no_data
    if scale is None:
        return rgb
    t = scale.normalise(values)
    known = np.isfinite(t)
    index = np.round(t[known] * (len(colors) - 1)).astype(np.intp)
    rgb[known] = colors[index]
    return rgb


def mode_ids(mode: ColorMode, data: SliceData, tables: ColorTables) -> np.ndarray | None:
    """The ids a discrete mode colours by (material, cell or universe), else None."""
    if mode is ColorMode.MATERIAL:
        return data.materials
    if mode is ColorMode.CELL:
        return data.cells
    if mode is ColorMode.UNIVERSE:
        return universe_ids(data, tables)
    return None


def id_counts(ids: np.ndarray) -> dict[int, int]:
    """Pixel count per non-negative id."""
    valid = ids[ids >= 0]
    if valid.size == 0:
        return {}
    if int(valid.max()) < DENSE_LIMIT:
        counts = np.bincount(valid.ravel())
        present = np.flatnonzero(counts)
        return dict(zip(present.tolist(), counts[present].tolist(), strict=True))
    unique, counts = np.unique(valid, return_counts=True)
    return dict(zip(unique.tolist(), counts.tolist(), strict=True))


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


def fade(rgb: np.ndarray, background: RGB, share: float) -> np.ndarray:
    """Mix `share` (0 … 1, in 16ths) of the background into the colours."""
    weight = min(16, max(0, round(share * 16)))
    mixed = rgb.astype(np.uint16) * (16 - weight)
    mixed += np.array(background, dtype=np.uint16) * weight
    return (mixed >> 4).astype(np.uint8)


def colorize(
    data: SliceData,
    style: Style,
    tables: ColorTables,
    highlight: Highlight | None = None,
) -> np.ndarray:
    """RGBA image (v, h, 4) of a slice; row 0 stays the top edge."""
    mode = style.mode
    if mode is ColorMode.MATERIAL:
        rgb = map_ids(data.materials, tables.materials, style.void)  # VOID gets the default
    elif mode is ColorMode.CELL:
        rgb = map_ids(data.cells, cell_colors(data.cells, tables.cell_overrides), style.void)
    elif mode is ColorMode.UNIVERSE:
        rgb = map_ids(universe_ids(data, tables), tables.universes, style.no_data)
    elif mode.continuous:
        values = pixel_values(mode, data, tables)
        colors = palette.color_map(COLOR_MAP_OF[mode])
        if values is None:
            rgb = np.empty((*data.shape, 3), dtype=np.uint8)
            rgb[:] = style.no_data
        else:
            rgb = shade(values, tables.scales.get(mode), colors, style.no_data)
        if mode is ColorMode.DENSITY:
            rgb[data.materials == VOID] = style.void
    else:
        rgb = np.empty((*data.shape, 3), dtype=np.uint8)
        rgb[:] = style.background
    if mode is not ColorMode.NONE:
        rgb[data.materials == OUTSIDE] = style.outside
    rgb[data.materials == OVERLAP] = style.overlap
    if highlight is not None:
        dim = ~highlight.mask(data)
        background = np.array(style.background, dtype=np.uint16)
        rgb[dim] = ((rgb[dim].astype(np.uint16) * 3 + background * 7) // 10).astype(np.uint8)
    if style.lines:
        rgb[boundaries(data, style.line_mode, style.line_width)] = style.line_color
    if style.fade > 0:
        rgb = fade(rgb, style.background, style.fade)
    rgba = np.empty((*data.shape, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = 255
    return rgba
