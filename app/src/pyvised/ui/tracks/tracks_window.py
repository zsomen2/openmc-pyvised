"""The particle tracks window: generate tracks, choose how they are drawn, play them back.

A non-modal tool window next to the main window; the tracks are drawn on the
two geometry views (`TrackOverlay`). It asks the backend for a tracked run of
the open model, shows what happened, and sets the overlay's style (particles
only or with their track lines, colours, markers, the slab around the cut
plane), its filter and the playback. The followed history gets an event table:
clicking a row moves the playback there and marks the state, double-clicking
moves both cut planes through it. The choices are remembered in the settings.
"""

import math
from collections.abc import Callable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import ModelInfo
from pyvised_protocol.tracks import (
    FISSION_BATCHES,
    MAX_HISTORIES,
    MAX_STATES,
    PARTICLE_CODES,
    PointSource,
    TrackData,
    TrackEvent,
    TrackRequest,
    TrackSummary,
)

from ...core.icons import icon
from ...core.settings import AppSettings
from ...core.track_session import TrackSession
from ...render import palette
from ...render.palette import RGB
from ...render.scales import Scale
from ...render.tracks import (
    DEFAULT_LINE_RGB,
    DEFAULT_PARTICLE_RGB,
    EVENT_COLORS,
    MarkerGroup,
    Timeline,
    TrackColor,
    TrackFilter,
    TrackScene,
    TrackStyle,
    position_of,
    with_prefix,
)
from .names import event_name, particle_label
from .overlay import TrackOverlay
from .player import SPEEDS

SLIDER_STEPS = 1000
ParentIndex = QModelIndex | QPersistentModelIndex
NO_PARENT = QModelIndex()
DEFAULT_SLAB = 1.0
SYMBOLS = (
    ("○", TrackEvent.SOURCE),
    ("●", TrackEvent.COLLISION),
    ("·", TrackEvent.CROSSING),
    ("✕", TrackEvent.ABSORBED),
    ("△", TrackEvent.LEAKED),
    ("□", TrackEvent.KILLED),
)


def parse_number(text: str) -> float | None:
    """A number typed with a dot or a comma; None when empty or not a number."""
    text = text.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class ColorButton(QToolButton):
    colorChanged = Signal(tuple)
    """(r, g, b)"""

    def __init__(self, rgb: RGB, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.rgb = rgb
        self.setToolTip(title)
        self.setIconSize(QSize(16, 16))
        self.clicked.connect(self._choose)
        self.set_rgb(rgb)

    def set_rgb(self, rgb: RGB) -> None:
        self.rgb = rgb
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(*rgb))
        painter = QPainter(pixmap)
        painter.setPen(QColor(90, 90, 90))
        painter.drawRect(0, 0, 15, 15)
        painter.end()
        self.setIcon(QIcon(pixmap))

    def _choose(self) -> None:
        color = QColorDialog.getColor(QColor(*self.rgb), self, self.title)
        if color.isValid():
            self.set_rgb((color.red(), color.green(), color.blue()))
            self.colorChanged.emit(self.rgb)


