from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyvised_backend import server
from pyvised_backend.config import Config
from pyvised_protocol import API_PREFIX, PROTOCOL_VERSION
from pyvised_protocol.schemas import HealthInfo, SelfTestResult, SelfTestStep

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = Config(
        token=TOKEN, workspace=tmp_path, cross_sections=None, custom_data=tmp_path / "custom"
    )
    return TestClient(server.create_app(config))


def test_health_needs_no_token(client: TestClient):
    response = client.get(f"{API_PREFIX}/health")
    assert response.status_code == 200
    health = HealthInfo.model_validate(response.json())
    assert health.protocol_version == PROTOCOL_VERSION
    assert "selftest" in health.capabilities


@pytest.mark.parametrize("header", [None, "Bearer wrong", TOKEN])
def test_selftest_rejects_missing_or_wrong_token(client: TestClient, header: str | None):
    headers = {"Authorization": header} if header else {}
    response = client.post(f"{API_PREFIX}/selftest", headers=headers)
    assert response.status_code == 401


def test_selftest_with_token(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    fake = SelfTestResult(ok=True, steps=[SelfTestStep(name="build", ok=True, seconds=0.1)])
    monkeypatch.setattr(server, "run_selftest_subprocess", lambda: fake)
    response = client.post(f"{API_PREFIX}/selftest", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert SelfTestResult.model_validate(response.json()) == fake
