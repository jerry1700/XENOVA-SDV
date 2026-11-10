"""원 PC fence 사유와 A loop의 유한 진단·기존 정리 경계를 검사한다.

고정 software clock과 socketpair만 사용한다. S 생성·worker 등록·Device·NN·DB를
시작하지 않으며, owner·출력 경계는 원 관측 철회 호출을 기록하는 대역이다.
"""

from dataclasses import replace
import json
import os
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_pc_channel as channel_fixture
import test_pc_acquisition_spawn as spawn_fixture
from tools.hil_observation import acquisition
from xenova_vehicle.runtime import pc_resident_runtime, resident_runtime
from xenova_vehicle.runtime import pc_workers
from xenova_vehicle.runtime.native_resources import ArenaSpec
from xenova_vehicle.runtime.pc_channel import PcActorLink, PcChannelError, pc_actor_pair
from xenova_vehicle.runtime.raw_leases import ARENA_BYTES, Endpoint, LeaseLedgerError, LeaseRequest, RawScope
from xenova_vehicle.runtime.resident_workers import _StopWork
from xenova_vehicle.simulation.pc_input import PcSourceBinding


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SOCK_SEQPACKET'),
    'actual PC actor seqpacket transport requires POSIX')
class PcSourceFenceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000
        self.binding = channel_fixture.binding()
        for module in (pc_resident_runtime, resident_runtime):
            clock = patch.object(module, 'j_us', lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)

    def links(self, role):
        left, right = pc_actor_pair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        peer = channel_fixture.peer(role)
        supervisor = PcActorLink(left, peer, 'S', self.binding, clock=lambda: self.now)
        worker = PcActorLink(right, peer, role, self.binding, clock=lambda: self.now)
        self.addCleanup(supervisor.close)
        self.addCleanup(worker.close)
        return supervisor, worker

    def runtime(self, role, supervisor):
        # 실제 S 생성의 자원 부작용 없이 원 dispatch와 inherited event만 호출한다.
        runtime = object.__new__(pc_resident_runtime.PcResidentRuntime)
        runtime._pc_owner = SimpleNamespace(fence=Mock())
        runtime._pc_links = {role: supervisor}
        runtime.events = []
        runtime.broker = SimpleNamespace(set_publication=Mock())
        runtime.jio = SimpleNamespace(set_world_dispatch_enabled=Mock())
        runtime._queue_forward = Mock()
        return runtime

    def receive_fence(self, role, metadata):
        supervisor, worker = self.links(role)
        packet = worker.send('PC_SOURCE_FENCE', metadata,
            request_id=channel_fixture.uid(100), issued_us=self.now,
            expires_us=self.now+100_000)
        self.assertIs(worker.flush().packet, packet)
        received, = supervisor.poll()
        self.assertEqual(received.packet.raw, packet.raw)
        return supervisor, received

    def test_a_and_m_fence_record_original_metadata_and_keep_runtime_resident(self):
        for role, failure in (('A', 'stream receive failed: ConnectionResetError'), ('M', None)):
            with self.subTest(role=role):
                original = dict(reason='TransportError' if failure else 'DURATION', failure=failure)
                metadata = dict(original)
                supervisor, received = self.receive_fence(role, metadata)
                packet = received.packet
                raw, expires, request, sequence = packet.raw, packet.expires_us, packet.request_id, packet.sequence
                metadata['reason'] = 'changed after original send'
                packet.header['metadata']['failure'] = 'changed decoded copy'
                runtime = self.runtime(role, supervisor)

                runtime._pc_message(role, received)

                self.assertEqual(runtime.events, [dict(event='PC_SOURCE_FENCE',
                    observed_j_us=str(self.now), role=role, metadata=original),
                    dict(event='PC_INPUT_UNAVAILABLE', observed_j_us=str(self.now), role=role)])
                runtime._pc_owner.fence.assert_called_once_with('PC_INPUT_UNAVAILABLE')
                runtime.broker.set_publication.assert_called_once_with(None)
                runtime.jio.set_world_dispatch_enabled.assert_called_once_with(False)
                runtime._queue_forward.assert_called_once_with(received, 'M' if role == 'A' else 'A')
                self.assertTrue(runtime._pc_input_unavailable)
                runtime.events[0]['metadata']['reason'] = 'changed diagnostic copy'
                self.assertEqual(packet.metadata, original)
                self.assertEqual((packet.raw, packet.expires_us, packet.request_id, packet.sequence),
                    (raw, expires, request, sequence))

    def test_missing_owner_rejected_but_expired_fence_keeps_original_cleanup_fact(self):
        for role in ('A', 'M'):
            with self.subTest(role=role):
                supervisor, received = self.receive_fence(role, dict(reason='DURATION', failure=None))
                runtime = self.runtime(role, supervisor)
                owner = runtime._pc_owner
                runtime._pc_owner = None
                with self.assertRaisesRegex(TimeoutError,
                        '^PC actor message has no current original source owner/lifetime$'):
                    runtime._pc_message(role, received)
                self.assertEqual(runtime.events, [])

                runtime._pc_owner = owner
                self.now = received.packet.expires_us
                runtime._pc_message(role, received)
                owner.fence.assert_called_once_with('PC_INPUT_UNAVAILABLE')
                self.assertEqual(runtime.events[0]['event'], 'PC_SOURCE_FENCE')
                self.assertEqual(received.packet.expires_us, self.now)
                self.assertLess(received.received_j_us, received.packet.expires_us)

    def test_foreign_actual_receive_ticket_is_rejected_before_diagnostic(self):
        for role in ('A', 'M'):
            with self.subTest(role=role):
                _, foreign = self.receive_fence(role, dict(reason='DURATION', failure=None))
                supervisor, _ = self.links(role)
                runtime = self.runtime(role, supervisor)
                with self.assertRaisesRegex(PcChannelError,
                        '^actual receive fact belongs to another PC endpoint$'):
                    runtime._pc_message(role, foreign)
                self.assertEqual(runtime.events, [])


