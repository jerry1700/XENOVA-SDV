"""실제 Pi Gateway 코드(xenova_gateway.feedback3)와 펌웨어의 통신 처리를 맞물려 본다.

  1. 거절 확인: 펌웨어가 알리는 capability 그대로면 Pi 가 HELLO_ACK 를 받아들이지 않는다.
  2. 부분 호환성 시험: 시험용 중계가 HELLO_ACK 의 capability 에 Pi 필수 bit 를 채워 넣으면, Pi 가 CLOCK_PROBE 를
     보내고 펌웨어의 CLOCK_REPLY 로 lease 를 만들어 CLOCK_INSTALL 하고 CLOCK_ACK 를 받아들인다.
     첫 lease 뒤로 --seconds 동안 계속 주고받게 두고, 그동안 INSTALLED 응답과 Pi 가 쓸 수 있는 lease 가
     끊기지 않는지, 끝날 때에도 쓸 수 있는 lease 가 있는지 본다. 그동안 장치가 보낸 SENSOR 를 Pi 가
     오류 없이 받아 차량 표본을 만드는지도 본다.
     capability 를 고쳐 넣은 시험이므로 실제 펌웨어와 Pi 가 끝까지 연결됐다는 뜻이 아니다.

Pi 쪽은 socket:// 로 이 도구의 중계에 붙고, 중계는 호스트 시뮬레이터나 실보드와 바이트를 주고받는다.
게이트웨이 소스(feedback-3 가 들어 있는 platform/)와 protobuf==6.33.6, paho-mqtt, pyserial 이 필요하다.

  python pi_link_check.py --platform <저장소>/platform --sim <빌드 폴더>/sim_link
  python pi_link_check.py --platform <저장소>/platform --port COM8
"""
import argparse
import os
import socket
import sys
import threading
import time
from uuid import uuid4

from fb3 import (ACK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, HELLO, HELLO3, HELLO_ACK,
                 REQUIRED_CAPS, SENSOR, SENSOR3, Peer, Splitter, add_device_arguments, open_device, packet, unpack)

HANDSHAKE_TIMEOUT_MS = 1000
LEASE_WAIT_S = 5.0
LEASE_US = 1000000   # Pi 가 만드는 lease 의 길이
MAX_SILENCE_S = 0.8  # INSTALLED 응답이 이보다 오래 끊기면 다음 lease 가 앞 lease 의 기한 안에 이어지지 못한다
MAX_SENSOR_SILENCE_S = 0.2  # SENSOR 는 20 ms 마다 온다. PC 가 받는 시각으로 재므로 넉넉히 본다
# Pi odometry 에 주는 차체 값. 조립 안내서에서 잰 치수 언저리이고 보정한 값이 아니다
METRES_PER_TICK = .000164
MAXIMUM_SPEED_MPS = 1.
NAMES = {HELLO: 'HELLO', HELLO_ACK: 'HELLO_ACK', CLOCK_PROBE: 'CLOCK_PROBE', CLOCK_REPLY: 'CLOCK_REPLY',
         CLOCK_INSTALL: 'CLOCK_INSTALL', CLOCK_ACK: 'CLOCK_ACK', SENSOR: 'SENSOR'}


