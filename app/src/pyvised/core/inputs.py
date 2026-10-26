"""The open model's own code, for the Input tab: which files it shows and how they read.

The tab shows the model file first, then the other `.py` and `.ipynb` files in its
folder (the helper modules a builder imports), by name. It leaves out the sidecar,
data files, XML (an XML model shows nothing) and the run folders. The backend's
cache key (`source_files`) also counts data files, so the two rules are not shared.
A run keeps copies of the model's code in its `INPUT_FOLDER`, read with the same rule.

Notebooks read as their cells: a header line per cell, code cells as code,
Markdown as plain text, outputs left out. "Open in editor" must never run a
builder: on many Windows PCs opening a `.py` file with its default program runs
it (py.exe), so it takes VS Code when installed, else Notepad.
"""

import json
import os
import re
import shutil
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pyvised_protocol.runs import INPUT_FOLDER

INPUT_SUFFIXES = (".py", ".ipynb")
MAX_BYTES = 5_000_000
"""Larger files are not loaded: open them in an editor."""
HEADER_WIDTH = 64
"""Characters of a notebook cell's header line."""
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")


class LineKind(StrEnum):
    CODE = "code"
    HEADER = "header"
    """A notebook cell's header line."""
    TEXT = "text"
    """Markdown and raw cells, the gaps between cells, a notebook shown as plain text."""


@dataclass
class InputText:
    text: str
    kinds: list[LineKind] | None = None
    """What each line is; None: all Python code."""
    broken: bool = False
    """A notebook that is not valid JSON, shown as plain text."""


class TooLarge(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"{size} bytes")
        self.size = size


def is_input(path: Path) -> bool:
    return path.suffix.lower() in INPUT_SUFFIXES


def input_files(model: Path) -> list[Path]:
    """The model file, then the other `.py`/`.ipynb` files of its folder by name; nothing for
    an XML model."""
    if not is_input(model):
        return []
    try:
        others = [p for p in model.parent.iterdir() if p != model and is_input(p) and p.is_file()]
    except OSError:
        others = []
    others.sort(key=lambda p: (p.name.lower(), p.name))
    return [model, *others] if model.is_file() else others


def run_input_files(run_folder: Path, model_name: str) -> list[Path]:
    """The copies of a run's code in its `INPUT_FOLDER`, the model file first; none for runs
    made before the copies (or of an XML model)."""
    folder = run_folder / INPUT_FOLDER
    return input_files(folder / model_name) if folder.is_dir() else []


def decode(data: bytes) -> str:
    """UTF-8 without its byte-order mark, undecodable bytes replaced, line ends as "\\n"."""
    text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_input(path: Path) -> InputText:
    """The text of a model file as the tab shows it. Raises OSError or `TooLarge`."""
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise TooLarge(size)
    text = decode(path.read_bytes())
    if path.suffix.lower() != ".ipynb":
        return InputText(text)
    cells = notebook_text(text)
    if cells is None:
        return InputText(text, [LineKind.TEXT] * (text.count("\n") + 1), broken=True)
    return cells


def _header(title: str) -> str:
    start = f"── {title} "
    return start + "─" * max(2, HEADER_WIDTH - len(start))


def notebook_text(text: str) -> InputText | None:
    """A notebook's cells under header lines (``── In [3] ──`` for code with its execution
    count, ``── Markdown ──``) without outputs; None when `text` is not a notebook."""
    try:
        notebook = json.loads(text)
    except ValueError:
        return None
    cells = notebook.get("cells") if isinstance(notebook, dict) else None
    if not isinstance(cells, list):
        return None
    lines: list[str] = []
    kinds: list[LineKind] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("cell_type", ""))
        source = cell.get("source", "")
        body = "".join(map(str, source)) if isinstance(source, list) else str(source)
        if cell_type == "code":
            count = cell.get("execution_count")
            title, kind = f"In [{count if isinstance(count, int) else ' '}]", LineKind.CODE
        else:
            title, kind = cell_type.capitalize() or "Cell", LineKind.TEXT
        if lines:
            lines.append("")
            kinds.append(LineKind.TEXT)
        lines.append(_header(title))
        kinds.append(LineKind.HEADER)
        body_lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(body_lines) > 1 and body_lines[-1] == "":
            body_lines.pop()
        lines.extend(body_lines)
        kinds.extend([kind] * len(body_lines))
    return InputText("\n".join(lines), kinds)


def find_all(text: str, needle: str) -> list[tuple[int, int]]:
    """Start and end of each `needle` in `text`, ignoring case (matches do not overlap)."""
    if not needle:
        return []
    return [m.span() for m in re.finditer(re.escape(needle), text, re.IGNORECASE)]


def utf16_index(text: str) -> Callable[[int], int]:
    """Maps an index into `text` to Qt's position there, which counts UTF-16 code units:
    characters beyond U+FFFF (emoji) take two."""
    astral = [] if text.isascii() else [m.start() for m in _ASTRAL.finditer(text)]
    if not astral:
        return lambda index: index
    return lambda index: index + bisect_left(astral, index)


# -- opening files elsewhere -------------------------------------------------------------------


def vscode() -> Path | None:
    """VS Code's `Code.exe`: next to the `code` command on PATH, or a per-user or all-users
    install. (`code` itself is a batch file; running Code.exe avoids cmd.exe's quoting.)"""
    candidates = []
    command = shutil.which("code")
    if command:
        candidates.append(Path(command).resolve().parent.parent / "Code.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe")
    for variable in ("ProgramFiles", "ProgramW6432"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Microsoft VS Code" / "Code.exe")
    return next((path for path in candidates if path.is_file()), None)


def notepad() -> str:
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    path = Path(root) / "System32" / "notepad.exe" if root else None
    return str(path) if path is not None and path.is_file() else "notepad.exe"


def editor_command(path: Path, code: Path | None) -> list[str] | None:
    """The program and arguments that open `path` for reading without running it: VS Code
    (`code`, see `vscode()`) when installed, else Notepad for a `.py` file; None when there
    is nothing suitable (a notebook without VS Code)."""
    if code is not None:
        return [str(code), str(path)]
    if path.suffix.lower() == ".py":
        return [notepad(), str(path)]
    return None
