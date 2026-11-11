"""실제 socketpair와 caller 소유 thread의 개발 session software 회귀다.

관측 bytes/PC capture 구간은 명시 software fixture다. 실제 renderer, PC 원격망,
NN, Gateway, D, Scene, World 또는 READY를 수락하는 시험이 아니다. library는
thread를 만들지 않으며 아래 thread의 stop/join은 시험 caller가 직접 수행한다.
"""

from dataclasses import replace
import gc
import math
import select
import socket
import threading
import unittest
from unittest.mock import patch

from tools.hil_observation.clock import ClockSource, ClockPolicy, ClockProbe, ClockReply
from tools.hil_observation.session import (run_jetson_session, run_pc_session, monotonic_us,
    ObservationSessionError, IO_TIMEOUT_US, JetsonObservationPump, _Session)
from tools.hil_observation.transport import (SessionScope, ClockPacket, ObservationFrame,
    PacketSender, PacketReceiver, TransportTimeout, MAX_IO, PREFIX, KINDS)
from xenova_vehicle.simulation.pc_input import PcSourceBinding


RGB = b'\x10' * (640 * 360 * 3 // 2)
DEPTH = b'\xe8\x03' * (640 * 480)


def fixtures():
    binding = PcSourceBinding(dict(profile='development-pc-source-1',
        service_instance='11111111-1111-4111-8111-111111111111',
        source_id='22222222-2222-4222-8222-222222222222',
        pc_clock_epoch='33333333-3333-4333-8333-333333333333',
        jetson_session='44444444-4444-4444-8444-444444444444',
        stream_instances=dict(rgb_video='55555555-5555-4555-8555-555555555555',
            depth_right='66666666-6666-4666-8666-666666666666'),
        destination_clock_epoch='77777777-7777-4777-8777-777777777777',
        calibration_profile_sha256='a' * 64, motion_profile_sha256='b' * 64, nn_manifest_sha256='c' * 64))
    fields = binding.fields
    scope = SessionScope(fields['source_id'], fields['jetson_session'], binding.sha256,
        fields['calibration_profile_sha256'], fields['motion_profile_sha256'], fields['pc_clock_epoch'])
    policy = ClockPolicy(max_rtt_us=150_000, max_offset_width_us=150_000, drift_ppm=100,
        lease_us=1_000_000, lookback_us=500_000)
    return binding, scope, policy


def observation(scope, sequence):
    now = monotonic_us()
    # 실제 촬영 주장이 아닌 원 software 표본이다. 전송 중 now로 E를 다시 만들지 않는다.
    return ObservationFrame(scope, sequence, rgb_pc_interval=(now - 200_000, now - 199_000),
        depth_pc_interval=(now - 199_900, now - 198_900), render_completed_pc_us=now - 100_000,
        expires_pc_us=now + 300_000, rgb=RGB, depth=DEPTH)


class MeasuredSocket(socket.socket):
    """실제 nonblocking FD의 recv 호출 수/크기만 관측하는 시험 owner다."""
    def __init__(self, endpoint):
        super().__init__(endpoint.family, endpoint.type, endpoint.proto, fileno=endpoint.detach())
        self.setblocking(False)
        self.read_limits = []
        self.before_recv = self.after_recv = None

    def recv(self, size, flags=0):
        self.read_limits.append(size)
        if self.before_recv is not None: self.before_recv()
        raw = super().recv(size, flags)
        if self.after_recv is not None: self.after_recv()
        return raw


class HilObservationSessionTests(unittest.TestCase):
    def pair(self):
        left, right = socket.socketpair()
        left.setblocking(False); right.setblocking(False)
        self.addCleanup(left.close); self.addCleanup(right.close)
        return left, right

    def start(self, function, stop):
        state = {}
        def target():
            try: state['result'] = function()
            except BaseException as error: state['error'] = error
        thread = threading.Thread(target=target, daemon=False)
        thread.start()
        def cleanup():
            stop.set(); thread.join(2)
            self.assertFalse(thread.is_alive(), 'caller must reap the session thread')
        self.addCleanup(cleanup)
        return thread, state

    def peer_exchange(self, endpoint, scope, packet, *, expected_kind):
        sender, receiver = PacketSender(endpoint, scope), PacketReceiver(endpoint, scope, completed_clock=monotonic_us)
        started = monotonic_us()
        end = started + IO_TIMEOUT_US
        sender.offer(packet, expires_us=end, now_us=started)
        packets = []
        while monotonic_us() < end:
            now = monotonic_us()
            if sender.pending is not None or sender.active is not None: sender.step(now)
            readable, _, _ = select.select([endpoint], [], [], .002)
            if not readable: continue
            if receiver.deadline_us is None: receiver.begin(expires_us=end, now_us=monotonic_us())
            received = receiver.step(monotonic_us())
            if received is not None:
                packets.append(received)
                if received.packet.kind == expected_kind: return packets
        self.fail('actual original peer packet did not complete inside its fixed200ms operation')

    def body_session(self):
        """원 frame header만 실제 수신한 Jetson session과 유한 software clock이다."""
        binding, scope, policy = fixtures()
        left, peer = self.pair()
        endpoint = MeasuredSocket(left)
        self.addCleanup(endpoint.close)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        ticks, stopped = [monotonic_us()], [False]
        session = _Session(endpoint, scope, 2, lambda: ticks[0], lambda: stopped[0], role='JETSON',
            binding=binding, clock_policy=policy, destination_clock_id=binding.clock_id,
            probe_interval_us=20_000, on_frame=lambda *_: False)
        self.addCleanup(lambda: session.complete('CANCELLED', None))
        packet = observation(scope, 1)
        for part in packet.wire_parts[:2]: self.assertEqual(peer.send(part), len(part))
        session.receiver.begin(expires_us=ticks[0] + IO_TIMEOUT_US, now_us=ticks[0])
        session.receiver.step(ticks[0])
        session.receiver.step(ticks[0])
        endpoint.read_limits.clear()
        return session, endpoint, peer, ticks, stopped, packet

    def leave_last_frame_byte(self, session, peer, ticks, frame):
        body = frame.rgb + frame.depth
        for start in range(0, len(body) - 1, MAX_IO):
            chunk = body[start:min(start + MAX_IO, len(body) - 1)]
            self.assertEqual(peer.send(chunk), len(chunk))
            self.assertIsNone(session.receiver.step(ticks[0]))
        self.assertEqual(session.receiver.partial_bytes, len(body) - 1)
        return body[-1:]

    def test_pending_unstarted_frame_yields_to_original_clock_without_refunding_head(self):
        _, scope, _ = fixtures()
        pc, peer = self.pair()
        now = monotonic_us()
        frame = observation(scope, 1)
        frames = [frame]
        session = _Session(pc, scope, 2, lambda: now, lambda: False, role='PC',
            next_frame=lambda: frames.pop() if frames else None)
        self.addCleanup(lambda: session.complete('CANCELLED', None))
        fields = scope.fields
        probe = ClockProbe(ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['session_id']),
            1, b'\x02' * 16, now - 10, now - 10 + IO_TIMEOUT_US)
        packet = ClockPacket(scope, 'CLOCK_PROBE', probe.raw_bytes)
        for part in packet.wire_parts: self.assertEqual(peer.send(part), len(part))
        # prefix만 먼저 받은 상태에서 다음 step이 FRAME을 pending에 올리고 PROBE를 완성한다.
        session.receiver.begin(expires_us=now + IO_TIMEOUT_US, now_us=now)
        self.assertIsNone(session.receiver.step(now))
        self.assertIsNone(session.step(wait_cap_us=0))
        prefix = peer.recv(PREFIX.size)
        self.assertEqual(len(prefix), PREFIX.size)
        self.assertEqual(PREFIX.unpack(prefix)[2], KINDS['CLOCK_REPLY'])
        self.assertEqual(session.counts['frames_dropped'], 1)
        self.assertEqual(session.counts['frames_sent'], 0)
        self.assertEqual(session.sender.active.kind, 'CLOCK_REPLY')
        reply = ClockReply.from_raw(session.sender.active.clock_raw_bytes)
        self.assertEqual((reply.probe.raw_bytes, reply.p2_us, reply.p3_us), (probe.raw_bytes, now, now))
        # 철회한 FRAME도 offer 시 소비된 순번/E를 재발급하지 않는다.
        self.assertFalse(session.sender.offer(frame, expires_us=frame.expires_pc_us, now_us=now))
        self.assertIsNone(session.sender.pending)
        self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(session.counts['replies_sent'], 1)
        self.assertEqual(session.counts['frames_dropped'], 1)

    def test_jetson_drain_returns_after_four_original_io_quanta(self):
        session, endpoint, peer, ticks, _, frame = self.body_session()
        end = session.receiver.deadline_us
        payload = frame.rgb[:4 * MAX_IO + 1]
        self.assertEqual(peer.send(payload), len(payload))
        self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(endpoint.read_limits, [MAX_IO] * 4)
        self.assertEqual(session.receiver.partial_bytes, 4 * MAX_IO)
        self.assertEqual(session.receiver.deadline_us, end)
        self.assertEqual(session.counts['frames_received'], 0)

    def test_jetson_drain_stops_when_partial_socket_is_empty_without_renewing_e(self):
        session, endpoint, peer, ticks, _, _ = self.body_session()
        end = session.receiver.deadline_us
        self.assertEqual(peer.send(b'x'), 1)
        self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(len(endpoint.read_limits), 1)
        self.assertEqual(session.receiver.partial_bytes, 1)
        self.assertEqual(session.receiver.deadline_us, end)

    def test_jetson_drain_checks_original_partial_deadline_before_each_recv(self):
        session, endpoint, peer, ticks, _, frame = self.body_session()
        end = session.receiver.deadline_us
        self.assertEqual(peer.send(frame.rgb[:2 * MAX_IO]), 2 * MAX_IO)
        endpoint.after_recv = lambda: ticks.__setitem__(0, end)
        with self.assertRaisesRegex(TransportTimeout, 'original receive deadline expired'):
            session.step(wait_cap_us=0)
        self.assertEqual(len(endpoint.read_limits), 1)
        self.assertTrue(session.receiver.closed)

    def test_jetson_drain_stops_at_eagain_despite_stale_readability(self):
        session, endpoint, peer, _, _, _ = self.body_session()
        end = session.receiver.deadline_us
        self.assertEqual(peer.send(b'x'), 1)
        # readiness 뒤 read가 EAGAIN인 정상 경쟁을 선언한다. 원 recv 자체는 실제 FD다.
        with patch('tools.hil_observation.session.select.select', return_value=([endpoint], [], [])):
            self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(len(endpoint.read_limits), 2)
        self.assertEqual(session.receiver.partial_bytes, 1)
        self.assertEqual(session.receiver.deadline_us, end)

    def test_completed_frame_and_empty_socket_leave_no_phantom_receive_deadline(self):
        session, endpoint, peer, ticks, _, frame = self.body_session()
        last = self.leave_last_frame_byte(session, peer, ticks, frame)
        endpoint.read_limits.clear()
        self.assertEqual(peer.send(last), 1)
        self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(session.counts['frames_received'], 1)
        self.assertEqual(len(endpoint.read_limits), 1)
        self.assertIsNone(session.receiver.deadline_us)
        self.assertEqual(session.receiver.partial_bytes, 0)

    def test_completed_frame_reference_is_released_before_next_partial_read(self):
        session, endpoint, peer, ticks, _, frame = self.body_session()
        last = self.leave_last_frame_byte(session, peer, ticks, frame)
        received_ids, stamps = [], []
        original = session.received
        def received(item):
            self.assertTrue(gc.is_tracked(item.packet))
            received_ids.append(id(item.packet))
            stamps.append(item.received_local_us)
            original(item)
        session.received = received
        def before_recv():
            if received_ids:
                self.assertFalse(any(id(value) == received_ids[-1] for value in gc.get_objects()),
                                 'completed frame still retained before next partial read')
        endpoint.before_recv = before_recv
        completed = ticks[0] + 7
        endpoint.after_recv = lambda: ticks.__setitem__(0, completed)
        endpoint.read_limits.clear()
        self.assertEqual(peer.send(last + b'X'), 2)
        self.assertIsNone(session.step(wait_cap_us=0))
        self.assertEqual(stamps, [completed])
        self.assertEqual(len(endpoint.read_limits), 2)
        self.assertEqual(session.receiver.partial_bytes, 1)

    def test_jetson_drain_observes_cancellation_between_receives(self):
        session, endpoint, peer, _, stopped, frame = self.body_session()
        self.assertEqual(peer.send(frame.rgb[:2 * MAX_IO]), 2 * MAX_IO)
        endpoint.after_recv = lambda: stopped.__setitem__(0, True)
        self.assertEqual(session.step(wait_cap_us=0), 'CANCELLED')
        self.assertEqual(len(endpoint.read_limits), 1)

    def test_two_original_clock_samples_map_frames_and_queue_drop_without_retiming(self):
        binding, scope, policy = fixtures(); jetson, pc = self.pair(); stop = threading.Event()
        sequence = [0]; original_frames = []; offered = []
        stopped_after_frames = [False]
        def next_frame():
            sequence[0] += 1
            frame = observation(scope, sequence[0])
            original_frames[:] = [frame]
            return frame
        pc_thread, pc_state = self.start(lambda: run_pc_session(pc, scope=scope, duration_s=2,
            next_frame=next_frame, cancelled=stop.is_set), stop)
        def on_frame(frame, owner):
            frame.validate_original()
            view = owner.current_view(frame, now_j_us=monotonic_us())
            frame.validate_current(view, monotonic_us(), binding=binding, owner_token=owner.owner_token)
            offered.append((frame, owner))
            if len(offered) == 1: return False
            stopped_after_frames[0] = True
            stop.set()
            return True
        result = run_jetson_session(jetson, binding=binding, scope=scope, clock_policy=policy,
            destination_clock_id=binding.clock_id, duration_s=2, probe_interval_us=20_000,
            on_frame=on_frame, cancelled=stop.is_set)
        stop.set(); pc_thread.join(2)
        self.assertFalse(pc_thread.is_alive()); self.assertNotIn('error', pc_state)
        self.assertIsNone(result.failure, result)
        self.assertTrue(stopped_after_frames[0])
        self.assertEqual(result.reason, 'CANCELLED')
        # 두 관측 뒤 caller가 peer를 닫았으므로 종료 중 reset은 원 진단으로 남길 수 있다.
        # 교환 중 일반 오류를 허용하거나 SessionResult의 실제 failure를 지우지는 않는다.
        peer_result = pc_state['result']
        if peer_result.failure is not None:
            self.assertEqual(peer_result.reason, 'TransportError', peer_result)
            self.assertEqual(peer_result.failure, 'stream receive failed: ConnectionResetError', peer_result)
        self.assertGreaterEqual(result.counts['replies_accepted'], 2, result)
        self.assertGreaterEqual(result.counts['leases_issued'], 1, result)
        self.assertEqual(result.counts['frames_offered'], 1, result)
        self.assertGreaterEqual(result.counts['frames_dropped'], 1, result)
        self.assertEqual(len(offered), 2, result)
        frame, owner = offered[-1]
        self.assertIs(frame.observation.rgb.payload, frame._rgb_payload)
        self.assertEqual(frame.clock_j4_us, tuple(s.j4_us for s in frame._lease_token.samples))
        self.assertEqual(frame.rgb_pc_interval, tuple(frame.proof_fields['packet_header']['rgb']['pc_interval_us']))
        self.assertLessEqual(frame.expires_j_us, frame.observation.capture_interval[0] + 500_000)
        self.assertIsNotNone(result.lease_evidence)
        self.assertEqual(result.last_frame_sequence, frame.observation.rgb.stamp.sequence)
        self.assertEqual(len(result.last_frame_proof_sha256), 64)
        with self.assertRaises(ValueError): owner.current_view(frame, now_j_us=monotonic_us())
        self.assertEqual((jetson.fileno(), pc.fileno()), (-1, -1))

    def test_cancel_arriving_during_select_precedes_the_next_receive(self):
        _, scope, _ = fixtures()
        endpoint, _ = self.pair()
        stop = threading.Event()
        def selected(*args):
            stop.set()
            return [endpoint], [], []
        with patch('tools.hil_observation.session.select.select', side_effect=selected), \
                patch.object(PacketReceiver, 'step', side_effect=AssertionError('receive after caller cancellation')):
            result = run_pc_session(endpoint, scope=scope, duration_s=2,
                next_frame=lambda: None, cancelled=stop.is_set)
        self.assertEqual(result.reason, 'CANCELLED')
        self.assertIsNone(result.failure)
        self.assertEqual(endpoint.fileno(), -1)

    def test_actor_pump_interleaves_original_probe_and_stop_without_a_receiver_thread(self):
        binding, scope, policy = fixtures()
        jetson, pc = self.pair()
        stopped = [False]
        pump = JetsonObservationPump(jetson, binding=binding, scope=scope, clock_policy=policy,
            destination_clock_id=binding.clock_id, duration_s=2, probe_interval_us=20_000,
            on_frame=lambda frame, owner: False, cancelled=lambda: stopped[0])
        self.addCleanup(pump.close)
        receiver = PacketReceiver(pc, scope, completed_clock=monotonic_us)
        now = monotonic_us()
        receiver.begin(expires_us=now + IO_TIMEOUT_US, now_us=now)
        original_probe = None
        end = now + IO_TIMEOUT_US
        while original_probe is None and monotonic_us() < end:
            self.assertIsNone(pump.step())
            original_probe = receiver.step(monotonic_us())
        self.assertIsNotNone(original_probe)
        self.assertEqual(original_probe.packet.kind, 'CLOCK_PROBE')
        probe = ClockProbe.from_raw(original_probe.packet.clock_raw_bytes)
        self.assertEqual(probe.source.jetson_session, binding.fields['jetson_session'])
        self.assertLessEqual(probe.expires_j_us - probe.j1_us, IO_TIMEOUT_US)
        # A main에서 lifecycle STOP을 읽은 뒤 다음 pump 진입이 수신 owner를 닫는다.
        stopped[0] = True
        result = pump.step()
        self.assertEqual(result.reason, 'CANCELLED')
        self.assertIsNone(result.failure)
        self.assertEqual(result.counts['probes_sent'], 1)
        self.assertIs(pump.close(), result)
        self.assertIs(pump.step(), result)
        self.assertEqual(jetson.fileno(), -1)

    def test_resident_pc_waits_for_actual_first_probe_before_taking_a_frame(self):
        _, scope, _ = fixtures()
        endpoint, peer = self.pair()
        stop = threading.Event(); took = threading.Event()
        def next_frame():
            took.set()
            return None
        thread, state = self.start(lambda: run_pc_session(endpoint, scope=scope, duration_s=1,
            next_frame=next_frame, cancelled=stop.is_set, wait_for_probe=True), stop)
        # peer가 먼저 준비되기 전에는 원 frame조차 take하지 않는다.
        self.assertFalse(took.wait(.02))
        with self.assertRaises(BlockingIOError): peer.recv(1)
        fields = scope.fields; now = monotonic_us()
        probe = ClockProbe(ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['session_id']),
            1, b'\x01' * 16, now, now + IO_TIMEOUT_US)
        received = self.peer_exchange(peer, scope, ClockPacket(scope, 'CLOCK_PROBE', probe.raw_bytes),
            expected_kind='CLOCK_REPLY')
        self.assertEqual(received[-1].packet.kind, 'CLOCK_REPLY')
        self.assertTrue(took.wait(.2))
        stop.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertNotIn('error', state)
        self.assertEqual(state['result'].counts['frames_sent'], 0)

    def test_clock_control_waits_for_active_frame_and_keeps_original_p2_p3(self):
        _, scope, _ = fixtures(); peer, pc = self.pair(); stop = threading.Event()
        frame = observation(scope, 1); original_e = frame.expires_pc_us; pending = [frame]
        thread, state = self.start(lambda: run_pc_session(pc, scope=scope, duration_s=1,
            next_frame=lambda: pending.pop() if pending else None, cancelled=stop.is_set), stop)
        # 원 시험은 이미 시작한 FRAME의 비선점이다. pending-only 우선순위와 구분한다.
        self.assertTrue(select.select([peer], [], [], .2)[0], 'actual FRAME first byte was not written')
        fields = scope.fields
        j1 = monotonic_us()
        probe = ClockProbe(ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['session_id']),
            1, b'\x01' * 16, j1, j1 + IO_TIMEOUT_US)
        packets = self.peer_exchange(peer, scope, ClockPacket(scope, 'CLOCK_PROBE', probe.raw_bytes), expected_kind='CLOCK_REPLY')
        stop.set(); thread.join(2)
        self.assertNotIn('error', state)
        self.assertEqual([r.packet.kind for r in packets], ['FRAME', 'CLOCK_REPLY'])
        self.assertEqual(packets[0].packet.header_bytes, frame.header_bytes)
        returned = ClockReply.from_raw(packets[1].packet.clock_raw_bytes)
        self.assertEqual(returned.probe.raw_bytes, probe.raw_bytes)
        self.assertLessEqual(returned.p2_us, returned.p3_us)
        self.assertLess(returned.p3_us, packets[1].received_local_us + 1)
        self.assertEqual(frame.expires_pc_us, original_e)
        self.assertEqual(state['result'].counts['replies_sent'], 1)
        self.assertEqual(state['result'].counts['frames_sent'], 1)

    def test_foreign_scope_is_terminal_and_closes_only_the_supplied_stream(self):
        _, scope, _ = fixtures(); session, peer = self.pair()
        fields = scope.fields
        foreign = SessionScope('88888888-8888-4888-8888-888888888888', fields['session_id'],
            fields['source_descriptor_sha256'], fields['calibration_sha256'], fields['motion_bundle_sha256'], fields['pc_clock_epoch'])
        now = monotonic_us()
        probe = ClockProbe(ClockSource(foreign.fields['source_id'], fields['pc_clock_epoch'], fields['session_id']),
            1, b'\x01' * 16, now, now + IO_TIMEOUT_US)
        for part in ClockPacket(foreign, 'CLOCK_PROBE', probe.raw_bytes).wire_parts:
            self.assertEqual(peer.send(part), len(part))
        result = run_pc_session(session, scope=scope, duration_s=1, next_frame=lambda: None, cancelled=lambda: False)
        self.assertEqual(result.reason, 'TransportError')
        self.assertEqual(result.counts['replies_sent'], 0)
        self.assertEqual(session.fileno(), -1); self.assertGreaterEqual(peer.fileno(), 0)

    def test_partial_prefix_keeps_first_deadline_and_times_out_without_resync(self):
        _, scope, _ = fixtures(); session, peer = self.pair(); peer.send(b'X')
        result = run_pc_session(session, scope=scope, duration_s=1, next_frame=lambda: None, cancelled=lambda: False)
        self.assertEqual(result.reason, 'TransportTimeout', result)
        self.assertGreaterEqual(result.finished_local_us - result.started_local_us, IO_TIMEOUT_US)
        self.assertEqual(result.counts['frames_sent'], 0)
        self.assertEqual(session.fileno(), -1)
        peer.settimeout(.3); self.assertEqual(peer.recv(1), b'')

    def test_hard_original_probe_conflict_is_not_a_transient_lease_branch(self):
        binding, scope, policy = fixtures(); jetson, peer = self.pair(); stop = threading.Event()
        thread, state = self.start(lambda: run_jetson_session(jetson, binding=binding, scope=scope,
            clock_policy=policy, destination_clock_id=binding.clock_id, duration_s=1, probe_interval_us=20_000,
            on_frame=lambda *_: self.fail('no FRAME was sent'), cancelled=stop.is_set), stop)
        receiver = PacketReceiver(peer, scope, completed_clock=monotonic_us)
        end = monotonic_us() + IO_TIMEOUT_US
        receiver.begin(expires_us=end, now_us=monotonic_us())
        original = None
        while monotonic_us() < end and original is None:
            if select.select([peer], [], [], .002)[0]: original = receiver.step(monotonic_us())
        self.assertIsNotNone(original)
        probe = ClockProbe.from_raw(original.packet.clock_raw_bytes)
        wrong = replace(probe, nonce=b'\xff' * 16)
        p2 = monotonic_us(); value = ClockReply(wrong, p2, monotonic_us())
        sender = PacketSender(peer, scope)
        sender.offer(ClockPacket(scope, 'CLOCK_REPLY', value.raw_bytes), expires_us=end, now_us=monotonic_us())
        while (sender.pending is not None or sender.active is not None) and monotonic_us() < end:
            sender.step(monotonic_us())
        thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertNotIn('error', state)
        result = state['result']
        self.assertEqual((result.reason, result.failure), ('HilClockError', 'CLOCK_PROBE_CONFLICT'))
        self.assertEqual(result.counts['clock_rejected'], 0)
        self.assertIsNone(result.lease_evidence)
        self.assertEqual(jetson.fileno(), -1)

    def test_cancel_invalid_configuration_clock_and_latest_callback_are_finite(self):
        _, scope, _ = fixtures()
        session, _ = self.pair()
        result = run_pc_session(session, scope=scope, duration_s=1,
            next_frame=lambda: self.fail('cancel precedes producer take'), cancelled=lambda: True)
        self.assertEqual(result.reason, 'CANCELLED'); self.assertEqual(session.fileno(), -1)
        for duration in (True, 0, math.inf):
            with self.subTest(duration=duration):
                session, _ = self.pair()
                with self.assertRaises(ObservationSessionError):
                    run_pc_session(session, scope=scope, duration_s=duration, next_frame=lambda: None, cancelled=lambda: False)
                self.assertEqual(session.fileno(), -1)
        session, _ = self.pair(); ticks = iter((1_000_000, 999_999))
        result = run_pc_session(session, scope=scope, duration_s=1, next_frame=lambda: None,
            cancelled=lambda: False, clock=lambda: next(ticks))
        self.assertEqual(result.failure, 'LOCAL_MONOTONIC_CLOCK_REGRESSED_OR_INVALID')
        self.assertEqual(session.fileno(), -1)
        session, _ = self.pair()
        result = run_pc_session(session, scope=scope, duration_s=1, next_frame=object, cancelled=lambda: False)
        self.assertEqual(result.failure, 'NEXT_FRAME_REQUIRES_ORIGINAL_SCOPE_OBSERVATION')
        self.assertEqual(session.fileno(), -1)
