"""The Input tab: file list, code view, live reload, find and go to line, a run's input."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from fakes import FakeApi
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication
from sim_fakes import run_info

from pyvised.backend.client import Connection
from pyvised.core import inputs, model_session
from pyvised.core.model_session import ModelSession
from pyvised.core.settings import CODE_FONT_DEFAULT
from pyvised.core.tasks import TaskRunner
from pyvised.ui.input import input_tab
from pyvised.ui.input.input_tab import InputTab


def model_text(changed: str = "") -> str:
    lines = ['"""A test builder."""', "", "import openmc", "", "from helper import R"]
    for n in range(6, 201):
        special = {10: "# the sphere", 50: "SPHERE = 3", 120: "name = 'Sphere'"}
        lines.append(special.get(n, f"x{n} = {n}{changed}  # line {n}"))
    return "\n".join(lines) + "\n"


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class WatchedSession(QObject):
    """The part of `ModelSession` the tab uses: its file watcher's signal."""

    filesChanged = Signal()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    folder = tmp_path / "hw"
    write(folder / "model.py", model_text())
    write(folder / "helper.py", "R = 3.0  # cm\n")
    write(folder / "notes.ipynb", json.dumps({"cells": [{"cell_type": "code", "source": "1"}]}))
    for name in ("pyvised.json", "data.csv", "model.xml", "runs/20261026-120000/model.xml"):
        write(folder / name, "{}")
    return folder


@pytest.fixture
def tab(qtbot, settings):
    widget = InputTab(WatchedSession(), settings)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.resize(1200, 700)
    widget.show()
    return widget


def test_without_a_model_the_tab_says_so(tab: InputTab, tmp_path: Path):
    assert tab.stack.currentWidget() is tab.empty
    tab.show_model(write(tmp_path / "xml" / "model.xml", "<model/>"))
    assert tab.stack.currentWidget() is tab.xml_state, "Python builders and notebooks only"
    assert tab.file_names() == []


def test_the_model_file_comes_first_with_its_text(tab: InputTab, folder: Path):
    tab.show_model(folder / "model.py")
    assert tab.stack.currentWidget() is tab.content
    assert tab.file_names() == ["Model files", "model.py", "helper.py", "notes.ipynb"]
    current = tab.files.currentItem()
    assert current is not None and current.text() == "model.py" and current.font().bold()
    assert current.toolTip() == str(folder / "model.py")
    assert tab.code.toPlainText() == model_text()
    assert tab.code.blockCount() == 201 and tab.line.placeholderText() == "1–201"
    assert tab.file_label.text() == "model.py"
    assert tab.code.isReadOnly()
    tab.files.setCurrentRow(2)
    assert tab.shown == folder / "helper.py" and tab.code.toPlainText() == "R = 3.0  # cm\n"
    tab.files.setCurrentRow(3)
    assert tab.code.toPlainText().split("\n")[1] == "1", "a notebook shows its cells"


def test_a_changed_file_is_read_again_in_place(qtbot, settings, monkeypatch, folder: Path):
    monkeypatch.setattr(model_session, "ApiClient", FakeApi)
    tasks = TaskRunner()
    session = ModelSession(tasks)
    session.set_connection(Connection("http://127.0.0.1:1", "t"), folder.parent)
    tab = InputTab(session, settings)
    qtbot.addWidget(tab)
    tab.resize(1000, 500)
    tab.show()
    model = folder / "model.py"
    with qtbot.waitSignal(session.opened, timeout=5000):
        session.open(model)
    tab.show_model(model)
    tab.code.go_to_line(150)
    position = tab.code.position()
    assert position.top > 100 and position.cursor == position.anchor == (149, 0)
    with qtbot.waitSignal(session.filesChanged, timeout=5000):
        model.write_text(model_text(changed=" + 1"), encoding="utf-8")  # the session's watcher
    assert tab.code.toPlainText() == model_text(changed=" + 1")
    assert tab.code.position() == position, "the same lines in view, the cursor on its line"

    tab.files.setCurrentRow(2)  # helper.py
    with qtbot.waitSignal(session.filesChanged, timeout=5000):
        write(folder / "helper.py", "R = 4.0  # cm\n")
    assert tab.shown == folder / "helper.py" and tab.code.toPlainText() == "R = 4.0  # cm\n"
    with qtbot.waitSignal(session.filesChanged, timeout=5000):
        write(folder / "extra.py", "")
    assert tab.file_names()[1:] == ["model.py", "extra.py", "helper.py", "notes.ipynb"]
    assert tab.files.currentItem().text() == "helper.py"  # type: ignore[union-attr]
    tab.files.setCurrentRow(1)
    assert tab.code.position() == position, "back where model.py was left"
    tasks.shutdown()


