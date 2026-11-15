"""차선 표시와 엔코더 자세를 받아 속도와 조향각을 정한다. 카메라·통신과는 떨어져 있어 모형으로 돌릴 수 있다.

긴 직선에서는 영상의 차로를 따라가고, 직선 끝이 가까워지면 왼쪽 모서리를 엔코더로 돈다. 트랙은 반시계 방향으로 돈다고 본다.
영상에서 본 차로와 직선 끝은 엔코더 좌표에 적어 두므로, 차로를 바꾼 직후처럼 차로가 안 보일 때도 경로를 세울 수 있다.
"""

from collections import deque
import math

from .lane import LANE_M, Straight, observe_straight, pursue, steady
from .path import DIVIDER_ARC_M, change_length, change_path, corner_path, pursue_path, straight_end

LOST_S = 0.5
FIX_M = 1.5           # 엔코더 좌표에 적어 둔 차로를 차가 이만큼 움직일 때까지 믿는다
PATH_LOOKAHEAD_M = 0.3
HANDOVER_M = 0.5      # 경로의 마지막 이만큼에서는 직선 차로가 보이면 바로 영상 추종으로 넘어간다
REJOIN_S = 3.0        # 넘어간 뒤 이 시간 동안은 차로와 많이 어긋나 있어도 받아들인다
PLAN_AT_M = 1.0       # 직선 끝이 이만큼 앞에 오면 모서리 경로를 세운다. 이보다 가까우면 직선 차로가 안 보인다
CHANGE_AFTER_M = 0.2  # 차로를 옮긴 뒤 모서리 호가 시작하기 전에 남아야 하는 직선
AVOID_RANGE_M = 1.3    # 내 차로의 물체가 이 안에 들어오면 피할지 정한다
AVOID_MIN_M = 0.95     # 물체가 이보다 가까우면 차로를 옮길 자리가 없다 (변경 0.82 + 앞 범퍼 0.30 − 옆으로 먼저 비키는 몫)
FRONT_STOP_M = 0.8     # 차가 지나갈 폭 안의 물체가 뒤 차축에서 이 안에 있으면 어느 상태에서든 선다. 깊이 값은 0.6 m 쯤부터만 나온다
HOLD_RANGE_M = 0.85    # 못 피하는 물체가 이 안에 들어오면 선다
OTHER_CLEAR_M = 0.9    # 옆 차로는 물체보다 이만큼 더 뒤까지 비어 있어야 한다
REJOIN_TURN_RAD = 0.45  # 모서리를 돌고 나와 잡은 차로의 방향이 경로가 끝나는 방향과 이보다 다르면 다른 줄을 본 것이다
REJOIN_SIDE_M = 0.2     # 경로를 따라온 차에서 그 차로 가운데가 이보다 멀면 다른 줄을 본 것이다
OFF_PATH_M = 0.2        # 경로에서 이보다 멀어지면 선다
CHECK_M = 0.05          # 엔코더 자세를 이만큼 움직일 때마다 살핀다
TIGHTEST_M = 0.3        # 차가 돌 수 있는 것보다 한참 작은 반경. 이보다 급하게 돈 것으로 읽히면 엔코더를 믿지 않는다
AVOID_EXTRA_M = 0.055   # 물체를 피할 때는 옆 차로 가운데보다 이만큼 더 비킨다. 차 옆면이 옆 차로의 바깥 선에 2 cm 까지 간다
STALL_S = 1.5           # 속도를 보내는데 이 시간 동안 엔코더 자세가 안 움직이면 바퀴가 막힌 것으로 보고 선다
STALL_M = 0.01
ANCHOR_GATE_M = 0.07    # 모서리의 선이 있어야 할 자리에서 이보다 먼 점은 그 선의 점으로 보지 않는다
ANCHOR_MIN = 6          # 이만큼의 점이 맞아야 경로를 옮긴다
ANCHOR_RIDGE = 2.0      # 점이 적거나 한쪽 방향으로만 보일 때 옮기는 양을 줄인다
ANCHOR_STEP_M = 0.005   # 한 장에 경로를 옮기는 최대 거리
ANCHOR_VIEW_M = (0.6, 1.5, 0.6)  # 이 범위(앞 최소, 앞 최대, 좌우)의 표시만 쓴다
EXIT_RADIUS_M = 0.65    # 안쪽 차로에서 들어가 바깥 차로로 나올 때의 호 반경


