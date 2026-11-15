"""중계 도구(pi_bridge.py)에 정한 속도·조향을 구간마다 보내고, 구간마다 엔코더로 본 회전 반경을 적는다.

  python3 arc_send.py 100:430:6 100:-430:6     (속도 mm/s : 조향 mrad : 초)
"""
import json
import math
import socket
import sys
import time

WHEELBASE_M = 0.167
link = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
link.settimeout(0.2)
sequence = 0
report = None


def send(mmps, mrad):
    global sequence, report
    sequence += 1
    link.sendto(json.dumps(dict(seq=sequence, mmps=mmps, mrad=mrad)).encode(), ('127.0.0.1', 17700))
    try:
        report = json.loads(link.recv(1024))
    except (socket.timeout, ValueError):
        pass


for _ in range(10):
    send(0, 0)
    time.sleep(0.05)
for text in sys.argv[1:]:
    mmps, mrad, seconds = text.split(':')
    mmps, mrad, seconds = int(mmps), int(mrad), float(seconds)
    began = time.monotonic()
    marks = []
    while time.monotonic() - began < seconds:
        send(mmps, mrad)
        if report is not None:
            marks.append((time.monotonic() - began, report['x'], report['y'], report['yaw'], report['ticks'], report.get('status')))
        time.sleep(0.05)
    # 조향이 자리 잡은 뒤(처음 1.5초 뒤)부터 끝까지로 반경을 구한다
    steady = [m for m in marks if m[0] >= 1.5]
    if len(steady) >= 2:
        first, last = steady[0], steady[-1]
        distance = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(steady, steady[1:]))
        turn = last[3] - first[3]
        left, right = last[4][0] - first[4][0], last[4][1] - first[4][1]
        radius = distance / abs(turn) if abs(turn) > 1e-3 else float('inf')
        print('%s: %.1f초 동안 %.3f m, %+.1f도 → 반경 %.3f m (앞바퀴 각 %.1f도에 해당), 엔코더 왼 %d 오른 %d, 마지막 상태 %s' % (
            text, last[0] - first[0], distance, math.degrees(turn), radius,
            math.degrees(math.atan(WHEELBASE_M / radius)), left, right, last[5]))
    else:
        print(text, ': 중계 도구의 답이 없음')
    for _ in range(12):
        send(0, 0)
        time.sleep(0.05)
