"""휴대폰 화면 중계 HTTP API (계약 4.13). 로직 테스트와 가짜 보드는 test_phone_link.py 에 있다."""

import pytest
from fastapi.testclient import TestClient

from dom0_manager.config import Settings
from dom0_manager.main import create_app
from test_phone_link import Board, config, link, runtime  # noqa: F401  (fixture 재사용)

@pytest.fixture
def api(runtime, config):
    app = create_app(Settings(driver="mock", log_level="WARNING", service_token="t" * 32))
    app.state.phone_link = link(runtime, config, Board(runtime))
    return TestClient(app, headers={"Authorization": "Bearer " + "t" * 32})


def test_http_status_and_issue_are_not_cached(api):
    response = api.get("/api/v1/display/phone-link")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["relay"] == "listening"
    response = api.post("/api/v1/display/phone-link/pairing-code")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "123457"


def test_http_issue_on_stopped_relay_is_409(api, runtime, config):
    api.app.state.phone_link = link(runtime, config, Board(runtime, state="inactive"))
    response = api.post("/api/v1/display/phone-link/pairing-code")
    assert response.status_code == 409 and response.json()["code"] == "InvalidState"


def test_http_requires_service_token(api):
    response = api.get("/api/v1/display/phone-link", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
