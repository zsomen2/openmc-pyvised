"""The shipped example models and how the app copies them into the workspace."""

from pathlib import Path

import pytest

from pyvised.core.examples import CopyState, find_examples
from pyvised.core.paths import examples_dir
from pyvised_protocol.sidecar import SIDECAR_NAME

EXAMPLES, PROBLEMS = find_examples(examples_dir())


def test_every_example_folder_is_an_example():
    assert EXAMPLES and not PROBLEMS


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.folder.name)
def test_examples_are_complete_in_both_languages(example):
    sidecar = example.sidecar
    for text in (sidecar.title, sidecar.description):
        assert text.en.strip() and text.hu.strip()
        assert "**" not in text.en + text.hu, "task texts without Markdown"
    defaults = sidecar.parameter_defaults()
    for p in sidecar.parameters:
        assert p.label.en and p.label.hu
        assert p.minimum is None or p.minimum <= p.default
        assert p.maximum is None or p.default <= p.maximum
    for norm in sidecar.tallies.values():
        assert norm.divide_by == "none" or (norm.divisor_value(defaults) or 0) > 0
    for reference in sidecar.references:
        assert set(reference.params) <= set(defaults)
    for preset in sidecar.run_presets:
        assert preset.label.en and preset.label.hu
    compile(example.model.read_text(encoding="utf-8"), str(example.model), "exec")


def test_copy_restore_and_runs_of_the_copy(tmp_path: Path):
    example = EXAMPLES[0]
    workspace = tmp_path / "ws"
    assert example.copy_state(workspace) is CopyState.MISSING
    model = example.copy_to(workspace)
    assert model == workspace / "examples" / example.folder.name / example.model.name
    assert model.is_file() and example.copy_state(workspace) is CopyState.SAME
    model.write_text("# my own version\n", encoding="utf-8")
    (model.parent / "runs").mkdir()
    (model.parent / "runs" / "run.json").write_text("{}", encoding="utf-8")
    assert example.copy_state(workspace) is CopyState.CHANGED
    (model.parent / SIDECAR_NAME).unlink()
    assert example.copy_state(workspace) is CopyState.CHANGED
    example.copy_to(workspace)
    assert example.copy_state(workspace) is CopyState.SAME
    assert (model.parent / "runs" / "run.json").is_file(), "restoring keeps the runs"


def test_folders_that_are_no_examples(tmp_path: Path):
    good = EXAMPLES[0]
    for name in ("task2_two_models", "task3_no_sidecar", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / "task2_two_models" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "task2_two_models" / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "task3_no_sidecar" / "m.py").write_text("", encoding="utf-8")
    copy = tmp_path / "task7_copy"
    copy.mkdir()
    for name in good.files():
        (copy / name).write_bytes((good.folder / name).read_bytes())
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "x.pyc").write_bytes(b"")
    examples, problems = find_examples(tmp_path)
    assert [(e.number, e.folder.name) for e in examples] == [(7, "task7_copy")]
    assert examples[0].files() == good.files()
    assert len(problems) == 3
    assert find_examples(tmp_path / "missing") == ([], [])
