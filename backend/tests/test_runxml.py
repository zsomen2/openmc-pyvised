"""Run settings patched into a model.xml (no OpenMC needed)."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyvised_backend.errors import ServiceError
from pyvised_backend.runxml import (
    entropy_dimension,
    first_check,
    patch_model,
    statepoint_batches,
)
from pyvised_protocol.runs import RunSettings
from pyvised_protocol.schemas import BoundingBox

MODEL = """<?xml version='1.0' encoding='utf-8'?>
<model>
  <materials/>
  <geometry/>
  <settings>
    <run_mode>{mode}</run_mode>
    <particles>2000</particles>
    <batches>220</batches>
    <inactive>20</inactive>
    <mesh id="1"><dimension>1 1 1</dimension></mesh>
    <state_point><batches>220</batches></state_point>
  </settings>
  <tallies>
    <mesh id="14" type="cylindrical"/>
    <tally id="2"><filters>1</filters><scores>flux</scores></tally>
  </tallies>
</model>
"""
BOX = BoundingBox(lower_left=(-1, -2, -3), upper_right=(1, 2, 3))


def model(tmp_path: Path, mode: str = "eigenvalue") -> Path:
    path = tmp_path / "model.xml"
    path.write_text(MODEL.format(mode=mode), encoding="utf-8")
    return path


def settings_of(path: Path) -> ET.Element:
    element = ET.parse(path).getroot().find("settings")
    assert element is not None
    return element


def test_statepoint_schedule():
    assert statepoint_batches(220) == [*range(11, 220, 11), 220]
    assert statepoint_batches(10) == list(range(1, 11))
    assert statepoint_batches(100, every=30) == [30, 60, 90, 100]
    assert statepoint_batches(100, every=30, extra=(45,)) == [30, 45, 60, 90, 100]
    assert first_check(220, 20) == 40
    assert first_check(30, 20) == 30
    assert entropy_dimension(20000) == 10


def test_model_values_are_kept_without_overrides(tmp_path: Path):
    path = model(tmp_path)
    plan, warnings = patch_model(path, RunSettings(entropy_mesh=False), 8, BOX)
    assert (plan.run_mode, plan.particles, plan.batches, plan.inactive) == (
        "eigenvalue",
        2000,
        220,
        20,
    )
    assert plan.threads == 8 and plan.first_check is None and not plan.entropy
    assert warnings == []
    s = settings_of(path)
    assert s.findtext("state_point/batches", "").split()[-1] == "220"
    assert len(s.findall("state_point")) == 1


def test_overrides_and_entropy_mesh(tmp_path: Path):
    path = model(tmp_path)
    request = RunSettings(particles=20000, batches=100, inactive=10, seed=7)
    plan, _ = patch_model(path, request, 4, BOX)
    s = settings_of(path)
    assert s.findtext("particles") == "20000" and s.findtext("batches") == "100"
    assert s.findtext("inactive") == "10" and s.findtext("seed") == "7"
    assert plan.seed == 7 and plan.statepoints == [5 * i for i in range(1, 21)]
    assert plan.entropy and plan.entropy_added
    mesh_id = s.findtext("entropy_mesh")
    assert mesh_id == "15", "a new id after every mesh of settings and tallies"
    mesh = s.find(f"mesh[@id='{mesh_id}']")
    assert mesh is not None and mesh.findtext("dimension") == "10 10 10"
    assert mesh.findtext("lower_left") == "-1 -2 -3"


def test_a_time_limit_writes_statepoints_more_often(tmp_path: Path):
    plan, _ = patch_model(model(tmp_path), RunSettings(batches=2000, time_limit=60), 4, BOX)
    assert plan.statepoints[:3] == [20, 40, 60] and plan.statepoints[-1] == 2000
    plan, _ = patch_model(model(tmp_path), RunSettings(batches=2000), 4, BOX)
    assert plan.statepoints[:2] == [100, 200]


def test_existing_entropy_mesh_is_kept(tmp_path: Path):
    path = model(tmp_path)
    text = path.read_text().replace("<particles>", "<entropy_mesh>1</entropy_mesh><particles>")
    path.write_text(text)
    plan, _ = patch_model(path, RunSettings(), 4, BOX)
    assert plan.entropy and not plan.entropy_added
    assert settings_of(path).findtext("entropy_mesh") == "1"


def test_keff_and_tally_triggers(tmp_path: Path):
    path = model(tmp_path)
    plan, _ = patch_model(path, RunSettings(keff_std=0.0005, tally_rel_err=0.05), 4, BOX)
    s = settings_of(path)
    assert plan.batches == 220 and plan.first_check == 40
    assert s.findtext("batches") == "40"
    assert s.findtext("trigger/active") == "true" and s.findtext("trigger/max_batches") == "220"
    assert s.findtext("keff_trigger/type") == "std_dev"
    assert float(s.findtext("keff_trigger/threshold", "")) == 0.0005
    assert 40 in plan.statepoints and plan.statepoints[-1] == 220
    tally = ET.parse(path).getroot().find("tallies/tally")
    assert tally is not None
    trigger = tally.find("trigger")
    assert trigger is not None and trigger.get("ignore_zeros") == "true"
    assert trigger.get("type") == "rel_err" and float(trigger.get("threshold", "")) == 0.05
    assert len(plan.triggers) == 2


def test_fixed_source_ignores_inactive_and_the_keff_target(tmp_path: Path):
    path = model(tmp_path, "fixed source")
    plan, warnings = patch_model(path, RunSettings(inactive=5, keff_std=0.001), 2, BOX)
    assert plan.inactive == 0 and plan.first_check is None and not plan.entropy
    assert warnings and "fixed-source" in warnings[0]
    assert settings_of(path).find("keff_trigger") is None


def test_bad_settings(tmp_path: Path):
    path = model(tmp_path)
    with pytest.raises(ServiceError) as info:
        patch_model(path, RunSettings(batches=20, inactive=20), 1, BOX)
    assert info.value.error == "bad_settings"
    text = path.read_text().replace("<particles>2000</particles>", "")
    path.write_text(text)
    with pytest.raises(ServiceError) as info:
        patch_model(path, RunSettings(), 1, BOX)
    assert info.value.error == "incomplete_settings"


def test_an_ignored_target_does_not_switch_triggers_on(tmp_path: Path):
    path = model(tmp_path)
    text = path.read_text().replace(
        '<tally id="2"><filters>1</filters><scores>flux</scores></tally>', ""
    )
    path.write_text(text)
    plan, warnings = patch_model(path, RunSettings(batches=200, tally_rel_err=0.05), 4, BOX)
    assert warnings and "no tallies" in warnings[0]
    assert plan.first_check is None and plan.triggers == [] and plan.batches == 200
    s = settings_of(path)
    assert s.findtext("batches") == "200" and s.find("trigger") is None
