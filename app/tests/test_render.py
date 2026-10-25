"""Palette, colouriser and view maths on synthetic ID maps (no Qt, no backend)."""

import numpy as np
import pytest

from pyvised.render import palette
from pyvised.render.colorize import (
    ColorMode,
    LineMode,
    Style,
    boundaries,
    colorize,
    map_ids,
)
from pyvised.render.views import (
    Window,
    device_pixels,
    nice_step,
    pixel_index,
    plane_extent,
    slice_request,
    world_point,
)
from pyvised_protocol.schemas import BoundingBox
from pyvised_protocol.slices import OUTSIDE, OVERLAP, VOID, SliceData

WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def slice_of(cells, instances=None, materials=None) -> SliceData:
    cells = np.asarray(cells, dtype=np.int32)
    return SliceData(
        cells=cells,
        instances=np.zeros_like(cells) if instances is None else np.asarray(instances, np.int32),
        materials=cells.copy() if materials is None else np.asarray(materials, np.int32),
    )


def test_vised_order_follows_sorted_material_ids():
    colors = palette.material_colors([5, 1, 3, 2, 4])
    assert colors[1] == (43, 0, 255)  # M1 blue
    assert colors[2] == (248, 255, 0)  # M2 yellow
    assert colors[3] == (0, 255, 75)  # M3 green
    assert colors[4] == (255, 0, 0)  # M4 red
    assert colors[5] == (0, 255, 255)  # M5 cyan
    sparse = palette.material_colors([10, 200])
    assert sparse[10] == palette.VISED[0] and sparse[200] == palette.VISED[1]


def test_overrides_and_extra_colours():
    colors = palette.material_colors([1, 2], {2: "#123456", 1: "not a colour"})
    assert colors[2] == (0x12, 0x34, 0x56) and colors[1] == palette.VISED[0]
    many = palette.material_colors(range(1, 60))
    assert len(set(many.values())) > 50
    assert palette.OVERLAP not in many.values()
    assert palette.to_hex(palette.parse_hex("#a0b1c2") or BLACK) == "#a0b1c2"


def test_map_ids_dense_and_sparse_paths():
    ids = np.array([[1, 2], [-1, 7]], dtype=np.int32)
    out = map_ids(ids, {1: (1, 1, 1), 2: (2, 2, 2)}, WHITE)
    assert out.tolist() == [[[1, 1, 1], [2, 2, 2]], [list(WHITE), list(WHITE)]]
    huge = np.array([[5_000_000, 1]], dtype=np.int32)
    out = map_ids(huge, {5_000_000: (9, 9, 9)}, WHITE)
    assert out.tolist() == [[[9, 9, 9], list(WHITE)]]


def test_boundaries_by_cell_instance_cell_and_material():
    # one cell (7) in two lattice instances, next to cell 8 of the same material
    cells = [[7, 7, 7, 8]]
    instances = [[0, 0, 1, 0]]
    materials = [[1, 1, 1, 1]]
    data = slice_of(cells, instances, materials)
    assert boundaries(data, LineMode.CELL_INSTANCE)[0].tolist() == [False, False, True, True]
    assert boundaries(data, LineMode.CELL)[0].tolist() == [False, False, False, True]
    assert not boundaries(data, LineMode.MATERIAL).any()
    assert boundaries(data, LineMode.CELL, width=2)[0].tolist() == [False, False, True, True]


def test_colorize_special_values_and_lines():
    cells = [[1, 1, 2, -4, OUTSIDE]]
    materials = [[1, 1, VOID, OVERLAP, OUTSIDE]]
    data = slice_of(cells, materials=materials)
    colors = {1: (10, 20, 30)}
    style = Style(lines=False)
    rgba = colorize(data, style, colors)
    assert rgba.shape == (1, 5, 4) and (rgba[..., 3] == 255).all()
    assert rgba[0, 0, :3].tolist() == [10, 20, 30]
    assert rgba[0, 2, :3].tolist() == list(style.void)
    assert rgba[0, 3, :3].tolist() == list(palette.OVERLAP)
    assert rgba[0, 4, :3].tolist() == list(style.outside)
    lined = colorize(data, Style(lines=True, line_mode=LineMode.CELL), colors)
    assert lined[0, 2, :3].tolist() == list(BLACK), "boundary between cell 1 and 2"
    assert lined[0, 1, :3].tolist() == [10, 20, 30]


def test_lines_only_and_cell_modes():
    data = slice_of([[1, 1, 2, 2]], materials=[[3, 3, 3, OUTSIDE]])
    lines_only = colorize(data, Style(mode=ColorMode.NONE), {3: (1, 2, 3)})
    assert lines_only[0, 0, :3].tolist() == list(WHITE)
    assert lines_only[0, 2, :3].tolist() == list(BLACK)
    assert lines_only[0, 3, :3].tolist() == list(WHITE), "lines only: no grey outside"
    by_cell = colorize(data, Style(mode=ColorMode.CELL, lines=False), {})
    assert by_cell[0, 0, :3].tolist() == list(palette.cell_color(1))
    assert by_cell[0, 0, :3].tolist() != by_cell[0, 2, :3].tolist()


def test_dark_background_style():
    style = Style.for_background((30, 30, 30))
    assert style.void == palette.VOID_DARK and style.outside == palette.OUTSIDE_DARK
    assert style.line_color == palette.LINE_LIGHT, "black lines over material colours"
    lines_only = Style.for_background((30, 30, 30), mode=ColorMode.NONE)
    assert lines_only.line_color == palette.LINE_DARK
    assert Style.for_background(WHITE, mode=ColorMode.NONE).line_color == palette.LINE_LIGHT


@pytest.mark.parametrize(
    ("basis", "expected"),
    [("xy", (1.0, 2.0, 3.0)), ("xz", (1.0, 3.0, 2.0)), ("yz", (3.0, 1.0, 2.0))],
)
def test_world_point_per_basis(basis, expected):
    assert world_point(basis, 1.0, 2.0, 3.0) == expected


def test_plane_extent_and_request():
    box = BoundingBox(lower_left=(-1, -2, -3), upper_right=(1, 2, 3))
    assert plane_extent(box, "xz") == ((-1, 1), (-3, 3), (-2, 2))
    request = slice_request("xz", Window(-1.0, -3.0, 2.0, 6.0), 0.5, (200, 600))
    assert request.origin == (0.0, 0.5, 0.0) and request.width == (2.0, 6.0)
    assert request.pixels == (200, 600) and request.basis == "xz"


def test_pixel_index_row_zero_is_the_top():
    window = Window(0.0, 0.0, 10.0, 5.0)
    assert pixel_index(window, (5, 10), 0.5, 4.5) == (0, 0)
    assert pixel_index(window, (5, 10), 9.5, 0.5) == (4, 9)
    assert pixel_index(window, (5, 10), 10.0, 0.0) == (4, 9)
    assert pixel_index(window, (5, 10), 11.0, 1.0) is None


def test_device_pixels_and_steps():
    assert device_pixels(500, 400, 1.5, 1.0) == (750, 600)
    assert device_pixels(500, 400, 1.0, 0.4) == (200, 160)
    w, h = device_pixels(10_000, 5_000, 1.0, 1.0, limit=4096)
    assert w == 4096 and h == 2048
    assert nice_step(240) == 0.5 and nice_step(64.8) == 0.1 and nice_step(1.0) == 0.001
