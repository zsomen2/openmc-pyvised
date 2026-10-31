"""Example models of the course tasks: shipped with the app, opened as copies in the workspace.

Each example is a folder ``task<N>_<name>`` in `examples_dir()` holding one model file
(builder, notebook or XML) and its pyvised.json, whose title is the example's short name
in the Examples menu and whose description is the task's text. Opening an example copies
its folder to ``<workspace>/examples/``, so students edit and run their own copy; a copy
that differs from the original is replaced only on request (its runs stay).
"""

import filecmp
import logging
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from pyvised_protocol.sidecar import SIDECAR_NAME, Sidecar

from .model_session import MODEL_SUFFIXES

log = logging.getLogger(__name__)

FOLDER_PATTERN = re.compile(r"task(\d+)_\w+")
WORKSPACE_FOLDER = "examples"
"""Where the copies go in the workspace."""
SKIPPED_FOLDERS = {"__pycache__", "runs"}
"""Never part of an example: Python's cache and the run history of a copy."""


class CopyState(StrEnum):
    MISSING = "missing"
    SAME = "same"
    CHANGED = "changed"
    """Some file of the example differs from the original or is missing."""


@dataclass(frozen=True)
class Example:
    number: int
    """The course task it belongs to."""
    folder: Path
    model: Path
    sidecar: Sidecar

    def title(self, language: str) -> str:
        return self.sidecar.title.get(language) or self.folder.name

    def files(self) -> list[Path]:
        """The example's files, relative to its folder."""
        return example_files(self.folder)

    def copy_folder(self, workspace: Path) -> Path:
        return workspace / WORKSPACE_FOLDER / self.folder.name

    def copy_model(self, workspace: Path) -> Path:
        """The model file of the copy in `workspace`."""
        return self.copy_folder(workspace) / self.model.name

    def copy_state(self, workspace: Path) -> CopyState:
        target = self.copy_folder(workspace)
        if not (target / self.model.name).is_file():
            return CopyState.MISSING
        for name in self.files():
            copy = target / name
            if not copy.is_file() or not filecmp.cmp(self.folder / name, copy, shallow=False):
                return CopyState.CHANGED
        return CopyState.SAME

    def copy_to(self, workspace: Path) -> Path:
        """Copy the example into `workspace`, replacing its files there; the copy's model file.

        Other files of an earlier copy (runs, the student's own files) stay.
        """
        target = self.copy_folder(workspace)
        for name in self.files():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.folder / name, target / name)
        return target / self.model.name


def example_files(folder: Path) -> list[Path]:
    """Files of an example folder (relative), without caches and run histories."""
    files = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if path.is_file() and path.suffix != ".pyc" and not SKIPPED_FOLDERS & set(relative.parts):
            files.append(relative)
    return files


def find_examples(root: Path) -> tuple[list[Example], list[str]]:
    """The examples in `root` by task number, and what is wrong with the other folders there."""
    examples: list[Example] = []
    problems: list[str] = []
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for folder in folders:
        match = FOLDER_PATTERN.fullmatch(folder.name)
        if match is None:
            if folder.name not in SKIPPED_FOLDERS:
                problems.append(f"{folder.name}: not named task<number>_<name>")
            continue
        models = [p for p in folder.iterdir() if p.is_file() and p.suffix in MODEL_SUFFIXES]
        if len(models) != 1:
            names = ", ".join(p.name for p in models) or "none"
            problems.append(f"{folder.name}: needs one model file, has {names}")
            continue
        try:
            sidecar = Sidecar.model_validate_json((folder / SIDECAR_NAME).read_bytes())
        except (OSError, ValidationError) as exc:
            problems.append(f"{folder.name}: {SIDECAR_NAME}: {exc}")
            continue
        examples.append(Example(int(match.group(1)), folder, models[0], sidecar))
    for problem in problems:
        log.warning("examples: %s", problem)
    examples.sort(key=lambda e: (e.number, e.folder.name))
    return examples, problems