def test_find_counts_matches_and_go_to_line_jumps(qtbot, tab: InputTab, folder: Path):
    tab.show_model(folder / "model.py")
    tab.find_field.setText("SPHERE")
    assert tab.count_label.text() == "1/3"
    assert tab.code.textCursor().selectedText() == "sphere"
    assert tab.code.textCursor().blockNumber() == 9
    assert len(tab.code.extraSelections()) == 1 + 3, "the current line and every match"
    tab.find_next()
    assert tab.count_label.text() == "2/3" and tab.code.textCursor().selectedText() == "SPHERE"
    qtbot.keyClick(tab.find_field, Qt.Key.Key_Return)
    assert tab.count_label.text() == "3/3" and tab.code.textCursor().blockNumber() == 119
    tab.find_next()
    assert tab.count_label.text() == "1/3", "round again"
    tab.find_previous()
    assert tab.count_label.text() == "3/3"
    tab.find_field.setText("nowhere")
    assert tab.count_label.text() == "no matches"
    tab.find_field.clear()
    assert tab.count_label.text() == "" and len(tab.code.extraSelections()) == 1

    tab.line.setText("40")
    qtbot.keyClick(tab.line, Qt.Key.Key_Return)
    cursor = tab.code.textCursor()
    assert cursor.blockNumber() == 39 and cursor.positionInBlock() == 0
    tab.line.setText("9999")
    qtbot.keyClick(tab.line, Qt.Key.Key_Return)
    assert tab.code.textCursor().blockNumber() == 200, "the last line"

    tab.find_field.setText("sphere")
    assert tab.count_label.text() == "1/3", "past the last match: from the top"
    tab.code.go_to_line(60)
    tab.files.setCurrentRow(2)
    tab.files.setCurrentRow(1)  # read again: no match selected, F3 goes on from the cursor
    assert tab.count_label.text() == "–/3" and tab.code.textCursor().blockNumber() == 59
    tab.find_next()
    assert tab.count_label.text() == "3/3"
    tab.files.setCurrentRow(2)
    tab.files.setCurrentRow(1)
    assert tab.count_label.text() == "3/3", "the selected match is kept"
    tab.find_previous()
    assert tab.count_label.text() == "2/3"


def test_the_shown_run_adds_its_copy_of_the_code(tab: InputTab, folder: Path):
    tab.show_model(folder / "model.py")
    run = run_info("run3")
    run.created = datetime.now().astimezone()
    copies = folder / "runs" / "run3" / "input"
    write(copies / "helper.py", "R = 2.0\n")
    write(copies / "model.py", "import helper  # as it was\n")
    tab.set_run(run, folder / "runs" / "run3")
    title = f"Run {run.created:%H:%M} “run3”"
    names = tab.file_names()
    assert names[4:] == ["", title, "model.py", "helper.py"]
    assert tab.files.item(6).font().bold(), "the run's model file"
    tab.files.setCurrentRow(6)
    assert tab.code.toPlainText() == "import helper  # as it was\n"
    assert tab.file_label.text() == f"model.py  ·  {title}"
    assert tab.file_label.toolTip() == str(copies / "model.py")

    older = run_info("run1")
    older.created, older.label = datetime(2026, 1, 2, 3, 4), ""
    tab.set_run(older, folder / "runs" / "run1")  # made before runs kept their input
    assert len(tab.file_names()) == 4
    assert tab.shown == folder / "model.py", "back to the model's own file"
    write(folder / "runs" / "run1" / "input" / "model.py")
    tab.set_run(None, None)
    tab.set_run(older, folder / "runs" / "run1")
    assert tab.file_names()[5] == f"Run {older.created.astimezone():%Y-%m-%d %H:%M}"
    tab.set_run(None, None)
    assert len(tab.file_names()) == 4


