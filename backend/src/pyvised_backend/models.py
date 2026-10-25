"""Opening models: workspace paths, export to a cached `model.xml`, `ModelInfo`.

A model is a `.py` builder, an `.ipynb` notebook or OpenMC XML (`model.xml`,
or `geometry.xml` with its siblings). Builders run in a subprocess in
export-only mode. Whatever comes out is normalised to one `model.xml` (plot
mode needs that or a `plots.xml`) in a cache folder named by a hash of the
source files, parameters and library, so reopening an unchanged model is
instant.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pyvised_protocol.schemas import ModelInfo, ModelKind, OpenModelRequest
from pyvised_protocol.sidecar import SIDECAR_NAME, Sidecar

from . import libraries
from .config import Config
from .errors import ServiceError
from .modelinfo import RESULT_MARKER

CACHE_FORMAT = 1
"""Raised when the cached files change meaning, so old cache folders are not reused."""
KINDS: dict[str, ModelKind] = {".py": "python", ".ipynb": "notebook", ".xml": "xml"}
SEPARATE_XML = ("materials", "geometry", "settings", "tallies", "plots")
DEPENDENCY_SUFFIXES = {".py", ".ipynb", ".json", ".csv", ".txt", ".dat", ".xml", ".yaml", ".yml"}
"""Files next to a builder that may feed it (helper modules, data); they are part of the key."""
MAX_DEPENDENCY_BYTES = 5_000_000
KEEP_CACHED = 30
INFO_TIMEOUT = 120.0
OUTPUT_LIMIT = 20_000
"""Characters of builder output kept for the client."""

Builder = Callable[[Path, Path, dict[str, float], str | None, float], str]
Describer = Callable[[Path], dict[str, Any]]


@dataclass
class OpenedModel:
    info: ModelInfo
    directory: Path
    """Cache folder holding `model.xml`."""

    @property
    def model_id(self) -> str:
        return self.info.model_id


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…\n" + text[-limit:]


def resolve_source(workspace: Path, relative: str) -> Path:
    """The file at the workspace-relative `relative`; it must stay inside the workspace."""
    text = relative.strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise ServiceError("bad_path", "no model path given", status=400)
    root = workspace.resolve()
    path = (root / PurePosixPath(text)).resolve()
    if PurePosixPath(text).is_absolute() or not path.is_relative_to(root):
        raise ServiceError(
            "outside_workspace", f"{relative} is not inside the workspace", status=403
        )
    if not path.is_file():
        raise ServiceError("not_found", f"{relative} does not exist in the workspace", status=404)
    return path


def model_kind(path: Path) -> ModelKind:
    kind = KINDS.get(path.suffix.lower())
    if kind is None:
        raise ServiceError(
            "unsupported", f"{path.name}: open a .py builder, an .ipynb notebook or OpenMC XML"
        )
    return kind


def read_sidecar(folder: Path) -> tuple[Sidecar | None, str | None]:
    """The sidecar next to a model, or None with a warning when it cannot be read."""
    path = folder / SIDECAR_NAME
    if not path.is_file():
        return None, None
    try:
        return Sidecar.model_validate_json(path.read_bytes()), None
    except (OSError, ValueError) as exc:
        return None, f"{SIDECAR_NAME} was ignored: {exc}"


def xml_root_tag(path: Path) -> str:
    try:
        for _event, element in ET.iterparse(path, events=("start",)):
            return element.tag
    except ET.ParseError as exc:
        raise ServiceError("bad_xml", f"{path.name}: {exc}") from exc
    return ""


def source_files(source: Path, kind: ModelKind) -> list[Path]:
    """The files whose content decides the exported model."""
    folder = source.parent
    if kind == "xml":
        if xml_root_tag(source) == "model":
            return [source]
        return [folder / f"{n}.xml" for n in SEPARATE_XML if (folder / f"{n}.xml").is_file()]
    files = [source]
    for path in sorted(folder.iterdir()):
        if path == source or path.name == SIDECAR_NAME or not path.is_file():
            continue
        if (
            path.suffix.lower() in DEPENDENCY_SUFFIXES
            and path.stat().st_size <= MAX_DEPENDENCY_BYTES
        ):
            files.append(path)
    return files


def cache_key(
    files: list[Path], kind: ModelKind, params: dict[str, float], library: str | None
) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([CACHE_FORMAT, kind, library, sorted(params.items())]).encode())
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue  # vanished between listing and reading; the next open sees the change
        digest.update(path.name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()[:16]


def merge_separate_xml(folder: Path, target: Path) -> None:
    """Combine `geometry.xml`, `materials.xml` and their siblings into one `model.xml`."""
    missing = [n for n in ("geometry", "materials") if not (folder / f"{n}.xml").is_file()]
    if missing:
        names = " and ".join(f"{n}.xml" for n in missing)
        raise ServiceError("incomplete_xml", f"{names} not found next to the model")
    root = ET.Element("model")
    for name in SEPARATE_XML:
        path = folder / f"{name}.xml"
        if path.is_file():
            try:
                root.append(ET.parse(path).getroot())
            except ET.ParseError as exc:
                raise ServiceError("bad_xml", f"{path.name}: {exc}") from exc
    if root.find("settings") is None:
        ET.SubElement(root, "settings")
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)


def copy_xml(source: Path, folder: Path) -> None:
    if xml_root_tag(source) == "model":
        shutil.copyfile(source, folder / "model.xml")
    else:
        merge_separate_xml(source.parent, folder / "model.xml")


def collect_export(folder: Path) -> None:
    """Make sure `folder/model.xml` holds what a builder exported into `folder`."""
    target = folder / "model.xml"
    if target.is_file():
        return
    if (folder / "geometry.xml").is_file():
        merge_separate_xml(folder, target)
        return
    for pattern in ("*/model.xml", "*/*/model.xml"):
        for path in sorted(folder.glob(pattern)):
            shutil.copyfile(path, target)
            return
    for pattern in ("*/geometry.xml", "*/*/geometry.xml"):
        for path in sorted(folder.glob(pattern)):
            merge_separate_xml(path.parent, target)
            return
    raise ServiceError(
        "no_export",
        "The script ran but exported no model. End it with model.export_to_model_xml() "
        "(or model.export_to_xml()).",
    )


def run_builder(
    source: Path, folder: Path, params: dict[str, float], library: str | None, timeout: float
) -> str:
    """Run a `.py`/`.ipynb` builder in `folder` in export-only mode; returns its output."""
    env = dict(os.environ)
    env.update(
        PYVISED_EXPORT_ONLY="1",
        PYVISED_PARAMS=json.dumps(params),
        PYVISED_SOURCE_DIR=str(source.parent),
        MPLBACKEND="Agg",
        PYTHONUNBUFFERED="1",
    )
    if library is not None:
        env["OPENMC_CROSS_SECTIONS"] = library
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyvised_backend.builder_runner", str(source)],
            cwd=folder,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.output.decode(errors="replace") if isinstance(exc.output, bytes) else ""
        raise ServiceError(
            "build_timeout", f"The script did not finish within {timeout:.0f} s.\n{_tail(output)}"
        ) from exc
    if proc.returncode != 0:
        raise ServiceError("build_failed", _tail(proc.stdout))
    return proc.stdout


def describe_model(model_xml: Path) -> dict[str, Any]:
    """Run `pyvised_backend.modelinfo` on `model_xml` in a subprocess."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyvised_backend.modelinfo", str(model_xml)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=INFO_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceError("info_failed", "reading the model took too long") from exc
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    raise ServiceError("info_failed", _tail(proc.stderr or proc.stdout))


