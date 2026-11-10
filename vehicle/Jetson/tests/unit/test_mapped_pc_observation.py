"""원 PC bytes/실제 pure clock factory/opaque owner의 software 수명 시험이다.

literal4시각은 단위 가정이다. 실제 PC↔Jetson 네트워크·SDK·S/A/M/D 등록·NN 실행·
World·물리 READY/HIL을 수락하지 않는다. GT pose나 scene 입력은 없다.
"""

from dataclasses import replace
import json
import unittest

from tools.hil_observation.clock import ClockSource, ClockPolicy, HilObservationClock, reply
from tools.hil_observation.transport import SessionScope, ObservationFrame, ReceivedPacket
from tools.hil_observation.mapped import MappedPcOwner, PcMappingError
from xenova_vehicle.perception.inputs import PerceptionInput, ImageFrame
from xenova_vehicle.simulation.pc_input import PcSourceBinding, PcFrameInput, PcCurrentView


RGB = b'\x10' * (640 * 360 * 3 // 2)
DEPTH = b'\xe8\x03' * (640 * 480)
SOURCE = '11111111-1111-4111-8111-111111111111'
PC = '22222222-2222-4222-8222-222222222222'
JETSON = '33333333-3333-4333-8333-333333333333'


def binding_fields():
    return dict(profile='development-pc-source-1', service_instance='44444444-4444-4444-8444-444444444444',
        source_id=SOURCE, pc_clock_epoch=PC, jetson_session=JETSON,
        stream_instances=dict(rgb_video='55555555-5555-4555-8555-555555555555',
            depth_right='66666666-6666-4666-8666-666666666666'),
        destination_clock_epoch='77777777-7777-4777-8777-777777777777',
        calibration_profile_sha256='a' * 64, motion_profile_sha256='b' * 64, nn_manifest_sha256='c' * 64)


def exchange(owner, sequence, j1):
    original = owner.probe(sequence, bytes([sequence]) * 16, j1, j1 + 100_000)
    returned = reply(original, j1 + 10, j1 + 11)
    return owner.accept(returned, j1 + 21)


def make_pc_case(binding=None, *, rgb_pc_interval=(1_080_000, 1_080_002),
                 depth_pc_interval=(1_079_999, 1_080_003), frame_received_j_us=1_100_100,
                 now_j_us=1_100_110, expires_pc_us=1_579_999):
    """consumer 시험도 재사용할 수 있는 명시 software leaf다. 새 source 승인 fixture는 아니다."""
    binding = PcSourceBinding(binding_fields()) if binding is None else binding
    value = binding.fields
    scope = SessionScope(value['source_id'], value['jetson_session'], binding.sha256,
        value['calibration_profile_sha256'], value['motion_profile_sha256'], value['pc_clock_epoch'])
    clocks = HilObservationClock(ClockSource(value['source_id'], value['pc_clock_epoch'], value['jetson_session']),
        ClockPolicy(max_rtt_us=100, max_offset_width_us=100, drift_ppm=0, lease_us=1_000_000, lookback_us=500_000))
    exchange(clocks, 1, 1_000_000)
    exchange(clocks, 2, 1_010_000)
    owner = MappedPcOwner(binding, scope, clocks, destination_clock_id=binding.clock_id)
    packet = ObservationFrame(scope, 1, rgb_pc_interval=rgb_pc_interval, depth_pc_interval=depth_pc_interval,
        render_completed_pc_us=max(rgb_pc_interval[1], depth_pc_interval[1]) + 100,
        expires_pc_us=expires_pc_us, rgb=RGB, depth=DEPTH)
    received = ReceivedPacket(packet, frame_received_j_us)
    frame = owner.accept_frame(received, now_j_us=now_j_us)
    view = owner.current_view(frame, now_j_us=now_j_us)
    return owner, frame, view, clocks, received, scope


class MappedPcObservationTests(unittest.TestCase):
    def test_original_pc_hulls_two_clock_samples_and_payload_identity_survive_mapping(self):
        owner, frame, view, clocks, received, _ = make_pc_case()
        frame.validate_original()
        frame.validate_current(view, 1_100_110, binding=frame.binding, owner_token=owner.owner_token)
        self.assertIs(frame.observation.rgb.payload, RGB)
        self.assertIs(frame.observation.depth.payload, DEPTH)
        self.assertEqual(frame.rgb_pc_interval, received.packet.rgb_pc_interval)
        self.assertEqual(frame.depth_pc_interval, received.packet.depth_pc_interval)
        self.assertEqual(frame.mapped_rgb_j_us, (1_079_989, 1_080_013))
        self.assertEqual(frame.mapped_depth_j_us, (1_079_988, 1_080_014))
        self.assertEqual(frame.expires_j_us, 1_579_988)
        self.assertEqual(frame.observation.rgb.stamp.available_us, received.received_local_us)
        self.assertTrue(frame.observation.rgb.stamp.source_id.startswith('sim-pc:'))
        self.assertFalse(frame.observation.live_authority)
        self.assertEqual(frame.clock_reply_bytes, tuple(s.reply.raw_bytes for s in clocks.lease_evidence.samples))
        self.assertEqual(frame.clock_j4_us, (1_000_021, 1_010_021))
        proof = frame.proof_fields
        self.assertEqual(proof['packet_header'], received.packet.fields)
        self.assertEqual(len(proof['clock']['samples']), 2)
        self.assertLessEqual(len(frame.proof_bytes), 16 * 1024)
        proof['packet_header']['sequence'] = 999
        self.assertEqual(frame.proof_fields['packet_header']['sequence'], 1)
        with self.assertRaises(AttributeError):
            frame._issuer.owner_token = object()

    def test_future_pc_expiry_uses_deadline_only_and_cannot_move_capture_or_extend_500ms(self):
        owner, frame, _, clocks, received, scope = make_pc_case(expires_pc_us=5_000_000)
        self.assertEqual(frame.expires_j_us, min(frame.observation.capture_interval) + 500_000)
        self.assertLess(frame.expires_j_us, clocks.lease.expires_j_us)
        self.assertEqual(frame.proof_fields['packet_header']['expires_pc_us'], 5_000_000)
        with self.assertRaises(ValueError):
            clocks.lease.map_interval(5_000_000, 5_000_000, 1_100_110)
        late = ObservationFrame(scope, 2, rgb_pc_interval=received.packet.rgb_pc_interval,
            depth_pc_interval=received.packet.depth_pc_interval, render_completed_pc_us=1_080_200,
            expires_pc_us=1_090_000, rgb=RGB, depth=DEPTH)
        with self.assertRaises(ValueError):
            owner.accept_frame(ReceivedPacket(late, 1_100_100), now_j_us=1_100_110)
        repackaged = ObservationFrame(scope, 2, rgb_pc_interval=late.rgb_pc_interval,
            depth_pc_interval=late.depth_pc_interval, render_completed_pc_us=late.render_completed_pc_us,
            expires_pc_us=1_579_999, rgb=RGB, depth=DEPTH)
        with self.assertRaises(ValueError):
            owner.accept_frame(ReceivedPacket(repackaged, 1_100_100), now_j_us=1_100_110)

    def test_warmup_foreign_scope_future_hull_and_owner_token_do_not_make_an_input(self):
        owner, frame, _, _, received, scope = make_pc_case()
        value = frame.binding.fields
        clock = HilObservationClock(ClockSource(value['source_id'], value['pc_clock_epoch'], value['jetson_session']),
            ClockPolicy(100, 100, 0, 1_000_000, 500_000))
        self.assertIsNone(exchange(clock, 1, 1_000_000))
        cold = MappedPcOwner(frame.binding, scope, clock, destination_clock_id=frame.binding.clock_id)
        with self.assertRaises(ValueError):
            cold.accept_frame(received, now_j_us=1_100_110)
        with self.assertRaises(ValueError):
            owner.accept_frame(received, now_j_us=1_100_110)
        view = owner.current_view(frame, now_j_us=1_100_110)
        with self.assertRaises(ValueError):
            frame.validate_current(view, 1_100_110, binding=frame.binding, owner_token=object())
        with self.assertRaises(TypeError):
            PcFrameInput(frame.proof_fields)
        with self.assertRaises(TypeError):
            PcCurrentView(True)
        with self.assertRaises(ValueError):
            make_pc_case(frame_received_j_us=1_080_000)
        changed = value
        changed['profile'] = 'development-owned-source-1'
        with self.assertRaises(ValueError):
            PcSourceBinding(changed)

    def test_normal_clock_renewal_and_new_capture_keep_active_original_lease_and_e(self):
        owner, frame, _, clock, _, scope = make_pc_case()
        original_evidence = frame._lease_token
        original_proof, original_e = frame.proof_bytes, frame.expires_j_us
        self.assertIsNotNone(exchange(clock, 3, 1_120_000))
        self.assertIsNot(clock.lease_evidence, original_evidence)
        second = ObservationFrame(scope, 2, rgb_pc_interval=(1_130_000, 1_130_002),
            depth_pc_interval=(1_129_999, 1_130_003), render_completed_pc_us=1_131_000,
            expires_pc_us=1_629_999, rgb=RGB, depth=DEPTH)
        owner.accept_frame(ReceivedPacket(second, 1_140_000), now_j_us=1_140_010)
        current = owner.current_view(frame, now_j_us=1_140_020)
        frame.validate_current(current, 1_140_020, binding=frame.binding, owner_token=owner.owner_token)
        self.assertIs(frame._lease_token, original_evidence)
        self.assertIs(frame.proof_bytes, original_proof)
        self.assertEqual(frame.expires_j_us, original_e)
        with self.assertRaises(AttributeError):
            owner.clock_owner = clock
        with self.assertRaises(AttributeError):
            owner.binding = frame.binding
        with self.assertRaises(AttributeError):
            owner.scope = scope

    def test_source_revoke_local_fence_and_original_expiry_require_new_current_view(self):
        for cause in ('clock-source', 'local', 'expired'):
            with self.subTest(cause=cause):
                owner, frame, _, clock, _, _ = make_pc_case()
                now = 1_100_110
                if cause == 'clock-source':
                    clock.replace_source(ClockSource(SOURCE, PC, '88888888-8888-4888-8888-888888888888'))
                elif cause == 'local':
                    owner.fence()
                else:
                    now = frame.expires_j_us
                with self.assertRaises(ValueError):
                    owner.current_view(frame, now_j_us=now)
                # source owner가 새 view를 발급하지 못하면 caller가 과거 view 대신 None을 공급한다.
                with self.assertRaises(ValueError):
                    frame.validate_current(None, now, binding=frame.binding, owner_token=owner.owner_token)

    def test_nn_join_preserves_rgb_right_objects_original_proof_and_current_token(self):
        owner, frame, view, _, _, _ = make_pc_case()
        original = frame.observation
        # carrier의 identity만 검사한다. 이 marker는 NN 결과/품질 수락이 아니며 DMap은 따로 거절한다.
        joined = PerceptionInput('SYNTHETIC', original.calibration_id, original.rgb, original.depth, object(), ('unit-marker',))
        result = frame.with_observation(joined)
        result.validate_original()
        result.validate_current(view, 1_100_110, binding=frame.binding, owner_token=owner.owner_token)
        self.assertIs(result.proof_bytes, frame.proof_bytes)
        self.assertIs(result.observation.rgb, frame.observation.rgb)
        self.assertIs(result._lease_token, frame._lease_token)
        copied = ImageFrame(original.rgb.stamp, 640, 360, 'NV12', original.rgb.payload)
        with self.assertRaises(ValueError):
            frame.with_observation(replace(joined, rgb=copied))
        with self.assertRaises(ValueError):
            frame.with_observation(replace(joined, kind='LIVE'))


if __name__ == '__main__':
    unittest.main()
