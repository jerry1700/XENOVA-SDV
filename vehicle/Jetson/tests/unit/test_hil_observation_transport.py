"""독립 software clock/bytes와 실제 socketpair의 framing 수명 시험이다.

PC↔Nano TCP 배포·Windows/POSIX 상호운용·관측 clock lease·M 수락·READY는 검증하지
않는다. clock payload fixture는 형식 시험이며 실제4timestamp 측정으로 부르지 않는다.
"""

import importlib.util
import json
from pathlib import Path
import socket
import sys
import unittest


def load_tool(filename, name):
    path = Path(__file__).resolve().parents[2] / 'tools/hil_observation' / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


T = load_tool('transport.py', 'hil_observation_transport')
RGB = b'\x10' * T.RGB_BYTES
DEPTH = b'\xe8\x03' * (640 * 480)
SOURCE = '11111111-1111-4111-8111-111111111111'
SESSION = '22222222-2222-4222-8222-222222222222'
PC = '33333333-3333-4333-8333-333333333333'


class Clock:
    def __init__(self):
        self.value = 2_000_000
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value + 5


def scope(*, descriptor='a' * 64):
    return T.SessionScope(SOURCE, SESSION, descriptor, 'b' * 64, 'c' * 64, PC)


def frame(sequence=1, *, original_scope=None, rgb=RGB):
    offset = sequence * 20
    return T.ObservationFrame(scope() if original_scope is None else original_scope, sequence,
        rgb_pc_interval=(1_999_000 + offset, 1_999_002 + offset),
        depth_pc_interval=(1_998_990 + offset, 1_999_003 + offset),
        render_completed_pc_us=1_999_030 + offset, expires_pc_us=2_500_000, rgb=rgb, depth=DEPTH)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def probe_fields():
    return dict(profile='hil-observation-clock-probe-1', source=dict(source_id=SOURCE, pc_session=PC,
        jetson_session=SESSION), sequence='7', nonce='1' * 32, j1_us='2000000', expires_j_us='2100000')


