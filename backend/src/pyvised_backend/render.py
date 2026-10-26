"""Geometry slices from long-lived worker processes around ``openmc.lib``.

OpenMC calls ``MPI_Abort`` on bad input, which kills the whole process, so the
library lives only in spawned workers. Each worker keeps one model loaded in
plot mode (``openmc.lib.init(['-p', folder])``; no nuclear data needed) and
answers slice requests with ready-encoded frames: the deepest cells, plus the
cells at one universe level, the temperature and the weight-window lower
bounds when asked for. It also answers ``inspect`` (the nesting path at a
point) with the model read through the Python API. When a worker dies, the
OpenMC error it printed (its stdout and stderr go to a log file) is reported
and a fresh worker is started for the next request.
"""

import contextlib
import logging
import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from pathlib import Path
from typing import Any

from pyvised_protocol.schemas import InspectResult, SliceRequest

from .errors import ServiceError

log = logging.getLogger(__name__)

NUDGE = 1e-6
"""Shift of the cut plane [cm], so pixel centres never sit exactly on a surface."""
CALL_TIMEOUT = 300.0


class RenderError(ServiceError):
    pass


def openmc_error(log_path: Path) -> str:
    """OpenMC's ``ERROR:`` lines from a worker log, or its last lines."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for index, line in enumerate(lines):
        if "ERROR" in line:
            return "\n".join(lines[index : index + 8]).strip()
    return "\n".join(lines[-15:]).strip()


def _slice_kwargs(request: dict[str, Any]) -> dict[str, Any]:
    origin = list(request["origin"])
    normal = {"xy": 2, "xz": 1, "yz": 0}[request["basis"]]
    origin[normal] += NUDGE
    return {
        "origin": origin,
        "width": request["width"],
        "basis": request["basis"],
        "pixels": request["pixels"],
        "show_overlaps": request["show_overlaps"],
    }


class _Loaded:
    """What a worker keeps about its model besides ``openmc.lib``'s state."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self._windows: list | None = None
        self._model: Any = None

    @property
    def windows(self) -> list:
        if self._windows is None:
            from .weightwindows import read_weight_windows

            self._windows = read_weight_windows(Path(self.folder) / "model.xml")
        return self._windows

    @property
    def model(self) -> Any:
        if self._model is None:
            from .inspection import load_model

            self._model = load_model(Path(self.folder) / "model.xml")
        return self._model


def render_slice(lib: Any, loaded: _Loaded, request: dict[str, Any], meta: dict[str, Any]) -> bytes:
    """One slice frame: the deepest cells and the layers the request asks for."""
    from pyvised_protocol.slices import encode_slice

    from .weightwindows import lower_bounds, slice_points

    start = time.perf_counter()
    kwargs = _slice_kwargs(request)
    ids, properties = lib.slice_data(**kwargs, include_properties=request["properties"])
    level_ids = None
    if request.get("level") is not None:
        level_ids, _ = lib.slice_data(**kwargs, level=request["level"], include_properties=False)
    ww_lower = None
    layer = request.get("weight_windows")
    if layer is not None:
        points = slice_points(
            request["basis"], kwargs["origin"], request["width"], request["pixels"]
        )
        values = lower_bounds(loaded.windows, points, layer["particle"], layer["energy"])
        ww_lower = values.reshape(ids.shape[0], ids.shape[1])
    meta["render_seconds"] = time.perf_counter() - start
    return encode_slice(ids, properties, meta, level_ids=level_ids, ww_lower=ww_lower)


def inspect(lib: Any, loaded: _Loaded, point: tuple[float, float, float]) -> dict[str, Any]:
    from .inspection import inspect_point

    try:
        _cell, instance = lib.find_cell(point)
    except Exception:  # GeometryError: no cell there
        instance = None
    return inspect_point(loaded.model, point, instance).model_dump()


