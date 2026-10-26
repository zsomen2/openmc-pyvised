"""Simulation runs: the ``openmc`` executable in a run folder next to the model, one at a time.

Starting a run opens (exports) the model with the run's parameters and
library, copies the export to ``<model folder>/runs/<stamp>[_label]/``,
patches the run settings into the copy (`runxml`) and starts
``openmc -s <threads>`` there. A reader thread appends OpenMC's output to
`LOG_FILE`, parses it (`runlog`) and publishes progress, log text and state
changes as events, at most every `FLUSH_INTERVAL`. A statepoint is complete
once OpenMC prints the next line; complete statepoints are summarised for the
convergence plots (`results snapshot`) and older ones are deleted. Stop and
the wall-time limit end OpenMC with SIGTERM: OpenMC cannot stop gracefully,
so the run keeps its latest complete statepoint. At the end the newest
readable statepoint is summarised into `RESULTS_FILE` and the run file gets
its final state. A run continues when the client only reconnects.

While OpenMC runs, the backend touches `HEARTBEAT_FILE` every `HEARTBEAT_INTERVAL`. A run
file that still says "running" without a fresh heartbeat belongs to a backend that went
away (a container restart): it is reported as interrupted. A fresh heartbeat means
another backend on the same workspace runs it, and its files are left alone.
"""

import contextlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

from pyvised_protocol import PROTOCOL_VERSION, frames
from pyvised_protocol.runs import (
    LOG_FILE,
    RESULTS_FILE,
    RUN_FILE,
    RUNS_FOLDER,
    TALLIES_FILE,
    Event,
    LogChunk,
    RunInfo,
    RunProgress,
    RunRequest,
    RunResults,
    RunStatus,
    StopReason,
    run_id_for,
    run_path_for,
)
from pyvised_protocol.schemas import OpenModelRequest

from . import __version__
from .config import Config
from .errors import ServiceError
from .events import EventHub
from .models import ModelService, resolve_source
from .results import RESULT_MARKER, statepoint_batch
from .runlog import LogParser
from .runxml import patch_model

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.3
"""[s] between event bursts: progress and log text are coalesced in between."""
PROGRESS_KEEPALIVE = 2.0
"""[s] after which progress is sent even without news (elapsed time, ETA)."""
TERMINATE_GRACE = 10.0
"""[s] OpenMC gets after SIGTERM before it is killed."""
SUMMARY_TIMEOUT = 900.0
SNAPSHOT_TIMEOUT = 300.0
LOG_CHUNK = 256 * 1024
WARNINGS_KEPT = 30
HEARTBEAT_FILE = "pyvised_heartbeat"
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_STALE = 30.0
"""[s] after which a run without heartbeat counts as interrupted."""
_UNSAFE = re.compile(r"[^\w.-]+")


def _versions() -> dict[str, str]:
    try:
        openmc_version = version("openmc")
    except PackageNotFoundError:
        openmc_version = ""
    return {
        "openmc": openmc_version,
        "backend": __version__,
        "protocol": str(PROTOCOL_VERSION),
    }


def folder_name(created: datetime, label: str) -> str:
    stamp = created.strftime("%Y%m%d-%H%M%S")
    slug = _UNSAFE.sub("-", label.strip()).strip("-.")[:40]
    return f"{stamp}_{slug}" if slug else stamp


def complete_utf8(data: bytes) -> int:
    """Length of the longest prefix of `data` that does not end inside a UTF-8 character."""
    end = len(data)
    for back in range(1, min(4, end) + 1):
        byte = data[end - back]
        if byte & 0xC0 == 0x80:  # a continuation byte: look further back
            continue
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return end - back if back < need else end
        return end
    return end


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def alive_elsewhere(folder: Path) -> bool:
    """Whether some backend touched the run's heartbeat recently."""
    try:
        age = time.time() - (folder / HEARTBEAT_FILE).stat().st_mtime
    except OSError:
        return False
    return age < HEARTBEAT_STALE


