"""PC 가 Pi 역할을 하며 펌웨어의 feedback-3 세션·시각 설치·주기 표본을 점검한다.

HELLO, CLOCK_PROBE, CLOCK_INSTALL 을 보내고 HELLO_ACK, CLOCK_REPLY, CLOCK_ACK 를 계약대로 받는지,
lease 가 살아 있는 동안에만 SENSOR 가 20 ms 마다 오는지, 그 사이에 보낸 CLOCK_PROBE 의 응답과 표본 순번·간격이
이어지는지, lease 가 기한에 만료되는지 본다.
모터·서보는 움직이지 않는다.

  실보드:            python link_bench.py --port COM8     (pyserial 필요, 다른 시리얼 창은 닫는다)
  호스트 시뮬레이터: python link_bench.py --sim <빌드 폴더>/sim_link

Pi Gateway 코드와의 연결은 pi_link_check.py 로 따로 본다.
"""
import argparse
import collections
import random
import statistics
import sys
import time
from uuid import UUID, uuid4

from fb3 import (ACK3, CLOCK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, HELLO, HELLO3,
                 HELLO_ACK, INSTALL3, PROBE3, REQUIRED_CAPS, SENSOR, SENSOR3, Peer, add_device_arguments, now_us,
                 open_device, packet, reason_name)

# CLOCK_INSTALL 을 Pi 와 같은 식으로 만들 때 쓰는 값. 실측값이 아니다
TIMESTAMP_ERROR_US = 100
DRIFT_PPM = 100
J_CUTOFF_US = 100
INSTALLED, REJECTED = 1, 3
SESSION_MISMATCH, SEQUENCE_REPLAY, IDEMPOTENCY_CONFLICT, CLOCK_UNAVAILABLE, CLOCK_EXPIRED = 3, 4, 5, 6, 7
DEVICE_FAULT, TIMESTAMP_ORDER = 11, 17
QUIET_S = 0.3  # 답이 없어야 할 때 기다리는 시간
# 펌웨어가 표본 사이에 두는 간격 (link.h 의 FB3_SENSOR_PERIOD_US, FB3_SAMPLE_GAP_US)
SENSOR_PERIOD_US = 20000
SAMPLE_GAP_US = 5000
# 표본이 제때보다 이만큼 넘게 늦으면 끊긴 것으로 본다. 시뮬레이터는 PC 가 멈칫하는 만큼 늦으므로 느슨하게 본다
BOARD_LATE_US = 3000
SIM_LATE_US = 80000
SWEEP_PROBES = 24         # lease 가 살아 있는 동안 보내 보는 CLOCK_PROBE 수
SWEEP_LEASE_US = 1000000  # 그동안 쓰는 lease 의 길이

# CLOCK_INSTALL 의 reference_pi_us, lower·upper_offset_us, valid_until_pi_us, cutoff_device_us
Terms = collections.namedtuple('Terms', 'reference lower upper expires cutoff')
# 표본을 실은 프레임 하나. kind 는 그 프레임의 종류다
Sample = collections.namedtuple('Sample', 'kind sequence device_us')


class Report:
    def __init__(self):
        self.checks = 0
        self.failed = 0

    def check(self, ok, text):
        self.checks += 1
        self.failed += not ok
        print(('ok    ' if ok else 'FAIL  ') + text)
        return bool(ok)

    def note(self, text):
        print('      ' + text)


