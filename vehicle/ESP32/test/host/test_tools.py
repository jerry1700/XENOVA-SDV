"""PC 점검 도구(tools/)의 판정을 장치 없이 시험한다.

답이 끊기거나 검증한 것이 없는데도 통과로 끝나지 않는지, 표본 흐름이 어긋난 것을 알아채는지 본다.
장치와 Pi 는 가짜다.
"""
import os
import random
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'tools'))
import cutoff_stress
import link_bench
import pi_link_check
import steer_check
from cutoff_stress import FAR_FIRST, IN_TURN, ONE, TWINS
from fb3 import (ACK3, CLOCK3, CLOCK_ACK, CLOCK_ACK3, CLOCK_INSTALL, CLOCK_PROBE, CLOCK_REPLY, COMMAND, COMMAND3, HELLO,
                 HELLO3, HELLO_ACK, INSTALL3, PROBE3, REPORT, REPORT3, SENSOR, SENSOR3, TOF3, TOF_CRC_BYTES, TOF_SAMPLE)
from link_bench import SENSOR_PERIOD_US, Sample

checks = failures = 0


def check(ok, text):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
        print('FAIL  ' + text)


class FakeLink:
    """cutoff_stress.run 이 쓰는 Link 의 대역. 프레임을 주고받을 때마다 장치 시각이 흐른다.

    answers         CLOCK_PROBE 에 답하는 횟수. 그 뒤로는 답이 없다
    late_us         도구가 어림한 것보다 장치가 프레임을 늦게 처리하는 시간
    second_late_us  한 시도의 두 번째 CLOCK_INSTALL 이 그보다 더 늦게 처리되는 시간 (PC 가 멈칫한 경우)
    after_cutoff    기한 뒤 재전송에 돌려줄 (status, reason). None 이면 답이 없고, 'same' 이면 처음의 INSTALLED 가 그대로 온다
    stall_us        기한 뒤 재전송을 보내기 전에 PC 가 멈춰 있는 시간
    early_us        장치가 lease 를 기한보다 이만큼 일찍 만료시킨다 (고장)
    impatient_us    기한 뒤 재전송이 기한보다 이만큼 일찍 나간다. 그때 장치가 이미 만료로 답한다 (고장)
    echo_us         장치가 CLOCK_ACK 에 돌려주는 cutoff 가 보낸 것과 이만큼 다르다 (고장)
    misjudge_us     장치가 기한이 지났는지를 이만큼 틀리게 본다. 양수면 남은 기한을 거절하고 음수면 지난 기한을 받는다 (고장)
    """
    TRANSIT_US = 10000

    def __init__(self, answers=10 ** 9, late_us=0, second_late_us=0, after_cutoff=(3, 7), stall_us=0, early_us=0,
                 impatient_us=0, echo_us=0, misjudge_us=0):
        self.now = 5000000
        self.sequence = 0
        self.installs = 0  # 이번 시도에서 보낸 CLOCK_INSTALL 수
        self.answers = answers
        self.late_us = late_us
        self.second_late_us = second_late_us
        self.after_cutoff = after_cutoff
        self.stall_us = stall_us
        self.early_us = early_us
        self.impatient_us = impatient_us
        self.echo_us = echo_us
        self.misjudge_us = misjudge_us

    def _pass(self, extra_us=0):
        """프레임 하나를 보내고 답을 받는 동안 흐른 시간. 장치가 처리한 시각을 돌려준다"""
        processed = self.now + self.TRANSIT_US + self.late_us + extra_us
        self.now = processed + self.TRANSIT_US
        return processed

    def _ack(self, lease_sequence, status, reason, device_now, cutoff):
        return (b'pi', b'device', b'epoch', lease_sequence, 0, status, reason, device_now, cutoff + self.echo_us)

    def probe(self):
        self.installs = 0
        at = self._pass()
        if self.answers <= 0:
            return None
        self.answers -= 1
        return (0, 0, at, at)

    def after(self, margin_us):
        return int(self.now + self.TRANSIT_US + margin_us)

    def install(self, measured, cutoff):
        self.sequence += 1
        self.installs += 1
        at = self._pass(self.second_late_us if self.installs == 2 else 0)
        status, reason = (1, 0) if cutoff > at + self.misjudge_us else (3, 7)
        return cutoff_stress.Lease(self.sequence, b'', cutoff, self._ack(self.sequence, status, reason, at, cutoff))

    def ask(self, lease):
        at = self._pass()
        if lease.ack[5] == 1 and at < lease.cutoff - self.early_us:
            return lease.ack
        return self._ack(lease.sequence, 3, 7, at, lease.cutoff)

    def settle(self, lease):
        self.now = max(self.now, lease.cutoff - self.impatient_us + cutoff_stress.SETTLE_US) + self.stall_us
        at = self._pass()
        if self.after_cutoff is None:
            return None
        if self.after_cutoff == 'same':
            return lease.ack
        return self._ack(lease.sequence, self.after_cutoff[0], self.after_cutoff[1], at, lease.cutoff)


