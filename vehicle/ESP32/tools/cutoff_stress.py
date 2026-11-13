"""cutoff alarm 을 '지금'에 바짝 붙인 기한으로 되풀이해 걸어 본다.

네 가지 시나리오를 목표 횟수만큼 검증한다. 검증 한 번은 건 기한이 모두 타이머에 걸렸고(INSTALLED),
기한 뒤 재전송에 REJECTED CLOCK_EXPIRED 가 온 것까지 본 것이다.

  기한 하나        cutoff 를 장치가 그 프레임을 처리하는 순간의 앞뒤 1 ms 안에 둔다
  먼 기한 먼저     150 ms 뒤 기한을 먼저 걸고, 위와 같은 기한을 하나 더 건다
  차례로           45 ms 뒤 기한과 90 ms 뒤 기한을 차례로 건다
  수십 us 차이로   45 ms 뒤 기한과 그보다 0 에서 79 us 늦은 기한을 건다

기한 둘을 건 시나리오는 둘 다 타이머에 걸려야 하고, 앞 기한이 끝난 뒤에 뒤 기한이 아직 살아 있는 것과
제때 만료되는 것을 모두 봐야 한다 (수십 us 차이인 둘은 사이에 볼 틈이 없어 둘 다 제때 만료되는 것만 본다).
처리 순간에 붙인 기한이 이미 지나 있어서 그 자리에서 REJECTED CLOCK_EXPIRED 가 온 것은 타이머에 걸린 적이
없으므로 따로 세고 검증 횟수에 넣지 않는다. DEVICE_FAULT 가 나오면 기한에 alarm 이 울리지 않은 것이다.

정한 시도 안에 목표를 채우지 못하거나 답이 잇달아 오지 않으면 통과가 아니라 미완료로 끝난다.

  실보드:            python cutoff_stress.py --port COM8 [--trials 1000]
  호스트 시뮬레이터: python cutoff_stress.py --sim <빌드 폴더>/sim_link
"""
import argparse
import collections
import random
import statistics
import sys
import time
from uuid import uuid4

from fb3 import (ACK3, CLOCK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, HELLO, HELLO3,
                 HELLO_ACK, INSTALL3, PROBE3, Peer, add_device_arguments, now_us, open_device, packet, reason_name)
from link_bench import lease_terms

INSTALLED, REJECTED, CLOCK_EXPIRED, TIMESTAMP_ORDER = 1, 3, 7, 17
NEAR_US = (-700, 1100)  # 처리 순간에 붙이는 기한을 그 순간 기준으로 어디에 둘지
SETTLE_US = 3000        # 기한이 지났다고 볼 여유. 펌웨어가 alarm 을 놓쳤다고 보는 1 ms 보다 길다
ONE, FAR_FIRST, IN_TURN, TWINS = '기한 하나', '먼 기한 먼저', '차례로', '수십 us 차이로'
ORDER = (ONE, ONE, FAR_FIRST, IN_TURN, TWINS)  # 도는 순서. 목표 횟수도 이 비율로 나눈다
ATTEMPTS_PER_GOAL = 3   # 목표 한 번마다 허용하는 시도 수. 처리 순간에 붙인 기한은 절반쯤이 이미 지나 있다
SILENT_LIMIT = 10       # 잇달아 이만큼 답을 받지 못하면 그만둔다
Lease = collections.namedtuple('Lease', 'sequence wire cutoff ack')


class Failed(Exception):
    """장치의 답이 기대와 다르다."""


class Lost(Exception):
    """답을 받지 못해 이번 시도로는 판정할 수 없다."""


