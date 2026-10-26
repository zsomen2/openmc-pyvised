"""The particle tracks on both geometry views: what is shown, and drawing it.

The overlay owns the tracks of the last tracked run (`TrackScene`), how they
are drawn (`TrackStyle`), what is filtered out (`TrackFilter`) and the state
marked from the event table. Whenever one of them, the playback position or a
view's cut plane changes, it computes each view's frame once the event loop is
free (several changes make one redraw) and hands it to the view's
`TrackItem`. It also answers what lies under the cursor.
"""

import logging
import time
from collections.abc import Sequence

from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtWidgets import QMenu

from pyvised_protocol.tracks import TrackEvent

from ...render.tracks import (
    TrackFilter,
    TrackScene,
    TrackStyle,
    ViewFrame,
    key_at,
    nearest_state,
    view_frame,
    with_prefix,
)
from ...render.views import BASES
from ..geometry.view_panel import ViewPanel
from .names import event_name, particle_label
from .player import TrackPlayer
from .track_item import TrackItem

log = logging.getLogger(__name__)

HOVER_PIXELS = 6.0
"""How close [px] the cursor must be to a particle state to describe it."""
BOUNDS_PAD = 0.05


class TrackOverlay(QObject):
    changed = Signal()
    """The tracks, their style, filter or visibility changed."""
    followRequested = Signal(int)
    """The view's menu asked to follow a history (1-based)."""
    drawn = Signal(float)
    """Seconds the last redraw of both views' frames took."""

    def __init__(
        self, panels: Sequence[ViewPanel], player: TrackPlayer, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.panels = list(panels)
        self.player = player
        self.scene: TrackScene | None = None
        self.style = TrackStyle()
        self.filter = TrackFilter()
        self.visible = True
        self.fade = True
        """Fade the geometry under the tracks (the geometry tab does it)."""
        self.mark: int | None = None
        self.frames: list[ViewFrame | None] = [None] * len(self.panels)
        self.items: list[TrackItem] = []
        for panel in self.panels:
            item = TrackItem()
            panel.view.add_overlay(item)
            self.items.append(item)
            panel.cutChanged.connect(self.schedule)
        player.changed.connect(self.schedule)
        self._scheduled = False
        self._bounds: dict[tuple[int, int, int], QRectF] = {}

    @property
    def shown(self) -> bool:
        return self.scene is not None and self.visible

    # -- state ---------------------------------------------------------------------------

    def set_scene(self, scene: TrackScene | None) -> None:
        self.scene = scene
        self.mark = None
        self.filter = TrackFilter()
        self._bounds.clear()
        if scene is not None:
            timeline = self.player.timeline
            self.player.set_timeline(timeline, *scene.timeline_range(timeline))
        self.schedule()
        self.changed.emit()

    def set_style(self, style: TrackStyle) -> None:
        if style != self.style:
            self.style = style
            self.schedule()

    def set_filter(self, filt: TrackFilter) -> None:
        if filt != self.filter:
            self.filter = filt
            self.schedule()

    def set_visible(self, visible: bool) -> None:
        if visible != self.visible:
            self.visible = visible
            self.schedule()
            self.changed.emit()

    def set_fade(self, fade: bool) -> None:
        if fade != self.fade:
            self.fade = fade
            self.changed.emit()

    def set_mark(self, state: int | None) -> None:
        self.mark = state
        self.schedule()

    @property
    def key(self) -> float:
        assert self.scene is not None
        return key_at(self.scene, self.player.timeline, self.player.position)

    # -- drawing ---------------------------------------------------------------------------

    def schedule(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            QTimer.singleShot(0, self.redraw)

    def redraw(self) -> None:
        self._scheduled = False
        start = time.perf_counter()
        scene = self.scene
        for index, (panel, item) in enumerate(zip(self.panels, self.items, strict=True)):
            if scene is None or not self.visible or panel.info is None:
                self.frames[index] = None
                item.set_frame(None)
                item.set_mark(None)
                continue
            axes = BASES[panel.basis]
            frame = view_frame(
                scene, self.style, self.filter, axes, panel.cut, self.player.timeline, self.key
            )
            self.frames[index] = frame
            if axes not in self._bounds:
                self._bounds[axes] = self.bounds(scene, axes)
            item.set_bounds(self._bounds[axes])
            item.set_frame(frame)
            mark = None
            if self.mark is not None:
                point = scene.data.position[self.mark]
                near = self.style.slab is None or abs(point[axes[2]] - panel.cut) <= self.style.slab
                mark = (float(point[axes[0]]), float(point[axes[1]])) if near else None
            item.set_mark(mark)
        seconds = time.perf_counter() - start
        self.drawn.emit(seconds)

    @staticmethod
    def bounds(scene: TrackScene, axes: tuple[int, int, int]) -> QRectF:
        """The rectangle the tracks can cover in a view with these world axes."""
        uv = scene.uv(axes[0], axes[1])
        if len(uv) == 0:
            return QRectF()
        low, high = uv.min(axis=0), uv.max(axis=0)
        pad = BOUNDS_PAD * max(float((high - low).max()), 1.0)
        return QRectF(
            low[0] - pad, low[1] - pad, high[0] - low[0] + 2 * pad, high[1] - low[1] + 2 * pad
        )

    # -- under the cursor ----------------------------------------------------------------

    def state_at(self, index: int, point: tuple[float, float]) -> int | None:
        frame = self.frames[index]
        if frame is None or self.scene is None:
            return None
        radius = HOVER_PIXELS * self.panels[index].view.pixel_size()
        return nearest_state(frame, point[0], point[1], radius)

    def describe(self, state: int) -> str:
        scene = self.scene
        assert scene is not None
        data = scene.data
        track = int(scene.state_track[state])
        event = TrackEvent(int(data.event[state]))
        parts = [
            self.tr("history {number}").format(number=int(scene.state_history[state]) + 1),
            particle_label(int(data.track_particle[track])),
            self.tr("{event} (step {step})").format(
                event=event_name(event), step=int(data.step[state])
            ),
            with_prefix(float(data.energy[state]), "eV"),
            "t = " + with_prefix(float(data.time[state]), "s"),
            self.tr("weight {weight:.3g}").format(weight=float(data.weight[state])),
            self.tr("cell {id}").format(id=int(data.cell[state])),
        ]
        material = int(data.material[state])
        parts.append(
            self.tr("void") if material < 0 else self.tr("material {id}").format(id=material)
        )
        return " · ".join(parts)

    def hover_text(self, index: int, point: tuple[float, float]) -> str:
        state = self.state_at(index, point)
        return "" if state is None else self.describe(state)

    def add_menu_actions(self, index: int, menu: QMenu, point: tuple[float, float]) -> None:
        state = self.state_at(index, point)
        if state is None or self.scene is None:
            return
        number = int(self.scene.state_history[state]) + 1
        menu.addSeparator()
        action = menu.addAction(self.tr("Follow history {number}").format(number=number))
        action.triggered.connect(lambda: self.followRequested.emit(number))

    def drawn_segments(self) -> int:
        return sum(frame.segments for frame in self.frames if frame is not None)
