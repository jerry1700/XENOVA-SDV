"""지금 카메라가 보는 바닥 그림과 흰색 표시를 한 장 저장하고 표시의 위치를 찍는다. 차는 움직이지 않는다.

  python -m tools.lane_drive.look <보정 json> <mapper json> <출력 폴더>
"""
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from xenova_vehicle.perception.calibration import CalibrationProfile
from xenova_vehicle.runtime.mapper_profile import MapperProfile

from .ground import GroundView
from .lane import observe_straight, pursue


def main():
    import depthai as dai
    from xenova_vehicle.oak.device import _pipeline

    calibration = CalibrationProfile.from_dict(json.loads(Path(sys.argv[1]).read_bytes()))
    value_min = int(os.environ['VALUE_MIN']) if os.environ.get('VALUE_MIN') else None
    view = GroundView(calibration, MapperProfile.from_bytes(Path(sys.argv[2]).read_bytes()).pipeline.tape,
                      value_min=value_min)
    out = Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    with dai.Device(_pipeline(dai, 15, rgb_sensor_mode='full')) as device:
        queue = device.getOutputQueue('rgb_video', maxSize=1, blocking=False)
        began = time.monotonic()
        nv12 = None
        while time.monotonic() - began < 6:
            message = queue.tryGet()
            if message is not None and time.monotonic() - began > 1.5:
                nv12 = np.asarray(message.getData(), np.uint8).reshape(540, 640)
                break
            time.sleep(0.01)
    if nv12 is None:
        raise SystemExit('no frame')
    mask = view.white(nv12)
    marks = view.marks(mask)
    stamp = time.strftime('%H%M%S')
    cv2.imwrite(str(out / f'rgb-{stamp}.jpg'), cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12))
    cv2.imwrite(str(out / f'mask-{stamp}.png'), mask * 255)
    print('SAVED', stamp)
    straight = observe_straight(marks)
    if straight is None:
        print('직선 차로 없음')
    else:
        print('점선 y %+.3f 방향 %+.1f도, 오른쪽 차로 가운데 %+.3f, 조향 %+.1f도' % (
            straight.divider_m, math.degrees(straight.heading_rad), straight.center('right')[0],
            math.degrees(pursue(straight, 'right', 0.6))))
    for mark in sorted(marks, key=lambda mark: mark.points[0][0]):
        if mark.kind == 'dash':
            print('dash  x %.2f y %+.2f' % mark.points[0])
        else:
            xs, ys = [p[0] for p in mark.points], [p[1] for p in mark.points]
            print('solid %.2f m, x %.2f..%.2f, y %+.2f..%+.2f, 첫 점 (%.2f, %+.2f) 끝 점 (%.2f, %+.2f)' % (
                mark.length_m, min(xs), max(xs), min(ys), max(ys), *mark.points[0], *mark.points[-1]))


if __name__ == '__main__':
    main()
