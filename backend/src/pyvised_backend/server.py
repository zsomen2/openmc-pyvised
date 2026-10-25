"""The HTTP service (FastAPI).

The API process never imports ``openmc.lib``: OpenMC aborts the whole process
on bad input, so everything that touches the library runs in a subprocess or a
worker process.
"""

import os
import platform
import secrets
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status

from pyvised_protocol import API_PREFIX, PROTOCOL_VERSION
from pyvised_protocol.schemas import HealthInfo, SelfTestResult, SelfTestStep

from . import __version__, libraries
from .config import Config
from .selftest import RESULT_MARKER

CAPABILITIES = ["selftest"]
SELFTEST_TIMEOUT = 600.0


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


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    app = FastAPI(title="PyVised backend", version=__version__)
    public = APIRouter(prefix=API_PREFIX)
    private = APIRouter(prefix=API_PREFIX, dependencies=[Depends(_token_dependency(config.token))])

    @public.get("/health")
    def health() -> HealthInfo:
        return HealthInfo(
            protocol_version=PROTOCOL_VERSION,
            backend_version=__version__,
            openmc_version=_openmc_version(),
            python_version=platform.python_version(),
            cpu_count=os.cpu_count() or 1,
            capabilities=CAPABILITIES,
            libraries=libraries.discover(config.cross_sections, config.custom_data),
        )

    @private.post("/selftest")
    def selftest() -> SelfTestResult:
        return run_selftest_subprocess()

    app.include_router(public)
    app.include_router(private)
    return app
