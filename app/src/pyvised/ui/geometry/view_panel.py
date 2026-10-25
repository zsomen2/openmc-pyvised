"""A geometry view with its basis selector and cut-plane slider; it fetches its own slices.

Rendering follows the plan: after ~150 ms without changes the panel asks for a
slice of exactly the visible window at the view's device-pixel size (times
the render-quality setting). While the slider is dragged it asks for quick
low-resolution previews instead. At most one request is in flight; the
newest wanted state is fetched when it returns.
"""

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import CellInfo, MaterialInfo, ModelInfo, SliceRequest
from pyvised_protocol.sidecar import Basis, SavedView
from pyvised_protocol.slices import OUTSIDE, OVERLAP, VOID, SliceData

from ...core.model_session import ModelSession
from ...render.colorize import Style, colorize
from ...render.palette import RGB
from ...render.views import (
    Window,
    axis_names,
    device_pixels,
    nice_step,
    pixel_index,
    plane_extent,
    slice_request,
    world_point,
)
from .slice_view import SliceView

IDLE_MS = 150
PREVIEW_SCALE = 0.4
SLIDER_STEPS = 1000
MIN_VIEW_PIXELS = 16


class ViewPanel(QFrame):
    hoverText = Signal(str)
    rendered = Signal(float)
    """Seconds from the request to the shown image."""
    sharpShown = Signal()
    """A full-resolution slice of the current state is on screen."""

    def __init__(self, session: ModelSession, quality: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.quality = quality
        self.basis: Basis = "xy"
        self.cut = 0.0
        self.info: ModelInfo | None = None
        self.render_style = Style()
        self.material_colors: dict[int, RGB] = {}
        self.sharp = False
        self._cells: dict[int, CellInfo] = {}
        self._materials: dict[int, MaterialInfo] = {}
        self._data: SliceData | None = None
        self._shown: SliceRequest | None = None
        self._shown_window: Window | None = None
        self._generation = -1
        self._inflight = False
        self._pending: bool | None = None  # preview flag of the state to fetch next
        self._initial_view: SavedView | bool | None = False  # False: nothing to apply

        self.basis_combo = QComboBox()
        for basis, text in (
            ("xy", self.tr("Top (XY)")),
            ("xz", self.tr("Front (XZ)")),
            ("yz", self.tr("Side (YZ)")),
        ):
            self.basis_combo.addItem(text, basis)
        self.cut_label = QLabel()
        self.view = SliceView()
        self.axis_label = QLabel()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.setPageStep(SLIDER_STEPS // 20)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(4)
        self.spin.setSuffix(" cm")
        self.spin.setKeyboardTracking(False)
        self.spin.setMinimumWidth(110)

        header = QHBoxLayout()
        header.addWidget(self.basis_combo)
        header.addWidget(self.cut_label)
        header.addStretch()
        controls = QHBoxLayout()
        controls.addWidget(self.axis_label)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.spin)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        layout.addLayout(controls)

        self._idle = QTimer(self, singleShot=True, interval=IDLE_MS)
        self._idle.timeout.connect(lambda: self._render(preview=False))
        self.basis_combo.currentIndexChanged.connect(self._basis_chosen)
        self.slider.valueChanged.connect(self._slider_moved)
        self.slider.sliderReleased.connect(lambda: self._render(preview=False))
        self.spin.valueChanged.connect(self._spin_changed)
        self.view.windowChanged.connect(self._window_changed)
        self.view.resetRequested.connect(self.reset_view)
        self.view.hovered.connect(self._hovered)
        self._set_controls_enabled(False)

    # -- model and style -----------------------------------------------------------

    def set_model(
        self, info: ModelInfo, saved: SavedView | None, *, keep: bool, default_basis: Basis
    ) -> None:
        """Show a newly opened model; `keep` = the same model reloaded (keep cut and zoom)."""
        first = self.info is None
        self.info = info
        self._cells = {c.id: c for c in info.cells}
        self._materials = {m.id: m for m in info.materials}
        self._generation = self.session.generation
        self._data, self._shown, self.sharp = None, None, False
        self._set_controls_enabled(True)
        if keep and not first:
            lo, hi = self._cut_range()
            self.cut = min(max(self.cut, lo), hi)
            self._update_controls()
            self._render(preview=False)
            return
        basis = saved.basis if saved is not None else default_basis
        self._select_basis(basis)
        lo, hi = self._cut_range()
        self.cut = saved.cut if saved is not None and lo <= saved.cut <= hi else (lo + hi) / 2
        self._update_controls()
        self._initial_view = saved
        self._apply_initial_view()

    def set_style(self, style: Style, material_colors: dict[int, RGB]) -> None:
        self.render_style = style
        self.material_colors = material_colors
        self._paint()

    def set_quality(self, quality: float) -> None:
        self.quality = quality
        self._schedule()

    def reset_view(self) -> None:
        if self.info is None:
            return
        u_range, v_range, _ = plane_extent(self.info.bounding_box, self.basis)
        self.view.show_range(u_range, v_range)

    # -- controls ----------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.basis_combo, self.slider, self.spin):
            widget.setEnabled(enabled)

    def _select_basis(self, basis: Basis) -> None:
        self.basis = basis
        self.basis_combo.blockSignals(True)
        self.basis_combo.setCurrentIndex(max(0, self.basis_combo.findData(basis)))
        self.basis_combo.blockSignals(False)
        h, v, _ = axis_names(basis)
        self.view.set_axis_labels(h, v)

    def _cut_range(self) -> tuple[float, float]:
        if self.info is None:
            return 0.0, 1.0
        return plane_extent(self.info.bounding_box, self.basis)[2]

    def _update_controls(self) -> None:
        lo, hi = self._cut_range()
        name = axis_names(self.basis)[2]
        for widget in (self.slider, self.spin):
            widget.blockSignals(True)
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(nice_step(hi - lo))
        self.spin.setValue(self.cut)
        span = hi - lo
        self.slider.setValue(round((self.cut - lo) / span * SLIDER_STEPS) if span > 0 else 0)
        for widget in (self.slider, self.spin):
            widget.blockSignals(False)
        self.axis_label.setText(name)
        self.cut_label.setText(f"{name} = {self.cut:.4g} cm")

    def _basis_chosen(self, index: int) -> None:
        basis = self.basis_combo.itemData(index)
        if basis == self.basis or self.info is None:
            return
        self._select_basis(basis)
        lo, hi = self._cut_range()
        self.cut = (lo + hi) / 2
        self._update_controls()
        self._data, self._shown = None, None
        self.view.clear_image()
        self.reset_view()

    def _slider_moved(self, value: int) -> None:
        lo, hi = self._cut_range()
        self.cut = lo + (hi - lo) * value / SLIDER_STEPS
        self.spin.blockSignals(True)
        self.spin.setValue(self.cut)
        self.spin.blockSignals(False)
        self.cut_label.setText(f"{axis_names(self.basis)[2]} = {self.cut:.4g} cm")
        if self.slider.isSliderDown():
            self._render(preview=True)
        else:
            self._schedule()

    def _spin_changed(self, value: float) -> None:
        self.cut = value
        self._update_controls()
        self._schedule()

    # -- rendering -----------------------------------------------------------------------

    def _apply_initial_view(self) -> None:
        width, height = self.view.view_size()
        if self._initial_view is False or self.info is None or min(width, height) < MIN_VIEW_PIXELS:
            return  # applied once the view has a size
        saved, self._initial_view = self._initial_view, False
        if isinstance(saved, SavedView) and saved.width:
            u_range, v_range, _ = plane_extent(self.info.bounding_box, self.basis)
            center = saved.center or ((u_range[0] + u_range[1]) / 2, (v_range[0] + v_range[1]) / 2)
            self.view.show_window(center, saved.width)
        else:
            self.reset_view()
        self._schedule()  # after the axes have laid out for the new range

    def _window_changed(self) -> None:
        if self._initial_view is not False:
            self._apply_initial_view()
        else:
            self._schedule()

    def _schedule(self) -> None:
        if self.info is not None:
            self._idle.start()

    def _render(self, *, preview: bool) -> None:
        if self.info is None or self._initial_view is not False:
            return
        width, height = self.view.view_size()
        if min(width, height) < MIN_VIEW_PIXELS:
            return
        if self._inflight:
            self._pending = preview if self._pending is None else self._pending and preview
            return
        if not preview:
            self._idle.stop()
        window = self.view.visible_window()
        scale = self.quality * (PREVIEW_SCALE if preview else 1.0)
        pixels = device_pixels(width, height, self.view.devicePixelRatioF(), scale)
        request = slice_request(self.basis, window, self.cut, pixels)
        if not preview and self.sharp and request == self._shown:
            return
        generation, start = self._generation, time.perf_counter()
        self._inflight = True
        sent = self.session.request_slice(
            request,
            lambda data: self._received(data, request, window, preview, generation, start),
            lambda error: self._failed(error, generation),
        )
        if not sent:
            self._inflight = False

    def _received(
        self,
        data: SliceData,
        request: SliceRequest,
        window: Window,
        preview: bool,
        generation: int,
        start: float,
    ) -> None:
        self._inflight = False
        if generation == self._generation:
            self._data, self._shown, self._shown_window = data, request, window
            self.sharp = not preview
            self.view.show_message("")
            self._paint()
            self.rendered.emit(time.perf_counter() - start)
            if self.sharp:
                self.sharpShown.emit()
        self._next()

    def _failed(self, error: Exception, generation: int) -> None:
        self._inflight = False
        if generation == self._generation:
            self.sharp = True  # do not retry the same state in a loop
            self.view.show_message(
                self.tr("The slice could not be rendered.\n{error}").format(error=error)
            )
        self._next()

    def _next(self) -> None:
        if self._pending is not None:
            preview, self._pending = self._pending, None
            self._render(preview=preview)

    def _paint(self) -> None:
        if self._data is None or self._shown_window is None:
            return
        rgba = colorize(self._data, self.render_style, self.material_colors)
        self.view.show_image(rgba, self._shown_window)

    # -- hover -----------------------------------------------------------------------------

    def _hovered(self, point: tuple[float, float] | None) -> None:
        if point is None or self.info is None:
            self.hoverText.emit("")
            return
        u, v = point
        x, y, z = world_point(self.basis, u, v, self.cut)
        text = f"x = {x:.3f}   y = {y:.3f}   z = {z:.3f} cm"
        if self._data is not None and self._shown_window is not None:
            index = pixel_index(self._shown_window, self._data.shape, u, v)
            if index is not None:
                text += "   │   " + self.describe_pixel(*index)
        self.hoverText.emit(text)

    def describe_pixel(self, row: int, col: int) -> str:
        assert self._data is not None
        cell = int(self._data.cells[row, col])
        instance = int(self._data.instances[row, col])
        material = int(self._data.materials[row, col])
        if material == OUTSIDE:
            return self.tr("outside the geometry")
        if material == OVERLAP:
            return self.tr("overlap of {count} cells").format(count=-cell - 2)
        info = self._cells.get(cell)
        parts = [
            self.tr("cell {id}").format(id=cell) + (f" {info.name}" if info and info.name else ""),
            self.tr("instance {number}").format(number=instance),
        ]
        if material == VOID:
            parts.append(self.tr("void"))
        else:
            mat = self._materials.get(material)
            name = f" {mat.name}" if mat and mat.name else ""
            parts.append(self.tr("material {id}").format(id=material) + name)
        if info is not None:
            parts.append(self.tr("universe {id}").format(id=info.universe))
        return " · ".join(parts)
