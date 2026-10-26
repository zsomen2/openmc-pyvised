"""The Input tab's rules without Qt: which files, how a notebook reads, find, which editor."""

import json
from pathlib import Path

import pytest

from pyvised.core import inputs
from pyvised.core.inputs import (
    InputText,
    LineKind,
    TooLarge,
    editor_command,
    find_all,
    input_files,
    load_input,
    notebook_text,
    run_input_files,
    utf16_index,
    vscode,
)


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def notebook(*cells: dict) -> str:
    return json.dumps({"cells": list(cells), "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def test_the_model_file_first_then_its_helpers(tmp_path: Path):
    model = write(tmp_path / "sphere.py")
    for name in ("Zeta.py", "alpha.py", "notes.ipynb", "pyvised.json", "data.json", "table.csv"):
        write(tmp_path / name)
    for name in ("readme.txt", "model.xml", "geometry.xml", "runs/20261026-1200/model.py"):
        write(tmp_path / name)
    write(tmp_path / "lib" / "deep.py")
    names = [p.name for p in input_files(model)]
    assert names == ["sphere.py", "alpha.py", "notes.ipynb", "Zeta.py"]


def test_an_xml_model_has_no_input(tmp_path: Path):
    model = write(tmp_path / "model.xml", "<model/>")
    write(tmp_path / "helper.py")
    assert input_files(model) == []


def test_a_missing_model_file_leaves_its_helpers(tmp_path: Path):
    write(tmp_path / "helper.py")
    assert [p.name for p in input_files(tmp_path / "gone.py")] == ["helper.py"]


def test_a_run_lists_its_copies_with_the_model_first(tmp_path: Path):
    run = tmp_path / "runs" / "20261026-144700_r-3"
    write(run / "model.xml")
    assert run_input_files(run, "sphere.py") == [], "a run made before the copies"
    for name in ("a_helper.py", "sphere.py"):
        write(run / "input" / name)
    assert [p.name for p in run_input_files(run, "sphere.py")] == ["sphere.py", "a_helper.py"]


def test_text_is_utf8_with_replacements_and_plain_line_ends(tmp_path: Path):
    path = tmp_path / "m.py"
    path.write_bytes(b"\xef\xbb\xbfx = 1\r\ny = '\xff'\rz = 'r\xc3\xbad'\n")
    loaded = load_input(path)
    assert loaded == InputText("x = 1\ny = '�'\nz = 'rúd'\n")


def test_files_over_the_limit_are_not_loaded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(inputs, "MAX_BYTES", 10)
    path = tmp_path / "big.py"
    path.write_bytes(b"x = 1234567890\n")
    with pytest.raises(TooLarge) as caught:
        load_input(path)
    assert caught.value.size == 15
    with pytest.raises(OSError):
        load_input(tmp_path / "missing.py")


def test_a_notebook_reads_as_its_cells(tmp_path: Path):
    text = notebook(
        {"cell_type": "markdown", "metadata": {}, "source": ["# Task 1\n", "An *iron* sphere."]},
        {
            "cell_type": "code",
            "execution_count": 3,
            "metadata": {},
            "outputs": [{"output_type": "stream", "name": "stdout", "text": ["SECRET OUTPUT"]}],
            "source": ["import openmc\n", "r = 3.0"],
        },
        {"cell_type": "code", "execution_count": None, "metadata": {}, "source": "s = '''a\nb'''"},
        {"cell_type": "raw", "metadata": {}, "source": ""},
    )
    loaded = load_input(write(tmp_path / "model.ipynb", text))
    lines = loaded.text.split("\n")
    assert lines[0].startswith("── Markdown ──") and len(lines[0]) == inputs.HEADER_WIDTH
    assert lines[1:3] == ["# Task 1", "An *iron* sphere."]
    assert lines[4].startswith("── In [3] ──")
    assert lines[5:7] == ["import openmc", "r = 3.0"]
    assert lines[8].startswith("── In [ ] ──")
    assert lines[9:11] == ["s = '''a", "b'''"]
    assert lines[12].startswith("── Raw ──") and lines[13] == ""
    assert "SECRET OUTPUT" not in loaded.text, "outputs are left out"
    kinds = loaded.kinds
    assert kinds is not None and len(kinds) == len(lines)
    header, text_, code = LineKind.HEADER, LineKind.TEXT, LineKind.CODE
    assert kinds[:8] == [header, text_, text_, text_, header, code, code, text_]
    assert kinds[8:] == [header, code, code, text_, header, text_]
    assert not loaded.broken


def test_a_broken_notebook_is_shown_as_text(tmp_path: Path):
    loaded = load_input(write(tmp_path / "model.ipynb", '{"cells": [\n  {"cell_type": "co'))
    assert loaded.broken and loaded.text.startswith('{"cells"')
    assert loaded.kinds == [LineKind.TEXT, LineKind.TEXT]
    assert notebook_text("[1, 2]") is None, "JSON, but not a notebook"
    assert notebook_text('{"cells": []}') == InputText("", [])


def test_find_ignores_case_and_counts_utf16_positions():
    text = "import openmc\nOpenMC = 1  # 🙂 openmc\n"
    spans = find_all(text, "OPENMC")
    assert spans == [(7, 13), (14, 20), (30, 36)]
    assert find_all(text, "") == [] and find_all(text, "a.b") == []
    position = utf16_index(text)
    assert [position(a) for a, _b in spans] == [7, 14, 31], "the emoji takes two units"
    assert utf16_index("plain")(3) == 3


def test_open_in_editor_never_uses_the_default_program(tmp_path: Path, monkeypatch):
    code = tmp_path / "VS Code" / "Code.exe"
    builder, notes = tmp_path / "m.py", tmp_path / "m.ipynb"
    assert editor_command(builder, code) == [str(code), str(builder)]
    assert editor_command(notes, code) == [str(code), str(notes)]
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "nowhere"))
    assert editor_command(builder, None) == ["notepad.exe", str(builder)]
    assert editor_command(notes, None) is None, "a notebook without VS Code: its folder"


def test_vscode_is_found_next_to_its_command_or_in_the_user_install(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "pf"))
    monkeypatch.setattr(inputs.shutil, "which", lambda name: None)
    assert vscode() is None
    user = write(tmp_path / "local" / "Programs" / "Microsoft VS Code" / "Code.exe")
    assert vscode() == user
    portable = write(tmp_path / "portable" / "Code.exe")
    command = write(tmp_path / "portable" / "bin" / "code.cmd")
    monkeypatch.setattr(inputs.shutil, "which", lambda name: str(command))
    assert vscode() == portable.resolve(), "the `code` on PATH comes first"
