"""RPi4 에서 돈다. 젯슨이 UDP 로 보낸 속도·조향을 ESP32 에 DRIVE 로 넘기고 엔코더 자세를 돌려준다.

게이트웨이가 시리얼을 쥐고 있으면 쓸 수 없다. ESP32 통신 도구(fb3.py, link_bench.py)와 같은 폴더에 둔다.
받는 값이 0.3초 넘게 끊기면 STOP 을 보내고, DRIVE 하나의 기한은 80 ms 라 이 도구가 죽어도 장치가 스스로 멈춘다.

  python3 pi_bridge.py --port /dev/ttyUSB0 --seconds 20 --max-mmps 150            (받은 값을 찍기만 한다)
  python3 pi_bridge.py --port /dev/ttyUSB0 --seconds 20 --max-mmps 150 --drive    (바퀴가 돈다)
"""
import argparse
import json
import math
import socket
import sys
import time
from uuid import uuid4

from fb3 import (ACK3, CLOCK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, COMMAND, COMMAND3,
                 HELLO, HELLO3, HELLO_ACK, INSTALL3, PROBE3, Peer, SerialPort, now_us, reason_name)
from link_bench import APPLIED, DRIVE, DRIVE_LIFE_US, INSTALLED, STOP, Flow, lease_terms

LEASE_US = 5000000
RENEW_BEFORE_US = 2000000
PERIOD_S = 0.04
STALE_S = 0.3
MAX_STEER_MRAD = 490
M_PER_TICK, TRACK_M = 0.000164, 0.174


