"""OAK 영상의 차선 표시와 RPi4 가 돌려주는 엔코더 자세로 속도·조향을 정해 중계 도구(pi_bridge.py)로 보낸다.

차로가 0.5초 넘게 안 보이거나 중계 도구의 답이 0.3초 넘게 끊기면 속도 0 을 보낸다. 중계 도구는 값이 끊기면 스스로 STOP 한다.

  python -m tools.lane_drive.drive <보정 json> <mapper json> --pi 192.168.40.17 --seconds 8 --mmps 0     (보기만)
  python -m tools.lane_drive.drive <보정 json> <mapper json> --pi 192.168.40.17 --seconds 14 --mmps 120  (직선만)
  python -m tools.lane_drive.drive <보정 json> <mapper json> --pi 192.168.40.17 --seconds 70 --corners --long 1.51 --short 0.46
"""
import argparse
import json
import math
from pathlib import Path
import socket
import time

import numpy as np

from xenova_vehicle.perception.calibration import CalibrationProfile
from xenova_vehicle.runtime.mapper_profile import MapperProfile

from .driver import Driver
from .ground import GroundView
from .lane import steady
from .obstacle import DepthView, Steady, front_obstacle, lane_obstacles

BRIDGE_STALE_S = 0.3
DEPTH_STALE_S = 0.3


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('calibration', type=Path)
    parser.add_argument('mapper', type=Path)
    parser.add_argument('--pi', required=True, help='중계 도구가 도는 RPi4 주소')
    parser.add_argument('--port', type=int, default=17700)
    parser.add_argument('--seconds', type=float, default=8)
    parser.add_argument('--mmps', type=int, default=0, help='직선 차로를 따라갈 때의 속도. 0 이면 보기만 한다')
    parser.add_argument('--lane', choices=('right', 'left'), default='right')
    parser.add_argument('--lookahead', type=float, default=0.6)
    parser.add_argument('--steer-bias-mrad', type=int, default=0, help='조향 가운데가 치우친 만큼 더하는 값')
    parser.add_argument('--steer-gain', type=float, default=1.0, help='명령 조향 / 바라는 앞바퀴 각')
    parser.add_argument('--value-min', type=int, help='흰색으로 볼 밝기 하한. 안 주면 mapper profile 의 값')
    parser.add_argument('--corners', action='store_true', help='직선 끝이 가까워지면 왼쪽 모서리를 엔코더로 돈다')
    parser.add_argument('--corner-mmps', type=int, default=100)
    parser.add_argument('--after', type=float, default=0.4, help='모서리를 돈 뒤 경로로 더 가는 거리')
    parser.add_argument('--long', type=float, help='긴 변의 직선 길이. 주면 영상으로 직선 끝을 못 잡아도 모서리를 돈다')
    parser.add_argument('--short', type=float, help='짧은 변의 직선 길이. 주면 짧은 변의 모서리까지 이어서 돈다')
    parser.add_argument('--stay-inner', action='store_true', help='안쪽 차로에서 모서리를 만나면 바깥으로 나오지 않고 안쪽 차로를 그대로 돈다')
    parser.add_argument('--keep-right', action='store_true', help='안쪽 차로에서 모서리를 돌고 나온 직선에서 바깥 차로가 비어 있으면 돌아간다')
    parser.add_argument('--no-anchor', action='store_true', help='모서리를 도는 동안 보이는 선으로 경로를 바로잡지 않는다')
    parser.add_argument('--first-left', type=float, help='출발할 때 뒤 차축에서 직선 끝까지의 거리')
    parser.add_argument('--change-after', type=float, help='모서리를 처음 돌고 나온 뒤 이만큼 달렸을 때 옆 차로로 옮겨 본다')
    parser.add_argument('--obstacles', action='store_true', help='깊이 영상으로 물체를 보고 차로를 옮기거나 선다')
    parser.add_argument('--log', type=Path, help='장마다의 관측과 중계 도구의 답을 적을 jsonl')
    args = parser.parse_args()
    import depthai as dai
    from xenova_vehicle.oak.device import _pipeline

    calibration = CalibrationProfile.from_dict(json.loads(args.calibration.read_bytes()))
    view = GroundView(calibration, MapperProfile.from_bytes(args.mapper.read_bytes()).pipeline.tape,
                      value_min=args.value_min)
    driver = Driver(lane=args.lane, mmps=args.mmps, corner_mmps=args.corner_mmps, lookahead_m=args.lookahead,
                    after_m=args.after, short_m=args.short, long_m=args.long,
                    corners=args.corners, first_left_m=args.first_left, stay_inner=args.stay_inner,
                    anchor=not args.no_anchor, keep_right=args.keep_right)
    depth_view = DepthView(calibration.right) if args.obstacles else None
    raised = raised_at = None
    held = {'right': Steady(), 'left': Steady(), 'front': Steady()}
    obstacles = front = None
    link = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    link.setblocking(False)
    log = None if args.log is None else args.log.open('w', buffering=1)
    sequence = frames = 0
    report = report_at = None
    corner_done_at = None
    changed = False
    with dai.Device(_pipeline(dai, 15, rgb_sensor_mode='full')) as device:
        queue = device.getOutputQueue('rgb_video', maxSize=1, blocking=False)
        depth_queue = device.getOutputQueue('depth_right', maxSize=1, blocking=False) if args.obstacles else None
        began = time.monotonic()
        last_print = began
        print('CAMERA_OPEN', device.getUsbSpeed(), flush=True)
        while time.monotonic() - began < args.seconds:
            message = queue.tryGet()
            if message is None:
                time.sleep(0.002)
                continue
            now = time.monotonic()
            data = message.getData()
            if len(data) != 345600:
                raise ValueError('unexpected RGB frame size')
            while True:
                try:
                    report, report_at = json.loads(link.recv(1024)), now
                except BlockingIOError:
                    break
                except ValueError:
                    continue
            marks = view.marks(view.white(np.asarray(data, np.uint8).reshape(540, 640)))
            frames += 1
            current = report is not None and now - report_at <= BRIDGE_STALE_S
            pose = (report['x'], report['y'], report['yaw']) if current else None
            if depth_queue is not None:
                depth = depth_queue.tryGet()
                if depth is not None:
                    raised, raised_at = depth_view.raised(depth.getFrame()), now
                    lanes = driver.lanes(pose) or (steady(driver.recent) if len(driver.recent) >= 3 else None)
                    if not driver.lined(now):
                        lanes = None  # 차로의 자리가 확인되지 않았으면 트랙 밖의 물건이 차로 안으로 들어온다
                    # 경로를 따라가는 동안에는 깊이 영상으로 서지 않는다. 모서리에서는 트랙 밖을 정면으로 봐서 헛것이 생기고,
                    # 차로를 옮길 때는 피하는 물체의 가장자리가 걸린다. 가까운 물체는 ESP32 의 앞 ToF 가 막는다
                    front = held['front'].push(None if driver.state == 'path' or lanes is None else front_obstacle(
                        raised, driver.steer, None, lanes))
                    seen = lane_obstacles(raised, lanes) if lanes is not None else {}
                    obstacles = {lane: held[lane].push(seen.get(lane)) for lane in ('right', 'left')}
                elif raised is None or now - raised_at > DEPTH_STALE_S:
                    obstacles = front = None
            speed, steer = driver.step(now, marks, pose, obstacles, front)
            if driver.note:
                print('%5.1fs %s' % (now - began, driver.note), flush=True)
                if driver.note.startswith('모서리 경로 끝') and corner_done_at is None:
                    corner_done_at = report['x'], report['y']
            note, driver.note = driver.note, None
            if (args.change_after is not None and not changed and corner_done_at is not None and pose is not None
                    and math.hypot(pose[0] - corner_done_at[0], pose[1] - corner_done_at[1]) >= args.change_after):
                changed = driver.change_lane(now, pose)
            if driver.state == 'path' and not current:
                speed = 0
            sequence += 1
            command = dict(seq=sequence, mmps=speed, mrad=round(steer * args.steer_gain * 1000) + args.steer_bias_mrad)
            link.sendto(json.dumps(command, separators=(',', ':')).encode(), (args.pi, args.port))
            if log is not None:
                log.write(json.dumps(dict(
                    t=round(now - began, 3), took_ms=round((time.monotonic() - now) * 1000, 1), command=command,
                    state=driver.state, lane=driver.lane, remaining=driver.remaining, note=note, bridge=report,
                    obstacles=obstacles, front=front, raised=None if raised is None else len(raised),
                    raised_points=[[round(float(v), 2) for v in point] for point in raised[::max(1, len(raised) // 120)]]
                    if raised is not None and (front is not None or obstacles and any(obstacles.values())) else None,
                    dashes=[[round(v, 3) for v in mark.points[0]] for mark in marks if mark.kind == 'dash'][:24],
                    solids=[[[round(v, 3) for v in mark.points[0]], [round(v, 3) for v in mark.points[-1]],
                             len(mark.points)] for mark in marks if mark.kind == 'solid'][:8]), separators=(',', ':')) + '\n')
            if now - last_print >= 0.5:
                last_print = now
                seen = driver.recent[-1] if driver.recent else None
                print('%5.1fs %-6s %s 조향 %+5.1f도 속도 %d%s | 중계 %s' % (
                    now - began, driver.state if driver.remaining is None else '남은 %.2f' % driver.remaining,
                    '차로 없음' if seen is None else '점선 y %+.3f 방향 %+5.1f도 (점선 %d, 실선 점 %d)' % (
                        seen.divider_m, math.degrees(seen.heading_rad), seen.dashes, seen.solid_points),
                    math.degrees(steer), speed,
                    '' if depth_queue is None else ' | 물체 %s 정면 %s' % (obstacles, front),
                    '답 없음' if not current else '이동 (%.3f, %.3f, %+.1f도) 앞막힘 %d' % (
                        report['x'], report['y'], math.degrees(report['yaw']), report['front_blocked'])), flush=True)
        link.sendto(json.dumps(dict(seq=sequence + 1, mmps=0, mrad=0)).encode(), (args.pi, args.port))
    print('끝: %d장, 초당 %.1f장' % (frames, frames / max(1e-6, time.monotonic() - began)))


if __name__ == '__main__':
    main()