class Relay:
    """Pi 쪽 socket 과 시험 대상 사이를 잇고 오간 프레임을 적어 둔다.

    supplement 가 0 이 아니면 장치가 보낸 HELLO_ACK 의 capability 에 그 bit 를 채워 Pi 로 넘긴다.
    """

    def __init__(self, device, supplement):
        self.device = device
        self.supplement = supplement
        self.from_pi = []      # (종류, payload)
        self.from_device = []  # 장치가 보낸 그대로 (종류, payload, 받은 시각)
        self.broken = 0        # 장치 쪽에서 깨져 와서 Pi 로 넘기지 않은 조각
        self._listener = socket.create_server(('127.0.0.1', 0))
        self.port = self._listener.getsockname()[1]
        self._pi = None
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._serve, daemon=True)]
        self._threads[0].start()

    def _serve(self):
        self._pi, _ = self._listener.accept()
        self._pi.settimeout(0.05)
        downlink = threading.Thread(target=self._to_pi, daemon=True)
        self._threads.append(downlink)
        downlink.start()
        frames = Splitter()
        while not self._stop.is_set():
            try:
                data = self._pi.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self._record(self.from_pi, frames, data)
            self.device.write(data)

    def _to_pi(self):
        frames = Splitter()
        while not self._stop.is_set():
            for wire in frames.feed(self.device.read(0.05)):
                try:
                    kind, payload = unpack(wire)
                except ValueError:
                    self.broken += 1
                    continue
                self.from_device.append((kind, payload, time.monotonic()))
                if kind == HELLO_ACK and self.supplement and len(payload) == ACK3.size:
                    fields = list(ACK3.unpack(payload))
                    fields[3] |= self.supplement
                    wire = packet(kind, ACK3.pack(*fields))
                try:
                    self._pi.sendall(wire)
                except OSError:
                    return

    @staticmethod
    def _record(log, frames, data):
        for wire in frames.feed(data):
            try:
                log.append(unpack(wire))
            except ValueError:
                pass

    def count(self, log, kind):
        return sum(1 for row in log if row[0] == kind)

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1)
        if self._pi is not None:
            self._pi.close()
        self._listener.close()


def load_gateway(platform):
    for part in ('services/gateway', 'services/vm-agent/mqtt-client', 'apps/drive/world/python'):
        sys.path.insert(0, os.path.join(platform, part))
    try:
        from xenova_gateway.feedback3 import Feedback3Profile, Feedback3SerialSink
        from xenova_gateway.odometry import OdometryConfig, VehicleOdometry
        from xenova_gateway.scene_authority import StationaryPolicy
    except ImportError as exc:
        sys.exit(f'게이트웨이 코드를 불러오지 못했다 ({exc}). --platform 이 feedback-3 가 들어 있는 platform 폴더인지, '
                 'protobuf==6.33.6·paho-mqtt·pyserial 이 깔려 있는지 본다')
    return Feedback3Profile, Feedback3SerialSink, OdometryConfig, VehicleOdometry, StationaryPolicy


def describe_device(device):
    """HELLO 를 한 번 보내 장치가 알리는 digest·capability·encoder_bits 를 읽는다."""
    peer = Peer(device)
    peer.send(HELLO, HELLO3.pack(3, uuid4().bytes, bytes(32)))
    frame = peer.receive(2.0)
    if frame is None or frame[0] != HELLO_ACK or len(frame[1]) != ACK3.size:
        sys.exit(f'장치가 HELLO 에 HELLO_ACK 로 답하지 않는다 (깨진 프레임 {peer.invalid}개)')
    _, _, _, capabilities, config, firmware, encoder_bits = ACK3.unpack(frame[1])
    return capabilities, config.hex(), firmware.hex(), encoder_bits


def make_sink(gateway, relay, config, firmware, encoder_bits):
    Feedback3Profile, Feedback3SerialSink, OdometryConfig, VehicleOdometry, StationaryPolicy = gateway
    # PC 에서 중계를 거치는 시험이라 시각 허용 폭을 넉넉히 둔다. 차량·센서 실측값이 아니다
    odometry = VehicleOdometry(OdometryConfig(
        vehicle='esp32-link-check', calibration='not-measured', metres_per_tick=METRES_PER_TICK, track_width_m=.174,
        wheelbase_m=.167, maximum_speed_mps=MAXIMUM_SPEED_MPS, maximum_clock_error_us=100000,
        maximum_sample_age_us=400000, sample_period_ms=20, encoder_bits=encoder_bits, position_error_m=.005,
        heading_error_rad=.01, distance_error_ratio=.02, heading_error_per_m=.02), str(uuid4()))
    policy = StationaryPolicy(30000, 100000, 200000, METRES_PER_TICK, .05, .1, 0., 0.)
    profile = Feedback3Profile(config, firmware, 100, 100, 200000, LEASE_US, 200000, 100, 100, policy, 10000000,
                               .02, 20000)
    return Feedback3SerialSink(f'socket://127.0.0.1:{relay.port}', timeout_ms=HANDSHAKE_TIMEOUT_MS,
                               odometry=odometry, profile=profile)


def poll_until(sink, seconds, done):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not done():
        sink.poll(time.monotonic())
        time.sleep(0.001)


