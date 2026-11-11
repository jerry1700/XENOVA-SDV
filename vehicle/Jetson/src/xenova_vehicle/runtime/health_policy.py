"""Jetson의 결과 수명과 독립된 worker 고장 감시 정책.

프레임 만료/입력 부재는 정상 흐름이다. 이 한도는 실제 제어 정체와 취소 불응을
회수할 때만 쓴다. 초기 값은 운영 벤치 값이며 Nano 부하 실측으로 조정한다.
RPi의 관측/명령 만료나 원 촬영 시각은 변경하지 않는다.
"""

from dataclasses import dataclass
from functools import lru_cache
import math
import os


class FrameExpired(ValueError):
    """내용이 올바른 관측의 사용 기한이 끝남. 해당 작업만 폐기한다."""


class ReservationExpired(FrameExpired):
    """native token 취득 전에 예약 기한이 끝남. 취소 통로로 예약만 반환한다."""


class ConfigurationError(ValueError):
    """재기동으로 해결되지 않는 명시적인 설치·자산 설정 불일치."""


@dataclass(frozen=True)
class HealthPolicy:
    control_stall_us: int = 5_000_000
    capture_stall_us: int = 15_000_000
    compute_stall_us: int = 15_000_000

    def __post_init__(self):
        for value in (self.control_stall_us, self.capture_stall_us, self.compute_stall_us):
            if type(value) is not int or not 0 < value < 2**63:
                raise ConfigurationError('Jetson health intervals must be positive signed64 microseconds')


@lru_cache(maxsize=1)
def health_policy():
    """기동 시 process별 한 번 읽는다. 운행 중 환경 변화로 기한을 갱신하지 않는다."""
    fields = {}
    for field in ('control_stall_us', 'capture_stall_us', 'compute_stall_us'):
        name = 'XENOVA_JETSON_' + field.removesuffix('_us').upper() + '_S'
        if name in os.environ:
            try:value = float(os.environ[name])
            except ValueError:raise ConfigurationError(name + ' must be a number') from None
            if not math.isfinite(value) or not 0 < value < 2**63 / 1_000_000:
                raise ConfigurationError(name + ' must be positive finite signed64 microseconds')
            fields[field] = int(value * 1_000_000)
    return HealthPolicy(**fields)
