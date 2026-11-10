"""Bounded read-only adapters the phone entry point relays: the loopback feed or the display domU.

No listener, browser authentication policy, new collector or MQTT client here.
The phone entry point supplies its own authenticated browser session separately.
Plan A for the phone (349): the web app lives in the display domU (VM3 guest_app);
dom0 only relays its files and presentation over USB, and the phone browser renders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import suppress
import http.client
import ipaddress
import re
from urllib.parse import urlparse

# /display/combined is the integrated vehicle screen the phone shows; /display/mobile is kept
# for the runtime readiness check (mobile_preflight). Both are the same built index.html.
READ_PATHS = frozenset({"/display/mobile", "/display/combined", "/api/v1/display/presentation"})
ASSET = re.compile(r"/assets/[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.(?:js|css|png|jpg|jpeg|webp|svg|woff2?|ttf|ico)")
MAX_BODY = 2 * 1024 * 1024


class FeedUnavailable(Exception):
    """Public error contains a fixed code only, never raw upstream details."""
    def __init__(self, code="DISPLAY_FEED_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FeedResponse:
    status: int
    content_type: str
    body: bytes = field(repr=False)


def allowed_path(path):
    return path in READ_PATHS or ASSET.fullmatch(path) is not None


@dataclass(frozen=True)
class LocalFeed:
    token: str = field(repr=False)
    port: int = 8082

    def __post_init__(self):
        if (not isinstance(self.token, str) or not self.token.strip() or any(c in self.token for c in "\r\n\0")
                or type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("valid local feed configuration required")

    def get(self, path, *, connect=http.client.HTTPConnection):
        return read(connect, "127.0.0.1", self.port, path, {"Authorization": f"Bearer {self.token}"}, 2)


@dataclass(frozen=True)
class GuestUI:
    """The display domU's guest_app (Plan A). It admits dom0 by its Xen bridge address only,
    so no dom0 token is sent; its presentation comes back through dom0's guest data path."""
    host: str
    port: int = 8086

    def __post_init__(self):
        try:
            address = ipaddress.IPv4Address(self.host)
        except (ValueError, TypeError):
            raise ValueError("display domU IPv4 address required") from None
        if (not address.is_private or address.is_loopback or address.is_unspecified
                or address.is_multicast or address.is_link_local or str(address) != self.host
                or type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("display domU must be a private bridge address and port")

    @classmethod
    def from_origin(cls, origin):
        """Only http://<private IPv4>:<port>, as in the HDMI kiosk's ui_origin."""
        parsed = urlparse(origin) if isinstance(origin, str) and len(origin) <= 64 else None
        try:
            port = parsed.port if parsed else None
        except ValueError:
            port = None
        if (parsed is None or parsed.scheme != "http" or port is None or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("ui_origin must be http://<private IPv4>:<port>")
        return cls(parsed.hostname or "", port)

    def get(self, path, *, connect=http.client.HTTPConnection):
        # VM3 → dom0 guest data → feed adds a hop; the page itself aborts at 2.5 s.
        return read(connect, self.host, self.port, path, {}, 3)


def read(connect, host, port, path, headers, timeout):
    if not allowed_path(path):
        raise ValueError("read path is not allowed")
    # No external URL, proxy, forwarding headers, browser cookie or redirect.
    connection = None
    try:
        connection = connect(host, port, timeout=timeout)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status in (401, 403):
            raise FeedUnavailable("DISPLAY_FEED_AUTHENTICATION_FAILED")
        if response.status not in (200, 404):
            raise FeedUnavailable()
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise FeedUnavailable("DISPLAY_FEED_RESPONSE_TOO_LARGE")
        content_type = response.getheader("Content-Type", "application/octet-stream")
        # No upstream Set-Cookie/Location/WWW-Authenticate or service headers.
        return FeedResponse(response.status, content_type, body)
    except FeedUnavailable:
        raise
    except Exception:
        raise FeedUnavailable() from None
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.close()
