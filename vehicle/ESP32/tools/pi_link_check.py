"""실제 Pi Gateway 코드(xenova_gateway.feedback3)와 펌웨어의 통신 처리를 맞물려 본다.

Pi 쪽은 socket:// 로 이 도구의 중계에 붙고, 중계는 호스트 시뮬레이터나 실보드와 바이트를 주고받는다.
PC 에서 게이트웨이 코드를 돌리는 것이므로 실제 Pi 의 USB 와 연결됐다는 뜻은 아니다.

  1. 연결: Pi 가 HELLO_ACK 를 받아들이고 CLOCK_PROBE·CLOCK_INSTALL 로 lease 를 이어 간다.
     첫 lease 뒤로 --seconds 동안 계속 주고받게 두고, 그동안 INSTALLED 응답과 Pi 가 쓸 수 있는 lease, 장치의
     SENSOR 가 끊기지 않는지, Pi 의 odometry 가 오류 없이 표본을 받는지 본다.
  2. STOP: Pi 가 STOP 을 보내고, 장치의 APPLIED 보고와 표본으로 정지를 확인한다.
  3. DRIVE: Pi 가 1초 동안 20 ms 마다 DRIVE 를 보내고 모두 APPLIED 를 받는다. 보내기를 멈추면 장치가 스스로
     출력을 끊어 속도가 0 이 되고, 이어서 보낸 STOP 으로 다시 정지를 확인한다.
     실보드에서는 --drive 를 줄 때만 한다. 바퀴가 0.2 m/s 로 1초 돈다. 바퀴를 띄우고 모터 전원을 넣었을 때만 준다.
  4. HOLD_STEER: 장치가 정지 조향(capability 0x0400)을 알리면, Pi 가 방향을 바꾸는 자리의 STOP 뒤에 0.5초 동안
     HOLD_STEER 를 보내고 APPLIED 를 받는다. 바퀴는 돌지 않고 조향이 왼쪽 0.1 rad 로 움직인다. 3 과 같은 때에만 한다.
     Pi 는 HOLD_STEER 를 내보낼 때마다 정지 확인 창을 다시 검사하고, 통과하지 못한 순간의 것은 내보내지 않는다.
     그런 것이 하나라도 있으면 실패다. Pi 가 방향 전환을 허락하는 단계(gate state 4)까지 가는지는 판정하지 않고 값만
     보여 준다. 조향 측정이 없는 장치에서는 그 단계에 이르지 않는다.

펌웨어가 알리는 capability 에 Pi 필수 bit 가 모자라면 Pi 가 HELLO_ACK 를 받지 않는 것을 먼저 확인하고,
중계가 모자란 bit 를 채워 넣은 채로 1 만 본다. 그것은 부분 호환성 시험이다.

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
from uuid import UUID, uuid4

from fb3 import (ACK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, COMMAND, HELLO, HELLO3,
                 HELLO_ACK, REPORT, REPORT3, REQUIRED_CAPS, SENSOR, SENSOR3, Peer, Splitter, add_device_arguments,
                 open_device, packet, unpack)

HANDSHAKE_TIMEOUT_MS = 1000
LEASE_WAIT_S = 5.0
LEASE_US = 1000000   # Pi 가 만드는 lease 의 길이
MAX_SILENCE_S = 0.8  # INSTALLED 응답이 이보다 오래 끊기면 다음 lease 가 앞 lease 의 기한 안에 이어지지 못한다
MAX_SENSOR_SILENCE_S = 0.2  # SENSOR 는 20 ms 마다 온다. PC 가 받는 시각으로 재므로 넉넉히 본다
STOP_WAIT_S = 1.0           # STOP 을 보낸 뒤 Pi 가 정지를 확인할 때까지 기다리는 시간
DRIVE_MPS = .2              # --drive 로 보내는 DRIVE 의 속도
DRIVE_S = 1.0               # DRIVE 를 이어 보내는 시간
DRIVE_PERIOD_S = .02
DRIVE_LIFE_US = 90000       # DRIVE 하나의 기한
STATIONARY_STEER = 0x0400   # HELLO_ACK capabilities 가운데 HOLD_STEER 를 받는다는 bit
HOLD_S = .5                 # HOLD_STEER 를 이어 보내는 시간
HOLD_RAD = .1               # 그 HOLD_STEER 의 조향. 왼쪽이 +
# Pi odometry 에 주는 차체 값. 조립 안내서에서 잰 치수 언저리이고 보정한 값이 아니다
METRES_PER_TICK = .000164
MAXIMUM_SPEED_MPS = 1.
NAMES = {HELLO: 'HELLO', HELLO_ACK: 'HELLO_ACK', CLOCK_PROBE: 'CLOCK_PROBE', CLOCK_REPLY: 'CLOCK_REPLY',
         CLOCK_INSTALL: 'CLOCK_INSTALL', CLOCK_ACK: 'CLOCK_ACK', SENSOR: 'SENSOR', COMMAND: 'COMMAND',
         REPORT: 'REPORT'}


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
        heading_error_rad=.0005, distance_error_ratio=.02, heading_error_per_m=.02), str(uuid4()))
    # 정지로 볼 한도. Pi 는 이웃한 두 표본의 간격에서 두 표본의 시각 오차를 뺀 시간으로 속도를 구한다
    policy = StationaryPolicy(30000, 100000, 200000, METRES_PER_TICK, .05, 1., 0., 0.)
    profile = Feedback3Profile(config, firmware, 100, 100, 200000, LEASE_US, 200000, 100, 100, policy, 10000000,
                               .02, 20000)
    return Feedback3SerialSink(f'socket://127.0.0.1:{relay.port}', timeout_ms=HANDSHAKE_TIMEOUT_MS,
                               odometry=odometry, profile=profile)


def poll(sink):
    """Pi 쪽을 한 번 돌린다. 장치의 적용 보고는 OutputOwner 가 그러듯 꺼내 비운다"""
    sink.poll(time.monotonic())
    while not sink.events.empty():
        sink.events.get_nowait()


def poll_until(sink, seconds, done):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not done():
        poll(sink)
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


def stop_confirmed(sink):
    """Pi 가 DRIVE 를 내보내기 전에 하는 정지 검사를 지금 통과하는지. 마지막 STOP 의 APPLIED 보고와 그 뒤의 표본으로 본다"""
    now = int(sink.clock() * 1e6)
    matched, source, pin = sink.matched_stop, sink.odometry.current(now), sink.stop_pin
    if matched is None or source is None or pin is None:
        return False
    try:
        matched.check(dict(vehicle=source['vehicle'], calibration=source['calibration'], odom_epoch=source['odom_epoch']),
                      sink.odometry.host_boot, pin['gateway'], pin['frame'].generation, sink.pi_session,
                      sink.device_session, sink.stationary_policy, now)
    except ValueError:
        return False
    return True


def confirm_stop(sink, stop_frame, counter, witness):
    """STOP 을 보내고 Pi 가 정지를 확인할 때까지 돌린다. 확인하기까지 걸린 초, 확인하지 못했으면 None"""
    sent = time.monotonic()
    sink.write(stop_frame(1, counter & 255, 0, witness), sent)
    poll_until(sink, STOP_WAIT_S, lambda: stop_confirmed(sink))
    return time.monotonic() - sent if stop_confirmed(sink) else None


def cusp_witness(gateway_instance):
    """방향을 바꾸는 자리의 STOP 에 붙는 문맥. Pi 는 이런 STOP 뒤에만, 같은 문맥의 HOLD_STEER 를 내보낸다"""
    command = dict(next_direction=-1, calculation_sequence='1', plan_revision='1', phase_revision='1', segment_index=0,
                   cusp_index=0, mission_id=str(uuid4()), mission_revision='1', grant_revision='1',
                   driver_session=str(UUID(int=0)), driver_action_seq='0', driver_state_seq='0')
    return dict(gateway_instance=gateway_instance, vm_id='esp32-link-check', app_id='autonomous', generation=1,
                direction_intent_admitted=True, command=command)


def gate_state(sink, witness):
    """Pi 가 지금 내놓는 방향 전환 gate 의 state. 4 가 방향 전환을 허락하는 값이다. 내놓지 않으면 None"""
    machine = type('Machine', (), {'generation': witness['generation']})()
    gate = sink.direction_witness(witness['vm_id'], witness['app_id'], machine, int(sink.clock() * 1e6))
    return None if gate is None else gate.gate.state


def keep_holding(sink, drive_frame, counter, witness, seconds):
    """seconds 동안 HOLD_STEER 를 이어 보낸다. witness 의 command_kind 를 보고 Pi 가 HOLD_STEER 로 내보낸다.
    Pi 가 내보내지 않은 것은 건너뛰고 계속한다.
    (낸 횟수, 그 가운데 Pi 가 내보낸 수, 내보내지 않은 까닭들, 그동안 본 방향 전환 gate state 들)"""
    from xenova_gateway.sink import SinkError
    tried, sent, declined, states = 0, 0, [], set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        now = int(sink.clock() * 1e6)
        try:
            sink.write(drive_frame(1, (counter + tried) & 255, 0., HOLD_RAD, now, now + DRIVE_LIFE_US, witness),
                       time.monotonic())
            sent += 1
        except SinkError as error:
            declined.append(str(error))
        tried += 1
        poll_until(sink, DRIVE_PERIOD_S, lambda: False)
        states.add(gate_state(sink, witness))
    return tried, sent, declined, states


def keep_driving(sink, drive_frame, counter, witness, seconds):
    """seconds 동안 DRIVE 를 이어 보낸다. (보낸 수, Pi 가 보내기를 거절한 까닭 또는 None, 그동안 Pi 가 본 가장 큰 속도)"""
    from xenova_gateway.sink import SinkError
    sent, refused, fastest = 0, None, 0.
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        now = int(sink.clock() * 1e6)
        try:
            sink.write(drive_frame(1, (counter + sent) & 255, DRIVE_MPS, 0., now, now + DRIVE_LIFE_US, witness),
                       time.monotonic())
        except SinkError as error:
            refused = str(error)
            break
        sent += 1
        poll_until(sink, DRIVE_PERIOD_S, lambda: False)
        vehicle = sink.snapshot.get('vehicle_sample')
        if vehicle is not None:
            fastest = max(fastest, vehicle['velocity_mps'])
    return sent, refused, fastest


def applied_commands(relay, kind):
    """장치가 APPLIED 로 알린 그 종류 명령의 순번들"""
    reports = (REPORT3.unpack(row[1]) for row in relay.from_device if row[0] == REPORT and len(row[1]) == REPORT3.size)
    return {fields[2] for fields in reports if fields[5] == kind and fields[6] == 2}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_device_arguments(parser)
    parser.add_argument('--platform', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                                                           '..', 'platform'),
                        help='feedback-3 가 들어 있는 platform 폴더 (기본: 이 저장소)')
    parser.add_argument('--seconds', type=float, default=3.0,
                        help='첫 lease 를 받은 뒤 계속 주고받는 시간 (기본 3, 최소 2)')
    parser.add_argument('--drive', action='store_true',
                        help='실보드에 DRIVE·HOLD_STEER 까지 보낸다. 바퀴가 돌고 조향이 움직인다. '
                             '바퀴를 띄우고 모터 전원을 넣었을 때만 준다')
    args = parser.parse_args()
    if args.seconds < 2 * LEASE_US / 1e6:
        parser.error('--seconds 는 lease 길이의 두 배(2초) 이상이어야 중간에 끊긴 것이 드러난다')
    sys.stdout.reconfigure(errors='backslashreplace')
    gateway = load_gateway(args.platform)
    from xenova_gateway.frame import DriveFrame, StopFrame
    failed = 0

    def check(ok, text):
        nonlocal failed
        failed += not ok
        print(('ok    ' if ok else 'FAIL  ') + text)
        return bool(ok)

    device = open_device(args)
    try:
        capabilities, config, firmware, encoder_bits = describe_device(device)
        missing = REQUIRED_CAPS & ~capabilities
        print(f'      장치 capability 0x{capabilities:04x}, Pi 필수 0x{REQUIRED_CAPS:04x} 가운데 없는 것 0x{missing:04x}')

        if missing:
            print('거절 확인: 펌웨어가 알리는 capability 그대로')
            relay = Relay(device, 0)
            sink = make_sink(gateway, relay, config, firmware, encoder_bits)
            try:
                poll_until(sink, HANDSHAKE_TIMEOUT_MS / 1000 + 0.5, lambda: sink.fault is not None)
                # Pi 가 HELLO 를 다시 보내는 판이면 HELLO_ACK 도 그만큼 온다
                answered = relay.count(relay.from_device, HELLO_ACK)
                check(answered >= 1, f'장치가 Pi 의 HELLO 에 HELLO_ACK 로 답함 ({answered}개)')
                check(not sink.ready and 1 <= sink.rejected <= answered,
                      f'Pi 가 그 HELLO_ACK 를 받아들이지 않음 (ready {sink.ready}, rejected {sink.rejected})')
                check(sink.fault == 'DEVICE_HANDSHAKE_TIMEOUT', f'Pi 가 handshake 실패로 끝냄 (fault {sink.fault})')
                check(relay.count(relay.from_pi, CLOCK_PROBE) == 0, 'Pi 가 CLOCK_PROBE 를 보내지 않음')
                print('      ' + transcript(relay))
            finally:
                sink.close()
                relay.close()
            print(f'부분 호환성 시험: 중계가 HELLO_ACK 의 capability 에 모자란 bit(0x{missing:04x})를 채워 넣음')
        else:
            print('연결: 펌웨어가 알리는 capability 그대로')

        relay = Relay(device, missing)
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

            if not missing:
                def clean(text):
                    errors = dict(sink.odometry.errors)
                    return check(sink.fault is None and sink.rejected == 0 and not errors,
                                 f'{text} (fault {sink.fault}, 버린 프레임 {sink.rejected}개, odometry 오류 {errors or "없음"})')

                print('STOP: Pi 가 STOP 을 보내고 정지를 확인')
                witness = {'gateway_instance': str(uuid4())}
                took = confirm_stop(sink, StopFrame, 0, witness)
                check(took is not None and sink.stop_pin['applied'],
                      'Pi 가 장치의 APPLIED 보고와 표본으로 정지를 확인함'
                      + (f' ({took:.2f}초)' if took is not None else ''))
                check(len(applied_commands(relay, 2)) == 1, '장치가 그 STOP 을 APPLIED 로 알림')
                clean('그동안 Pi 가 장치를 고장으로 보거나 프레임을 버리지 않음')

                if args.sim or args.drive:
                    print(f'DRIVE: Pi 가 {DRIVE_S:.0f}초 동안 {DRIVE_MPS} m/s 의 DRIVE 를 이어 보냄')
                    sent, refused, fastest = keep_driving(sink, DriveFrame, 1, witness, DRIVE_S)
                    check(refused is None and sent >= DRIVE_S / DRIVE_PERIOD_S * 6 // 10,
                          f'Pi 가 DRIVE {sent}개를 보냄' + (f' (그만둔 까닭: {refused})' if refused else ''))
                    told = len(applied_commands(relay, 1))
                    check(told >= sent * 9 // 10 > 0, f'장치가 그 가운데 {told}개를 APPLIED 로 알림')
                    if args.sim:
                        check(abs(fastest - DRIVE_MPS) <= DRIVE_MPS * 15 / 100,
                              f'Pi 가 본 속도가 보낸 속도와 15 % 안에서 같음 (가장 큰 값 {fastest:.3f} m/s)')
                    else:
                        check(fastest > DRIVE_MPS / 10, f'Pi 가 바퀴가 도는 것을 봄 (가장 큰 속도 {fastest:.3f} m/s)')
                    poll_until(sink, 0.4, lambda: False)
                    before = sink.snapshot.get('vehicle_sample')
                    poll_until(sink, 0.1, lambda: False)
                    after = sink.snapshot.get('vehicle_sample')
                    check(before is not None and after is not None and after['velocity_mps'] == 0
                          and (before['left_ticks'], before['right_ticks']) == (after['left_ticks'], after['right_ticks']),
                          'DRIVE 를 그만 보내면 장치가 스스로 출력을 끊어 바퀴가 멈춤')
                    took = confirm_stop(sink, StopFrame, 1 + sent, witness)
                    check(took is not None, 'DRIVE 뒤의 STOP 으로 Pi 가 다시 정지를 확인함'
                          + (f' ({took:.2f}초)' if took is not None else ''))
                    clean('DRIVE 를 주고받는 동안 Pi 가 장치를 고장으로 보거나 프레임을 버리지 않음')

                    if capabilities & STATIONARY_STEER:
                        print(f'HOLD_STEER: Pi 가 방향을 바꾸는 자리의 STOP 뒤에 {HOLD_S}초 동안 HOLD_STEER 를 이어 보냄')
                        counter = 2 + sent
                        cusp = cusp_witness(witness['gateway_instance'])
                        took = confirm_stop(sink, StopFrame, counter, cusp)
                        check(took is not None, 'Pi 가 그 STOP 으로 정지를 확인함' + (f' ({took:.2f}초)' if took is not None else ''))
                        before = sink.snapshot.get('vehicle_sample')
                        steer = dict(cusp, command=dict(cusp['command'], command_kind='HOLD_STEER'))
                        tried, held, declined, states = keep_holding(sink, DriveFrame, counter + 1, steer, HOLD_S)
                        check(held >= HOLD_S / DRIVE_PERIOD_S * 6 // 10, f'Pi 가 HOLD_STEER {held}개를 내보냄')
                        check(not declined, f'Pi 가 내보내지 않은 HOLD_STEER 가 없음 ({len(declined)}개'
                              + (': ' + ', '.join(sorted(set(declined))) if declined else '') + ')')
                        told = len(applied_commands(relay, 3))
                        check(told >= held * 9 // 10 > 0, f'장치가 그 가운데 {told}개를 APPLIED 로 알림')
                        row = sink.hold_row
                        check(row is not None and row.get('applied'), 'Pi 가 HOLD_STEER 의 APPLIED 보고를 받아들임')
                        after = sink.snapshot.get('vehicle_sample')
                        check(before is not None and after is not None
                              and (before['left_ticks'], before['right_ticks']) == (after['left_ticks'], after['right_ticks']),
                              'HOLD_STEER 동안 Pi 가 본 엔코더 누계가 그대로임')
                        seen = ', '.join(str(state) for state in sorted(states, key=lambda state: (state is None, state)))
                        print(f'      그동안 Pi 의 방향 전환 gate state: {seen} (4 가 방향 전환 허락. 조향 측정이 없으면 '
                              '이르지 않는다. None 은 정지 확인 창이 통과하지 못한 때)')
                        took = confirm_stop(sink, StopFrame, counter + 1 + tried, witness)
                        check(took is not None, 'HOLD_STEER 뒤의 STOP 으로 Pi 가 다시 정지를 확인함'
                              + (f' ({took:.2f}초)' if took is not None else ''))
                        clean('HOLD_STEER 를 주고받는 동안 Pi 가 장치를 고장으로 보거나 프레임을 버리지 않음')
            print('      ' + transcript(relay))
        finally:
            sink.close()
            relay.close()
    finally:
        device.close()
    if failed:
        print(f'pi_link_check: 실패 {failed}건')
    elif missing:
        print('pi_link_check: 통과. 부분 호환성 시험이며 종단 연결 완료가 아니다')
    else:
        print('pi_link_check: 통과. capability 를 채우지 않고 맞물렸다. PC 에서 돌린 게이트웨이 코드이며 실제 Pi 의 USB 는 아니다')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