class ModelService:
    """Opens models into the cache and remembers them by `model_id` (the cache key)."""

    def __init__(
        self,
        config: Config,
        *,
        builder: Builder = run_builder,
        describer: Describer = describe_model,
    ) -> None:
        self.config = config
        self._builder = builder
        self._describer = describer
        self._models: dict[str, OpenedModel] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    @property
    def root(self) -> Path:
        return self.config.cache_dir / "models"

    def get(self, model_id: str) -> OpenedModel:
        with self._lock:
            model = self._models.get(model_id)
        if model is None:
            raise ServiceError("unknown_model", f"model {model_id} is not open", status=404)
        return model

    def open(self, request: OpenModelRequest) -> OpenedModel:
        source = resolve_source(self.config.workspace, request.path)
        kind = model_kind(source)
        sidecar, sidecar_warning = read_sidecar(source.parent)
        params = {**(sidecar.parameter_defaults() if sidecar else {}), **request.params}
        library_id, library_path = self._library(request.library)
        key = cache_key(source_files(source, kind), kind, params, library_path)
        directory = self.root / key
        with self._key_lock(key):
            cached = self._read_cache(directory)
            if cached is None:
                cached = self._export(source, kind, params, library_path, directory)
        summary = dict(cached["summary"])
        warnings = list(summary.pop("warnings", []))
        if sidecar_warning:
            warnings.insert(0, sidecar_warning)
        info = ModelInfo(
            model_id=key,
            source=source.relative_to(self.config.workspace.resolve()).as_posix(),
            kind=kind,
            library=library_id,
            params=params,
            sidecar=sidecar,
            warnings=warnings,
            builder_output=cached["builder_output"],
            build_seconds=cached["build_seconds"],
            **summary,
        )
        model = OpenedModel(info, directory)
        with self._lock:
            self._models[key] = model
        self._prune()
        return model

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _library(self, library_id: str | None) -> tuple[str | None, str | None]:
        found = libraries.discover(self.config.cross_sections, self.config.custom_data)
        for lib in found:
            if (library_id is None and lib.builtin) or lib.id == library_id:
                return lib.id, lib.path
        if library_id is None:
            return None, None
        raise ServiceError("unknown_library", f"no nuclear-data library {library_id!r}")

    def _read_cache(self, directory: Path) -> dict[str, Any] | None:
        try:
            cached = json.loads((directory / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not (directory / "model.xml").is_file():
            return None
        os.utime(directory)  # most recently used, see _prune
        return cached

    def _export(
        self,
        source: Path,
        kind: ModelKind,
        params: dict[str, float],
        library: str | None,
        directory: Path,
    ) -> dict[str, Any]:
        work = directory.with_name(f"{directory.name}.{uuid.uuid4().hex[:8]}.tmp")
        work.mkdir(parents=True)
        try:
            start = time.perf_counter()
            output = ""
            if kind == "xml":
                copy_xml(source, work)
            else:
                output = self._builder(source, work, params, library, self.config.build_timeout)
                collect_export(work)
            cached = {
                "summary": self._describer(work / "model.xml"),
                "builder_output": output[-OUTPUT_LIMIT:],
                "build_seconds": time.perf_counter() - start,
            }
            (work / "info.json").write_text(json.dumps(cached), encoding="utf-8")
            shutil.rmtree(directory, ignore_errors=True)
            work.rename(directory)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return cached

    def _prune(self) -> None:
        """Delete all but the `KEEP_CACHED` most recently used cache folders."""
        try:
            folders = [d for d in self.root.iterdir() if d.is_dir() and d.suffix != ".tmp"]
            folders.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in folders[KEEP_CACHED:]:
            with self._lock:
                self._models.pop(old.name, None)
            shutil.rmtree(old, ignore_errors=True)
