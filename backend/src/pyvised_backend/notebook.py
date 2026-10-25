"""Turn a Jupyter notebook into a plain script that runs its code cells in order.

IPython syntax has no meaning outside Jupyter: line magics and shell escapes
(``%matplotlib``, ``!pip``) become ``pass``, magics that time Python code
(``%time``, ``%%time``, ``%%capture``, ...) keep the code, ``%%writefile``
writes its file, and cells in other languages (``%%bash``, ``%%html``, ...)
are skipped.
"""

import json
from pathlib import Path

PYTHON_CELL_MAGICS = {"time", "timeit", "capture", "prun"}
PYTHON_LINE_MAGICS = {"time", "timeit", "prun"}


def _cell_lines(source: str | list[str]) -> list[str]:
    text = "".join(source) if isinstance(source, list) else source
    return text.splitlines()


def _writefile(header: str, body: list[str]) -> list[str] | None:
    args = header.split()[1:]
    append = "-a" in args or "--append" in args
    names = [a for a in args if not a.startswith("-")]
    if not names:
        return None
    mode = "a" if append else "w"
    text = "\n".join(body) + "\n"
    return [f"open({names[0]!r}, {mode!r}, encoding='utf-8').write({text!r})"]


def _convert_cell(lines: list[str]) -> list[str] | None:
    if lines and lines[0].lstrip().startswith("%%"):
        header = lines[0].strip()[2:]
        magic = header.split()[0] if header.split() else ""
        if magic == "writefile":
            return _writefile(header, lines[1:])
        if magic not in PYTHON_CELL_MAGICS:
            return None
        lines = lines[1:]
    out = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        magic, _, statement = stripped[1:].partition(" ")
        if stripped.startswith("%") and magic in PYTHON_LINE_MAGICS and statement.strip():
            out.append(indent + statement.strip())
        elif stripped.startswith(("%", "!")):
            out.append(f"{indent}pass  # skipped: {stripped}")
        else:
            out.append(line)
    return out


def notebook_source(path: Path) -> str:
    """The code cells of the notebook at `path` as one Python source text."""
    notebook = json.loads(path.read_text(encoding="utf-8"))
    parts = []
    for index, cell in enumerate(notebook.get("cells", []), start=1):
        if cell.get("cell_type") != "code":
            continue
        lines = _convert_cell(_cell_lines(cell.get("source", "")))
        if lines is None:
            parts.append(f"# %% cell {index}: skipped (not Python)")
        else:
            parts.append(f"# %% cell {index}\n" + "\n".join(lines))
    return "\n\n".join(parts) + "\n"
