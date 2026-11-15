"""흰선과 깊이 가림을 같은 왜곡 보정 RGB 격자로 옮긴다."""

from dataclasses import replace
from functools import lru_cache

from .rectify import rgb_view


@lru_cache(maxsize=2)
def tape_rgb_geometry(camera):
    """원 영상과 YOLOP의 좌표를 보존하고 흰선 전용 격자만 준비한다."""
    if not any(camera.distortion):
        return camera, None, None

    import cv2
    import numpy as np

    view = rgb_view(camera)
    map_x, map_y = view.map_u, view.map_v
    left, top, right, bottom = camera.roi
    # Bilinear sampling needs all contributing pixels inside the native ROI.
    valid = ((map_x >= np.ceil(left)) & (map_y >= np.ceil(top))
             & (map_x < np.ceil(right) - 1) & (map_y < np.ceil(bottom) - 1))
    valid = cv2.erode(valid.astype(np.uint8), np.ones((3, 3), np.uint8),
                      borderType=cv2.BORDER_CONSTANT, borderValue=0)
    # A qualified remap must already include residual pixel/ray error in the profile bounds.
    corrected = replace(camera, distortion=(), qualified=camera.qualified and camera.remap_qualified)
    valid = np.frombuffer(valid.tobytes(), dtype=valid.dtype).reshape(valid.shape)
    return corrected, (map_x, map_y), valid
