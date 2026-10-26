import numpy as np
import pytest
from pydantic import ValidationError

from pyvised_protocol.tracks import (
    MAX_HISTORIES,
    TrackData,
    TrackEvent,
    TrackRequest,
    TrackSummary,
    decode_tracks,
    encode_tracks,
    particle_name,
)


def two_tracks() -> TrackData:
    """One history: a neutron (3 states) and a photon born at its second state (2 states)."""
    return TrackData(
        position=np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0], [1, 0, 0], [1, 5, 0]], float),
        energy=np.array([2e6, 1e6, 1e6, 2.2e6, 2.2e6]),
        time=np.array([0.0, 1e-9, 2e-9, 1e-9, 1.1e-9]),
        weight=np.ones(5),
        cell=np.array([1, 2, 2, 2, 3]),
        instance=np.zeros(5),
        material=np.array([1, 2, 2, 2, -1]),
        event=np.array([0, 4, 8, 1, 7]),
        step=np.array([0, 1, 2, 1, 2]),
        track_start=np.array([0, 3, 5]),
        track_history=np.array([0, 0]),
        track_particle=np.array([2112, 22]),
        track_parent=np.array([-1, 0]),
        history_id=np.array([1]),
        summary=TrackSummary(model_id="m1", seed=7, histories=1, tracks=2, states=5),
    )


def test_tracks_round_trip_with_their_dtypes():
    data = decode_tracks(encode_tracks(two_tracks()))
    assert data.n_states == 5 and data.n_tracks == 2 and data.n_histories == 1
    assert data.position.dtype == np.float32 and data.position.shape == (5, 3)
    assert data.time.dtype == np.float64 and data.event.dtype == np.int8
    assert data.track_start.dtype == np.int64
    assert data.summary.model_id == "m1" and data.summary.seed == 7
    assert data.state_track().tolist() == [0, 0, 0, 1, 1]
    assert TrackEvent(int(data.event[2])) is TrackEvent.ABSORBED


def test_track_request_limits_and_defaults():
    request = TrackRequest()
    assert request.histories == 100 and request.start == "model" and request.seed is None
    assert request.point.particle == "neutron" and request.point.energy == 2.0e6
    with pytest.raises(ValidationError):
        TrackRequest(histories=MAX_HISTORIES + 1)
    with pytest.raises(ValidationError):
        TrackRequest(start="somewhere")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TrackRequest.model_validate({"point": {"energy": 0}})


def test_particle_names_and_event_counts():
    assert particle_name(2112) == "neutron" and particle_name(-11) == "positron"
    assert particle_name(1000020040) == "1000020040"
    summary = TrackSummary(events={"collision": 3})
    assert summary.count(TrackEvent.COLLISION) == 3 and summary.count(TrackEvent.LEAKED) == 0
