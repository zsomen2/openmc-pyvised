"""Playback of the particle tracks: the position on the timeline, play, pause, step, speed.

The position is an event step or, on the time-of-flight timeline, log10 of the
time [s]. Playing advances it by the wall time since the last tick times the
speed, so a slow frame drops frames instead of slowing the playback down. At
the end of the timeline everything is shown; the views start there.
"""

import math

from PySide6.QtCore import QElapsedTimer, QObject, Qt, QTimer, Signal

from ...render.tracks import Timeline

FRAME_MS = 30
MAX_TICK = 0.5
"""[s] at most one tick advances by (after a stall, e.g. a dialog)."""
SPEEDS = {
    Timeline.EVENTS: (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0),
    Timeline.TIME: (0.1, 0.2, 0.5, 1.0, 2.0),
}
"""Steps per second, or decades of time per second."""
DEFAULT_SPEED = {Timeline.EVENTS: 20.0, Timeline.TIME: 0.5}
TIME_STEP = 0.1
"""Decades one step moves on the time-of-flight timeline."""


class TrackPlayer(QObject):
    changed = Signal()
    """The position, the timeline, the speed or the playing state changed."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.timeline = Timeline.EVENTS
        self.low, self.high = 0.0, 1.0
        self.position = self.high
        self.playing = False
        self.loop = False
        self.speed = dict(DEFAULT_SPEED)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()

    @property
    def at_end(self) -> bool:
        return self.position >= self.high

    @property
    def fraction(self) -> float:
        span = self.high - self.low
        return (self.position - self.low) / span if span > 0 else 1.0

    def set_timeline(self, timeline: Timeline, low: float, high: float) -> None:
        """Switch to `timeline` with its range; the playback goes to the end."""
        self.pause(emit=False)
        self.timeline = timeline
        self.low, self.high = low, max(high, low)
        self.position = self.high
        self.changed.emit()

    def set_speed(self, speed: float) -> None:
        self.speed[self.timeline] = speed
        self.changed.emit()

    def set_loop(self, on: bool) -> None:
        self.loop = on

    def play(self) -> None:
        if self.at_end:
            self.position = self.low
        self.playing = True
        self._clock.start()
        self._timer.start()
        self.changed.emit()

    def pause(self, *, emit: bool = True) -> None:
        was = self.playing
        self.playing = False
        self._timer.stop()
        if emit and was:
            self.changed.emit()

    def toggle(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def seek(self, position: float) -> None:
        self.position = min(max(position, self.low), self.high)
        self._clock.restart()
        self.changed.emit()

    def seek_fraction(self, fraction: float) -> None:
        self.seek(self.low + fraction * (self.high - self.low))

    def step(self, direction: int) -> None:
        """One event (or `TIME_STEP` decades) forward (+1) or back (-1)."""
        self.pause(emit=False)
        if self.timeline is Timeline.EVENTS:
            nearest = math.floor(self.position) if direction > 0 else math.ceil(self.position)
            self.seek(nearest + direction)
        else:
            self.seek(self.position + direction * TIME_STEP)

    def to_start(self) -> None:
        self.pause(emit=False)
        self.seek(self.low)

    def to_end(self) -> None:
        self.pause(emit=False)
        self.seek(self.high)

    def _tick(self) -> None:
        elapsed = min(self._clock.restart() / 1000.0, MAX_TICK)
        position = self.position + elapsed * self.speed[self.timeline]
        if position >= self.high:
            if self.loop and self.high > self.low:
                position = self.low
            else:
                position = self.high
                self.playing = False
                self._timer.stop()
        self.position = position
        self.changed.emit()
