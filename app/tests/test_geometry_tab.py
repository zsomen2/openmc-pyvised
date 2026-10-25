"""The Geometry tab with a fake session: rendering, hover read-out, colour modes, info panel."""

import numpy as np
import pytest
from fakes import FakeSession, model_info

from pyvised.core.settings import PlotBackground
from pyvised.render.colorize import ColorMode
from pyvised.ui.geometry.geometry_tab import GeometryTab
from pyvised_protocol.sidecar import SavedView


@pytest.fixture
def tab(qtbot, settings):
    settings.plot_background = PlotBackground.WHITE
    session = FakeSession()
    widget = GeometryTab(session, settings)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.resize(1400, 800)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def show(qtbot, tab: GeometryTab, info=None, reloaded=False):
    with qtbot.waitSignal(tab.allSharp, timeout=5000):
        tab.show_model(info or model_info(), reloaded)


def test_both_views_render_at_their_device_pixel_size(qtbot, tab: GeometryTab):
    assert tab.stack.currentIndex() == 0, "empty state before a model is open"
    show(qtbot, tab)
    assert tab.stack.currentIndex() == 1
    left, right = tab.panels
    assert (left.basis, right.basis) == ("xz", "xy"), "Front XZ + Top XY by default"
    assert left.cut == pytest.approx(0.0) and right.cut == pytest.approx(0.0)
    for panel in tab.panels:

        def settled(panel=panel) -> bool:
            width, height = panel.view.view_size()
            shown = panel._shown
            return shown is not None and shown.pixels == (round(width), round(height))

        qtbot.waitUntil(settled, timeout=5000)
        assert panel.sharp and panel.view.window is not None


def test_hover_describes_the_pixel_under_the_cursor(qtbot, tab: GeometryTab):
    show(qtbot, tab)
    texts: list[str] = []
    tab.cursorText.connect(texts.append)
    top = tab.panels[1]
    top.view.hovered.emit((-5.0, 0.0))
    assert "x = -5.000" in texts[-1] and "z = 0.000" in texts[-1]
    assert "cell 1 fuel" in texts[-1] and "material 1 fuel" in texts[-1]
    assert "universe 0" in texts[-1]
    top.view.hovered.emit((5.0, 7.0))
    assert "cell 3 gap" in texts[-1] and "void" in texts[-1]
    top.view.hovered.emit((10.2, 0.0))
    assert "outside the geometry" in texts[-1]
    top.view.hovered.emit(None)
    assert texts[-1] == ""


def test_colour_mode_and_lines_need_no_new_slice(qtbot, tab: GeometryTab):
    show(qtbot, tab)
    top = tab.panels[1]
    requests = len(tab.session.requests)  # type: ignore[attr-defined]

    def image() -> np.ndarray:
        return np.asarray(top.view.image.image)[..., :3]

    material = image().copy()
    tab.set_color_mode(ColorMode.NONE)
    white = (image() == 255).all(axis=-1)
    assert white.mean() > 0.9 and not (material == 255).all(axis=-1).mean() > 0.9
    tab.set_lines(False)
    assert (image() == 255).all(), "lines only, without lines: nothing but background"
    tab.set_color_mode(ColorMode.MATERIAL)
    tab.set_lines(True)
    assert np.array_equal(image(), material)
    assert len(tab.session.requests) == requests  # type: ignore[attr-defined]
    assert tab.settings.color_mode == "material" and tab.settings.show_lines


def test_saved_views_and_reloads(qtbot, tab: GeometryTab):
    info = model_info()
    info.sidecar.views = [  # type: ignore[union-attr]
        SavedView(basis="yz", cut=2.5),
        SavedView(basis="xy", cut=-1.0, center=(-5.0, 0.0), width=8.0),
    ]
    show(qtbot, tab, info)
    left, right = tab.panels
    assert (left.basis, left.cut) == ("yz", 2.5)
    window = right.view.visible_window()
    assert window.width == pytest.approx(8.0, rel=0.02)
    assert window.center[0] == pytest.approx(-5.0, abs=0.1)

    right.spin.setValue(3.0)
    tab.session.generation += 1  # type: ignore[attr-defined]
    show(qtbot, tab, model_info(), reloaded=True)
    assert right.cut == pytest.approx(3.0), "a reload keeps the cut"
    assert right.view.visible_window().width == pytest.approx(8.0, rel=0.02)


def test_slider_drag_asks_for_previews(qtbot, tab: GeometryTab):
    show(qtbot, tab)
    top = tab.panels[1]
    full = top._shown.pixels  # type: ignore[union-attr]
    top.slider.setSliderDown(True)
    top.slider.setValue(top.slider.value() + 100)
    qtbot.waitUntil(lambda: top._shown is not None and top._shown.pixels != full)
    assert top._shown.pixels[0] < full[0] and not top.sharp  # type: ignore[union-attr]
    top.slider.setSliderDown(False)
    with qtbot.waitSignal(top.sharpShown, timeout=5000):
        top.slider.sliderReleased.emit()
    assert top._shown.pixels == full  # type: ignore[union-attr]
    assert top._shown.origin[2] == pytest.approx(top.cut + 0.0)  # type: ignore[union-attr]


def test_render_errors_are_shown_in_the_view(qtbot, tab: GeometryTab):
    tab.session.fail = True  # type: ignore[attr-defined]
    tab.show_model(model_info(), False)
    top = tab.panels[1]
    qtbot.waitUntil(lambda: top.view.message.isVisible())
    assert "worker died" in top.view.message.text()


def test_info_panel_lists_the_model(qtbot, tab: GeometryTab):
    show(qtbot, tab)
    panel = tab.info_panel
    assert panel.title.text() == "Test model"
    sections = [panel.tree.topLevelItem(i).text(0) for i in range(panel.tree.topLevelItemCount())]
    assert sections == [
        "Model",
        "Materials (2)",
        "Cells and universes (3)",
        "Settings",
        "Tallies (0)",
    ]
    materials = panel.tree.topLevelItem(1)
    assert materials.child(0).text(0) == "1  fuel" and materials.child(0).text(1) == "10 g/cm³"
    cells = panel.tree.topLevelItem(2)
    assert [cells.child(i).text(1) for i in range(cells.childCount())] == [
        "material 1",
        "material 2",
        "void",
    ]
    assert panel.output_button.isVisibleTo(panel)
