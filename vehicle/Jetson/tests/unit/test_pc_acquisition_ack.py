"""A의 원 ACK 처리 경계를 실제 metadata socketpair와 software clock으로 검사한다.

worker의 model-start와 EARLY full-send 선행 상태는 명시 단위 fixture다. 실제 등록,
SHM 게시, SDK, NN, Source 또는 READY를 주장하지 않는다. M→S→A의 metadata bytes와
수신 ticket, mapped frame/current owner, driver의 ACK·expiry·enqueue 코드는 실제다.
"""
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_mapped_pc_observation import make_pc_case
from test_pc_slot import identifier
from xenova_vehicle.oak.sim_nn import HostClockSample, SimNnSubmission
from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.pc_acquisition import PcAcquisitionDriver
from xenova_vehicle.runtime.pc_channel import PcActorLink, pc_actor_pair, forward_received
from xenova_vehicle.runtime.pc_job_messages import PcSnapshotAck, snapshot_ack_metadata
from xenova_vehicle.runtime.pc_workers import PcWorkerActor


class CaptureLeaf:
    """SDK를 열지 않는 명시 source port다. 전달된 원 관측/E와 호출만 기록한다."""
    def __init__(self, case):
        self.case, self.calls, self.busy, self.error = case, [], False, None

    def capture(self, observation, *, captured_j_us, expires_j_us):
        self.calls.append((observation, captured_j_us, expires_j_us))
        if self.error is not None: raise self.error
        if self.busy: return None
        now = self.case.now
        return SimNnSubmission(observation, captured_j_us, expires_j_us,
            HostClockSample(now, 1_000_000, now), 'c' * 64)


class AckCase:
    def __init__(self, test):
        self.test, self.now = test, 1_100_110
        self.owner, self.frame, _, _, _, _ = make_pc_case()
        self.binding = self.frame.binding
        service = self.binding.fields['service_instance']
        self.a_peer = LifecyclePeer(service, identifier(980), identifier(981), 'A', 'a' * 64)
        self.m_peer = LifecyclePeer(service, identifier(982), identifier(983), 'M', 'b' * 64)
        self.s_a, self.a = self.links(self.a_peer)
        self.s_m, self.m = self.links(self.m_peer)
        # ACK 메서드의 선행 worker 상태만 선언한다. constructor/start_model 검증과 구분한다.
        actor = object.__new__(PcWorkerActor)
        actor.boot, actor.binding = SimpleNamespace(peer=self.a_peer), self.binding
        actor.stop, actor.leases_released, actor._nn_start = None, True, object()
        actor._nn_source = self.capture = CaptureLeaf(self)
        self.driver = PcAcquisitionDriver(actor, self.a, self.m_peer, self.owner)
        self.driver.offer(self.frame, self.owner)
        with self.clock(): self.job = self.driver.promote()
        self.driver._phase = 'WAIT_SNAPSHOT'
        self.driver._meta_sent = SimpleNamespace(started_us=self.now - 10)

    def clock(self):
        return patch('xenova_vehicle.runtime.pc_acquisition.j_us', side_effect=lambda: self.now)

    def links(self, peer):
        left, right = pc_actor_pair()
        supervisor = PcActorLink(left, peer, 'S', self.binding, clock=lambda: self.now)
        worker = PcActorLink(right, peer, peer.role, self.binding, clock=lambda: self.now)
        self.test.addCleanup(supervisor.close); self.test.addCleanup(worker.close)
        return supervisor, worker

    def ack(self, *, change=None, job_id=None, expires=None, foreign=False):
        meta = snapshot_ack_metadata(self.frame, job_id=self.job.job_id, purpose=self.job.purpose,
            snapshot=None, now_us=self.now)
        if change: meta.update(change)
        supervisor, worker = (self.s_m, self.m)
        if foreign:
            peer = LifecyclePeer(self.m_peer.service_instance, identifier(984), identifier(985), 'M', 'b' * 64)
            supervisor, worker = self.links(peer)
        worker.send('PC_SNAPSHOT_ACK', meta, request_id=self.job.job_id if job_id is None else job_id,
            issued_us=self.now, expires_us=self.job.expires_us if expires is None else expires)
        self.test.assertIsNotNone(worker.flush())
        original, = supervisor.poll()
        self.test.assertIsNotNone(forward_received(original, self.s_a, origin_link=supervisor))
        self.test.assertIsNotNone(self.s_a.flush())
        received, = self.a.poll()
        return received

    def accept(self, received):
        with self.clock(): return self.driver.snapshot_ack(received)


