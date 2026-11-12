"""휴대폰 화면 중계 상태 조회와 연결번호 발급 로직 (계약 4.13). HTTP 는 test_phone_link_api.py."""

import json
import os
import subprocess

import pytest

from dom0_manager.phone_link import PhoneLink

CONFIG = {"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "10.117.188.86", "port": 8084,
          "ui_origin": "http://192.168.77.8:8086"}
ENDPOINT = {"url": "http://10.117.188.25:8084/display/mobile", "interface": "usb0", "pairing_ttl_s": 120,
            "session_ttl_s": 28800}
ADDR = [{"ifname": "usb0", "addr_info": [
    {"family": "inet", "local": "10.117.188.25", "prefixlen": 24, "scope": "global"},
    {"family": "inet", "local": "169.254.3.3", "prefixlen": 16, "scope": "link"}]}]
ROUTES = [{"dst": "default", "gateway": "10.117.188.86", "dev": "usb0"}, {"dst": "10.117.188.0/24", "dev": "usb0"}]


class Board:
    """systemctl·ip 를 흉내 낸다. kill 을 받으면 중계처럼 pairing-code 를 새로 쓴다."""

    def __init__(self, runtime, *, state="active", addr=ADDR, routes=ROUTES, rotate=True, kill_rc=0):
        self.runtime, self.state, self.addr, self.routes = runtime, state, addr, routes
        self.rotate, self.kill_rc, self.calls, self.issued = rotate, kill_rc, [], 0

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out, rc = "", 0
        if argv[:2] == ["systemctl", "is-active"]:
            out = self.state + "\n"
        elif argv[:2] == ["systemctl", "kill"]:
            rc = self.kill_rc
            if self.rotate and rc == 0:
                self.issued += 1
                temporary = self.runtime / "pairing-code.new"
                temporary.write_text(f"{123456 + self.issued:06d}\n")
                temporary.replace(self.runtime / "pairing-code")
        elif argv[:4] == ["ip", "-j", "-4", "addr"]:
            if self.addr is None:
                return subprocess.CompletedProcess(argv, 1, "", 'Device "usb0" does not exist.\n')
            out = json.dumps(self.addr)
        elif argv[:4] == ["ip", "-j", "-4", "route"]:
            out = json.dumps(self.routes)
        return subprocess.CompletedProcess(argv, rc, out, "")


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    (path / "endpoint.json").write_text(json.dumps(ENDPOINT))
    return path


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "display-mobile.json"
    path.write_text(json.dumps(CONFIG))
    return path


def link(runtime, config, board, now=None):
    clock = (lambda: now) if now is not None else __import__("time").time
    return PhoneLink(str(runtime), str(config), run=board, clock=clock, sleep=lambda _: None)


def test_listening_status_has_url_usb_and_no_code_value(runtime, config):
    (runtime / "pairing-code").write_text("654321\n")
    status = link(runtime, config, Board(runtime)).status()
    assert status["relay"] == "listening" and status["configured"] is True
    assert status["url"] == ENDPOINT["url"]
    assert status["usb"] == {"interface": "usb0", "present": True, "ipv4": ["10.117.188.25/24"], "gateway": "10.117.188.86"}
    assert status["configured_phone_ipv4"] == "10.117.188.86" and status["subnet_mismatch"] is False
    assert status["pairing"]["active"] is True and status["pairing"]["expires_at"]
    assert "654321" not in json.dumps(status)


def test_expired_code_file_is_not_active(runtime, config):
    code = runtime / "pairing-code"
    code.write_text("654321\n")
    os.utime(code, (1_000_000, 1_000_000))
    status = link(runtime, config, Board(runtime), now=1_000_000 + 121).status()
    assert status["pairing"] == {"active": False, "expires_at": None}


def test_running_without_endpoint_is_waiting_for_link(runtime, config):
    (runtime / "endpoint.json").unlink()
    status = link(runtime, config, Board(runtime, addr=None)).status()
    assert status["relay"] == "waiting_link" and status["url"] is None
    assert status["usb"] == {"interface": "usb0", "present": False, "ipv4": [], "gateway": None}
    assert status["subnet_mismatch"] is None


def test_stopped_relay_hides_a_stale_endpoint(runtime, config):
    status = link(runtime, config, Board(runtime, state="inactive")).status()
    assert status["relay"] == "stopped" and status["url"] is None


def test_phone_moved_to_another_subnet_is_reported(runtime, config):
    moved = [{"ifname": "usb0", "addr_info": [{"family": "inet", "local": "10.55.3.20", "prefixlen": 24, "scope": "global"}]}]
    status = link(runtime, config, Board(runtime, state="activating", addr=moved,
                                         routes=[{"dst": "default", "gateway": "10.55.3.1"}])).status()
    assert status["relay"] == "starting"
    assert status["subnet_mismatch"] is True and status["usb"]["gateway"] == "10.55.3.1"


def test_missing_config_is_not_configured(runtime, tmp_path):
    status = link(runtime, tmp_path / "absent.json", Board(runtime, state="inactive")).status()
    assert status["configured"] is False and status["usb"]["interface"] == "usb0"
    assert status["configured_phone_ipv4"] is None


def test_issue_signals_main_process_and_returns_the_new_code(runtime, config):
    (runtime / "pairing-code").write_text("000001\n")
    board = Board(runtime)
    issued = link(runtime, config, board).issue_code()
    assert issued["code"] == "123457" and issued["expires_at"]
    assert ["systemctl", "kill", "--kill-whom=main", "--signal=USR1", "xenova-display-mobile.service"] in board.calls


def test_issue_requires_a_listening_relay(runtime, config):
    from dom0_manager.errors import InvalidState
    board = Board(runtime, state="inactive")
    with pytest.raises(InvalidState):
        link(runtime, config, board).issue_code()
    assert not any(call[:2] == ["systemctl", "kill"] for call in board.calls)


def test_issue_times_out_when_relay_does_not_rotate(runtime, config):
    from dom0_manager.errors import PlatformUnavailable
    ticks = iter(range(100))
    phone = PhoneLink(str(runtime), str(config), run=Board(runtime, rotate=False), clock=lambda: next(ticks),
                      sleep=lambda _: None)
    with pytest.raises(PlatformUnavailable):
        phone.issue_code()


def test_issue_fails_fast_when_signal_is_refused(runtime, config):
    from dom0_manager.errors import PlatformUnavailable
    with pytest.raises(PlatformUnavailable):
        link(runtime, config, Board(runtime, kill_rc=1)).issue_code()


def test_missing_systemctl_is_platform_unavailable(runtime, config):
    from dom0_manager.errors import PlatformUnavailable

    def absent(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(PlatformUnavailable):
        link(runtime, config, absent).status()