class EnergyBar(QWidget):
    """The energy colours with decade ticks."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scale: Scale | None = None
        self.colors: list[RGB] = []
        self.setMinimumHeight(34)

    def set_scale(self, scale: Scale | None, colors: list[RGB]) -> None:
        self.scale, self.colors = scale, colors
        self.update()

    def paintEvent(self, event) -> None:
        if self.scale is None or not self.colors:
            return
        painter = QPainter(self)
        width = self.width() - 2
        bar = 12
        for i, rgb in enumerate(self.colors):
            x0 = 1 + round(i * width / len(self.colors))
            x1 = 1 + round((i + 1) * width / len(self.colors))
            painter.fillRect(x0, 1, x1 - x0, bar, QColor(*rgb))
        painter.setPen(self.palette().color(self.foregroundRole()))
        metrics = painter.fontMetrics()
        ticks = self.scale.ticks(6)
        for value in ticks:
            x = 1 + self.scale.position(value) * width
            painter.drawLine(round(x), bar + 1, round(x), bar + 4)
            text = with_prefix(value, "eV")
            w = metrics.horizontalAdvance(text)
            left = min(max(0.0, x - w / 2), self.width() - w)
            painter.drawText(round(left), bar + 4 + metrics.ascent(), text)
        painter.end()


class EventTable(QAbstractTableModel):
    """The states of one history: what happened where, with energy, time and weight."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.scene: TrackScene | None = None
        self.states: list[int] = []
        self._first_track = 0

    def headers(self) -> list[str]:
        return [
            self.tr("Step"),
            self.tr("Track"),
            self.tr("Particle"),
            self.tr("Event"),
            self.tr("Energy"),
            self.tr("Time"),
            self.tr("Weight"),
            self.tr("Cell"),
            self.tr("Material"),
            "x [cm]",
            "y [cm]",
            "z [cm]",
        ]

    def set_states(self, scene: TrackScene | None, states: list[int]) -> None:
        self.beginResetModel()
        self.scene, self.states = scene, states
        self._first_track = int(scene.state_track[states[0]]) if scene and states else 0
        self.endResetModel()

    def rowCount(self, parent: ParentIndex = NO_PARENT) -> int:
        return 0 if parent.isValid() else len(self.states)

    def columnCount(self, parent: ParentIndex = NO_PARENT) -> int:
        return 0 if parent.isValid() else 12

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers()[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if self.scene is None or not index.isValid():
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() in (2, 3):
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        data = self.scene.data
        state = self.states[index.row()]
        column = index.column()
        track = int(self.scene.state_track[state])
        if column == 0:
            return str(int(data.step[state]))
        if column == 1:
            return str(track - self._first_track + 1)
        if column == 2:
            return particle_label(int(data.track_particle[track]))
        if column == 3:
            return event_name(int(data.event[state]))
        if column == 4:
            return with_prefix(float(data.energy[state]), "eV")
        if column == 5:
            return with_prefix(float(data.time[state]), "s")
        if column == 6:
            return f"{float(data.weight[state]):.4g}"
        if column == 7:
            return str(int(data.cell[state]))
        if column == 8:
            material = int(data.material[state])
            return self.tr("void") if material < 0 else str(material)
        return f"{float(data.position[state][column - 9]):.4f}"


class TracksWindow(QWidget):
    moveCutsRequested = Signal(object)
    """(x, y, z): move both views' cut planes through this point."""

    def __init__(
        self,
        overlay: TrackOverlay,
        session: TrackSession,
        settings: AppSettings,
        inspected: Callable[[], tuple[float, float, float] | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Tool)
        self.overlay = overlay
        self.player = overlay.player
        self.session = session
        self.settings = settings
        self.inspected = inspected
        """The point the user inspected last in a view, if any."""
        self.info: ModelInfo | None = None
        self.setWindowTitle(self.tr("Particle tracks"))
        self._elapsed = 0.0
        self._busy_timer = QTimer(self, interval=500)
        self._busy_timer.timeout.connect(self._tick_busy)
        saved = settings.tracks_style

        # -- generate --------------------------------------------------------------------
        self.histories = QSpinBox()
        self.histories.setRange(1, MAX_HISTORIES)
        self.histories.setValue(settings.tracks_histories)
        self.start = QComboBox()
        self.start.addItem(self.tr("The model's source"), "model")
        self.start.addItem(
            self.tr("The fission source (after {batches} batches)").format(batches=FISSION_BATCHES),
            "fission",
        )
        self.start.addItem(self.tr("A point source"), "point")
        self.start.setToolTip(
            self.tr(
                "The model's source is where an eigenvalue run starts (its first generation). "
                "The fission source is where fissions happen once the source has settled."
            )
        )
        self.point_particle = QComboBox()
        self.point_particle.addItem(particle_label(PARTICLE_CODES["neutron"]), "neutron")
        self.point_particle.addItem(particle_label(PARTICLE_CODES["photon"]), "photon")
        self.point_energy = QLineEdit("2e6")
        self.point_energy.setToolTip(self.tr("Energy of the source particles [eV]"))
        self.point_xyz = []
        for _ in range(3):
            spin = QDoubleSpinBox()
            spin.setRange(-1e6, 1e6)
            spin.setDecimals(4)
            spin.setKeyboardTracking(False)
            self.point_xyz.append(spin)
        self.use_inspected = QPushButton(self.tr("Use the inspected point"))
        self.use_inspected.setToolTip(self.tr("Click a point in a geometry view first."))
        self.use_inspected.clicked.connect(self._use_inspected)
        self.point_box = QWidget()
        point_grid = QGridLayout(self.point_box)
        point_grid.setContentsMargins(0, 0, 0, 0)
        point_grid.addWidget(self.point_particle, 0, 0)
        point_grid.addWidget(self.point_energy, 0, 1)
        point_grid.addWidget(QLabel("eV"), 0, 2)
        xyz = QHBoxLayout()
        for name, spin in zip("xyz", self.point_xyz, strict=True):
            xyz.addWidget(QLabel(name))
            xyz.addWidget(spin, 1)
        xyz.addWidget(QLabel("cm"))
        point_grid.addLayout(xyz, 1, 0, 1, 3)
        point_grid.addWidget(self.use_inspected, 2, 0, 1, 3)
        self.seed = QSpinBox()
        self.seed.setRange(0, 2**31 - 1)
        self.seed.setSpecialValueText(self.tr("random"))
        self.seed.setToolTip(self.tr("The same seed gives the same tracks."))
        self.weight_windows = QCheckBox(self.tr("Weight windows (splitting and roulette)"))
        self.weight_windows.setChecked(True)
        self.photons = QCheckBox(self.tr("Photon transport"))
        self.generate_button = QPushButton(self.tr("Generate"))
        self.generate_button.setDefault(True)
        self.generate_button.clicked.connect(self.generate)
        self.cancel_button = QPushButton(self.tr("Cancel"))
        self.cancel_button.clicked.connect(self.session.cancel)
        self.cancel_button.setEnabled(False)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        generate_box = QGroupBox(self.tr("Generate"))
        form = QFormLayout(generate_box)
        form.addRow(self.tr("Histories:"), self.histories)
        form.addRow(self.tr("Start from:"), self.start)
        form.addRow("", self.point_box)
        form.addRow(self.tr("Seed:"), self.seed)
        form.addRow("", self.weight_windows)
        form.addRow("", self.photons)
        buttons = QHBoxLayout()
        buttons.addWidget(self.generate_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        form.addRow(buttons)
        form.addRow(self.status)

        # -- display -----------------------------------------------------------------------
        self.show_tracks = QCheckBox(self.tr("Show on the geometry views"))
        self.show_tracks.setChecked(True)
        self.fade = QCheckBox(self.tr("Fade the geometry"))
        self.fade.setChecked(settings.tracks_fade)
        self.draw = QComboBox()
        self.draw.addItem(self.tr("Particles only"), "0")
        self.draw.addItem(self.tr("Particles and track lines"), "1")
        self._select(self.draw, saved["lines"])
        self.particle_color = self._color_combo(with_event=True)
        self._select(self.particle_color, saved["particle_color"])
        self.particle_rgb = ColorButton(
            palette.parse_hex(saved["particle_rgb"]) or DEFAULT_PARTICLE_RGB,
            self.tr("Colour of the particles"),
        )
        self.line_color = self._color_combo(with_event=False)
        self._select(self.line_color, saved["line_color"])
        self.line_rgb = ColorButton(
            palette.parse_hex(saved["line_rgb"]) or DEFAULT_LINE_RGB,
            self.tr("Colour of the track lines"),
        )
        self.energy_bar = EnergyBar()
        self.legend = QLabel()
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.markers: dict[MarkerGroup, QCheckBox] = {
            MarkerGroup.BIRTHS: QCheckBox(self.tr("births")),
            MarkerGroup.COLLISIONS: QCheckBox(self.tr("collisions")),
            MarkerGroup.CROSSINGS: QCheckBox(self.tr("surface crossings")),
            MarkerGroup.ENDS: QCheckBox(self.tr("ends")),
        }
        chosen = set(saved["markers"].split(","))
        for group, box in self.markers.items():
            box.setChecked(group.value in chosen)
        self.near_cut = QCheckBox(self.tr("Only near the cut plane, ±"))
        self.near_cut.setToolTip(
            self.tr("Each view shows only what lies this close to its cut plane.")
        )
        self.slab = QDoubleSpinBox()
        self.slab.setRange(0.001, 1e5)
        self.slab.setDecimals(3)
        self.slab.setValue(DEFAULT_SLAB)
        self.slab.setSuffix(" cm")
        self.slab.setKeyboardTracking(False)

        display = QWidget()
        display_form = QFormLayout(display)
        display_form.addRow(self.show_tracks)
        display_form.addRow(self.fade)
        display_form.addRow(self.tr("Draw:"), self.draw)
        display_form.addRow(
            self.tr("Particles:"), self._row(self.particle_color, self.particle_rgb)
        )
        display_form.addRow(self.tr("Track lines:"), self._row(self.line_color, self.line_rgb))
        display_form.addRow(self.energy_bar)
        marker_row = QHBoxLayout()
        for box in self.markers.values():
            marker_row.addWidget(box)
        marker_row.addStretch()
        display_form.addRow(self.tr("Markers:"), marker_row)
        display_form.addRow(self.legend)
        display_form.addRow(self._row(self.near_cut, self.slab))

        # -- filter --------------------------------------------------------------------------
        self.first = QSpinBox()
        self.last = QSpinBox()
        for spin in (self.first, self.last):
            spin.setRange(1, 1)
        self.particles: dict[int, QCheckBox] = {}
        particle_row = QHBoxLayout()
        for code in PARTICLE_CODES.values():
            box = QCheckBox(particle_label(code))
            box.setChecked(True)
            self.particles[code] = box
            particle_row.addWidget(box)
        particle_row.addStretch()
        self.energy_min = QLineEdit()
        self.energy_max = QLineEdit()
        for edit in (self.energy_min, self.energy_max):
            edit.setPlaceholderText(self.tr("any"))
        filter_page = QWidget()
        filter_form = QFormLayout(filter_page)
        histories_row = QHBoxLayout()
        histories_row.addWidget(self.first)
        histories_row.addWidget(QLabel("–"))
        histories_row.addWidget(self.last)
        histories_row.addStretch()
        filter_form.addRow(self.tr("Histories:"), histories_row)
        filter_form.addRow(self.tr("Particles:"), particle_row)
        energy_row = QHBoxLayout()
        energy_row.addWidget(self.energy_min)
        energy_row.addWidget(QLabel("–"))
        energy_row.addWidget(self.energy_max)
        energy_row.addWidget(QLabel("eV"))
        filter_form.addRow(self.tr("Energy:"), energy_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(display, self.tr("Display"))
        self.tabs.addTab(filter_page, self.tr("Filter"))

        # -- playback ------------------------------------------------------------------------
        self.events_timeline = QRadioButton(self.tr("Event steps"))
        self.events_timeline.setToolTip(self.tr("All histories advance one event per step."))
        self.time_timeline = QRadioButton(self.tr("Time of flight"))
        self.time_timeline.setToolTip(
            self.tr("Real time since the start of each history, on a log scale.")
        )
        self.timeline_group = QButtonGroup(self)
        self.timeline_group.addButton(self.events_timeline)
        self.timeline_group.addButton(self.time_timeline)
        (self.time_timeline if saved["timeline"] == "time" else self.events_timeline).setChecked(
            True
        )
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.position_label = QLabel()
        self.position_label.setMinimumWidth(110)
        self.to_start_button = self._tool_button("mdi6.skip-backward", self.tr("To the start"))
        self.back_button = self._tool_button("mdi6.step-backward", self.tr("One step back"))
        self.play_button = self._tool_button("mdi6.play", self.tr("Play or pause"))
        self.forward_button = self._tool_button("mdi6.step-forward", self.tr("One step forward"))
        self.to_end_button = self._tool_button(
            "mdi6.skip-forward", self.tr("To the end: everything")
        )
        self.speed = QComboBox()
        self.speed.setToolTip(self.tr("Playback speed"))
        self.loop = QCheckBox(self.tr("Loop"))
        playback_box = QGroupBox(self.tr("Playback"))
        playback = QVBoxLayout(playback_box)
        timeline_row = QHBoxLayout()
        timeline_row.addWidget(self.events_timeline)
        timeline_row.addWidget(self.time_timeline)
        timeline_row.addStretch()
        playback.addLayout(timeline_row)
        slider_row = QHBoxLayout()
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.position_label)
        playback.addLayout(slider_row)
        button_row = QHBoxLayout()
        for button in (
            self.to_start_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.to_end_button,
        ):
            button_row.addWidget(button)
        button_row.addWidget(self.speed)
        button_row.addWidget(self.loop)
        button_row.addStretch()
        playback.addLayout(button_row)

        # -- the followed history -----------------------------------------------------------
        self.follow = QCheckBox(self.tr("Follow history"))
        self.follow_number = QSpinBox()
        self.follow_number.setRange(1, 1)
        self.table_model = EventTable(self)
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setToolTip(
            self.tr("Click: go to this event. Double-click: move the cut planes through it.")
        )
        self.history_page = QWidget()
        history = QVBoxLayout(self.history_page)
        follow_row = QHBoxLayout()
        follow_row.addWidget(self.follow)
        follow_row.addWidget(self.follow_number)
        follow_row.addStretch()
        history.addLayout(follow_row)
        history.addWidget(self.table, 1)
        self.tabs.addTab(self.history_page, self.tr("History"))

        layout = QVBoxLayout(self)
        layout.addWidget(generate_box)
        layout.addWidget(playback_box)
        layout.addWidget(self.tabs, 1)
        self.resize(500, 900)

        # -- wiring ----------------------------------------------------------------------------
        self.start.currentIndexChanged.connect(self._update_point_box)
        self.session.generated.connect(self.show_tracks_data)
        self.session.failed.connect(self._failed)
        self.session.busyChanged.connect(self._busy)
        self.show_tracks.toggled.connect(self.overlay.set_visible)
        self.fade.toggled.connect(self._fade_changed)
        for widget in (self.draw, self.particle_color, self.line_color):
            widget.currentIndexChanged.connect(self._style_changed)
        for button in (self.particle_rgb, self.line_rgb):
            button.colorChanged.connect(self._style_changed)
        for box in (*self.markers.values(), self.near_cut):
            box.toggled.connect(self._style_changed)
        self.slab.valueChanged.connect(self._style_changed)
        for spin in (self.first, self.last, self.follow_number):
            spin.valueChanged.connect(self._filter_changed)
        for box in (*self.particles.values(), self.follow):
            box.toggled.connect(self._filter_changed)
        for edit in (self.energy_min, self.energy_max):
            edit.editingFinished.connect(self._filter_changed)
        self.timeline_group.buttonToggled.connect(self._timeline_changed)
        self.slider.valueChanged.connect(self._slider_moved)
        self.to_start_button.clicked.connect(self.player.to_start)
        self.back_button.clicked.connect(lambda: self.player.step(-1))
        self.play_button.clicked.connect(self.player.toggle)
        self.forward_button.clicked.connect(lambda: self.player.step(1))
        self.to_end_button.clicked.connect(self.player.to_end)
        self.speed.currentIndexChanged.connect(self._speed_chosen)
        self.loop.toggled.connect(self.player.set_loop)
        self.player.changed.connect(self._show_position)
        self.overlay.followRequested.connect(self.follow_history)
        self.table.selectionModel().currentRowChanged.connect(self._row_chosen)
        self.table.doubleClicked.connect(self._row_activated)

        self._fill_speeds()
        self._style_changed()
        self._update_point_box()
        self._set_scene_controls(False)
        self.status.setText(self.tr("No tracks yet: choose the histories and press Generate."))
        self._show_position()

    # -- helpers -------------------------------------------------------------------------------

    def _color_combo(self, *, with_event: bool) -> QComboBox:
        combo = QComboBox()
        combo.addItem(self.tr("by energy"), TrackColor.ENERGY.value)
        combo.addItem(self.tr("by particle type"), TrackColor.PARTICLE.value)
        if with_event:
            combo.addItem(self.tr("by event"), TrackColor.EVENT.value)
        combo.addItem(self.tr("by history"), TrackColor.HISTORY.value)
        combo.addItem(self.tr("one colour"), TrackColor.FIXED.value)
        return combo

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):  # findData misses StrEnum values (see CLAUDE.md)
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    @staticmethod
    def _row(*widgets: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch()
        return row

    def _tool_button(self, name: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setProperty("icon_name", name)
        button.setIcon(icon(name))
        button.setToolTip(tip)
        return button

    def refresh_icons(self) -> None:
        for button in (
            self.to_start_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.to_end_button,
        ):
            button.setIcon(icon(str(button.property("icon_name"))))

    # -- model ----------------------------------------------------------------------------------

    def set_model(self, info: ModelInfo | None) -> None:
        """The open model changed; tracks of another model (or version) are cleared."""
        previous = self.info
        self.info = info
        scene = self.overlay.scene
        if scene is not None and (info is None or scene.model_id != info.model_id):
            self.overlay.set_scene(None)
            self._set_scene_controls(False)
            self.table_model.set_states(None, [])
            self.status.setText(
                self.tr("The model changed: generate the tracks again.")
                if info is not None
                else self.tr("No model is open.")
            )
        if info is None:
            self.setWindowTitle(self.tr("Particle tracks"))
            self.generate_button.setEnabled(False)
            return
        self.setWindowTitle(self.tr("Particle tracks — {model}").format(model=info.source))
        self.generate_button.setEnabled(not self.session.busy)
        eigenvalue = (info.settings.run_mode or "eigenvalue") == "eigenvalue"
        item = self.start.model().item(1)  # type: ignore[attr-defined]
        if item is not None:
            item.setEnabled(eigenvalue)
        if not eigenvalue and self.start.currentData() == "fission":
            self.start.setCurrentIndex(0)
        self.weight_windows.setEnabled(info.settings.weight_windows_on)
        self.weight_windows.setToolTip(
            "" if info.settings.weight_windows_on else self.tr("The model uses no weight windows.")
        )
        if previous is None or previous.source != info.source:
            self.photons.setChecked(info.settings.photon_transport)
            lo, hi = info.bounding_box.lower_left, info.bounding_box.upper_right
            for spin, a, b in zip(self.point_xyz, lo, hi, strict=True):
                spin.setValue(0.0 if a <= 0.0 <= b else (a + b) / 2)

    def _update_point_box(self) -> None:
        self.point_box.setVisible(self.start.currentData() == "point")

    def _use_inspected(self) -> None:
        point = self.inspected()
        if point is None:
            self.status.setText(self.tr("Click a point in a geometry view first."))
            return
        for spin, value in zip(self.point_xyz, point, strict=True):
            spin.setValue(value)

    # -- generate -----------------------------------------------------------------------------

    def request(self) -> TrackRequest:
        energy = parse_number(self.point_energy.text())
        point = PointSource(
            particle=self.point_particle.currentData(),
            position=(
                self.point_xyz[0].value(),
                self.point_xyz[1].value(),
                self.point_xyz[2].value(),
            ),
            energy=energy if energy is not None and energy > 0 else 2.0e6,
        )
        return TrackRequest(
            histories=self.histories.value(),
            start=self.start.currentData(),
            point=point,
            seed=self.seed.value() or None,
            weight_windows=self.weight_windows.isChecked(),
            photon_transport=self.photons.isChecked(),
        )

    def generate(self) -> None:
        if self.info is None:
            return
        self.settings.tracks_histories = self.histories.value()
        if not self.session.generate(self.info.model_id, self.request()):
            self.status.setText(self.tr("The OpenMC backend is not ready yet."))

    def _busy(self, busy: bool) -> None:
        self.generate_button.setEnabled(not busy and self.info is not None)
        self.cancel_button.setEnabled(busy)
        if busy:
            self._elapsed = 0.0
            self._busy_timer.start()
            self._tick_busy()
        else:
            self._busy_timer.stop()
            if self.overlay.scene is None:
                self.status.setText(
                    self.tr("No tracks yet: choose the histories and press Generate.")
                )

    def _tick_busy(self) -> None:
        self.status.setText(
            self.tr("Tracking {count} histories… {seconds:.0f} s").format(
                count=self.histories.value(), seconds=self._elapsed
            )
        )
        self._elapsed += self._busy_timer.interval() / 1000

    def _failed(self, message: str, detail: str) -> None:
        self.status.setText(message)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Particle tracks"))
        box.setText(message)
        if detail:
            box.setInformativeText(detail.strip().splitlines()[-1][:300])
            box.setDetailedText(detail)
        box.exec()

    def show_tracks_data(self, data: TrackData) -> None:
        scene = TrackScene(data)
        self.overlay.set_scene(scene)
        n = scene.histories
        for spin, value in ((self.first, 1), (self.last, n), (self.follow_number, 1)):
            spin.blockSignals(True)
            spin.setRange(1, max(1, n))
            spin.setValue(value)
            spin.blockSignals(False)
        for code, box in self.particles.items():
            box.blockSignals(True)
            box.setVisible(code in scene.particles)
            box.setChecked(True)
            box.blockSignals(False)
        self.energy_min.clear()
        self.energy_max.clear()
        self.follow.blockSignals(True)
        self.follow.setChecked(False)
        self.follow.blockSignals(False)
        self._set_scene_controls(True)
        self.status.setText(self.summary_text(data.summary))
        self._timeline_changed()
        self._style_changed()
        self._filter_changed()

    def summary_text(self, s: TrackSummary) -> str:
        e = TrackEvent
        rows = [
            (
                self.tr("Histories"),
                self.tr("{n} (seed {seed}, {seconds:.1f} s)").format(
                    n=s.histories, seed=s.seed, seconds=s.seconds
                ),
            ),
            (
                self.tr("Particle tracks"),
                self.tr("{n} (split copies {split}, secondary particles {secondary})").format(
                    n=s.tracks, split=s.count(e.SPLIT), secondary=s.count(e.SECONDARY)
                ),
            ),
            (
                self.tr("Events"),
                self.tr(
                    "collisions {n} · surface crossings {crossings} · reflections {reflections}"
                ).format(
                    n=s.count(e.COLLISION),
                    crossings=s.count(e.CROSSING),
                    reflections=s.count(e.REFLECTION),
                ),
            ),
            (
                self.tr("Ends"),
                self.tr("absorbed {absorbed} · leaked {leaked} · killed {killed}").format(
                    absorbed=s.count(e.ABSORBED), leaked=s.count(e.LEAKED), killed=s.count(e.KILLED)
                ),
            ),
        ]
        html = "<table>" + "".join(
            f"<tr><td><b>{name}</b>&nbsp;&nbsp;</td><td>{value}</td></tr>" for name, value in rows
        )
        html += "</table>"
        notes = []
        if s.run_mode == "eigenvalue":
            notes.append(
                self.tr(
                    "Eigenvalue model: an absorption may be a fission, whose neutrons start the "
                    "next generation. Killed: roulette or the energy cutoff after a collision."
                )
            )
        if s.dropped:
            notes.append(
                self.tr("{n} more histories were left out (at most {states} states).").format(
                    n=s.dropped, states=MAX_STATES
                )
            )
        notes += [self.tr("OpenMC: {warning}").format(warning=w) for w in s.warnings[:3]]
        return html + "".join(f"<p>{note}</p>" for note in notes)

    def _set_scene_controls(self, on: bool) -> None:
        for widget in (self.tabs, self.slider, self.speed, self.loop, self.follow):
            widget.setEnabled(on)
        for button in (
            self.to_start_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.to_end_button,
            self.events_timeline,
            self.time_timeline,
        ):
            button.setEnabled(on)
        self.follow_number.setEnabled(on and self.follow.isChecked())

    # -- style and filter ----------------------------------------------------------------------

    def style(self) -> TrackStyle:
        return TrackStyle(
            lines=self.draw.currentData() == "1",
            particle_color=TrackColor(self.particle_color.currentData()),
            particle_rgb=self.particle_rgb.rgb,
            line_color=TrackColor(self.line_color.currentData()),
            line_rgb=self.line_rgb.rgb,
            markers=frozenset(g for g, box in self.markers.items() if box.isChecked()),
            slab=self.slab.value() if self.near_cut.isChecked() else None,
        )

    def _style_changed(self, *args) -> None:
        style = self.style()
        self.overlay.set_style(style)
        self.line_color.setEnabled(style.lines)
        self.particle_rgb.setEnabled(style.particle_color is TrackColor.FIXED)
        self.line_rgb.setEnabled(style.lines and style.line_color is TrackColor.FIXED)
        self.slab.setEnabled(style.slab is not None)
        scene = self.overlay.scene
        energy = style.particle_color is TrackColor.ENERGY or (
            style.lines and style.line_color is TrackColor.ENERGY
        )
        self.energy_bar.setVisible(energy)
        if scene is not None:
            self.energy_bar.set_scale(scene.energy_scale, scene.energy_colors)
        else:
            self.energy_bar.set_scale(None, [])
        self._update_legend(style)
        self.settings.set_tracks_style(
            {
                "lines": "1" if style.lines else "0",
                "particle_color": style.particle_color.value,
                "particle_rgb": palette.to_hex(style.particle_rgb),
                "line_color": style.line_color.value,
                "line_rgb": palette.to_hex(style.line_rgb),
                "markers": ",".join(g.value for g in MarkerGroup if g in style.markers),
            }
        )

    def _update_legend(self, style: TrackStyle) -> None:
        names = {
            TrackEvent.SOURCE: self.tr("birth"),
            TrackEvent.COLLISION: self.tr("collision"),
            TrackEvent.CROSSING: self.tr("crossing"),
            TrackEvent.ABSORBED: self.tr("absorbed"),
            TrackEvent.LEAKED: self.tr("leaked"),
            TrackEvent.KILLED: self.tr("killed"),
        }
        parts = []
        for symbol, event in SYMBOLS:
            if style.particle_color is TrackColor.EVENT:
                symbol = (
                    f'<span style="color:{palette.to_hex(EVENT_COLORS[event])}">{symbol}</span>'
                )
            parts.append(f"{symbol}&nbsp;{names[event]}")
        self.legend.setText("&nbsp;&nbsp; ".join(parts))

    def _fade_changed(self, on: bool) -> None:
        self.settings.tracks_fade = on
        self.overlay.set_fade(on)

    def track_filter(self) -> TrackFilter:
        scene = self.overlay.scene
        present = set(scene.particles) if scene is not None else set()
        chosen = frozenset(
            c for c, box in self.particles.items() if box.isChecked() and c in present
        )
        return TrackFilter(
            first=self.first.value(),
            last=self.last.value(),
            particles=None if chosen == present else chosen,
            energy_min=parse_number(self.energy_min.text()),
            energy_max=parse_number(self.energy_max.text()),
            history=self.follow_number.value() if self.follow.isChecked() else None,
        )

    def _filter_changed(self, *args) -> None:
        filt = self.track_filter()
        self.overlay.set_filter(filt)
        self.follow_number.setEnabled(self.follow.isEnabled() and self.follow.isChecked())
        scene = self.overlay.scene
        states = scene.history_states(filt.history).tolist() if scene and filt.history else []
        if states != self.table_model.states:
            self.table_model.set_states(scene, states)
            self.overlay.set_mark(None)

    def follow_history(self, number: int) -> None:
        """Follow history `number` (1-based), e.g. chosen in a view's menu."""
        if self.overlay.scene is None:
            return
        self.follow_number.setValue(number)
        self.follow.setChecked(True)
        self._filter_changed()
        self.tabs.setCurrentWidget(self.history_page)
        self.show()
        self.raise_()

    # -- playback ------------------------------------------------------------------------------

    def _timeline(self) -> Timeline:
        return Timeline.TIME if self.time_timeline.isChecked() else Timeline.EVENTS

    def _timeline_changed(self, button=None, checked: bool = True) -> None:
        if not checked:
            return  # the other button of the pair reports its check
        timeline = self._timeline()
        self.settings.set_tracks_style({"timeline": timeline.value})
        scene = self.overlay.scene
        if scene is not None:
            self.player.set_timeline(timeline, *scene.timeline_range(timeline))
        self._fill_speeds()

    def _fill_speeds(self) -> None:
        timeline = self._timeline()
        self.speed.blockSignals(True)
        self.speed.clear()
        for value in SPEEDS[timeline]:
            if timeline is Timeline.EVENTS:
                text = self.tr("{n:g} steps/s").format(n=value)
            else:
                text = self.tr("{n:g} decades/s").format(n=value)
            self.speed.addItem(text, value)
        current = self.player.speed[timeline]
        values = [self.speed.itemData(i) for i in range(self.speed.count())]
        self.speed.setCurrentIndex(values.index(current) if current in values else 0)
        self.speed.blockSignals(False)

    def _speed_chosen(self, index: int) -> None:
        value = self.speed.itemData(index)
        if value is not None and self._timeline() is self.player.timeline:
            self.player.set_speed(float(value))

    def _slider_moved(self, value: int) -> None:
        self.player.seek_fraction(value / SLIDER_STEPS)

    def _show_position(self) -> None:
        player = self.player
        self.slider.blockSignals(True)
        self.slider.setValue(round(player.fraction * SLIDER_STEPS))
        self.slider.blockSignals(False)
        self.play_button.setProperty("icon_name", "mdi6.pause" if player.playing else "mdi6.play")
        self.play_button.setIcon(icon(str(self.play_button.property("icon_name"))))
        scene = self.overlay.scene
        if scene is None:
            self.position_label.setText("")
        elif player.at_end:
            self.position_label.setText(self.tr("all"))
        elif player.timeline is Timeline.EVENTS:
            self.position_label.setText(
                self.tr("step {step:.1f} / {last}").format(
                    step=player.position, last=scene.max_step
                )
            )
        else:
            self.position_label.setText("t = " + with_prefix(10.0**player.position, "s"))

    # -- event table ---------------------------------------------------------------------------

    def _row_chosen(self, current: QModelIndex, previous: QModelIndex) -> None:
        scene = self.overlay.scene
        if scene is None or not current.isValid() or current.row() >= len(self.table_model.states):
            return
        state = self.table_model.states[current.row()]
        self.overlay.set_mark(state)
        self.player.pause()
        self.player.seek(position_of(scene, self.player.timeline, state))

    def _row_activated(self, index: QModelIndex) -> None:
        scene = self.overlay.scene
        if scene is None or not index.isValid():
            return
        state = self.table_model.states[index.row()]
        x, y, z = (float(v) for v in scene.data.position[state])
        self.moveCutsRequested.emit((x, y, z))

    # -- window ---------------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.settings.tracks_window = self.saveGeometry()
        super().closeEvent(event)