def worker_main(conn: multiprocessing.connection.Connection, log_path: str) -> None:
    """Body of a render worker: (op, model folder, ...) messages in, (status, payload) out."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)

    import openmc.lib  # pyright: ignore[reportMissingImports]  (container only)

    loaded: _Loaded | None = None
    while True:
        try:
            message = conn.recv()
        except EOFError:
            break
        if message is None:
            break
        op, folder, *args = message
        if loaded is None or loaded.folder != folder:
            try:
                if loaded is not None:
                    openmc.lib.finalize()
                    loaded = None
                openmc.lib.init(["-p", folder], output=False)
            except Exception as exc:
                conn.send(("error", f"{type(exc).__name__}: {exc}"))
                conn.close()
                os._exit(1)  # a half-initialised library cannot be reused
            loaded = _Loaded(folder)
        try:
            if op == "load":
                conn.send(("ok", None))
            elif op == "slice":
                request, meta = args
                conn.send(("ok", render_slice(openmc.lib, loaded, request, meta)))
            elif op == "inspect":
                (point,) = args
                conn.send(("ok", inspect(openmc.lib, loaded, point)))
            else:
                conn.send(("error", f"unknown operation {op!r}"))
        except Exception as exc:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
    if loaded is not None:
        openmc.lib.finalize()


class _Worker:
    def __init__(self, index: int, context: Any, log_dir: Path) -> None:
        self.index = index
        self.busy = False
        self.loaded: str | None = None
        self._context = context
        self._log = log_dir / f"render-{index}.log"
        self._process: Any = None
        self._conn: multiprocessing.connection.Connection | None = None

    def start(self) -> None:
        """Spawn the process now; it imports openmc.lib while nothing waits for it."""
        self._ensure_running()

    def _ensure_running(self) -> multiprocessing.connection.Connection:
        if self._process is None or not self._process.is_alive() or self._conn is None:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            parent, child = self._context.Pipe()
            process = self._context.Process(
                target=worker_main, args=(child, str(self._log)), daemon=True,
                name=f"pyvised-render-{self.index}",
            )  # fmt: skip
            process.start()
            child.close()
            self._process, self._conn, self.loaded = process, parent, None
            log.info("render worker %d started (pid %s)", self.index, process.pid)
        assert self._conn is not None
        return self._conn

    def call(self, op: str, folder: str, *args: Any, timeout: float = CALL_TIMEOUT) -> Any:
        conn = self._ensure_running()
        try:
            conn.send((op, folder, *args))
            if not conn.poll(timeout):
                self.stop(kill=True)
                raise RenderError("render_timeout", f"no answer from OpenMC within {timeout:.0f} s")
            status, payload = conn.recv()
        except (EOFError, OSError, BrokenPipeError) as exc:
            process = self._process
            if process is not None:
                process.join(2.0)
            code = process.exitcode if process is not None else None
            self._process, self._conn, self.loaded = None, None, None
            detail = openmc_error(self._log) or f"the render worker exited with code {code}"
            log.warning("render worker %d died (exit code %s): %s", self.index, code, detail)
            raise RenderError("openmc_error", detail) from exc
        if status != "ok":
            self.loaded = None  # the worker may have exited; at worst it loads the model again
            raise RenderError("openmc_error", str(payload))
        self.loaded = folder
        return payload

    def stop(self, *, kill: bool = False) -> None:
        process, conn = self._process, self._conn
        self._process, self._conn, self.loaded = None, None, None
        if process is None:
            return
        if not kill and conn is not None:
            try:
                conn.send(None)
                process.join(2.0)
            except (OSError, BrokenPipeError):
                pass
        if process.is_alive():
            process.kill()
            process.join(2.0)


class RenderPool:
    """A fixed number of render workers; a request prefers a worker that has its model."""

    def __init__(self, size: int, log_dir: Path) -> None:
        context = multiprocessing.get_context("spawn")
        self._workers = [_Worker(i, context, log_dir) for i in range(max(1, size))]
        self._available = threading.Condition()

    def start(self) -> None:
        for worker in self._workers:
            worker.start()

    def _acquire(self, folder: str) -> _Worker:
        with self._available:
            while True:
                idle = [w for w in self._workers if not w.busy]
                if idle:
                    worker = next((w for w in idle if w.loaded == folder), idle[0])
                    worker.busy = True
                    return worker
                self._available.wait()

    def _acquire_idle_without(self, folder: str) -> _Worker | None:
        with self._available:
            for worker in self._workers:
                if not worker.busy and worker.loaded != folder:
                    worker.busy = True
                    return worker
        return None

    def _release(self, worker: _Worker) -> None:
        with self._available:
            worker.busy = False
            self._available.notify()

    def _call(self, op: str, folder: Path, *args: Any) -> Any:
        worker = self._acquire(str(folder))
        try:
            return worker.call(op, str(folder), *args)
        finally:
            self._release(worker)

    def preload(self, folder: Path) -> None:
        """Load the model in a worker, which checks that OpenMC accepts it; the other idle
        workers load it in the background, so the first slices of both views are quick."""
        self._call("load", folder)
        for _ in self._workers[1:]:
            threading.Thread(target=self._warm, args=(str(folder),), daemon=True).start()

    def _warm(self, folder: str) -> None:
        worker = self._acquire_idle_without(folder)
        if worker is None:
            return
        try:
            with contextlib.suppress(RenderError):
                worker.call("load", folder)
        finally:
            self._release(worker)

    def render(self, folder: Path, request: SliceRequest, meta: dict[str, Any]) -> bytes:
        return self._call("slice", folder, request.model_dump(), meta)

    def inspect(self, folder: Path, point: tuple[float, float, float]) -> InspectResult:
        return InspectResult.model_validate(self._call("inspect", folder, point))

    def shutdown(self) -> None:
        for worker in self._workers:
            worker.stop()