class Link:
    def __init__(self, peer):
        self.peer, self.flow = peer, Flow(peer)
        self.sequence = self.lease = self.probe = self.stop_sequence = 0
        self.epoch = uuid4().bytes

    def open(self):
        self.peer.send(HELLO, HELLO3.pack(3, uuid4().bytes, bytes(32)))
        frame = self.flow.answer()
        if frame is None or frame[0] != HELLO_ACK or len(frame[1]) != ACK3.size:
            raise RuntimeError('HELLO 에 답이 없음')
        config = ACK3.unpack(frame[1])[4]
        self.pi = uuid4().bytes
        self.peer.send(HELLO, HELLO3.pack(3, self.pi, config))
        frame = self.flow.answer()
        if frame is None or frame[0] != HELLO_ACK or ACK3.unpack(frame[1])[1] != self.pi:
            raise RuntimeError('session 을 맺지 못함')
        self.device = ACK3.unpack(frame[1])[2]

    def install(self, between=None):
        """probe 두 번으로 시각 lease 를 새로 설치한다. between 은 단계 사이에 부르는 함수다."""
        measured = None
        for _ in range(2):
            self.probe += 1
            p1 = now_us()
            self.peer.send(CLOCK_PROBE, PROBE3.pack(self.pi, self.device, self.probe, p1))
            reply = self.flow.answer(0.5)
            if reply is None or reply[0] != CLOCK_REPLY or len(reply[1]) != CLOCK3.size:
                raise RuntimeError('CLOCK_PROBE 에 답이 없음')
            measured = (p1, reply[2]) + CLOCK3.unpack(reply[1])
            if between is not None:
                between()
        terms = lease_terms(measured[0], measured[1], measured[6], measured[8], LEASE_US)
        self.peer.send(CLOCK_INSTALL, INSTALL3.pack(self.pi, self.device, self.epoch, self.lease + 1, self.probe, *terms))
        ack = self.flow.answer(0.5)
        if (ack is None or ack[0] != CLOCK_ACK or len(ack[1]) != CLOCK_ACK3.size
                or CLOCK_ACK3.unpack(ack[1])[5:7] != (INSTALLED, 0)):
            raise RuntimeError('시각 lease 를 설치하지 못함')
        self.lease += 1
        self.to_device = measured[6] - measured[0]
        self.expires_us = measured[0] + LEASE_US

    def command(self, kind, mmps=0, mrad=0, wait=0.15):
        """명령을 보내고 그 REPORT 의 (status, reason) 을 돌려준다. 답이 없으면 None 이다."""
        self.sequence += 1
        sequence = self.sequence
        if kind == STOP:
            body = COMMAND3.pack(self.pi, self.device, sequence, bytes(16), 0, 0, 0, 0, 1, sequence & 255, kind,
                                 0, 0, 0, 0, 0)
        else:
            issued = now_us()
            body = COMMAND3.pack(self.pi, self.device, sequence, self.epoch, self.lease, issued,
                                 issued + DRIVE_LIFE_US + 10000, issued + self.to_device + DRIVE_LIFE_US, 1,
                                 sequence & 255, kind, self.stop_sequence, mmps, mrad, 0, 0)
        self.peer.send(COMMAND, body)
        fields = self.flow.report(lambda row: row[2] == sequence, wait)
        if fields is not None and kind == STOP and fields[6] == APPLIED:
            self.stop_sequence = sequence
        return None if fields is None else (fields[6], fields[18])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', required=True)
    parser.add_argument('--baud', type=int, default=460800)
    parser.add_argument('--listen', type=int, default=17700, help='젯슨의 값을 받는 UDP 포트')
    parser.add_argument('--seconds', type=float, default=20, help='이 시간이 지나면 멈추고 끝낸다')
    parser.add_argument('--max-mmps', type=int, default=150)
    parser.add_argument('--max-m', type=float, default=3.0, help='엔코더로 본 이동 거리가 이만큼이면 멈추고 끝낸다')
    parser.add_argument('--drive', action='store_true', help='실제로 DRIVE 를 보낸다. 바퀴가 돈다')
    args = parser.parse_args()
    if not 0 < args.max_mmps <= 300 or not 0 < args.seconds <= 300:
        parser.error('--max-mmps 는 300 이하, --seconds 는 300 이하여야 한다')
    sys.stdout.reconfigure(errors='backslashreplace', line_buffering=True)
    inbound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    inbound.bind(('0.0.0.0', args.listen))
    inbound.setblocking(False)
    device = SerialPort(args.port, args.baud)
    link = Link(Peer(device))
    moving = False
    try:
        link.open()
        link.install()
        status = link.command(STOP, wait=1.0)
        if status is None or status[0] != APPLIED:
            raise RuntimeError(f'첫 STOP 이 APPLIED 가 아님 ({status})')
        link.flow.listen(0.1)
        if not link.flow.ticks:
            raise RuntimeError('엔코더 표본이 오지 않음')
        print('ok    준비됨. 엔코더', link.flow.ticks[-1], '실제 주행' if args.drive else '찍기만 함')
        began = time.monotonic()
        previous = link.flow.ticks[-1]
        x = y = yaw = travelled = 0.0
        target = None  # (mmps, mrad, 받은 시각, 순번)
        sender = None
        cycle = refused = 0
        last_print = began
        while time.monotonic() - began < args.seconds and travelled < args.max_m:
            tick = time.monotonic()
            cycle += 1
            while True:
                try:
                    data, address = inbound.recvfrom(512)
                except BlockingIOError:
                    break
                try:
                    value = json.loads(data)
                    mmps = max(0, min(args.max_mmps, int(value['mmps'])))
                    mrad = max(-MAX_STEER_MRAD, min(MAX_STEER_MRAD, int(value['mrad'])))
                    target, sender = (mmps, mrad, tick, int(value.get('seq', 0))), address
                except (ValueError, KeyError, TypeError):
                    continue
            fresh = target is not None and tick - target[2] <= STALE_S
            if link.expires_us - now_us() < RENEW_BEFORE_US:
                link.install((lambda: link.command(DRIVE, target[0], target[1], wait=0.0))
                             if moving and fresh else None)
            status = None
            if fresh and target[0] > 0 and args.drive:
                status = link.command(DRIVE, target[0], target[1])
                moving = True
                if status is None or status[0] != APPLIED:
                    refused += 1
            elif moving:
                status = link.command(STOP, wait=0.3)
                moving = False
            link.flow.listen(max(0.0, PERIOD_S - (time.monotonic() - tick)))
            now = link.flow.ticks[-1]
            left, right = (now[0] - previous[0]) * M_PER_TICK, (now[1] - previous[1]) * M_PER_TICK
            previous = now
            distance, turn = (left + right) / 2, (right - left) / TRACK_M
            x += distance * math.cos(yaw + turn / 2)
            y += distance * math.sin(yaw + turn / 2)
            yaw += turn
            travelled += abs(distance)
            quality = link.flow.sensors[-1][6] if link.flow.sensors else 0
            report = dict(t=round(time.monotonic() - began, 3), x=round(x, 4), y=round(y, 4), yaw=round(yaw, 4),
                          ticks=list(now), front_blocked=bool(quality & 0x08), rear_blocked=bool(quality & 0x10),
                          status=None if status is None else list(status), moving=moving,
                          echo=None if target is None else target[3])
            if sender is not None:
                inbound.sendto(json.dumps(report, separators=(',', ':')).encode(), sender)
            if tick - last_print >= 0.5:
                last_print = tick
                print('%5.1fs 목표 %s 이동 %.3f m 자세 (%.3f, %.3f, %+.1f도) 엔코더 %s 앞막힘 %d 상태 %s 거절 %d' % (
                    tick - began, None if target is None else target[:2], travelled, x, y, math.degrees(yaw), now,
                    bool(quality & 0x08), None if status is None else '%d/%s' % (status[0], reason_name(status[1])), refused))
        print('끝: %.1f초, 이동 %.3f m, 주기 %d번, 거절 %d번' % (time.monotonic() - began, travelled, cycle, refused))
    finally:
        try:
            status = link.command(STOP, wait=0.5)
            print('마지막 STOP', status)
        finally:
            device.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
