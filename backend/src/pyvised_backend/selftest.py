"""Self-test: build, render and run a tiny pin-cell model.

Run as ``python -m pyvised_backend selftest``. The server starts it in a
subprocess, so an OpenMC abort (``MPI_Abort`` on bad input) can never take the
service down; the result is printed as one JSON line after `RESULT_MARKER`.
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pyvised_protocol.schemas import SelfTestResult, SelfTestStep

RESULT_MARKER = "PYVISED_RESULT "
BATCHES = 6


def build_pincell(directory: Path) -> dict[str, int]:
    """Export an infinite lattice of UO2 rods in water (the unit cell of course task 5)."""
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    fuel = openmc.Material(name="UO2, 10 % U-235")
    fuel.add_nuclide("U235", 0.1)
    fuel.add_nuclide("U238", 0.9)
    fuel.add_element("O", 2.0)
    fuel.set_density("g/cm3", 10.0)
    water = openmc.Material(name="water")
    water.add_element("H", 2.0)
    water.add_element("O", 1.0)
    water.set_density("g/cm3", 1.0)
    water.add_s_alpha_beta("c_H_in_H2O")

    rod = openmc.ZCylinder(r=0.5)
    sides = openmc.model.RectangularPrism(2.0, 2.0, boundary_type="reflective")
    bottom = openmc.ZPlane(-1.0, boundary_type="reflective")
    top = openmc.ZPlane(1.0, boundary_type="reflective")
    fuel_cell = openmc.Cell(name="fuel", fill=fuel, region=-rod & +bottom & -top)
    water_cell = openmc.Cell(name="water", fill=water, region=+rod & -sides & +bottom & -top)

    model = openmc.Model()
    model.materials = openmc.Materials([fuel, water])
    model.geometry = openmc.Geometry([fuel_cell, water_cell])
    model.settings.run_mode = "eigenvalue"
    model.settings.particles = 1000
    model.settings.batches = BATCHES
    model.settings.inactive = 2
    model.settings.source = openmc.IndependentSource(space=openmc.stats.Point())
    model.export_to_xml(directory)
    # plot mode insists on a plots.xml when the model comes as separate XML files
    openmc.Plots().export_to_xml(directory)
    return {"fuel": fuel_cell.id, "water": water_cell.id}


def check_render(directory: Path, cells: dict[str, int]) -> str:
    """Load the model in plot mode and check a slice through the rod."""
    import numpy as np
    import openmc.lib  # pyright: ignore[reportMissingImports]  (container only)

    openmc.lib.init(["-p", str(directory)], output=False)
    try:
        ids, _ = openmc.lib.slice_data(
            origin=(0.0, 0.0, 0.0),
            width=(2.0, 2.0),
            basis="xy",
            pixels=(64, 64),
            include_properties=False,
        )
    finally:
        openmc.lib.finalize()
    centre, corner = int(ids[32, 32, 0]), int(ids[0, 0, 0])
    if centre != cells["fuel"] or corner != cells["water"]:
        raise RuntimeError(f"unexpected cells: centre {centre}, corner {corner}, expected {cells}")
    found = sorted(int(c) for c in np.unique(ids[..., 0]))
    return f"{ids.shape[1]}×{ids.shape[0]} px, cells {found}"


def check_transport(directory: Path) -> tuple[float, float]:
    """Run OpenMC for a few batches and read k-eff from the statepoint."""
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    threads = str(min(4, os.cpu_count() or 1))
    proc = subprocess.run(
        ["openmc", "-s", threads],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openmc exited with {proc.returncode}:\n{proc.stdout[-1500:]}")
    with openmc.StatePoint(directory / f"statepoint.{BATCHES}.h5") as sp:
        keff = sp.keff
    return float(keff.nominal_value), float(keff.std_dev)


def _step[T](steps: list[SelfTestStep], name: str, func: Callable[[], T]) -> T | None:
    start = time.perf_counter()
    try:
        value = func()
    except Exception as exc:  # reported to the client, not raised
        steps.append(
            SelfTestStep(
                name=name,
                ok=False,
                seconds=time.perf_counter() - start,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )
        return None
    detail = value if isinstance(value, str) else ""
    steps.append(
        SelfTestStep(name=name, ok=True, seconds=time.perf_counter() - start, detail=detail)
    )
    return value


@contextmanager
def _workdir(keep: Path | None) -> Iterator[Path]:
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        yield keep
        return
    with tempfile.TemporaryDirectory(prefix="pyvised-selftest-") as tmp:
        yield Path(tmp)


def run_selftest(keep: Path | None = None) -> SelfTestResult:
    steps: list[SelfTestStep] = []
    k_eff = k_std = None
    with _workdir(keep) as directory:
        cells = _step(steps, "build", lambda: build_pincell(directory))
        if cells is not None and _step(steps, "render", lambda: check_render(directory, cells)):
            keff = _step(steps, "transport", lambda: check_transport(directory))
            if keff is not None:
                k_eff, k_std = keff
                steps[-1].detail = f"k-eff = {k_eff:.4f} ± {k_std:.4f}"
    return SelfTestResult(
        ok=all(s.ok for s in steps) and len(steps) == 3, steps=steps, k_eff=k_eff, k_eff_std=k_std
    )
