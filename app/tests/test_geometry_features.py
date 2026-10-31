"""Phase 3 features of the Geometry tab with a fake session: colour modes and their layers,
highlight, legend, colour editing, linked cut lines, inspect, labels and export."""

import json
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeSession, model_info

from pyvised.core.settings import PlotBackground
from pyvised.render import palette
from pyvised.render.colorize import ColorMode
from pyvised.render.labels import LabelKind
from pyvised.ui.geometry.geometry_tab import GeometryTab
from pyvised_protocol.sidecar import SIDECAR_NAME


@pytest.fixture
def tab(qtbot, settings, tmp_path: Path):
    settings.plot_background = PlotBackground.WHITE
    session = FakeSession(tmp_path)
    widget = GeometryTab(session, settings)  # type: ignore[arg-type]
    session.setParent(widget)  # its queued answers go with the widget
    qtbot.addWidget(widget)
    widget.resize(1400, 800)
    widget.show()
    qtbot.waitExposed(widget)
    info = model_info()
    session.info = info
    with qtbot.waitSignal(widget.allSharp, timeout=5000):
        widget.show_model(info, False)
    return widget


def image(tab: GeometryTab, index: int = 1) -> np.ndarray:
    return np.asarray(tab.panels[index].view.image.image)[..., :3]


def settle(qtbot, tab: GeometryTab, check) -> None:
    qtbot.waitUntil(lambda: all(p.sharp and p.data is not None and check(p) for p in tab.panels))


def test_modes_ask_for_the_layers_they_need(qtbot, tab: GeometryTab):
    requests = tab.session.requests  # type: ignore[attr-defined]
    before = len(requests)
    tab.set_color_mode(ColorMode.DENSITY)
    tab.set_color_mode(ColorMode.CELL)
    assert len(requests) == before, "density and cell colours come from the ids"
    tab.set_color_mode(ColorMode.IMPORTANCE)
    settle(qtbot, tab, lambda p: p.data.ww_lower is not None)
    assert requests[-1].weight_windows is not None and requests[-1].level is None
    top = tab.panels[1]
    left_x = top.data.cells == 1
    viridis = palette.color_map("viridis")
    colours = image(tab)[::-1][left_x]  # the image item stores rows bottom-up
    assert (colours == viridis[0]).all(axis=-1).mean() > 0.9, "importance 1 at the bottom"
    tab.set_color_mode(ColorMode.TEMPERATURE)
    settle(qtbot, tab, lambda p: p.data.temperature is not None)
    assert requests[-1].properties and requests[-1].weight_windows is None
    tab.set_color_mode(ColorMode.UNIVERSE)
    tab.set_universe_level(0)
    settle(qtbot, tab, lambda p: p.data.level_cells is not None)
    assert requests[-1].level == 0
    assert tab.settings.color_mode == "universe"


def test_highlight_from_the_info_panel_and_the_legend(qtbot, tab: GeometryTab):
    plain = image(tab).copy()
    materials = tab.info_panel.tree.topLevelItem(1)
    tab.info_panel.tree.setCurrentItem(materials.child(0))  # material 1 fuel
    assert tab.selection == ("material", 1)
    dimmed = image(tab)
    data = tab.panels[1].data
    water = (data.materials == 2)[::-1]
    assert not np.array_equal(dimmed[water], plain[water]), "water is dimmed"
    fuel = (data.materials == 1)[::-1]
    assert np.array_equal(dimmed[fuel], plain[fuel])
    # the legend lists both materials; clicking the selected one again clears the highlight
    legend = tab.legend
    qtbot.waitUntil(lambda: legend.tree.topLevelItemCount() == 2)
    texts = [legend.tree.topLevelItem(i).text(0) for i in range(legend.tree.topLevelItemCount())]
    assert texts == ["1  fuel", "2  water"]
    item = legend.tree.topLevelItem(0)
    assert item.isSelected(), "the legend follows the selection"
    legend.tree.itemClicked.emit(item, 0)
    assert tab.selection is None
    assert np.array_equal(image(tab), plain)


def test_colour_edits_are_saved_in_the_sidecar(qtbot, tab: GeometryTab, tmp_path: Path):
    assert tab.info is not None and tab.info.sidecar is not None
    (tmp_path / SIDECAR_NAME).write_text(tab.info.sidecar.model_dump_json(), encoding="utf-8")
    tab._save_color("material", 2, (1, 2, 3))
    data = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert data["material_colors"] == {"2": "#010203"}
    assert data["title"]["en"] == "Test model", "the rest of the sidecar is kept"
    top = tab.panels[1]
    water = (top.data.materials == 2) & ~(top.data.cells == 3)
    assert (image(tab)[::-1][water] == (1, 2, 3)).all(axis=-1).mean() > 0.8
    tab._save_color("material", 2, None)
    data = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert "material_colors" not in data


