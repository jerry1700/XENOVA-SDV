"""깊이 영상에서 바닥보다 높은 점을 찾아 차로마다 가장 가까운 장애물까지의 거리를 구한다.

좌표는 차체 기준이다(x 앞, y 왼쪽, z 위, 뒤 차축 가운데의 바닥이 원점, 단위 m).
"""

import math

from collections import deque

from .lane import LANE_M, WHEELBASE_M

STRIDE = 2
HEIGHT_M = (0.07, 0.30)   # 바닥의 깊이 오차보다 높고 차보다 낮은 범위만 본다
AHEAD_M = (0.35, 1.6)    # 깊이 값은 뒤 차축에서 0.6 m 쯤부터 나온다
BIN_M = 0.05
MIN_POINTS = 25           # 5 cm 구간에 이만큼의 점이 있어야 물체로 본다
EDGE_M = 0.04             # 차선 표시 가까이의 점은 어느 차로에도 넣지 않는다. 넓은 물체의 가장자리가 부풀어 옆 차로로 넘어와 보인다
CORRIDOR_M = 0.145        # 차 폭의 절반에 3 cm 를 더한 값
PATH_CORRIDOR_M = 0.125   # 경로를 따라 물체 옆을 지날 때의 폭. 깊이 영상의 물체는 가장자리가 2~3 cm 부풀어 보인다
KEEP = 4                  # 최근 이만큼의 장 가운데
AGREE = 3                 # 이만큼에서 보인 물체만 쓴다. 가까운 곳에 한두 장씩 헛것이 생긴다
SAME_M = 0.15
TALL_M = 0.035            # 한 구간의 점들이 높이로 이만큼은 퍼져 있어야 한다. 점선 무늬를 잘못 맞춘 깊이는 한 높이의 얇은 띠로 나온다


class DepthView:
    """깊이 카메라 한 벌의 화소마다 차체 기준의 시선 방향을 미리 구해 둔다."""

    def __init__(self, camera):
        import numpy as np

        self.camera = camera
        k = np.asarray(camera.k, dtype=np.float64)
        v, u = np.mgrid[0:camera.height:STRIDE, 0:camera.width:STRIDE].astype(np.float64)
        y = (v - k[1, 2]) / k[1, 1]
        rays = np.stack(((u - k[0, 2] - k[0, 1] * y) / k[0, 0], y, np.ones_like(y)), axis=-1)
        turn = np.asarray(camera.rotation, dtype=np.float64) @ np.asarray(camera.rectification, dtype=np.float64).T
        self.rays = rays @ turn.T
        self.origin = np.asarray(camera.translation, dtype=np.float64)

    def raised(self, depth_mm):
        """RAW16 깊이 한 장(height×width, mm)에서 바닥보다 높은 점의 (x, y, z) 배열."""
        import numpy as np

        z = depth_mm[::STRIDE, ::STRIDE].astype(np.float64) * .001
        valid = (z >= self.camera.near_m) & (z <= self.camera.far_m)
        points = self.origin + z[..., None] * self.rays
        keep = (valid & (points[..., 2] >= HEIGHT_M[0]) & (points[..., 2] <= HEIGHT_M[1])
                & (points[..., 0] >= AHEAD_M[0]) & (points[..., 0] <= AHEAD_M[1]))
        return points[keep]


def lane_obstacles(points, straight):
    """{'right': 거리 또는 None, 'left': …}. 거리는 뒤 차축에서 차로 방향으로 잰 가장 가까운 물체까지다."""
    import numpy as np

    result = {'right': None, 'left': None}
    if len(points) == 0:
        return result
    c, s = math.cos(straight.heading_rad), math.sin(straight.heading_rad)
    along = c * points[:, 0] + s * (points[:, 1] - straight.divider_m)
    side = -s * points[:, 0] + c * (points[:, 1] - straight.divider_m)  # 가운데 점선에서 왼쪽으로
    for lane, low, high in (('right', -LANE_M['right'] + EDGE_M, -EDGE_M), ('left', EDGE_M, LANE_M['left'] - EDGE_M)):
        inside = (side >= low) & (side <= high)
        result[lane] = _nearest(along[inside], points[inside, 2])
    return result


def on_track(points, straight):
    """두 차로의 바깥 선 안쪽에 있는 점만 남긴다. 트랙 밖과 가운데 섬의 물건은 버린다."""
    c, s = math.cos(straight.heading_rad), math.sin(straight.heading_rad)
    side = -s * points[:, 0] + c * (points[:, 1] - straight.divider_m)
    return points[(side >= -LANE_M['right'] + EDGE_M) & (side <= LANE_M['left'] - EDGE_M)]


def front_obstacle(points, steer_rad=0., path=None, lanes=None):
    """차가 지나갈 폭 안에서 뒤 차축부터 가장 가까운 물체까지의 거리(달리는 길을 따라 잰다). 없으면 None 이다.

    path(차체 기준의 경로 점들, 2 cm 간격)를 주면 그 경로를, 안 주면 지금 조향각으로 도는 호를 지나갈 길로 본다.
    lanes(직선 차로)를 주면 두 차로 안의 점만 본다.
    """
    import numpy as np

    if lanes is not None:
        points = on_track(points, lanes)
    if len(points) == 0:
        return None
    if path is not None and len(path) >= 2:
        line = np.asarray(path, dtype=np.float64)
        gaps = np.hypot(points[:, None, 0] - line[None, :, 0], points[:, None, 1] - line[None, :, 1])
        index = gaps.argmin(axis=1)
        inside = gaps[np.arange(len(points)), index] <= PATH_CORRIDOR_M
        start = float(np.hypot(line[0, 0], line[0, 1]))
        return _nearest(start + index[inside] * 0.02, points[inside, 2])
    if abs(steer_rad) < 0.02:
        inside = np.abs(points[:, 1]) <= CORRIDOR_M
        return _nearest(points[inside, 0], points[inside, 2])
    radius = WHEELBASE_M / math.tan(steer_rad)  # 왼쪽으로 돌면 +, 도는 중심은 (0, radius)
    side = (radius - points[:, 1]) * (1. if radius > 0 else -1.)
    inside = np.abs(np.hypot(points[:, 0], side) - abs(radius)) <= CORRIDOR_M
    return _nearest(abs(radius) * np.arctan2(points[inside, 0], side[inside]), points[inside, 2])


class Steady:
    """최근 몇 장에서 거듭 보인 거리만 내놓는다."""

    def __init__(self):
        self.recent = deque(maxlen=KEEP)

    def push(self, distance):
        self.recent.append(distance)
        seen = sorted(value for value in self.recent if value is not None)
        if distance is None or len(seen) < AGREE:
            return None
        middle = seen[len(seen) // 2]
        return middle if sum(abs(value - middle) <= SAME_M for value in seen) >= AGREE else None


def _nearest(along, height):
    import numpy as np

    keep = along >= AHEAD_M[0]
    along, height = along[keep], height[keep]
    if len(along) < MIN_POINTS:
        return None
    index = ((along - AHEAD_M[0]) / BIN_M).astype(int)
    counts = np.bincount(index)
    for filled in np.flatnonzero(counts >= MIN_POINTS):
        low, high = np.quantile(height[index == filled], (0.1, 0.9))
        if high - low >= TALL_M:
            return AHEAD_M[0] + float(filled) * BIN_M
    return None
