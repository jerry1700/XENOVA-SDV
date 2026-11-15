"""차량 표시 장치 모드(HDMI / 휴대폰) 조회와 전환 (계약 4.14).

전환 로직은 바꾸지 않는다. 보드 운영자가 쓰는 명령 `python3 -m xenova_display.display_mode <status|hdmi|mobile>`
을 그대로 실행하고, 그 명령이 내는 JSON 한 줄을 계약 응답으로 옮긴다.
- 전환은 그 명령이 직접 잠그고(동시 실행이면 DISPLAY_MODE_BUSY), 실패하면 이전 모드로 되돌린다.
- 휴대폰 모드는 중계(xenova-display-mobile)를 켜고 HDMI 를 끈다. HDMI 모드는 반대다.
- 휴대폰 모드로 바꾸기 전에 USB 링크(테더링)가 없으면 명령을 부르지 않고 바로 409 를 낸다.
  명령에 맡기면 중계를 켠 뒤 15초를 기다리고서야 실패하기 때문이다.

전환 도중에는 명령을 끊지 않는다. 끊으면 명령의 되돌리기가 돌지 않아 두 화면이 모두 꺼질 수 있다.
그래서 요청 예산(4.9)은 시작 전에만 확인하고, 실행 시한은 명령 자체의 최대 대기(되돌리기 포함)보다 길게 둔다.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable

from . import budget
from .errors import DriverError, DriverTimeout, InvalidState, PlatformUnavailable

COMMAND = ("/usr/bin/python3", "-m", "xenova_display.display_mode")
STATUS_TIMEOUT_S = 10.0
# display_mode 의 최대 대기: 휴대폰 전환 50+15+25초, HDMI 전환 50+45+15초, 실패 시 되돌리기 최대 75초
SWITCH_TIMEOUT_S = 240.0
MODES = ("hdmi", "mobile")
_SERVICES = {
    "hdmi": "xenova-display-hdmi.service",
    "mobile": "xenova-display-mobile.service",
    "feed": "xenova-display-feed.service",
}

# 전환이 적용되지 않았고 이전 모드가 그대로인 경우(409). 값은 (사람용 설명, 다시 시도해 볼 만한가)
_NOT_APPLIED = {
    "DISPLAY_MODE_BUSY": ("another display mode switch is running", True),
    "USB_LINK_ABSENT": ("no USB link to the phone; connect the cable and turn on USB tethering", True),
    "MOBILE_ENDPOINT_NOT_READY": ("phone relay did not get a usable USB link; previous mode was kept", True),
    "HDMI_RENDERER_NOT_READY": ("HDMI screen did not come up; previous mode was kept", True),
    "DISPLAY_SERVICE_TRANSITION_FAILED": ("display service did not change state; previous mode was kept", True),
    "DISPLAY_MODE_FAILED_RESTORED": ("display mode switch failed; previous mode was kept", True),
    "DISPLAY_SERVICE_NOT_SETTLED": ("a display service is still starting or stopping", True),
    "SHARED_FEED_NOT_ACTIVE": ("display feed service is not running", False),
    "MODE_GUARD_NOT_LOADED": ("board display mode setup is missing", False),
    "NATIVE_DISPLAY_UNITS_REQUIRED": ("board display services are not installed", False),
    "LEGACY_MOBILE_FIRST_REQUIRES_MIGRATION": ("old mobile-first marker must be migrated on the board", False),
    "FOREIGN_MODE_MARKER": ("unexpected display mode marker on the board", False),
    "UNSAFE_CONFIGURATION": ("display mode files have unsafe ownership or permissions", False),
    "UNSAFE_RUNTIME_DIRECTORY": ("display mode runtime directory is unsafe", False),
}
_ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE_CHECK_DISPLAY_SERVICES"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DisplayMode:
    def __init__(self, *, usb_present: Callable[[], bool], run=subprocess.run, now: Callable[[], str] = _now,
                 command: tuple[str, ...] = COMMAND):
        self.usb_present = usb_present
        self.run, self.now, self.command = run, now, command

    def status(self) -> dict:
        return self._public(self._call("status", budget.clamp(STATUS_TIMEOUT_S, "display mode status")))

    def switch(self, mode: str) -> dict:
        if mode not in MODES:  # 모델 검증이 먼저 막는다. 직접 호출에 대한 방어
            raise InvalidState(f"unsupported display mode {mode!r}")
        budget.ensure("display mode switch")
        if mode == "mobile" and not self.usb_present():
            raise self._refused("USB_LINK_ABSENT")
        return self._public(self._call(mode, SWITCH_TIMEOUT_S))

    # ---- 내부 ----
    def _call(self, argument: str, timeout_s: float) -> dict:
        try:
            result = self.run([*self.command, argument], capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            raise DriverTimeout("display mode command did not finish in time; check the board display services",
                                platform_code="DISPLAY_MODE_TIMEOUT", retryable=False) from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlatformUnavailable("cannot run the display mode command", detail=type(exc).__name__) from None
        value = self._last_json(result.stdout)
        if value is None:
            raise PlatformUnavailable("display mode command gave no readable result",
                                      detail=f"exit={result.returncode}")
        if result.returncode == 0 and "selected" in value:
            return value
        code = value.get("code") if isinstance(value.get("code"), str) else "DISPLAY_MODE_CHECK_FAILED"
        if code in _NOT_APPLIED:
            raise self._refused(code)
        if code == _ROLLBACK_INCOMPLETE:
            raise DriverError("display mode switch failed and the previous mode could not be fully restored; "
                              "check the board display services", platform_code=code, retryable=False)
        raise PlatformUnavailable("display mode command failed", platform_code=code, retryable=True)

    @staticmethod
    def _refused(code: str) -> InvalidState:
        message, retryable = _NOT_APPLIED[code]
        return InvalidState(message, platform_code=code, retryable=retryable)

    @staticmethod
    def _last_json(stdout: str | None) -> dict | None:
        for line in reversed((stdout or "").strip().splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
        return None

    def _public(self, value: dict) -> dict:
        services = value.get("services") if isinstance(value.get("services"), dict) else {}

        def state(unit: str) -> str:
            entry = services.get(unit)
            active = entry.get("ActiveState") if isinstance(entry, dict) else None
            return active if isinstance(active, str) and active else "unknown"

        selected = value.get("selected")
        if selected not in MODES:
            raise PlatformUnavailable("display mode command gave an unknown mode", detail=str(selected)[:32])
        boot = value.get("boot_default")
        return {
            "mode": selected,
            "boot_default": boot if boot in MODES else "hdmi",
            "services": {name: state(unit) for name, unit in _SERVICES.items()},
            "hdmi_renderer_ready": value.get("hdmi_renderer_ready") is True,
            "observed_at": self.now(),
        }