def test_stress_counts_what_it_verified():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(), 20)
    check(outcome.goal == {ONE: 8, FAR_FIRST: 4, IN_TURN: 4, TWINS: 4}, f'시험 20번의 목표 {outcome.goal}')
    check(outcome.verdict() == ('통과', 0), f'모두 제때 만료되면 통과 {outcome.verdict()}')
    check(dict(outcome.verified) == outcome.goal, f'시나리오마다 목표만큼 검증 {dict(outcome.verified)}')
    check(outcome.rejected > 0 and len(outcome.overdue) == outcome.rejected,
          f'이미 지난 기한은 따로 센다 ({outcome.rejected}번)')
    # 그 자리에서 거절된 것은 검증 횟수에 들어가지 않으므로, 시도는 검증보다 그만큼 많다
    check(outcome.attempts == sum(outcome.goal.values()) + outcome.rejected, f'시도 {outcome.attempts}번')
    check(all(margin > 0 for margin in outcome.margins) and all(late >= 0 for late in outcome.overdue),
          '남은 시간은 0 보다 크고 지난 시간은 0 이상')


def test_stress_is_incomplete_when_replies_stop():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(answers=0), 20)
    check(outcome.verdict() == ('미완료', 2), f'본 시험의 CLOCK_PROBE 에 답이 없으면 미완료 {outcome.verdict()}')
    check(not outcome.verified and outcome.attempts == cutoff_stress.SILENT_LIMIT,
          f'검증 0번, 잇달아 {cutoff_stress.SILENT_LIMIT}번 답이 없으면 그만둠 (시도 {outcome.attempts}번)')
    check(outcome.stopped is not None and not outcome.problems, f'그만둔 까닭을 남김 ({outcome.stopped})')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(answers=7), 20)
    check(outcome.verdict() == ('미완료', 2) and 0 < sum(outcome.verified.values()) < 20,
          f'도중에 답이 끊겨도 미완료 (검증 {sum(outcome.verified.values())}번)')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(after_cutoff=None), 20)
    check(outcome.verdict() == ('미완료', 2) and not outcome.verified,
          f'기한 뒤 재전송에 답이 없으면 만료를 확인하지 못했으므로 미완료 {outcome.verdict()}')


def test_stress_is_incomplete_when_nothing_reaches_the_timer():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(late_us=2000), 20)
    check(outcome.verdict() == ('미완료', 2), f'처리 순간에 붙인 기한이 모두 이미 지나 있으면 미완료 {outcome.verdict()}')
    check(outcome.verified[ONE] == 0 and outcome.verified[FAR_FIRST] == 0 and outcome.rejected > 0,
          f'그 자리에서 거절된 {outcome.rejected}번은 검증으로 세지 않음')
    check(outcome.verified[IN_TURN] == 4 and outcome.verified[TWINS] == 4, '여유 있는 기한 둘은 검증됨')
    check(outcome.stopped is not None and '시도' in outcome.stopped, f'시도 한도에서 그만둠 ({outcome.stopped})')


def test_stress_needs_both_deadlines_on_the_timer():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(second_late_us=200000), 20)
    check(outcome.verdict() == ('미완료', 2), f'둘째 기한이 늦어 걸리지 못하면 미완료 {outcome.verdict()}')
    check(outcome.verified[ONE] == 8 and not any(outcome.verified[kind] for kind in (FAR_FIRST, IN_TURN, TWINS)),
          f'기한 둘 가운데 하나만 걸린 시도는 검증으로 세지 않음 {dict(outcome.verified)}')


def test_stress_needs_to_see_the_later_deadline_alive():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(stall_us=200000), 20)
    check(outcome.verdict() == ('미완료', 2), f'뒤 기한이 살아 있는 것을 못 봤으면 미완료 {outcome.verdict()}')
    check(outcome.verified[FAR_FIRST] == 0 and outcome.verified[IN_TURN] == 0,
          f'PC 가 늦어 사이에 보지 못한 시도는 검증으로 세지 않음 {dict(outcome.verified)}')
    check(outcome.verified[ONE] == 8 and outcome.verified[TWINS] == 4, '기한 하나와 수십 us 차이인 둘은 그대로 검증됨')


