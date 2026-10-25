"""HTTP client for the backend API (blocking; call it through the TaskRunner)."""

from dataclasses import dataclass

import httpx

from pyvised_protocol import API_PREFIX
from pyvised_protocol.schemas import (
    HealthInfo,
    ModelInfo,
    OpenModelRequest,
    SelfTestResult,
    SliceRequest,
)
from pyvised_protocol.slices import SliceData, decode_slice

OPEN_TIMEOUT = 300.0
"""Opening runs the model's builder script, which may take a while."""


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