class Driver:
    def __init__(self, *, lane='right', mmps=120, corner_mmps=100, lookahead_m=0.6, after_m=0.4,
                 short_m=None, long_m=None, corners=True, inset_m=0.04, first_left_m=None, stay_inner=False, anchor=True,
                 keep_right=False):
        self.keep_right = keep_right  # 안쪽 차로에서 모서리를 돌고 나온 직선에서 바깥 차로가 비어 있으면 돌아간다
        self.cornered = True          # 마지막으로 물체를 피한 뒤 모서리를 돌았는지. 피한 물체 옆에서는 돌아가지 않는다
        self.anchor = anchor  # 모서리를 도는 동안 보이는 선으로 경로를 바로잡는다
        self.stay_inner = stay_inner  # 안쪽 차로에서 모서리를 만나면 안쪽 차로를 그대로 돈다. 아니면 바깥 차로로 나온다
        self.lane, self.mmps, self.corner_mmps = lane, mmps, corner_mmps
        self.lookahead_m, self.after_m = lookahead_m, after_m
        self.short_m, self.long_m, self.corners, self.inset_m = short_m, long_m, corners, inset_m
        self.first_left_m = first_left_m  # 출발할 때 뒤 차축에서 직선 끝까지의 거리. 알려 주면 첫 직선의 끝으로 쓴다
        self.recent = deque(maxlen=5)
        self.ends = deque(maxlen=7)   # 직선 끝의 엔코더 좌표
        self.fix = None               # (그때 차의 위치, 가운데 점선 위의 점, 차로 방향) — 엔코더 좌표
        self.model_end = None         # 트랙 치수로 따진 직선 끝의 엔코더 좌표. 모서리를 돌고 나온 뒤에만 안다
        self.path = self.path_kind = self.remaining = None
        self.seen = self.rejoin_until = self.exit_heading = None
        self.steer = 0.
        self.state = 'follow'
        self.note = None  # 방금 일어난 일을 한 번 알리는 글
        self.holding = False
        self.fault = None  # 더 달리면 안 되는 까닭. 한 번 생기면 풀리지 않는다
        self.lined_at = None  # 가운데 점선과 나란한 실선까지 함께 보인 마지막 시각
        self.moved_from = self.moved_at = None
        self.arcs = ()      # 도는 중인 모서리마다 (트랙 모서리의 중심, 들어가는 방향) — 엔코더 좌표
        self.shifted = 0.   # 이번 모서리 경로를 영상으로 옮긴 거리의 합
        self.checked = None

    def _trust(self, pose):
        """엔코더 자세가 차가 실제로 움직일 수 있는 범위 안에서 바뀌었는지 살핀다."""
        if self.checked is None:
            self.checked = pose
            return
        moved = math.hypot(pose[0] - self.checked[0], pose[1] - self.checked[1])
        turned = abs(pose[2] - self.checked[2])
        if turned > max(moved, CHECK_M) / TIGHTEST_M + 0.05:
            self.fault = self.note = '엔코더 자세가 %.2f m 에 %.0f도 돌았음. 한쪽 바퀴 값이 멈춘 것으로 보고 섬' % (
                moved, math.degrees(turned))
        elif moved >= CHECK_M:
            self.checked = pose

    def _center(self, pose, lane):
        """lane 차로 가운데 선에서 차에 가장 가까운 점."""
        _, (px, py), direction = self.fix
        half = LANE_M[lane] / 2 * (-1. if lane == 'right' else 1.)
        cx, cy = px - math.sin(direction) * half, py + math.cos(direction) * half
        along = (pose[0] - cx) * math.cos(direction) + (pose[1] - cy) * math.sin(direction)
        return cx + along * math.cos(direction), cy + along * math.sin(direction)

    def _fixed(self, pose):
        return (self.fix is not None and pose is not None
                and math.hypot(pose[0] - self.fix[0][0], pose[1] - self.fix[0][1]) <= FIX_M)

    def _expected(self, pose, straight):
        """모서리를 돌고 나와 넓게 찾은 차로가 경로가 끝나는 방향을 향하는지."""
        if self.exit_heading is None or pose is None:
            return True
        turn = (pose[2] + straight.heading_rad - self.exit_heading + math.pi) % (2 * math.pi) - math.pi
        return abs(turn) <= REJOIN_TURN_RAD

    def _end(self):
        if len(self.ends) >= 5:
            return (sorted(p[0] for p in self.ends)[len(self.ends) // 2],
                    sorted(p[1] for p in self.ends)[len(self.ends) // 2]), '영상'
        return (self.model_end, '트랙 치수') if self.model_end is not None else (None, None)

    def straight_left(self, pose):
        """뒤 차축에서 직선 끝까지 남은 거리. 직선 끝을 모르면 None 이다."""
        end, _ = self._end()
        if end is None or self.fix is None or pose is None:
            return None
        direction = self.fix[2]
        return (end[0] - pose[0]) * math.cos(direction) + (end[1] - pose[1]) * math.sin(direction)

    def _start(self, kind, path, note):
        self.path, self.path_kind, self.state, self.note = path, kind, 'path', note
        self.recent.clear()

    def _plan_corner(self, pose, left, source):
        center, direction = self._center(pose, self.lane), self.fix[2]
        # 가운데 점선은 들어가는 차로 가운데에서 왼쪽(바깥 차로) 또는 오른쪽(안쪽 차로)으로 반 차로 떨어져 있다
        side = LANE_M[self.lane] / 2 * (1. if self.lane == 'right' else -1.)
        c, s = math.cos(direction), math.sin(direction)
        turns = [(left, side + DIVIDER_ARC_M, direction)]
        if self.short_m is not None:
            turns.append((left, side + DIVIDER_ARC_M + self.short_m, direction + math.pi / 2))
        self.arcs = tuple(((center[0] + c * px - s * py, center[1] + s * px + c * py), heading)
                          for px, py, heading in turns)
        self.shifted = 0.
        exit_lane = self.lane if self.stay_inner else 'right'
        path = corner_path(center, direction, left, self.lane, exit_lane=exit_lane, radius_m=EXIT_RADIUS_M,
                           after_m=self.after_m, short_m=self.short_m, inset_m=self.inset_m)
        self._start('corner', path, '모서리 경로(%s, %s 차로에서): 직선 끝까지 %.2f m, 길이 %.2f m' % (
            source, self.lane, left, len(path) * 0.02))
        self.exit_heading = direction + (math.pi / 2 if self.short_m is None else math.pi)
        self.lane = exit_lane
        self.ends.clear()
        # 적어 둔 차로는 지나온 직선의 것이다. 모서리를 돌고 나면 쓰지 않는다
        self.model_end = self.fix = None

    def lanes(self, pose):
        """엔코더 좌표에 적어 둔 직선 차로를 지금 차체 기준으로 옮긴 것. 차로를 옮기는 중에도 쓸 수 있다. 모르면 None 이다."""
        if not self._fixed(pose):
            return None
        _, (px, py), direction = self.fix
        x, y, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        bx, by = c * (px - x) + s * (py - y), -s * (px - x) + c * (py - y)
        heading = (direction - yaw + math.pi) % (2 * math.pi) - math.pi
        if abs(heading) > 1.2:
            return None
        return Straight(by - math.tan(heading) * bx, heading, 0, 0, {})

    def lined(self, now):
        """방금 본 차로가 점선과 실선 둘로 확인된 것인지. 점선만으로 잡은 차로는 자리가 틀릴 수 있다."""
        return self.lined_at is not None and now - self.lined_at <= LOST_S

    def ahead(self, pose, reach_m=1.2):
        """경로를 따라가는 중이면 앞으로 지나갈 경로의 점들을 차체 기준으로 돌려준다. 아니면 None 이다."""
        if self.state != 'path' or pose is None:
            return None
        x, y, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        nearest = min(range(len(self.path)), key=lambda i: (self.path[i][0] - x) ** 2 + (self.path[i][1] - y) ** 2)
        return tuple((c * (px - x) + s * (py - y), -s * (px - x) + c * (py - y))
                     for px, py in self.path[nearest:nearest + int(reach_m / 0.02)])

    def _anchor(self, marks, pose):
        """모서리의 점선과 실선이 트랙 모서리의 중심에서 정해진 반경에 보이도록 경로를 조금 옮긴다.

        모서리를 도는 동안에는 내 차로가 안 보여 엔코더 좌표의 경로만 따라간다. 직선 끝을 잘못 봤거나 엔코더 자세가
        틀어진 만큼 경로가 트랙과 어긋나는데, 앞에 보이는 휘어진 선으로 그 어긋남을 줄인다.
        """
        x, y, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        near, far, wide = ANCHOR_VIEW_M
        rows = []
        for mark in marks:
            if mark.kind == 'dash':
                points, radii = mark.points[:1], (DIVIDER_ARC_M,)
            else:
                points, radii = mark.points[::3], (DIVIDER_ARC_M - LANE_M['left'], DIVIDER_ARC_M + LANE_M['right'])
            for bx, by in points:
                if not (near <= bx <= far and abs(by) <= wide):
                    continue
                px, py = x + c * bx - s * by, y + s * bx + c * by
                best = None
                for (cx, cy), heading in self.arcs:
                    vx, vy = px - cx, py - cy
                    # 호는 중심에서 볼 때 들어가는 방향의 앞쪽·오른쪽 사분면에 있다
                    if (vx * math.cos(heading) + vy * math.sin(heading) < -0.03
                            or -vx * math.sin(heading) + vy * math.cos(heading) > 0.03):
                        continue
                    distance = math.hypot(vx, vy)
                    for radius in radii:
                        gap = distance - radius
                        if abs(gap) <= ANCHOR_GATE_M and (best is None or abs(gap) < abs(best[2])):
                            best = (vx / distance, vy / distance, gap)
                if best is not None:
                    rows.append(best)
        if len(rows) < ANCHOR_MIN:
            return
        a11 = sum(u * u for u, _, _ in rows) + ANCHOR_RIDGE
        a22 = sum(v * v for _, v, _ in rows) + ANCHOR_RIDGE
        a12 = sum(u * v for u, v, _ in rows)
        b1, b2 = sum(u * gap for u, _, gap in rows), sum(v * gap for _, v, gap in rows)
        det = a11 * a22 - a12 * a12
        dx, dy = (a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det
        size = math.hypot(dx, dy)
        if size > ANCHOR_STEP_M:
            dx, dy = dx * ANCHOR_STEP_M / size, dy * ANCHOR_STEP_M / size
        self.path = tuple((px + dx, py + dy) for px, py in self.path)
        self.arcs = tuple(((cx + dx, cy + dy), heading) for (cx, cy), heading in self.arcs)
        self.shifted += math.hypot(dx, dy)

    def change_lane(self, now, pose, extra_m=0.):
        """지금 직선에서 옆 차로로 옮기는 경로를 세운다. 차로를 모르거나 직선이 모자라면 False 다."""
        if self.state != 'follow' or not self._fixed(pose):
            return False
        left = self.straight_left(pose)
        # 안쪽 차로에서 바깥으로 나오는 모서리 호가 직선 끝보다 얼마나 앞에서 시작하는지
        lead = 0. if self.stay_inner else max(0., EXIT_RADIUS_M - (DIVIDER_ARC_M + LANE_M['right'] / 2))
        if left is not None and left < change_length(extra_m) + CHANGE_AFTER_M + lead:
            return False
        target = 'left' if self.lane == 'right' else 'right'
        self._start('change', change_path(self._center(pose, self.lane), self.fix[2], self.lane, target, extra_m),
                    '차로 변경 → %s (직선 %s 남음)' % (target, '?' if left is None else '%.2f m' % left))
        self.lane = target
        return True

    def _avoid(self, now, pose, obstacles):
        """내 차로의 물체를 보고 차로를 옮기거나 서야 하면 True(서야 함)를 돌려준다."""
        left = self.straight_left(pose) if self._fixed(pose) else None
        # 직선 끝 너머로 보이는 것은 트랙 밖의 물체다
        reach = AVOID_RANGE_M if left is None else min(AVOID_RANGE_M, left + 0.1)
        ahead = obstacles.get(self.lane)
        if ahead is None or ahead > reach:
            return False
        other = obstacles.get('left' if self.lane == 'right' else 'right')
        if other is not None and left is not None and other > left + 0.1:
            other = None
        if (ahead >= AVOID_MIN_M and (other is None or other > ahead + OTHER_CLEAR_M)
                and self.change_lane(now, pose, AVOID_EXTRA_M)):
            self.note += ', 물체 %.2f m 앞' % ahead
            self.cornered = False
            return False
        return ahead <= HOLD_RANGE_M

    def step(self, now, marks, pose, obstacles=None, front=None):
        """(속도 mm/s, 조향 rad). pose 는 엔코더 자세 (x, y, yaw) 이고 없으면 None 이다. 바퀴가 막힌 채로 계속 밀지 않는다."""
        speed, steer = self._decide(now, marks, pose, obstacles, front)
        if speed <= 0 or pose is None:
            self.moved_from = None
        elif (self.moved_from is None
              or math.hypot(pose[0] - self.moved_from[0], pose[1] - self.moved_from[1]) > STALL_M):
            self.moved_from, self.moved_at = pose, now
        elif now - self.moved_at > STALL_S:
            self.fault = self.note = '속도를 보내는데 %.1f초 동안 바퀴가 돌지 않음, 섬' % STALL_S
            return 0, steer
        return speed, steer

    def _decide(self, now, marks, pose, obstacles, front):
        """(속도 mm/s, 조향 rad).

        obstacles 는 차로마다 가장 가까운 물체까지의 거리({'right': m 또는 None, 'left': …})이고,
        front 는 차가 지나갈 폭 안에서 뒤 차축부터 가장 가까운 물체까지의 거리다.
        """
        if pose is not None and self.fault is None:
            self._trust(pose)
        if self.fault is not None:
            return 0, self.steer
        wide = (self.state == 'path' and self.remaining is not None and self.remaining <= HANDOVER_M
                or self.rejoin_until is not None and now <= self.rejoin_until)
        straight = observe_straight(marks, wide=wide)
        if straight is not None and wide and not self._expected(pose, straight):
            straight = None
        if straight is not None:
            self.recent.append(straight)
            if straight.width_m:
                self.lined_at = now
        elif self.seen is not None and now - self.seen > LOST_S:
            self.recent.clear()
        lane = steady(self.recent) if len(self.recent) >= 3 else None
        if (lane is not None and straight is not None and pose is not None
                and not (self.state == 'path' and self.path_kind == 'corner')):
            x, y, yaw = pose
            self.fix = ((x, y), (x - math.sin(yaw) * lane.divider_m, y + math.cos(yaw) * lane.divider_m),
                        yaw + lane.heading_rad)
            if self.first_left_m is not None:
                self.model_end = (x + math.cos(self.fix[2]) * self.first_left_m, y + math.sin(self.fix[2]) * self.first_left_m)
                self.first_left_m = None
            end = straight_end(marks, lane)
            if end is not None:
                side = lane.divider_m + math.tan(lane.heading_rad) * end
                self.ends.append((x + math.cos(yaw) * end - math.sin(yaw) * side,
                                  y + math.sin(yaw) * end + math.cos(yaw) * side))

        if front is not None and front <= FRONT_STOP_M:
            if not self.holding:
                self.note = '바로 앞 물체 %.2f m, 섬' % front
            self.holding = True
            return 0, self.steer
        if self.state == 'path':
            if pose is None:
                return 0, self.steer
            if self.path_kind == 'corner' and self.anchor:
                self._anchor(marks, pose)
            self.steer, self.remaining = pursue_path(pose, self.path, PATH_LOOKAHEAD_M)
            away = min(math.hypot(px - pose[0], py - pose[1]) for px, py in self.path)
            if away > OFF_PATH_M:
                self.fault = self.note = '경로에서 %.2f m 벗어남, 섬' % away
                return 0, self.steer
            seen = (self.path_kind == 'corner' and self.remaining <= HANDOVER_M
                    and straight is not None and lane is not None
                    and abs(lane.center(self.lane)[0]) <= REJOIN_SIDE_M)
            if self.remaining > 0.04 and not seen:
                return self.corner_mmps, self.steer
            self.note = '%s 경로 끝(남은 %.2f m)' % ('모서리' if self.path_kind == 'corner' else '차로 변경', self.remaining)
            if self.path_kind == 'corner' and self.shifted:
                self.note += ', 영상으로 경로를 모두 %.2f m 옮김' % self.shifted
            if self.path_kind == 'corner':
                self.rejoin_until = now + REJOIN_S
                self.cornered = True
                if self.long_m is not None:
                    # 경로의 마지막 after_m 이 새 직선 위에 있다. 경로를 그린 좌표에서 직선 끝을 따진다
                    (ax, ay), (bx, by) = self.path[-2], self.path[-1]
                    scale = (self.long_m - self.after_m) / math.hypot(bx - ax, by - ay)
                    self.model_end = (bx + (bx - ax) * scale, by + (by - ay) * scale)
            self.state, self.path, self.remaining = 'follow', None, None
            self.seen = now

        if lane is not None and straight is not None:
            self.seen = now
            self.steer = pursue(lane, self.lane, self.lookahead_m)
        if self.corners and self._fixed(pose):
            left = self.straight_left(pose)
            if left is not None and left <= PLAN_AT_M:
                self._plan_corner(pose, left, self._end()[1])
                return self.corner_mmps, self.steer
        if (self.keep_right and self.lane == 'left' and self.state == 'follow' and self.cornered
                and obstacles is not None and self.straight_left(pose) is not None
                and (obstacles.get('right') is None or obstacles['right'] > change_length() + OTHER_CLEAR_M)
                and self.change_lane(now, pose)):
            self.note += ', 바깥 차로로 돌아감'
            return self.corner_mmps, self.steer
        if obstacles is not None and self._avoid(now, pose, obstacles):
            if self.note is None:
                self.note = '물체 앞에서 섬 (%.2f m)' % obstacles[self.lane] if not self.holding else None
            self.holding = True
            return 0, self.steer
        self.holding = False
        if self.state == 'path':
            return self.corner_mmps, self.steer
        visible = self.seen is not None and now - self.seen <= LOST_S
        return (self.mmps if visible else 0), self.steer