def read_info(folder: Path) -> RunInfo:
    try:
        return RunInfo.model_validate_json((folder / RUN_FILE).read_bytes())
    except (OSError, ValueError) as exc:
        raise ServiceError("unknown_run", f"no readable run in {folder.name}", status=404) from exc


def write_info(folder: Path, info: RunInfo) -> None:
    path = folder / RUN_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(info.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(path)


@dataclass
class _Run:
    """The active run: its process, parser and what was already published."""

    info: RunInfo
    folder: Path
    process: subprocess.Popen
    parser: LogParser
    time_limit: float | None
    started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    log_file: Any = None
    log_offset: int = 0
    pending_log: list[str] = field(default_factory=list)
    pending_offset: int = 0
    sent: int = 0
    """Generations already published."""
    last_flush: float = 0.0
    last_progress: float = 0.0
    last_scalars: tuple = ()
    complete: int = 0
    """Announced statepoints known to be complete."""
    queued: int = 0
    snapshots: "queue.Queue[list[Path] | None]" = field(default_factory=queue.Queue)
    snapshot_thread: threading.Thread | None = None
    stop_reason: StopReason | None = None
    terminated_at: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    """OpenMC has exited."""
    finished: threading.Event = field(default_factory=threading.Event)
    """The run's final state is written and published."""


class RunManager:
    """Starts, watches and stops runs; reads their files for the API."""

    def __init__(
        self,
        config: Config,
        models: ModelService,
        hub: EventHub,
        *,
        command: Sequence[str] = ("openmc",),
        python: str = sys.executable,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.models = models
        self.hub = hub
        self.command = list(command)
        self.python = python
        self._clock = clock
        self._lock = threading.Lock()
        self._starting = False
        self._active: _Run | None = None
        self._summary_locks: dict[Path, threading.Lock] = {}

    # -- paths ---------------------------------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self.config.workspace.resolve()

    def folder_of(self, run_id: str) -> Path:
        try:
            relative = run_path_for(run_id)
        except ValueError as exc:
            raise ServiceError("unknown_run", str(exc), status=404) from exc
        pure = PurePosixPath(relative)
        folder = (self.workspace / pure).resolve()
        if pure.is_absolute() or not folder.is_relative_to(self.workspace):
            raise ServiceError("unknown_run", f"{relative} is not in the workspace", status=404)
        if folder.parent.name != RUNS_FOLDER or not (folder / RUN_FILE).is_file():
            raise ServiceError("unknown_run", f"{relative} is not a run folder", status=404)
        return folder

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace).as_posix()

    # -- start ---------------------------------------------------------------------------

    @property
    def active(self) -> RunInfo | None:
        run = self._active
        return run.info if run is not None and run.info.active else None

    def start(self, request: RunRequest) -> RunInfo:
        with self._lock:
            if self._starting or (self._active is not None and self._active.info.active):
                raise ServiceError(
                    "run_active", "Another simulation is running; stop it first.", status=409
                )
            self._starting = True
        try:
            return self._start(request)
        finally:
            with self._lock:
                self._starting = False

    def _start(self, request: RunRequest) -> RunInfo:
        source = resolve_source(self.config.workspace, request.path)
        library = self.models.library(request.library)
        opened = self.models.open(
            OpenModelRequest(path=request.path, library=request.library, params=request.params)
        )
        created = request.created or self._clock()
        runs = source.parent / RUNS_FOLDER
        name = folder_name(created, request.label)
        folder = runs / name
        number = 2
        while folder.exists():
            folder = runs / f"{name}-{number}"
            number += 1
        folder.mkdir(parents=True)
        try:
            for item in opened.directory.iterdir():
                if item.is_file() and item.name != "info.json":
                    shutil.copy2(item, folder / item.name)
            threads = min(request.settings.threads or self.config.cpus, self.config.cpus)
            plan, warnings = patch_model(
                folder / "model.xml", request.settings, threads, opened.info.bounding_box
            )
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        relative = self._relative(folder)
        info = RunInfo(
            id=run_id_for(relative),
            path=relative,
            model=opened.info.source,
            label=request.label,
            state="running",
            created=created,
            started=self._clock(),
            request=request.settings,
            plan=plan,
            params=opened.info.params,
            library=library.id if library else None,
            library_name=library.name if library else "",
            model_id=opened.model_id,
            versions=_versions(),
            warnings=warnings,
        )
        write_info(folder, info)
        env = dict(os.environ)
        if library is not None:
            env["OPENMC_CROSS_SECTIONS"] = library.path
        env["OMP_NUM_THREADS"] = str(threads)
        log_file = (folder / LOG_FILE).open("w", encoding="utf-8", newline="\n")
        try:
            process = subprocess.Popen(
                [*self.command, "-s", str(threads)],
                cwd=folder,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            log_file.close()
            info.state, info.stop_reason = "failed", "error"
            info.error = f"OpenMC could not be started: {exc}"
            info.finished = self._clock()
            write_info(folder, info)
            raise ServiceError("run_failed", info.error) from exc
        parser = LogParser(
            particles=plan.particles,
            batches=plan.batches,
            inactive=plan.inactive,
            generations_per_batch=plan.generations_per_batch,
        )
        parser.start()
        run = _Run(
            info=info,
            folder=folder,
            process=process,
            parser=parser,
            time_limit=request.settings.time_limit,
            log_file=log_file,
        )
        run.snapshot_thread = threading.Thread(
            target=self._snapshot_worker, args=(run,), daemon=True, name="pyvised-snapshots"
        )
        run.snapshot_thread.start()
        with self._lock:
            self._active = run
        self._publish("run.state", info)  # before any progress of the run
        threading.Thread(target=self._read, args=(run,), daemon=True, name="pyvised-run").start()
        threading.Thread(target=self._watch, args=(run,), daemon=True, name="pyvised-watch").start()
        log.info("run %s started with %d threads", relative, threads)
        return info

    # -- while running ----------------------------------------------------------------------

    def _read(self, run: _Run) -> None:
        stdout = run.process.stdout
        assert stdout is not None
        try:
            for line in stdout:
                try:
                    with run.lock:
                        run.pending_log.append(line)
                        before = len(run.parser.statepoints)
                        run.parser.feed(line)
                        # a statepoint is complete once OpenMC printed the next line
                        run.complete = max(run.complete, before)
                        self._queue_snapshots(run)
                        self._flush(run)
                except Exception:  # keep reading, or OpenMC blocks on a full pipe
                    log.exception("run %s: handling OpenMC's output failed", run.info.path)
        finally:
            run.process.wait()
            self._finish(run)

    def _watch(self, run: _Run) -> None:
        beat = 0.0
        while not run.done.wait(FLUSH_INTERVAL / 2):
            if time.monotonic() - beat >= HEARTBEAT_INTERVAL:
                beat = time.monotonic()
                with contextlib.suppress(OSError):
                    (run.folder / HEARTBEAT_FILE).touch()
            with run.lock:
                run.parser.tick()
                elapsed = time.monotonic() - run.started
                if (
                    run.time_limit is not None
                    and elapsed >= run.time_limit
                    and run.stop_reason is None
                ):
                    log.info("run %s reached its time limit of %.0f s", run.info.path, elapsed)
                    self._terminate(run, "time_limit")
                stuck = run.terminated_at is not None and (
                    time.monotonic() - run.terminated_at > TERMINATE_GRACE
                )
                if stuck:
                    with contextlib.suppress(OSError):
                        run.process.kill()
                self._flush(run)

    def _flush(self, run: _Run, *, force: bool = False) -> None:
        """Publish new log text and progress (caller holds `run.lock`)."""
        now = time.monotonic()
        if not force and now - run.last_flush < FLUSH_INTERVAL:
            return
        run.last_flush = now
        if run.pending_log:
            text = "".join(run.pending_log)
            run.pending_log.clear()
            try:  # the file and the events carry the same text at the same offsets
                run.log_file.write(text)
                run.log_file.flush()
            except (OSError, ValueError) as exc:
                log.warning("run %s: writing %s failed: %s", run.info.path, LOG_FILE, exc)
            self._publish_data("run.log", run.info.id, {"text": text, "offset": run.pending_offset})
            run.pending_offset += len(text.encode("utf-8"))
        p = run.parser.progress
        scalars = (p.batch, p.statepoint, p.trigger, p.phase)
        new = len(p.k) > run.sent
        if (
            new
            or scalars != run.last_scalars
            or force
            or now - run.last_progress > PROGRESS_KEEPALIVE
        ):
            update = self._progress(run, run.sent)
            run.sent = len(p.k)
            run.last_scalars = scalars
            run.last_progress = now
            self._publish_data("run.progress", run.info.id, update.model_dump(mode="json"))

    @staticmethod
    def _progress(run: _Run, first: int) -> RunProgress:
        p = run.parser.progress
        update = p.model_copy()
        update.first = first
        update.k = p.k[first:]
        update.entropy = p.entropy[first:] if len(p.entropy) > first else []
        update.mean = p.mean[first:]
        update.std = p.std[first:]
        return update

    def _terminate(self, run: _Run, reason: StopReason) -> None:
        """End OpenMC (SIGTERM); nothing to do once it has exited (e.g. while summarising)."""
        if run.done.is_set() or run.process.poll() is not None:
            return
        if run.stop_reason is None:
            run.stop_reason = reason
        if run.terminated_at is None:
            run.terminated_at = time.monotonic()
            with contextlib.suppress(OSError):
                run.process.send_signal(signal.SIGTERM)

    # -- statepoints -----------------------------------------------------------------------

    def _queue_snapshots(self, run: _Run) -> None:
        names = run.parser.statepoints[run.queued : run.complete]
        if names:
            run.queued = run.complete
            run.snapshots.put([run.folder / name for name in names])

    def _snapshot_worker(self, run: _Run) -> None:
        while True:
            item = run.snapshots.get()
            if item is None:
                return
            paths = list(item)
            while True:  # take everything queued meanwhile in one subprocess
                try:
                    more = run.snapshots.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    run.snapshots.put(None)
                    break
                paths.extend(more)
            done = self._snapshot(run.folder, [p for p in paths if p.is_file()])
            if done:
                newest = max(statepoint_batch(p) for p in done)
                for old in run.folder.glob("statepoint.*.h5"):
                    if 0 <= statepoint_batch(old) < newest:
                        with contextlib.suppress(OSError):
                            old.unlink()

    def _snapshot(self, folder: Path, paths: list[Path]) -> list[Path]:
        """Summarise `paths` for the convergence plots; returns the ones that worked."""
        if not paths:
            return []
        try:
            proc = subprocess.run(
                [self.python, "-m", "pyvised_backend.results", "snapshot", str(folder)]
                + [str(p) for p in paths],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SNAPSHOT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("statepoint snapshots of %s timed out", folder.name)
            return []
        if proc.returncode != 0:
            log.warning("statepoint snapshots of %s failed: %s", folder.name, _tail(proc.stderr))
            return []
        failed = {line.split(":", 1)[0] for line in proc.stderr.splitlines() if ":" in line}
        return [p for p in paths if p.name not in failed]

    # -- the end ----------------------------------------------------------------------------

    def _finish(self, run: _Run) -> None:
        """Record the end of a run; the final state is always published, whatever fails."""
        info = run.info
        try:
            self._wind_up(run)
        except Exception:
            log.exception("run %s: finishing failed", info.path)
            if info.active:
                info.state, info.stop_reason = "failed", "error"
                info.error = info.error or "PyVised could not finish the run (see the backend log)."
                info.finished = self._clock()
            with contextlib.suppress(Exception):
                write_info(run.folder, info)
        finally:
            with contextlib.suppress(OSError):
                (run.folder / HEARTBEAT_FILE).unlink()
            log.info("run %s ended: %s (%s)", info.path, info.state, info.stop_reason)
            self._publish("run.finished", info)
            run.finished.set()

    def _wind_up(self, run: _Run) -> None:
        run.done.set()
        code = run.process.returncode
        info = run.info
        with run.lock:
            run.parser.finish()
            run.parser.tick()
            if code == 0:  # every announced statepoint was written
                run.complete = len(run.parser.statepoints)
            elif run.stop_reason is not None:
                # killed: the last announced one may be half written; the summary skips it
                run.complete = len(run.parser.statepoints)
            self._queue_snapshots(run)
            self._flush(run, force=True)
            with contextlib.suppress(OSError, ValueError):
                run.log_file.close()
            info.state = "summarising"
            info.exit_code = code
            info.batches_done = run.parser.progress.batch
            info.seconds = time.monotonic() - run.started
            info.warnings = (info.warnings + run.parser.warnings)[:WARNINGS_KEPT]
        self._publish("run.state", info)
        run.snapshots.put(None)
        if run.snapshot_thread is not None:
            run.snapshot_thread.join()
        summary = self._summarise(run.folder, info.id)
        if summary is not None:
            info.has_results = True
            info.k_eff = summary.get("k_eff")
            info.k_std = summary.get("k_std")
            info.batches_done = max(info.batches_done, int(summary.get("batches") or 0))
        if code == 0:  # finished on its own, also when a Stop came too late
            info.state = "finished"
            info.stop_reason = "triggers" if run.parser.triggers_satisfied else "completed"
        elif run.stop_reason is not None:
            info.state, info.stop_reason = "stopped", run.stop_reason
        else:
            info.state, info.stop_reason = "failed", "error"
            if not run.parser.error:
                with contextlib.suppress(OSError):
                    text = (run.folder / LOG_FILE).read_text(encoding="utf-8", errors="replace")
                    info.error = _tail(text) or f"OpenMC exited with code {code}"
        if run.parser.error:
            info.error = run.parser.error
        info.finished = self._clock()
        write_info(run.folder, info)

    def _summarise(self, folder: Path, run_id: str) -> dict[str, Any] | None:
        """Write the results files from the newest statepoint; None when there is none."""
        try:
            proc = subprocess.run(
                [self.python, "-m", "pyvised_backend.results", "summary", str(folder), run_id],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SUMMARY_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("summarising %s timed out", folder.name)
            return None
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith(RESULT_MARKER):
                return json.loads(line[len(RESULT_MARKER) :])
        if proc.returncode != 3:  # 3: no statepoint, nothing to summarise
            log.warning("summarising %s failed: %s", folder.name, _tail(proc.stderr))
        return None

    # -- stop --------------------------------------------------------------------------------

    def stop(self, run_id: str) -> RunInfo:
        run = self._active
        if run is None or run.info.id != run_id or not run.info.active:
            return self._load(self.folder_of(run_id))
        with run.lock:
            self._terminate(run, "user")
        return run.info

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the active run (the service is going down)."""
        run = self._active
        if run is None or not run.info.active:
            return
        with run.lock:
            self._terminate(run, "interrupted")
        run.finished.wait(timeout)

    # -- reading -----------------------------------------------------------------------------

    def _load(self, folder: Path) -> RunInfo:
        info = read_info(folder)
        run = self._active
        if run is not None and run.info.id == info.id:
            return run.info
        if info.active and not alive_elsewhere(folder):
            # the backend went away during the run (a restart): it can no longer finish
            info.state, info.stop_reason = "stopped", "interrupted"
            info.has_results = info.has_results or any(folder.glob("statepoint.*.h5"))
            with contextlib.suppress(OSError):
                write_info(folder, info)
        return info

    def status(self, run_id: str) -> RunStatus:
        run = self._active
        if run is not None and run.info.id == run_id:
            with run.lock:
                return RunStatus(run=run.info.model_copy(), progress=self._progress(run, 0))
        return RunStatus(run=self._load(self.folder_of(run_id)))

    def list(self, model: str) -> list[RunInfo]:
        """The runs of the workspace-relative model `model`, newest first."""
        text = model.strip().replace("\\", "/")
        pure = PurePosixPath(text)
        source = (self.workspace / pure).resolve()
        if pure.is_absolute() or not source.is_relative_to(self.workspace):
            raise ServiceError(
                "outside_workspace", f"{model} is not inside the workspace", status=403
            )
        relative = source.relative_to(self.workspace).as_posix()
        runs = source.parent / RUNS_FOLDER
        found = []
        for run_file in runs.glob(f"*/{RUN_FILE}") if runs.is_dir() else []:
            try:
                info = self._load(run_file.parent)
            except ServiceError:
                continue
            if info.model == relative:
                found.append(info)
        found.sort(key=lambda i: i.created, reverse=True)
        return found

    def log_text(self, run_id: str, offset: int = 0, limit: int = LOG_CHUNK) -> LogChunk:
        folder = self.folder_of(run_id)
        path = folder / LOG_FILE
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = min(max(0, offset), size)
                f.seek(start)
                data = f.read(max(1, limit))
        except OSError:
            return LogChunk(offset=0, text="", next=0, size=0)
        data = data[: complete_utf8(data)]  # an incomplete character waits for the next call
        return LogChunk(
            offset=start,
            text=data.decode("utf-8", errors="replace"),
            next=start + len(data),
            size=size,
        )

    def results(self, run_id: str) -> RunResults:
        folder = self.folder_of(run_id)
        path = folder / RESULTS_FILE
        active = self._active
        running = active is not None and active.info.id == run_id and active.info.active
        running = running or alive_elsewhere(folder)
        if not running:
            with self._summary_lock(folder):  # one summary at a time per run
                if not path.is_file() and any(folder.glob("statepoint.*.h5")):
                    self._summarise(folder, run_id)  # e.g. interrupted by a backend restart
        try:
            return RunResults.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as exc:
            detail = "The run is still going." if running else "The run left no statepoint."
            raise ServiceError("no_results", detail, status=404) from exc

    def _summary_lock(self, folder: Path) -> threading.Lock:
        with self._lock:
            return self._summary_locks.setdefault(folder, threading.Lock())

    def tally(self, run_id: str, tally_id: int) -> bytes:
        results = self.results(run_id)
        tally = next((t for t in results.tallies if t.id == tally_id), None)
        if tally is None:
            raise ServiceError("unknown_tally", f"the run has no tally {tally_id}", status=404)
        import numpy as np

        path = self.folder_of(run_id) / TALLIES_FILE
        try:
            with np.load(path) as data:
                mean, std = data[f"mean_{tally_id}"], data[f"std_{tally_id}"]
        except (OSError, KeyError, ValueError) as exc:
            raise ServiceError("no_results", f"{TALLIES_FILE}: {exc}", status=404) from exc
        meta = {"run_id": run_id, "statepoint": results.statepoint, "tally": tally.model_dump()}
        return frames.encode({"mean": mean, "std": std}, meta)

    def delete(self, run_id: str) -> None:
        folder = self.folder_of(run_id)
        active = self._active
        if alive_elsewhere(folder):
            raise ServiceError("run_active", "Stop the run before deleting it.", status=409)
        if active is not None and active.info.id == run_id:
            if active.info.active:
                raise ServiceError("run_active", "Stop the run before deleting it.", status=409)
            with self._lock:
                self._active = None
        shutil.rmtree(folder)

    # -- events --------------------------------------------------------------------------------

    def _publish(self, kind: str, info: RunInfo) -> None:
        self._publish_data(kind, info.id, info.model_dump(mode="json"))

    def _publish_data(self, kind: str, run_id: str, data: dict[str, Any]) -> None:
        self.hub.publish(Event(type=kind, run_id=run_id, data=data))
