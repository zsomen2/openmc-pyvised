"""The Geometry tab: two slice views side by side, the legend and the model information.

The tab owns what both views share: the colour mode and its tables, the
universe level, the highlight, the labels and the colours saved in the
model's sidecar. Each view shows the other view's cut plane as a line that can
be dragged; a click inspects the point under the cursor. The particle tracks
(`TrackOverlay`) are drawn on both views, with the geometry faded underneath.
"""

import json
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import numpy as np
from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import InspectResult, ModelInfo, WeightWindowLayer
from pyvised_protocol.slices import SliceData

from ...core.model_session import ModelSession
from ...core.settings import AppSettings, PlotBackground
from ...render import palette
from ...render.colorize import (
    COLOR_MAP_OF,
    ColorMode,
    ColorTables,
    Highlight,
    LineMode,
    Style,
    id_counts,
    mode_ids,
    pixel_values,
    shade,
)
from ...render.labels import LabelKind
from ...render.palette import RGB
from ...render.scales import format_value
from ...render.structure import ModelStructure
from ...render.views import SliceLayers
from ..dialogs.text_dialog import TextDialog
from ..empty_state import EmptyState
from ..parameters_panel import ParametersPanel
from ..tracks.overlay import TrackOverlay
from ..tracks.player import TrackPlayer
from .info_panel import InfoPanel
from .legend import LegendEntry, LegendPanel, legend_pixmap
from .view_panel import ViewPanel

MAX_VALUE_ENTRIES = 12
"""A continuous mode lists its values in the legend when there are at most this many."""
LEGEND_DELAY_MS = 40
COLOR_FIELDS = {"material": "material_colors", "cell": "cell_colors", "universe": "universe_colors"}
TRACK_FADE = 0.6
"""Share of the background mixed into the geometry under the particle tracks."""


class ChangeBanner(QFrame):
    """'The model files changed' bar above the views (reload-on-change = ask)."""

    reloadRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        label = QLabel(self.tr("The model files changed on disk."))
        reload = QPushButton(self.tr("Reload"))
        dismiss = QPushButton(self.tr("Dismiss"))
        reload.clicked.connect(self.hide)
        reload.clicked.connect(self.reloadRequested)
        dismiss.clicked.connect(self.hide)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(label, 1)
        layout.addWidget(reload)
        layout.addWidget(dismiss)


