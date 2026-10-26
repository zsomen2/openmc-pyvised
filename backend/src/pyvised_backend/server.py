"""The HTTP service (FastAPI).

The API process never imports ``openmc.lib``: OpenMC aborts the whole process
on bad input, so everything that touches the library runs in a subprocess or a
worker process.
"""

import asyncio
import platform
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from pyvised_protocol import API_PREFIX, PROTOCOL_VERSION, frames
from pyvised_protocol.runs import LogChunk, RunInfo, RunRequest, RunResults, RunStatus
from pyvised_protocol.schemas import (
    ErrorInfo,
    HealthInfo,
    InspectResult,
    LibraryInfo,
    ModelInfo,
    OpenModelRequest,
    SelfTestResult,
    SelfTestStep,
    SliceRequest,
)

from . import __version__, libraries
from .config import Config
from .errors import ServiceError
from .events import EventHub
from .models import ModelService, OpenedModel
from .render import RenderPool
from .runs import RunManager
from .selftest import RESULT_MARKER

CAPABILITIES = [
    "selftest",
    "models",
    "slices",
    "levels",
    "weight_windows",
    "inspect",
    "runs",
    "events",
]
SELFTEST_TIMEOUT = 600.0


class Renderer(Protocol):
    def start(self) -> None: ...
    def preload(self, folder: Any) -> None: ...
    def render(self, folder: Any, request: SliceRequest, meta: dict[str, Any]) -> bytes: ...
    def inspect(self, folder: Any, point: tuple[float, float, float]) -> InspectResult: ...
    def shutdown(self) -> None: ...


def _openmc_version() -> str | None:
    try:
        return version("openmc")
    except PackageNotFoundError:
        return None


def _token_dependency(token: str):
    expected = f"Bearer {token}".encode()

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization is None or not secrets.compare_digest(authorization.encode(), expected):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="missing or wrong token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token


def run_selftest_subprocess(timeout: float = SELFTEST_TIMEOUT) -> SelfTestResult:
    """Run `python -m pyvised_backend selftest` and parse its result line."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyvised_backend", "selftest"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        step = SelfTestStep(name="selftest", ok=False, seconds=timeout, detail="timed out")
        return SelfTestResult(ok=False, steps=[step])
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return SelfTestResult.model_validate_json(line[len(RESULT_MARKER) :])
    # no result line: the process died, typically an OpenMC abort
    output = (proc.stderr or proc.stdout).strip()[-2000:]
    step = SelfTestStep(
        name="selftest", ok=False, seconds=0.0, detail=f"exit code {proc.returncode}\n{output}"
    )
    return SelfTestResult(ok=False, steps=[step])


def create_app(
    config: Config | None = None,
    *,
    models: ModelService | None = None,
    renderer: Renderer | None = None,
    runs: RunManager | None = None,
    hub: EventHub | None = None,
) -> FastAPI:
    config = config or Config.from_env()
    models = models or ModelService(config)
    renderer = renderer or RenderPool(config.render_workers, config.cache_dir / "logs")
    hub = hub or EventHub()
    runs = runs or RunManager(config, models, hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.bind(asyncio.get_running_loop())
        renderer.start()
        yield
        hub.close()
        runs.shutdown()
        renderer.shutdown()

    app = FastAPI(title="PyVised backend", version=__version__, lifespan=lifespan)
    public = APIRouter(prefix=API_PREFIX)
    private = APIRouter(prefix=API_PREFIX, dependencies=[Depends(_token_dependency(config.token))])

    @app.exception_handler(ServiceError)
    def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        body = ErrorInfo(error=exc.error, detail=exc.detail)
        return JSONResponse(status_code=exc.status, content=body.model_dump())

    @public.get("/health")
    def health() -> HealthInfo:
        return HealthInfo(
            protocol_version=PROTOCOL_VERSION,
            backend_version=__version__,
            openmc_version=_openmc_version(),
            python_version=platform.python_version(),
            cpu_count=config.cpus,
            capabilities=CAPABILITIES,
            libraries=libraries.discover(config.cross_sections, config.custom_data),
        )

    @private.post("/selftest")
    def selftest() -> SelfTestResult:
        return run_selftest_subprocess()

    @private.post("/models/open")
    def open_model(request: OpenModelRequest) -> ModelInfo:
        model: OpenedModel = models.open(request)
        renderer.preload(model.directory)
        return model.info

    @private.get("/models/{model_id}")
    def model_info(model_id: str) -> ModelInfo:
        return models.get(model_id).info

    @private.post(
        "/models/{model_id}/slice",
        response_class=Response,
        responses={200: {"content": {frames.CONTENT_TYPE: {}}}},
    )
    def model_slice(model_id: str, request: SliceRequest) -> Response:
        model = models.get(model_id)
        meta = {"model_id": model_id, **request.model_dump()}
        data = renderer.render(model.directory, request, meta)
        return Response(content=data, media_type=frames.CONTENT_TYPE)

    @private.get("/models/{model_id}/inspect")
    def model_inspect(model_id: str, x: float, y: float, z: float) -> InspectResult:
        model = models.get(model_id)
        return renderer.inspect(model.directory, (x, y, z))

    @private.get("/libraries")
    def library_list() -> list[LibraryInfo]:
        return libraries.discover(config.cross_sections, config.custom_data)

    # -- runs ----------------------------------------------------------------------------

    @private.post("/runs", status_code=status.HTTP_201_CREATED)
    def run_start(request: RunRequest) -> RunInfo:
        return runs.start(request)

    @private.get("/runs")
    def run_list(model: str) -> list[RunInfo]:
        return runs.list(model)

    @private.get("/runs/active")
    def run_active() -> RunStatus | None:
        info = runs.active
        return runs.status(info.id) if info is not None else None

    @private.get("/runs/{run_id}")
    def run_status(run_id: str) -> RunStatus:
        return runs.status(run_id)

    @private.post("/runs/{run_id}/stop")
    def run_stop(run_id: str) -> RunInfo:
        return runs.stop(run_id)

    @private.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
    def run_delete(run_id: str) -> Response:
        runs.delete(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @private.get("/runs/{run_id}/log")
    def run_log(run_id: str, offset: int = 0) -> LogChunk:
        return runs.log_text(run_id, offset)

    @private.get("/runs/{run_id}/results")
    def run_results(run_id: str) -> RunResults:
        return runs.results(run_id)

    @private.get(
        "/runs/{run_id}/tallies/{tally_id}",
        response_class=Response,
        responses={200: {"content": {frames.CONTENT_TYPE: {}}}},
    )
    def run_tally(run_id: str, tally_id: int) -> Response:
        return Response(content=runs.tally(run_id, tally_id), media_type=frames.CONTENT_TYPE)

    @private.get("/events", response_class=StreamingResponse)
    async def events() -> StreamingResponse:
        return StreamingResponse(
            hub.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(public)
    app.include_router(private)
    return app
