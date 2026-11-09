"""연결된 개발 stream 한 개의 PC/J 관측 교환이다. listen/reconnect/thread는 없다.

caller가 만든 nonblocking endpoint를 소유하고 종료 시 실제 close한다. next_frame은
외부 active1+latest1 producer의 take만, on_frame은 queue offer만 수행해야 한다.
이 loop의 보유량은 Sender active1 + 미송신 CLOCK1 + FRAME1 + Receiver partial1이다.
완료 frame은 callback 동안에만 추가 참조하고 다음 partial 수신 전에 놓는다.
clock/관측/반환 통계는 SDK, DB, Scene, World, READY 또는 실제 전송 승인이 아니다.
"""

from dataclasses import dataclass
from hashlib import sha256
import math
import select
import socket
import time
from uuid import uuid4

from .clock import ClockSource, ClockPolicy, ClockProbe, ClockReply, HilObservationClock, HilClockError, MAX_PROBE_US
from .mapped import MappedPcOwner, PcMappingError
from .transport import (SessionScope, ObservationFrame, ClockPacket, PacketSender, PacketReceiver,
    TransportError, TransportEOF, TransportTimeout)


IO_TIMEOUT_US = 200_000
U63_MAX = (1 << 63) - 1
_COUNTERS = ('probes_sent', 'replies_sent', 'replies_accepted', 'leases_issued', 'clock_rejected',
    'frames_received', 'frames_sent', 'frames_mapped', 'frames_offered', 'frames_dropped')
_TRANSIENT_CLOCK = frozenset(('CLOCK_RTT', 'CLOCK_OFFSET_WIDTH'))


def monotonic_us():
    """최초 local tick이다. 서로 다른 호스트의 수치를 여기서 같은 시계로 보지 않는다."""
    return time.monotonic_ns() // 1000


class ObservationSessionError(ValueError):
    """원 session 조건이 깨졌다. 재동기화나 원 E 재발급 없이 연결을 닫는다."""


def _need(value, reason):
    if not value: raise ObservationSessionError(reason)


@dataclass(frozen=True, slots=True)
class SessionResult:
    """종료된 local 실행의 유한 사실이다. lease_evidence도 현재 사용권이 아니다.

    on_frame이 보관한 작업은 최종 채택 때 원 session owner를 통해 최신 current_view를
    다시 받아야 한다. 종료 fence 뒤 과거 PcCurrentView로 publish해서는 안 된다.
    """
    role: str
    reason: str
    started_local_us: int
    finished_local_us: int
    counters: tuple
    lease_evidence: object
    last_frame_sequence: int | None
    last_frame_proof_sha256: str | None
    failure: str | None

    @property
    def counts(self): return dict(self.counters)