def test_linked_cut_lines(qtbot, tab: GeometryTab):
    front, top = tab.panels  # XZ at y = 0, XY at z = 0
    assert front.view.cut_line.isVisible() and top.view.cut_line.isVisible()
    assert front.view.cut_line.angle == 0 and front.view.cut_line.value() == pytest.approx(0.0)
    top.spin.setValue(3.0)  # the top view moves to z = 3
    assert front.view.cut_line.value() == pytest.approx(3.0), "the front view shows z = 3"
    # dragging the line in the front view moves the top view's cut plane
    front.view.cutLineDragged.emit(-4.0, True)
    assert top.cut == pytest.approx(-4.0)
    # parallel planes: no line
    top.basis_combo.setCurrentIndex(top.basis_combo.findData("xz"))
    assert not front.view.cut_line.isVisible()


def test_inspect_shows_the_path_and_a_marker(qtbot, tab: GeometryTab):
    top = tab.panels[1]
    top.view.clicked.emit((-5.0, 2.0))
    qtbot.waitUntil(lambda: tab.info_panel.tree.topLevelItem(0).text(0).startswith("Point"))
    assert tab.session.inspected == [(-5.0, 2.0, 0.0)]  # type: ignore[attr-defined]
    section = tab.info_panel.tree.topLevelItem(0)
    rows = [section.child(i).text(0) for i in range(section.childCount())]
    assert rows[0] == "level 0: cell 1  fuel" and "Material" in rows
    assert len(top.view.marker.data) == 1
    top.spin.setValue(1.0)
    assert len(top.view.marker.data) == 0, "the marker goes with the plane"


def test_labels_and_overlap_count(qtbot, tab: GeometryTab):
    top = tab.panels[1]
    assert top.overlap_label.text() == "No overlaps"
    tab.set_labels(LabelKind.MATERIAL)
    assert top.view.label_count() >= 2
    texts = {item.textItem.toPlainText() for item in top.view._labels if item.isVisible()}
    assert {"1", "2"} <= texts
    tab.set_labels(LabelKind.NONE)
    assert top.view.label_count() == 0


def test_export_png_and_npz(qtbot, tab: GeometryTab, tmp_path: Path):
    top = tab.panels[1]
    tab.save_png(top, tmp_path / "plain.png", with_axes=False)
    tab.save_png(top, tmp_path / "full.png", with_axes=True)
    from PySide6.QtGui import QImage

    plain = QImage(str(tmp_path / "plain.png"))
    rows, cols = top.data.shape
    assert (plain.width(), plain.height()) == (cols, rows)
    full = QImage(str(tmp_path / "full.png"))
    assert full.width() > top.view.width() and full.height() == top.view.grab().height()
    tab.save_npz(top, tmp_path / "slice.npz")
    with np.load(tmp_path / "slice.npz") as saved:
        assert np.array_equal(saved["cells"], top.data.cells)
        meta = json.loads(str(saved["meta"]))
    assert meta["basis"] == "xy" and meta["model"] == "test/model.py"


def test_a_long_description_scrolls_and_leaves_room_for_the_tree(qtbot):
    from pyvised.ui.geometry.info_panel import DESCRIPTION_LINES, InfoPanel
    from pyvised_protocol.sidecar import LocalizedText

    panel = InfoPanel()
    qtbot.addWidget(panel)
    panel.resize(360, 700)
    panel.show()
    info = model_info()
    assert info.sidecar is not None
    info.sidecar.description = LocalizedText(en="One short line.")
    panel.set_model(info, {})
    area = panel.description
    short = area.height()
    assert area.isVisible() and 0 < short < area.fontMetrics().lineSpacing() * 3
    info.sidecar.description = LocalizedText(en="\n".join(f"8.{n}. A sub-task." for n in range(20)))
    panel.set_model(info, {})
    assert area.height() == area.fontMetrics().lineSpacing() * DESCRIPTION_LINES
    assert area.verticalScrollBar().maximum() > 0, "the rest scrolls"
    info.sidecar.description = LocalizedText()
    panel.set_model(info, {})
    assert not area.isVisible()
