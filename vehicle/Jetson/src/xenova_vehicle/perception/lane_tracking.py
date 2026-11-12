"""관측된 차로 중심을 차량 자세로 지도에 옮겨 같은 차로를 추적한다."""

from dataclasses import dataclass
import math

from ..mapping.registration import transform_point
from .road import RoadResult
from .tape import TapeGraph


MAX_LANE_SECTIONS = 16


@dataclass(frozen=True)
class LaneSection:
    source_key: tuple
    captured_us: tuple[int, int]
    points: tuple
    error_m: float


@dataclass(frozen=True)
class LaneTrack:
    scope: tuple
    first_source_key: tuple
    sections: tuple[LaneSection, ...]
    side: str | None
    side_confirmed: bool

    @property
    def latest(self):
        return self.sections[-1]

    def summary(self, status, section=None):
        section = self.latest if section is None else section
        return dict(status=status, map_frame=self.scope[0], first_source_key=self.first_source_key,
                    source_key=section.source_key, captured_us=section.captured_us,
                    center_xy=section.points, error_m=section.error_m, mapped_sections=len(self.sections),
                    side=self.side if self.side_confirmed else None)


def _observed_centers(road, tape, profile):
    tips = {s.local_id: (s.start_kind, s.end_kind) for s in tape.strips if s.observed}
    centers = {}
    for h in road.hypotheses:
        inferred = any(h.inferred)
        # 도로 오차의 방향 항은 경계까지의 거리로 계산된다. 관측 두 경계의
        # 중점에는 그 절반을 적용하고, 반대편 가림 폭의 오차는 포함하지 않는다.
        max_width = max(math.dist(d, p) for edges in (h.left_outer, h.right_outer)
                        for d, p in zip(h.divider, edges))
        error = h.error_m - max_width * math.sin(h.yaw_error_rad) / 2
        if not all(h.strip_ids):
            error -= profile.missing_outer_width_error_m
        if error > profile.max_geometry_error_m:
            continue
        for index, points, name, opposite in ((0, h.left_center, 'LEFT', 'RIGHT'),
                                              (2, h.right_center, 'RIGHT', 'LEFT')):
            outer, divider = pair = (h.strip_ids[index], h.strip_ids[1])
            if not all(pair) or any(identity not in tips for ids in pair for identity in ids):
                continue
            # PARTIAL은 반대편 누락 또는 점선 보간을 포함할 수 있다. 여기서는 실제로
            # 관측된 두 단일 strip만 사용해 추정 구간이 차로 추적의 입력이 되는 것을 막는다.
            if inferred and any(len(ids) != 1 for ids in pair):
                continue
            side = None
            if road.status == 'VALID':
                side = name
            elif len(outer) == len(divider) == 1:
                outer_tips, divider_tips = tips[outer[0]], tips[divider[0]]
                if 'observed_paint_end' in divider_tips and outer_tips == ('FOV_clip', 'FOV_clip'):
                    side = name
                elif 'observed_paint_end' in outer_tips and divider_tips == ('FOV_clip', 'FOV_clip'):
                    side = opposite
            key = tuple(sorted(pair))
            if key not in centers or error < centers[key][1]:
                centers[key] = (points, error, side)
    return centers.values()