class _Session:
    def __init__(self, endpoint, scope, duration_s, clock, cancelled, *, role,
                 binding=None, clock_policy=None, destination_clock_id=None,
                 probe_interval_us=None, on_frame=None, next_frame=None, wait_for_probe=False):
        _need(isinstance(endpoint, socket.socket) and not endpoint.getblocking(), 'NONBLOCKING_ENDPOINT_REQUIRED')
        _need(type(scope) is SessionScope and callable(clock) and callable(cancelled), 'ORIGINAL_SCOPE_CLOCK_CANCEL_REQUIRED')
        _need(type(duration_s) in (int, float) and (type(duration_s) is int or math.isfinite(duration_s)) and duration_s > 0,
            'FINITE_POSITIVE_DURATION_REQUIRED')
        self.endpoint, self.scope, self.clock, self.cancelled, self.role = endpoint, scope, clock, cancelled, role
        self.last_now = None
        self.started = self.now()
        _need(duration_s < (U63_MAX - self.started) / 1_000_000, 'BOUNDED_DURATION_REQUIRED')
        duration = int(duration_s * 1_000_000)
        _need(1 < duration < U63_MAX - self.started, 'BOUNDED_DURATION_REQUIRED')
        self.expires = self.started + duration
        self.on_frame, self.next_frame = on_frame, next_frame
        _need(type(wait_for_probe) is bool and (role == 'PC' or not wait_for_probe),
            'EXPLICIT_PC_STARTUP_WAIT_FLAG_REQUIRED')
        self.peer_started = not wait_for_probe
        self.clock_owner = self.pc_owner = None
        if role == 'JETSON':
            _need(type(clock_policy) is ClockPolicy and callable(on_frame)
                and type(probe_interval_us) is int and 0 < probe_interval_us < clock_policy.lease_us,
                'EXPLICIT_CLOCK_POLICY_INTERVAL_AND_QUEUE_CALLBACK_REQUIRED')
            source = scope.fields
            self.clock_owner = HilObservationClock(ClockSource(source['source_id'], source['pc_clock_epoch'], source['session_id']), clock_policy)
            self.pc_owner = MappedPcOwner(binding, scope, self.clock_owner, destination_clock_id=destination_clock_id)
        else:
            _need(callable(next_frame), 'LATEST_FRAME_TAKE_REQUIRED')
        self.interval = probe_interval_us
        self.sender = PacketSender(endpoint, scope)
        self.receiver = PacketReceiver(endpoint, scope, completed_clock=self.now)
        self.waiting = self.control = self.frame = None
        self.send_end = None
        self.next_probe = self.started
        self.sequence = 0
        self.counts = dict.fromkeys(_COUNTERS, 0)
        self.last_frame_sequence = self.last_frame_sha = None

    def now(self):
        value = self.clock()
        _need(type(value) is int and 0 <= value < U63_MAX - 1
            and (self.last_now is None or value >= self.last_now), 'LOCAL_MONOTONIC_CLOCK_REGRESSED_OR_INVALID')
        self.last_now = value
        return value

    def count(self, name): self.counts[name] = min(U63_MAX, self.counts[name] + 1)

    def stopped(self):
        value = self.cancelled()
        _need(type(value) is bool, 'CANCELLED_MUST_RETURN_BOOL')
        return value

    def received(self, item):
        packet = item.packet
        if self.role == 'PC':
            _need(type(packet) is ClockPacket and packet.kind == 'CLOCK_PROBE', 'PC_REQUIRES_ORIGINAL_CLOCK_PROBE')
            _need(self.control is None, 'OUTSTANDING_CLOCK_CONTROL_EXCEEDS_ONE')
            self.peer_started = True
            # P2는 transport의 완전수신 tick이다. P3는 실제 송신 준비까지 읽지 않는다.
            self.control = (ClockProbe.from_raw(packet.clock_raw_bytes), item.received_local_us,
                min(item.received_local_us + IO_TIMEOUT_US, self.expires))
            return
        if type(packet) is ClockPacket:
            _need(packet.kind == 'CLOCK_REPLY' and self.waiting is not None, 'JETSON_REQUIRES_MATCHED_CLOCK_REPLY')
            value = ClockReply.from_raw(packet.clock_raw_bytes)
            before = self.clock_owner.fence_revision
            original = self.waiting
            try:
                lease = self.clock_owner.accept(value, item.received_local_us)
            except HilClockError as error:
                # 원 owner가 명시한 일시적 품질 실패만 기존 lease를 보존한다.
                if (self.clock_owner.fence_revision != before or str(error) not in _TRANSIENT_CLOCK
                        or self.clock_owner.reason != str(error)):
                    raise
                self.count('clock_rejected')
            else:
                self.count('replies_accepted')
                if lease is not None: self.count('leases_issued')
            self.waiting = None
            self.next_probe = max(original.j1_us + self.interval, item.received_local_us)
            return
        _need(type(packet) is ObservationFrame, 'ORIGINAL_FRAME_REQUIRED')
        self.count('frames_received')
        try:
            frame = self.pc_owner.accept_frame(item, now_j_us=self.now())
        except PcMappingError:
            # 원 frame만 drop한다. 아직 두 clock 표본이 없거나 원 E가 지난 자료를 재시각화하지 않는다.
            self.count('frames_dropped')
            return
        self.count('frames_mapped')
        self.last_frame_sequence = frame.observation.rgb.stamp.sequence
        self.last_frame_sha = sha256(frame.proof_bytes).hexdigest()
        offered = self.on_frame(frame, self.pc_owner)
        _need(type(offered) is bool, 'ON_FRAME_MUST_RETURN_QUEUE_OFFER_BOOL')
        self.count('frames_offered' if offered else 'frames_dropped')
        _need(self.now() + 1 < frame.expires_j_us, 'QUEUE_CALLBACK_OVERRAN_ORIGINAL_FRAME')

    def schedule(self, now):
        if self.send_end is not None and now >= self.send_end:
            raise TransportTimeout('original packet send operation exceeded its fixed local deadline')
        idle = self.sender.active is None and self.sender.pending is None
        if self.role == 'JETSON':
            if self.waiting is not None:
                _need(now + 1 < self.waiting.expires_j_us, 'ORIGINAL_CLOCK_REPLY_TIMEOUT')
            elif idle and now >= self.next_probe:
                j1 = self.now()
                if j1 + 1 >= self.expires: return
                self.sequence += 1
                probe = self.clock_owner.probe(self.sequence, uuid4().bytes, j1,
                    min(j1 + MAX_PROBE_US, self.expires))
                self.waiting = probe
                self.sender.offer(ClockPacket(self.scope, 'CLOCK_PROBE', probe.raw_bytes),
                    expires_us=probe.expires_j_us, now_us=j1)
                self.send_end = probe.expires_j_us
            return
        if self.control is not None:
            probe, p2, local_end = self.control
            _need(now < local_end, 'ORIGINAL_CLOCK_REPLY_SEND_TIMEOUT')
            if idle:
                p3 = self.now()  # 최초 송신 준비에서 한 번 고정한다. partial/reoffer 때 갱신하지 않는다.
                _need(p3 < local_end, 'ORIGINAL_CLOCK_REPLY_SEND_TIMEOUT')
                value = ClockReply(probe, p2, p3)
                self.sender.offer(ClockPacket(self.scope, 'CLOCK_REPLY', value.raw_bytes),
                    expires_us=local_end, now_us=p3)
                self.send_end = local_end
                self.control = None
                idle = False
        if not self.peer_started: return
        if self.frame is None:
            packet = self.next_frame()
            _need(packet is None or type(packet) is ObservationFrame and packet.scope.raw_bytes == self.scope.raw_bytes,
                'NEXT_FRAME_REQUIRES_ORIGINAL_SCOPE_OBSERVATION')
            self.frame = packet
            now = self.now()
        if self.frame is not None and now >= self.frame.expires_pc_us:
            self.frame = None
            self.count('frames_dropped')
        if idle and self.control is None and self.frame is not None:
            packet, self.frame = self.frame, None
            # FRAME의 원 PC E를 session E나 현재 시각으로 다시 포장하지 않는다.
            if not self.sender.offer(packet, expires_us=packet.expires_pc_us, now_us=now):
                self.count('frames_dropped')
            else:
                # 원 FRAME E는 그대로 두고 local transfer 시작보다 이른 offer에서 200ms를 고정한다.
                self.send_end = min(packet.expires_pc_us, now + IO_TIMEOUT_US)

    def step(self, *, wait_cap_us=None):
        """원 session에서 유한 I/O 한 번을 진행한다. cap은 원 deadline을 줄이기만 한다."""
        if wait_cap_us is not None:
            _need(type(wait_cap_us) is int and 0 <= wait_cap_us <= IO_TIMEOUT_US,
                'WAIT_CAP_MUST_NOT_EXTEND_ORIGINAL_IO_BOUND')
        now = self.now()
        if now >= self.expires: return 'DURATION'
        if self.stopped(): return 'CANCELLED'
        self.schedule(now)
        deadlines = [self.expires, now + IO_TIMEOUT_US]
        if wait_cap_us is not None: deadlines.append(now + wait_cap_us)
        if self.receiver.deadline_us is not None: deadlines.append(self.receiver.deadline_us)
        if self.waiting is not None: deadlines.append(self.waiting.expires_j_us)
        elif self.role == 'JETSON': deadlines.append(self.next_probe)
        if self.control is not None: deadlines.append(self.control[2])
        if self.send_end is not None: deadlines.append(self.send_end)
        writing = self.sender.active is not None or self.sender.pending is not None
        try:
            readable, _, _ = select.select([self.endpoint], [self.endpoint] if writing else [], [],
                max(0, min(deadlines) - self.now()) / 1_000_000)
        except InterruptedError:
            return None
        now = self.now()
        if now >= self.expires: return 'DURATION'
        # 대기 중 도착한 종료 요청을 peer close에 따른 다음 수신보다 먼저 반영한다.
        if self.stopped(): return 'CANCELLED'
        if readable:
            if self.receiver.deadline_us is None:
                self.receiver.begin(expires_us=min(now + IO_TIMEOUT_US, self.expires), now_us=now)
            item = self.receiver.step(now)
            if item is not None: self.received(item)
        elif self.receiver.deadline_us is not None and now >= self.receiver.deadline_us:
            self.receiver.step(now)  # 원 partial E를 갱신하지 않고 실제 timeout으로 닫는다.
        if self.stopped(): return 'CANCELLED'
        self.schedule(self.now())
        if self.sender.active is not None or self.sender.pending is not None:
            sent = self.sender.step(self.now())
            if sent is not None:
                self.send_end = None
                self.count({'FRAME': 'frames_sent', 'CLOCK_PROBE': 'probes_sent', 'CLOCK_REPLY': 'replies_sent'}[sent.kind])
        return None

    def complete(self, reason, failure):
        """원 owner를 fence하고 작은 종료 사실만 남긴다. endpoint close는 caller가 수행한다."""
        evidence = None if self.clock_owner is None else self.clock_owner.lease_evidence
        if self.pc_owner is not None: self.pc_owner.fence()
        self.control = self.frame = self.waiting = None
        self.sender.close()
        self.receiver.close()
        return SessionResult(self.role, reason, self.started, self.last_now, tuple(self.counts.items()),
            evidence, self.last_frame_sequence, self.last_frame_sha, failure)

    def run(self):
        reason, failure = 'DURATION', None
        try:
            while True:
                terminal = self.step()
                if terminal is not None:
                    reason = terminal
                    break
        except TransportEOF:
            reason = 'EOF'
        except (TransportError, HilClockError, ObservationSessionError, OSError) as error:
            reason, failure = type(error).__name__, str(error)[:240]
        except Exception as error:
            reason, failure = 'CALLBACK_ERROR', type(error).__name__
        finally:
            result = self.complete(reason, failure)
        return result


