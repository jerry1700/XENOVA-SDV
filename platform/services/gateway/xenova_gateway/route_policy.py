"""설치된 route budget의 단위·상한. 물리 기본값·지원 승인을 생성하지 않는다."""
from dataclasses import dataclass
import math

from xenova_drive_world.route import RouteReason


U63=(1<<63)-1
NIL=bytes(16)
ZERO=bytes(32)


class RouteError(ValueError):
    def __init__(self,reason):
        self.reason=RouteReason(reason)
        super().__init__(self.reason.name)


def require(condition,reason):
    if not condition:raise RouteError(reason)


def advance(value):
    require(type(value) is int and 0<value<U63,RouteReason.REQUEST_CONTEXT_CHANGED)
    return value+1


@dataclass(frozen=True)
class RouteBudgetPolicy:
    route_duration_us: int
    route_max_travel_m: float
    exit_phase_us: int
    merge_phase_us: int
    road_phase_us: int
    final_stop_phase_us: int
    progress_timeout_us: int
    max_unobserved_gap_us: int
    search_work_cap: int
    max_occurrences: int
    max_laps: int
    required_extent_policy_sha256: bytes
    terminal_profile_sha256: bytes
    registered_sha256: bytes

    def __post_init__(self):
        for name in ('route_duration_us','exit_phase_us','merge_phase_us','road_phase_us',
                     'final_stop_phase_us','progress_timeout_us','max_unobserved_gap_us'):
            value=getattr(self,name)
            require(type(value) is int and 0<value<=U63,RouteReason.UNSUPPORTED_PROFILE)
        require(all(getattr(self,name)<=self.route_duration_us for name in
            ('exit_phase_us','merge_phase_us','road_phase_us','final_stop_phase_us','progress_timeout_us')),
            RouteReason.UNSUPPORTED_PROFILE)
        require(self.max_unobserved_gap_us<=self.progress_timeout_us,RouteReason.UNSUPPORTED_PROFILE)
        require(type(self.route_max_travel_m) in (int,float) and math.isfinite(self.route_max_travel_m)
            and self.route_max_travel_m>0,RouteReason.UNSUPPORTED_PROFILE)
        require(type(self.search_work_cap) is int and 0<self.search_work_cap<(1<<32),RouteReason.UNSUPPORTED_PROFILE)
        require(type(self.max_occurrences) is int and 0<self.max_occurrences<=96
            and type(self.max_laps) is int and self.max_laps==1,RouteReason.UNSUPPORTED_PROFILE)
        for name in ('required_extent_policy_sha256','terminal_profile_sha256','registered_sha256'):
            value=getattr(self,name)
            require(type(value) is bytes and len(value)==32 and value!=ZERO,RouteReason.UNSUPPORTED_PROFILE)

    @classmethod
    def from_registered(cls,value,registered_sha256):
        """기존 설치 검증 owner가 확인한 digest를 받는다. 자기신고 hash로 설치를 승인하지 않는다."""
        fields=set(cls.__dataclass_fields__)-{'registered_sha256'}
        require(type(value) is dict and set(value)==fields,RouteReason.UNSUPPORTED_PROFILE)
        return cls(**value,registered_sha256=registered_sha256)

    def phase_cap(self,phase):
        caps={1:self.exit_phase_us,2:self.merge_phase_us,3:self.road_phase_us,4:self.final_stop_phase_us}
        require(phase in caps,RouteReason.REQUEST_CONTEXT_CHANGED)
        return caps[phase]
