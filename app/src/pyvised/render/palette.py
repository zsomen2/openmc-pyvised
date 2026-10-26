"""Colours of the geometry views: the Vised material palette, stable cell and universe
colours, and the colour maps of the continuous modes (density, temperature, importance).

Materials are coloured in ascending id order, as in Vised: for the course
models M1 is blue (fuel), M2 yellow (water), M3 green (aluminium), M4 red
(graphite) and M5 cyan (concrete). The first four are the colours MCNP's
Vised draws. Magenta is kept for overlaps, so no palette colour and no colour
map is close to it.
"""

import colorsys
import functools
from collections.abc import Iterable

import numpy as np

RGB = tuple[int, int, int]

VISED: list[RGB] = [
    (43, 0, 255),  # blue
    (248, 255, 0),  # yellow
    (0, 255, 75),  # green
    (255, 0, 0),  # red
    (0, 255, 255),  # cyan
    (255, 165, 0),  # orange
    (75, 0, 130),  # indigo
    (150, 75, 0),  # brown
    (255, 150, 200),  # pink
    (64, 224, 208),  # turquoise
    (128, 128, 0),  # olive
    (100, 149, 237),  # cornflower blue
    (139, 0, 0),  # dark red
    (144, 238, 144),  # light green
    (0, 0, 128),  # navy
    (218, 165, 32),  # goldenrod
]

OVERLAP: RGB = (255, 0, 255)
OUTSIDE_LIGHT: RGB = (80, 80, 80)
OUTSIDE_DARK: RGB = (70, 70, 70)
VOID_LIGHT: RGB = (255, 255, 255)
VOID_DARK: RGB = (18, 18, 18)
LINE_LIGHT: RGB = (0, 0, 0)
LINE_DARK: RGB = (205, 205, 205)
NO_DATA_LIGHT: RGB = (200, 200, 200)
"""Inside the geometry but without a value (no weight window there)."""
NO_DATA_DARK: RGB = (110, 110, 110)

# continuous colour maps: anchors spaced evenly over [0, 1], interpolated to 256 colours
COLOR_MAPS: dict[str, list[RGB]] = {
    "viridis": [
        (68, 1, 84), (72, 40, 120), (62, 73, 137), (49, 104, 142), (38, 130, 142),
        (31, 158, 137), (53, 183, 121), (110, 206, 88), (181, 222, 43), (253, 231, 37),
    ],
    "thermal": [  # blue - yellow - red (ColorBrewer RdYlBu, reversed)
        (49, 54, 149), (69, 117, 180), (116, 173, 209), (171, 217, 233), (254, 224, 144),
        (253, 174, 97), (244, 109, 67), (215, 48, 39), (165, 0, 38),
    ],
}  # fmt: skip


@functools.cache
def color_map(name: str, size: int = 256) -> np.ndarray:
    """(size, 3) uint8 colours of a continuous colour map (shared: do not modify)."""
    anchors = np.array(COLOR_MAPS[name], dtype=float)
    positions = np.linspace(0.0, 1.0, len(anchors))
    t = np.linspace(0.0, 1.0, size)
    channels = [np.interp(t, positions, anchors[:, k]) for k in range(3)]
    return np.round(np.stack(channels, axis=1)).astype(np.uint8)


_GOLDEN = 0.618033988749895
_MAGENTA_HUE = 300 / 360


def spread_color(index: int, saturation: float = 0.8, value: float = 0.92) -> RGB:
    """Well-separated colours for any index, kept away from the magenta overlap colour."""
    hue = (index * _GOLDEN) % 1.0
    if abs(hue - _MAGENTA_HUE) < 0.06:
        hue = (hue + 0.12) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return round(r * 255), round(g * 255), round(b * 255)


def parse_hex(text: str) -> RGB | None:
    """'#rrggbb' -> (r, g, b); None for anything else."""
    text = text.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def material_colors(
    material_ids: Iterable[int], overrides: dict[int, str] | None = None
) -> dict[int, RGB]:
    """Material id -> colour: the Vised palette in ascending id order, then spread colours."""
    colors = {}
    for index, mid in enumerate(sorted(set(material_ids))):
        if index < len(VISED):
            colors[mid] = VISED[index]
        else:
            colors[mid] = spread_color(index - len(VISED))
    return overridden(colors, overrides)


def cell_color(cell_id: int) -> RGB:
    """A stable colour per cell id (the same cell keeps its colour across models and runs)."""
    return spread_color(cell_id * 7 + 3, saturation=0.65, value=0.95)


QUALITATIVE: list[RGB] = [  # Tableau 20 without its greys and pinks
    (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189),
    (140, 86, 75), (188, 189, 34), (23, 190, 207), (174, 199, 232), (255, 187, 120),
    (152, 223, 138), (255, 152, 150), (197, 176, 213), (196, 156, 148), (219, 219, 141),
    (158, 218, 229), (247, 182, 210),
]  # fmt: skip


def universe_color(universe_id: int) -> RGB:
    """A stable colour per universe id (for universes outside a model's list)."""
    return spread_color(universe_id * 5 + 11, saturation=0.55, value=0.92)


def universe_colors(universe_ids: Iterable[int]) -> dict[int, RGB]:
    """Universe id -> colour: well-separated colours in ascending id order."""
    colors = {}
    for index, uid in enumerate(sorted(set(universe_ids))):
        if index < len(QUALITATIVE):
            colors[uid] = QUALITATIVE[index]
        else:
            colors[uid] = spread_color(index - len(QUALITATIVE), saturation=0.55, value=0.92)
    return colors


def overridden(colors: dict[int, RGB], overrides: dict[int, str] | None) -> dict[int, RGB]:
    """`colors` with the valid '#rrggbb' overrides of a sidecar applied."""
    out = dict(colors)
    for key, text in (overrides or {}).items():
        rgb = parse_hex(text)
        if rgb is not None:
            out[key] = rgb
    return out
