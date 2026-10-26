"""HTTP client for the backend API (blocking; call it through the TaskRunner)."""

from dataclasses import dataclass

import httpx
import numpy as np

from pyvised_protocol import API_PREFIX, frames
from pyvised_protocol.runs import (
    LogChunk,
    RunInfo,
    RunRequest,
    RunResults,
    RunStatus,
    TallyResult,
)
from pyvised_protocol.schemas import (
    HealthInfo,
    InspectResult,
    LibraryInfo,
    ModelInfo,
    OpenModelRequest,
    SelfTestResult,
    SliceRequest,
)
from pyvised_protocol.slices import SliceData, decode_slice

OPEN_TIMEOUT = 300.0
"""Opening runs the model's builder script, which may take a while."""
RUN_START_TIMEOUT = OPEN_TIMEOUT + 60.0
"""Starting a run exports the model with its parameters first."""


@dataclass(frozen=True)
class TallyData:
    """A tally of a run's results: mean and σ in the tally's shape (see `TallyResult`)."""

    tally: TallyResult
    mean: np.ndarray
    std: np.ndarray
    statepoint: str = ""


class BackendError(RuntimeError):
    """A failed request; `code` and `detail` come from the backend's `ErrorInfo`, if any."""

    def __init__(self, message: str, code: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Connection:
    base_url: str
    token: str


def probe_health(base_url: str, timeout: float = 2.0) -> HealthInfo:
    """`GET /health` (no token needed); raises httpx errors while the backend starts up."""
    response = httpx.get(f"{base_url}{API_PREFIX}/health", timeout=timeout)
    response.raise_for_status()
    return HealthInfo.model_validate(response.json())


def _error_from(response: httpx.Response) -> BackendError:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        detail = str(body.get("detail", ""))
        return BackendError(f"{body['error']}: {detail}".strip(": "), body["error"], detail)
    return BackendError(f"backend error {response.status_code}: {response.text[:500]}")


class ApiClient:
    """One connection to the backend; safe to share between worker threads."""

    def __init__(self, connection: Connection, timeout: float = 15.0) -> None:
        self._http = httpx.Client(
            base_url=f"{connection.base_url}{API_PREFIX}",
            headers={"Authorization": f"Bearer {connection.token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BackendError(f"no answer from the backend: {exc}") from exc
        if response.status_code >= 400:
            raise _error_from(response)
        return response

    def health(self) -> HealthInfo:
        return HealthInfo.model_validate(self._request("GET", "/health").json())

    def selftest(self) -> SelfTestResult:
        response = self._request("POST", "/selftest", timeout=660.0)
        return SelfTestResult.model_validate(response.json())

    def open_model(self, request: OpenModelRequest) -> ModelInfo:
        response = self._request(
            "POST", "/models/open", json=request.model_dump(), timeout=OPEN_TIMEOUT
        )
        return ModelInfo.model_validate(response.json())

    def slice(self, model_id: str, request: SliceRequest) -> SliceData:
        response = self._request(
            "POST", f"/models/{model_id}/slice", json=request.model_dump(), timeout=120.0
        )
        return decode_slice(response.content)

    def inspect(self, model_id: str, point: tuple[float, float, float]) -> InspectResult:
        x, y, z = point
        response = self._request(
            "GET", f"/models/{model_id}/inspect", params={"x": x, "y": y, "z": z}, timeout=60.0
        )
        return InspectResult.model_validate(response.json())

    def libraries(self) -> list[LibraryInfo]:
        return [LibraryInfo.model_validate(x) for x in self._request("GET", "/libraries").json()]

    # -- runs ----------------------------------------------------------------------------

    def start_run(self, request: RunRequest) -> RunInfo:
        response = self._request(
            "POST", "/runs", json=request.model_dump(mode="json"), timeout=RUN_START_TIMEOUT
        )
        return RunInfo.model_validate(response.json())

    def runs(self, model: str) -> list[RunInfo]:
        """The runs of the workspace-relative model `model`, newest first."""
        response = self._request("GET", "/runs", params={"model": model}, timeout=60.0)
        return [RunInfo.model_validate(item) for item in response.json()]

    def active_run(self) -> RunStatus | None:
        data = self._request("GET", "/runs/active").json()
        return RunStatus.model_validate(data) if data is not None else None

    def run_status(self, run_id: str) -> RunStatus:
        return RunStatus.model_validate(self._request("GET", f"/runs/{run_id}").json())

    def stop_run(self, run_id: str) -> RunInfo:
        return RunInfo.model_validate(self._request("POST", f"/runs/{run_id}/stop").json())

    def delete_run(self, run_id: str) -> None:
        self._request("DELETE", f"/runs/{run_id}")

    def run_log(self, run_id: str, offset: int = 0) -> LogChunk:
        response = self._request("GET", f"/runs/{run_id}/log", params={"offset": offset})
        return LogChunk.model_validate(response.json())

    def run_results(self, run_id: str) -> RunResults:
        # an interrupted run is summarised on demand, which reads its statepoint
        response = self._request("GET", f"/runs/{run_id}/results", timeout=OPEN_TIMEOUT)
        return RunResults.model_validate(response.json())

    def tally_data(self, run_id: str, tally_id: int) -> TallyData:
        response = self._request("GET", f"/runs/{run_id}/tallies/{tally_id}", timeout=120.0)
        frame = frames.decode(response.content)
        return TallyData(
            tally=TallyResult.model_validate(frame.meta["tally"]),
            mean=frame.arrays["mean"],
            std=frame.arrays["std"],
            statepoint=str(frame.meta.get("statepoint", "")),
        )
