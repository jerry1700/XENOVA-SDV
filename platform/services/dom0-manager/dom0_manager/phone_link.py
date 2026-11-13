"""USB 휴대폰 화면 중계(xenova-display-mobile)의 상태 조회와 연결번호 발급 (계약 4.13).

중계 코드는 바꾸지 않는다. 중계가 이미 남기는 흔적만 밖에서 읽는다.
- 실행 여부: systemctl is-active
- 접속 주소: <runtime>/endpoint.json (쓸 수 있는 링크가 없으면 파일이 없다)
- 연결번호: <runtime>/pairing-code. 휴대폰이 연결에 성공하면 지워지고, 만료돼도 남는다(만료는 파일 시각 + TTL)
- 새 번호: SIGUSR1. 중계의 rotate() 는 이미 연결된 휴대폰 세션도 끊는다
- usb0 실제 주소·게이트웨이(휴대폰): ip -j

연결번호 값은 발급 응답에만 담는다. 상태 조회·로그·예외 메시지에는 넣지 않는다.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .errors import InvalidState, PlatformUnavailable

UNIT = "xenova-display-mobile.service"
DEFAULT_INTERFACE = "usb0"
CODE_TTL_S = 120  # 중계 CODE_SECONDS. endpoint.json 의 pairing_ttl_s 가 있으면 그 값을 쓴다
COMMAND_TIMEOUT_S = 3.0
ISSUE_WAIT_S = 2.0
ISSUE_POLL_S = 0.05
MAX_FILE_BYTES = 4096
_CODE = re.compile(r"[0-9]{6}")
_RELAY_STATES = {"active": "listening", "activating": "starting", "reloading": "starting", "failed": "failed",
                 "inactive": "stopped", "deactivating": "stopped"}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class PhoneLink:
    def __init__(self, runtime_dir: str, config_path: str, *, unit: str = UNIT, run=subprocess.run,
                 clock=time.time, sleep=time.sleep):
        self.runtime = Path(runtime_dir)
        self.config_path = Path(config_path)
        self.unit = unit
        self.run, self.clock, self.sleep = run, clock, sleep
        self._issue = threading.Lock()  # 동시 발급이 서로의 번호를 읽지 않게 한 번에 하나만

    # ---- 조회 ----
    def status(self) -> dict:
        config = self._config()
        relay = self._relay_state()
        endpoint = self._endpoint() if relay == "listening" else None
        if relay == "listening" and endpoint is None:
            relay = "waiting_link"  # 실행 중이지만 케이블·테더링·주소가 쓸 수 없는 상태
        interface = config.get("usb_interface") if isinstance(config.get("usb_interface"), str) else DEFAULT_INTERFACE
        usb = self._usb(interface)
        phone = config.get("phone_ipv4") if isinstance(config.get("phone_ipv4"), str) else None
        ttl = self._ttl(endpoint)
        expires = self._code_expiry(ttl)
        now = self.clock()
        return {
            "configured": bool(config),
            "relay": relay,
            "url": endpoint.get("url") if endpoint and isinstance(endpoint.get("url"), str) else None,
            "usb": usb,
            "configured_phone_ipv4": phone,
            "subnet_mismatch": self._mismatch(usb["ipv4"], phone),
            "pairing": {"active": expires is not None and now < expires,
                        "expires_at": _iso(expires) if expires is not None and now < expires else None},
            "observed_at": _iso(now),
        }

    # ---- 발급 ----
    def issue_code(self) -> dict:
        with self._issue:
            if self._relay_state() != "listening" or self._endpoint() is None:
                raise InvalidState("phone relay is not listening; start mobile mode and connect the phone first")
            before = self._signature()
            if self._systemctl("kill", "--kill-whom=main", "--signal=USR1", self.unit).returncode != 0:
                raise PlatformUnavailable("could not signal the phone relay; retry later")
            deadline = self.clock() + ISSUE_WAIT_S
            while True:
                after = self._signature()
                if after is not None and after != before:
                    code = self._read_code()
                    if code is not None:
                        expires = after[2] / 1e9 + self._ttl(self._endpoint())
                        return {"code": code, "expires_at": _iso(expires)}
                if self.clock() >= deadline:
                    raise PlatformUnavailable("phone relay did not publish a new pairing code; retry later")
                self.sleep(ISSUE_POLL_S)

    # ---- 내부 ----
    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return self.run(["systemctl", *args], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlatformUnavailable("cannot query the phone relay service", detail=type(exc).__name__) from None

    def _relay_state(self) -> str:
        result = self._systemctl("is-active", self.unit)
        return _RELAY_STATES.get(result.stdout.strip(), "unknown")

    def _read_json(self, path: Path) -> dict | None:
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PlatformUnavailable("cannot read phone relay state", detail=type(exc).__name__) from None
        if len(raw) > MAX_FILE_BYTES:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _config(self) -> dict:
        return self._read_json(self.config_path) or {}

    def _endpoint(self) -> dict | None:
        return self._read_json(self.runtime / "endpoint.json")

    @staticmethod
    def _ttl(endpoint: dict | None) -> int:
        value = (endpoint or {}).get("pairing_ttl_s")
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 3600 else CODE_TTL_S

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            info = os.stat(self.runtime / "pairing-code")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PlatformUnavailable("cannot read phone relay state", detail=type(exc).__name__) from None
        return info.st_ino, info.st_size, info.st_mtime_ns

    def _code_expiry(self, ttl: int) -> float | None:
        signature = self._signature()
        return None if signature is None else signature[2] / 1e9 + ttl

    def _read_code(self) -> str | None:
        try:
            with open(self.runtime / "pairing-code", encoding="ascii") as stream:
                value = stream.read(16).strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value if _CODE.fullmatch(value) else None

    def _ip(self, *args: str) -> list | None:
        try:
            result = self.run(["ip", "-j", "-4", *args], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlatformUnavailable("cannot read the USB network link", detail=type(exc).__name__) from None
        if result.returncode != 0:
            return None  # 장치 없음(케이블 뽑힘·테더링 꺼짐)
        try:
            value = json.loads(result.stdout or "[]")
        except ValueError:
            return None
        return value if isinstance(value, list) else None

    def _usb(self, interface: str) -> dict:
        rows = self._ip("addr", "show", "dev", interface)
        addresses: list[str] = []
        for row in rows or []:
            for info in row.get("addr_info", []) if isinstance(row, dict) else []:
                if info.get("family") == "inet" and info.get("scope") == "global" and "local" in info:
                    addresses.append(f"{info['local']}/{info.get('prefixlen', 32)}")
        gateway = None
        if rows:
            for route in self._ip("route", "show", "dev", interface) or []:
                if isinstance(route, dict) and route.get("dst") == "default" and isinstance(route.get("gateway"), str):
                    gateway = route["gateway"]
                    break
        return {"interface": interface, "present": bool(rows), "ipv4": addresses, "gateway": gateway}

    @staticmethod
    def _mismatch(addresses: list[str], phone: str | None) -> bool | None:
        """usb 주소가 설정된 휴대폰 대역 밖이면 true(휴대폰이 테더링 대역을 바꿈). 판단할 수 없으면 null."""
        if not addresses or not phone:
            return None
        try:
            target = ipaddress.IPv4Address(phone)
            return not any(target in ipaddress.IPv4Interface(value).network for value in addresses)
        except ValueError:
            return None
