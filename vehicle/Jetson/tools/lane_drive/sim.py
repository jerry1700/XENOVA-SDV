"""트랙과 차를 간단한 모형으로 두고 Driver 를 돌려 본다. 카메라가 보는 범위만 차선 표시로 넘긴다.

트랙은 긴 직선 둘과 왼쪽 90도 모서리 넷이고 차는 반시계 방향으로 돈다. 좌표의 x 는 첫 긴 직선의 방향이다.

  python -m tools.lane_drive.sim [--short 0.46] [--laps 1] [--gain 1.0] [--bias-deg 0] [--yaw-scale 1.0] [--change 0.3]
"""
import argparse
import math

from .driver import Driver
from .ground import Mark
from .lane import LANE_M, WHEELBASE_M
from .path import DIVIDER_ARC_M

LONG_M, SHORT_M = 1.51, 0.46
DASH_PITCH_M = 0.13
LENS_X_M, NEAR_X_M, FAR_X_M, HALF_FOV = 0.294, 0.63, 2.0, math.radians(34.5)
HALF_WIDTH_M, FRONT_M, REAR_M = 0.115, 0.30, 0.10
WHEELS = tuple((dx, dy) for dx in (WHEELBASE_M, 0.) for dy in (HALF_WIDTH_M, -HALF_WIDTH_M))  # 선을 밟는지는 네 바퀴로 본다


def divider(short_m, step=0.005):
    """가운데 점선을 따라 한 바퀴 도는 (x, y, 진행 방향) 목록."""
    points = []
    position, heading = (0., 0.), 0.
    for length in (LONG_M, short_m, LONG_M, short_m):
        for index in range(int(length / step)):
            points.append((position[0] + math.cos(heading) * index * step, position[1] + math.sin(heading) * index * step, heading))
        position = (position[0] + math.cos(heading) * length, position[1] + math.sin(heading) * length)
        center = (position[0] - math.sin(heading) * DIVIDER_ARC_M, position[1] + math.cos(heading) * DIVIDER_ARC_M)
        steps = int(DIVIDER_ARC_M * math.pi / 2 / step)
        for index in range(steps):
            angle = heading - math.pi / 2 + math.pi / 2 * index / steps
            points.append((center[0] + DIVIDER_ARC_M * math.cos(angle), center[1] + DIVIDER_ARC_M * math.sin(angle),
                           angle + math.pi / 2))
        heading += math.pi / 2
        position = (center[0] + DIVIDER_ARC_M * math.cos(heading - math.pi / 2),
                    center[1] + DIVIDER_ARC_M * math.sin(heading - math.pi / 2))
    return points


def offset(line, distance):
    """진행 방향의 왼쪽으로 distance 만큼 옮긴 선."""
    return [(x - math.sin(h) * distance, y + math.cos(h) * distance) for x, y, h in line]


