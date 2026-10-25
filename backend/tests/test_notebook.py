import json
from pathlib import Path

from pyvised_backend.notebook import notebook_source

FIXTURES = Path(__file__).parent / "fixtures"


def notebook(tmp_path: Path, *cells: str | dict) -> Path:
    entries = []
    for cell in cells:
        if isinstance(cell, dict):
            entries.append(cell)
        else:
            entries.append({"cell_type": "code", "metadata": {}, "source": cell, "outputs": []})
    path = tmp_path / "n.ipynb"
    path.write_text(json.dumps({"cells": entries, "nbformat": 4}), encoding="utf-8")
    return path


def test_magics_and_shell_escapes_are_neutralised(tmp_path: Path):
    source = notebook_source(
        notebook(
            tmp_path,
            {"cell_type": "markdown", "source": "# title"},
            "%matplotlib inline\nimport math\n!pip install x",
            "if True:\n    %time y = 2\n    %load_ext foo\nz = 3",
        )
    )
    namespace: dict = {}
    exec(source, namespace)
    assert namespace["y"] == 2 and namespace["z"] == 3 and "math" in namespace
    assert "# title" not in source
    assert "pass  # skipped: !pip install x" in source


def test_cell_magics(tmp_path: Path):
    source = notebook_source(
        notebook(
            tmp_path,
            "%%bash\nexit 1",
            "%%time\na = 1",
            ["%%writefile data.txt\n", "hello\n"],
        )
    )
    assert "skipped (not Python)" in source
    namespace: dict = {}
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exec(source, namespace)
    finally:
        os.chdir(cwd)
    assert namespace["a"] == 1
    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == "hello\n"


def test_fixture_notebook_converts_to_valid_python():
    source = notebook_source(FIXTURES / "sphere.ipynb")
    compile(source, "sphere.ipynb", "exec")
    assert "model.export_to_model_xml()" in source
    assert "exit 1" not in source
