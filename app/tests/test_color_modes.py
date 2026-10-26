"""Universe, density, temperature and importance modes, highlight, scales and labels."""

import numpy as np
from test_render import slice_of

from pyvised.render import palette
from pyvised.render.colorize import (
    ColorMode,
    ColorTables,
    Highlight,
    Style,
    colorize,
    id_counts,
    universe_ids,
)
from pyvised.render.labels import LabelKind, place_labels
from pyvised.render.scales import Scale, format_value
from pyvised_protocol.schemas import (
    BoundingBox,
    CellInfo,
    MaterialInfo,
    ModelInfo,
    UniverseInfo,
    WeightWindowInfo,
)
from pyvised_protocol.sidecar import Sidecar
from pyvised_protocol.slices import OUTSIDE, VOID, SliceData


def tables_for_model() -> ColorTables:
    """Cells 1 (fuel, material 1) and 2 (water, material 2, Cell.density 0.5) in universe 4,
    which fills cell 10 of the root universe 0; cell 3 is void."""
    info = ModelInfo(
        model_id="m",
        source="m.py",
        kind="python",
        bounding_box=BoundingBox(lower_left=(-1, -1, -1), upper_right=(1, 1, 1)),
        root_universe=0,
        materials=[
            MaterialInfo(id=1, name="fuel", mass_density=10.0),
            MaterialInfo(id=2, name="water", mass_density=1.0),
        ],
        cells=[
            CellInfo(id=1, universe=4, fill_type="material", fill=1),
            CellInfo(id=2, universe=4, fill_type="material", fill=2, density=0.5),
            CellInfo(id=3, universe=4, fill_type="void"),
            CellInfo(id=10, universe=0, fill_type="universe", fill=4),
        ],
        universes=[UniverseInfo(id=0, cells=[10]), UniverseInfo(id=4, cells=[1, 2, 3])],
        weight_windows=[
            WeightWindowInfo(id=1, mesh_id=1, mesh_kind="regular", lower_min=0.125, lower_max=0.5)
        ],
        sidecar=Sidecar(universe_colors={4: "#102030"}),
    )
    return ColorTables.for_model(info)


def test_tables_from_model_info():
    tables = tables_for_model()
    assert tables.cell_universe == {1: 4, 2: 4, 3: 4, 10: 0}
    assert tables.cell_density == {1: 10.0, 2: 0.5}, "Cell.density wins over the material's"
    assert tables.universes[4] == (0x10, 0x20, 0x30)
    assert tables.universes[0] == palette.QUALITATIVE[0], "first in id order"
    assert tables.scales[ColorMode.DENSITY] == Scale(0.5, 10.0, log=False)
    assert tables.scales[ColorMode.IMPORTANCE] == Scale(1.0, 4.0, log=True)
    assert tables.importance_lower_bound == 0.5


def test_universe_mode_uses_the_level_layer_where_it_reaches():
    tables = tables_for_model()
    data = slice_of([[1, 2, 3, OUTSIDE]], materials=[[1, 2, VOID, OUTSIDE]])
    assert universe_ids(data, tables).tolist() == [[4, 4, 4, -1]]
    data.level_cells = np.array([[10, 10, OUTSIDE, OUTSIDE]], dtype=np.int32)
    assert universe_ids(data, tables).tolist() == [[0, 0, 4, -1]]
    rgba = colorize(data, Style(mode=ColorMode.UNIVERSE, lines=False), tables)
    assert rgba[0, 0, :3].tolist() == list(palette.QUALITATIVE[0])
    assert rgba[0, 2, :3].tolist() == [0x10, 0x20, 0x30]


def test_density_and_importance_modes():
    tables = tables_for_model()
    data = slice_of([[1, 2, 3, OUTSIDE]], materials=[[1, 2, VOID, OUTSIDE]])
    style = Style(mode=ColorMode.DENSITY, lines=False)
    rgba = colorize(data, style, tables)
    viridis = palette.color_map("viridis")
    assert rgba[0, 0, :3].tolist() == viridis[-1].tolist(), "10 g/cm³: top of the scale"
    assert rgba[0, 1, :3].tolist() == viridis[0].tolist(), "0.5 g/cm³: bottom"
    assert rgba[0, 2, :3].tolist() == list(style.void)
    assert rgba[0, 3, :3].tolist() == list(style.outside)
    # importance = 0.5 / lower bound; no window -> no data
    data.ww_lower = np.array([[0.5, 0.125, np.nan, np.nan]], dtype=np.float32)
    rgba = colorize(data, Style(mode=ColorMode.IMPORTANCE, lines=False), tables)
    assert rgba[0, 0, :3].tolist() == viridis[0].tolist()
    assert rgba[0, 1, :3].tolist() == viridis[-1].tolist()
    assert rgba[0, 2, :3].tolist() == list(Style().no_data)