class Link:
    """session 을 맺고 CLOCK_INSTALL 을 보내며, 지금 보낸 프레임을 장치가 처리할 장치 시각을 어림한다."""

    def __init__(self, peer):
        self.peer = peer
        self.lost = collections.Counter()  # 답이 오지 않았거나 깨져 온 횟수 (보낸 프레임 종류별)
        self.probe_sequence = 0
        self.clock_sequence = 0
        self.offset = None  # 첫 probe 의 p1 - d2. PC 시각에서 빼면 장치 시각 언저리가 된다
        self.lags = collections.deque(maxlen=15)  # ACK 의 device_now 가 그 어림보다 얼마나 뒤였는지
        self.pi, self.epoch = uuid4().bytes, uuid4().bytes
        self.device, config = self._hello(uuid4().bytes, bytes(32))
        self._hello(self.pi, config)

    def _hello(self, pi, config):
        for _ in range(3):
            self.peer.send(HELLO, HELLO3.pack(3, pi, config))
            frame = self.peer.receive()
            if frame is not None and frame[0] == HELLO_ACK and len(frame[1]) == ACK3.size:
                fields = ACK3.unpack(frame[1])
                if fields[1] == pi:
                    return fields[2], fields[4]
            self.lost['HELLO'] += 1
        sys.exit('장치가 HELLO 에 HELLO_ACK 로 답하지 않는다')

    def _reply(self, kind, layout, at, sequence, sent_as):
        """보낸 것과 순번이 같은 응답의 (필드들, 받은 시각). 0.5초 안에 오지 않으면 None"""
        deadline = time.monotonic() + 0.5
        while True:
            frame = self.peer.receive(deadline - time.monotonic())
            if frame is None:
                self.lost[sent_as] += 1
                return None
            if frame[0] == kind and len(frame[1]) == layout.size:
                fields = layout.unpack(frame[1])
                if fields[at] == sequence:
                    return fields, frame[2]

    def probe(self):
        """(p1, p4, d2, d3). 답을 받지 못하면 None"""
        self.probe_sequence += 1
        p1 = now_us()
        self.peer.send(CLOCK_PROBE, PROBE3.pack(self.pi, self.device, self.probe_sequence, p1))
        reply = self._reply(CLOCK_REPLY, CLOCK3, 2, self.probe_sequence, 'CLOCK_PROBE')
        if reply is None:
            return None
        fields, p4 = reply
        if self.offset is None:
            self.offset = p1 - fields[4]
        return p1, p4, fields[4], fields[6]

    def ask(self, lease):
        """CLOCK_INSTALL 을 보내고 CLOCK_ACK 필드 9개를 받는다. 답을 받지 못하면 None"""
        sent = now_us()
        self.peer.send_wire(lease.wire)
        reply = self._reply(CLOCK_ACK, CLOCK_ACK3, 3, lease.sequence, 'CLOCK_INSTALL')
        if reply is None:
            return None
        ack = reply[0]
        if ack != lease.ack:  # 살아 있는 lease 의 재전송에는 처음 적어 둔 device_now 가 온다
            self.lags.append(ack[7] - (sent - self.offset))
        return ack

    def after(self, margin_us):
        """지금 보내는 프레임을 장치가 처리할 때부터 margin_us 뒤의 장치 시각 (어림)"""
        return int(now_us() - self.offset + (statistics.median(self.lags) if self.lags else 0) + margin_us)

    def install(self, measured, cutoff):
        self.clock_sequence += 1
        terms = lease_terms(*measured, 500000)._replace(cutoff=cutoff)
        wire = packet(CLOCK_INSTALL, INSTALL3.pack(self.pi, self.device, self.epoch, self.clock_sequence,
                                                   self.probe_sequence, *terms))
        lease = Lease(self.clock_sequence, wire, cutoff, None)
        return lease._replace(ack=self.ask(lease))

    def settle(self, lease):
        """기한이 지난 뒤 다시 보내 마지막 답을 받는다. 아직 기한 전이라 같은 INSTALLED 가 오면 더 기다린다.
        끝까지 INSTALLED 면 그 답을, 답이 한 번도 오지 않았으면 None 을 돌려준다."""
        last = None
        for _ in range(8):
            time.sleep(max(0, lease.cutoff + SETTLE_US - self.after(0)) / 1e6)
            ack = self.ask(lease)
            if ack is not None:
                last = ack
                if not (ack[5] == INSTALLED and ack[7] < lease.cutoff):
                    break
        return last

    def calibrate(self):
        """장치까지 걸리는 시간을 잰다. 넉넉한 기한으로 몇 번 설치하고 alarm 으로 끝나게 둔다. 재지 못하면 False"""
        for _ in range(20):
            if len(self.lags) >= 5:
                time.sleep(0.4)
                return True
            measured = self.probe()
            if measured is not None:
                self.install(measured, self.after(300000))
        return False


