"""표시 모드 조회·전환 (계약 4.14). 보드의 display_mode 명령은 가짜 run 으로 흉내 낸다."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from dom0_manager.config import Settings
from dom0_manager.display_mode import COMMAND, SWITCH_TIMEOUT_S, DisplayMode
from dom0_manager.errors import DriverError, DriverTimeout, InvalidState, PlatformUnavailable
from dom0_manager.main import create_app

NOW = "2026-11-14T09:00:00Z"


def board_status(selected="hdmi", hdmi="active", mobile="inactive", feed="active", ready=True):
    return {
        "schema_version": 1, "selected": selected, "boot_default": "hdmi",
        "services": {
            "xenova-display-hdmi.service": {"LoadState": "loaded", "ActiveState": hdmi},
            "xenova-display-mobile.service": {"LoadState": "loaded", "ActiveState": mobile},
            "xenova-display-feed.service": {"LoadState": "loaded", "ActiveState": feed},
        },
        "hdmi_renderer_ready": ready, "physical_display_verified": False, "phone_connection_verified": False,
    }


class Board:
    """display_mode 명령 흉내. 부른 인자를 calls 에 남긴다."""

    def __init__(self, *, mode="hdmi", fail=None, stdout=None, raises=None):
        self.mode, self.fail, self.stdout, self.raises = mode, fail, stdout, raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs["timeout"]))
        if self.raises:
            raise self.raises
        if self.stdout is not None:
            return subprocess.CompletedProcess(argv, 1, self.stdout, "")
        action = argv[-1]
        if action != "status" and self.fail:
            return subprocess.CompletedProcess(argv, 1, json.dumps({"applied": False, "code": self.fail}) + "\n", "")
        if action != "status":
            self.mode = action
        body = board_status(self.mode, *(("inactive", "active", "active", False) if self.mode == "mobile" else ()))
        if action != "status":
            body.update(applied=True, rollback="not_needed")
        return subprocess.CompletedProcess(argv, 0, "log line\n" + json.dumps(body) + "\n", "")


def modes(board, usb=True):
    return DisplayMode(usb_present=lambda: usb, run=board, now=lambda: NOW)


def test_status_maps_board_output_to_contract():
    board = Board()
    assert modes(board).status() == {
        "mode": "hdmi", "boot_default": "hdmi",
        "services": {"hdmi": "active", "mobile": "inactive", "feed": "active"},
        "hdmi_renderer_ready": True, "observed_at": NOW,
    }
    assert board.calls[0][0] == [*COMMAND, "status"]


def test_switch_to_mobile_runs_command_with_long_timeout():
    board = Board()
    result = modes(board).switch("mobile")
    assert result["mode"] == "mobile" and result["services"]["mobile"] == "active"
    assert board.calls == [([*COMMAND, "mobile"], SWITCH_TIMEOUT_S)]


def test_switch_to_mobile_without_usb_link_is_refused_without_running():
    board = Board()
    with pytest.raises(InvalidState) as caught:
        modes(board, usb=False).switch("mobile")
    assert caught.value.platform_code == "USB_LINK_ABSENT" and caught.value.retryable is True
    assert board.calls == []


def test_switch_to_hdmi_does_not_need_usb():
    board = Board(mode="mobile")
    assert modes(board, usb=False).switch("hdmi")["mode"] == "hdmi"


@pytest.mark.parametrize("code, retryable", [
    ("DISPLAY_MODE_BUSY", True),
    ("MOBILE_ENDPOINT_NOT_READY", True),
    ("DISPLAY_MODE_FAILED_RESTORED", True),
    ("MODE_GUARD_NOT_LOADED", False),
])
def test_not_applied_codes_are_409_with_platform_code(code, retryable):
    with pytest.raises(InvalidState) as caught:
        modes(Board(fail=code)).switch("mobile")
    assert caught.value.platform_code == code and caught.value.retryable is retryable


def test_incomplete_rollback_is_500():
    with pytest.raises(DriverError) as caught:
        modes(Board(fail="ROLLBACK_INCOMPLETE_CHECK_DISPLAY_SERVICES")).switch("hdmi")
    assert type(caught.value) is DriverError and caught.value.status_code == 500


@pytest.mark.parametrize("board", [
    Board(fail="ROOT_REQUIRED"),
    Board(stdout="not json\n"),
    Board(raises=FileNotFoundError()),
])
def test_command_failures_are_503(board):
    with pytest.raises(PlatformUnavailable):
        modes(board).switch("hdmi")


def test_command_timeout_is_504():
    with pytest.raises(DriverTimeout):
        modes(Board(raises=subprocess.TimeoutExpired("x", 1))).switch("hdmi")


# ---- HTTP ----


@pytest.fixture
def api():
    app = create_app(Settings(driver="mock", log_level="WARNING", service_token="t" * 32))
    app.state.display_mode = modes(Board())
    return TestClient(app, headers={"Authorization": "Bearer " + "t" * 32})


def test_http_get_and_post(api):
    response = api.get("/api/v1/display/mode")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["mode"] == "hdmi"
    response = api.post("/api/v1/display/mode", json={"mode": "mobile"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["mode"] == "mobile"


def test_http_rejects_unknown_mode_as_400(api):
    response = api.post("/api/v1/display/mode", json={"mode": "tv"})
    assert response.status_code == 400 and response.json()["code"] == "InvalidArgument"


def test_http_refusal_body_carries_platform_code(api):
    api.app.state.display_mode = modes(Board(), usb=False)
    response = api.post("/api/v1/display/mode", json={"mode": "mobile"})
    assert response.status_code == 409
    assert response.json()["code"] == "InvalidState" and response.json()["platform_code"] == "USB_LINK_ABSENT"


def test_http_requires_service_token(api):
    assert api.get("/api/v1/display/mode", headers={"Authorization": "Bearer wrong"}).status_code == 401
