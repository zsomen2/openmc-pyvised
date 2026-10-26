"""Real runs of a small model: only inside the backend container."""

import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openmc")

from pyvised_backend.config import Config
from pyvised_backend.models import ModelService
from pyvised_backend.results import SNAPSHOTS_FILE
from pyvised_backend.runs import RunManager
from pyvised_protocol import frames
from pyvised_protocol.runs import (
    FINAL_STATES,
    RESULTS_FILE,
    TALLIES_FILE,
    Event,
    RunInfo,
    RunRequest,
    RunSettings,
)

pytestmark = pytest.mark.openmc

FIXTURES = Path(__file__).parent / "fixtures"


class Hub:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
def manager(tmp_path: Path) -> RunManager:
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
        render_workers=1,
        cpus=2,
    )
    return RunManager(config, ModelService(config), Hub())  # type: ignore[arg-type]


def wait(manager: RunManager, run_id: str, timeout: float = 300.0) -> RunInfo:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = manager.status(run_id).run
        run = manager._active
        if info.state in FINAL_STATES and (run is None or run.finished.wait(timeout)):
            return info
        time.sleep(0.1)
    raise AssertionError("the run did not end")


def test_a_real_run_is_summarised(manager: RunManager):
    settings = RunSettings(particles=400, batches=12, inactive=3, seed=5)
    info = manager.start(RunRequest(path="pins/pins.py", params={"pitch": 1.5}, settings=settings))
    assert info.plan is not None and info.plan.entropy_added
    final = wait(manager, info.id)
    assert final.state == "finished" and final.stop_reason == "completed", final.error
    assert final.k_eff is not None and 0.0 < final.k_eff < 2.0 and final.k_std
    assert final.params == {"pitch": 1.5} and final.versions["openmc"].startswith("0.16")
    folder = manager.workspace / final.path
    assert [p.name for p in folder.glob("statepoint.*.h5")] == ["statepoint.12.h5"]
    assert (folder / SNAPSHOTS_FILE).is_file() and (folder / TALLIES_FILE).is_file()

    results = manager.results(info.id)
    assert (results.batches, results.inactive, results.particles) == (12, 3, 400)
    assert results.keff is not None and results.keff.combined is not None
    assert results.keff.combined.value == pytest.approx(final.k_eff)
    assert len(results.k_generation) == 12 and len(results.entropy) == 12
    assert results.rate_active and results.runtime["active batches"] > 0
    (tally,) = results.tallies
    assert (tally.id, tally.scores, tally.shape) == (4, ["flux"], [1, 1, 1])
    assert tally.filters[0].kind == "cell" and tally.filters[0].ids == [1]
    assert tally.realizations == 9 and tally.max_rel_err is not None and tally.zero_bins == 0
    convergence = results.convergence[4]
    assert convergence.binned and convergence.batches[-1] == 12
    assert len(convergence.mean) == len(convergence.batches) >= 9
    assert results.keff_convergence is not None

    frame = frames.decode(manager.tally(info.id, 4))
    assert frame.arrays["mean"].shape == (1, 1, 1)
    assert frame.meta["tally"]["id"] == 4
    assert np.isclose(frame.arrays["mean"].sum(), tally.totals[0].value)

    progress = manager.status(info.id).progress
    assert progress is not None and len(progress.k) == 12 and progress.k[0] > 0


def test_stopping_a_real_run_keeps_results(manager: RunManager):
    settings = RunSettings(particles=2000, batches=2000, inactive=2, statepoint_every=5)
    info = manager.start(RunRequest(path="pins/pins.py", settings=settings))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        progress = manager.status(info.id).progress
        if progress is not None and progress.batch >= 12:
            break
        time.sleep(0.05)
    manager.stop(info.id)
    final = wait(manager, info.id)
    assert final.state == "stopped" and final.stop_reason == "user"
    assert final.has_results and final.exit_code != 0
    results = manager.results(info.id)
    assert results.batches >= 10 and results.batches % 5 == 0
    assert (manager.workspace / final.path / RESULTS_FILE).is_file()


def test_openmc_errors_are_reported(manager: RunManager):
    folder = manager.workspace / "pins"
    text = (folder / "pins.py").read_text()
    broken = text.replace('fuel.add_nuclide("U238", 0.9)', 'fuel.add_nuclide("U250", 0.9)')
    (folder / "broken.py").write_text(broken, encoding="utf-8")
    info = manager.start(
        RunRequest(path="pins/broken.py", settings=RunSettings(particles=200, batches=4))
    )
    final = wait(manager, info.id)
    assert final.state == "failed" and final.stop_reason == "error" and not final.has_results
    assert final.error.startswith("ERROR:") and "U250" in final.error