def _contains_vehicle(points, error, half_width):
    a, b = points[0], points[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    if not 0 < a[0] <= .85 or dx <= 0 or abs(dy) > .20 * dx:
        return False
    # 초기 획득은 차량과 방향이 가까운 관측에 한정한다. 이 검사는 관측 선을
    # 차량까지 연장해 지도에 넣지 않는다.
    return abs(a[1] - a[0] * dy / dx) + error < half_width


def _overlaps(points, reference, tolerance, min_run):
    if tolerance <= 0:
        return False
    segments = []
    for a, b in zip(reference, reference[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length > 0:
            segments.append((a, dx / length, dy / length, length))
    run = 0.
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not length:
            continue
        x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        supported = False
        for origin, ux, uy, extent in segments:
            rx, ry = x - origin[0], y - origin[1]
            if (0 <= rx * ux + ry * uy <= extent and abs(rx * uy - ry * ux) <= tolerance
                    and dx * ux + dy * uy >= .85 * length):
                supported = True
                break
        run = run + length if supported else 0.
        if run >= min_run:
            return True
    return False


def mapped_lane_ahead(track, pose, profile):
    """현재 위치와 방향이 실제 지도 조각에 걸릴 때만 원 관측을 재사용한다."""
    sample = pose.sample
    x, y = sample.x, sample.y
    fx, fy = math.cos(sample.yaw), math.sin(sample.yaw)
    half_width = profile.missing_outer_width_m / 2
    for section in reversed(track.sections):
        tolerance = half_width - section.error_m - sample.position_error_m
        if tolerance <= 0:
            continue
        segments = tuple(zip(section.points, section.points[1:]))
        total = sum(math.dist(a, b) for a, b in segments)
        remaining = 0.
        for a, b in reversed(segments):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if not length:
                continue
            ux, uy = dx / length, dy / length
            rx, ry = x - a[0], y - a[1]
            along = rx * ux + ry * uy
            ahead = length + remaining - along
            heading_error = math.acos(max(-1., min(1., ux * fx + uy * fy)))
            if (0 <= along <= length and ahead >= profile.min_usable_length_m + sample.position_error_m
                    and total - ahead >= sample.position_error_m and abs(rx * uy - ry * ux) < tolerance
                    and heading_error + sample.yaw_error_rad <= .5):
                return section
            remaining += length
    return None


def track_lane(previous, road, tape, pose, scope, profile):
    """지도 겹침으로 차로를 갱신한다. 공백 뒤에는 같은 쪽 경계와 차량 위치를 함께 확인한다."""
    if previous is not None and scope is not None and previous.scope != scope:
        previous = None
    waiting = 'UNAVAILABLE' if previous is None else 'HELD'
    if (pose is None or scope is None or type(road) is not RoadResult or type(tape) is not TapeGraph
            or not road.complete
            or not road.calibration_qualified or road.source_key is None or road.source_key != tape.source_key
            or previous is not None and road.source_key == previous.latest.source_key):
        return previous, waiting
    sample = pose.sample
    transform = sample.x, sample.y, sample.yaw
    half_width = profile.missing_outer_width_m / 2
    matches = []
    for points, error, side in _observed_centers(road, tape, profile):
        radius = max(math.hypot(*point) for point in points)
        yaw_pad = 2 * radius * math.sin(min(math.pi, sample.yaw_error_rad) / 2)
        bound = error + sample.position_error_m + yaw_pad
        if bound >= half_width:
            continue
        mapped = tuple(transform_point(point, transform) for point in points)
        overlap = False
        if previous is not None:
            if previous.side_confirmed and side is not None and side != previous.side:
                continue
            overlap = any(_overlaps(mapped, old.points, half_width - old.error_m - bound,
                                    profile.min_usable_length_m / 2)
                          for old in reversed(previous.sections))
        can_use_vehicle = (previous is None or previous.side_confirmed and side == previous.side)
        if overlap or can_use_vehicle and _contains_vehicle(points, bound, half_width):
            matches.append((mapped, bound, side, overlap))
            if len(matches) > 1:
                return previous, waiting
    if not matches:
        return previous, waiting
    points, error, side, overlap = matches[0]
    first = road.source_key if previous is None else previous.first_source_key
    side_confirmed = bool(previous is not None and (previous.side_confirmed
        or overlap and side is not None and side == previous.side))
    if previous is not None and (side is None or previous.side_confirmed):
        side = previous.side
    sections = () if previous is None else previous.sections
    if sections and sections[-1].points == points:
        sections = sections[:-1]
    if len(sections) == MAX_LANE_SECTIONS:
        # 가까운 선의 연속 관측으로 이미 멀리서 본 선을 밀어내지 않는다.
        fx, fy = math.cos(sample.yaw), math.sin(sample.yaw)
        retire = min(range(len(sections)), key=lambda i: max(x*fx + y*fy for x, y in sections[i].points))
        sections = sections[:retire] + sections[retire+1:]
    section = LaneSection(road.source_key, pose.captured_us, points, error)
    return LaneTrack(scope, first, (*sections, section), side, side_confirmed), 'TRACKED'
