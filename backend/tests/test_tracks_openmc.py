"""Real tracked runs of the pin-lattice fixture: only inside the backend container."""

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openmc")

from pyvised_backend.config import Config
from pyvised_backend.models import ModelService
from pyvised_backend.tracks import TrackService
from pyvised_protocol.schemas import OpenModelRequest
from pyvised_protocol.tracks import PointSource, TrackEvent, TrackRequest, decode_tracks

pytestmark = pytest.mark.openmc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def opened(tmp_path_factory) -> tuple[TrackService, str]:
    tmp_path = tmp_path_factory.mktemp("tracks")
    workspace = tmp_path / "ws"
    (workspace / "pins").mkdir(parents=True)
    shutil.copy(FIXTURES / "pins.py", workspace / "pins" / "pins.py")
    xs = os.environ.get("OPENMC_CROSS_SECTIONS")
    config = Config(
        token="t",
        workspace=workspace,
        cross_sections=Path(xs) if xs else None,
        custom_data=tmp_path / "custom",
        cache_dir=tmp_path / "cache",
        cpus=2,
    )
    models = ModelService(config)
    model = models.open(OpenModelRequest(path="pins/pins.py"))
    return TrackService(config, models), model.model_id


def test_model_source_tracks(opened):
    service, model_id = opened
    data = decode_tracks(service.generate(model_id, TrackRequest(histories=50, seed=3)))
    summary = data.summary
    assert data.n_histories == 50 and summary.seed == 3 and summary.run_mode == "eigenvalue"
    assert data.history_id.tolist() == list(range(1, 51))
    first = data.track_start[:-1]
    assert summary.count(TrackEvent.SOURCE) == 50
    assert np.all(data.position[first[data.track_parent < 0]] == 0.0)  # the point source
    assert summary.count(TrackEvent.LEAKED) > 0  # a small model with vacuum boundaries
    assert summary.count(TrackEvent.COLLISION) > 0 and summary.count(TrackEvent.CROSSING) > 0
    ends = data.track_start[1:] - 1
    assert set(data.event[ends].tolist()) <= {e.value for e in TrackEvent}
    assert summary.count(TrackEvent.JUMP) == 0
    again = decode_tracks(service.generate(model_id, TrackRequest(histories=50, seed=3)))
    assert np.array_equal(again.position, data.position)  # same seed, same tracks


def test_fission_source_and_point_source(opened):
    service, model_id = opened
    data = decode_tracks(service.generate(model_id, TrackRequest(histories=10, start="fission")))
    assert data.n_histories == 10
    sources = data.track_start[:-1][data.track_parent < 0]
    assert len({tuple(b) for b in data.position[sources].tolist()}) > 1  # fission sites
    assert np.all(data.time[sources] == 0.0)  # not the time of the fission that made them
    assert data.summary.warnings == []  # one active batch etc. are no news
    point = PointSource(particle="photon", position=(0.5, 0.5, 0.0), energy=1.0e6)
    data = decode_tracks(
        service.generate(model_id, TrackRequest(histories=20, start="point", point=point))
    )
    assert data.summary.photon_transport and set(data.summary.particles) == {"photon"}
    assert np.allclose(data.position[0], (0.5, 0.5, 0.0))