class GeometryTab(QWidget):
    cursorText = Signal(str)
    renderTime = Signal(float)
    reloadRequested = Signal()
    statusMessage = Signal(str)
    allSharp = Signal()
    """Both views show a full-resolution slice of the newly opened model."""

    def __init__(
        self,
        session: ModelSession,
        settings: AppSettings,
        parent: QWidget | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.language = language or settings.language
        """UI language of this run, for the model's title and description."""
        self.info: ModelInfo | None = None
        self.structure: ModelStructure | None = None
        self.color_mode = ColorMode(settings.color_mode)
        self.lines = settings.show_lines
        self.label_kind = LabelKind(settings.labels)
        self.universe_level: int | None = None
        """Universe level of the universe colours; None = the deepest cells."""
        self.weight_window_layer = WeightWindowLayer()
        self.tables = ColorTables()
        self.selection: tuple[str, int] | None = None
        """What is highlighted: ("material" | "cell" | "universe", id)."""
        self.render_style = Style()
        self.inspected_point: tuple[float, float, float] | None = None
        """The point inspected last (a click in a view)."""
        self.active_panel = 0
        self._announced = -1
        self._inspect_generation = -1

        self.empty = EmptyState(
            "mdi6.cube-outline",
            self.tr("No model open"),
            self.tr(
                "Open a Python model builder, a Jupyter notebook or OpenMC XML from the "
                "workspace (File › Open model…)."
            ),
        )
        self.banner = ChangeBanner()
        self.banner.hide()
        self.panels = (
            ViewPanel(session, settings.render_quality),
            ViewPanel(session, settings.render_quality),
        )
        self.legend = LegendPanel()
        self.info_panel = InfoPanel()
        self.views = QSplitter(Qt.Orientation.Horizontal)
        for panel in self.panels:
            self.views.addWidget(panel)
        self.side = QSplitter(Qt.Orientation.Vertical)
        self.parameters = ParametersPanel()
        self.parameters.applyRequested.connect(self._apply_parameters)
        self.parameters_box = QGroupBox(self.tr("Builder parameters"))
        box_layout = QVBoxLayout(self.parameters_box)
        box_layout.setContentsMargins(6, 4, 6, 4)
        box_layout.addWidget(self.parameters)
        self.parameters_box.hide()
        self.side.addWidget(self.legend)
        self.side.addWidget(self.parameters_box)
        self.side.addWidget(self.info_panel)
        self.side.setStretchFactor(0, 2)
        self.side.setStretchFactor(1, 0)
        self.side.setStretchFactor(2, 3)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.views)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 360])

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.addWidget(self.banner)
        content_layout.addWidget(self.splitter, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        self.player = TrackPlayer(self)
        self.tracks = TrackOverlay(self.panels, self.player, self)
        self.tracks.fade = settings.tracks_fade
        self.tracks.changed.connect(self._restyle_panels)
        self._legend_timer = QTimer(self, singleShot=True, interval=LEGEND_DELAY_MS)
        self._legend_timer.timeout.connect(self._update_legend)
        for index, panel in enumerate(self.panels):
            other = self.panels[1 - index]
            panel.hoverText.connect(self.cursorText)
            panel.rendered.connect(self.renderTime)
            panel.sharpShown.connect(self._check_sharp)
            panel.dataChanged.connect(self._legend_timer.start)
            panel.cutChanged.connect(self._sync_cut_lines)
            panel.cutLineMoved.connect(
                lambda value, done, other=other: other.set_cut(value, preview=not done)
            )
            panel.otherCutRequested.connect(
                lambda point, other=other: other.set_cut(point[other.normal_axis])
            )
            panel.inspectRequested.connect(self.inspect)
            panel.exportRequested.connect(lambda kind, index=index: self.export_view(index, kind))
            panel.activated.connect(lambda index=index: self._set_active(index))
            panel.hover_extra = lambda point, index=index: (
                self.tracks.hover_text(index, point) if self.tracks.shown else ""
            )
            panel.menu_extra = lambda menu, point, index=index: self.tracks.add_menu_actions(
                index, menu, point
            )
        self.banner.reloadRequested.connect(self.reloadRequested)
        self.info_panel.outputRequested.connect(self._show_builder_output)
        self.info_panel.highlightRequested.connect(lambda s: self.set_highlight(s, "info"))
        self.info_panel.colorEditRequested.connect(self.edit_color)
        self.legend.highlightRequested.connect(lambda s: self.set_highlight(s, "legend"))
        self.legend.colorEditRequested.connect(self.edit_color)
        self.legend.colorResetRequested.connect(lambda kind, key: self._save_color(kind, key, None))
        self.apply_settings()

    # -- model -----------------------------------------------------------------------

    def show_model(self, info: ModelInfo, reloaded: bool) -> None:
        self.info = info
        self.structure = ModelStructure(info)
        self.tables = ColorTables.for_model(info)
        self.banner.hide()
        if not reloaded:
            self.selection = None
            self.universe_level = None
        elif self.universe_level is not None:
            self.universe_level = min(self.universe_level, info.max_level)
        particles = {ww.particle for ww in info.weight_windows}
        if particles and self.weight_window_layer.particle not in particles:
            self.weight_window_layer = WeightWindowLayer(particle=sorted(particles)[0])
        self.stack.setCurrentIndex(1)
        views = info.sidecar.views if info.sidecar else []
        defaults = self.settings.default_views
        for index, panel in enumerate(self.panels):
            self._style_panel(panel)
            panel.set_labels(self.label_kind)
            panel.layers = self._layers()
            saved = views[index] if index < len(views) else None
            panel.set_model(info, saved, keep=reloaded, default_basis=defaults[index])
        self.info_panel.set_model(info, self.tables.materials, self.language)
        self.parameters.set_model(info, self.language)
        self.parameters_box.setVisible(self.parameters.has_parameters)
        self.legend.select(self.selection)
        self._sync_cut_lines()
        self._update_legend()
        self.tracks.schedule()

    def show_files_changed(self) -> None:
        if self.info is not None:
            self.banner.show()

    def reset_views(self) -> None:
        for panel in self.panels:
            panel.reset_view()

    # -- style ------------------------------------------------------------------------

    def set_color_mode(self, mode: ColorMode) -> None:
        self.color_mode = mode
        self.settings.color_mode = mode.value
        self.apply_settings()

    def set_lines(self, on: bool) -> None:
        self.lines = on
        self.settings.show_lines = on
        self.apply_settings()

    def set_labels(self, kind: LabelKind) -> None:
        self.label_kind = kind
        self.settings.labels = kind.value
        for panel in self.panels:
            panel.set_labels(kind)

    def set_universe_level(self, level: int | None) -> None:
        self.universe_level = level
        self._apply_layers()
        self._update_legend()

    def set_weight_window_layer(self, layer: WeightWindowLayer) -> None:
        self.weight_window_layer = layer
        self._apply_layers()

    def apply_settings(self) -> None:
        """Colours, lines and quality from the settings and the current theme."""
        palette_ = self.palette()
        if self.settings.plot_background is PlotBackground.WHITE:
            background, foreground = QColor("white"), QColor("black")
        else:
            background = palette_.color(QPalette.ColorRole.Base)
            foreground = palette_.color(QPalette.ColorRole.Text)
        self.render_style = Style.for_background(
            (background.red(), background.green(), background.blue()),
            mode=self.color_mode,
            lines=self.lines,
            line_mode=LineMode(self.settings.line_mode),
            line_width=self.settings.line_width,
        )
        for panel in self.panels:
            panel.view.set_colors(background, foreground)
            self._style_panel(panel)
            panel.set_quality(self.settings.render_quality)
        self._apply_layers()
        self._update_legend()

    def _style_panel(self, panel: ViewPanel) -> None:
        style = self.render_style
        if self.tracks.shown and self.tracks.fade:
            style = replace(style, fade=TRACK_FADE)
        panel.set_style(style, self.tables, self._highlight())

    def _restyle_panels(self) -> None:
        """The tracks appeared, went or their fading changed."""
        for panel in self.panels:
            self._style_panel(panel)

    def move_cuts(self, point: tuple[float, float, float]) -> None:
        """Move both cut planes through `point`."""
        for panel in self.panels:
            panel.set_cut(point[panel.normal_axis])

    def _layers(self) -> SliceLayers:
        mode = self.color_mode
        has_windows = self.info is not None and bool(self.info.weight_windows)
        return SliceLayers(
            level=self.universe_level if mode is ColorMode.UNIVERSE else None,
            temperature=mode is ColorMode.TEMPERATURE,
            weight_windows=self.weight_window_layer
            if mode is ColorMode.IMPORTANCE and has_windows
            else None,
        )

    def _apply_layers(self) -> None:
        layers = self._layers()
        for panel in self.panels:
            panel.set_layers(layers)

    def set_busy(self, busy: bool) -> None:
        """The model is being opened: its parameters cannot be applied again meanwhile."""
        self.parameters.set_busy(busy)

    def _apply_parameters(self, params: dict[str, float]) -> None:
        if self.session.path is not None:
            self.session.open(self.session.path, params)

    def refresh_icon(self) -> None:
        self.empty.refresh_icon()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.apply_settings()  # light/dark switch: plot background, lines, axes

    # -- highlight ----------------------------------------------------------------------

    def set_highlight(self, selection: tuple[str, int] | None, source: str = "") -> None:
        """Highlight a material, cell or universe in both views (None: nothing)."""
        self.selection = selection
        if source != "legend":
            self.legend.select(selection)
        if source != "info":
            self.info_panel.clear_selection()
        for panel in self.panels:
            self._style_panel(panel)

    def _highlight(self) -> Highlight | None:
        if self.selection is None or self.structure is None:
            return None
        kind, key = self.selection
        if kind == "material":
            return Highlight(materials=frozenset({key}))
        if kind == "cell":
            return Highlight(cells=frozenset(self.structure.cells_in_cell(key)))
        return Highlight(cells=frozenset(self.structure.cells_in_universe(key)))

    # -- colours ----------------------------------------------------------------------------

    def color_of(self, kind: str, key: int) -> RGB:
        if kind == "material":
            return self.tables.materials.get(key, palette.spread_color(key))
        if kind == "cell":
            return self.tables.cell_overrides.get(key, palette.cell_color(key))
        return self.tables.universes.get(key, palette.universe_color(key))

    def edit_color(self, kind: str, key: int) -> None:
        """Ask for a new colour of a material, cell or universe and save it in the sidecar."""
        if self.info is None or kind not in COLOR_FIELDS:
            return
        names = {
            "material": self.tr("Colour of material {id}"),
            "cell": self.tr("Colour of cell {id}"),
            "universe": self.tr("Colour of universe {id}"),
        }
        color = QColorDialog.getColor(
            QColor(*self.color_of(kind, key)), self, names[kind].format(id=key)
        )
        if color.isValid():
            self._save_color(kind, key, (color.red(), color.green(), color.blue()))

    def _save_color(self, kind: str, key: int, rgb: RGB | None) -> None:
        """Store (or with None remove) a colour in the sidecar and repaint."""
        info = self.info
        if info is None or kind not in COLOR_FIELDS:
            return
        field = COLOR_FIELDS[kind]
        current = getattr(info.sidecar, field) if info.sidecar is not None else {}
        colors = {str(k): v for k, v in current.items()}
        if rgb is None:
            colors.pop(str(key), None)
        else:
            colors[str(key)] = palette.to_hex(rgb)
        try:
            self.session.update_sidecar({field: colors or None})
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                self.tr("Colours"),
                self.tr(
                    "The colour could not be saved in the model's pyvised.json.\n{error}"
                ).format(error=exc),
            )
            return
        self.tables = ColorTables.for_model(info)
        for panel in self.panels:
            self._style_panel(panel)
        self.info_panel.set_material_colors(self.tables.materials)
        self._update_legend()
        self.statusMessage.emit(self.tr("Colour saved in pyvised.json."))

    # -- legend ------------------------------------------------------------------------------

    def _legend_title(self) -> str:
        mode = self.color_mode
        if mode is ColorMode.UNIVERSE and self.universe_level is not None:
            return self.tr("Universes at level {level}").format(level=self.universe_level)
        titles = {
            ColorMode.MATERIAL: self.tr("Materials"),
            ColorMode.CELL: self.tr("Cells"),
            ColorMode.UNIVERSE: self.tr("Universes (deepest level)"),
            ColorMode.DENSITY: self.tr("Density [g/cm³]"),
            ColorMode.TEMPERATURE: self.tr("Temperature [K]"),
            ColorMode.IMPORTANCE: self.tr(
                "Importance ({bound} / weight-window lower bound)"
            ).format(bound=format_value(self.tables.importance_lower_bound)),
            ColorMode.NONE: self.tr("Lines only"),
        }
        return titles[mode]

    def _entry_text(self, kind: str, key: int) -> str:
        info = self.info
        if kind == "material":
            mat = next((m for m in info.materials if m.id == key), None) if info else None
            name = mat.name if mat else ""
            return f"{key}  {name}".rstrip()
        if kind == "cell":
            cell = next((c for c in info.cells if c.id == key), None) if info else None
            return f"{key}  {cell.name}".rstrip() if cell else str(key)
        text = self.tr("universe {id}").format(id=key)
        universe = next((u for u in info.universes if u.id == key), None) if info else None
        if universe is not None and universe.name:
            return f"{text}  {universe.name}"
        if universe is not None and len(universe.cells) == 1 and info is not None:
            # universe names do not survive the XML; a lone cell (a lattice element's
            # translated wrapper, a lattice cell's own material) tells what it is
            cell = next((c for c in info.cells if c.id == universe.cells[0]), None)
            if cell is not None and cell.name:
                return f"{text}  ({cell.name})"
        return text

    def legend_entries(self, datas: list[SliceData]) -> list[LegendEntry]:
        """What the legend lists for slices `datas` in the current mode."""
        mode = self.color_mode
        if mode is ColorMode.NONE:
            return []
        if not mode.continuous:
            counts: Counter[int] = Counter()
            for data in datas:
                ids = mode_ids(mode, data, self.tables)
                if ids is not None:
                    counts.update(id_counts(ids))
            total = sum(counts.values()) or 1
            kind = mode.value
            return [
                LegendEntry(key, self._entry_text(kind, key), self.color_of(kind, key), n / total)
                for key, n in sorted(counts.items())
            ]
        values = [pixel_values(mode, data, self.tables) for data in datas]
        finite = [v[np.isfinite(v)] for v in values if v is not None]
        if not finite or not sum(v.size for v in finite):
            return []
        unique, counts_ = np.unique(np.concatenate(finite), return_counts=True)
        if len(unique) > 50 * MAX_VALUE_ENTRIES:
            return []
        merged: Counter[float] = Counter()
        for value, n in zip(unique.tolist(), counts_.tolist(), strict=True):
            merged[float(f"{value:.4g}")] += n  # float32 layers: no false distinctions
        if len(merged) > MAX_VALUE_ENTRIES:
            return []
        values = np.array(sorted(merged))
        colors = shade(
            values, self.tables.scales.get(mode), palette.color_map(COLOR_MAP_OF[mode]), (0, 0, 0)
        )
        total = sum(merged.values())
        return [
            LegendEntry(v, format_value(v), (int(r), int(g), int(b)), merged[v] / total)
            for v, (r, g, b) in zip(values.tolist(), colors, strict=True)
        ]

    def _update_legend(self) -> None:
        if self.info is None:
            self.legend.clear()
            return
        datas = [p.data for p in self.panels if p.data is not None]
        entries = self.legend_entries(datas)
        mode = self.color_mode
        if mode.continuous:
            colors = palette.color_map(COLOR_MAP_OF[mode])
            self.legend.show_scale(
                self._legend_title(), self.tables.scales.get(mode), colors, entries
            )
        else:
            kind = mode.value if mode is not ColorMode.NONE else None
            self.legend.show_entries(self._legend_title(), kind, entries)
        self.legend.select(self.selection)

    # -- linked views ----------------------------------------------------------------------

    def _sync_cut_lines(self) -> None:
        left, right = self.panels
        left.set_cut_line(right.normal_axis, right.cut)
        right.set_cut_line(left.normal_axis, left.cut)

    def _set_active(self, index: int) -> None:
        self.active_panel = index

    # -- inspect ---------------------------------------------------------------------------

    def inspect(self, point: tuple[float, float, float]) -> None:
        """Show the nesting path at `point` in the info panel."""
        generation = self.session.generation
        self._inspect_generation = generation
        self.session.inspect(
            point,
            lambda result: self._inspected(result, generation),
            lambda error: self._inspect_failed(error, generation),
        )

    def _inspected(self, result: InspectResult, generation: int) -> None:
        if generation != self.session.generation or generation != self._inspect_generation:
            return
        self.inspected_point = result.point
        self.info_panel.show_point(result)
        for panel in self.panels:
            panel.mark_point(result.point)

    def _inspect_failed(self, error: Exception, generation: int) -> None:
        if generation == self.session.generation:
            self.statusMessage.emit(self.tr("Inspect failed: {error}").format(error=error))

    # -- export -------------------------------------------------------------------------------

    def export_view(self, index: int | None = None, kind: str = "image") -> None:
        """Save a view as PNG (with or without axes and legend) or its slice data as .npz."""
        panel = self.panels[self.active_panel if index is None else index]
        if self.info is None or panel.data is None:
            return
        name = re.sub(
            r"[^\w.-]+", "_", f"{Path(self.info.source).stem}_{panel.basis}_{panel.cut:.4g}"
        )
        folder = self.session.path.parent if self.session.path else Path.home()
        if kind == "data":
            path, _ = QFileDialog.getSaveFileName(
                self,
                self.tr("Export slice data"),
                str(folder / f"{name}.npz"),
                self.tr("Slice data (*.npz)"),
            )
            if path:
                self._write(lambda: self.save_npz(panel, Path(path)), path)
            return
        with_axes = self.tr("PNG image with axes and legend (*.png)")
        slice_only = self.tr("PNG image of the slice only (*.png)")
        path, chosen = QFileDialog.getSaveFileName(
            self,
            self.tr("Export image"),
            str(folder / f"{name}.png"),
            f"{with_axes};;{slice_only}",
        )
        if path:
            self._write(lambda: self.save_png(panel, Path(path), chosen != slice_only), path)

    def _write(self, action, path: str) -> None:
        try:
            action()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Export"), str(exc))
            return
        self.statusMessage.emit(self.tr("Saved {path}").format(path=path))

    def save_png(self, panel: ViewPanel, path: Path, with_axes: bool = True) -> None:
        if panel.rgba is None:
            raise ValueError(self.tr("The view shows no slice yet."))
        if not with_axes:
            rgba = np.ascontiguousarray(panel.rgba)
            height, width = rgba.shape[:2]
            image = QImage(rgba.data, width, height, 4 * width, QImage.Format.Format_RGBA8888)
            if not image.copy().save(str(path)):
                raise OSError(self.tr("Could not write {path}").format(path=path))
            return
        view = panel.view.grab()
        background = panel.view.backgroundBrush().color()
        foreground = self.palette().color(QPalette.ColorRole.Text)
        if self.settings.plot_background is PlotBackground.WHITE:
            foreground = QColor("black")
        entries = self.legend_entries([panel.data] if panel.data is not None else [])
        mode = self.color_mode
        legend = legend_pixmap(
            self._legend_title(),
            entries,
            self.tables.scales.get(mode),
            palette.color_map(COLOR_MAP_OF[mode]) if mode.continuous else None,
            background,
            foreground,
            self.font(),
            view.height(),
        )
        combined = QPixmap(view.width() + legend.width(), view.height())
        combined.fill(background)
        painter = QPainter(combined)
        painter.drawPixmap(0, 0, view)
        painter.drawPixmap(view.width(), 0, legend)
        painter.end()
        if not combined.save(str(path)):
            raise OSError(self.tr("Could not write {path}").format(path=path))

    def save_npz(self, panel: ViewPanel, path: Path) -> None:
        data, request = panel.data, panel.shown_request
        if data is None or request is None or self.info is None:
            raise ValueError(self.tr("The view shows no slice yet."))
        arrays = {"cells": data.cells, "instances": data.instances, "materials": data.materials}
        for name in ("temperature", "level_cells", "level_instances", "ww_lower"):
            value = getattr(data, name)
            if value is not None:
                arrays[name] = value
        meta = {
            "model": self.info.source,
            "params": self.info.params,
            "basis": request.basis,
            "origin": request.origin,
            "width": request.width,
            "pixels": request.pixels,
            "level": request.level,
            "row_0": "top",
            "special_values": {"outside": -2, "void_material": -1, "overlap_material": -3},
        }
        arrays["meta"] = np.array(json.dumps(meta))
        np.savez_compressed(path, **cast(dict[str, Any], arrays))

    # -- state ---------------------------------------------------------------------------

    def save_state(self) -> QByteArray:
        return self.splitter.saveState()

    def restore_state(self, state: QByteArray | None) -> None:
        if state is not None:
            self.splitter.restoreState(state)

    def _check_sharp(self) -> None:
        generation = self.session.generation
        if generation != self._announced and all(p.sharp for p in self.panels):
            self._announced = generation
            self.allSharp.emit()

    def _show_builder_output(self) -> None:
        if self.info is not None:
            TextDialog(self.tr("Builder output"), self.info.builder_output, self).exec()