def test_notebooks_large_files_and_broken_notebooks(tab: InputTab, tmp_path: Path, monkeypatch):
    cells = [
        {"cell_type": "markdown", "source": ["# Iron sphere"]},
        {"cell_type": "code", "execution_count": 2, "source": ["import openmc\n", "r = 3"]},
    ]
    model = write(tmp_path / "nb" / "model.ipynb", json.dumps({"cells": cells}))
    tab.show_model(model)
    lines = tab.code.toPlainText().split("\n")
    assert lines[0].startswith("── Markdown ──") and lines[3].startswith("── In [2] ──")
    assert lines[1] == "# Iron sphere" and lines[4:] == ["import openmc", "r = 3"]
    assert len(tab.code.extraSelections()) == 1 + 2, "the cell headers span the view"

    write(model, '{"cells": [')
    tab.refresh()
    assert tab.code.toPlainText() == '{"cells": ['
    assert tab.file_label.text() == "model.ipynb  ·  not a valid notebook, shown as text"

    monkeypatch.setattr(inputs, "MAX_BYTES", 5)
    tab.refresh()
    assert tab.view_stack.currentWidget() is tab.message
    assert "too large" in tab.message.text() and not tab.find_field.isEnabled()
    assert tab.act_open.isEnabled(), "it can still be opened in an editor"


def test_open_in_editor_never_runs_the_builder(tab: InputTab, folder: Path, monkeypatch):
    started: list[tuple[str, list[str], str]] = []
    monkeypatch.setattr(
        input_tab, "start_detached", lambda p, a, n="": started.append((p, a, n)) or True
    )
    code = folder / "Code.exe"
    monkeypatch.setattr(input_tab, "vscode", lambda: code)
    tab.show_model(folder / "model.py")
    tab.act_open.trigger()
    assert started[-1] == (str(code), [str(folder / "model.py")], "")
    monkeypatch.setattr(input_tab, "vscode", lambda: None)
    monkeypatch.setattr(inputs, "notepad", lambda: "notepad.exe")
    tab.open_in_editor(folder / "helper.py")
    assert started[-1] == ("notepad.exe", [str(folder / "helper.py")], "")
    tab.open_in_editor(folder / "notes.ipynb")
    assert started[-1] == ("explorer.exe", [], f'/select,"{folder / "notes.ipynb"}"')
    tab.act_copy.trigger()
    clipboard = QApplication.clipboard()
    assert clipboard is not None and clipboard.text() == str(folder / "model.py")
    menu = tab.file_menu(folder / "helper.py")
    assert [a.text() for a in menu.actions()] == ["Open in editor", "Show in folder", "Copy path"]


def test_the_font_size_zooms_and_is_saved(qtbot, tab: InputTab, folder: Path, settings):
    tab.show_model(folder / "model.py")
    assert tab.code.font().pointSize() == CODE_FONT_DEFAULT
    mod = Qt.KeyboardModifier.ControlModifier
    qtbot.keyClick(tab.code, Qt.Key.Key_Plus, mod)
    qtbot.keyClick(tab.code, Qt.Key.Key_Plus, mod)
    qtbot.keyClick(tab.code, Qt.Key.Key_Minus, mod)
    assert tab.code.font().pointSize() == CODE_FONT_DEFAULT + 1
    assert settings.input_font_size == CODE_FONT_DEFAULT + 1
    again = InputTab(WatchedSession(), settings)  # type: ignore[arg-type]
    qtbot.addWidget(again)
    assert again.code.font().pointSize() == CODE_FONT_DEFAULT + 1
    qtbot.keyClick(tab.code, Qt.Key.Key_0, mod)
    assert settings.input_font_size == CODE_FONT_DEFAULT
    for _ in range(50):
        tab.code.zoom(-1)
    assert settings.input_font_size == 6