class JetsonObservationPump:
    """실제 A main에 끼우는 한 연결의 수신기다. thread/listen/reconnect를 만들지 않는다.

    step의 기본 wait cap은 0이며 원 partial·probe·frame E를 늘리지 않는다. caller는
    매 iteration에서 lifecycle/NN/slot을 함께 처리한다. 수신 callback과 current_view는
    같은 main thread에서만 호출한다. 종료는 source fence이며 model close와 별개다.
    """

    def __init__(self, endpoint, *, binding, scope, clock_policy, destination_clock_id,
                 duration_s, probe_interval_us, on_frame, cancelled, clock=monotonic_us):
        self._endpoint = endpoint
        self._session = None
        self._result = None
        try:
            self._session = _Session(endpoint, scope, duration_s, clock, cancelled, role='JETSON',
                binding=binding, clock_policy=clock_policy, destination_clock_id=destination_clock_id,
                probe_interval_us=probe_interval_us, on_frame=on_frame)
        except BaseException:
            if isinstance(endpoint, socket.socket): endpoint.close()
            raise

    @property
    def pc_owner(self): return self._session.pc_owner

    @property
    def result(self): return self._result

    def _finish(self, reason, failure=None):
        if self._result is None:
            try:
                self._result = self._session.complete(reason, failure)
            finally:
                self._endpoint.close()
        return self._result

    def step(self, *, wait_cap_us=0):
        """한 번 진행하고 종료 때만 SessionResult를 반환한다. 종료 뒤 재사용은 하지 않는다."""
        if self._result is not None: return self._result
        try:
            terminal = self._session.step(wait_cap_us=wait_cap_us)
        except TransportEOF:
            return self._finish('EOF')
        except (TransportError, HilClockError, ObservationSessionError, OSError) as error:
            return self._finish(type(error).__name__, str(error)[:240])
        except BaseException as error:
            self._finish('CALLBACK_ERROR', type(error).__name__)
            raise
        return None if terminal is None else self._finish(terminal)

    def close(self):
        """실제 수신 owner를 fence하고 endpoint를 닫는다. 반복 호출은 원 결과를 보존한다."""
        return self._finish('CANCELLED')


