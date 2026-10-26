"""Test doubles for the Simulation tab: a run API with results, a model with presets."""

from datetime import datetime
from pathlib import Path
from typing import ClassVar

import numpy as np
from fakes import FakeSession, model_info

from pyvised.backend.client import TallyData
from pyvised_protocol.runs import (
    Convergence,
    Estimate,
    KeffResults,
    LogChunk,
    RunInfo,
    RunPlan,
    RunProgress,
    RunRequest,
    RunResults,
    RunStatus,
    TallyFilterResult,
    TallyResult,
)
from pyvised_protocol.schemas import ModelInfo, SettingsInfo
from pyvised_protocol.sidecar import (
    LocalizedText,
    ParameterSpec,
    ReferenceResult,
    RunPreset,
    Sidecar,
    TallyNormalisation,
)

ENERGY = TallyFilterResult(kind="energy", bins=3, edges=[0.0, 0.5, 5e5, 2e7])
SURFACE = TallyFilterResult(kind="surface", bins=1, ids=[7])
DOSE = TallyResult(
    id=2,
    name="dose",
    scores=["flux"],
    nuclides=["total"],
    filters=[SURFACE, ENERGY],
    shape=[1, 3, 1, 1],
    realizations=200,
    max_rel_err=0.12,
    totals=[Estimate(value=6.0e-7, std=4.0e-8)],
)


def sim_model_info(folder: Path | None = None) -> ModelInfo:
    info = model_info(source="hw/model.py")
    info.params = {"thermal_scattering": 1.0, "rod": 30.0}
    info.settings = SettingsInfo(run_mode="eigenvalue", particles=2000, batches=220, inactive=20)
    info.sidecar = Sidecar(
        title=LocalizedText(en="Homework"),
        beta_eff=0.0065,
        parameters=[
            ParameterSpec(
                name="thermal_scattering",
                label=LocalizedText(en="S(α,β)"),
                default=1,
                minimum=0,
                maximum=1,
                step=1,
            ),
            ParameterSpec(
                name="rod",
                label=LocalizedText(en="Rod"),
                unit="cm",
                default=30,
                minimum=-30,
                maximum=30,
                step=1,
            ),
        ],
        run_presets=[
            RunPreset(
                name="validation",
                label=LocalizedText(en="Validation"),
                particles=20000,
                batches=200,
                inactive=20,
                params={"thermal_scattering": 0},
            ),
        ],
        references=[
            ReferenceResult(
                name="k-eff other code",
                value=1.0300,
                std=0.0012,
                source="other code",
                params={"thermal_scattering": 0},
            ),
            ReferenceResult(
                name="k-eff model B",
                value=1.04000,
                std=0.0005,
                source="model B",
                params={"thermal_scattering": 1},
            ),
            ReferenceResult(
                name="dose",
                value=2.0e3,
                rel_err=0.073,
                unit="µSv/h",
                tally=2,
                params={"thermal_scattering": 1},
            ),
        ],
        tallies={
            2: TallyNormalisation(
                source_strength=1.0e15,
                divide_by="area",
                divisor=1.25e5,
                multiplier=3.6e-3,
                unit="µSv/h",
            )
        },
    )
    return info


def run_info(run_id: str, state: str = "finished", k: float | None = 1.0405, **kwargs) -> RunInfo:
    return RunInfo(
        id=run_id,
        path=f"hw/runs/{run_id}",
        model="hw/model.py",
        label=run_id,
        state=state,  # type: ignore[arg-type]
        created=datetime(2026, 10, 26, 12, int(run_id[-1]) if run_id[-1].isdigit() else 0),
        plan=RunPlan(run_mode="eigenvalue", particles=2000, batches=220, inactive=20, threads=4),
        params={"thermal_scattering": 1.0, "rod": 30.0},
        k_eff=k,
        k_std=0.0004 if k else None,
        has_results=k is not None,
        **kwargs,
    )


def results_for(run_id: str, k: float = 1.0405) -> RunResults:
    return RunResults(
        run_id=run_id,
        statepoint="statepoint.220.h5",
        run_mode="eigenvalue",
        batches=220,
        inactive=20,
        particles=2000,
        realizations=200,
        keff=KeffResults(
            combined=Estimate(value=k, std=0.0004), collision=Estimate(value=k + 0.0001, std=0.0005)
        ),
        leakage=Estimate(value=1e-5, std=1e-6),
        k_generation=[1.0] * 220,
        runtime={"active batches": 60.0, "total": 75.0},
        rate_active=40000.0,
        tallies=[DOSE],
        convergence={
            2: Convergence(
                batches=[50, 100, 220],
                seconds=[10, 25, 60],
                mean=[[1, 2, 3], [1.1, 2.1, 3.1], [1.0, 2.0, 3.0]],
                std=[[0.3, 0.3, 0.3], [0.2, 0.2, 0.2], [0.1, 0.1, 0.1]],
            )
        },
        keff_convergence=Convergence(
            batches=[50, 100, 220],
            seconds=[10, 25, 60],
            mean=[[1.04], [1.0402], [k]],
            std=[[0.001], [0.0007], [0.0004]],
            binned=False,
        ),
    )


def dose_data() -> TallyData:
    mean = np.array([1.0e-7, 2.0e-7, 3.0e-7]).reshape(DOSE.shape)
    std = np.array([0.5e-8, 1.5e-8, 3.6e-8]).reshape(DOSE.shape)
    return TallyData(tally=DOSE, mean=mean, std=std, statepoint="statepoint.220.h5")


class FakeRunApi:
    """Stands in for `ApiClient` in `RunSession`."""

    instances: ClassVar[list["FakeRunApi"]] = []

    def __init__(self, connection) -> None:
        self.connection = connection
        self.started: list[RunRequest] = []
        self.stopped: list[str] = []
        self.deleted: list[str] = []
        self.listed = [run_info("run2", k=1.0410), run_info("run1", k=1.0405)]
        FakeRunApi.instances.append(self)

    def start_run(self, request: RunRequest) -> RunInfo:
        self.started.append(request)
        info = run_info("run3", "running", k=None)
        self.listed.insert(0, info)
        return info

    def stop_run(self, run_id: str) -> RunInfo:
        self.stopped.append(run_id)
        return run_info(run_id, "running", k=None)

    def runs(self, model: str) -> list[RunInfo]:
        return list(self.listed)

    def active_run(self) -> RunStatus | None:
        return None

    def run_status(self, run_id: str) -> RunStatus:
        return RunStatus(run=next(r for r in self.listed if r.id == run_id), progress=RunProgress())

    def run_log(self, run_id: str, offset: int = 0) -> LogChunk:
        text = f"log of {run_id}\n"
        return LogChunk(offset=0, text=text, next=len(text), size=len(text))

    def run_results(self, run_id: str) -> RunResults:
        run = next(r for r in self.listed if r.id == run_id)
        return results_for(run_id, run.k_eff or 1.0)

    def tally_data(self, run_id: str, tally_id: int) -> TallyData:
        return dose_data()

    def delete_run(self, run_id: str) -> None:
        self.deleted.append(run_id)
        self.listed = [r for r in self.listed if r.id != run_id]

    def close(self) -> None:
        pass


class SimSession(FakeSession):
    """`FakeSession` plus what the Simulation tab uses: open and the workspace."""

    def __init__(self, folder: Path) -> None:
        super().__init__(folder)
        self.workspace = folder.parent
        self.opened: list[tuple[Path, dict]] = []

    def open(self, path, params=None) -> None:
        self.opened.append((path, dict(params or {})))
