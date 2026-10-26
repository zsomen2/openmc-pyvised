"""Statepoint summaries: ``python -m pyvised_backend.results snapshot|summary RUN_DIR …``.

Runs in a subprocess: the OpenMC Python API takes a second to import and h5py
may crash on a damaged file, so both stay out of the API process.

- ``snapshot RUN_DIR STATEPOINT…`` appends one line per statepoint to
  `SNAPSHOTS_FILE`: k-eff and every tally's mean and σ (per bin, or the totals
  of a big tally), read with h5py alone, so it is quick. These lines feed the
  convergence plots; the run manager deletes older statepoints afterwards.
- ``summary RUN_DIR RUN_ID`` reads the newest readable statepoint with
  ``openmc.StatePoint`` and writes `RESULTS_FILE` (`RunResults`) and the tally
  arrays (`TALLIES_FILE`: ``mean_<id>`` and ``std_<id>`` in the tally's shape);
  it prints the statepoint and k-eff after `RESULT_MARKER`.
"""

import json
import math
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from pyvised_protocol.runs import (
    RESULTS_FILE,
    TALLIES_FILE,
    Convergence,
    Estimate,
    KeffResults,
    MeshInfo,
    RunResults,
    TallyFilterResult,
    TallyResult,
)

RESULT_MARKER = "PYVISED_RESULT "
SNAPSHOTS_FILE = "pyvised_snapshots.jsonl"
MAX_BINNED = 2000
"""Tallies with more values than this keep only their totals per statepoint."""
_BATCH = re.compile(r"statepoint\.0*(\d+)\.h5$")
ID_FILTERS = {"cell", "surface", "material", "universe", "cellborn", "cellfrom"}
EDGE_FILTERS = {"energy", "energyout", "time", "mu", "polar", "azimuthal"}


def finite(value: Any) -> float | None:
    """`value` as a float, None for NaN and infinities (JSON has neither)."""
    number = float(value)
    return number if math.isfinite(number) else None


def finite_list(values: Any) -> list[float | None]:
    return [finite(v) for v in values]


def statepoint_batch(path: Path) -> int:
    match = _BATCH.search(path.name)
    return int(match.group(1)) if match else -1


def statepoints(run_dir: Path) -> list[Path]:
    """The run's statepoints, oldest first."""
    found = [p for p in run_dir.glob("statepoint.*.h5") if statepoint_batch(p) >= 0]
    return sorted(found, key=statepoint_batch)


# -- snapshots (h5py) ---------------------------------------------------------------------


def _moments(results: Any, n: int) -> tuple[Any, Any]:
    import numpy as np

    total, squares = results[..., 0], results[..., 1]
    if n < 1:
        nan = np.full(total.shape, np.nan)
        return nan, nan
    mean = total / n
    if n < 2:
        return mean, np.full(total.shape, np.nan)
    variance = np.maximum(squares / n - mean**2, 0.0) / (n - 1)
    return mean, np.sqrt(variance)


def read_snapshot(path: Path) -> dict[str, Any]:
    import h5py  # pyright: ignore[reportMissingImports]  (container only)
    import numpy as np

    with h5py.File(path, "r") as f:
        line: dict[str, Any] = {
            "batch": int(f["current_batch"][()]),
            "realizations": int(f["n_realizations"][()]) if "n_realizations" in f else 0,
            "active_seconds": float(f["runtime"]["active batches"][()])
            if "runtime" in f and "active batches" in f["runtime"]
            else None,
            "keff": None,
            "tallies": {},
        }
        if "k_combined" in f:
            value, std = (float(x) for x in f["k_combined"][()])
            if math.isfinite(value) and math.isfinite(std):
                line["keff"] = [value, std]
        group = f.get("tallies", {})
        for key in group:
            if not key.startswith("tally "):
                continue
            tally = group[key]
            if int(tally.attrs.get("internal", 0)):
                continue
            n = int(tally["n_realizations"][()])
            mean, std = _moments(tally["results"][()], n)
            entry: dict[str, Any] = {"realizations": n, "binned": mean.size <= MAX_BINNED}
            if entry["binned"]:
                entry["mean"], entry["std"] = finite_list(mean.ravel()), finite_list(std.ravel())
            else:  # sums over the filter bins, per nuclide and score
                entry["mean"] = finite_list(mean.sum(axis=0).ravel())
                entry["std"] = finite_list(np.sqrt((std**2).sum(axis=0)).ravel())
            line["tallies"][key.split()[1]] = entry
    return line


