from datetime import datetime

import pytest

from pyvised_protocol.runs import (
    Event,
    RunInfo,
    RunResults,
    format_sse,
    parse_sse,
    run_id_for,
    run_path_for,
)
from pyvised_protocol.sidecar import ReferenceResult, Sidecar


def test_run_ids_round_trip():
    path = "lattice/runs/20261026-120000_rúd 30"
    run_id = run_id_for(path)
    assert "/" not in run_id and "=" not in run_id
    assert run_path_for(run_id) == path
    with pytest.raises(ValueError):
        run_path_for("%%%")


def test_server_sent_events_round_trip():
    events = [
        Event(type="run.progress", run_id="a", data={"first": 3, "k": [1.0]}),
        Event(type="run.log", run_id="a", data={"text": "line\nnext\n", "offset": 10}),
    ]
    text = (b": connected\n\n" + b"".join(format_sse(e) for e in events)).decode()
    assert list(parse_sse(text.splitlines(keepends=True))) == events


def test_run_info_active_and_results_defaults():
    info = RunInfo(
        id="x", path="m/runs/x", model="m/a.py", state="summarising", created=datetime(2026, 1, 1)
    )
    assert info.active
    assert not info.model_copy(update={"state": "stopped"}).active
    results = RunResults(
        run_id="x",
        statepoint="statepoint.5.h5",
        run_mode="fixed source",
        batches=5,
        runtime={"active batches": 2.0},
    )
    assert results.active_seconds == 2.0 and results.keff is None


def test_references_match_parameters_and_presets_parse():
    ref = ReferenceResult(name="k", value=1.0, params={"variant": 1, "rod": 25})
    assert ref.matches({"variant": 1.0, "rod": 25.0, "other": 7})
    assert not ref.matches({"variant": 1.0, "rod": 26.0})
    assert not ref.matches({"variant": 1.0})
    assert ReferenceResult(name="any", value=1.0).matches({})
    sidecar = Sidecar.model_validate(
        {"run_presets": [{"name": "quick", "particles": 5000, "batches": 60, "inactive": 20}]}
    )
    (preset,) = sidecar.run_presets
    assert (preset.particles, preset.batches, preset.time_limit) == (5000, 60, None)
