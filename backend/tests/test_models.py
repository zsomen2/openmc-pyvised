"""Model loading without OpenMC: paths, cache keys, XML collection, the service with fakes."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyvised_backend import models
from pyvised_backend.config import Config
from pyvised_backend.errors import ServiceError
from pyvised_protocol.schemas import OpenModelRequest

SUMMARY = {
    "bounding_box": {"lower_left": [-1, -1, -1], "upper_right": [1, 1, 1]},
    "root_universe": 0,
    "cells": [{"id": 1, "universe": 0, "fill_type": "void"}],
    "warnings": ["from the model"],
}


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        token="t",
        workspace=tmp_path / "ws",
        cross_sections=None,
        custom_data=tmp_path / "custom",
        cache_dir=tmp_path / "cache",
    )


def test_resolve_source_stays_inside_the_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    model = write(ws / "a" / "model.py")
    write(tmp_path / "secret.py")
    assert models.resolve_source(ws, "a/model.py") == model.resolve()
    assert models.resolve_source(ws, "a\\model.py") == model.resolve()
    for bad, code in (
        ("../secret.py", 403),
        ("/etc/passwd", 403),
        ("a/missing.py", 404),
        ("", 400),
    ):
        with pytest.raises(ServiceError) as raised:
            models.resolve_source(ws, bad)
        assert raised.value.status == code, bad


def test_model_kind():
    assert models.model_kind(Path("m.py")) == "python"
    assert models.model_kind(Path("m.IPYNB")) == "notebook"
    assert models.model_kind(Path("geometry.xml")) == "xml"
    with pytest.raises(ServiceError):
        models.model_kind(Path("deck.mcnp"))


def test_sidecar_is_optional_and_errors_become_warnings(tmp_path: Path):
    assert models.read_sidecar(tmp_path) == (None, None)
    write(tmp_path / "pyvised.json", json.dumps({"title": {"en": "T"}, "tallies": {"2": {}}}))
    sidecar, warning = models.read_sidecar(tmp_path)
    assert warning is None and sidecar is not None
    assert sidecar.title.get("hu") == "T" and 2 in sidecar.tallies
    write(tmp_path / "pyvised.json", "{not json")
    sidecar, warning = models.read_sidecar(tmp_path)
    assert sidecar is None and warning and "pyvised.json" in warning


def test_cache_key_follows_sources_params_and_library(tmp_path: Path):
    script = write(tmp_path / "m.py", "print(1)")
    helper = write(tmp_path / "helper.py", "X = 1")
    write(tmp_path / "pyvised.json", "{}")
    files = models.source_files(script, "python")
    assert files == [script, helper], "the sidecar and the script itself appear once"
    key = models.cache_key(files, "python", {"a": 1.0}, "/lib.xml")
    assert key == models.cache_key(files, "python", {"a": 1.0}, "/lib.xml")
    assert key != models.cache_key(files, "python", {"a": 2.0}, "/lib.xml")
    assert key != models.cache_key(files, "python", {"a": 1.0}, "/other.xml")
    write(tmp_path / "pyvised.json", '{"title": {"en": "changed"}}')
    assert key == models.cache_key(
        models.source_files(script, "python"), "python", {"a": 1.0}, "/lib.xml"
    )
    helper.write_text("X = 2", encoding="utf-8")
    assert key != models.cache_key(files, "python", {"a": 1.0}, "/lib.xml")


def test_separate_xml_files_are_merged_into_model_xml(tmp_path: Path):
    write(tmp_path / "geometry.xml", "<geometry><cell id='1'/></geometry>")
    write(tmp_path / "materials.xml", "<materials><material id='1'/></materials>")
    write(tmp_path / "tallies.xml", "<tallies/>")
    target = tmp_path / "out.xml"
    models.merge_separate_xml(tmp_path, target)
    root = ET.parse(target).getroot()
    assert root.tag == "model"
    assert [child.tag for child in root] == ["materials", "geometry", "tallies", "settings"]
    assert models.source_files(tmp_path / "geometry.xml", "xml") == [
        tmp_path / "materials.xml",
        tmp_path / "geometry.xml",
        tmp_path / "tallies.xml",
    ]


def test_merge_needs_geometry_and_materials(tmp_path: Path):
    write(tmp_path / "geometry.xml", "<geometry/>")
    with pytest.raises(ServiceError, match=r"materials.xml"):
        models.merge_separate_xml(tmp_path, tmp_path / "model.xml")


def test_collect_export_finds_model_xml_in_a_subfolder(tmp_path: Path):
    write(tmp_path / "out" / "model.xml", "<model/>")
    models.collect_export(tmp_path)
    assert (tmp_path / "model.xml").read_text(encoding="utf-8") == "<model/>"
    with pytest.raises(ServiceError) as raised:
        models.collect_export(tmp_path / "out" / "nothing-here")
    assert raised.value.error == "no_export"


class FakeBuilder:
    def __init__(self, fail: str | None = None):
        self.calls: list[tuple[Path, dict]] = []
        self.fail = fail

    def __call__(self, source, folder, params, library, timeout):
        self.calls.append((source, params))
        if self.fail:
            raise ServiceError("build_failed", self.fail)
        (folder / "model.xml").write_text("<model/>", encoding="utf-8")
        return "builder says hi"


def test_open_runs_the_builder_once_and_caches(config: Config):
    write(config.workspace / "hw" / "model.py")
    write(
        config.workspace / "hw" / "pyvised.json",
        json.dumps({"parameters": [{"name": "rods", "default": 3}, {"name": "x", "default": 1}]}),
    )
    builder = FakeBuilder()
    service = models.ModelService(config, builder=builder, describer=lambda path: SUMMARY)

    first = service.open(OpenModelRequest(path="hw/model.py", params={"x": 2}))
    info = first.info
    assert info.source == "hw/model.py" and info.kind == "python"
    assert info.params == {"rods": 3.0, "x": 2.0}
    assert info.builder_output == "builder says hi"
    assert info.warnings == ["from the model"]
    assert info.sidecar is not None and info.cells[0].fill_type == "void"
    assert (first.directory / "model.xml").is_file()
    assert service.get(info.model_id) is first

    again = service.open(OpenModelRequest(path="hw/model.py", params={"x": 2}))
    assert again.model_id == info.model_id
    assert len(builder.calls) == 1, "the second open comes from the cache"
    other = service.open(OpenModelRequest(path="hw/model.py", params={"x": 5}))
    assert other.model_id != info.model_id and len(builder.calls) == 2


def test_failed_build_leaves_no_cache_folder(config: Config):
    write(config.workspace / "bad.py")
    service = models.ModelService(
        config, builder=FakeBuilder(fail="Traceback: boom"), describer=lambda path: SUMMARY
    )
    with pytest.raises(ServiceError, match="boom"):
        service.open(OpenModelRequest(path="bad.py"))
    assert not any(service.root.iterdir())
    with pytest.raises(ServiceError) as raised:
        service.get("nope")
    assert raised.value.status == 404


def test_xml_models_are_copied_without_a_builder(config: Config):
    write(config.workspace / "x" / "model.xml", "<model><geometry/></model>")
    builder = FakeBuilder()
    service = models.ModelService(config, builder=builder, describer=lambda path: SUMMARY)
    opened = service.open(OpenModelRequest(path="x/model.xml"))
    assert opened.info.kind == "xml" and builder.calls == []
    assert "<geometry" in (opened.directory / "model.xml").read_text(encoding="utf-8")


def test_unknown_library_is_rejected(config: Config):
    write(config.workspace / "m.py")
    service = models.ModelService(config, builder=FakeBuilder(), describer=lambda path: SUMMARY)
    with pytest.raises(ServiceError, match="library"):
        service.open(OpenModelRequest(path="m.py", library="custom:nope"))