def snapshot(run_dir: Path, paths: list[Path]) -> None:
    with (run_dir / SNAPSHOTS_FILE).open("a", encoding="utf-8") as out:
        for path in paths:
            try:
                line = read_snapshot(path)
            except Exception as exc:  # a damaged statepoint: skip it, keep the others
                print(f"{path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            out.write(json.dumps(line, allow_nan=False) + "\n")


def read_snapshots(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / SNAPSHOTS_FILE
    lines: dict[int, dict[str, Any]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        lines[int(line["batch"])] = line  # a later line for the same batch wins
    return [lines[b] for b in sorted(lines)]


def convergence(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[int, Convergence], Convergence | None]:
    tallies: dict[int, Convergence] = {}
    keff = Convergence(binned=False)
    for line in snapshots:
        seconds = line.get("active_seconds") or 0.0
        if line.get("keff") is not None:
            keff.batches.append(line["batch"])
            keff.seconds.append(seconds)
            keff.mean.append([line["keff"][0]])
            keff.std.append([line["keff"][1]])
        for key, entry in line.get("tallies", {}).items():
            if not entry.get("realizations"):
                continue
            item = tallies.setdefault(int(key), Convergence(binned=bool(entry["binned"])))
            if item.binned != bool(entry["binned"]):
                continue
            item.batches.append(line["batch"])
            item.seconds.append(seconds)
            item.mean.append(entry["mean"])
            item.std.append(entry["std"])
    return tallies, keff if keff.batches else None


# -- summary (openmc.StatePoint) ------------------------------------------------------------


def _estimate(value: Any, std: Any) -> Estimate | None:
    number = finite(value)
    return None if number is None else Estimate(value=number, std=finite(std))


def _mesh(mesh: Any) -> MeshInfo:
    kind = type(mesh).__name__.removesuffix("Mesh").lower()
    info = MeshInfo(id=int(mesh.id), kind=kind)
    dimension = getattr(mesh, "dimension", None)
    if dimension is not None:
        info.dimension = [int(d) for d in dimension]
    for attribute in ("lower_left", "upper_right", "origin"):
        value = getattr(mesh, attribute, None)
        if value is not None:
            setattr(info, attribute, [float(v) for v in value])
    grids = {
        "rectilinear": ("x_grid", "y_grid", "z_grid"),
        "cylindrical": ("r_grid", "phi_grid", "z_grid"),
        "spherical": ("r_grid", "theta_grid", "phi_grid"),
    }.get(kind)
    if grids:
        info.grids = [[float(v) for v in getattr(mesh, g)] for g in grids]
    elif kind == "regular" and info.lower_left and info.upper_right and info.dimension:
        info.grids = [
            [lo + (hi - lo) * i / n for i in range(n + 1)]
            for lo, hi, n in zip(info.lower_left, info.upper_right, info.dimension, strict=False)
        ]
    return info


def _filter(f: Any) -> TallyFilterResult:
    kind = str(getattr(f, "short_name", type(f).__name__.removesuffix("Filter"))).lower()
    result = TallyFilterResult(kind=kind, bins=int(f.num_bins))
    if kind in ID_FILTERS:
        result.ids = [int(b) for b in f.bins]
    elif kind in EDGE_FILTERS:
        result.edges = [float(v) for v in f.values]
        result.summary = (
            f"{len(result.edges) - 1} bins, {result.edges[0]:g}–{result.edges[-1]:g}"
            if result.edges
            else ""
        )
    elif kind in ("mesh", "meshsurface", "meshborn"):
        result.mesh = _mesh(f.mesh)
        dims = "×".join(str(d) for d in result.mesh.dimension)
        result.summary = f"mesh {result.mesh.id} ({result.mesh.kind} {dims})"
    elif kind == "energyfunction":
        result.labels = ["f(E)"]
        result.summary = f"{len(f.energy)} points, {f.interpolation}"
    elif kind == "distribcell":
        result.ids = [int(f.bins[0])] if len(f.bins) else []
    elif kind == "cellinstance":  # bins are (cell, instance) pairs
        result.labels = [f"{int(cell)}#{int(instance)}" for cell, instance in f.bins]
    else:
        result.labels = [str(b) for b in f.bins]
    return result


def _tally(tally: Any) -> tuple[TallyResult, Any, Any]:
    import numpy as np

    filters = [_filter(f) for f in tally.filters]
    shape = [f.bins for f in filters] + [len(tally.nuclides), len(tally.scores)]
    realizations = int(tally.num_realizations)
    if realizations < 1:  # stopped before the first active batch
        mean = np.zeros(shape)
        std = np.full(shape, np.nan)
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            mean = np.asarray(tally.mean, dtype=np.float64).reshape(shape)
            std = np.asarray(tally.std_dev, dtype=np.float64).reshape(shape)
    nonzero = mean != 0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.where(nonzero, std / np.abs(mean), np.nan)
    flat_mean = mean.reshape(-1, len(tally.nuclides) * len(tally.scores))
    flat_std = std.reshape(flat_mean.shape)
    totals = [
        Estimate(value=float(m), std=finite(s))
        for m, s in zip(flat_mean.sum(axis=0), np.sqrt((flat_std**2).sum(axis=0)), strict=True)
    ]
    largest = float(np.nanmax(rel)) if np.isfinite(rel).any() else None
    result = TallyResult(
        id=int(tally.id),
        name=tally.name or "",
        scores=[str(s) for s in tally.scores],
        nuclides=[str(n) for n in tally.nuclides],
        filters=filters,
        shape=shape,
        realizations=realizations,
        estimator=tally.estimator,
        max_rel_err=largest,
        zero_bins=int((~nonzero).sum()),
        totals=totals,
    )
    return result, mean, std


def newest_readable(run_dir: Path) -> tuple[Path, list[str]]:
    """The newest statepoint h5py can read (a killed run may have left a damaged one)."""
    import h5py  # pyright: ignore[reportMissingImports]  (container only)

    warnings = []
    for path in reversed(statepoints(run_dir)):
        try:
            with h5py.File(path, "r") as f:
                int(f["current_batch"][()])
        except Exception as exc:
            warnings.append(f"{path.name} could not be read ({type(exc).__name__}); skipped")
            continue
        return path, warnings
    raise FileNotFoundError("the run wrote no statepoint")


def summarise(run_dir: Path, run_id: str) -> RunResults:
    import numpy as np
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    path, warnings = newest_readable(run_dir)
    arrays: dict[str, Any] = {}
    with openmc.StatePoint(path, autolink=False) as sp:
        run_mode = str(sp.run_mode)
        eigenvalue = run_mode == "eigenvalue"
        batches = int(sp.current_batch)
        inactive = int(sp.n_inactive or 0) if eigenvalue else 0
        particles = int(sp.n_particles)
        generations = int(sp.generations_per_batch or 1) if eigenvalue else 1
        globals_ = {
            (row["name"].decode() if isinstance(row["name"], bytes) else str(row["name"])): row
            for row in sp.global_tallies
        }
        keff = None
        if eigenvalue:
            keff = KeffResults()
            combined = sp._f["k_combined"][()] if "k_combined" in sp._f else None
            if combined is not None:
                keff.combined = _estimate(combined[0], combined[1])
            for name, attribute in (
                ("k-collision", "collision"),
                ("k-tracklength", "track_length"),
                ("k-absorption", "absorption"),
            ):
                row = globals_.get(name)
                if row is not None:
                    setattr(keff, attribute, _estimate(row["mean"], row["std_dev"]))
        leakage_row = globals_.get("leakage")
        leakage = _estimate(leakage_row["mean"], leakage_row["std_dev"]) if leakage_row else None
        runtime = {str(k): float(v) for k, v in sp.runtime.items()}
        tallies = []
        for tid in sorted(sp.tallies):
            result, mean, std = _tally(sp.tallies[tid])
            tallies.append(result)
            arrays[f"mean_{tid}"], arrays[f"std_{tid}"] = mean, std
        k_generation = [float(k) for k in sp.k_generation] if sp.k_generation is not None else []
        entropy = [float(e) for e in sp._f["entropy"][()]] if "entropy" in sp._f else []
        realizations = int(sp.n_realizations)

    def rate(histories: int, seconds: float | None) -> float | None:
        return histories / seconds if seconds and histories > 0 else None

    per_batch = particles * generations
    tally_convergence, keff_convergence = convergence(read_snapshots(run_dir))
    results = RunResults(
        run_id=run_id,
        statepoint=path.name,
        run_mode=run_mode,
        batches=batches,
        inactive=inactive,
        particles=particles,
        generations_per_batch=generations,
        realizations=realizations,
        keff=keff,
        leakage=leakage,
        k_generation=k_generation,
        entropy=entropy,
        runtime=runtime,
        rate_active=rate(per_batch * (batches - inactive), runtime.get("active batches")),
        rate_inactive=rate(per_batch * inactive, runtime.get("inactive batches")),
        tallies=tallies,
        convergence=tally_convergence,
        keff_convergence=keff_convergence,
        warnings=warnings,
    )
    # replaced in one step each, so a reader never sees half a file
    tallies_tmp = run_dir / f"{TALLIES_FILE}.tmp"
    with tallies_tmp.open("wb") as out:
        np.savez(out, **arrays)
    tallies_tmp.replace(run_dir / TALLIES_FILE)
    results_tmp = run_dir / f"{RESULTS_FILE}.tmp"
    results_tmp.write_text(results.model_dump_json(indent=1), encoding="utf-8")
    results_tmp.replace(run_dir / RESULTS_FILE)
    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("snapshot", "summary"):
        print(
            "usage: python -m pyvised_backend.results snapshot RUN_DIR STATEPOINT…\n"
            "       python -m pyvised_backend.results summary RUN_DIR RUN_ID",
            file=sys.stderr,
        )
        return 2
    run_dir = Path(argv[1])
    try:
        if argv[0] == "snapshot":
            snapshot(run_dir, [Path(p) for p in argv[2:]])
            return 0
        results = summarise(run_dir, argv[2] if len(argv) > 2 else "")
    except FileNotFoundError as exc:
        print(f"no_statepoint: {exc}", file=sys.stderr)
        return 3
    except Exception:
        traceback.print_exc()
        return 1
    combined = results.keff.combined if results.keff else None
    line = {"statepoint": results.statepoint, "batches": results.batches}
    if combined is not None:
        line["k_eff"], line["k_std"] = combined.value, combined.std
    print(RESULT_MARKER + json.dumps(line), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
