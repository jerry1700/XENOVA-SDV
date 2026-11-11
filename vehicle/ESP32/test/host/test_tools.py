"""PC 점검 도구(tools/)의 판정을 장치 없이 시험한다.

답이 끊기거나 검증한 것이 없는데도 통과로 끝나지 않는지 본다. 장치와 Pi 는 가짜다.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'tools'))
import cutoff_stress
import pi_link_check
from cutoff_stress import FAR_FIRST, IN_TURN, ONE, TWINS

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
    print(f'tools: {checks} checks, {failures} failures')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
