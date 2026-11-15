"""엔코더 좌표에 모서리·차로 변경 경로를 그리고, 그 경로를 따라가는 조향각을 구한다."""

import math

from .lane import LANE_M, MAX_STEER_RAD, WHEELBASE_M

DIVIDER_ARC_M = 0.47  # 모서리에서 가운데 점선의 반경
STEP_M = 0.02
INLIER_M = 0.025
CURVE_SIDE_M = (0.035, 0.26)  # 직선에서 왼쪽으로 이만큼 벗어난 조각을 호 위의 조각으로 본다
CHANGE_SHORT_M = 0.025   # 옆 차로 가운데보다 이만큼 덜 옮긴다. 따라가다 넘치는 만큼이다
CHANGE_SETTLE_M = 0.1    # 차로를 옮긴 뒤 곧게 가는 거리
RAMP_M = 0.4            # 모서리 앞뒤에서 안쪽으로 당겼다 되돌리는 구간
CHANGE_RADIUS_M = 0.55  # 차로 변경의 두 호의 반경. 진행 거리는 약 0.74 m 가 된다


def straight_end(marks, straight):
    """직선 차로가 끝나는 곳의 x(차체 기준). 왼쪽으로 휘기 시작한 점선 조각이 보일 때만 값을 준다.

    호 위의 조각은 직선에서 r(1 − cos θ) 만큼 벗어나므로, 벗어난 양에서 호가 시작한 곳을 되짚는다.
    """
    slope = math.tan(straight.heading_rad)
    scale = math.hypot(1., slope)
    c, s = math.cos(straight.heading_rad), math.sin(straight.heading_rad)
    rows = []
    for mark in marks:
        if mark.kind != 'dash':
            continue
        x, y = mark.points[0]
        if 0.6 <= x <= 1.6:
            # 차로 방향으로 잰 거리와 직선에서 왼쪽으로 벗어난 양
            rows.append((c * x + s * (y - straight.divider_m), (y - straight.divider_m - slope * x) / scale))
    on_line = [along for along, side in rows if abs(side) <= INLIER_M]
    if len(on_line) < 2:
        return None
    last = max(on_line)
    curved = [(along, side) for along, side in rows
              if CURVE_SIDE_M[0] <= side <= CURVE_SIDE_M[1] and last - 0.02 <= along <= last + 0.45]
    if not curved:
        return None
    along, side = min(curved)
    end = along - DIVIDER_ARC_M * math.sin(math.acos(1 - side / DIVIDER_ARC_M))
    # 직선 위에 있다고 본 마지막 조각보다 한참 앞에서 끝났다고 나오면 잘못 본 것이다. 끝 가까이의 조각 한둘은 안 보일 수 있다
    return end if last - 0.2 <= end <= last + 0.2 else None


def change_length(extra_m=0.):
    """차로 변경 경로가 차로 방향으로 차지하는 길이."""
    shift = (LANE_M['right'] + LANE_M['left']) / 2 - CHANGE_SHORT_M + extra_m
    turn = math.acos(1 - shift / (2 * CHANGE_RADIUS_M))
    return 2 * CHANGE_RADIUS_M * math.sin(turn) + CHANGE_SETTLE_M


def _to_odometry(origin, direction, points):
    """차로 좌표(원점은 차 옆의 차로 가운데, x 는 차로 방향)의 점을 엔코더 좌표로 옮긴다."""
    c, s = math.cos(direction), math.sin(direction)
    return tuple((origin[0] + c * px - s * py, origin[1] + s * px + c * py) for px, py in points)


def _line(points, start, heading, length):
    c, s = math.cos(heading), math.sin(heading)
    distance = STEP_M
    while distance <= length + 1e-9:
        points.append((start[0] + c * distance, start[1] + s * distance))
        distance += STEP_M
    return (start[0] + c * length, start[1] + s * length)


def _slant(points, start, heading, length, lateral):
    """length 만큼 나아가면서 왼쪽으로 lateral 만큼 고르게 옮긴다. 끝점을 돌려준다."""
    c, s = math.cos(heading), math.sin(heading)
    steps = int(length / STEP_M)
    for index in range(1, steps + 1):
        along, side = length * index / steps, lateral * index / steps
        points.append((start[0] + c * along - s * side, start[1] + s * along + c * side))
    return points[-1] if steps else start


def _left_arc(points, start, heading, radius):
    """start 에서 heading 방향으로 달리다 왼쪽으로 90도 도는 호. 끝점을 돌려준다."""
    center = (start[0] - math.sin(heading) * radius, start[1] + math.cos(heading) * radius)
    steps = max(2, int(radius * math.pi / 2 / STEP_M))
    for index in range(1, steps + 1):
        angle = heading - math.pi / 2 + math.pi / 2 * index / steps
        points.append((center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)))
    return points[-1]