class PcAcquisitionDiagnosticsTests(unittest.TestCase):
    """원 loop/종료 호출의 software 대역 회귀다. SDK·등록·Source·SHM 수락은 아니다."""

    def setUp(self):
        self.now = 2_000_000
        clock = patch.object(acquisition, 'j_us', lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_stage_timing_preserves_return_arguments_exception_and_original_expiry(self):
        diagnostic = acquisition._AcquisitionDiagnostic()
        token = object()
        request = LeaseRequest(channel_fixture.uid(90), 10, 20)

        def work(original, *, expires_us):
            self.assertIs(original, token)
            self.assertEqual(expires_us, request.expires_us)
            self.now += 7
            return token

        for stage in diagnostic._STAGES:
            self.assertIs(diagnostic.call(stage, work, token, expires_us=20), token)
        self.assertTrue(all(row == dict(calls=1, errors=0, max_duration_us=7)
                            for row in diagnostic.stages.values()))
        def expired():
            self.now += 35
            request.check(20)
        with self.assertRaisesRegex(LeaseLedgerError, 'original half-open lifetime'):
            diagnostic.call('raw_publish', expired)
        self.assertEqual((request.issued_us, request.expires_us), (10, 20))
        self.assertEqual(diagnostic.stages['raw_publish'], dict(calls=2, errors=1, max_duration_us=35))

    def pump(self):
        class Payload:
            def __repr__(self):
                raise AssertionError('payload must not be formatted')
        session = SimpleNamespace(
            sender=SimpleNamespace(_active=(Payload(), 2_100_000), _pending=None, _part=2, _offset=123,
                stats=dict(sent_bytes=456, sent_packets=1)),
            receiver=SimpleNamespace(partial_bytes=789, deadline_us=2_120_000,
                stats=dict(received_bytes=789, received_packets=0)),
            send_end=2_100_000, waiting=SimpleNamespace(expires_j_us=2_130_000),
            frame=Payload(), clock_owner=Payload())
        return SimpleNamespace(_session=session, result=None, pc_owner=object(), close=Mock())

    def test_last_pump_positions_and_copy_metadata_survive_original_fence_clearing(self):
        diagnostic = acquisition._AcquisitionDiagnostic()
        pump = self.pump()
        pump.step = Mock(return_value=None)
        self.assertIsNone(diagnostic.step(pump))
        self.now += 200_001
        terminal = SimpleNamespace(reason='ObservationSessionError', failure='ORIGINAL_CLOCK_REPLY_TIMEOUT',
            counts=dict(replies_received=3), lease_evidence=object())
        def finish():
            pump.result = terminal
            pump._session.receiver.partial_bytes = 0
            pump._session.receiver.deadline_us = None
            pump._session.waiting = None
            return terminal
        pump.step = finish
        self.assertIs(diagnostic.step(pump), terminal)
        diagnostic.pending_copy(2_000_100, 2_150_100, 'NN', 2_400_000, selected=True)
        actor = SimpleNamespace(state='DEGRADED', leases_released=False, save=Mock())
        diagnostic.capture_exit(actor, pump, SimpleNamespace(phase='IDLE', active=None), source_fenced=True,
            request_draft=None, metadata_draft=None, start_draft=None)
        diagnostic.save_once(actor)
        diagnostic.save_once(actor)
        actor.save.assert_called_once()
        filename, value = actor.save.call_args.args
        self.assertEqual(filename, 'pc-acquisition-diagnostic.json')
        self.assertEqual(value['pump_max_call_gap_us'], 200_001)
        self.assertEqual(value['positions_before_last_pump']['receiver_partial_bytes'], 789)
        self.assertEqual(value['positions_before_last_pump']['clock_probe_E'], 2_130_000)
        self.assertEqual(value['exit']['transport_at_loop_exit']['positions']['receiver_partial_bytes'], 0)
        self.assertEqual(value['exit']['copy_request'], dict(issued_us=2_000_100, expires_us=2_150_100,
            phase='NN', frame_expires_us=2_400_000, selected=True))
        self.assertFalse(value['exit']['leases_released'])
        self.assertEqual(value['exit']['pump_result_before_cleanup'], dict(reason=terminal.reason,
            failure=terminal.failure, counters=terminal.counts))
        raw = json.dumps(value).encode('ascii')
        self.assertLessEqual(len(raw), 16*1024)
        self.assertNotIn('lease_evidence', value['exit']['pump_result_before_cleanup'])
        self.assertNotIn(b'Payload', raw)

    def test_oversize_diagnostic_is_not_written_and_stage_count_stays_fixed(self):
        diagnostic = acquisition._AcquisitionDiagnostic()
        self.assertEqual(len(diagnostic.stages), 8)
        diagnostic.final = dict(actor_state='x' * 17000)
        actor = SimpleNamespace(save=Mock())
        diagnostic.save_once(actor)
        diagnostic.save_once(actor)
        actor.save.assert_not_called()

    def test_save_failure_preserves_original_stop_and_worker_device_close(self):
        ends = [socket.socketpair() for _ in range(2)]
        for pair in ends:
            for endpoint in pair:
                self.addCleanup(endpoint.close)
        settings, binding_bytes, manifest_bytes, profile, release = spawn_fixture._settings(ends[0][0], ends[1][0])
        binding = PcSourceBinding.from_bytes(binding_bytes)
        peer = replace(channel_fixture.peer('A'), release_sha256=release)
        writer = Endpoint(peer.channel_epoch, peer.worker_instance)
        arena = ArenaSpec(channel_fixture.uid(40), '/xnv_raw_'+channel_fixture.uid(40), ARENA_BYTES, 'RAW')
        scope = RawScope(peer.service_instance, arena.arena_id, writer, binding.sha256,
            arena.name, arena.size_bytes, arena.role)
        boot = pc_workers.PcWorkerBoot(peer, peer, profile, scope, arena, binding_bytes, manifest_bytes,
            ((None, None), (None, None)), self.now+5_000_000, 'software-only-no-diagnostic-write')
        actor = object.__new__(pc_workers.PcWorkerActor)
        actor.boot, actor.binding, actor.bind = boot, binding, object()
        actor.state, actor.stop, actor.leases_released = 'WARMUP', None, True
        actor._nn_start = SimpleNamespace(canonical_receipt_bytes=b'original-software-start-not-sent')
        actor.await_bind = actor.attach_arena = actor.start_model = Mock()
        actor.close_model, actor.finish, actor.fault = Mock(), Mock(), Mock()
        stop = _StopWork()
        def stopped():
            actor.stop = SimpleNamespace(expires_us=self.now+50_000)
            raise stop
        actor.poll = Mock(side_effect=stopped)
        actor.save = Mock(side_effect=OSError('diagnostic unavailable'))
        pump = self.pump()
        driver = SimpleNamespace(phase='IDLE', active=None, fence=Mock())
        link = SimpleNamespace(close=Mock())
        with patch.object(acquisition, 'PcActorLink', return_value=link), \
                patch.object(acquisition, 'JetsonObservationPump', return_value=pump), \
                patch.object(acquisition, 'PcAcquisitionDriver', return_value=driver), \
                patch.object(acquisition, 'PcWriterSlotContext', return_value=object()), \
                patch.object(acquisition.time, 'sleep'), \
                patch.object(pc_workers, 'PcWorkerActor', return_value=actor), \
                patch.object(pc_workers, 'j_us', lambda: self.now):
            pc_workers.pc_worker_main(Mock(), boot, lambda selected: acquisition.run_acquisition(selected, settings))
        actor.save.assert_called_once()
        self.assertEqual(actor.save.call_args.args[1]['stages']['actor_poll']['errors'], 1)
        self.assertEqual(actor.save.call_args.args[1]['exit']['pump_result_before_cleanup'], None)
        actor.fault.assert_not_called()
        actor.close_model.assert_called_once_with(actor.stop.expires_us)
        actor.finish.assert_called_once()
        pump.close.assert_called_once()
        driver.fence.assert_called_once()
        link.close.assert_called_once()
