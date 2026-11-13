"""연속 수락한 휠 표본 사이의 이동량과 오차를 구간별로 계산한다."""

from dataclasses import dataclass
import math

from .registration import wrap_yaw


@dataclass(frozen=True)
class WheelObservation:
    source: tuple
    sequence: int
    device_us: int
    ticks: tuple[int, int]
    clock_pad_us: int


@dataclass(frozen=True)
class WheelMotionProfile:
    encoder_bits: int
    metres_per_tick: float
    metres_per_tick_upper: float
    track_width_m: float
    distance_error_ratio: float
    heading_error_per_m: float

    def __post_init__(self):
        values = (self.metres_per_tick, self.metres_per_tick_upper, self.track_width_m,
                  self.distance_error_ratio, self.heading_error_per_m)
        if (type(self.encoder_bits) is not int or self.encoder_bits not in (32, 64)
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in values) or self.metres_per_tick_upper < self.metres_per_tick):
            raise ValueError('explicit wheel geometry and interval error bounds required')

    @classmethod
    def from_odometry(cls, config):
        return cls(**{key: config[key] for key in cls.__dataclass_fields__})

    def distances(self, previous, current, max_speed, max_gap_us):
        elapsed_us = current.device_us - previous.device_us
        if (current.source != previous.source or current.sequence != previous.sequence + 1
                or not 0 < elapsed_us <= max_gap_us):
            return None
        modulus = 1 << self.encoder_bits
        half = modulus // 2
        counts = (*previous.ticks, *current.ticks)
        if any(not -half <= count < half for count in counts):
            return None
        deltas = tuple((now - old + half) % modulus - half
                       for old, now in zip(previous.ticks, current.ticks))
        if any(delta == -half for delta in deltas):
            return None
        distances = tuple(delta * self.metres_per_tick for delta in deltas)
        if max(map(abs, distances)) > max_speed * elapsed_us / 1e6 + self.metres_per_tick_upper:
            return None
        return distances


@dataclass(frozen=True)
class WheelProgress:
    """같은 연속 구간의 nominal 이동 거리 prefix다."""

    segment: tuple
    observation: WheelObservation
    profile: WheelMotionProfile
    travel_m: float = 0.
    wheel_travel_m: float = 0.
    turn_travel_rad: float = 0.
    position_roundoff_m: float = 0.
    yaw_roundoff_rad: float = 0.


@dataclass(frozen=True)
class WheelCapture:
    """원 엔코더 표본과 촬영 구간 사이의 위치·각도 여유를 보관한다."""

    progress: WheelProgress
    position_pad_m: float
    yaw_pad_rad: float


def wheel_progress(profile, previous, old_pose, current, pose, *, max_speed, max_gap_us):
    if current is None:
        return None
    if previous is not None:
        distances = profile.distances(previous.observation, current, max_speed, max_gap_us)
        if distances is not None:
            left, right = distances
            distance = (left + right) / 2
            turn = (right - left) / profile.track_width_m
            mid = old_pose.yaw + turn / 2
            arc = distance if not turn else distance * math.sin(turn / 2) / (turn / 2)
            # Gateway는 pose를 float32로 발행한다. 틱과 다른 적분이 들어오면 구간을 끊는다.
            guard = 2**-21 * max(1., abs(old_pose.x), abs(old_pose.y), abs(pose.x), abs(pose.y))
            position_roundoff = math.hypot(pose.x - old_pose.x - arc * math.cos(mid),
                                           pose.y - old_pose.y - arc * math.sin(mid))
            yaw_roundoff = abs(wrap_yaw(pose.yaw - old_pose.yaw - turn))
            if position_roundoff <= guard and yaw_roundoff <= 2**-20:
                return WheelProgress(previous.segment, current, profile,
                    previous.travel_m + abs(distance), previous.wheel_travel_m + abs(left) + abs(right),
                    previous.turn_travel_rad + abs(turn),
                    previous.position_roundoff_m + position_roundoff, previous.yaw_roundoff_rad + yaw_roundoff)
    return WheelProgress((*current.source, current.sequence, current.device_us), current, profile)


def relative_wheel_bounds(start, end):
    """start 차체 좌표로 본 end의 위치·방향 오차. 증거가 끊기면 None을 반환한다."""
    first, last = start.wheel_capture, end.wheel_capture
    if first is None or last is None or start.sample.context != end.sample.context:
        return None
    a, b = first.progress, last.progress
    if a.segment != b.segment or a.profile != b.profile:
        return None
    steps = abs(a.observation.sequence - b.observation.sequence)
    travel = abs(a.travel_m - b.travel_m)
    wheel_travel = abs(a.wheel_travel_m - b.wheel_travel_m)
    model = a.profile
    tick = model.metres_per_tick_upper
    # Gateway의 거리/방향 오차율은 각각의 전체 보정 오차율이다. tick 상한은 양자화에 사용한다.
    travelled = wheel_travel / 2
    yaw = model.heading_error_per_m * travelled + (2 * tick / model.track_width_m if steps else 0.)
    yaw_roundoff = abs(a.yaw_roundoff_rad - b.yaw_roundoff_rad)
    yaw += yaw_roundoff
    # 연속 카운터의 오차 차분에 호의 평균 방향을 곱해 부분합하면, 양끝과 방향
    # 벡터의 변화량만 남는다. 각 차분을 따로 더한 상한도 사용할 수 있다.
    turn_travel = abs(a.turn_travel_rad - b.turn_travel_rad) + yaw_roundoff
    quantization = min(tick * steps, tick * (1 + turn_travel / 2))
    distance_error = model.distance_error_ratio * travelled
    position = distance_error + quantization
    position += abs(a.position_roundoff_m - b.position_roundoff_m)
    position += 2 * (travel + distance_error + tick*steps) * math.sin(min(math.pi, yaw) / 2)
    pads = first.position_pad_m + last.position_pad_m
    displacement = math.hypot(end.sample.x - start.sample.x, end.sample.y - start.sample.y) + pads
    position += pads + 2 * displacement * math.sin(min(math.pi, first.yaw_pad_rad) / 2)
    yaw += first.yaw_pad_rad + last.yaw_pad_rad
    # Prefix 차분과 float32 원 자세 변환에서 생기는 수치 오차를 덮는다.
    guard = 2**-21 * max(1., abs(start.sample.x), abs(start.sample.y), abs(end.sample.x), abs(end.sample.y))
    return position + guard, min(math.pi, yaw + 2**-20)