class Flow:
    """응답을 기다리는 사이에 끼어드는 SENSOR 를 걸러 내고, 표본을 실은 프레임을 받은 순서대로 적어 둔다."""

    def __init__(self, peer):
        self.peer = peer
        self.samples = []  # CLOCK_REPLY 와 SENSOR 의 표본, 받은 순서
        self.sensors = []  # SENSOR 필드 11개, 받은 순서
        self.odd = 0       # 크기가 틀린 SENSOR

    def _next(self, deadline):
        """다음 프레임. 표본을 실었으면 적어 둔다. deadline 까지 오지 않으면 None"""
        frame = self.peer.receive(max(0.0, deadline - time.monotonic()))
        if frame is None:
            return None
        kind, payload = frame[:2]
        if kind == SENSOR:
            if len(payload) == SENSOR3.size:
                fields = SENSOR3.unpack(payload)
                self.sensors.append(fields)
                self.samples.append(Sample(kind, fields[2], fields[3]))
            else:
                self.odd += 1
        elif kind == CLOCK_REPLY and len(payload) == CLOCK3.size:
            fields = CLOCK3.unpack(payload)
            self.samples.append(Sample(kind, fields[7], fields[5]))
        return frame

    def answer(self, timeout=1.0):
        """SENSOR 가 아닌 다음 프레임을 (종류, payload, 받은 시각 us) 로. 시간 안에 오지 않으면 None"""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._next(deadline)
            if frame is None or frame[0] != SENSOR:
                return frame

    def sensor(self, timeout=1.0):
        """다음 SENSOR 를 받은 시각(us). 그 앞에 온 다른 프레임은 버린다. 시간 안에 오지 않으면 None"""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._next(deadline)
            if frame is None:
                return None
            if frame[0] == SENSOR and len(frame[1]) == SENSOR3.size:
                return frame[2]

    def listen(self, seconds):
        """seconds 동안 SENSOR 를 받아 적는다. 그동안 온 다른 프레임의 수를 돌려준다"""
        end = time.monotonic() + seconds
        others = 0
        while time.monotonic() < end:
            if self.answer(end - time.monotonic()) is not None:
                others += 1
        return others