def _run(endpoint, scope, duration_s, clock, cancelled, **kwargs):
    try:
        return _Session(endpoint, scope, duration_s, clock, cancelled, **kwargs).run()
    finally:
        if isinstance(endpoint, socket.socket): endpoint.close()


def run_jetson_session(endpoint, *, binding, scope, clock_policy, destination_clock_id, duration_s,
                       probe_interval_us, on_frame, cancelled, clock=monotonic_us):
    """Jetson owner의 원 J1/J4/두 표본과 매핑 frame을 queue callback에 전달한다.

    on_frame(frame, pc_owner)는 exact bool을 반환하는 비차단 queue offer여야 한다.
    owner_token 초기 등록과 current_view query는 같은 session owner에서만 수행한다.
    async 계산의 최종 사용은 caller가 그 owner에서 재조회한 최신 view/실패 None으로 검산한다.
    endpoint 연결/인증/SSH forward는 caller 책임이다. 이 함수는 source 등록을 만들지 않는다.
    """
    return _run(endpoint, scope, duration_s, clock, cancelled, role='JETSON', binding=binding,
        clock_policy=clock_policy, destination_clock_id=destination_clock_id,
        probe_interval_us=probe_interval_us, on_frame=on_frame)


def run_pc_session(endpoint, *, scope, duration_s, next_frame, cancelled, clock=monotonic_us,
                   wait_for_probe=False):
    """완전수신 P2/최초 송신 준비 P3만 읽으며 원 FRAME의 bytes/E를 그대로 보낸다.

    next_frame()은 외부 producer의 latest1 take다. 이 loop에서 render/SDK/I/O를 실행하지
    않아야 한다. 이 함수는 render thread를 생성하거나 next_frame의 blocking을 숨기지 않는다.
    상주 기동 caller는 wait_for_probe=True로 실제 A pump의 첫 원 probe까지 frame take를
    미룬다. OAK coldboot 동안 미리 보낸 영상이200ms 전송 E를 소모하는 것을 막는다.
    원 실행 duration/Frame E는 그대로며 probe를 수락했다고 NN/Source READY가 되지 않는다.
    """
    return _run(endpoint, scope, duration_s, clock, cancelled, role='PC', next_frame=next_frame,
        wait_for_probe=wait_for_probe)
