"""조향 명령에 서보가 어느 쪽으로 얼마나 움직이는지 눈으로 보게, 속도 0 인 DRIVE 로 조향만 보낸다.

속도 0 인 DRIVE 는 모터 PWM 을 0 으로 둔 채 서보 펄스만 바꾼다. 뒷바퀴는 돌지 않고, 기한이 지나도 서보는 마지막
자리에 머문다. 조향 값은 왼쪽이 + 다(MQTT 계약 v1: left 는 steer_rad +).

이 도구가 확인하는 것은 장치가 그 DRIVE 를 APPLIED 로 알리고, 머무는 동안 받은 표본(SENSOR 와 CLOCK_REPLY)마다
엔코더 누계가 시작값 그대로라는 것까지다. DRIVE 뒤로 표본이 오지 않은 자리는 확인하지 못한 것이므로 실패로 친다. 서보가 실제로 움직인
방향과 바퀴 각도는 사람이 보고 잰다.

실보드에는 --drive 를 줄 때만 보낸다. 서보가 움직인다. 앞바퀴가 바닥에 닿지 않게 차를 올려 두고 준다.

  실보드:            python steer_check.py --port COM8 --drive                  (0, +200, 0, -200, 0 mrad 를 3초씩)
  값을 골라서:       python steer_check.py --port COM8 --drive --mrad 100 300 500 --hold 5
  호스트 시뮬레이터: python steer_check.py --sim <빌드 폴더>/sim_link --hold 0.1
"""
import argparse
import sys
import time
from uuid import uuid4

from fb3 import (ACK3, CLOCK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, COMMAND, COMMAND3,
                 HELLO, HELLO3, HELLO_ACK, INSTALL3, PROBE3, Peer, add_device_arguments, now_us, open_device,
                 reason_name)
from link_bench import APPLIED, DRIVE, DRIVE_LIFE_US, INSTALLED, STOP, Flow, Report, lease_terms

LEASE_US = 1000000
RENEW_S = 0.5  # 머무는 동안 lease 를 다시 설치하는 간격. SENSOR 는 lease 가 살아 있을 때만 온다
MIN_HOLD_S = 0.1
DEFAULT_STEPS = (0, 200, 0, -200, 0)


def side(mrad):
    return '가운데' if mrad == 0 else f'왼쪽 {mrad} mrad' if mrad > 0 else f'오른쪽 {-mrad} mrad'


