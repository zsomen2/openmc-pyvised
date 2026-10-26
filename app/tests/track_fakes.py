"""Test doubles for particle tracks: two small histories and a fake track session.

History 1: a neutron from the origin along +x, crossing x = 2, colliding at
(2, 2, 0) (where a photon is born that leaks at (6, 2, 0)) and absorbed at
(2, 2, 4). History 2: a neutron from (0, 0, 5) killed at (0, 0, 8).
"""

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from pyvised_protocol.tracks import TrackData, TrackEvent, TrackRequest, TrackSummary

E = TrackEvent


def sample_tracks(model_id: str = "fake1") -> TrackData:
    position = [
        (0, 0, 0), (2, 0, 0), (2, 2, 0), (2, 2, 4),  # neutron, history 1
        (2, 2, 0), (6, 2, 0),  # photon, history 1
        (0, 0, 5), (0, 0, 8),  # neutron, history 2
    ]  # fmt: skip
    events = [
        E.SOURCE, E.CROSSING, E.COLLISION, E.ABSORBED,
        E.SECONDARY, E.LEAKED,
        E.SOURCE, E.KILLED,
    ]  # fmt: skip
    return TrackData(
        position=np.array(position, dtype=np.float32),
        energy=np.array([2e6, 2e6, 1e5, 1e5, 1e6, 1e6, 1e6, 5e5], dtype=np.float32),
        time=np.array([0, 1e-9, 2e-9, 1e-6, 2e-9, 2.1e-9, 0, 1e-9]),
        weight=np.ones(8, dtype=np.float32),
        cell=np.array([1, 2, 2, 2, 2, 3, 1, 1], dtype=np.int32),
        instance=np.zeros(8, dtype=np.int32),
        material=np.array([1, 2, 2, 2, 2, -1, 1, 1], dtype=np.int32),
        event=np.array(events, dtype=np.int8),
        step=np.array([0, 1, 2, 3, 2, 3, 0, 1], dtype=np.int32),
        track_start=np.array([0, 4, 6, 8]),
        track_history=np.array([0, 0, 1], dtype=np.int32),
        track_particle=np.array([2112, 22, 2112], dtype=np.int32),
        track_parent=np.array([-1, 0, -1], dtype=np.int32),
        history_id=np.array([1, 2]),
        summary=TrackSummary(
            model_id=model_id,
            seed=42,
            run_mode="eigenvalue",
            histories=2,
            tracks=3,
            states=8,
            particles={"neutron": 2, "photon": 1},
            events={
                "source": 2,
                "secondary": 1,
                "crossing": 1,
                "collision": 1,
                "absorbed": 1,
                "leaked": 1,
                "killed": 1,
            },
            max_step=3,
            seconds=0.7,
        ),
    )


class FakeTrackSession(QObject):
    """Stands in for `TrackSession`: answers with `sample_tracks` (or an error) at once."""

    generated = Signal(object)
    failed = Signal(str, str)
    busyChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, TrackRequest]] = []
        self.cancelled = 0
        self.error: tuple[str, str] | None = None
        self.busy = False
        self.ready = True

    def generate(self, model_id: str, request: TrackRequest) -> bool:
        self.requests.append((model_id, request))
        self.busy = True
        self.busyChanged.emit(True)
        QTimer.singleShot(0, lambda: self._answer(model_id))
        return True

    def _answer(self, model_id: str) -> None:
        self.busy = False
        self.busyChanged.emit(False)
        if self.error is not None:
            self.failed.emit(*self.error)
        else:
            self.generated.emit(sample_tracks(model_id))

    def cancel(self) -> None:
        self.cancelled += 1