class Outcome:
    """run 의 결과."""

    def __init__(self, goal):
        self.goal = goal                       # 시나리오별 목표 검증 횟수
        self.verified = collections.Counter()  # 시나리오별로 검증을 끝낸 횟수
        self.attempts = 0
        self.rejected = 0    # 처리 순간에 붙인 기한이 이미 지나 그 자리에서 거절된 횟수. 타이머에 걸리지 않았다
        self.early = 0       # 그런 기한이 probe 응답보다도 앞서 TIMESTAMP_ORDER 로 거절된 횟수
        self.margins = []    # INSTALLED 로 답한 CLOCK_ACK 를 만들 때 cutoff 까지 남아 있던 시간
        self.overdue = []    # 그 자리에서 거절한 CLOCK_ACK 를 만들 때 cutoff 를 지나 있던 시간
        self.problems = []   # 장치의 답이 기대와 달랐던 것
        self.stopped = None  # 목표를 채우기 전에 그만둔 까닭

    def complete(self):
        return all(self.verified[kind] >= count for kind, count in self.goal.items())

    def verdict(self):
        """(끝줄에 쓸 말, 종료 코드). 목표를 다 채우고 문제가 없을 때만 통과다"""
        if self.problems:
            return '실패', 1
        if self.stopped is not None or not self.complete():
            return '미완료', 2
        return '통과', 0


