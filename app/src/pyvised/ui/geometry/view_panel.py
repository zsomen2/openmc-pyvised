"""A geometry view with its basis selector and cut-plane slider; it fetches its own slices.

Rendering follows the plan: after ~150 ms without changes the panel asks for a
slice of exactly the visible window at the view's device-pixel size (times
the render-quality setting). While the slider is dragged it asks for quick
low-resolution previews instead. At most one request is in flight; the
newest wanted state is fetched when it returns. Colour modes that need an
extra layer (universe level, temperature, weight windows) ask for it with the
next slice.
"""

import time

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import CellInfo, MaterialInfo, ModelInfo, SliceRequest
from pyvised_protocol.sidecar import Basis, SavedView
from pyvised_protocol.slices import OUTSIDE, OVERLAP, VOID, SliceData

from ...core.model_session import ModelSession
from ...render.colorize import ColorTables, Highlight, Style, colorize
from ...render.labels import LabelKind, place_labels
from ...render.scales import format_value
from ...render.views import (
    BASES,
    SliceLayers,
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
LABEL_ROOM = 5.0
"""Slice pixels a label needs around its centre, plus `LABEL_ROOM_PER_CHAR` per character."""
LABEL_ROOM_PER_CHAR = 3.5


class ViewPanel(QFrame):
    hoverText = Signal(str)
    rendered = Signal(float)
    """Seconds from the request to the shown image."""
    sharpShown = Signal()
    """A full-resolution slice of the current state is on screen."""
    dataChanged = Signal()
    """A new slice is on screen (the legend counts its pixels)."""
    cutChanged = Signal()
    """The basis or the cut moved; the other view's line follows."""
    cutLineMoved = Signal(float, bool)
    """The user dragged the other view's cut line to a position; True when released."""
    inspectRequested = Signal(object)
    """(x, y, z): show what lies there."""
    otherCutRequested = Signal(object)
    """(x, y, z): move the other view's cut plane through this point."""
    exportRequested = Signal(str)
    """"image" or "data"."""
    activated = Signal()
    """The user works in this view (the File menu's export uses the active view)."""

    def __init__(self, session: ModelSession, quality: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.quality = quality
        self.basis: Basis = "xy"
        self.cut = 0.0
        self.info: ModelInfo | None = None
        self.render_style = Style()
        self.tables = ColorTables()
        self.highlight: Highlight | None = None
        self.label_kind = LabelKind.NONE
        self.layers = SliceLayers()
        self.sharp = False
        self._cells: dict[int, CellInfo] = {}
        self._materials: dict[int, MaterialInfo] = {}
        self._data: SliceData | None = None
        self._rgba: np.ndarray | None = None
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
        self.overlap_label = QLabel()
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
        header.addWidget(self.overlap_label)
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
        self.view.clicked.connect(self._clicked)
        self.view.contextRequested.connect(self._context_menu)
        self.view.cutLineDragged.connect(self.cutLineMoved)
        self._set_controls_enabled(False)

    # -- model and style -----------------------------------------------------------

    @property
    def data(self) -> SliceData | None:
        return self._data

    @property
    def rgba(self) -> np.ndarray | None:
        """The image on screen (row 0 = top), as coloured for the current style."""
        return self._rgba

    @property
    def shown_request(self) -> SliceRequest | None:
        return self._shown

    @property
    def normal_axis(self) -> int:
        """World axis (0 = x, 1 = y, 2 = z) along which the cut plane moves."""
        return BASES[self.basis][2]

    def set_model(
        self, info: ModelInfo, saved: SavedView | None, *, keep: bool, default_basis: Basis
    ) -> None:
        """Show a newly opened model; `keep` = the same model reloaded (keep cut and zoom)."""
        first = self.info is None
        self.info = info
        self._cells = {c.id: c for c in info.cells}
        self._materials = {m.id: m for m in info.materials}
        self._generation = self.session.generation
        self._data, self._rgba, self._shown, self.sharp = None, None, None, False
        self.view.show_marker(None)
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

    def set_style(
        self, style: Style, tables: ColorTables, highlight: Highlight | None = None
    ) -> None:
        relabel = tables is not self.tables
        self.render_style = style
        self.tables = tables
        self.highlight = highlight
        self._paint()
        if relabel:
            self._update_labels()

    def set_labels(self, kind: LabelKind) -> None:
        self.label_kind = kind
        self._update_labels()

    def set_layers(self, layers: SliceLayers) -> None:
        """The extra maps the colour mode needs; a new slice brings them."""
        if layers != self.layers:
            self.layers = layers
            self._render(preview=False)

    def set_quality(self, quality: float) -> None:
        self.quality = quality
        self._schedule()

    def reset_view(self) -> None:
        if self.info is None:
            return
        u_range, v_range, _ = plane_extent(self.info.bounding_box, self.basis)
        self.view.show_range(u_range, v_range)

    def set_cut(self, value: float, *, preview: bool = False) -> None:
        """Move the cut plane (clamped to the model), e.g. from the other view's line."""
        if self.info is None:
            return
        lo, hi = self._cut_range()
        self.cut = min(max(value, lo), hi)
        self._update_controls()
        self._cut_moved()
        if preview:
            self._render(preview=True)
        else:
            self._render(preview=False)

    def set_cut_line(self, axis: int | None, value: float) -> None:
        """Show the plane `axis` = value (the other view's cut) as a line, if it crosses ours."""
        h, v, _ = BASES[self.basis]
        if axis == h:
            self.view.set_cut_line(True, value)
        elif axis == v:
            self.view.set_cut_line(False, value)
        else:
            self.view.set_cut_line(None)

    def mark_point(self, point: tuple[float, float, float] | None) -> None:
        """Mark an inspected point when it lies in this view's plane."""
        if point is None or abs(point[self.normal_axis] - self.cut) > 1e-6 * (1 + abs(self.cut)):
            self.view.show_marker(None)
            return
        h, v, _ = BASES[self.basis]
        self.view.show_marker((point[h], point[v]))

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

    def _cut_moved(self) -> None:
        self.view.show_marker(None)
        self.cutChanged.emit()

    def _basis_chosen(self, index: int) -> None:
        basis = self.basis_combo.itemData(index)
        if basis == self.basis or self.info is None:
            return
        self._select_basis(basis)
        lo, hi = self._cut_range()
        self.cut = (lo + hi) / 2
        self._update_controls()
        self._data, self._rgba, self._shown = None, None, None
        self.view.clear_image()
        self.overlap_label.clear()
        self._cut_moved()
        self.reset_view()

    def _slider_moved(self, value: int) -> None:
        lo, hi = self._cut_range()
        self.cut = lo + (hi - lo) * value / SLIDER_STEPS
        self.spin.blockSignals(True)
        self.spin.setValue(self.cut)
        self.spin.blockSignals(False)
        self.cut_label.setText(f"{axis_names(self.basis)[2]} = {self.cut:.4g} cm")
        self._cut_moved()
        if self.slider.isSliderDown():
            self._render(preview=True)
        else:
            self._schedule()

    def _spin_changed(self, value: float) -> None:
        self.cut = value
        self._update_controls()
        self._cut_moved()
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
        request = slice_request(self.basis, window, self.cut, pixels, layers=self.layers)
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
            self._update_overlaps()
            self._update_labels()
            self.rendered.emit(time.perf_counter() - start)
            self.dataChanged.emit()
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
        self._rgba = colorize(self._data, self.render_style, self.tables, self.highlight)
        self.view.show_image(self._rgba, self._shown_window)

    def _update_overlaps(self) -> None:
        if self._data is None:
            self.overlap_label.clear()
            return
        materials = self._data.materials
        overlaps = int(np.count_nonzero(materials == OVERLAP))
        outside = 100.0 * np.count_nonzero(materials == OUTSIDE) / max(1, materials.size)
        if overlaps:
            self.overlap_label.setText(self.tr("%n overlap pixel(s)", "", overlaps))
            self.overlap_label.setStyleSheet("color: #e0218a; font-weight: bold;")
        else:
            self.overlap_label.setText(self.tr("No overlaps"))
            self.overlap_label.setStyleSheet("")
            self.overlap_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.overlap_label.setToolTip(
            self.tr(
                "Overlapping cells are drawn magenta. Outside the geometry: {share:.1f} % "
                "of the view."
            ).format(share=outside)
        )

    def _update_labels(self) -> None:
        data, window, rgba = self._data, self._shown_window, self._rgba
        if self.label_kind is LabelKind.NONE or data is None or window is None or rgba is None:
            self.view.show_labels([])
            return
        rows, cols = data.shape
        items = []
        for label in place_labels(data, self.label_kind, self.tables):
            if label.room < LABEL_ROOM + LABEL_ROOM_PER_CHAR * len(label.text):
                continue
            r, g, b = (int(x) for x in rgba[int(label.row), int(label.col), :3])
            light = 0.299 * r + 0.587 * g + 0.114 * b > 140
            u = window.u0 + (label.col + 0.5) / cols * window.width
            v = window.v0 + window.height - (label.row + 0.5) / rows * window.height
            items.append((u, v, label.text, QColor("black" if light else "white")))
        self.view.show_labels(items)

    # -- hover, click, menu ----------------------------------------------------------------

    def _world(self, point: tuple[float, float]) -> tuple[float, float, float]:
        return world_point(self.basis, point[0], point[1], self.cut)

    def _hovered(self, point: tuple[float, float] | None) -> None:
        if point is None or self.info is None:
            self.hoverText.emit("")
            return
        self.activated.emit()
        x, y, z = self._world(point)
        text = f"x = {x:.3f}   y = {y:.3f}   z = {z:.3f} cm"
        if self._data is not None and self._shown_window is not None:
            index = pixel_index(self._shown_window, self._data.shape, *point)
            if index is not None:
                text += "   │   " + self.describe_pixel(*index)
        self.hoverText.emit(text)

    def describe_pixel(self, row: int, col: int) -> str:
        data = self._data
        assert data is not None
        cell = int(data.cells[row, col])
        instance = int(data.instances[row, col])
        material = int(data.materials[row, col])
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
        if data.level_cells is not None and self.layers.level is not None:
            level_cell = int(data.level_cells[row, col])
            if level_cell >= 0 and level_cell != cell:
                universe = self.tables.cell_universe.get(level_cell)
                parts.append(
                    self.tr("level {level}: cell {cell} in universe {universe}").format(
                        level=self.layers.level, cell=level_cell, universe=universe
                    )
                )
        density = self.tables.cell_density.get(cell)
        if density is not None and material != VOID:
            parts.append(f"{format_value(density)} g/cm³")
        if data.temperature is not None:
            parts.append(f"{format_value(float(data.temperature[row, col]))} K")
        if data.ww_lower is not None:
            lower = float(data.ww_lower[row, col])
            if np.isfinite(lower) and lower > 0:
                parts.append(
                    self.tr("importance {value} (lower bound {lower})").format(
                        value=format_value(self.tables.importance_lower_bound / lower),
                        lower=format_value(lower),
                    )
                )
            else:
                parts.append(self.tr("no weight window"))
        return " · ".join(parts)

    def _clicked(self, point: tuple[float, float]) -> None:
        if self.info is None:
            return
        self.activated.emit()
        self.view.show_marker(point)
        self.inspectRequested.emit(self._world(point))

    def _context_menu(self, point: tuple[float, float], screen: QPoint) -> None:
        if self.info is None:
            return
        self.activated.emit()
        world = self._world(point)
        menu = QMenu(self)
        inspect = menu.addAction(self.tr("Inspect here"))
        other = menu.addAction(self.tr("Set the other view's cut here"))
        centre = menu.addAction(self.tr("Centre here"))
        copy = menu.addAction(self.tr("Copy coordinates"))
        menu.addSeparator()
        export_image = menu.addAction(self.tr("Export image…"))
        export_data = menu.addAction(self.tr("Export slice data (.npz)…"))
        export_image.setEnabled(self._rgba is not None)
        export_data.setEnabled(self._data is not None)
        chosen = menu.exec(screen)
        if chosen is inspect:
            self._clicked(point)
        elif chosen is other:
            self.otherCutRequested.emit(world)
        elif chosen is centre:
            self.view.centre_on(point)
        elif chosen is copy:
            QGuiApplication.clipboard().setText("{:.6g} {:.6g} {:.6g}".format(*world))
        elif chosen is export_image:
            self.exportRequested.emit("image")
        elif chosen is export_data:
            self.exportRequested.emit("data")
