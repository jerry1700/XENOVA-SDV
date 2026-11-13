"""Resolve a verified USB interface without assigning IPs or changing routes."""
import contextlib
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess

PRIVATE = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ip(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in block for block in PRIVATE):
        raise ValueError("RFC1918 IPv4 required")
    return address


@dataclass(frozen=True)
class Endpoint:
    interface: str
    local_ip: str
    phone_ip: str
    port: int
    # A re-plugged phone is a new kernel device even when the name and address repeat.
    # SO_BINDTODEVICE keeps the old index, so a listener must follow this value (393).
    ifindex: int = 0

    @property
    def host(self): return f"{self.local_ip}:{self.port}"

    @property
    def origin(self): return f"http://{self.host}"


def resolve(config, *, sysfs=Path("/sys/class/net"), run=subprocess.run,
            leases=Path("/run/systemd/netif/leases")):
    if (not isinstance(config, dict) or set(config) - {"ui_origin"} != {"schema_version", "usb_interface", "phone_ipv4", "port"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1):
        raise ValueError("invalid mobile config")
    interface, port = config["usb_interface"], config["port"]
    if (not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface)
            or type(port) is not int or not 1024 <= port <= 65535
            or port in (8081, 8082, 8083)):
        raise ValueError("verified USB interface and separate unprivileged port required")
    if (sysfs / interface / "device/subsystem").resolve(strict=True).name != "usb":
        raise ValueError("configured interface is not a USB device")
    phone = private_ip(config["phone_ipv4"])
    result = run(["ip", "-j", "-4", "address", "show", "dev", interface],
                 capture_output=True, text=True, timeout=2, check=True)
    if len(result.stdout) > 65536:
        raise ValueError("address response too large")
    rows = json.loads(result.stdout)
    if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("ifname") != interface
            or "UP" not in rows[0].get("flags", [])):
        raise ValueError("configured USB link is not up")
    ifindex = rows[0].get("ifindex")
    if type(ifindex) is not int or ifindex <= 0:
        raise ValueError("USB link index required")
    links = []
    for value in rows[0].get("addr_info", []):
        if value.get("family") != "inet" or value.get("scope") != "global":
            continue
        address = private_ip(value["local"])
        links.append((address, ipaddress.IPv4Network(f"{address}/{value['prefixlen']}", strict=False)))
    matches = [(address, phone) for address, network in links if on_link(phone, address, network)]
    if not matches and links:
        # The phone picks its tethering subnet itself and may change it (seen 2026-11-12:
        # 10.117.188.0/24 one day, 10.58.23.0/24 the next). The configured address then no
        # longer fits. The phone is the DHCP server/router of this link, so use the address it
        # gave in this link's DHCP lease (the board installs no route through the phone).
        # Read-only, same USB device and one-address checks as above.
        matches = [(address, server) for server in lease_servers(leases / str(ifindex))
                   for address, network in links if on_link(server, address, network)]
    if len(matches) != 1:
        raise ValueError("one USB address on the verified phone subnet required")
    address, phone = matches[0]
    return Endpoint(interface, str(address), str(phone), port, ifindex)


def on_link(phone, address, network):
    return phone != address and phone in network and phone not in (network.network_address, network.broadcast_address)


def lease_servers(path):
    """The DHCP server/router addresses in systemd-networkd's lease for this link (the phone)."""
    try:
        if path.stat().st_size > 65536:
            raise ValueError("lease file too large")
        text = path.read_text()
    except FileNotFoundError:
        return []
    servers = set()
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("SERVER_ADDRESS", "ROUTER") and value:
            with contextlib.suppress(ValueError):
                servers.add(private_ip(value.split()[0]))
    return sorted(servers)


def bind(endpoint, *, socket_factory=socket.socket):
    sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Do not let an address later appearing on Wi-Fi/Tailscale expose this socket.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, endpoint.interface.encode() + b"\0")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((endpoint.local_ip, endpoint.port))
        sock.listen(16)
        sock.setblocking(False)
        return sock
    except Exception:
        sock.close()
        raise
