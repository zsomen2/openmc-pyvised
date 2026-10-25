"""Colours of the geometry views: the Vised material palette and stable cell colours.

Materials are coloured in ascending id order, as in Vised: for the course
models M1 is blue (fuel), M2 yellow (water), M3 green (aluminium), M4 red
(graphite) and M5 cyan (concrete). The first four are the colours MCNP's
Vised draws. Magenta is kept for overlaps, so no palette colour is close to it.
"""

import colorsys
from collections.abc import Iterable

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
    for mid, text in (overrides or {}).items():
        rgb = parse_hex(text)
        if rgb is not None:
            colors[mid] = rgb
    return colors


def cell_color(cell_id: int) -> RGB:
    """A stable colour per cell id (the same cell keeps its colour across models and runs)."""
    return spread_color(cell_id * 7 + 3, saturation=0.65, value=0.95)
