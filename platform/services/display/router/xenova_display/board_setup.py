"""One-time per-board display configuration on dom0 (root).

The image ships only examples; these files are provisioned per board and are not
recreated by an image flash:

  /etc/xenova/display-runtime.json   HDMI 1대, ui_origin = Interaction VM (created if absent)
  /etc/xenova/display-Xorg.conf      from the packaged example (created if absent)
  /etc/xenova/display-kiosk.env      the feed's XENOVA_DISPLAY_TOKEN line, 0600 (created if absent)
  /etc/xenova/display-mobile.json    ui_origin updated; created only with --phone-ipv4

Existing files are kept; only their ui_origin is changed. Nothing is restarted and no
secret is printed. Usage:

  python3 -m xenova_display.board_setup --vm-ip 192.168.77.8 [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .outputs import RuntimeConfig

ETC = Path("/etc/xenova")
XORG_EXAMPLE = Path("/usr/share/xenova/display/examples/display-Xorg.conf.example")
FEED_UNIT = "xenova-display-feed.service"
TOKEN_KEY = "XENOVA_DISPLAY_TOKEN="
UI_PORT = 8086


def _write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def connector_edid(sysfs, connector):
    """xrandr HDMI-N is DRM card*-HDMI-A-N; the monitor must be connected with an EDID."""
    if not connector.startswith("HDMI-") or not connector[5:].isdigit():
        raise ValueError("connector must look like HDMI-1")
    for path in sorted(Path(sysfs).glob(f"card*-HDMI-A-{connector[5:]}")):
        if (path / "status").read_text().strip() == "connected":
            edid = (path / "edid").read_bytes()
            if edid:
                return hashlib.sha256(edid).hexdigest()
    raise ValueError(f"{connector}: no connected monitor with an EDID")


def feed_token_line(run=subprocess.run):
    """The feed's token line, read from the env files of its own unit."""
    out = run(["systemctl", "show", "-p", "EnvironmentFiles", "--value", FEED_UNIT],
              capture_output=True, text=True, timeout=5, check=False).stdout
    for entry in out.split():
        if entry.startswith("("):
            continue
        path = Path(entry.lstrip("-"))
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(TOKEN_KEY) and len(line) > len(TOKEN_KEY):
                    return line + "\n"
        except OSError:
            continue
    raise ValueError("feed token not found in the feed unit's environment files")


def plan(vm_ip, *, etc=ETC, sysfs="/sys/class/drm", connector="HDMI-1", width=1920, height=1080,
         phone_ipv4=None, usb_interface="usb0", xorg_example=XORG_EXAMPLE, run=subprocess.run):
    """Returns [(path, text, mode, action)] for the files that need writing."""
    etc = Path(etc)
    origin = f"http://{vm_ip}:{UI_PORT}"
    writes = []

    runtime = etc / "display-runtime.json"
    if runtime.exists():
        value = json.loads(runtime.read_text(encoding="utf-8"))
        action = "unchanged" if value.get("ui_origin") == origin else "ui_origin"
        value["ui_origin"] = origin
    else:
        action = "created"
        value = {"backend": "x11", "display": ":5", "feed_origin": "http://127.0.0.1:8082", "ui_origin": origin,
                 "outputs": [{"output_id": "monitor-1", "connector": connector,
                              "edid_sha256": connector_edid(sysfs, connector), "x": 0, "y": 0,
                              "width": width, "height": height, "page": "/display/combined"}]}
    RuntimeConfig.model_validate(value)  # same checks as the HDMI service; ui_origin must be a private IPv4
    writes.append((runtime, json.dumps(value, indent=2) + "\n", 0o644, action))

    xorg = etc / "display-Xorg.conf"
    if xorg.exists():
        writes.append((xorg, None, None, "kept"))
    else:
        text = "".join(l for l in Path(xorg_example).read_text(encoding="utf-8").splitlines(True)
                       if not l.startswith("#"))
        writes.append((xorg, text, 0o644, "created"))

    kiosk = etc / "display-kiosk.env"
    if kiosk.exists():
        writes.append((kiosk, None, None, "kept"))
    else:
        writes.append((kiosk, feed_token_line(run), 0o600, "created"))

    mobile = etc / "display-mobile.json"
    if mobile.exists():
        value = json.loads(mobile.read_text(encoding="utf-8"))
        action = "unchanged" if value.get("ui_origin") == origin else "ui_origin"
        value["ui_origin"] = origin
        writes.append((mobile, json.dumps(value, indent=2) + "\n", 0o600, action))
    elif phone_ipv4:
        value = {"schema_version": 1, "usb_interface": usb_interface, "phone_ipv4": phone_ipv4,
                 "port": 8084, "ui_origin": origin}
        writes.append((mobile, json.dumps(value, indent=2) + "\n", 0o600, "created"))
    else:
        writes.append((mobile, None, None, "skipped: pass --phone-ipv4 to create"))
    return writes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vm-ip", required=True, help="Interaction VM reserved IPv4 (web console card)")
    parser.add_argument("--connector", default="HDMI-1")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--phone-ipv4", help="only needed when display-mobile.json does not exist yet")
    parser.add_argument("--usb-interface", default="usb0")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 and not args.dry_run:
        parser.error("run as root (or use --dry-run)")
    writes = plan(args.vm_ip, connector=args.connector, width=args.width, height=args.height,
                  phone_ipv4=args.phone_ipv4, usb_interface=args.usb_interface)
    for path, text, mode, action in writes:
        if text is not None and action != "unchanged" and not args.dry_run:
            _write(path, text, mode)
    print(json.dumps({"dry_run": args.dry_run, "ui_origin": f"http://{args.vm_ip}:{UI_PORT}",
                      "files": {str(p): a for p, _, _, a in writes},
                      "next": "python3 -m xenova_display.display_mode hdmi"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
