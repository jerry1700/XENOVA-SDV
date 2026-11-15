"""녹화한 장에서 차로 관측과 조향각을 찍어 본다. 차는 움직이지 않는다.

  python -m tools.lane_drive.probe <capture 폴더> <보정 json> <mapper json> [시작 장] [끝 장]
"""
import json
import math
import sys
from pathlib import Path
from time import perf_counter

import numpy as np

from xenova_vehicle.perception.calibration import CalibrationProfile
from xenova_vehicle.runtime.mapper_profile import MapperProfile

from .ground import GroundView
from .lane import observe_straight, pursue


def main():
    capture = Path(sys.argv[1])
    calibration = CalibrationProfile.from_dict(json.loads(Path(sys.argv[2]).read_bytes()))
    view = GroundView(calibration, MapperProfile.from_bytes(Path(sys.argv[3]).read_bytes()).pipeline.tape)
    frames = [json.loads(line) for line in (capture / 'frames.jsonl').read_text().splitlines()]
    first = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    last = min(int(sys.argv[5]) if len(sys.argv) > 5 else len(frames), len(frames))
    times = []
    with (capture / 'frames.bin').open('rb') as stream:
        for index in range(first, last):
            stream.seek(frames[index]['offset_bin'])
            nv12 = np.frombuffer(stream.read(345600), np.uint8).reshape(540, 640)
            tick = perf_counter()
            marks = view.marks(view.white(nv12))
            straight = observe_straight(marks)
            times.append((perf_counter() - tick) * 1000)
            dashes = sum(1 for mark in marks if mark.kind == 'dash')
            solids = [mark for mark in marks if mark.kind == 'solid']
            text = '%3d %4.0fms dash %2d solid %d' % (index, times[-1], dashes, len(solids))
            if straight is None:
                text += ' | 직선 차로 없음'
            else:
                text += ' | 점선 y %+.3f 방향 %+5.1f도 (점선 %d, 실선 점 %d) | 오른쪽 차로 가운데 %+.3f 조향 %+5.1f도' % (
                    straight.divider_m, math.degrees(straight.heading_rad), straight.dashes, straight.solid_points,
                    straight.center('right')[0], math.degrees(pursue(straight, 'right', 0.6)))
            print(text, flush=True)
    times.sort()
    print('TIME p50 %.0f p95 %.0f max %.0f ms over %d' % (times[len(times) // 2], times[int(len(times) * .95)], times[-1], len(times)))


if __name__ == '__main__':
    main()
