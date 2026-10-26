"""Test doubles for runs: a recording event hub, a model service, a manager without OpenMC."""

import sys
from pathlib import Path

from pyvised_backend.config import Config
from pyvised_backend.errors import ServiceError
from pyvised_backend.models import OpenedModel
from pyvised_backend.runs import RunManager
from pyvised_protocol.runs import RESULTS_FILE, Event, RunResults
from pyvised_protocol.schemas import BoundingBox, LibraryInfo, ModelInfo

FAKE = Path(__file__).parent / "fixtures" / "fake_openmc.py"
MODEL = """<?xml version='1.0' encoding='utf-8'?>
<model><materials/><geometry/>
  <settings>
    <run_mode>eigenvalue</run_mode><particles>100</particles>
    <batches>{batches}</batches><inactive>2</inactive>
  </settings>
</model>
"""


class Hub:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class FakeModels:
    def __init__(self, cache: Path, batches: int = 20) -> None:
        self.cache = cache
        self.batches = batches
        self.opened = []

    def library(self, library_id):
        if library_id not in (None, "builtin"):
            raise ServiceError("unknown_library", str(library_id))
        return LibraryInfo(id="builtin", name="ENDF/B-VII.1", path="/xs.xml", builtin=True)

    def open(self, request):
        self.opened.append(request)
        folder = self.cache / f"m{len(self.opened)}"
        folder.mkdir(parents=True)
        (folder / "model.xml").write_text(MODEL.format(batches=self.batches), encoding="utf-8")
        (folder / "info.json").write_text("{}", encoding="utf-8")
        info = ModelInfo(
            model_id=f"m{len(self.opened)}",
            source=request.path,
            kind="python",
            library="builtin",
            params={"rod": 1.0, **request.params},
            bounding_box=BoundingBox(lower_left=(-1, -1, -1), upper_right=(1, 1, 1)),
            root_universe=0,
        )
        return OpenedModel(info, folder)


class Manager(RunManager):
    """Summaries and snapshots without OpenMC: the newest statepoint's batch is the 'result'."""

    snapshotted: list[list[str]]

    def _snapshot(self, folder, paths):
        self.snapshotted.append([p.name for p in paths])
        return paths

    def _summarise(self, folder, run_id):
        points = sorted(folder.glob("statepoint.*.h5"))
        if not points:
            return None
        results = RunResults(
            run_id=run_id,
            statepoint=points[-1].name,
            run_mode="eigenvalue",
            batches=int(points[-1].name.split(".")[1]),
        )
        (folder / RESULTS_FILE).write_text(results.model_dump_json(), encoding="utf-8")
        return {"statepoint": points[-1].name, "k_eff": 1.0, "k_std": 0.001}


def manager_for(
    workspace: Path, tmp_path: Path, batches: int = 20, hub=None
) -> tuple[Manager, Hub]:
    config = Config(
        token="t", workspace=workspace, cross_sections=None, custom_data=tmp_path / "c", cpus=4
    )
    hub = hub if hub is not None else Hub()
    manager = Manager(
        config,
        FakeModels(tmp_path / "cache", batches),
        hub,  # type: ignore[arg-type]
        command=[sys.executable, str(FAKE)],
    )
    manager.snapshotted = []
    return manager, hub
