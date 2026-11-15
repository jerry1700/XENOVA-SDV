"""왜곡 계수가 있는 RGB 카메라를 편 영상과 무왜곡 모델로 바꿔 쓰는 view.

geometry 의 연속 bound 는 왜곡이 없는 pinhole 에서만 성립한다. 편 영상의 각 픽셀이 원 영상의 어느
자리에서 왔는지는 정방향 왜곡 모델로 계산하므로, 편 영상 위의 픽셀 구간은 같은 K 의 무왜곡 광선 구간과
그대로 대응한다. 원 영상의 픽셀 좌표를 편 영상으로 옮기는 쪽은 반복 역왜곡이라, 결과를 정방향으로 다시
넣어 원 좌표와 맞는 점만 내준다. 보정의 식별자와 오차 값은 바꾸지 않는다.
"""

from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from .calibration import CameraModel


# 역왜곡한 점을 정방향으로 다시 넣었을 때 원 픽셀과 벌어져도 되는 거리
POINT_RESIDUAL_PX = 0.02


def _inside_rectangle(inside):
    """inside 가 모두 참인 축 정렬 사각형 (x0, y0, x1, y1). 거짓이 가장 많은 변부터 깎는다."""
    import numpy as np
    rows, columns = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
    if not len(rows) or not len(columns):
        return None
    x0, y0, x1, y1 = int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1
    while x0 < x1 and y0 < y1:
        window = inside[y0:y1, x0:x1]
        if window.all():
            return x0, y0, x1, y1
        missing = (int(np.count_nonzero(~window[0])) / window.shape[1],
                   int(np.count_nonzero(~window[-1])) / window.shape[1],
                   int(np.count_nonzero(~window[:, 0])) / window.shape[0],
                   int(np.count_nonzero(~window[:, -1])) / window.shape[0])
        side = max(range(4), key=missing.__getitem__)
        if side == 0:
            y0 += 1
        elif side == 1:
            y1 -= 1
        elif side == 2:
            x0 += 1
        else:
            x1 -= 1
    return None


@dataclass(frozen=True)
class RgbView:
    """source 는 원 보정의 카메라, camera 는 편 영상에 쓰는 무왜곡 카메라다."""
    source: CameraModel
    camera: CameraModel
    map_u: Any
    map_v: Any

    def image(self, bgr):
        """원 영상과 같은 크기의 편 영상. source ROI 밖에서 온 픽셀은 0 이고 camera.roi 에 들지 않는다."""
        import cv2
        import numpy as np
        if (not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8
                or bgr.shape[:2] != (self.source.height, self.source.width)):
            raise ValueError('rectified view input differs from the source camera grid')
        return cv2.remap(bgr, self.map_u, self.map_v, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def points(self, u, v):
        """원 영상의 픽셀 좌표를 편 영상의 좌표로. 정방향으로 되돌려 맞지 않는 점은 nan 이다."""
        import cv2
        import numpy as np
        u, v = np.asarray(u, dtype=np.float64), np.asarray(v, dtype=np.float64)
        if u.shape != v.shape:
            raise ValueError('pixel coordinate arrays differ in shape')
        out_u, out_v = np.full(u.shape, np.nan), np.full(u.shape, np.nan)
        finite = np.isfinite(u) & np.isfinite(v)
        if not finite.any():
            return out_u, out_v
        k, d = np.asarray(self.source.k, dtype=np.float64), np.asarray(self.source.distortion, dtype=np.float64)
        pixels = np.stack((u[finite], v[finite]), axis=-1).reshape(-1, 1, 2)
        normalized = cv2.undistortPoints(pixels, k, d).reshape(-1, 2)
        rays = np.concatenate((normalized, np.ones((len(normalized), 1))), axis=1)
        back = cv2.projectPoints(rays.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), k, d)[0].reshape(-1, 2)
        agree = np.hypot(*(back - pixels.reshape(-1, 2)).T) <= POINT_RESIDUAL_PX
        ru = np.where(agree, normalized[:, 0] * k[0, 0] + k[0, 2], np.nan)
        rv = np.where(agree, normalized[:, 1] * k[1, 1] + k[1, 2], np.nan)
        out_u[finite], out_v[finite] = ru, rv
        return out_u, out_v


@lru_cache(maxsize=4)
def rgb_view(camera):
    """왜곡 계수가 모두 0 이면 None. 아니면 같은 K·같은 외부 보정의 무왜곡 view."""
    if type(camera) is not CameraModel:
        raise ValueError('rectified view requires a camera model')
    if not any(camera.distortion):
        return None
    import cv2
    import numpy as np
    k, d = np.asarray(camera.k, dtype=np.float64), np.asarray(camera.distortion, dtype=np.float64)
    map_u, map_v = cv2.initUndistortRectifyMap(k, d, None, k, (camera.width, camera.height), cv2.CV_32FC1)
    x0, y0, x1, y1 = camera.roi
    # 쌍선형 보간이 읽는 네 픽셀이 모두 source ROI 안에 있어야 한다
    inside = ((map_u >= x0) & (map_u <= x1 - 1) & (map_v >= y0) & (map_v <= y1 - 1)
              & np.isfinite(map_u) & np.isfinite(map_v))
    rectangle = _inside_rectangle(inside)
    if rectangle is None:
        raise ValueError('rectified view keeps no pixel of the source ROI')
    for value in (map_u, map_v):
        value.setflags(write=False)
    rectified = replace(camera, distortion=(), roi=tuple(float(value) for value in rectangle))
    return RgbView(camera, rectified, map_u, map_v)
