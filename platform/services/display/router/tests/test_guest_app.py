"""413: display-domU screen app server — local files plus one read-only data path to dom0."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue

import pytest

from xenova_display.guest_app import DataSource, load_config, serve

TOKEN = "guest-data-token"
PRESENTATION = {"type": "display.presentation", "revision": 7}


@pytest.fixture
def web_root(tmp_path):
    root = tmp_path/"web"
    (root/"assets").mkdir(parents=True)
    (root/"index.html").write_text("<!doctype html><title>vm-screens</title>")
    (root/"assets"/"app-1a2b.js").write_text("console.log('screen')")
    (root/"assets"/"track-01-a1b2.mp3").write_bytes(b"ID3\x04" + bytes(64))
    (root/"assets"/"clip.wav").write_bytes(b"RIFF" + bytes(16))
    (root/"secret.txt").write_text("not an asset")
    return root


def write_config(tmp_path, **changes):
    value = {"web_root": str(tmp_path/"web"), "data_host": "192.168.77.1",
             "data_token_file": "/etc/xenova/guest-display/guest-data.token", **changes}
    path = tmp_path/"domu.json"
    path.write_text(json.dumps(value))
    return path


def test_config_defaults_and_validation(tmp_path, web_root):
    c = load_config(write_config(tmp_path))
    assert (c["app_port"], c["data_port"], c["page"], c["display"], c["width"], c["height"]) == \
        (8086, 8085, "/display/mobile", ":0", 1280, 720)
    for bad in [dict(web_root="relative"), dict(web_root=str(tmp_path)), dict(data_host="8.8.8.8"),
                dict(data_host="0.0.0.0"), dict(data_host="127.0.0.1"), dict(page="/admin"),
                dict(display="remote:0"), dict(width=4000), dict(app_port=0), dict(extra=1),
                dict(chromium="chromium"), dict(data_token_file="token")]:
        with pytest.raises(ValueError):
            load_config(write_config(tmp_path, **bad))
    assert load_config(write_config(tmp_path, data_host="127.0.0.1"), allow_loopback_data=True)["data_host"] == "127.0.0.1"


class FakeResponse:
    def __init__(self, status=200, content_type="application/json", body=json.dumps(PRESENTATION).encode()):
        self.status, self.content_type, self.body = status, content_type, body
    def getheader(self, name, default=None):
        return self.content_type if name.lower() == "content-type" else default
    def read(self, limit): return self.body[:limit]


class FakeConnection:
    def __init__(self, response=None, refuse=False):
        self.response, self.refuse, self.requests = response or FakeResponse(), refuse, []
    def __call__(self, host, port, timeout):
        if self.refuse: raise ConnectionRefusedError()
        self.target = (host, port)
        return self
    def request(self, method, path, headers): self.requests.append((method, path, headers))
    def getresponse(self): return self.response
    def close(self): pass


@pytest.mark.parametrize("connection,code", [
    (FakeConnection(FakeResponse(status=401)), "GUEST_DATA_AUTH_FAILED"),
    (FakeConnection(FakeResponse(status=503)), "DISPLAY_DATA_UNAVAILABLE"),
    (FakeConnection(FakeResponse(content_type="text/html")), "DISPLAY_DATA_UNAVAILABLE"),
    (FakeConnection(FakeResponse(body=b"x"*(2*1024*1024+1))), "DISPLAY_DATA_UNAVAILABLE"),
    (FakeConnection(refuse=True), "DISPLAY_DATA_UNAVAILABLE"),
])
def test_data_failures_become_fixed_codes(connection, code):
    status, error, body = DataSource("192.168.77.1", 8085, TOKEN, connect=connection).get()
    assert (status, error, body) == (503, {"code": code}, None)


def test_data_read_uses_fixed_path_and_token():
    connection = FakeConnection()
    status, error, body = DataSource("192.168.77.1", 8085, TOKEN, connect=connection).get()
    assert status == 200 and json.loads(body) == PRESENTATION
    assert connection.target == ("192.168.77.1", 8085)
    assert connection.requests == [("GET", "/api/v1/display/presentation", {"Authorization": f"Bearer {TOKEN}"})]


@pytest.fixture
def app(web_root):
    config = {"web_root": web_root.resolve(), "app_port": 0, "data_host": "192.168.77.1", "data_port": 8085}
    server = serve(config, TOKEN, connect=FakeConnection())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown(); server.server_close()


def get(address, path, method="GET"):
    connection = http.client.HTTPConnection(*address, timeout=3)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), body


def test_server_binds_loopback_and_serves_pages_assets_and_data(app):
    assert app[0] == "127.0.0.1"
    for page in ("/display/mobile", "/display/combined", "/display/monitor-1"):
        status, kind, body = get(app, page)
        assert status == 200 and kind.startswith("text/html") and b"vm-screens" in body
    status, kind, body = get(app, "/assets/app-1a2b.js")
    assert status == 200 and kind == "text/javascript"
    status, kind, body = get(app, "/assets/track-01-a1b2.mp3")
    assert status == 200 and kind == "audio/mpeg" and body.startswith(b"ID3")
    status, kind, body = get(app, "/api/v1/display/presentation")
    assert status == 200 and kind == "application/json" and json.loads(body) == PRESENTATION
    assert TOKEN.encode() not in body


@pytest.mark.parametrize("path,method,status", [
    ("/secret.txt", "GET", 404), ("/assets/../secret.txt", "GET", 404), ("/assets/missing.js", "GET", 404),
    ("/assets/clip.wav", "GET", 404), ("/assets/missing.mp3", "GET", 404),
    ("/display/admin", "GET", 404), ("/display/mobile?token=x", "GET", 404),
    ("/api/v1/display/state", "GET", 404), ("/api/v1/display/outputs/monitor-1/ack", "POST", 405),
    ("/api/v1/display/presentation", "POST", 405),
])
def test_everything_else_is_refused(app, path, method, status):
    assert get(app, path, method)[0] == status


def test_real_socket_round_trip_to_a_dom0_stand_in(web_root, caplog):
    seen = []
    authorized = threading.Event()
    class Dom0(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if not authorized.is_set():
                self.send_error(401)
                return
            data = json.dumps(PRESENTATION).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    dom0 = ThreadingHTTPServer(("127.0.0.1", 0), Dom0)
    threading.Thread(target=dom0.serve_forever, daemon=True).start()
    config = {"web_root": web_root.resolve(), "app_port": 0, "data_host": "127.0.0.1", "data_port": dom0.server_address[1]}
    server = serve(config, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with caplog.at_level("INFO", logger="xenova_display.guest_app"):
            status, _, body = get(server.server_address, "/api/v1/display/presentation")
            assert status == 503 and json.loads(body) == {"code": "GUEST_DATA_AUTH_FAILED"}
            authorized.set()
            status, _, body = get(server.server_address, "/api/v1/display/presentation")
            assert status == 200 and json.loads(body) == PRESENTATION
            server.shutdown()
            server.requests.report()
        assert seen == [("/api/v1/display/presentation", f"Bearer {TOKEN}")] * 2
        lines = [json.loads(record.message.split("responses ", 1)[1]) for record in caplog.records]
        assert lines[-1]["counts"] == {"GUEST_DATA_AUTH_FAILED": 1, "served": 1}
        assert lines[-1]["last_result"] == "served" and lines[-1]["last_success_unix_s"] > 0
        assert TOKEN not in caplog.text
    finally:
        for s in (server, dom0): s.shutdown(); s.server_close()


@pytest.mark.skipif(not shutil.which("mosquitto"), reason="requires a local Mosquitto binary")
def test_vm_heartbeat_survives_broker_loss_and_screen_process_restart(tmp_path, web_root):
    from xenova_mqtt import Identity, XenovaClient

    identity = "850e8400-e29b-41d4-a716-446655440001"
    other = "850e8400-e29b-41d4-a716-446655440002"
    account = "interaction-" + identity
    topic = f"xenova/v1/vms/{identity}/apps/interaction/events"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        broker_port = listener.getsockname()[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        app_port = listener.getsockname()[1]
    conf = tmp_path / "mosquitto.conf"
    passwords = tmp_path / "passwd"
    acl = tmp_path / "acl"
    subprocess.run(["mosquitto_passwd", "-b", "-c", str(passwords), account, "test-publisher"], check=True)
    subprocess.run(["mosquitto_passwd", "-b", str(passwords), "observer", "test-reader"], check=True)
    rules = Path(__file__).resolve().parents[4] / "deploy/mosquitto/etc/policy.json"
    writes = json.loads(rules.read_text())["interaction_app"]["write"]
    acl.write_text(f"user {account}\n" + "".join(f"topic write {t.format(vm_id=identity)}\n" for t in writes)
                   + "user observer\ntopic read xenova/v1/#\n")
    conf.write_text(f"listener {broker_port} 127.0.0.1\nallow_anonymous false\n"
                    f"password_file {passwords}\nacl_file {acl}\n")

    @contextmanager
    def process(args, env=None):
        with subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            try:
                yield proc
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    def wait_for(check):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except OSError:
                pass
            time.sleep(0.05)
        pytest.fail("local process did not become observable")

    env = dict(os.environ, XENOVA_INTERACTION_VM_ID=identity, XENOVA_INTERACTION_BROKER_HOST="127.0.0.1",
               XENOVA_INTERACTION_BROKER_PORT=str(broker_port), XENOVA_INTERACTION_BROKER_PASSWORD="test-publisher")
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    app_conf = write_config(tmp_path, app_port=app_port, data_token_file=str(token_file))
    app_cmd = [sys.executable, "-B", "-m", "xenova_display.guest_app", "--config", str(app_conf)]
    broker_cmd = ["mosquitto", "-c", str(conf)]
    messages = Queue()
    observer = XenovaClient(Identity(source_id="observer"), port=broker_port,
                            username="observer", password="test-reader")
    app_address = ("127.0.0.1", app_port)
    with process(app_cmd, env):
        wait_for(lambda: get(app_address, "/display/mobile")[0] == 200)
        observer.subscribe("xenova/v1/vms/+/apps/interaction/events", messages.put)
        observer.start()
        try:
            with process(broker_cmd):
                first = messages.get(timeout=12)
                assert (first.topic, first.type, first.payload["state"]) == (topic, "app.heartbeat", "idle")
                writer = XenovaClient(Identity(vm_id=other), port=broker_port,
                                      username=account, password="test-publisher")
                try:
                    assert writer.start(wait_s=4)
                    writer.publish(f"xenova/v1/vms/{other}/apps/interaction/events",
                                   "app.heartbeat", {"state": "idle", "uptime_s": 0})
                    for _ in range(2):
                        assert messages.get(timeout=5).topic == topic
                finally:
                    writer.stop()
            wait_for(lambda: not observer.connected)
            assert get(app_address, "/display/mobile")[0] == 200
            while not messages.empty():
                messages.get_nowait()
            with pytest.raises(Empty):
                messages.get(timeout=1)
            with process(broker_cmd):
                resumed = messages.get(timeout=12)
                assert resumed.envelope["agent_instance_id"] == first.envelope["agent_instance_id"]
                assert resumed.envelope["seq"] > first.envelope["seq"]
        finally:
            observer.stop(flush_s=0)

    observer = XenovaClient(Identity(source_id="observer"), port=broker_port,
                            username="observer", password="test-reader")
    restarted = Queue()
    with process(broker_cmd), process(app_cmd, env):
        observer.subscribe(topic, restarted.put)
        observer.start()
        try:
            report = restarted.get(timeout=12)
            assert report.envelope["vm_id"] == identity
            assert report.envelope["boot_id"] == first.envelope["boot_id"]
            assert report.envelope["agent_instance_id"] != first.envelope["agent_instance_id"]
            assert get(app_address, "/display/mobile")[0] == 200
        finally:
            observer.stop(flush_s=0)