class Track:
    def __init__(self, short_m):
        self.center = divider(short_m)
        self.outer = offset(self.center, -LANE_M['right'])[::6]
        self.inner = offset(self.center, LANE_M['left'])[::6]
        pitch = int(DASH_PITCH_M / 0.005)
        self.dashes = [(x, y) for x, y, _ in self.center[pitch // 2::pitch]]

    def marks(self, pose):
        """차의 실제 자세에서 카메라에 보이는 표시."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)

        def body(point):
            dx, dy = point[0] - x0, point[1] - y0
            return c * dx + s * dy, -s * dx + c * dy

        def visible(point):
            return NEAR_X_M <= point[0] <= FAR_X_M and abs(point[1]) <= (point[0] - LENS_X_M) * math.tan(HALF_FOV)

        result = [Mark('dash', (point,), 0.085) for point in map(body, self.dashes) if visible(point)]
        for line in (self.outer, self.inner):
            run = []
            for point in [*map(body, line), None]:
                if point is not None and visible(point) and (not run or math.dist(run[-1], point) < 0.06):
                    run.append(point)
                    continue
                if len(run) >= 8:
                    result.append(Mark('solid', tuple(run), sum(math.dist(a, b) for a, b in zip(run, run[1:]))))
                run = [point] if point is not None and visible(point) else []
        return tuple(result)

    def clearance(self, pose):
        """네 바퀴가 바깥 실선 안쪽·섬 바깥쪽에 얼마나 남는지 (바깥 여유, 섬 여유). 음수면 넘은 것이다."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        corners = [(x0 + c * dx - s * dy, y0 + s * dx + c * dy) for dx, dy in WHEELS]
        outside = inside = math.inf
        for corner in corners:
            px, py, heading = min(self.center, key=lambda p: (p[0] - corner[0]) ** 2 + (p[1] - corner[1]) ** 2)
            lateral = -math.sin(heading) * (corner[0] - px) + math.cos(heading) * (corner[1] - py)  # 왼쪽이 +
            outside = min(outside, lateral + LANE_M['right'])
            inside = min(inside, LANE_M['left'] - lateral)
        return outside, inside

    def over_divider(self, pose):
        """바퀴가 뒤 차축이 있는 차로에서 가운데 점선을 얼마나 넘었는지. 음수면 안 넘은 것이다."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        sides = []
        for dx, dy in WHEELS:
            corner = (x0 + c * dx - s * dy, y0 + s * dx + c * dy)
            px, py, heading = min(self.center, key=lambda p: (p[0] - corner[0]) ** 2 + (p[1] - corner[1]) ** 2)
            sides.append(-math.sin(heading) * (corner[0] - px) + math.cos(heading) * (corner[1] - py))
        return max(sides) if self.lane_offset(pose) < 0 else -min(sides)

    def obstacles(self, pose, items):
        """차의 실제 자세에서 깊이 카메라가 본 것으로 친 차로별 가장 가까운 물체 거리. items 는 (x, y, 반경)."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        result = {'right': None, 'left': None}
        for ox, oy, radius in items:
            dx, dy = ox - x0, oy - y0
            bx, by = c * dx + s * dy, -s * dx + c * dy
            near = bx - radius
            if not 0.65 <= near <= 1.6 or abs(by) > (bx - LENS_X_M) * math.tan(HALF_FOV) + radius:
                continue
            px, py, heading = min(self.center, key=lambda p: (p[0] - ox) ** 2 + (p[1] - oy) ** 2)
            lateral = -math.sin(heading) * (ox - px) + math.cos(heading) * (oy - py)
            lane = 'left' if lateral > 0 else 'right'
            if result[lane] is None or near < result[lane]:
                result[lane] = near
        return result

    def front(self, pose, items, steer, path=None):
        """차가 지나갈 폭(path 가 있으면 그 경로, 없으면 지금 조향각의 호) 안에서 가장 가까운 물체까지의 거리. 깊이 카메라의 시야 안만 본다."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        best = None
        for ox, oy, radius in items:
            dx, dy = ox - x0, oy - y0
            bx, by = c * dx + s * dy, -s * dx + c * dy
            if bx - radius < 0.65 or abs(by) > (bx - LENS_X_M) * math.tan(HALF_FOV) + radius:
                continue
            if path is not None and len(path) >= 2:
                index = min(range(len(path)), key=lambda i: (path[i][0] - bx) ** 2 + (path[i][1] - by) ** 2)
                away = math.hypot(path[index][0] - bx, path[index][1] - by)
                near = math.hypot(*path[0]) + index * 0.02 - radius
            elif abs(steer) < 0.02:
                away, near = abs(by), bx - radius
            else:
                turn = WHEELBASE_M / math.tan(steer)
                side = (turn - by) * (1. if turn > 0 else -1.)
                away, near = abs(math.hypot(bx, side) - abs(turn)), abs(turn) * math.atan2(bx, side) - radius
            if away <= HALF_WIDTH_M + 0.03 + radius and (best is None or near < best):
                best = near
        return best

    def gap(self, pose, items):
        """차체와 물체 사이의 가장 좁은 틈. 음수면 닿은 것이다."""
        x0, y0, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        best = math.inf
        for ox, oy, radius in items:
            dx, dy = ox - x0, oy - y0
            bx, by = c * dx + s * dy, -s * dx + c * dy
            nx = min(max(bx, -REAR_M), FRONT_M)
            ny = min(max(by, -HALF_WIDTH_M), HALF_WIDTH_M)
            best = min(best, math.hypot(bx - nx, by - ny) - radius)
        return best

    def place(self, along_m, lane, radius):
        """첫 긴 직선의 시작에서 along_m 만큼 간 곳의 lane 차로 가운데에 물체를 둔다. 한 바퀴를 넘으면 그만큼 돈 곳이다."""
        index = int(along_m / 0.005) % len(self.center)
        px, py, heading = self.center[index]
        lateral = LANE_M['left'] / 2 if lane == 'left' else -LANE_M['right'] / 2
        return px - math.sin(heading) * lateral, py + math.cos(heading) * lateral, radius

    def lane_offset(self, pose):
        """뒤 차축이 가운데 점선에서 왼쪽으로 얼마나 떨어져 있는지."""
        px, py, heading = min(self.center, key=lambda p: (p[0] - pose[0]) ** 2 + (p[1] - pose[1]) ** 2)
        return -math.sin(heading) * (pose[0] - px) + math.cos(heading) * (pose[1] - py)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--short', type=float, default=SHORT_M, help='짧은 변의 직선 길이')
    parser.add_argument('--know-short', action='store_true', help='Driver 에 짧은 변 길이를 알려 준다')
    parser.add_argument('--laps', type=float, default=1.0)
    parser.add_argument('--gain', type=float, default=1.0, help='실제 조향 / 명령 조향')
    parser.add_argument('--bias-deg', type=float, default=0.0, help='조향 가운데의 치우침')
    parser.add_argument('--yaw-scale', type=float, default=1.0, help='엔코더가 보는 회전 / 실제 회전')
    parser.add_argument('--inset', type=float, default=0.04)
    parser.add_argument('--change', type=float, help='긴 직선에서 이만큼 달린 뒤 옆 차로로 옮긴다')
    parser.add_argument('--obstacle', action='append', default=[], metavar='거리:차로',
                        help='가운데 점선을 따라 잰 거리와 차로(right/left)에 반경 5 cm 물체를 둔다. 여러 번 줄 수 있다')
    parser.add_argument('--keep-right', action='store_true', help='안쪽 차로에서 모서리를 돌고 나온 직선에서 바깥 차로로 돌아간다')
    parser.add_argument('--no-anchor', action='store_true', help='모서리를 도는 동안 보이는 선으로 경로를 바로잡지 않는다')
    parser.add_argument('--end-error', type=float, default=0., help='알려 주는 첫 직선 끝을 이만큼 멀리 잡는다')
    parser.add_argument('--stay-inner', action='store_true', help='안쪽 차로에서 모서리를 만나면 안쪽 차로를 그대로 돈다')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--trace', type=float, help='이 거리마다 차로 가운데에서 벗어난 양과 조향을 적는다')
    args = parser.parse_args()
    track = Track(args.short)
    driver = Driver(short_m=args.short if args.know_short else None, inset_m=args.inset,
                    long_m=LONG_M if args.know_short else None,
                    first_left_m=LONG_M - 0.05 + args.end_error if args.know_short else None,
                    stay_inner=args.stay_inner, anchor=not args.no_anchor, keep_right=args.keep_right)
    items = [track.place(float(text.split(':')[0]), text.split(':')[1], 0.05) for text in args.obstacle]
    closest = math.inf
    pose = [0.05, -LANE_M['right'] / 2, 0.]
    odometry = [0., 0., 0.]
    perimeter = 2 * (LONG_M + args.short) + 2 * math.pi * (DIVIDER_ARC_M + LANE_M['right'] / 2)
    travelled = now = 0.
    actual = 0.
    worst = [math.inf, math.inf]
    over = -math.inf
    where = {}
    changed = False
    stopped_at = None
    period, tick = 0.09, 0.01
    while travelled < args.laps * perimeter and now < 400:
        mmps, steer = driver.step(now, track.marks(pose), tuple(odometry),
                                  track.obstacles(pose, items) if items else None,
                                  track.front(pose, items, driver.steer) if items and driver.state != 'path' else None)
        if driver.note and not args.quiet:
            print('%6.1fs %.2f m  %s' % (now, travelled, driver.note))
        driver.note = None
        if args.change is not None and not changed and travelled >= args.change:
            changed = driver.change_lane(now, tuple(odometry))
        if mmps == 0:
            stopped_at = stopped_at if stopped_at is not None else now
            if now - stopped_at > 3:
                print('멈춤: %.1f초, %.2f m, 상태 %s, 점선에서 %+.3f m' % (now, travelled, driver.state, track.lane_offset(pose)))
                break
        else:
            stopped_at = None
        for _ in range(int(period / tick)):
            target = max(-0.49, min(0.49, steer * args.gain + math.radians(args.bias_deg)))
            actual += max(-0.03, min(0.03, target - actual))  # 조향이 따라오는 속도 3 rad/s
            distance = mmps / 1000 * tick
            turn = distance * math.tan(actual) / WHEELBASE_M
            pose[2] += turn
            pose[0] += distance * math.cos(pose[2])
            pose[1] += distance * math.sin(pose[2])
            odometry[2] += turn * args.yaw_scale
            odometry[0] += distance * math.cos(odometry[2])
            odometry[1] += distance * math.sin(odometry[2])
            travelled += distance
        now += period
        outside, inside = track.clearance(pose)
        if args.trace and int(travelled / args.trace) != int((travelled - mmps / 1000 * period) / args.trace):
            print('  %5.2f m %-6s 뒤 차축이 바깥 차로 가운데에서 안쪽으로 %+.3f m, 조향 %+5.1f도, 바깥 여유 %+.3f, 점선 넘음 %+.3f' % (
                travelled, driver.state if driver.state == 'follow' else driver.path_kind,
                track.lane_offset(pose) + LANE_M['right'] / 2, math.degrees(actual), outside, track.over_divider(pose)))
        if outside < worst[0]:
            where['바깥 실선'] = '%.2f m %s' % (travelled, driver.state if driver.state == 'follow' else driver.path_kind)
        worst = [min(worst[0], outside), min(worst[1], inside)]
        if not (driver.state == 'path' and driver.path_kind == 'change') and track.over_divider(pose) > over:
            over = track.over_divider(pose)
            where['점선'] = '%.2f m %s' % (travelled, driver.state if driver.state == 'follow' else driver.path_kind)
        if items:
            closest = min(closest, track.gap(pose, items))
    print('끝: %.1f초, %.2f m (한 바퀴 %.2f m), 바깥 실선 여유 최소 %+.3f m, 섬 여유 최소 %+.3f m, 점선 넘은 양 최대 %+.3f m, '
          '마지막 점선에서 %+.3f m%s' % (
        now, travelled, perimeter, worst[0], worst[1], over, track.lane_offset(pose),
        ', 물체와의 틈 최소 %+.3f m' % closest if items else ''))
    print('가장 나빴던 곳: ' + ', '.join('%s %s' % item for item in where.items()))


if __name__ == '__main__':
    main()
