"""HTTP client for the backend API (blocking; call it through the TaskRunner)."""

from dataclasses import dataclass

import httpx

from pyvised_protocol import API_PREFIX
from pyvised_protocol.schemas import HealthInfo, SelfTestResult


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class Connection:
    base_url: str
    token: str


def probe_health(base_url: str, timeout: float = 2.0) -> HealthInfo:
    """`GET /health` (no token needed); raises httpx errors while the backend starts up."""
    response = httpx.get(f"{base_url}{API_PREFIX}/health", timeout=timeout)
    response.raise_for_status()
    return HealthInfo.model_validate(response.json())


class ApiClient:
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
            raise BackendError(f"backend error {response.status_code}: {response.text[:500]}")
        return response

    def health(self) -> HealthInfo:
        return HealthInfo.model_validate(self._request("GET", "/health").json())

    def selftest(self) -> SelfTestResult:
        response = self._request("POST", "/selftest", timeout=660.0)
        return SelfTestResult.model_validate(response.json())