def hold(sink, seconds, clock=time.monotonic, sleep=time.sleep):
    """seconds 동안 계속 주고받게 두고, 그동안 Pi 에 쓸 수 있는 lease 가 없던 가장 긴 시간을 돌려준다."""
    last = clock()
    end = last + seconds
    since = None
    longest = 0.0
    while True:
        now = clock()
        if now >= end:
            return longest
        sink.poll(now)
        if sink._lease(int(sink.clock() * 1e6)) is not None:
            since = None
        else:
            since = last if since is None else since
            longest = max(longest, now - since)
        last = now
        sleep(0.001)


def longest_silence(times, start, end):
    """start 부터 end 까지에서 사건이 없던 가장 긴 시간. times 는 사건이 일어난 시각들이다."""
    longest, last = 0.0, start
    for at in sorted(at for at in times if start <= at <= end):
        longest, last = max(longest, at - last), at
    return max(longest, end - last)


def transcript(relay):
    kinds = sorted({row[0] for row in relay.from_pi} | {row[0] for row in relay.from_device})
    text = ', '.join(f'{NAMES.get(kind, hex(kind))} Pi→{relay.count(relay.from_pi, kind)} '
                     f'장치→{relay.count(relay.from_device, kind)}' for kind in kinds)
    return text + (f', 깨져 와서 Pi 로 넘기지 않은 장치 조각 {relay.broken}' if relay.broken else '')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_device_arguments(parser)
    parser.add_argument('--platform', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                                                           '..', 'platform'),
                        help='feedback-3 가 들어 있는 platform 폴더 (기본: 이 저장소)')
    parser.add_argument('--seconds', type=float, default=3.0,
                        help='부분 호환성 시험에서 첫 lease 를 받은 뒤 계속 주고받는 시간 (기본 3, 최소 2)')
    args = parser.parse_args()
    if args.seconds < 2 * LEASE_US / 1e6:
        parser.error('--seconds 는 lease 길이의 두 배(2초) 이상이어야 중간에 끊긴 것이 드러난다')
    sys.stdout.reconfigure(errors='backslashreplace')
    gateway = load_gateway(args.platform)
    failed = 0

    def check(ok, text):
        nonlocal failed
        failed += not ok
        print(('ok    ' if ok else 'FAIL  ') + text)

    device = open_device(args)
    try:
        capabilities, config, firmware, encoder_bits = describe_device(device)
        missing = REQUIRED_CAPS & ~capabilities
        print(f'      장치 capability 0x{capabilities:04x}, Pi 필수 0x{REQUIRED_CAPS:04x} 가운데 없는 것 0x{missing:04x}')

        print('거절 확인: 펌웨어가 알리는 capability 그대로')
        relay = Relay(device, 0)
        sink = make_sink(gateway, relay, config, firmware, encoder_bits)
        try:
            poll_until(sink, HANDSHAKE_TIMEOUT_MS / 1000 + 0.5, lambda: sink.fault is not None)
            check(missing != 0, '펌웨어의 capability 에 Pi 필수 bit 가 모자람')
            check(relay.count(relay.from_device, HELLO_ACK) == 1, '장치가 Pi 의 HELLO 에 HELLO_ACK 로 답함')
            check(not sink.ready and sink.rejected == 1, f'Pi 가 그 HELLO_ACK 를 받아들이지 않음 (ready {sink.ready}, rejected {sink.rejected})')
            check(sink.fault == 'DEVICE_HANDSHAKE_TIMEOUT', f'Pi 가 handshake 실패로 끝냄 (fault {sink.fault})')
            check(relay.count(relay.from_pi, CLOCK_PROBE) == 0, 'Pi 가 CLOCK_PROBE 를 보내지 않음')
            print('      ' + transcript(relay))
        finally:
            sink.close()
            relay.close()

        print(f'부분 호환성 시험: 중계가 HELLO_ACK 의 capability 에 모자란 bit(0x{missing:04x})를 채워 넣음')
        relay = Relay(device, REQUIRED_CAPS)
        sink = make_sink(gateway, relay, config, firmware, encoder_bits)
        try:
            poll_until(sink, LEASE_WAIT_S, lambda: any(lease['ack'] for lease in sink.leases.values()))
            started = time.monotonic()
            taken = sink.odometry.sequence
            dark = hold(sink, args.seconds)
            ended = time.monotonic()
            taken = sink.odometry.sequence - taken
            usable = sink._lease(int(sink.clock() * 1e6)) is not None
            acks = [CLOCK_ACK3.unpack(row[1]) for row in relay.from_device if row[0] == CLOCK_ACK]
            installed = [(row[2], CLOCK_ACK3.unpack(row[1])[3]) for row in relay.from_device
                         if row[0] == CLOCK_ACK and CLOCK_ACK3.unpack(row[1])[5:7] == (1, 0)]
            silence = longest_silence([at for at, _ in installed], started, ended)
            renewed = len({sequence for at, sequence in installed if at >= started})
            check(sink.ready and sink.fault is None, f'Pi 가 HELLO_ACK 를 받아들임 (ready {sink.ready}, fault {sink.fault})')
            check(relay.count(relay.from_pi, CLOCK_PROBE) >= 2
                  and relay.count(relay.from_device, CLOCK_REPLY) >= 2, 'Pi 의 CLOCK_PROBE 에 장치가 CLOCK_REPLY 로 답함')
            check(relay.count(relay.from_pi, CLOCK_INSTALL) >= 1, 'Pi 가 그 응답으로 lease 를 만들어 CLOCK_INSTALL 을 보냄')
            check(bool(acks) and all(ack[5:7] == (1, 0) for ack in acks), f'장치가 INSTALLED 로 답함 ({len(acks)}개)')
            check(any(lease['ack'] for lease in sink.leases.values()), 'Pi 가 CLOCK_ACK 를 받아들여 lease 를 쓸 수 있게 됨')
            check(sink.rejected == 0, f'Pi 가 버린 장치 프레임 {sink.rejected}개')
            check(silence <= MAX_SILENCE_S,
                  f'첫 lease 뒤 {ended - started:.1f}초 동안 INSTALLED 응답이 {MAX_SILENCE_S}초 넘게 끊기지 않음 '
                  f'(가장 긴 공백 {silence:.2f}초, 그동안 새 lease {renewed}개)')
            check(dark == 0, f'그동안 Pi 에 쓸 수 있는 lease 가 끊기지 않음 (없던 가장 긴 시간 {dark:.2f}초)')
            check(usable, '끝날 때 Pi 에 쓸 수 있는 lease 가 있음')
            sensors = [row[2] for row in relay.from_device
                       if row[0] == SENSOR and len(row[1]) == SENSOR3.size and started <= row[2] <= ended]
            sensor_silence = longest_silence(sensors, started, ended)
            errors = dict(sink.odometry.errors)
            vehicle = sink.snapshot.get('vehicle_sample')
            check(sensor_silence <= MAX_SENSOR_SILENCE_S,
                  f'그동안 장치의 SENSOR 가 {MAX_SENSOR_SILENCE_S}초 넘게 끊기지 않음 '
                  f'(가장 긴 공백 {sensor_silence:.2f}초, {len(sensors)}개)')
            check(not errors, f'Pi 가 표본을 받다가 낸 odometry 오류 {errors or "없음"}')
            check(taken >= len(sensors) * 9 // 10 > 0, f'Pi 가 그동안 표본 {taken}개를 받아들임')
            check(vehicle is not None, '끝날 때 Pi 에 쓸 수 있는 차량 표본이 있음')
            if vehicle is not None:
                print(f"      Pi 가 본 속도 {vehicle['velocity_mps']:.3f} m/s, 누계 좌 {vehicle['left_ticks']}, "
                      f"우 {vehicle['right_ticks']}, 시각 오차 {vehicle['clock_error_us']} us")
                if args.sim and args.ticks_per_second:
                    expected = args.ticks_per_second * METRES_PER_TICK
                    check(abs(vehicle['velocity_mps'] - expected) <= abs(expected) / 10,
                          f'그 속도가 시뮬레이터의 엔코더 속도 {expected:.3f} m/s 와 10 % 안에서 같음')
            print('      ' + transcript(relay))
        finally:
            sink.close()
            relay.close()
    finally:
        device.close()
    print('pi_link_check: ' + ('실패 %d건' % failed if failed else '통과. 두 번째는 부분 호환성 시험이며 종단 연결 완료가 아니다'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