def goals(trials):
    """시험 횟수를 시나리오별 목표 검증 횟수로 나눈다."""
    return {kind: -(-trials * ORDER.count(kind) // len(ORDER)) for kind in dict.fromkeys(ORDER)}


def describe(ack):
    return '답 없음' if ack is None else f'status {ack[5]}, reason {reason_name(ack[6])}'


def run(link, trials):
    """시나리오마다 목표 횟수만큼 검증을 끝낼 때까지 돌리고 Outcome 을 돌려준다."""
    outcome = Outcome(goals(trials))
    budget = ATTEMPTS_PER_GOAL * sum(outcome.goal.values()) + SILENT_LIMIT

    def answered(lease):
        """설치에 온 첫 CLOCK_ACK. 보낸 cutoff 를 그대로 돌려줘야 한다"""
        if lease.ack is None:
            raise Lost
        if lease.ack[8] != lease.cutoff:
            raise Failed(f'보낸 cutoff {lease.cutoff} 대신 {lease.ack[8]} 를 돌려줌')
        return lease.ack

    def at_the_edge(lease, d3):
        """처리 순간에 붙인 기한. 타이머에 걸렸으면 True, 이미 지나 그 자리에서 거절됐으면 False,
        probe 응답보다도 앞서서 거절됐으면 None (주고받는 시간이 거의 없는 시뮬레이터에서만 나온다)"""
        ack = answered(lease)
        if ack[5:7] == (INSTALLED, 0):
            if ack[7] >= lease.cutoff:
                raise Failed(f'INSTALLED 인데 device_now 가 cutoff 를 {ack[7] - lease.cutoff} us 지남')
            outcome.margins.append(lease.cutoff - ack[7])
            return True
        if ack[5:7] == (REJECTED, CLOCK_EXPIRED):
            if ack[7] < lease.cutoff:
                raise Failed(f'cutoff 까지 {lease.cutoff - ack[7]} us 남았는데 CLOCK_EXPIRED')
            outcome.rejected += 1
            outcome.overdue.append(ack[7] - lease.cutoff)
            return False
        if ack[5:7] == (REJECTED, TIMESTAMP_ORDER) and lease.cutoff <= d3:
            outcome.early += 1
            return None
        raise Failed(describe(ack))

    def on_timer(lease):
        """여유를 두고 건 기한이 타이머에 걸렸는가. PC 가 늦어 이미 지난 기한이 됐으면 False"""
        ack = answered(lease)
        if ack[5:7] == (INSTALLED, 0):
            if ack[7] >= lease.cutoff:
                raise Failed(f'INSTALLED 인데 device_now 가 cutoff 를 {ack[7] - lease.cutoff} us 지남')
            return True
        if ack[5:7] == (REJECTED, CLOCK_EXPIRED) and ack[7] >= lease.cutoff:
            return False
        raise Failed(f'여유 있는 기한 설치에 {describe(ack)}')

    def alive(lease):
        """앞 기한이 끝난 지금도 살아 있는 것을 봤으면 True. 살아 있으면 처음과 같은 CLOCK_ACK 가 온다.
        기한이 코앞이거나 답을 받지 못해 보지 못했으면 False"""
        if link.after(0) > lease.cutoff - 2 * SETTLE_US:
            return False
        ack = link.ask(lease)
        if ack is None:
            return False
        if ack == lease.ack:
            return True
        if ack[7] < lease.cutoff:
            raise Failed(f'cutoff 까지 {lease.cutoff - ack[7]} us 남았는데 {describe(ack)}')
        return False

    def expect_expired(lease):
        ack = link.settle(lease)
        if ack is None:
            raise Lost
        if ack[5:7] != (REJECTED, CLOCK_EXPIRED):
            raise Failed(f'기한 뒤 재전송에 {describe(ack)}')
        if ack[7] < lease.cutoff:
            raise Failed(f'cutoff 까지 {lease.cutoff - ack[7]} us 남았는데 CLOCK_EXPIRED')

    def attempt(kind):
        """시나리오를 한 번 해 본다. 건 기한이 모두 타이머에 걸렸다가 제때 만료된 것까지 봤으면 True"""
        measured = link.probe()
        if measured is None:
            raise Lost
        if kind in (ONE, FAR_FIRST):
            later = link.install(measured, link.after(150000)) if kind == FAR_FIRST else None
            sooner = link.install(measured, link.after(random.uniform(*NEAR_US)))
            held = at_the_edge(sooner, measured[3])
            if held is not None:
                expect_expired(sooner)
        else:
            sooner = link.install(measured, link.after(45000))
            later = link.install(measured, link.after(90000) if kind == IN_TURN
                                 else sooner.cutoff + random.randrange(80))
            held = on_timer(sooner)
            if held:
                expect_expired(sooner)
        if later is None:
            return bool(held)
        if not on_timer(later):
            return False
        # 수십 us 차이인 두 기한은 사이에 볼 틈이 없다. 나머지는 뒤 기한이 살아 있는 것까지 봐야 한 번이다
        outlived = kind == TWINS or alive(later)
        expect_expired(later)
        return bool(held) and outlived

    turn = silent = 0
    while not outcome.complete():
        if outcome.attempts >= budget:
            outcome.stopped = f'{budget}번 시도 안에 목표를 채우지 못함'
            break
        if silent >= SILENT_LIMIT:
            outcome.stopped = f'잇달아 {SILENT_LIMIT}번 답을 받지 못함'
            break
        while outcome.verified[ORDER[turn % len(ORDER)]] >= outcome.goal[ORDER[turn % len(ORDER)]]:
            turn += 1
        kind = ORDER[turn % len(ORDER)]
        turn += 1
        outcome.attempts += 1
        try:
            if attempt(kind):
                outcome.verified[kind] += 1
            silent = 0
        except Lost:
            silent += 1
        except Failed as failure:
            outcome.problems.append(f'{outcome.attempts}번째 시도 ({kind}): {failure}')
            break
    return outcome


def report(outcome):
    done = ', '.join(f'{kind} {outcome.verified[kind]}/{goal}' for kind, goal in outcome.goal.items())
    print(f'타이머에 걸렸다가 제때 만료된 것까지 확인: {done} (시도 {outcome.attempts}번)')
    print(f'이미 지난 기한이라 그 자리에서 거절: {outcome.rejected}번. 타이머에 걸리지 않았으므로 위 횟수에 넣지 않는다')
    if outcome.early:
        print(f'probe 응답보다 앞선 기한이라 거절: {outcome.early}번')
    for name, values in (('INSTALLED 로 답할 때 cutoff 까지 남아 있던 시간', outcome.margins),
                         ('그 자리에서 거절할 때 cutoff 를 지나 있던 시간', outcome.overdue)):
        if values:
            near = ', '.join(f'{limit} us 미만 {sum(value < limit for value in values)}' for limit in (20, 50, 200))
            print(f'  {name}: 최소 {min(values)} us, {near}')
    if outcome.margins or outcome.overdue:
        print('  (장치가 CLOCK_ACK 를 만든 시각으로 잰 것이다. alarm 인터럽트의 지연을 잰 값이 아니다)')
    for text in outcome.problems:
        print('FAIL  ' + text)
    if outcome.stopped is not None:
        print('그만둠: ' + outcome.stopped)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_device_arguments(parser)
    parser.add_argument('--trials', type=int, default=1000, help='검증할 횟수. 시나리오에 2:1:1:1 로 나눈다 (기본 1000)')
    parser.add_argument('--seed', type=int, default=441)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials 는 1 이상이어야 한다')
    sys.stdout.reconfigure(errors='backslashreplace')
    random.seed(args.seed)
    device = open_device(args)
    peer = Peer(device)
    try:
        link = Link(peer)
        if link.calibrate():
            outcome = run(link, args.trials)
        else:
            outcome = Outcome(goals(args.trials))
            outcome.stopped = '준비 단계에서 CLOCK_INSTALL 에 답을 받지 못함'
    finally:
        device.close()

    report(outcome)
    lost = ', '.join(f'{name} {times}' for name, times in link.lost.items()) or '없음'
    print(f'답을 받지 못한 프레임: {lost}. 깨진 프레임 {peer.invalid}개')
    word, code = outcome.verdict()
    print('cutoff_stress: ' + word)
    return code


if __name__ == '__main__':
    sys.exit(main())
