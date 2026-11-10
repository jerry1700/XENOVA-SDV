"""349 Plan A for the phone: dom0 relays the display domU's web app (VM3 guest_app) over USB."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from xenova_display.mobile_access import upstream_for
from xenova_display.mobile_network import resolve
from xenova_display.mobile_upstream import FeedUnavailable, GuestUI, LocalFeed, MAX_BODY

TOKEN = "test-only-internal-token"
VM3 = "192.168.77.6"


@contextmanager
def guest_app(status=200, body=b"vm3", content_type="text/html"):
    """A local stand-in for VM3 guest_app; `connect` maps the domU address onto it."""
    calls, opened = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Set-Cookie", "not-for-phone=vm3")
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def log_message(self, *args): pass
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    def connect(host, port, timeout):
        opened.append((host, port, timeout))
        return http.client.HTTPConnection("127.0.0.1", instance.server_port, timeout=timeout)
    try: yield connect, calls, opened
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(2)


@pytest.mark.parametrize("path", ["/display/mobile", "/api/v1/display/presentation", "/assets/index-test.js",
                                  "/assets/track-01-a1b2.mp3"])
def test_phone_reads_come_from_the_display_domu_without_a_dom0_token(path):
    ui = GuestUI.from_origin(f"http://{VM3}:8086")
    with guest_app() as (connect, calls, opened):
        response = ui.get(path, connect=connect)
    assert (response.status, response.body, response.content_type) == (200, b"vm3", "text/html")
    assert opened == [(VM3, 8086, 3)]
    assert calls[0][0] == path
    # guest_app admits dom0 by its bridge address; no dom0 credential or browser cookie leaves dom0.
    assert "Authorization" not in calls[0][1] and "Cookie" not in calls[0][1]
    assert not hasattr(response, "headers")


@pytest.mark.parametrize("path", ["/api/v1/display/session", "/api/v1/vms", "/display/monitor-1",
                                  "http://outside.invalid/", "/assets/../secret.js", "/assets/x.js?x=1",
                                  "/assets/x.mp3?t=1", "/assets/x.wav", "/assets/x.mp4", "/assets/x.m4a"])
def test_control_and_arbitrary_paths_never_reach_the_domu(path):
    def forbidden(*a, **k): raise AssertionError("network must not be opened")
    with pytest.raises(ValueError):
        GuestUI(VM3).get(path, connect=forbidden)


@pytest.mark.parametrize("status,code", [(403, "DISPLAY_FEED_AUTHENTICATION_FAILED"),
                                         (401, "DISPLAY_FEED_AUTHENTICATION_FAILED"),
                                         (503, "DISPLAY_FEED_UNAVAILABLE"), (302, "DISPLAY_FEED_UNAVAILABLE")])
def test_domu_failures_are_fixed_codes(status, code):
    with guest_app(status=status, body=b"internal detail") as (connect, _, _):
        with pytest.raises(FeedUnavailable) as error:
            GuestUI(VM3).get("/display/mobile", connect=connect)
    assert error.value.code == code and "internal" not in str(error.value)


def test_domu_oversized_response_and_unreachable_domu_are_rejected():
    with guest_app(body=b"x" * (MAX_BODY + 1)) as (connect, _, _):
        with pytest.raises(FeedUnavailable, match="TOO_LARGE"):
            GuestUI(VM3).get("/assets/big.js", connect=connect)
    def down(host, port, timeout):
        raise ConnectionRefusedError(host)
    with pytest.raises(FeedUnavailable) as error:
        GuestUI(VM3).get("/display/mobile", connect=down)
    assert VM3 not in str(error.value)


@pytest.mark.parametrize("origin", [
    "https://192.168.77.6:8086", "http://192.168.77.6", "http://192.168.77.6:8086/display",
    "http://user:pw@192.168.77.6:8086", "http://192.168.77.6:8086?x=1", "http://8.8.8.8:8086",
    "http://127.0.0.1:8086", "http://0.0.0.0:8086", "http://169.254.1.1:8086", "http://vm3.local:8086",
    "http://192.168.77.6:99999", "", None, 8086, "http://" + "1" * 80])
def test_only_a_private_domu_http_origin_is_accepted(origin):
    with pytest.raises(ValueError):
        GuestUI.from_origin(origin)


def test_origin_with_trailing_slash_is_the_same_domu():
    assert GuestUI.from_origin("http://192.168.77.6:8086/") == GuestUI(VM3, 8086)


def test_upstream_is_the_domu_only_when_the_root_config_names_it():
    config = {"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "10.104.83.148", "port": 8084}
    assert isinstance(upstream_for(config, TOKEN), LocalFeed)
    assert upstream_for({**config, "ui_origin": f"http://{VM3}:8086"}, TOKEN) == GuestUI(VM3, 8086)
    with pytest.raises(ValueError):
        upstream_for({**config, "ui_origin": "http://8.8.8.8:8086"}, TOKEN)
    with pytest.raises(ValueError):  # the loopback feed still needs its token
        upstream_for(config, "")


def test_phone_link_check_accepts_ui_origin_but_no_other_extra_key(tmp_path):
    subsystem = tmp_path / "bus/usb"
    subsystem.mkdir(parents=True)
    device = tmp_path / "net/usb0/device"
    device.mkdir(parents=True)
    (device / "subsystem").symlink_to(subsystem)
    info = [{"ifname": "usb0", "ifindex": 7, "flags": ["UP"], "addr_info": [
        {"family": "inet", "scope": "global", "local": "192.168.42.2", "prefixlen": 24}]}]
    run = lambda argv, **kw: SimpleNamespace(stdout=json.dumps(info))
    config = {"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "192.168.42.129", "port": 8084}
    plain = resolve(config, sysfs=tmp_path / "net", run=run)
    assert resolve({**config, "ui_origin": f"http://{VM3}:8086"}, sysfs=tmp_path / "net", run=run) == plain
    with pytest.raises(ValueError):
        resolve({**config, "upstream": "anything"}, sysfs=tmp_path / "net", run=run)