class PcAcquisitionAckTests(unittest.TestCase):
    def test_exact_late_ack_retires_current_job_and_allows_next_fresh_frame(self):
        case = AckCase(self); received = case.ack()
        original = case.job
        case.now = original.expires_us + 5585
        self.assertIsNone(case.accept(received))
        self.assertEqual((case.driver.phase, case.driver.counts['expired']), ('IDLE', 1))
        self.assertIsNone(case.driver.active); self.assertEqual(case.capture.calls, [])
        _, frame, _, _, _, _ = make_pc_case(rgb_pc_interval=(1_680_000, 1_680_002),
            depth_pc_interval=(1_679_999, 1_680_003), frame_received_j_us=1_700_100,
            now_j_us=1_700_110, expires_pc_us=2_179_999)
        # 같은 원 owner의 새 실제 표본을 새 순번으로 발행한다.
        from tools.hil_observation.transport import ObservationFrame, ReceivedPacket
        old = frame.proof_fields['packet_header']
        packet = ObservationFrame(case.owner.scope, 2, rgb_pc_interval=frame.rgb_pc_interval,
            depth_pc_interval=frame.depth_pc_interval, render_completed_pc_us=old['render_completed_pc_us'],
            expires_pc_us=old['expires_pc_us'], rgb=frame.observation.rgb.payload, depth=frame.observation.depth.payload)
        case.now = 1_700_110
        fresh = case.owner.accept_frame(ReceivedPacket(packet, case.now - 10), now_j_us=case.now)
        case.driver.offer(fresh, case.owner)
        with case.clock(): next_job = case.driver.promote()
        self.assertIs(next_job.frame, fresh); self.assertNotEqual(next_job.job_id, original.job_id)
        self.assertEqual(original.expires_us, original.frame.expires_j_us)
        self.assertEqual(next_job.expires_us, fresh.expires_j_us)

    def test_original_e_minus_one_exact_e_and_later_are_retired_without_capture(self):
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                case = AckCase(self); received = case.ack(); case.now = case.job.expires_us + delta
                self.assertIsNone(case.accept(received))
                self.assertEqual(case.driver.counts['expired'], 1)
                self.assertEqual(case.capture.calls, [])

    def test_live_exact_ack_enqueues_original_frame_and_e(self):
        case = AckCase(self); received = case.ack(); case.now += 10
        submission = case.accept(received)
        self.assertIs(submission.observation, case.job.frame.observation)
        self.assertEqual(submission.expires_j_us, case.job.expires_us)
        self.assertEqual(case.capture.calls, [(case.frame.observation, case.frame.observation.capture_interval, case.job.expires_us)])
        self.assertEqual((case.driver.phase, case.driver.counts['enqueued']), ('NN_PENDING', 1))

    def test_expired_wrong_job_e_key_proof_and_purpose_still_raise(self):
        changes = [dict(expires=1_579_989),
            dict(change=dict(full_key=['sim-pc:11111111-1111-4111-8111-111111111111',
                '55555555-5555-4555-8555-555555555555', '77777777-7777-4777-8777-777777777777', 2])),
            dict(change=dict(pc_frame_proof_sha256='f' * 64)),
            dict(change=dict(purpose='METRIC_MAP', snapshot_j_us='1100000', sensor_fence_revision='0', source_context_sha256='f' * 64))]
        for change in changes:
            with self.subTest(change=change):
                case = AckCase(self); received = case.ack(**change); case.now = case.job.expires_us
                with self.assertRaises(ValueError): case.accept(received)
                self.assertIs(case.driver.active, case.job); self.assertEqual(case.capture.calls, [])
                self.assertEqual(case.driver.counts['expired'], 0)

    def test_foreign_original_peer_and_wrong_endpoint_ticket_still_raise_after_e(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                case = AckCase(self); received = case.ack(foreign=foreign)
                if not foreign:
                    s_other, a_other = case.links(case.a_peer)
                    s_other.endpoint.send(received.packet.raw)
                    received, = a_other.poll()
                case.now = case.job.expires_us
                with self.assertRaises(ValueError): case.accept(received)
                self.assertIs(case.driver.active, case.job)

    def test_changed_duplicate_ack_bytes_are_rejected_even_after_e(self):
        case = AckCase(self); case.capture.busy = True
        self.assertIsNone(case.accept(case.ack()))
        case.now += 10
        changed = case.ack()  # 同一identityでも新issued/sequenceは原ACK bytesと異なる。
        case.now = case.job.expires_us
        with self.assertRaisesRegex(ValueError, 'one original snapshot ACK'): case.accept(changed)
        self.assertEqual(len(case.capture.calls), 1)
        self.assertEqual(case.driver.counts['expired'], 0)

    def test_original_relay_order_is_checked_before_expiry_drop(self):
        case = AckCase(self); received = case.ack()
        case.driver._meta_sent = SimpleNamespace(started_us=received.received_j_us + 1)
        case.now = case.job.expires_us
        with self.assertRaisesRegex(ValueError, 'relay changed original actual event order'): case.accept(received)
        self.assertIs(case.driver.active, case.job)

    def test_matching_retired_ack_is_no_effect_and_expired_unmatched_job_cannot_enqueue(self):
        case = AckCase(self); received = case.ack(); case.now = case.job.expires_us
        with case.clock(): self.assertTrue(case.driver.expire())
        self.assertIsNone(case.accept(received))
        self.assertIsNone(case.driver.active)
        self.assertEqual(case.capture.calls,[])
        other = AckCase(self); received = other.ack(job_id=identifier(997))
        other.now = other.job.expires_us
        self.assertIsNone(other.accept(received))
        self.assertEqual(other.capture.calls,[])
        self.assertIs(other.driver.active,other.job)

    def test_busy_ack_then_expired_enqueue_retires_without_another_capture(self):
        case = AckCase(self); case.capture.busy = True
        self.assertIsNone(case.accept(case.ack())); self.assertTrue(case.driver.waiting_enqueue)
        case.now = case.job.expires_us - 1
        with case.clock(): self.assertIsNone(case.driver.enqueue())
        self.assertEqual(len(case.capture.calls), 1)
        self.assertEqual(case.driver.phase, 'IDLE')

    def test_expiry_between_ack_validation_and_enqueue_does_not_call_capture(self):
        case = AckCase(self); received = case.ack()
        ticks = iter([case.now, case.job.expires_us, case.job.expires_us])
        with patch('xenova_vehicle.runtime.pc_acquisition.j_us', side_effect=lambda: next(ticks)):
            self.assertIsNone(case.driver.snapshot_ack(received))
        self.assertEqual(case.capture.calls, []); self.assertEqual(case.driver.phase, 'IDLE')

    def test_current_source_failure_and_sdk_capture_error_are_not_expiry_drops(self):
        for fenced in (False, True):
            with self.subTest(fenced=fenced):
                case = AckCase(self); received = case.ack()
                if fenced: case.owner.fence()
                else: case.capture.error = TimeoutError('original SDK send outcome remains a failure')
                with self.assertRaises((ValueError, TimeoutError)): case.accept(received)
                self.assertIs(case.driver.active, case.job)
                self.assertEqual(case.driver.counts['expired'], 0)

    def test_identity_only_never_weakens_common_current_validator_or_future_issue(self):
        case = AckCase(self); received = case.ack()
        from xenova_vehicle.runtime.pc_channel import decode_forward
        ack = PcSnapshotAck(decode_forward(received.packet, case.m_peer, case.binding).original_packet, case.binding)
        original = dict(job_id=case.job.job_id, expires_us=case.job.expires_us, full_key=case.job.full_key,
            pc_frame_proof_sha256=sha256(case.frame.proof_bytes).hexdigest(), purpose=case.job.purpose)
        self.assertIs(ack.validate_identity(**original, now_us=case.job.expires_us), ack)
        for now in (case.now - 1, case.job.expires_us - 1, case.job.expires_us):
            with self.subTest(now=now), self.assertRaises(ValueError): ack.validate_original(**original, now_us=now)
        with self.assertRaises(ValueError): ack.validate_identity(**original, now_us=case.now - 1)


if __name__ == '__main__': unittest.main()