def corner_path(origin, direction, end_m, lane, *, exit_lane=None, radius_m=None, after_m=0.4, short_m=None,
                inset_m=0.04):
    """엔코더 좌표의 점 목록: 직선 → 왼쪽 90도 호 → (짧은 직선 → 호) → 다음 직선 after_m.

    origin 은 차 옆의 lane 차로 가운데 점, direction 은 차로 방향, end_m 은 거기서 직선 끝까지의 거리다.
    exit_lane 을 안 주면 들어간 차로를 그대로 따라 돈다(호의 중심이 트랙 모서리의 중심이다).
    다른 차로로 나오게 하면 반경 radius_m 의 호를 나오는 차로에 닿게 놓는다.
    바깥 차로에서는 차로 가운데보다 inset_m 만큼 안쪽을 돈다. 앞 범퍼의 바깥 모서리가 바깥 실선 쪽으로 밀려나는 만큼을 안쪽과 나눈다.
    short_m 을 주면 짧은 변의 모서리까지 이어서 돈다.
    """
    exit_lane = exit_lane or lane
    center = {'right': DIVIDER_ARC_M + LANE_M['right'] / 2, 'left': DIVIDER_ARC_M - LANE_M['left'] / 2}
    lead = inset_m if lane == 'right' else 0.
    tail = inset_m if exit_lane == 'right' else 0.
    if exit_lane == lane or radius_m is None:
        radius = center[lane] - lead
    else:
        radius = radius_m if short_m is None else min(
            radius_m, (center[lane] + center[exit_lane] + max(0., short_m) - lead - tail) / 2)
    arc_start = max(0., end_m + center[exit_lane] - tail - radius)
    ramp = min(RAMP_M, arc_start)
    points = [(0., 0.)]
    at = _line(points, (0., 0.), 0., arc_start - ramp)
    at = _slant(points, at, 0., ramp, lead)
    at = _left_arc(points, at, 0., radius)
    heading = math.pi / 2
    if short_m is not None:
        at = _line(points, at, heading, max(0., short_m + center[lane] + center[exit_lane] - lead - tail - 2 * radius))
        at = _left_arc(points, at, heading, radius)
        heading = math.pi
    back = min(RAMP_M, after_m)
    at = _slant(points, at, heading, back, -tail)
    _line(points, at, heading, after_m - back)
    return _to_odometry(origin, direction, points)


def change_path(origin, direction, lane, target, extra_m=0.):
    """지금 차로 가운데(origin, direction)에서 옆 차로 가운데로 옮겨 가는 경로. extra_m 만큼 더 멀리 옮긴다."""
    shift = (LANE_M[lane] + LANE_M[target]) / 2 - CHANGE_SHORT_M + extra_m
    side = 1. if target == 'left' else -1.
    # 반경이 같은 두 호를 이어 붙인다. 호 하나가 옆으로 shift/2 만큼 옮긴다.
    turn = math.acos(1 - shift / (2 * CHANGE_RADIUS_M))
    steps = max(2, int(CHANGE_RADIUS_M * turn / STEP_M))
    half = (CHANGE_RADIUS_M * math.sin(turn), CHANGE_RADIUS_M * (1 - math.cos(turn)))
    points = [(0., 0.)]
    for index in range(1, steps + 1):
        angle = turn * index / steps
        points.append((CHANGE_RADIUS_M * math.sin(angle), side * CHANGE_RADIUS_M * (1 - math.cos(angle))))
    for index in range(1, steps + 1):
        angle = turn * (1 - index / steps)
        points.append((2 * half[0] - CHANGE_RADIUS_M * math.sin(angle),
                       side * (shift - CHANGE_RADIUS_M * (1 - math.cos(angle)))))
    _line(points, points[-1], 0., CHANGE_SETTLE_M)
    return _to_odometry(origin, direction, points)


def pursue_path(pose, path, lookahead_m):
    """(조향각, 남은 거리). 경로에서 차에 가장 가까운 점보다 lookahead 앞의 점을 향한다."""
    x, y, yaw = pose
    nearest = min(range(len(path)), key=lambda index: (path[index][0] - x) ** 2 + (path[index][1] - y) ** 2)
    target = nearest
    while target < len(path) - 1 and math.hypot(path[target][0] - x, path[target][1] - y) < lookahead_m:
        target += 1
    dx, dy = path[target][0] - x, path[target][1] - y
    local_x = math.cos(yaw) * dx + math.sin(yaw) * dy
    local_y = -math.sin(yaw) * dx + math.cos(yaw) * dy
    span = local_x * local_x + local_y * local_y
    steer = 0. if span < 1e-6 else math.atan(2 * WHEELBASE_M * local_y / span)
    return max(-MAX_STEER_RAD, min(MAX_STEER_RAD, steer)), (len(path) - 1 - nearest) * STEP_M
