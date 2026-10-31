import json

import numpy as np
import pytest

from pyvised_protocol.schemas import InspectResult, PathStep, SliceRequest
from pyvised_protocol.sidecar import (
    SIDECAR_NAME,
    Sidecar,
    TallyNormalisation,
    evaluate_formula,
    formula_names,
    update_sidecar,
)
from pyvised_protocol.slices import OUTSIDE, decode_slice, encode_slice


def test_slice_layers_round_trip():
    ids = np.zeros((4, 5, 3), dtype=np.int32)
    ids[..., 0] = 7
    props = np.full((4, 5, 2), 293.6)
    level = np.full((4, 5, 3), OUTSIDE, dtype=np.int32)
    level[:2, :, 0] = 11
    lower = np.full((4, 5), np.nan)
    lower[0, 0] = 0.25
    data = decode_slice(encode_slice(ids, props, {"a": 1}, level_ids=level, ww_lower=lower))
    assert data.shape == (4, 5) and data.meta == {"a": 1}
    assert data.temperature is not None and data.temperature.dtype == np.float32
    assert data.level_cells is not None and int(data.level_cells[0, 0]) == 11
    assert data.level_instances is not None and int(data.level_instances[3, 4]) == OUTSIDE
    assert data.ww_lower is not None and data.ww_lower[0, 0] == pytest.approx(0.25)
    assert np.isnan(data.ww_lower[1, 1])
    plain = decode_slice(encode_slice(ids))
    assert plain.temperature is None and plain.level_cells is None and plain.ww_lower is None


def test_slice_request_layers_are_optional():
    request = SliceRequest(origin=(0, 0, 0), width=(1, 1), pixels=(2, 2))
    assert request.level is None and request.weight_windows is None and not request.properties
    full = SliceRequest.model_validate(
        {
            "origin": [0, 0, 0],
            "width": [1, 1],
            "pixels": [2, 2],
            "level": 2,
            "weight_windows": {"particle": "photon", "energy": 1e6},
        }
    )
    assert full.level == 2 and full.weight_windows is not None
    assert full.weight_windows.particle == "photon"
    with pytest.raises(ValueError):
        SliceRequest(origin=(0, 0, 0), width=(1, 1), pixels=(2, 2), level=-1)


def test_inspect_result_round_trip():
    result = InspectResult(
        point=(1.0, 2.0, 3.0),
        found=True,
        path=[
            PathStep(
                level=0, universe=0, cell=11, local=(1, 2, 3), lattice=5, lattice_index=[1, 2]
            ),
            PathStep(level=1, universe=4, cell=6, local=(0.5, 0.5, 3), overlapping=[7]),
        ],
        instance=3,
        material=2,
        density=2.7,
    )
    assert InspectResult.model_validate_json(result.model_dump_json()) == result


def test_update_sidecar_keeps_unknown_keys(tmp_path):
    (tmp_path / SIDECAR_NAME).write_text(
        json.dumps({"schema_version": 1, "title": {"en": "T"}, "future_key": [1, 2]}),
        encoding="utf-8",
    )
    sidecar = update_sidecar(tmp_path, {"material_colors": {"3": "#112233"}})
    assert sidecar.material_colors == {3: "#112233"} and sidecar.title.en == "T"
    data = json.loads((tmp_path / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert data["future_key"] == [1, 2] and data["material_colors"] == {"3": "#112233"}
    update_sidecar(tmp_path, {"material_colors": None})
    assert "material_colors" not in json.loads((tmp_path / SIDECAR_NAME).read_text("utf-8"))


def test_update_sidecar_creates_the_file_and_rejects_bad_values(tmp_path):
    sidecar = update_sidecar(tmp_path, {"universe_colors": {"5": "#abcdef"}})
    assert sidecar.universe_colors == {5: "#abcdef"} and sidecar.importance_lower_bound == 0.5
    with pytest.raises(ValueError):
        update_sidecar(tmp_path, {"importance_lower_bound": -1})
    assert Sidecar.model_validate_json((tmp_path / SIDECAR_NAME).read_bytes()).universe_colors


def test_divisor_formulas_of_the_builder_parameters():
    assert formula_names("4*pi*radius**2") == {"radius"}
    assert formula_names("2 * pi * (r + t) * (-h1 + h2)") == {"r", "t", "h1", "h2"}
    assert evaluate_formula("4*pi*radius**2", {"radius": 3}) == pytest.approx(113.0973355)
    assert evaluate_formula("-(1 - 2) / 4", {}) == 0.25
    for bad in ("__import__('os')", "radius.real", "[1]", "'a'", "True", "r if r else 1", "1 +"):
        with pytest.raises(ValueError):
            formula_names(bad)
    for bad, params in (("r", {}), ("1/0", {}), ("(-1)**0.5", {}), ("10.0**400", {})):
        with pytest.raises(ValueError):
            evaluate_formula(bad, params)
    norm = TallyNormalisation(divide_by="area", divisor="4*pi*radius**2")
    assert norm.divisor_value({"radius": 1}) == pytest.approx(4 * np.pi)
    assert TallyNormalisation(divisor=12.5).divisor_value({}) == 12.5
    with pytest.raises(ValueError):
        TallyNormalisation(divisor="radius +")


def test_divisor_formulas_must_use_builder_parameters(tmp_path):
    parameters = [{"name": "radius", "default": 3}]
    tallies = {"2": {"divide_by": "area", "divisor": "4*pi*radius**2"}}
    sidecar = Sidecar.model_validate({"parameters": parameters, "tallies": tallies})
    assert sidecar.tallies[2].divisor == "4*pi*radius**2"
    fixed = Sidecar.model_validate({"tallies": {"2": {"divisor": "150.5"}}})
    assert fixed.tallies[2].divisor_value({}) == pytest.approx(150.5)
    with pytest.raises(ValueError, match="radius, which is not a builder parameter"):
        Sidecar.model_validate({"tallies": tallies})
    with pytest.raises(ValueError):
        update_sidecar(tmp_path, {"tallies": tallies})