def drift_pad(elapsed_us):
    return -(-DRIFT_PPM * elapsed_us // (1000000 - DRIFT_PPM))


def lease_terms(p1, p4, d2, d3, lease_us):
    """한 번의 probe 측정으로 Pi 와 같은 식의 offset 구간과 기한을 만든다 (계약 §5)."""
    pad = 2 * TIMESTAMP_ERROR_US + drift_pad(p4 - p1)
    upper = p4 - d3 + pad
    expires = p1 + lease_us
    return Terms(p4, p1 - d2 - pad, upper, expires, expires - (upper + drift_pad(expires - p4)) - J_CUTOFF_US)


def stream_problems(samples, late_us=None):
    """받은 순서대로의 표본에서 순번·시각·간격이 어긋난 곳을 글로 돌려준다.

    순번은 CLOCK_REPLY 와 SENSOR 를 통틀어 하나씩 늘어야 한다. 간격은 SENSOR 다음 SENSOR 가 한 주기,
    CLOCK_REPLY 다음 SENSOR 가 반 주기, CLOCK_REPLY 는 앞 표본에서 SAMPLE_GAP_US 이상이어야 한다.
    late_us 를 주면 lease 가 내내 살아 있던 구간으로 보고, SENSOR 가 그 간격보다 late_us 넘게 늦은 것도 어긋난 것으로 본다.
    """
    problems = []
    for before, after in zip(samples, samples[1:]):
        if after.sequence != before.sequence + 1:
            problems.append(f'표본 {before.sequence} 다음에 {after.sequence}')
            continue
        if after.kind == CLOCK_REPLY:
            least = SAMPLE_GAP_US
        else:
            least = SENSOR_PERIOD_US if before.kind == SENSOR else SENSOR_PERIOD_US // 2
        gap = after.device_us - before.device_us
        if gap < least:
            problems.append(f'표본 {after.sequence} 가 앞 표본에서 {gap} us 뒤 (적어도 {least} us)')
        elif late_us is not None and after.kind == SENSOR and gap > least + late_us:
            problems.append(f'표본 {after.sequence} 가 앞 표본에서 {gap} us 뒤 (늦어도 {least + late_us} us)')
    return problems


def lease_problems(sensors, pi, device, epoch, sequence, start_us, cutoff_us, late_us, encoder_bits):
    """한 lease 가 살아 있던 장치 시각 start_us 부터 cutoff_us 사이에 온 SENSOR 가 어긋난 곳을 글로 돌려준다.

    모두 그 session 과 lease 를 가리키고, 표본 시각이 그 구간 안이고, 처음부터 끝까지 한 주기에 late_us 를
    더한 것보다 길게 끊긴 데가 없어야 한다.
    """
    if not sensors:
        return ['SENSOR 가 하나도 오지 않음']
    problems = []
    limit = 2 ** (encoder_bits - 1)
    for fields in sensors:
        s_pi, s_device, sample, stamp, left, right, quality, steering, error, s_epoch, s_sequence = fields
        steered = quality & 6
        if (s_pi, s_device) != (pi, device):
            problems.append(f'표본 {sample} 의 session 이 다름')
        elif (s_epoch, s_sequence) != (epoch, sequence):
            problems.append(f'표본 {sample} 가 clock_sequence {s_sequence} 를 가리킴')
        elif not start_us < stamp < cutoff_us:
            problems.append(f'표본 {sample} 의 시각이 lease 가 살아 있던 구간 밖 (cutoff 기준 {stamp - cutoff_us} us)')
        elif not quality & 1 or quality & ~7 or steered == 6 or not -limit <= left < limit or not -limit <= right < limit:
            problems.append(f'표본 {sample} 의 quality {quality}·누계 {left}, {right} 가 계약 밖')
        elif (error < 1 or abs(steering) > 1400) if steered else (steering or error):
            problems.append(f'표본 {sample} 의 조향 {steering} mrad·오차 {error} mrad 가 quality {quality} 와 안 맞음')
    marks = [start_us] + [fields[3] for fields in sensors] + [cutoff_us]
    widest = max(after - before for before, after in zip(marks, marks[1:]))
    if widest > SENSOR_PERIOD_US + late_us:
        problems.append(f'표본이 {widest} us 동안 끊김 (허용 {SENSOR_PERIOD_US + late_us} us)')
    return problems


def run(flow, lease_us, late_us, report):
    check, note = report.check, report.note
    peer = flow.peer

    # 1. 장치가 무엇을 알리는지 본다. 등록 config 를 모르므로 0 으로 채운 digest 로 묻는다
    asker = uuid4().bytes
    peer.send(HELLO, HELLO3.pack(3, asker, bytes(32)))
    frame = flow.answer()
    if not check(frame is not None and frame[0] == HELLO_ACK and len(frame[1]) == ACK3.size,
                 'HELLO 에 HELLO_ACK 로 답함'):
        return
    version, echoed, device, capabilities, config, firmware, encoder_bits = ACK3.unpack(frame[1])
    check(version == 3 and echoed == asker, 'HELLO_ACK 가 버전 3 과 보낸 pi_session 을 돌려줌')
    check(any(device), 'device_boot_session 이 NIL 이 아님')
    check(encoder_bits in (32, 64), f'encoder_bits {encoder_bits}')
    note(f'device_boot_session {UUID(bytes=device)}')
    note(f'capabilities 0x{capabilities:04x} (Pi 필수 0x{REQUIRED_CAPS:04x} 가운데 없는 것 0x{REQUIRED_CAPS & ~capabilities:04x})')
    note(f'vehicle_config_sha256 {config.hex()}')
    note(f'firmware_sha256 {firmware.hex()}')
    if config != bytes(32):
        peer.send(CLOCK_PROBE, PROBE3.pack(asker, device, 1, now_us()))
        check(flow.answer(QUIET_S) is None, '등록 config 가 다른 HELLO 뒤에는 CLOCK_PROBE 에 답하지 않음')

    # 2. 장치의 config digest 로 session 을 맺는다
    pi = uuid4().bytes
    hello = packet(HELLO, HELLO3.pack(3, pi, config))
    peer.send_wire(hello)
    first = flow.answer()
    if not check(first is not None and first[0] == HELLO_ACK and ACK3.unpack(first[1])[1:3] == (pi, device),
                 '같은 config 의 HELLO 에 같은 device_boot_session 으로 답함'):
        return
    peer.send_wire(hello)
    again = flow.answer()
    check(again is not None and again[:2] == first[:2], '같은 HELLO 재전송에 같은 HELLO_ACK')

    # 3. CLOCK_PROBE → CLOCK_REPLY
    def probe(sequence, session=pi, wait=1.0):
        """(p1, p4, CLOCK_REPLY 필드 13개). 답이 없으면 None"""
        p1 = now_us()
        peer.send(CLOCK_PROBE, PROBE3.pack(session, device, sequence, p1))
        reply = flow.answer(wait)
        if reply is None or reply[0] != CLOCK_REPLY or len(reply[1]) != CLOCK3.size:
            return None
        return (p1, reply[2]) + CLOCK3.unpack(reply[1])

    one = probe(1)
    if not check(one is not None, 'CLOCK_PROBE 에 CLOCK_REPLY 로 답함'):
        return
    p1, p4, r_pi, r_device, r_sequence, r_p1, d2, latch, d3, sample, left, right, quality, steering, error = one
    check((r_pi, r_device, r_sequence, r_p1) == (pi, device, 1, p1), 'session·probe_sequence·p1 을 그대로 돌려줌')
    check(0 < d2 <= latch <= d3 < 2 ** 63, f'0 < d2 <= latch <= d3 (d2 {d2}, latch +{latch - d2}, d3 +{d3 - d2} us)')
    steered = quality & 6
    check(quality & 1 and not quality & ~7 and steered != 6,
          f'quality {quality}: 엔코더 bit 가 있고 조향은 측정·추정 가운데 하나 이하')
    check((error >= 1 and abs(steering) <= 1400) if steered else (steering == 0 and error == 0),
          f'조향 {steering} mrad, 오차 {error} mrad 가 quality 와 맞음')
    limit = 2 ** (encoder_bits - 1)
    check(sample >= 1 and -limit <= left < limit and -limit <= right < limit,
          f'표본 {sample}: 누계 좌 {left}, 우 {right}')
    note(f'왕복 {p4 - p1} us, 장치 안에서 {d3 - d2} us, Pi-장치 offset {p1 - d2}..{p4 - d3} us')

    time.sleep(0.05)
    two = probe(2)
    if not check(two is not None, '두 번째 CLOCK_PROBE 에도 답함'):
        return
    q1, q4, e2, latch2, e3, sample2 = two[0], two[1], two[6], two[7], two[8], two[9]
    check(sample2 == sample + 1 and latch2 > latch and e2 > d3, '새로 뜬 표본: 순번 +1, 시각이 앞 응답보다 뒤')
    check(max(p1 - d2, q1 - e2) <= min(p4 - d3, q4 - e3), '두 측정의 offset 구간이 겹침')
    check(probe(1, wait=QUIET_S) is None, '지난 probe_sequence 에는 답하지 않음')
    check(probe(3, uuid4().bytes, QUIET_S) is None, '다른 pi_session 의 CLOCK_PROBE 에는 답하지 않음')

    # 4. CLOCK_INSTALL → CLOCK_ACK
    def exchange(wire):
        """(CLOCK_ACK 필드 9개, 받은 시각). 답이 없으면 None"""
        peer.send_wire(wire)
        reply = flow.answer()
        if reply is None or reply[0] != CLOCK_ACK or len(reply[1]) != CLOCK_ACK3.size:
            return None
        return CLOCK_ACK3.unpack(reply[1]) + (reply[2],)

    def install(epoch, sequence, probe_sequence, terms):
        wire = packet(CLOCK_INSTALL, INSTALL3.pack(pi, device, epoch, sequence, probe_sequence, *terms))
        return wire, exchange(wire)

    def refused(ack, reason):
        return ack is not None and ack[5:7] == (REJECTED, reason)

    def outcome(ack):
        return '답 없음' if ack is None else f'status {ack[5]}, reason {reason_name(ack[6])}'

    # lease 는 probe 를 보낸 때부터 lease_us 동안이라, 설치 직전에 새로 잰다
    three = probe(3)
    if not check(three is not None, '세 번째 CLOCK_PROBE 에도 답함'):
        return
    d3 = three[8]
    epoch = uuid4().bytes
    terms = lease_terms(three[0], three[1], three[6], d3, lease_us)
    check(refused(install(epoch, 5, 9999, terms)[1], CLOCK_UNAVAILABLE), '보낸 적 없는 probe 를 가리키면 REJECTED CLOCK_UNAVAILABLE')
    check(refused(install(epoch, 5, 3, terms._replace(cutoff=d3))[1], TIMESTAMP_ORDER),
          'cutoff 가 d3 보다 뒤가 아니면 REJECTED TIMESTAMP_ORDER')

    wire, ack = install(epoch, 5, 3, terms)
    if not check(ack is not None and ack[5:7] == (INSTALLED, 0), f'CLOCK_INSTALL 에 INSTALLED 로 답함 ({outcome(ack)})'):
        return
    check(not flow.sensors, f'lease 를 설치하기 전에는 SENSOR 를 보내지 않음 ({len(flow.sensors)}개)')
    check(ack[:5] == (pi, device, epoch, 5, 3) and ack[8] == terms.cutoff, 'session·epoch·순번·probe·cutoff 를 그대로 돌려줌')
    check(d3 < ack[7] < terms.cutoff and ack[9] < terms.expires,
          f'device_now 가 d3 와 cutoff 사이 (cutoff 까지 {terms.cutoff - ack[7]} us), Pi 기한 전에 도착')
    installed_us = ack[7]
    retry = exchange(wire)
    check(retry is not None and retry[:9] == ack[:9], '같은 CLOCK_INSTALL 재전송에 같은 CLOCK_ACK')
    check(refused(install(epoch, 5, 3, terms._replace(upper=terms.upper + 1))[1], IDEMPOTENCY_CONFLICT),
          '같은 순번의 다른 내용은 REJECTED IDEMPOTENCY_CONFLICT')
    check(refused(install(uuid4().bytes, 6, 3, terms)[1], SESSION_MISMATCH), '다른 clock_epoch 는 REJECTED SESSION_MISMATCH')

    # 5. 기한이 지나면 lease 가 만료된다. cutoff 는 장치 시계로 Pi 기한(expires)보다 앞이다
    flow.listen(max(0, terms.expires + 50000 - now_us()) / 1e6)
    late = exchange(wire)
    check(refused(late, CLOCK_EXPIRED), f'기한 뒤 재전송은 REJECTED CLOCK_EXPIRED ({outcome(late)})')
    if refused(late, DEVICE_FAULT):
        note('DEVICE_FAULT 는 기한에 alarm 이 울리지 않았다는 뜻이다')
    elif late is not None:
        check(late[7] >= terms.cutoff, f'그때 device_now 는 cutoff 를 {late[7] - terms.cutoff} us 지남')

    # 6. lease 가 살아 있던 동안에는 SENSOR 가 이어졌고, 기한 뒤에는 오지 않는다
    during = list(flow.sensors)
    problems = lease_problems(during, pi, device, epoch, 5, installed_us, terms.cutoff, late_us, encoder_bits)
    check(not problems, f'lease 가 살아 있는 {terms.cutoff - installed_us} us 동안 그 lease 를 가리키는 SENSOR 가 '
                        f'{SENSOR_PERIOD_US + late_us} us 넘게 끊기지 않고 옴 ({len(during)}개)')
    for text in problems[:3]:
        note(text)
    if len(during) >= 2:
        gaps = [after[3] - before[3] for before, after in zip(during, during[1:])]
        note(f'SENSOR 간격 중앙값 {statistics.median(gaps):.0f} us, 최소 {min(gaps)}, 최대 {max(gaps)} us. '
             f'마지막 표본은 cutoff {terms.cutoff - during[-1][3]} us 전')
    check(flow.listen(QUIET_S) == 0 and len(flow.sensors) == len(during), '기한이 지난 뒤에는 SENSOR 를 보내지 않음')

    # 7. 만료 뒤에도 새 lease 를 설치할 수 있고, 지난 순번은 받지 않는다
    four = probe(4)
    if check(four is not None, '만료 뒤에도 CLOCK_PROBE 에 답함'):
        fresh = lease_terms(four[0], four[1], four[6], four[8], lease_us)
        ack = install(epoch, 6, 4, fresh)[1]
        check(ack is not None and ack[5:7] == (INSTALLED, 0), f'새 lease 를 INSTALLED 로 설치 ({outcome(ack)})')
        check(refused(install(epoch, 4, 4, fresh)[1], SEQUENCE_REPLAY), '지난 clock_sequence 는 REJECTED SEQUENCE_REPLAY')
        flow.listen(0.2)
        renewed = flow.sensors[len(during):]
        check(len(renewed) >= 4 and all(fields[9:11] == (epoch, 6) for fields in renewed),
              f'새 lease 를 가리키는 SENSOR 가 다시 옴 (0.2초에 {len(renewed)}개)')

    # 8. lease 가 살아 있는 동안 CLOCK_PROBE 를 여러 번 보낸다. 표본은 CLOCK_REPLY 와 SENSOR 를 오가며 이어지고,
    #    SENSOR 의 표본에 붙어 온 probe 에는 간격이 찬 뒤에 답한다. 도중에 설치한 새 lease 로 SENSOR 가 넘어간다
    spread = random.Random(453)
    first = probe(5)
    if not check(first is not None, 'lease 가 살아 있는 동안에도 CLOCK_PROBE 에 답함'):
        return
    ack = install(epoch, 7, 5, lease_terms(first[0], first[1], first[6], first[8], SWEEP_LEASE_US))[1]
    if not check(ack is not None and ack[5:7] == (INSTALLED, 0), f'1초짜리 lease 를 INSTALLED 로 설치 ({outcome(ack)})'):
        return
    sweep_from, pointed_from, switched = len(flow.samples), len(flow.sensors), None
    answered, waits = 0, []
    for index in range(SWEEP_PROBES):
        # SENSOR 하나를 받은 때부터 한 주기 안의 아무 때에 보내, probe 가 주기의 모든 자리에 고루 닿게 한다
        heard = flow.sensor()
        send_at = (now_us() if heard is None else heard) + spread.uniform(0, SENSOR_PERIOD_US)
        while now_us() < send_at:
            pass
        measured = probe(6 + index)
        if measured is None:
            continue
        answered += 1
        waits.append(measured[7] - measured[6])
        if index == SWEEP_PROBES // 2:
            terms = lease_terms(measured[0], measured[1], measured[6], measured[8], SWEEP_LEASE_US)
            ack = install(epoch, 8, 6 + index, terms)[1]
            check(ack is not None and ack[5:7] == (INSTALLED, 0), f'그 사이에 새 lease 를 INSTALLED 로 설치 ({outcome(ack)})')
            switched = len(flow.sensors)
    flow.listen(0.05)
    check(answered == SWEEP_PROBES, f'그동안 보낸 CLOCK_PROBE {SWEEP_PROBES}개에 모두 답함 ({answered}개)')
    held = [wait for wait in waits if wait >= SAMPLE_GAP_US // 10]
    check(held and max(held) <= SAMPLE_GAP_US + late_us,
          f'SENSOR 의 표본에 붙어 온 probe 에는 간격이 찬 뒤에 답함 ({len(held)}개, 받고 나서 표본까지 가장 길게 {max(waits, default=0)} us)')
    if switched is not None:
        old, new = flow.sensors[pointed_from:switched], flow.sensors[switched:]
        check(old and new and all(fields[10] == 7 for fields in old) and all(fields[10] == 8 for fields in new),
              f'새 lease 를 설치한 뒤의 SENSOR 는 새 lease 를 가리킴 (앞 lease {len(old)}개, 새 lease {len(new)}개)')
    sweep = flow.samples[sweep_from:]
    problems = stream_problems(sweep, late_us)
    check(not problems and len(sweep) >= 2 * SWEEP_PROBES,
          f'그동안의 표본 {len(sweep)}개가 CLOCK_REPLY 와 SENSOR 를 오가며 간격을 지키고 늦지 않음')
    for text in problems[:3]:
        note(text)

    # 9. 새 pi_session 이 오면 앞 session 의 것은 받지 않는다
    newcomer = uuid4().bytes
    peer.send(HELLO, HELLO3.pack(3, newcomer, config))
    reply = flow.answer()
    check(reply is not None and reply[0] == HELLO_ACK and ACK3.unpack(reply[1])[1:3] == (newcomer, device),
          '새 pi_session 의 HELLO 에 같은 device_boot_session 으로 답함')
    before = len(flow.sensors)
    check(probe(999, wait=QUIET_S) is None, '앞 pi_session 의 CLOCK_PROBE 에는 답하지 않음')
    check(len(flow.sensors) == before, '새 pi_session 이 온 뒤에는 앞 session 의 lease 로 SENSOR 를 보내지 않음')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_device_arguments(parser)
    parser.add_argument('--lease-ms', type=int, default=500, help='설치할 lease 길이 (기본 500)')
    args = parser.parse_args()
    sys.stdout.reconfigure(errors='backslashreplace')  # 한글을 못 쓰는 콘솔에서도 멈추지 않게
    report = Report()
    device = open_device(args)
    peer = Peer(device)
    flow = Flow(peer)
    try:
        run(flow, args.lease_ms * 1000, SIM_LATE_US if args.sim else BOARD_LATE_US, report)
    finally:
        device.close()
    problems = stream_problems(flow.samples)
    report.check(not problems and not flow.odd,
                 f'표본 {len(flow.samples)}개의 순번이 CLOCK_REPLY 와 SENSOR 를 통틀어 하나씩 늘고 간격을 지킴')
    for text in problems[:3]:
        report.note(text)
    if flow.odd:
        report.note(f'크기가 틀린 SENSOR {flow.odd}개')
    # 응답이 깨져 오면 그 응답을 기다리던 검사에서 멈춘다. 그때도 깨진 조각을 보여 준다
    report.check(peer.invalid == 0, f'깨진 프레임 {peer.invalid}개')
    for wire in peer.broken[:3]:
        report.note(f'{len(wire)} B: {wire.hex()}')
    print(f'link_bench: {report.checks} checks, {report.failed} failed')
    return 1 if report.failed else 0


if __name__ == '__main__':
    sys.exit(main())