def test_temperature_mode_waits_for_its_layer():
    tables = tables_for_model()
    data = slice_of([[1, 2, 3, OUTSIDE]], materials=[[1, 2, VOID, OUTSIDE]])
    style = Style(mode=ColorMode.TEMPERATURE, lines=False)
    assert colorize(data, style, tables)[0, 0, :3].tolist() == list(style.no_data)
    data.temperature = np.array([[293.6, 600.0, 293.6, -2.0]], dtype=np.float32)
    tables.scales[ColorMode.TEMPERATURE] = Scale(293.6, 600.0)
    rgba = colorize(data, style, tables)
    thermal = palette.color_map("thermal")
    assert rgba[0, 0, :3].tolist() == thermal[0].tolist()
    assert rgba[0, 1, :3].tolist() == thermal[-1].tolist()
    assert rgba[0, 3, :3].tolist() == list(style.outside)


def test_highlight_dims_everything_else():
    tables = ColorTables(materials={1: (200, 0, 0), 2: (0, 200, 0)})
    data = slice_of([[1, 2]], materials=[[1, 2]])
    style = Style(lines=False)
    rgba = colorize(data, style, tables, Highlight(materials=frozenset({1})))
    assert rgba[0, 0, :3].tolist() == [200, 0, 0]
    assert rgba[0, 1, :3].tolist() == [178, 238, 178], "30 % colour, 70 % white background"
    rgba = colorize(data, style, tables, Highlight(cells=frozenset({2})))
    assert rgba[0, 1, :3].tolist() == [0, 200, 0]


def test_id_counts():
    assert id_counts(np.array([[3, 3, -2], [7, 3, -1]])) == {3: 3, 7: 1}
    assert id_counts(np.array([[5_000_000, 1]])) == {1: 1, 5_000_000: 1}
    assert id_counts(np.array([[-2]])) == {}


def test_scales_and_ticks():
    linear = Scale.fit([1.0, 2.7, 5.3])
    assert linear == Scale(1.0, 5.3, log=False)
    assert linear.ticks() == [1.0, 2.0, 3.0, 4.0, 5.0]
    log = Scale.fit([1e-3, 7.85])
    assert log is not None and log.log and log.ticks() == [1e-3, 1e-2, 1e-1, 1.0]
    assert log.position(1e-3) == 0.0 and log.position(7.85) == 1.0
    assert Scale.fit([]) is None and Scale.fit([float("nan")]) is None
    single = Scale.fit([293.6], log=False)
    assert single is not None and single.low < 293.6 < single.high
    assert np.isnan(Scale(1, 2).normalise(np.array([np.nan]))[0])
    assert format_value(0.000123) == "1.23e-04" and format_value(2.5) == "2.5"


def test_labels_sit_inside_their_regions():
    # a 60 x 60 slice: cell 1 on the left half, cell 2 (two lattice instances) on the right
    cells = np.ones((60, 60), dtype=np.int32)
    cells[:, 30:] = 2
    instances = np.zeros_like(cells)
    instances[30:, 30:] = 1
    data = SliceData(cells=cells, instances=instances, materials=cells.copy())
    labels = place_labels(data, LabelKind.CELL, ColorTables())
    assert sorted(label.text for label in labels) == ["1", "2", "2"]
    for label in labels:
        assert cells[int(label.row), int(label.col)] == int(label.text)
        assert label.room >= 10
    assert labels[0].text == "1", "the roomiest region comes first"
    tables = ColorTables(cell_universe={1: 7, 2: 8})
    texts = sorted(label.text for label in place_labels(data, LabelKind.UNIVERSE, tables))
    assert texts == ["7", "8", "8"]
    assert place_labels(data, LabelKind.NONE, tables) == []


def test_labels_follow_the_universe_level_and_centre_in_strips():
    # a 20 x 120 strip of cell 1 (universe 4), nested at level 0 in cell 10 (universe 0)
    cells = np.ones((20, 120), dtype=np.int32)
    data = SliceData(cells=cells, instances=np.zeros_like(cells), materials=cells.copy())
    tables = ColorTables(cell_universe={1: 4, 10: 0})
    (label,) = place_labels(data, LabelKind.UNIVERSE, tables)
    assert label.text == "4" and 50 <= label.col <= 70, "near the middle of the strip"
    data.level_cells = np.full_like(cells, 10)
    data.level_instances = np.zeros_like(cells)
    (label,) = place_labels(data, LabelKind.UNIVERSE, tables)
    assert label.text == "0", "the universe of the level's cell"