def test_stress_fails_on_wrong_answer():
    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(after_cutoff=(3, 11)), 20)
    check(outcome.verdict() == ('실패', 1), f'기한 뒤 재전송에 DEVICE_FAULT 가 오면 실패 {outcome.verdict()}')
    check(len(outcome.problems) == 1 and 'DEVICE_FAULT' in outcome.problems[0], f'{outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(after_cutoff=(1, 0)), 20)
    check(outcome.verdict() == ('실패', 1), f'기한이 지나도 INSTALLED 면 실패 {outcome.verdict()}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(after_cutoff='same'), 20)
    check(outcome.verdict() == ('실패', 1) and 'status 1' in outcome.problems[0],
          f'기한이 지나도 처음의 INSTALLED 가 그대로 오면 실패 {outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(early_us=120000), 20)
    check(outcome.verdict() == ('실패', 1) and '남았는데' in outcome.problems[0],
          f'앞 기한이 끝난 뒤에 보니 뒤 기한이 기한 전에 만료돼 있으면 실패 {outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(impatient_us=20000), 20)
    check(outcome.verdict() == ('실패', 1) and '남았는데' in outcome.problems[0],
          f'cutoff 전의 device_now 로 CLOCK_EXPIRED 가 오면 실패 {outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(misjudge_us=-5000), 20)
    check(outcome.verdict() == ('실패', 1) and 'INSTALLED 인데' in outcome.problems[0],
          f'이미 지난 기한에 INSTALLED 로 답하면 실패 {outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(misjudge_us=5000), 20)
    check(outcome.verdict() == ('실패', 1) and '남았는데 CLOCK_EXPIRED' in outcome.problems[0],
          f'아직 남은 기한을 CLOCK_EXPIRED 로 거절하면 실패 {outcome.problems}')

    random.seed(1)
    outcome = cutoff_stress.run(FakeLink(echo_us=1), 20)
    check(outcome.verdict() == ('실패', 1) and '돌려줌' in outcome.problems[0],
          f'다른 cutoff 를 돌려주면 실패 {outcome.problems}')


def settled(answers):
    """통신 없이 Link.settle 만 돌려 본다. answers 는 재전송마다 돌려줄 답이고, 기한은 이미 지나 있다"""
    link = object.__new__(cutoff_stress.Link)
    replies = iter(answers)
    link.after = lambda margin_us: 10 ** 12
    link.ask = lambda lease: next(replies, None)
    return link.settle(cutoff_stress.Lease(1, b'', 1000, answers[0]))


def test_settle_tells_silence_from_a_stuck_lease():
    installed = (b'pi', b'device', b'epoch', 1, 0, 1, 0, 900, 1000)
    expired = (b'pi', b'device', b'epoch', 1, 0, 3, 7, 5000, 1000)
    check(settled([installed, None, installed, expired, installed]) == expired, '만료로 바뀐 답이 오면 그 답')
    check(settled([installed] * 8) == installed, '끝까지 INSTALLED 면 그 답을 돌려줘 실패로 판정된다')
    check(settled([installed] + [None] * 7) == installed, '한 번이라도 답이 왔으면 답이 없던 것으로 보지 않는다')
    check(settled([None] * 8) is None, '답이 한 번도 없으면 None')


class FakeClock:
    """기다린 만큼만 흐르는 시계."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSink:
    """pi_link_check.hold 가 보는 Pi 쪽 대역. lease 가 처음 valid_for 초 동안만 살아 있다."""

    def __init__(self, clock, valid_for):
        self.clock = clock
        self.until = clock() + valid_for
        self.polls = 0

    def poll(self, now):
        self.polls += 1

    def _lease(self, now_us):
        return {} if now_us / 1e6 < self.until else None


def held(valid_for, seconds):
    clock = FakeClock()
    sink = FakeSink(clock, valid_for)
    return pi_link_check.hold(sink, seconds, clock=clock, sleep=clock.sleep), sink


def test_pi_check_sees_silence():
    silence = pi_link_check.longest_silence
    check(abs(silence([10.2, 10.4, 10.6, 10.8], 10.0, 11.0) - 0.2) < 1e-9, '응답이 0.2초마다 오면 가장 긴 공백 0.2초')
    check(abs(silence([10.2, 10.4], 10.0, 40.0) - 29.6) < 1e-9, '초반 응답 뒤 끊기면 끝까지가 공백')
    check(silence([], 10.0, 40.0) == 30.0, '응답이 없으면 구간 전체가 공백')
    check(abs(silence([5.0, 10.3, 99.0], 10.0, 11.0) - 0.7) < 1e-9, '구간 밖의 응답은 세지 않음')
    check(silence([10.2, 10.4], 10.0, 40.0) > pi_link_check.MAX_SILENCE_S, '그 공백은 허용 시간을 넘음')

    dark, sink = held(valid_for=60.0, seconds=30.0)
    check(dark == 0 and sink.polls > 1000, 'lease 가 계속 이어지면 끊긴 시간 0')
    dark, _ = held(valid_for=1.0, seconds=30.0)
    check(abs(dark - 29.0) < 0.01, f'초반 lease 가 끝난 뒤 새 lease 가 없으면 그 뒤가 모두 끊긴 시간 ({dark:.2f}초)')
    dark, _ = held(valid_for=0.0, seconds=30.0)
    check(abs(dark - 30.0) < 0.01, f'lease 가 처음부터 없으면 구간 전체가 끊긴 시간 ({dark:.2f}초)')


PI, DEVICE, EPOCH = b'p' * 16, b'd' * 16, b'e' * 16


def sensor_fields(sample, stamp, lease=5, pi=PI, quality=1, steering=0, error=0, ticks=0):
    return (pi, DEVICE, sample, stamp, ticks, ticks, quality, steering, error, EPOCH, lease)


def lease_checked(sensors, start=1000000, cutoff=1500000, late=link_bench.BOARD_LATE_US):
    return link_bench.lease_problems(sensors, PI, DEVICE, EPOCH, 5, start, cutoff, late, 32)


def test_bench_sees_broken_sample_order():
    problems = link_bench.stream_problems
    period = SENSOR_PERIOD_US
    good = [Sample(CLOCK_REPLY, 1, 1000), Sample(SENSOR, 2, 1000 + period), Sample(SENSOR, 3, 1000 + 2 * period),
            Sample(CLOCK_REPLY, 4, 1000 + 3 * period), Sample(SENSOR, 5, 1000 + 4 * period)]
    check(problems(good) == [], f'주기와 간격을 지킨 흐름에는 문제 없음 {problems(good)}')

    def moved(index, **change):
        return good[:index] + [good[index]._replace(**change)] + good[index + 1:]

    check(len(problems(moved(2, sequence=4))) == 2, '순번을 건너뛰면 그 앞뒤가 모두 어긋남')
    check(len(problems(moved(1, sequence=1))) == 2, '같은 순번이 다시 오면 어긋남')
    check(len(problems(moved(2, device_us=good[1].device_us + period - 1))) == 1, 'SENSOR 가 한 주기보다 일찍 오면 어긋남')
    check(len(problems(moved(1, device_us=good[0].device_us + period - 1))) == 1,
          'CLOCK_REPLY 다음 SENSOR 도 한 주기보다 일찍 오면 어긋남')
    check(len(problems(moved(3, device_us=good[2].device_us + period - 1))) == 1,
          'CLOCK_REPLY 의 표본도 앞 표본에서 한 주기보다 일찍 오면 어긋남')
    wedged = [Sample(SENSOR, 1, 1000), Sample(CLOCK_REPLY, 2, 1000 + period // 2), Sample(SENSOR, 3, 1000 + period)]
    check(len(problems(wedged)) == 2, 'CLOCK_REPLY 의 표본이 한 주기 떨어진 두 SENSOR 사이에 끼면 앞뒤가 모두 어긋남')
    check(problems(moved(3, device_us=good[2].device_us - 1)) != [], '표본 시각이 거꾸로 가면 어긋남')
    check(problems(good[:1]) == [] and problems([]) == [], '표본이 하나 이하면 견줄 것이 없음')

    late = link_bench.BOARD_LATE_US
    check(problems(good, late) == [], '늦지 않은 흐름에는 늦음을 봐도 문제 없음')
    slow = moved(2, device_us=good[1].device_us + period + late + 1)
    check(problems(slow[:3]) == [] and len(problems(slow[:3], late)) == 1, 'SENSOR 가 한 주기보다 늦으면 lease 가 살아 있던 구간에서만 어긋남')
    slow = moved(1, device_us=good[0].device_us + period + late + 1)
    check(len(problems(slow[:2], late)) == 1, 'CLOCK_REPLY 다음 SENSOR 가 한 주기보다 늦어도 어긋남')
    check(problems(moved(2, device_us=good[1].device_us + period + late)[:3], late) == [], '허용한 만큼 늦은 것은 문제 없음')
    check(problems([good[2], good[3]._replace(device_us=good[2].device_us + 10 ** 6)], late) == [],
          'CLOCK_REPLY 는 probe 가 올 때 나오므로 늦음을 보지 않음')


def test_bench_needs_sensor_through_the_lease():
    steady = [sensor_fields(2 + i, 1010000 + 20000 * i) for i in range(25)]
    check(lease_checked(steady) == [], f'lease 가 살아 있는 동안 20 ms 마다 오면 문제 없음 {lease_checked(steady)}')
    check(lease_checked([]) == ['SENSOR 가 하나도 오지 않음'], 'SENSOR 가 없으면 문제')
    check(any('끊김' in text for text in lease_checked(steady[:5])), '초반에만 오고 끊기면 문제')
    check(any('끊김' in text for text in lease_checked(steady[5:])), '늦게 시작하면 문제')
    check(any('끊김' in text for text in lease_checked(steady[:8] + steady[10:])), '가운데 둘이 빠지면 문제')
    check(any('끊김' in text for text in lease_checked(steady[:8] + steady[9:])), '하나만 빠져도 문제')
    check(lease_checked(steady[:8] + steady[9:], late=link_bench.SIM_LATE_US) == [], '시뮬레이터에 허용한 늦음 안이면 문제 없음')
    check(any('구간 밖' in text for text in lease_checked(steady + [sensor_fields(27, 1500000)])), 'cutoff 와 같은 시각의 표본은 문제')
    check(any('구간 밖' in text for text in lease_checked([sensor_fields(1, 1000000)] + steady)), '설치 전 시각의 표본은 문제')
    other = steady[:3] + [sensor_fields(5, 1070000, lease=6)] + steady[4:]
    check(any('clock_sequence 6' in text for text in lease_checked(other)), '다른 lease 를 가리키면 문제')
    stranger = steady[:3] + [sensor_fields(5, 1070000, pi=b'x' * 16)] + steady[4:]
    check(any('session' in text for text in lease_checked(stranger)), '다른 session 이면 문제')
    for name, bad in (('엔코더 bit 없음', dict(quality=0)), ('모르는 quality bit', dict(quality=9)),
                      ('측정과 추정을 함께', dict(quality=7, error=3)), ('누계가 32비트 밖', dict(ticks=2 ** 31)),
                      ('조향 값만 있음', dict(steering=10)), ('측정 조향에 오차 없음', dict(quality=3)),
                      ('조향이 범위 밖', dict(quality=3, steering=1401, error=3))):
        broken = steady[:3] + [sensor_fields(5, 1070000, **bad)] + steady[4:]
        check(len(lease_checked(broken)) == 1, f'계약 밖 표본은 문제: {name}')
    measured = steady[:3] + [sensor_fields(5, 1070000, quality=3, steering=-1400, error=1)] + steady[4:]
    check(lease_checked(measured) == [], '오차가 있는 측정 조향은 문제 없음')


class FakePeer:
    """받을 프레임을 차례로 내주는 Peer 대역."""

    def __init__(self, frames):
        self.frames = list(frames)

    def receive(self, timeout=1.0):
        return self.frames.pop(0) if self.frames else None


def test_flow_sets_sensor_aside():
    reply = CLOCK3.pack(PI, DEVICE, 1, 2, 3, 4000, 5, 7, 0, 0, 1, 0, 0)
    ack = ACK3.pack(3, PI, DEVICE, 0xa0, bytes(32), bytes(32), 32)
    flow = link_bench.Flow(FakePeer([
        (SENSOR, SENSOR3.pack(*sensor_fields(6, 3000)), 10), (SENSOR, b'short', 11), (CLOCK_REPLY, reply, 12),
        (SENSOR, SENSOR3.pack(*sensor_fields(8, 14000)), 13), (HELLO_ACK, ack, 14)]))
    first = flow.answer(0.01)
    check(first is not None and first[0] == CLOCK_REPLY, '기다리던 응답 앞의 SENSOR 는 건너뜀')
    check(flow.samples == [Sample(SENSOR, 6, 3000), Sample(CLOCK_REPLY, 7, 4000)], f'표본은 받은 순서대로 적음 {flow.samples}')
    check(flow.odd == 1 and len(flow.sensors) == 1, '크기가 틀린 SENSOR 는 따로 셈')
    check(flow.listen(0.01) == 1 and len(flow.sensors) == 2 and len(flow.samples) == 3,
          '듣는 동안 온 SENSOR 는 적고 다른 프레임은 수로 돌려줌')
    check(flow.answer(0.01) is None, '더 오는 것이 없으면 None')


def report_fields(report_sequence, sample, stamp, command=1):
    return (PI, DEVICE, command, 1, 0, 2, 2, report_sequence, sample, stamp, 5, 0, 0, 1, bytes(16), 0, 0, 0, 0)


def test_bench_sees_report_out_of_step():
    problems = link_bench.report_problems
    one, two = Sample(SENSOR, 4, 1000), Sample(CLOCK_REPLY, 5, 9000)
    good = [(report_fields(1, 0, 0), None), (report_fields(2, 4, 1000), one), (report_fields(3, 5, 9000), two)]
    check(problems(good) == [], f'순번이 하나씩 늘고 바로 앞 표본을 가리키면 문제 없음 {problems(good)}')
    check(problems(good + [good[1]]) == [], '다시 보낸 REPORT 는 건너뜀')
    check(len(problems([good[0], good[2]])) == 1, 'report_sequence 를 건너뛰면 문제')
    check(len(problems([good[0], (report_fields(2, 3, 1000), one)])) == 1, '바로 앞 표본이 아닌 순번을 가리키면 문제')
    check(len(problems([good[0], (report_fields(2, 4, 999), one)])) == 1, '그 표본의 시각이 아니면 문제')
    check(len(problems([(report_fields(1, 4, 1000), None)])) == 1, '표본을 받기 전인데 표본을 가리키면 문제')
    check(len(problems([(report_fields(1, 0, 0), one)])) == 1, '표본을 받은 뒤인데 가리키지 않으면 문제')


def drive_report(sample, stamp, report_sequence=1, command=2):
    return report_fields(report_sequence, sample, stamp, command)


def test_bench_needs_drive_reports_to_follow_samples():
    problems = link_bench.drive_report_problems
    cutoff = 100000
    # 적용한 때의 표본 10, 그 뒤 20 ms 마다 11·12·13·14. 14 는 기한까지 5 ms 가 안 남았고 15 는 기한 뒤다
    samples = [Sample(SENSOR, 10, 20000), Sample(SENSOR, 11, 40000), Sample(SENSOR, 12, 60000), Sample(SENSOR, 13, 80000),
               Sample(SENSOR, 14, 96000), Sample(SENSOR, 15, 116000)]
    first = drive_report(10, 20000)
    told = [first, drive_report(11, 40000, 2), drive_report(12, 60000, 3), drive_report(13, 80000, 4)]
    check(problems(told, samples, first, cutoff) == ([], 3), f'기한 전의 표본마다 차례로 오면 문제 없음 {problems(told, samples, first, cutoff)}')
    check(problems(told[:3], samples, first, cutoff)[0] != [], '다음 표본도 기한 전인 표본의 REPORT 가 빠지면 문제')
    check(problems(told[:1], samples, first, cutoff)[0] != [], '후속 REPORT 가 하나도 없으면 문제')
    check(problems([first, told[2], told[1], told[3]], samples, first, cutoff)[0] != [], '표본 순서와 다르면 문제')
    check(problems(told + [drive_report(14, 96000, 5)], samples, first, cutoff)[0] != [], '기한까지 5 ms 가 안 남은 표본에 오면 문제')
    check(problems(told + [drive_report(15, 116000, 5)], samples, first, cutoff)[0] != [], '기한 뒤의 표본에 오면 문제')
    check(problems(told[1:], samples, first, cutoff)[0] != [], '적용한 때의 REPORT 가 맨 앞이 아니면 문제')
    check(problems([drive_report(10, 20000, 9)] + told[1:], samples, first, cutoff)[0] != [],
          '맨 앞의 REPORT 가 명령에 바로 답한 것과 다르면 문제')
    check(problems([], samples, None, cutoff)[0] != [], 'DRIVE 에 답이 없었으면 문제')
    # 기한 전의 표본이 13 하나뿐이면 그 REPORT 는 장치가 만들 때 이미 기한 뒤일 수 있다. 와도 되고 안 와도 된다
    late = [Sample(SENSOR, 12, 60000), Sample(SENSOR, 13, 80000), Sample(SENSOR, 14, 101000)]
    first = drive_report(12, 60000)
    check(problems([first], late, first, cutoff) == ([], 0) and problems([first, drive_report(13, 80000, 2)], late, first, cutoff) == ([], 0),
          '다음 표본이 기한 뒤인 마지막 표본의 REPORT 는 없어도 되고, 꼭 와야 하는 것은 0 으로 셈')


def test_flow_sets_report_aside():
    ack = ACK3.pack(3, PI, DEVICE, 0x1f7, bytes(32), bytes(32), 32)
    flow = link_bench.Flow(FakePeer([
        (REPORT, REPORT3.pack(*report_fields(1, 0, 0)), 9), (SENSOR, SENSOR3.pack(*sensor_fields(6, 3000)), 10),
        (REPORT, REPORT3.pack(*report_fields(2, 6, 3000)), 11), (REPORT, b'short', 12),
        (REPORT, REPORT3.pack(*report_fields(3, 6, 3000, command=7)), 13), (HELLO_ACK, ack, 14)]))
    found = flow.report(lambda fields: fields[2] == 7, 0.01)
    check(found is not None and found[7] == 3, '조건에 맞는 첫 REPORT 를 찾음')
    check([fields[7] for fields, _ in flow.reports] == [1, 2, 3], 'REPORT 는 받은 순서대로 적음')
    check(flow.reports[0][1] is None and flow.reports[1][1] == Sample(SENSOR, 6, 3000), '그 앞에 받은 표본을 함께 적음')
    check(flow.odd == 1, '크기가 틀린 REPORT 는 따로 셈')
    check(flow.report(lambda fields: fields[2] == 7, 0.01) is None, '이미 지나간 REPORT 는 다시 찾지 않음')
    flow = link_bench.Flow(FakePeer([(REPORT, REPORT3.pack(*report_fields(1, 0, 0)), 9), (HELLO_ACK, ack, 14)]))
    answer = flow.answer(0.01)
    check(answer is not None and answer[0] == HELLO_ACK and len(flow.reports) == 1, '응답을 기다릴 때 REPORT 는 건너뛰고 적어 둠')


def tof_payload(sensor=0, sample=1, stamp=1000, distance=600, status=1, quality=3, instance=b'i' * 16, generation=1,
                pi=PI, device=DEVICE, epoch=EPOCH, crc=None, rows=1, cols=1, frame_status=3, revision=1, error=17500,
                clock=5):
    body = TOF3.pack(pi, device, sample, stamp, error, epoch, clock, sensor, instance, generation, revision, b'c' * 32,
                     rows, cols, frame_status, distance, status, quality, 0)[:TOF_CRC_BYTES]
    return body + struct.pack('<I', zlib.crc32(body) if crc is None else crc)


def test_bench_checks_tof_samples():
    def problems(payloads):
        return link_bench.tof_problems(payloads, PI, DEVICE, EPOCH)

    first = tof_payload()
    good = [first, tof_payload(1, 1, 1500, 800, instance=b'j' * 16), tof_payload(0, 2, 2000),
            tof_payload(0, 3, 3000, distance=0, status=3, quality=2), tof_payload(1, 2, 3500, 801, instance=b'j' * 16)]
    check(problems(good) == [], f'규칙을 지킨 관측에는 문제 없음 {problems(good)}')
    check(problems([]) == [], '관측이 없으면 견줄 것이 없음')
    check(problems([tof_payload(crc=1)]) != [], 'sample_crc32 가 틀리면 문제')
    check(problems([tof_payload(pi=b'x' * 16)]) != [] and problems([tof_payload(device=b'x' * 16)]) != [],
          '다른 session 의 관측이면 문제')
    check(problems([tof_payload(epoch=b'x' * 16)]) != [] and problems([tof_payload(clock=0)]) != [],
          '지금 clock epoch 가 아니거나 lease 순번이 0 이면 문제')
    check(problems([first, tof_payload(0, 3, 2000)]) != [] and problems([first, tof_payload(0, 1, 2000)]) != [],
          '순번이 1 씩 늘지 않으면 문제')
    check(problems([first, tof_payload(0, 1, 2000, instance=b'k' * 16)]) != [], 'instance 만 바뀌면 문제')
    check(problems([first, tof_payload(0, 2, 2000, generation=2)]) != [], 'generation 만 바뀌면 문제')
    check(problems([first, tof_payload(0, 1, 2000, instance=b'k' * 16, generation=2)]) == [],
          '새 instance 와 더 큰 generation 이면 순번은 1 부터')
    check(problems([first, tof_payload(0, 2, 2000, instance=b'k' * 16, generation=2)]) != [],
          '새 instance 인데 순번이 1 이 아니면 문제')
    check(problems([first, tof_payload(0, 2, 1000)]) != [], '관측 시각이 늘지 않으면 문제')
    check(problems([tof_payload(distance=0)]) != [] and problems([tof_payload(distance=65535)]) != [],
          '유효한 관측의 거리가 0 이나 65535 면 문제')
    check(problems([tof_payload(distance=0, status=3, quality=3)]) != [], '유효하지 않은데 TARGET_VALID 가 켜져 있으면 문제')
    check(problems([tof_payload(distance=500, status=3, quality=2)]) != [], '유효하지 않은데 거리가 실려 있으면 문제')
    check(problems([tof_payload(quality=2)]) != [], '유효한데 TARGET_VALID 가 꺼져 있으면 문제')
    for change in (dict(rows=8), dict(cols=0), dict(sensor=2), dict(instance=bytes(16)), dict(generation=0),
                   dict(revision=0), dict(frame_status=8), dict(status=8, quality=2, distance=0), dict(quality=0x23),
                   dict(error=0), dict(stamp=0), dict(sample=0)):
        check(problems([tof_payload(**change)]) != [], f'계약 밖 칸이면 문제 {change}')

    ack = ACK3.pack(3, PI, DEVICE, 0x0df7, bytes(32), bytes(32), 32)
    flow = link_bench.Flow(FakePeer([(TOF_SAMPLE, first, 9), (TOF_SAMPLE, first[:-1], 10), (HELLO_ACK, ack, 11)]))
    answer = flow.answer(0.01)
    check(answer is not None and answer[0] == HELLO_ACK, '응답을 기다릴 때 TOF_SAMPLE 은 건너뜀')
    check(flow.tofs == [first] and flow.odd == 1, 'TOF_SAMPLE 은 따로 적고 크기가 틀린 것은 따로 셈')


class QuietReport(link_bench.Report):
    """도구의 판정만 세고 화면에는 쓰지 않는다."""

    def check(self, ok, text):
        self.checks += 1
        self.failed += not ok
        return bool(ok)

    def note(self, text):
        pass


class SteerDevice:
    """steer_check 가 말을 거는 장치의 대역. Peer 자리에 들어간다.

    drive    DRIVE·HOLD_STEER 에 돌려줄 (status, reason). None 이면 답이 없다
    sensors  보내는 SENSOR 수의 한도. lease 를 설치할 때와 DRIVE 를 받을 때 하나씩 보낸다. None 이면 한도가 없다
    after    DRIVE 에 답한 뒤 조용할 때 이어서 보내는 SENSOR 수
    creep    DRIVE 를 받을 때마다 엔코더 누계가 이만큼 늘어난다 (바퀴가 돈 경우)
    wobble   DRIVE 를 받으면 누계가 한 칸 늘었다가 제자리로 돌아온다
    drift    DRIVE 에 답한 뒤 이어서 보내는 SENSOR 에서만 누계가 한 칸 늘어 있다
    jump     CLOCK_REPLY 에 실린 누계만 한 칸 늘어 있다
    caps     HELLO_ACK 로 알리는 capabilities
    echo     REPORT 에 돌려주는 command_kind. None 이면 받은 그대로다
    """

    def __init__(self, drive=(2, 0), sensors=None, after=2, creep=0, wobble=False, drift=False, jump=False,
                 caps=0x05f7, echo=None):
        self.drive, self.sensors, self.after, self.creep, self.wobble = drive, sensors, after, creep, wobble
        self.drift, self.jump, self.caps, self.echo = drift, jump, caps, echo
        self.kinds = []  # 받은 DRIVE·HOLD_STEER 의 command_kind
        self.pending = 0  # 조용할 때 이어서 보낼 SENSOR 수
        self.pi = None
        self.frames = []
        self.steered = []  # 받은 DRIVE 의 (속도, 조향)
        self.invalid = 0
        self.ticks = self.sample = self.reports = 0
        self.epoch, self.lease = bytes(16), 0

    def _reply(self, kind, payload):
        self.frames.append((kind, payload, 0))

    def send(self, kind, payload):
        if kind == HELLO:
            pi = HELLO3.unpack(payload)[1]
            self._reply(HELLO_ACK, ACK3.pack(3, pi, DEVICE, self.caps, b'c' * 32, b'f' * 32, 32))
        elif kind == CLOCK_PROBE:
            pi, device, sequence, p1 = PROBE3.unpack(payload)
            self.sample += 1
            self._reply(CLOCK_REPLY, CLOCK3.pack(pi, device, sequence, p1, 1000, 1001, 1002, self.sample,
                                                 self.ticks + self.jump, self.ticks, 1, 0, 0))
        elif kind == CLOCK_INSTALL:
            pi, device, self.epoch, self.lease, probe = INSTALL3.unpack(payload)[:5]
            cutoff = INSTALL3.unpack(payload)[9]
            self._reply(CLOCK_ACK, CLOCK_ACK3.pack(pi, device, self.epoch, self.lease, probe, 1, 0, 2000, cutoff))
            self._sensor(pi)
        elif kind == COMMAND:
            fields = COMMAND3.unpack(payload)
            status, reason = (2, 0)
            if fields[10] != steer_check.STOP:
                self.steered.append(fields[12:14])
                self.kinds.append(fields[10])
                self.pi, self.pending = fields[0], self.after
                self.ticks += self.creep
                if self.wobble:
                    self.ticks += 1
                    self._sensor(fields[0])
                    self.ticks -= 1
                self._sensor(fields[0])
                if self.drive is None:
                    return
                status, reason = self.drive
            self.reports += 1
            self._reply(REPORT, REPORT3.pack(fields[0], fields[1], fields[2], fields[8], fields[9],
                                             fields[10] if self.echo is None else self.echo, status,
                                             self.reports, self.sample, 3000, 0, 0, 0, 0, fields[3], fields[4],
                                             fields[7], fields[11], reason))

    def _sensor(self, pi):
        if self.sensors is not None:
            if self.sensors == 0:
                return
            self.sensors -= 1
        self.sample += 1
        self._reply(SENSOR, SENSOR3.pack(pi, DEVICE, self.sample, 3000 + self.sample, self.ticks, self.ticks, 1, 0, 0,
                                         self.epoch, self.lease))

    def receive(self, timeout=1.0):
        if not self.frames and self.pending:
            self.pending -= 1
            self.ticks += self.drift
            self._sensor(self.pi)
            self.ticks -= self.drift
        return self.frames.pop(0) if self.frames else None


def steered(device, steps=(0, 200, -200), kind=steer_check.DRIVE):
    report = QuietReport()
    steer_check.run(link_bench.Flow(device), steps, 0.01, report, kind)
    return report


def test_steer_check_counts_only_applied_steering():
    device = SteerDevice()
    report = steered(device)
    check(report.failed == 0 and report.checks == 18, f'모두 APPLIED 면 통과 ({report.checks} 검사, 실패 {report.failed})')
    check(device.steered == [(0, 0), (0, 200), (0, -200)], f'속도는 0 이고 조향만 차례로 보냄 {device.steered}')
    check(device.kinds == [steer_check.DRIVE] * 3, f'따로 고르지 않으면 DRIVE 로 보냄 {device.kinds}')
    device = SteerDevice()
    report = steered(device, kind=steer_check.HOLD_STEER)
    check(report.failed == 0 and report.checks == 19 and device.kinds == [steer_check.HOLD_STEER] * 3
          and device.steered == [(0, 0), (0, 200), (0, -200)],
          f'--stationary 면 HOLD_STEER 로 보냄 ({report.checks} 검사, 실패 {report.failed}, {device.kinds})')
    device = SteerDevice(caps=0x01f7)
    report = steered(device, kind=steer_check.HOLD_STEER)
    check(report.failed == 1 and device.kinds == [], '정지 조향을 알리지 않는 장치에는 HOLD_STEER 를 보내지 않고 실패')
    check(steered(SteerDevice(caps=0x01f7)).failed == 0, 'DRIVE 로 보낼 때는 정지 조향을 알리지 않아도 됨')
    check(steered(SteerDevice(drive=(3, 16)), kind=steer_check.HOLD_STEER).failed == 3, 'HOLD_STEER 가 거절되면 그 자리마다 실패')
    check(steered(SteerDevice(echo=steer_check.DRIVE), kind=steer_check.HOLD_STEER).failed == 3,
          'REPORT 가 다른 종류의 명령을 돌려주면 실패')
    check(steered(SteerDevice(creep=1), kind=steer_check.HOLD_STEER).failed == 3, 'HOLD_STEER 에 엔코더 누계가 바뀌면 실패')
    check(steered(SteerDevice(drive=(3, 9))).failed == 3, 'DRIVE 가 거절되면 그 자리마다 실패')
    check(steered(SteerDevice(drive=None)).failed >= 3, 'DRIVE 에 답이 없으면 그 자리마다 실패')
    check(steered(SteerDevice(creep=1)).failed == 3, '엔코더 누계가 바뀌면 그 자리마다 실패')
    check(steered(SteerDevice(sensors=0)).failed == 1, 'SENSOR 가 처음부터 없어 누계를 보지 못했으면 실패')
    check(steered(SteerDevice(sensors=1, creep=1)).failed == 3,
          '첫 SENSOR 뒤로 표본이 끊기면 옛 누계로 넘어가지 않고 그 자리마다 실패')
    check(steered(SteerDevice(wobble=True)).failed == 3, '누계가 한 칸 움직였다가 제자리로 돌아와도 실패')
    report = steered(SteerDevice(sensors=5))
    check(report.failed == 2, f'도중에 표본이 끊기면 그 뒤 자리는 실패 (실패 {report.failed})')
    check(steered(SteerDevice(after=0)).failed == 3, 'DRIVE 에 답한 뒤로 표본이 없으면 앞서 받은 표본으로 넘어가지 않고 실패')
    check(steered(SteerDevice(drift=True)).failed == 3, 'DRIVE 에 답한 뒤의 표본에서 누계가 바뀌어 있으면 실패')
    check(steered(SteerDevice(jump=True)).failed == 3, 'CLOCK_REPLY 에 실린 누계만 바뀌어 있어도 실패')
    check(steer_check.side(0) == '가운데' and '왼쪽' in steer_check.side(1) and '오른쪽' in steer_check.side(-1),
          '조향 값은 왼쪽이 +')


def main():
    sys.stdout.reconfigure(errors='backslashreplace')  # 한글을 못 쓰는 콘솔에서도 멈추지 않게
    test_stress_counts_what_it_verified()
    test_stress_is_incomplete_when_replies_stop()
    test_stress_is_incomplete_when_nothing_reaches_the_timer()
    test_stress_needs_both_deadlines_on_the_timer()
    test_stress_needs_to_see_the_later_deadline_alive()
    test_stress_fails_on_wrong_answer()
    test_settle_tells_silence_from_a_stuck_lease()
    test_pi_check_sees_silence()
    test_bench_sees_broken_sample_order()
    test_bench_needs_sensor_through_the_lease()
    test_flow_sets_sensor_aside()
    test_bench_sees_report_out_of_step()
    test_bench_needs_drive_reports_to_follow_samples()
    test_flow_sets_report_aside()
    test_bench_checks_tof_samples()
    test_steer_check_counts_only_applied_steering()
    print(f'tools: {checks} checks, {failures} failures')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
