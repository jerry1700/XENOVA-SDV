import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from xenova_display.mobile_network import Endpoint, bind, resolve


@pytest.fixture
def usb(tmp_path):
    sysfs = tmp_path / "net"
    subsystem = tmp_path / "bus/usb"
    subsystem.mkdir(parents=True)
    device = sysfs / "usb0/device"
    device.mkdir(parents=True)
    (device / "subsystem").symlink_to(subsystem)
    config = {"schema_version": 1, "usb_interface": "usb0", "phone_ipv4": "192.168.42.129", "port": 8084}
    info = [{"ifindex": 7, "ifname": "usb0", "flags": ["UP", "BROADCAST"], "addr_info": [
        {"family": "inet", "scope": "global", "local": "192.168.42.2", "prefixlen": 24}]}]
    return sysfs, config, info


def read(usb, lease=None):
    sysfs, config, info = usb
    leases = sysfs.parent / "leases"
    leases.mkdir(exist_ok=True)
    if lease is not None:
        (leases / "7").write_text(lease)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=json.dumps(info))
    endpoint = resolve(config, sysfs=sysfs, run=run, leases=leases)
    return endpoint, calls


def test_usb_address_is_read_without_ip_assignment_or_route_changes(usb):
    endpoint, calls = read(usb, "SERVER_ADDRESS=10.0.0.1\n")
    assert endpoint == Endpoint("usb0", "192.168.42.2", "192.168.42.129", 8084, 7)
    assert endpoint.origin == "http://192.168.42.2:8084"
    assert calls[0][0] == ["ip", "-j", "-4", "address", "show", "dev", "usb0"]
    assert calls[0][1]["timeout"] == 2
    assert len(calls) == 1  # the configured phone fits the link: the lease is not needed


MOVED = [{"family": "inet", "scope": "global", "local": "10.58.23.26", "prefixlen": 24}]
# Shape of systemd-networkd's lease on the board, 2026-11-12 (phone at 10.58.23.17).
LEASE = "# This is private data. Do not parse.\nADDRESS=10.58.23.26\nNETMASK=255.255.255.0\n" \
        "ROUTER=10.58.23.17\nSERVER_ADDRESS=10.58.23.17\nNEXT_SERVER=10.58.23.17\nDNS=10.58.23.17\n"


def test_phone_that_moved_its_tethering_subnet_is_found_from_the_link_lease(usb):
    # The configured phone was 10.117.188.86 (192.168.42.129 here); the phone came back on
    # 10.58.23.0/24. The board has no route through the phone; the lease names it.
    usb[2][0]["addr_info"] = MOVED
    endpoint, calls = read(usb, LEASE)
    assert endpoint == Endpoint("usb0", "10.58.23.26", "10.58.23.17", 8084, 7)
    assert len(calls) == 1  # no extra commands


@pytest.mark.parametrize("lease", [
    None,                                                       # no lease for this link
    "ADDRESS=10.58.23.26\n",                                    # no server in the lease
    "SERVER_ADDRESS=10.99.0.1\n",                               # server outside the link subnet
    "SERVER_ADDRESS=10.58.23.26\n",                             # the board's own address
    "SERVER_ADDRESS=10.58.23.255\n",                            # broadcast
    "SERVER_ADDRESS=8.8.8.8\n",                                 # not private
    "SERVER_ADDRESS=10.58.23.17\nROUTER=10.58.23.18\n",         # two possible phones
    "SERVER_ADDRESS=not-an-address\n",
])
def test_moved_subnet_without_one_clear_phone_in_the_lease_is_refused(usb, lease):
    usb[2][0]["addr_info"] = MOVED
    with pytest.raises(ValueError): read(usb, lease)


@pytest.mark.parametrize("key,value", [("usb_interface", "../wlan0"), ("usb_interface", "VERIFY_USB_INTERFACE"),
    ("phone_ipv4", "100.87.115.29"), ("phone_ipv4", "8.8.8.8"), ("phone_ipv4", "127.0.0.1"),
    ("phone_ipv4", "192.168.43.1"), ("phone_ipv4", "192.168.42.2"), ("phone_ipv4", "192.168.42.255"),
    ("port", 8081), ("port", 8082), ("port", 8083), ("port", 80), ("port", True), ("schema_version", True)])
def test_unverified_or_unsafe_configuration_is_rejected(usb, key, value):
    usb[1][key] = value
    with pytest.raises((ValueError, OSError)): read(usb)


def test_non_usb_interface_is_not_used_as_a_wireless_fallback(usb, tmp_path):
    sysfs, _, _ = usb
    subsystem = tmp_path / "bus/pci"
    subsystem.mkdir()
    (sysfs / "usb0/device/subsystem").unlink()
    (sysfs / "usb0/device/subsystem").symlink_to(subsystem)
    with pytest.raises(ValueError, match="USB"): read(usb)


@pytest.mark.parametrize("change", ["down", "no-address", "ambiguous", "no-index", "bad-index"])
def test_down_missing_or_ambiguous_link_does_not_guess_an_address(usb, change):
    info = usb[2]
    if change == "down": info[0]["flags"] = []
    if change == "no-index": del info[0]["ifindex"]
    if change == "bad-index": info[0]["ifindex"] = True
    if change == "no-address": info[0]["addr_info"] = []
    if change == "ambiguous": info[0]["addr_info"].append({"family": "inet", "scope": "global", "local": "192.168.42.3", "prefixlen": 24})
    with pytest.raises(ValueError): read(usb)


def test_listener_is_bound_to_the_usb_device_and_specific_address():
    calls = []
    class Socket:
        def setsockopt(self, *args): calls.append(("option", *args))
        def bind(self, *args): calls.append(("bind", *args))
        def listen(self, *args): calls.append(("listen", *args))
        def setblocking(self, *args): calls.append(("blocking", *args))
        def close(self): calls.append(("close",))
    sock = Socket()
    assert bind(Endpoint("usb0", "192.168.42.2", "192.168.42.129", 8084), socket_factory=lambda *_: sock) is sock
    assert ("option", socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"usb0\0") in calls
    assert ("bind", ("192.168.42.2", 8084)) in calls


def test_interface_binding_failure_closes_socket_and_does_not_fall_back():
    calls = []
    class Socket:
        def setsockopt(self, *args): raise PermissionError("no device capability")
        def close(self): calls.append("closed")
    with pytest.raises(PermissionError):
        bind(Endpoint("usb0", "192.168.42.2", "192.168.42.129", 8084), socket_factory=lambda *_: Socket())
    assert calls == ["closed"]
