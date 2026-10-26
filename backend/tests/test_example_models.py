"""The example models of the course tasks with real OpenMC: only inside the backend container.

The test image has the repository's examples/ at /src/examples. Each example opens with
its default parameters and without warnings, shows no overlaps on slices through its
bounding box, and runs a few particles with every tally scoring.
"""

import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openmc")

from run_doubles import Hub

from pyvised_backend.config import Config
from pyvised_backend.models import ModelService
from pyvised_backend.render import RenderPool
from pyvised_backend.runs import RunManager
from pyvised_protocol.runs import FINAL_STATES, RunInfo, RunRequest, RunSettings
from pyvised_protocol.schemas import OpenModelRequest, SliceRequest
from pyvised_protocol.slices import OVERLAP, decode_slice

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FOLDERS = sorted(p for p in EXAMPLES.glob("task*_*") if p.is_dir()) if EXAMPLES.is_dir() else []

pytestmark = [
    pytest.mark.openmc,
    pytest.mark.skipif(not FOLDERS, reason=f"no examples in {EXAMPLES}"),
]


@pytest.fixture(autouse=True)
def fresh_ids():
    import openmc

    openmc.reset_auto_ids()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    xs = os.environ.get("OPENMC_CROSS_SECTIONS")
    workspace = tmp_path / "ws"
    for folder in FOLDERS:
        shutil.copytree(folder, workspace / folder.name)
    return Config(
        token="t",
        workspace=workspace,
        cross_sections=Path(xs) if xs else None,
        custom_data=tmp_path / "custom",
        cache_dir=tmp_path / "cache",
        render_workers=1,
        cpus=2,
    )


@pytest.fixture(scope="module")
def pool(tmp_path_factory: pytest.TempPathFactory):
    pool = RenderPool(1, tmp_path_factory.mktemp("logs"))
    yield pool
    pool.shutdown()


def model_path(folder: Path) -> str:
    (model,) = [p for p in folder.iterdir() if p.suffix in (".py", ".ipynb", ".xml")]
    return f"{folder.name}/{model.name}"


def wait(manager: RunManager, run_id: str, timeout: float = 300.0) -> RunInfo:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = manager.status(run_id).run
        run = manager._active
        if info.state in FINAL_STATES and (run is None or run.finished.wait(timeout)):
            return info
        time.sleep(0.1)
    raise AssertionError("the run did not end")


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_example_opens_without_overlaps(folder: Path, config: Config, pool: RenderPool):
    opened = ModelService(config).open(OpenModelRequest(path=model_path(folder)))
    info = opened.info
    assert info.sidecar is not None and not info.warnings, info.warnings
    assert info.params == info.sidecar.parameter_defaults()
    lower, upper = np.array(info.bounding_box.lower_left), np.array(info.bounding_box.upper_right)
    centre, size = (lower + upper) / 2, upper - lower
    axes = {"xy": (0, 1, 2), "xz": (0, 2, 1), "yz": (1, 2, 0)}
    overlaps = 0
    for basis, (h, v, normal) in axes.items():
        for fraction in (0.25, 0.5, 0.75):
            origin = centre.copy()
            origin[normal] = lower[normal] + fraction * size[normal]
            request = SliceRequest(
                basis=basis,  # type: ignore[arg-type]
                origin=tuple(origin),  # type: ignore[arg-type]
                width=(float(size[h]), float(size[v])),
                pixels=(200, 200),
            )
            data = decode_slice(pool.render(opened.directory, request, {}))
            overlaps += int((data.materials == OVERLAP).sum())
    assert overlaps == 0


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_example_runs_and_every_tally_scores(folder: Path, config: Config):
    manager = RunManager(config, ModelService(config), Hub())  # type: ignore[arg-type]
    settings = RunSettings(particles=2000, batches=4, inactive=2, seed=1)
    info = manager.start(RunRequest(path=model_path(folder), settings=settings))
    final = wait(manager, info.id)
    assert final.state == "finished" and final.stop_reason == "completed", final.error
    results = manager.results(info.id)
    assert results.tallies
    for tally in results.tallies:
        assert tally.totals and all(t.value > 0 for t in tally.totals), tally.id
