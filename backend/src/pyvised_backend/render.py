"""Geometry slices from long-lived worker processes around ``openmc.lib``.

OpenMC calls ``MPI_Abort`` on bad input, which kills the whole process, so the
library lives only in spawned workers. Each worker keeps one model loaded in
plot mode (``openmc.lib.init(['-p', folder])``; no nuclear data needed) and
answers slice requests with ready-encoded frames. When a worker dies, the
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

from pyvised_protocol.schemas import SliceRequest

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
        "level": request["level"],
        "include_properties": request["properties"],
    }


def worker_main(conn: multiprocessing.connection.Connection, log_path: str) -> None:
    """Body of a render worker: (op, model folder, ...) messages in, (status, payload) out."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)

    import openmc.lib  # pyright: ignore[reportMissingImports]  (container only)

    from pyvised_protocol.slices import encode_slice

    loaded: str | None = None
    while True:
        try:
            message = conn.recv()
        except EOFError:
            break
        if message is None:
            break
        op, folder, *args = message
        if loaded != folder:
            try:
                if loaded is not None:
                    openmc.lib.finalize()
                    loaded = None
                openmc.lib.init(["-p", folder], output=False)
            except Exception as exc:
                conn.send(("error", f"{type(exc).__name__}: {exc}"))
                conn.close()
                os._exit(1)  # a half-initialised library cannot be reused
            loaded = folder
        try:
            if op == "load":
                conn.send(("ok", None))
            elif op == "slice":
                request, meta = args
                start = time.perf_counter()
                ids, properties = openmc.lib.slice_data(**_slice_kwargs(request))
                meta["render_seconds"] = time.perf_counter() - start
                conn.send(("ok", encode_slice(ids, properties, meta)))
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

    def shutdown(self) -> None:
        for worker in self._workers:
            worker.stop()
