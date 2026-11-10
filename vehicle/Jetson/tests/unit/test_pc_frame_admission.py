"""실제 seqpacket 수신/송신 fact와 M-local PC 재검산의 software 회귀다.

BIND·clock·capture·NN tensor는 명시 software leaf다. slot은 codec owned 사본이며
별도 native 시험의 SHM/token 사실이나 실제 S 등록/worker/SDK/Pi/Scene/READY를
주장하지 않는다. callback 시각은 수치 관계 시험용이며 실시간 지연 수락이 아니다.
"""

from dataclasses import replace
from hashlib import sha256
import json
import unittest

import test_pc_binding_messages as bind_fixtures
import test_pc_slot as slot_fixtures
from tools.hil_observation import clock as clock_api
from tools.hil_observation.mapped import MappedPcOwner
from tools.hil_observation.transport import SessionScope, ObservationFrame, ReceivedPacket
from xenova_vehicle.perception.yolop import decode_yolop
from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.native_resources import ArenaSpec
from xenova_vehicle.runtime.pc_binding_messages import PcBindContext, PcBindMessage
from xenova_vehicle.runtime.pc_channel import (PcActorLink, PcActorPacket, PcChannelError,
    pc_actor_pair, forward_received)
from xenova_vehicle.runtime.pc_frame_admission import PcFrameAdmission, PcFrameAdmissionError, PcNnAdmission
from xenova_vehicle.runtime.pc_job_messages import snapshot_ack_metadata
from xenova_vehicle.runtime.pc_slot import PcSlotContext, PcSlotEncoding, decode_owned
from xenova_vehicle.runtime.pc_slot_messages import PcWriterSlotContext, PcReaderSlotContext, pc_slot_meta_fields


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


class PcFrameAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.a_frame, self.encoding, _, _ = slot_fixtures.bundle()
        self.owned = decode_owned(self.encoding.raw, slot_fixtures.expected(self.encoding))
        self.now = 1_100_110
        self.binding = self.encoding.binding
        raw_scope = self.encoding.context.write_key.scope
        service = self.binding.fields['service_instance']
        self.a_peer = LifecyclePeer(service, raw_scope.writer.channel_epoch, raw_scope.writer.worker_instance, 'A', 'e' * 64)
        self.m_peer = LifecyclePeer(service, slot_fixtures.identifier(951), slot_fixtures.identifier(952), 'M', 'f' * 64)
        arena = ArenaSpec(raw_scope.arena_id, raw_scope.arena_name, raw_scope.arena_size_bytes, 'RAW')
        binds = []
        for peer in (self.a_peer, self.m_peer):
            context = PcBindContext(peer, raw_scope.writer, self.binding, raw_scope, arena)
            fields = bind_fixtures.bind_fields(context, issued=self.now - 100, expires=self.now + 1_000_000)
            binds.append(PcBindMessage(bind_fixtures.packet(fields), context))
        self.context = PcReaderSlotContext(PcWriterSlotContext(binds[0], raw_scope), binds[1])
        self.s_a, self.a = self.links(self.a_peer)
        self.m_ticks = []
        self.s_m, self.m = self.links(self.m_peer, worker_clock=lambda: self.m_ticks.pop(0) if self.m_ticks else self.now)
        self.policy = clock_api.ClockPolicy(100, 100, 0, 1_000_000, 500_000)
        self.owner = PcFrameAdmission(self.context, self.m, clock_api=clock_api, clock_policy=self.policy)

    def links(self, peer, worker_clock=None):
        left, right = pc_actor_pair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        supervisor = PcActorLink(left, peer, 'S', self.binding, clock=lambda: self.now)
        worker = PcActorLink(right, peer, peer.role, self.binding, clock=(lambda: self.now) if worker_clock is None else worker_clock)
        self.addCleanup(supervisor.close); self.addCleanup(worker.close)
        return supervisor, worker

    def forward(self, encoding, *, m_receive=None):
        kind = 'PC_EARLY_META' if encoding.phase == 'EARLY' else 'PC_NN_RESULT_META'
        self.a.send(kind, pc_slot_meta_fields(encoding, purpose='PARSER_WARMUP'), encoding.descriptor_bytes,
            request_id=encoding.job_id, issued_us=self.now, expires_us=encoding.expires_j_us)
        self.assertIsNotNone(self.a.flush())
        self.now += 1
        received, = self.s_a.poll()
        self.assertIsNotNone(forward_received(received, self.s_m, origin_link=self.s_a))
        self.assertIsNotNone(self.s_m.flush())
        self.now = self.now + 1 if m_receive is None else m_receive
        result, = self.m.poll()
        return result, received

    def early(self):
        received, original = self.forward(self.encoding)
        frame = self.owner.admit_early(received, self.owned, now_us=self.now)
        return frame, received, original

    def ack(self, frame, *, preempted=False):
        meta = snapshot_ack_metadata(frame, job_id=self.encoding.job_id, purpose='PARSER_WARMUP', snapshot=None, now_us=self.now)
        if preempted: self.m_ticks[:] = [self.now, self.now, self.now + 100]
        self.m.send('PC_SNAPSHOT_ACK', meta, request_id=self.encoding.job_id, issued_us=self.now, expires_us=frame.expires_j_us)
        sent = self.m.flush(); self.assertIsNotNone(sent)
        self.now += 1
        received, = self.s_m.poll()
        self.assertIsNotNone(forward_received(received, self.s_a, origin_link=self.s_m))
        self.assertIsNotNone(self.s_a.flush())
        self.now += 1
        a_received, = self.a.poll()
        self.assertEqual(a_received.packet.kind, 'PC_FORWARD')
        return sent

    def nn(self, *, generation=2, received=None):
        key = replace(self.encoding.context.write_key, generation=generation, write_token=slot_fixtures.identifier(970 + generation))
        context = PcSlotContext(key, self.binding)
        stamp = self.now + 1 if received is None else received
        _, encoding, _, _ = slot_fixtures.bundle(phase='NN', frame=self.a_frame, context=context, nn_received=stamp)
        owned = decode_owned(encoding.raw, slot_fixtures.expected(encoding, completed=stamp + 3))
        self.now = max(self.now, stamp + 4)
        return encoding, owned

    def second_frame(self, *, renew=False):
        api = clock_api; fields = self.binding.fields
        clocks = api.HilObservationClock(api.ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['jetson_session']), self.policy)
        for raw, j4 in zip(self.a_frame.clock_reply_bytes, self.a_frame.clock_j4_us):
            value = api.ClockReply.from_raw(raw); p = value.probe
            clocks.probe(p.sequence, p.nonce, p.j1_us, p.expires_j_us); clocks.accept(value, j4)
        if renew:
            j1 = self.now + 100
            probe = clocks.probe(3, b'\x03' * 16, j1, j1 + 200)
            clocks.accept(api.reply(probe, j1 + 10, j1 + 11), j1 + 21)
            self.now = j1 + 100
        else: self.now += 1000
        scope = SessionScope(fields['source_id'], fields['jetson_session'], self.binding.sha256,
            fields['calibration_profile_sha256'], fields['motion_profile_sha256'], fields['pc_clock_epoch'])
        old = self.a_frame.proof_fields['packet_header']
        packet = ObservationFrame(scope, 2, rgb_pc_interval=tuple(v + 1000 for v in self.a_frame.rgb_pc_interval),
            depth_pc_interval=tuple(v + 1000 for v in self.a_frame.depth_pc_interval),
            render_completed_pc_us=old['render_completed_pc_us'] + 1000, expires_pc_us=old['expires_pc_us'] + 1000,
            rgb=self.a_frame.observation.rgb.payload, depth=self.a_frame.observation.depth.payload)
        mapper = MappedPcOwner(self.binding, scope, clocks, destination_clock_id=self.binding.clock_id)
        frame = mapper.accept_frame(ReceivedPacket(packet, self.now), now_j_us=self.now)
        context = replace(self.encoding.context, write_key=replace(self.encoding.context.write_key,
            generation=3, write_token=slot_fixtures.identifier(973)))
        _, encoding, _, _ = slot_fixtures.bundle(frame=frame, context=context)
        encoding = PcSlotEncoding(context, phase='EARLY', job_id=slot_fixtures.identifier(921),
            expires_j_us=encoding.expires_j_us, full_key=encoding.full_key, pc_frame_proof=encoding.pc_frame_proof,
            pc_packet_header=encoding.pc_packet_header, rgb=encoding.rgb, depth=encoding.depth)
        return frame, encoding, decode_owned(encoding.raw, slot_fixtures.expected(encoding, completed=self.now))

    def test_actual_forward_issues_new_local_capability_and_preserves_original_clock_and_frame(self):
        frame, received, _ = self.early()
        self.assertEqual(frame.proof_bytes, self.a_frame.proof_bytes)
        self.assertEqual((frame.clock_reply_bytes, frame.clock_j4_us), (self.a_frame.clock_reply_bytes, self.a_frame.clock_j4_us))
        self.assertEqual((frame.expires_j_us, frame.observation.capture_interval),
            (self.a_frame.expires_j_us, self.a_frame.observation.capture_interval))
        self.assertEqual((frame.observation.rgb.payload, frame.observation.depth.payload), (self.owned.rgb, self.owned.depth))
        self.assertIsNot(frame._issuer, self.a_frame._issuer)
        view = self.owner.current_view(frame, now_us=self.now)
        frame.validate_current(view, self.now, binding=self.binding, owner_token=self.owner.owner_token)
        with self.assertRaises(PcFrameAdmissionError): self.owner.current_view(self.a_frame, now_us=self.now)
        with self.assertRaises(AttributeError): self.owner.binding = self.binding
        self.assertIsNone(self.owner.admit_early(received, self.owned, now_us=self.now))
        self.assertIsNone(frame.observation.nn)

    def test_nn_requires_actual_original_ack_and_preserves_post_send_preemption_bounds(self):
        frame, _, _ = self.early(); before = self.now
        sent = self.ack(frame, preempted=True)
        encoding, owned = self.nn(received=before + 5)
        received, _ = self.forward(encoding, m_receive=before + 200)
        with self.assertRaises(ValueError):
            self.owner.admit_nn(frame, received, owned, snapshot_ack_sent=None, now_us=self.now)
        result = self.owner.admit_nn(frame, received, owned, snapshot_ack_sent=sent, now_us=self.now)
        self.assertIs(type(result), PcNnAdmission)
        self.assertIs(result.nn_output, owned.nn_output)
        self.assertEqual(result.filled_completed_j_us, owned.expected.original_filled_info.completed_us)
        self.assertLess(result.nn_input.received_j_us, result.snapshot_ack_sent.completed_us)
        self.assertGreaterEqual(result.nn_input.received_j_us, result.snapshot_ack_sent.started_us)
        joined = result.with_candidates(decode_yolop(result.nn_output))
        self.assertIs(joined.observation.rgb, frame.observation.rgb)
        self.assertIs(joined.observation.depth, frame.observation.depth)
        self.assertIs(joined.proof_bytes, frame.proof_bytes)
        self.assertIs(joined._frame_token, frame._frame_token)
        self.assertEqual(joined.observation.nn_key, owned.nn_key)
        with self.assertRaises(PcFrameAdmissionError): result.with_candidates(object())
        self.assertIsNone(self.owner.admit_nn(frame, received, owned, snapshot_ack_sent=sent, now_us=self.now))

    def test_packet_or_another_endpoint_receive_cannot_replace_original_actual_ticket(self):
        frame, received, _ = self.early()
        with self.assertRaises(ValueError): self.owner.admit_early(PcActorPacket(received.packet.raw), self.owned, now_us=self.now)
        other_s, other_m = self.links(self.m_peer)
        other_s.endpoint.send(received.packet.raw)
        foreign, = other_m.poll()
        with self.assertRaises(ValueError): self.owner.admit_early(foreign, self.owned, now_us=self.now)
        self.assertIsNone(self.owner.reason)
        frame.validate_current(self.owner.current_view(frame, now_us=self.now), self.now,
            binding=self.binding, owner_token=self.owner.owner_token)

    def test_fresh_clock_renewal_and_early2_do_not_revoke_delayed_nn1(self):
        frame, _, _ = self.early(); sent = self.ack(frame)
        nn_encoding, nn_owned = self.nn()
        nn_received, _ = self.forward(nn_encoding)  # 실제 수신했지만 main의 후속 슬롯 소비는 보류한다.
        _, encoding2, owned2 = self.second_frame(renew=True)
        received2, _ = self.forward(encoding2)
        frame2 = self.owner.admit_early(received2, owned2, now_us=self.now)
        self.assertEqual(frame2.observation.rgb.stamp.sequence, 2)
        frame.validate_current(self.owner.current_view(frame, now_us=self.now), self.now,
            binding=self.binding, owner_token=self.owner.owner_token)
        result = self.owner.admit_nn(frame, nn_received, nn_owned, snapshot_ack_sent=sent, now_us=self.now)
        self.assertIs(result.frame, frame)
        self.assertEqual(result.expires_j_us, self.a_frame.expires_j_us)

    def test_clock_same_probe_new_j4_is_not_reissued_by_a_new_frame(self):
        original, _, _ = self.early()
        _, encoding2, _ = self.second_frame()
        proof = json.loads(encoding2.pc_frame_proof)
        proof['clock']['samples'][-1]['j4_us'] += 1
        for interval in proof['mapped_j_us']: interval[1] += 1
        bad = PcSlotEncoding(encoding2.context, phase='EARLY', job_id=encoding2.job_id,
            expires_j_us=encoding2.expires_j_us, full_key=encoding2.full_key, pc_frame_proof=canonical(proof),
            pc_packet_header=encoding2.pc_packet_header, rgb=encoding2.rgb, depth=encoding2.depth)
        owned = decode_owned(bad.raw, slot_fixtures.expected(bad, completed=self.now))
        received, _ = self.forward(bad)
        with self.assertRaisesRegex(PcFrameAdmissionError, 'CLOCK_REPLY_OR_J4_CONFLICT'):
            self.owner.admit_early(received, owned, now_us=self.now)
        self.assertEqual(self.owner.reason, 'PC_CLOCK_PROOF_CONTRADICTION')
        with self.assertRaises(PcFrameAdmissionError): self.owner.current_view(original, now_us=self.now)
        self.assertEqual(original.clock_j4_us, self.a_frame.clock_j4_us)

    def test_source_fence_endpoint_close_and_original_expiry_never_make_a_current_view(self):
        frame, _, _ = self.early()
        self.now = frame.expires_j_us
        from xenova_vehicle.runtime.health_policy import FrameExpired
        with self.assertRaises(FrameExpired): self.owner.current_view(frame, now_us=self.now)
        self.assertEqual(frame.expires_j_us, self.a_frame.expires_j_us)
        self.owner.fence('PC_SOURCE_DISCONNECTED')
        self.assertEqual(self.owner.fence_revision, 1)
        self.owner.fence('PC_SOURCE_DISCONNECTED'); self.assertEqual(self.owner.fence_revision, 1)
        with self.assertRaises(PcFrameAdmissionError): self.owner.current_view(frame, now_us=self.now)
        fresh = PcFrameAdmission(self.context, self.m, clock_api=clock_api, clock_policy=self.policy)
        self.m.close()
        with self.assertRaises(EOFError): fresh.current_view(frame, now_us=self.now)
        self.assertEqual(fresh.reason, 'PC_SOURCE_CHANNEL_UNAVAILABLE')