class HilObservationTransportTests(unittest.TestCase):
    def pair(self, *, original_scope=None):
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        clock = Clock()
        bound = scope() if original_scope is None else original_scope
        sender = T.PacketSender(left, bound)
        receiver = T.PacketReceiver(right, bound, completed_clock=clock)
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        return sender, receiver, clock, left, right

    def transfer(self, sender, receiver, clock):
        for _ in range(2048):
            sender.step(clock.value)
            value = receiver.step(clock.value)
            if value is not None:
                return value
        self.fail('finite socketpair transfer did not complete')

    def inject(self, endpoint, receiver, clock, raw, *, chunk=4093):
        offset = 0
        for _ in range(2048):
            if offset < len(raw):
                try:
                    offset += endpoint.send(raw[offset:offset + chunk])
                except BlockingIOError:
                    pass
            value = receiver.step(clock.value)
            if value is not None or offset == len(raw) and receiver.deadline_us is None:
                return value
        self.fail('finite injected stream did not complete')

    def test_fragmented_original_frame_keeps_four_endpoints_payload_hashes_and_receive_event_separate(self):
        sender, receiver, clock, left, right = self.pair()
        original = frame()
        self.assertIs(original.rgb, RGB)
        self.assertIs(original.depth, DEPTH)
        self.assertFalse(hasattr(original, 'to_perception_input'))
        with self.assertRaises(AttributeError):
            original.sequence = 9
        fields = original.fields
        fields['scope']['source_id'] = 'changed'
        fields['rgb']['pc_interval_us'][0] = 0
        self.assertEqual(original.fields['scope']['source_id'], SOURCE)
        sender.offer(original, expires_us=original.expires_pc_us, now_us=clock.value)
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        got = self.transfer(sender, receiver, clock)
        self.assertEqual(got.packet.header_bytes, original.header_bytes)
        self.assertEqual((got.packet.rgb, got.packet.depth), (RGB, DEPTH))
        self.assertEqual(got.packet.rgb_pc_interval, original.rgb_pc_interval)
        self.assertEqual(got.packet.depth_pc_interval, original.depth_pc_interval)
        self.assertEqual(got.packet.render_completed_pc_us, original.render_completed_pc_us)
        self.assertEqual(got.received_local_us, clock.value + 5)
        self.assertNotEqual(got.received_local_us, got.packet.render_completed_pc_us)
        self.assertEqual(clock.calls, 1)
        self.assertEqual(sender.stats['sent_packets'], 1)
        self.assertEqual(receiver.stats['received_bytes'], sum(map(len, original.wire_parts)))
        self.assertFalse(left.getblocking())
        self.assertFalse(right.getblocking())

    def test_latest_one_plus_active_one_does_not_reissue_discarded_sequence_or_original_e(self):
        sender, receiver, clock, _, _ = self.pair()
        first, dropped, latest = frame(1), frame(2), frame(3)
        sender.offer(first, expires_us=first.expires_pc_us, now_us=clock.value)
        sender.step(clock.value)
        self.assertIs(sender.active, first)
        sender.offer(dropped, expires_us=dropped.expires_pc_us, now_us=clock.value)
        sender.offer(latest, expires_us=latest.expires_pc_us, now_us=clock.value)
        self.assertIs(sender.pending, latest)
        self.assertEqual(sender.stats['replaced'], 1)
        self.assertFalse(sender.discard_pending(dropped))
        self.assertFalse(sender.offer(latest, expires_us=latest.expires_pc_us, now_us=clock.value))
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        self.assertEqual(self.transfer(sender, receiver, clock).packet.sequence, 1)
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        self.assertEqual(self.transfer(sender, receiver, clock).packet.sequence, 3)
        with self.assertRaises(T.TransportError):
            sender.offer(dropped, expires_us=dropped.expires_pc_us, now_us=clock.value)
        with self.assertRaises(T.TransportError):
            sender.offer(latest, expires_us=latest.expires_pc_us + 1, now_us=clock.value)
        self.assertEqual(sender.stats['offered'], 3)

    def test_corrupt_payload_sha_wrong_body_length_and_oversize_header_fence_before_reuse(self):
        original = frame()
        prefix, header, rgb, depth = original.wire_parts
        broken = bytearray(rgb)
        broken[17] ^= 1
        cases = (
            prefix + header + bytes(broken) + depth,
            T.PREFIX.pack(T.MAGIC, T.VERSION, T.KINDS['FRAME'], len(header), T.FRAME_BYTES - 1, 1),
            T.PREFIX.pack(T.MAGIC, T.VERSION, T.KINDS['FRAME'], T.HEADER_CAP + 1, T.FRAME_BYTES, 1),
        )
        for raw in cases:
            with self.subTest(size=len(raw)):
                _, receiver, clock, left, right = self.pair()
                receiver.begin(expires_us=2_400_000, now_us=clock.value)
                with self.assertRaises(T.TransportError):
                    self.inject(left, receiver, clock, raw)
                self.assertTrue(receiver.closed)
                self.assertEqual(receiver.partial_bytes, 0)
                self.assertGreaterEqual(right.fileno(), 0)
                with self.assertRaises(T.TransportError):
                    receiver.begin(expires_us=3_000_000, now_us=clock.value)

    def test_truncated_eof_discards_partial_and_slow_peer_cannot_renew_original_receive_deadline(self):
        for eof in (True, False):
            with self.subTest(eof=eof):
                _, receiver, clock, left, right = self.pair()
                receiver.begin(expires_us=2_000_100, now_us=clock.value)
                left.send(frame().wire_parts[0][:9])
                self.assertIsNone(receiver.step(clock.value))
                self.assertEqual(receiver.partial_bytes, 9)
                with self.assertRaises(T.TransportError):
                    receiver.begin(expires_us=2_900_000, now_us=clock.value)
                if eof:
                    left.close()
                    error = T.TransportEOF
                else:
                    clock.value = 2_000_100
                    error = T.TransportTimeout
                with self.assertRaises(error):
                    receiver.step(clock.value)
                self.assertTrue(receiver.closed)
                self.assertEqual(receiver.partial_bytes, 0)
                self.assertGreaterEqual(right.fileno(), 0)

    def test_foreign_scope_unknown_gt_field_bool_dimensions_and_noncanonical_header_are_rejected(self):
        sender, _, clock, _, _ = self.pair()
        foreign = frame(original_scope=scope(descriptor='d' * 64))
        with self.assertRaises(T.TransportError):
            sender.offer(foreign, expires_us=foreign.expires_pc_us, now_us=clock.value)
        values = []
        value = frame().fields
        value['scope']['source_descriptor_sha256'] = 'd' * 64
        values.append(canonical(value))
        value = frame().fields
        value['pose_gt'] = [0, 0, 0]
        values.append(canonical(value))
        value = frame().fields
        value['rgb']['width'] = True
        values.append(canonical(value))
        values.append(b' ' + frame().header_bytes)
        for header in values:
            with self.subTest(header=header[:80]):
                _, receiver, clock, left, _ = self.pair()
                receiver.begin(expires_us=2_400_000, now_us=clock.value)
                raw = T.PREFIX.pack(T.MAGIC, T.VERSION, T.KINDS['FRAME'], len(header), T.FRAME_BYTES, 1) + header
                with self.assertRaises(T.TransportError):
                    self.inject(left, receiver, clock, raw)
        with self.assertRaises(T.TransportError):
            T.SessionScope('00000000-0000-0000-0000-000000000000', SESSION, 'a' * 64, 'b' * 64, 'c' * 64, PC)

    def test_exact_received_duplicate_has_no_new_delivery_and_same_sequence_changed_bytes_fences(self):
        _, receiver, clock, left, _ = self.pair()
        original = frame()
        raw = b''.join(original.wire_parts)
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        first = self.inject(left, receiver, clock, raw)
        self.assertEqual(first.packet.fields, original.fields)
        clock.value += 100
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        self.assertIsNone(self.inject(left, receiver, clock, raw))
        self.assertEqual(receiver.stats['duplicate'], 1)
        changed = frame(rgb=b'\x11' * T.RGB_BYTES)
        receiver.begin(expires_us=2_400_000, now_us=clock.value)
        with self.assertRaises(T.TransportError):
            self.inject(left, receiver, clock, b''.join(changed.wire_parts))
        self.assertTrue(receiver.closed)
        self.assertEqual(first.packet.rgb, RGB)
        self.assertEqual(first.packet.expires_pc_us, 2_500_000)

    def test_clock_original_ascii_fields_and_full_read_stamp_survive_without_lease_or_capture_claim(self):
        clock_module = load_tool('clock.py', 'hil_observation_transport_clock_fixture')
        sender, receiver, clock, _, _ = self.pair()
        probe = probe_fields()
        reply = dict(profile='hil-observation-clock-reply-1', probe=probe, p2_us='5000000', p3_us='5000001')
        for kind, fields in (('CLOCK_PROBE', probe), ('CLOCK_REPLY', reply)):
            typed = clock_module.ClockProbe if kind == 'CLOCK_PROBE' else clock_module.ClockReply
            raw = typed.from_fields(fields).raw_bytes
            packet = T.ClockPacket(scope(), kind, raw)
            sender.offer(packet, expires_us=2_100_000, now_us=clock.value)
            receiver.begin(expires_us=2_400_000, now_us=clock.value)
            got = self.transfer(sender, receiver, clock)
            self.assertEqual(got.packet.clock_raw_bytes, raw)
            self.assertEqual(got.packet.clock_fields, fields)
            self.assertEqual(typed.from_raw(got.packet.clock_raw_bytes).raw_bytes, raw)
            self.assertEqual(got.received_local_us, clock.value + 5)
            self.assertFalse(hasattr(got, 'lease'))
        for key in ('j1_us', 'expires_j_us', 'nonce'):
            missing = probe_fields()
            del missing[key]
            with self.assertRaises(T.TransportError):
                T.ClockPacket(scope(), 'CLOCK_PROBE', canonical(missing))
        for key in ('p2_us', 'p3_us'):
            missing = dict(reply)
            del missing[key]
            with self.assertRaises(T.TransportError):
                T.ClockPacket(scope(), 'CLOCK_REPLY', canonical(missing))
        for bad in (dict(reply, p2_us='5000002'), dict(reply, p3_us='05000001')):
            with self.assertRaises(T.TransportError):
                T.ClockPacket(scope(), 'CLOCK_REPLY', canonical(bad))
        wrong = probe_fields()
        wrong['source']['pc_session'] = SESSION
        with self.assertRaises(T.TransportError):
            T.ClockPacket(scope(), 'CLOCK_PROBE', canonical(wrong))
        with self.assertRaises(T.TransportError):
            T.ClockPacket(scope(), 'CLOCK_PROBE', b' ' + canonical(probe))

    def test_sender_partial_expiry_and_full_read_expiry_fence_without_closing_caller_socket(self):
        sender, receiver, clock, left, right = self.pair()
        original = frame()
        sender.offer(original, expires_us=original.expires_pc_us, now_us=clock.value)
        sender.step(clock.value)
        self.assertIs(sender.active, original)
        with self.assertRaises(T.TransportTimeout):
            sender.step(original.expires_pc_us)
        self.assertTrue(sender.closed)
        self.assertIsNone(sender.active)
        self.assertIsNone(sender.pending)
        self.assertGreaterEqual(left.fileno(), 0)
        self.assertGreaterEqual(right.fileno(), 0)
        _, receiver, clock, left, right = self.pair()
        packet = T.ClockPacket(scope(), 'CLOCK_PROBE', canonical(probe_fields()))
        receiver.begin(expires_us=clock.value + 5, now_us=clock.value)
        with self.assertRaises(T.TransportTimeout):
            self.inject(left, receiver, clock, b''.join(packet.wire_parts))
        self.assertEqual(clock.calls, 1)
        self.assertTrue(receiver.closed)
        self.assertGreaterEqual(right.fileno(), 0)

    def test_clock_contract_limits_match_actual_parser_without_narrowing_frame_u64(self):
        clock_module = load_tool('clock.py', 'hil_observation_transport_clock_limits')
        maximum = (1 << 63) - 1
        mutations = (
            ('sequence', '0'), ('sequence', str(maximum + 1)), ('nonce', '0' * 32),
            ('j1_us', str(maximum + 1)), ('expires_j_us', str(maximum + 1)),
            ('expires_j_us', '2200001'), ('sequence', '07'),
        )
        for key, value in mutations:
            with self.subTest(probe=key, value=value):
                fields = probe_fields()
                fields[key] = value
                raw = canonical(fields)
                with self.assertRaises(clock_module.HilClockError):
                    clock_module.ClockProbe.from_raw(raw)
                with self.assertRaises(T.TransportError):
                    T.ClockPacket(scope(), 'CLOCK_PROBE', raw)
        for p2, p3 in ((maximum, maximum), (0, maximum), (maximum + 1, maximum + 1)):
            with self.subTest(p2=p2, p3=p3):
                raw = canonical(dict(profile='hil-observation-clock-reply-1', probe=probe_fields(),
                    p2_us=str(p2), p3_us=str(p3)))
                with self.assertRaises(clock_module.HilClockError):
                    clock_module.ClockReply.from_raw(raw)
                with self.assertRaises(T.TransportError):
                    T.ClockPacket(scope(), 'CLOCK_REPLY', raw)
        edge = probe_fields()
        edge.update(sequence=str(maximum), j1_us=str(maximum - 200000), expires_j_us=str(maximum))
        typed = clock_module.ClockProbe.from_fields(edge)
        self.assertEqual(T.ClockPacket(scope(), 'CLOCK_PROBE', typed.raw_bytes).clock_raw_bytes, typed.raw_bytes)
        reply = clock_module.reply(typed, maximum - 1, maximum - 1)
        self.assertEqual(T.ClockPacket(scope(), 'CLOCK_REPLY', reply.raw_bytes).clock_raw_bytes, reply.raw_bytes)
        original = T.ObservationFrame(scope(), (1 << 64) - 1, rgb_pc_interval=(1, 2), depth_pc_interval=(1, 2),
            render_completed_pc_us=3, expires_pc_us=4, rgb=RGB, depth=DEPTH)
        self.assertEqual(original.sequence, (1 << 64) - 1)


if __name__ == '__main__':
    unittest.main()
