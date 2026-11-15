"""카메라 영상을 바닥 격자로 펴서 흰 차선 표시를 점선 조각과 실선으로 나눈다.

좌표는 차체 기준이다(x 앞, y 왼쪽, 뒤 차축 가운데의 바닥이 원점, 단위 m).
"""

from dataclasses import dataclass
import math

CELL_M = 0.005
BLOCK_CELLS = 6  # 실선을 3 cm 간격의 점으로 줄인다
DASH_LENGTH_M = (0.02, 0.11)
SOLID_MIN_LENGTH_M = 0.20
MARK_MAX_WIDTH_M = 0.035
MIN_CELLS = 8


@dataclass(frozen=True)
class Mark:
    kind: str  # 'dash' 또는 'solid'
    points: tuple  # dash 는 중심 1점, solid 는 선을 따라 3 cm 간격
    length_m: float


class GroundView:
    """보정 한 벌에 대한 바닥 격자. 행은 x, 열은 y 이고 값은 흰색 여부다."""

    def __init__(self, calibration, tape_profile, *, value_min=None):
        from xenova_vehicle.perception.rgb_rectification import tape_rgb_geometry
        from xenova_vehicle.perception.tape import _birdview_map

        self.camera, self.maps, _ = tape_rgb_geometry(calibration.rgb)
        self.u, self.v, self.inside = _birdview_map(self.camera, calibration.plane, tape_profile)
        self.x0, self.y0 = tape_profile.x_min_m, tape_profile.y_min_m
        self.value_min = tape_profile.value_min if value_min is None else value_min
        self.saturation_max = tape_profile.saturation_max

    def xy(self, row, col):
        return self.x0 + (row + .5) * CELL_M, self.y0 + (col + .5) * CELL_M

    def white(self, nv12):
        """NV12 한 장(540×640 uint8)의 흰색 바닥 마스크."""
        import cv2
        import numpy as np

        bgr = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
        if self.maps is not None:
            bgr = cv2.remap(bgr, *self.maps, cv2.INTER_LINEAR)
        ground = cv2.remap(bgr, self.u, self.v, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        hsv = cv2.cvtColor(ground, cv2.COLOR_BGR2HSV)
        return ((hsv[..., 2] >= self.value_min) & (hsv[..., 1] <= self.saturation_max) & self.inside).astype(np.uint8)

    def marks(self, mask):
        """흰색 덩어리 가운데 테이프 폭인 것만 점선 조각·실선으로 돌려준다."""
        import cv2
        import numpy as np

        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        result = []
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < MIN_CELLS:
                continue
            left, top = int(stats[label, cv2.CC_STAT_LEFT]), int(stats[label, cv2.CC_STAT_TOP])
            width, height = int(stats[label, cv2.CC_STAT_WIDTH]), int(stats[label, cv2.CC_STAT_HEIGHT])
            rows, cols = np.nonzero(labels[top:top + height, left:left + width] == label)
            rows, cols = rows + top, cols + left
            (_, _), (side_a, side_b), _ = cv2.minAreaRect(np.stack((cols, rows), axis=1).astype(np.float32))
            length = (max(side_a, side_b) + 1) * CELL_M
            if area * CELL_M * CELL_M / length > MARK_MAX_WIDTH_M:
                continue
            if DASH_LENGTH_M[0] <= length <= DASH_LENGTH_M[1]:
                result.append(Mark('dash', (self.xy(float(rows.mean()), float(cols.mean())),), length))
            elif length >= SOLID_MIN_LENGTH_M:
                keys = (rows // BLOCK_CELLS) * 4096 + cols // BLOCK_CELLS
                order = np.argsort(keys, kind='stable')
                edges = np.flatnonzero(np.diff(keys[order])) + 1
                points = tuple(self.xy(float(part_rows.mean()), float(part_cols.mean()))
                               for part_rows, part_cols in zip(np.split(rows[order], edges), np.split(cols[order], edges)))
                result.append(Mark('solid', points, length))
        return tuple(result)


def fit_line(points, *, tolerance_m=0.02):
    """점들에 맞는 직선 y = offset + slope·x. 가장 많은 점이 따르는 직선을 고르고 그 점들로 다시 맞춘다.

    (offset, slope, 따르는 점 수) 를 돌려주고, 점이 둘보다 적으면 None 이다.
    """
    points = tuple(points)
    best, best_error = (), 0.
    for index, (x0, y0) in enumerate(points):
        for x1, y1 in points[index + 1:]:
            if abs(x1 - x0) < 0.05:
                continue
            slope = (y1 - y0) / (x1 - x0)
            offset = y0 - slope * x0
            scale = math.hypot(1., slope)
            errors = tuple(abs(p[1] - offset - slope * p[0]) / scale for p in points)
            inliers = tuple(p for p, error in zip(points, errors) if error <= tolerance_m)
            error = sum(e for e in errors if e <= tolerance_m)
            # 따르는 점 수가 같으면 더 곧게 놓인 쪽을 고른다
            if len(inliers) > len(best) or len(inliers) == len(best) and error < best_error:
                best, best_error = inliers, error
    if len(best) < 2:
        return None
    count = len(best)
    mean_x, mean_y = sum(p[0] for p in best) / count, sum(p[1] for p in best) / count
    spread = sum((p[0] - mean_x) ** 2 for p in best)
    if spread < 1e-6:
        return None
    slope = sum((p[0] - mean_x) * (p[1] - mean_y) for p in best) / spread
    return mean_y - slope * mean_x, slope, count
