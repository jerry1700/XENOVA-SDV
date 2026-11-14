import hashlib
import json
import stat
from types import SimpleNamespace

import pytest

from xenova_display import board_setup
from xenova_display.outputs import RuntimeConfig

EDID = bytes(range(128))


@pytest.fixture
def board(tmp_path):
    etc, sysfs = tmp_path / "etc", tmp_path / "drm"
    connector = sysfs / "card0-HDMI-A-1"
    connector.mkdir(parents=True)
    (connector / "status").write_text("connected\n")
    (connector / "edid").write_bytes(EDID)
    (sysfs / "card0-HDMI-A-2").mkdir()
    (sysfs / "card0-HDMI-A-2" / "status").write_text("disconnected\n")
    (sysfs / "card0-HDMI-A-2" / "edid").write_bytes(b"")
    example = tmp_path / "display-Xorg.conf.example"
    example.write_text("# verify first\nSection \"Device\"\nEndSection\n")
    env = tmp_path / "display.env"
    env.write_text("OTHER=1\nXENOVA_DISPLAY_TOKEN=secret-token\n")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=f"{env} (ignore_errors=no)\n")
    def apply(**extra):
        writes = board_setup.plan("192.168.77.8", etc=etc, sysfs=sysfs, xorg_example=example, run=run, **extra)
        for path, text, mode, action in writes:
            if text is not None and action != "unchanged":
                board_setup._write(path, text, mode)
        return {p.name: a for p, _, _, a in writes}
    return SimpleNamespace(etc=etc, apply=apply, calls=calls)


def test_fresh_board_gets_hdmi_runtime_xorg_and_kiosk_token(board):
    actions = board.apply()
    assert actions == {"display-runtime.json": "created", "display-Xorg.conf": "created",
                       "display-kiosk.env": "created", "display-mobile.json": "skipped: pass --phone-ipv4 to create"}
    runtime = RuntimeConfig.load(board.etc / "display-runtime.json")
    output = runtime.outputs[0]
    assert runtime.ui_origin == "http://192.168.77.8:8086"
    assert (output.connector, output.width, output.height) == ("HDMI-1", 1920, 1080)
    assert output.edid_sha256 == hashlib.sha256(EDID).hexdigest()
    assert runtime.page_url(output) == "http://192.168.77.8:8086/display/combined"
    assert (board.etc / "display-Xorg.conf").read_text() == "Section \"Device\"\nEndSection\n"
    kiosk = board.etc / "display-kiosk.env"
    assert kiosk.read_text() == "XENOVA_DISPLAY_TOKEN=secret-token\n"
    assert stat.S_IMODE(kiosk.stat().st_mode) == 0o600
    assert board.calls[0][:4] == ["systemctl", "show", "-p", "EnvironmentFiles"]


def test_existing_files_are_kept_and_only_vm_address_follows(board):
    board.apply()
    xorg = board.etc / "display-Xorg.conf"
    xorg.write_text("custom\n")
    mobile = board.etc / "display-mobile.json"
    mobile.write_text(json.dumps({"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "10.58.23.17",
                                  "port": 8084, "ui_origin": "http://192.168.77.9:8086"}))
    runtime = json.loads((board.etc / "display-runtime.json").read_text())
    runtime["outputs"][0]["width"] = 1280
    runtime["outputs"][0]["height"] = 720
    runtime["ui_origin"] = "http://192.168.77.5:8086"
    (board.etc / "display-runtime.json").write_text(json.dumps(runtime))
    actions = board.apply()
    assert actions["display-runtime.json"] == actions["display-mobile.json"] == "ui_origin"
    assert actions["display-Xorg.conf"] == actions["display-kiosk.env"] == "kept"
    assert xorg.read_text() == "custom\n"
    kept = json.loads((board.etc / "display-runtime.json").read_text())
    assert kept["outputs"][0]["width"] == 1280 and kept["ui_origin"] == "http://192.168.77.8:8086"
    phone = json.loads(mobile.read_text())
    assert phone["phone_ipv4"] == "10.58.23.17" and phone["ui_origin"] == "http://192.168.77.8:8086"
    assert board.apply()["display-mobile.json"] == "unchanged"


def test_phone_config_is_created_only_when_asked(board):
    assert board.apply(phone_ipv4="10.58.23.17")["display-mobile.json"] == "created"
    value = json.loads((board.etc / "display-mobile.json").read_text())
    assert value == {"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "10.58.23.17",
                     "port": 8084, "ui_origin": "http://192.168.77.8:8086"}


@pytest.mark.parametrize("change", ["no-monitor", "public-vm", "no-token"])
def test_refuses_without_monitor_valid_vm_or_feed_token(board, tmp_path, change):
    if change == "no-monitor":
        (tmp_path / "drm" / "card0-HDMI-A-1" / "status").write_text("disconnected\n")
    if change == "no-token":
        (tmp_path / "display.env").write_text("OTHER=1\n")
    vm = "8.8.8.8" if change == "public-vm" else "192.168.77.8"
    with pytest.raises(ValueError):
        writes = board_setup.plan(vm, etc=board.etc, sysfs=tmp_path / "drm",
                                  xorg_example=tmp_path / "display-Xorg.conf.example",
                                  run=lambda *a, **k: SimpleNamespace(stdout=f"{tmp_path / 'display.env'}\n"))
    assert not (board.etc / "display-runtime.json").exists()


def test_dry_run_writes_nothing_and_prints_no_secret(board, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(board_setup, "ETC", board.etc)
    real_plan = board_setup.plan
    monkeypatch.setattr(board_setup, "plan", lambda vm, **kw: real_plan(
        vm, etc=board.etc, sysfs=tmp_path / "drm", xorg_example=tmp_path / "display-Xorg.conf.example",
        run=lambda *a, **k: SimpleNamespace(stdout=f"{tmp_path / 'display.env'}\n"),
        **{k: v for k, v in kw.items() if k in ("phone_ipv4", "usb_interface", "connector", "width", "height")}))
    assert board_setup.main(["--vm-ip", "192.168.77.8", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "secret-token" not in out and '"created"' in out
    assert not board.etc.exists()