def run(flow, steps, hold, report):
    check, note = report.check, report.note
    peer = flow.peer

    # 장치의 등록 config 를 모르므로 0 으로 채운 digest 로 먼저 묻고, 알려 준 digest 로 session 을 맺는다
    peer.send(HELLO, HELLO3.pack(3, uuid4().bytes, bytes(32)))
    frame = flow.answer()
    if not check(frame is not None and frame[0] == HELLO_ACK and len(frame[1]) == ACK3.size,
                 'HELLO 에 HELLO_ACK 로 답함'):
        return
    device, config = ACK3.unpack(frame[1])[2], ACK3.unpack(frame[1])[4]
    pi = uuid4().bytes
    peer.send(HELLO, HELLO3.pack(3, pi, config))
    frame = flow.answer()
    if not check(frame is not None and frame[0] == HELLO_ACK and ACK3.unpack(frame[1])[1:3] == (pi, device),
                 '장치의 config 로 session 을 맺음'):
        return

    epoch = uuid4().bytes
    clock = dict(sequence=0, probe=0, to_device=0)

    def renew():
        """1초짜리 lease 를 새로 설치한다. 다음 DRIVE 가 그 lease 를 가리킨다"""
        measured = None
        for _ in range(2):  # Pi 가 그러듯 두 번 재고 뒤의 것을 쓴다
            clock['probe'] += 1
            p1 = now_us()
            peer.send(CLOCK_PROBE, PROBE3.pack(pi, device, clock['probe'], p1))
            reply = flow.answer()
            if reply is None or reply[0] != CLOCK_REPLY or len(reply[1]) != CLOCK3.size:
                return False
            measured = (p1, reply[2]) + CLOCK3.unpack(reply[1])
        clock['sequence'] += 1
        terms = lease_terms(measured[0], measured[1], measured[6], measured[8], LEASE_US)
        peer.send(CLOCK_INSTALL, INSTALL3.pack(pi, device, epoch, clock['sequence'], clock['probe'], *terms))
        ack = flow.answer()
        clock['to_device'] = measured[6] - measured[0]
        return (ack is not None and ack[0] == CLOCK_ACK and len(ack[1]) == CLOCK_ACK3.size
                and CLOCK_ACK3.unpack(ack[1])[5:7] == (INSTALLED, 0))

    def command(sequence, kind, steering=0):
        """COMMAND 를 보내고 그 순번의 첫 REPORT 필드를 돌려준다. 답이 없으면 None"""
        if kind == STOP:
            body = COMMAND3.pack(pi, device, sequence, bytes(16), 0, 0, 0, 0, 1, sequence & 255, kind, 0, 0, 0, 0, 0)
        else:
            issued = now_us()
            body = COMMAND3.pack(pi, device, sequence, epoch, clock['sequence'], issued,
                                 issued + DRIVE_LIFE_US + 10000, issued + clock['to_device'] + DRIVE_LIFE_US, 1,
                                 sequence & 255, kind, 1, 0, steering, 0, 0)
        peer.send(COMMAND, body)
        return flow.report(lambda fields: fields[2] == sequence)

    def result(fields):
        return '답 없음' if fields is None else f'status {fields[6]}, reason {reason_name(fields[18])}'

    def watch(seconds):
        """seconds 동안 lease 를 이어 가며 표본을 받는다. lease 를 잇지 못하면 False"""
        end = time.monotonic() + seconds
        while True:
            flow.listen(max(0.0, min(RENEW_S, end - time.monotonic())))
            if time.monotonic() >= end:
                return True
            if not renew():
                return False

    if not check(renew(), '시각 lease 를 설치'):
        return
    fields = command(1, STOP)
    if not check(fields is not None and fields[6] == APPLIED, f'STOP 에 APPLIED 로 답함 ({result(fields)})'):
        return
    flow.listen(0.1)
    if not check(flow.sensors, 'SENSOR 가 와서 엔코더 누계를 볼 수 있음'):
        return
    start = flow.ticks[-1]

    for index, mrad in enumerate(steps):
        mark = len(flow.ticks)
        if not check(renew(), f'{side(mrad)}: 시각 lease 를 새로 설치'):
            return
        fields = command(2 + index, DRIVE, steering=mrad)
        applied = check(fields is not None and fields[6] == APPLIED,
                        f'{side(mrad)}: 속도 0 인 DRIVE 가 APPLIED ({result(fields)})')
        answered = len(flow.ticks)
        if not check(watch(hold), f'{side(mrad)}: 머무는 동안 lease 를 이어 설치'):
            return
        seen = flow.ticks[mark:]  # lease 를 설치하며 받은 CLOCK_REPLY 의 누계도 들어 있다
        late = len(flow.ticks) - answered  # DRIVE 의 답을 받은 뒤에 온 표본
        moved = [value for value in seen if value != start]
        if not late:
            check(False, f'{side(mrad)}: DRIVE 뒤로 표본이 오지 않아 엔코더를 확인하지 못함')
        elif moved:
            check(False, f'{side(mrad)}: 엔코더 누계가 바뀜 ({start} → {moved[0]}, 표본 {len(seen)}개 가운데 {len(moved)}개)')
        else:
            check(True, f'{side(mrad)}: DRIVE 뒤 표본 {late}개까지 엔코더 누계가 시작값 {start} 그대로')
        if applied:
            note(f'지금 서보는 {side(mrad)} 자리여야 한다')
    fields = command(2 + len(steps), STOP)
    check(fields is not None and fields[6] == APPLIED, f'마지막 STOP 에 APPLIED 로 답함 ({result(fields)})')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_device_arguments(parser)
    parser.add_argument('--mrad', type=int, nargs='+', default=list(DEFAULT_STEPS),
                        help='차례로 보낼 조향 (mrad, 왼쪽이 +). 기본 0 200 0 -200 0')
    parser.add_argument('--hold', type=float, default=3.0, help='한 자리에 머무는 시간 (초, 기본 3, 최소 0.1)')
    parser.add_argument('--drive', action='store_true',
                        help='실보드에 보낸다. 서보가 움직인다. 앞바퀴가 바닥에 닿지 않게 올려 두었을 때만 준다')
    args = parser.parse_args()
    if args.port and not args.drive:
        parser.error('실보드에는 --drive 를 줘야 보낸다. 서보가 움직인다')
    if args.hold < MIN_HOLD_S:
        parser.error(f'--hold 는 {MIN_HOLD_S} 이상이어야 DRIVE 뒤의 표본을 받는다')
    if any(not -32768 <= mrad <= 32767 for mrad in args.mrad):
        parser.error('--mrad 는 i16 범위여야 한다')
    sys.stdout.reconfigure(errors='backslashreplace')  # 한글을 못 쓰는 콘솔에서도 멈추지 않게
    report = Report()
    device = open_device(args)
    peer = Peer(device)
    try:
        run(Flow(peer), args.mrad, args.hold, report)
    finally:
        device.close()
    report.check(peer.invalid == 0, f'깨진 프레임 {peer.invalid}개')
    report.note('서보가 움직인 방향과 바퀴 각도는 이 도구가 보지 못한다. 눈으로 보고 잰다')
    print(f'steer_check: {report.checks} checks, {report.failed} failed')
    return 1 if report.failed else 0


if __name__ == '__main__':
    sys.exit(main())
