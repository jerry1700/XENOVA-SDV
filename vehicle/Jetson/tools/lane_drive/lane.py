"""직선 구간에서 가운데 점선과 바깥 실선으로 차로의 방향과 가운데를 구하고 조향각을 정한다."""

from dataclasses import dataclass
import math

from .ground import fit_line

LANE_M = {'right': 0.285, 'left': 0.285}  # 바깥 실선이 안 보일 때 쓰는 차로 폭
LANE_RANGE_M = (0.24, 0.38)
WHEELBASE_M = 0.167
MAX_STEER_RAD = 0.49  # 2026-11-15 에 바닥에서 왼쪽 0.49 rad 로 반경 0.30 m 를 돌았다
PARALLEL_RAD = 0.12  # 점선과 실선의 방향이 이보다 다르면 같은 차로의 두 선으로 보지 않는다
MAX_HEADING_RAD = 0.35  # 차로 방향이 차와 이보다 다르면 직선 추종에 쓰지 않는다
NEAR_X_M = (0.6, 1.45)  # 이보다 먼 표시는 위치가 부정확하고 트랙 밖 물체가 섞인다
NEAR_Y_M = 0.45
DASH_INLIER_M = 0.012  # 넓게 잡으면 모서리로 휘기 시작한 조각이 섞여 점선이 치우친다
WIDE = 1.7  # 모서리를 돌고 나와 차로를 다시 잡을 때 좌우·방향 한도를 넓히는 배수


def _near(points, reach=1.):
    return tuple(p for p in points if NEAR_X_M[0] <= p[0] <= NEAR_X_M[1] and abs(p[1]) <= NEAR_Y_M * reach)


@dataclass(frozen=True)
class Straight:
    """차체 기준의 직선 차로. divider_m 은 x=0 에서 가운데 점선의 y, heading_rad 는 차로 방향(왼쪽이 +).

    width_m 은 그 장에서 바깥 실선이 보인 차로의 폭이고, 안 보였으면 None 이다.
    """

    divider_m: float
    heading_rad: float
    dashes: int
    solid_points: int
    width_m: dict

    def center(self, lane):
        """lane 이 'right' 면 점선 오른쪽 차로, 'left' 면 왼쪽 차로 가운데 선의 (x=0 에서의 y, 기울기)."""
        side = -1. if lane == 'right' else 1.
        slope = math.tan(self.heading_rad)
        width = self.width_m.get(lane) or LANE_M[lane]
        return self.divider_m + side * width / 2 * math.hypot(1., slope), slope


def observe_straight(marks, *, wide=False):
    """점선 조각과 실선에서 직선 차로를 구한다. 가운데 점선이 안 보이면 None 이다.

    wide 면 차가 차로와 많이 어긋나 있어도 받아들인다.
    """
    reach = WIDE if wide else 1.
    dashes = _near((mark.points[0] for mark in marks if mark.kind == 'dash'), reach)
    divider = fit_line(dashes, tolerance_m=DASH_INLIER_M)
    if divider is None or divider[2] < 3:
        return None
    offset, slope, count = divider
    heading = math.atan(slope)
    if abs(heading) > MAX_HEADING_RAD * reach or abs(offset) > 1.5 * LANE_RANGE_M[1] * reach:
        # 모서리에서 휘는 점선이나 다른 줄의 조각을 직선 차로로 쓰지 않는다.
        return None
    support = 0
    slopes = [(slope, count)]
    widths = {}
    for mark in marks:
        if mark.kind != 'solid':
            continue
        line = fit_line(_near(mark.points, reach))
        if line is None or line[2] < 4 or abs(math.atan(line[1]) - heading) > PARALLEL_RAD:
            continue
        # 점선에서 한 차로 폭 떨어진 실선만 그 차로의 바깥 선이다.
        gap = (line[0] - offset) * math.cos(heading)
        if not LANE_RANGE_M[0] <= abs(gap) <= LANE_RANGE_M[1]:
            continue
        lane = 'left' if gap > 0 else 'right'
        if line[2] > widths.get(lane, (0., 0))[1]:
            widths[lane] = (abs(gap), line[2])
        slopes.append((line[1], line[2]))
        support += line[2]
    weight = sum(points for _, points in slopes)
    return Straight(offset, math.atan(sum(value * points for value, points in slopes) / weight), count, support,
                    {lane: width for lane, (width, _) in widths.items()})


def steady(recent):
    """최근 관측 몇 개의 가운데 값으로 만든 직선 차로. 한두 장의 튀는 값을 버린다."""
    recent = tuple(recent)
    middle = len(recent) // 2
    newest = recent[-1]
    return Straight(sorted(item.divider_m for item in recent)[middle], sorted(item.heading_rad for item in recent)[middle],
                    newest.dashes, newest.solid_points, newest.width_m)


def pursue(straight, lane, lookahead_m):
    """차로 가운데 선 위의 앞쪽 점을 향하는 앞바퀴 각(rad, 왼쪽이 +)."""
    offset, slope = straight.center(lane)
    # 뒤 차축에서 lookahead 만큼 떨어진 가운데 선 위의 점
    a, b, c = 1 + slope * slope, 2 * offset * slope, offset * offset - lookahead_m * lookahead_m
    root = b * b - 4 * a * c
    if root < 0:
        x = max(0.05, -b / (2 * a))
    else:
        x = (-b + math.sqrt(root)) / (2 * a)
    y = offset + slope * x
    steer = math.atan(2 * WHEELBASE_M * y / (x * x + y * y))
    return max(-MAX_STEER_RAD, min(MAX_STEER_RAD, steer))
