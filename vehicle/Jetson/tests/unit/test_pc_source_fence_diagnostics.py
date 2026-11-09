"""실제 PC 수신 ticket의 fence 사유와 기존 S 거절 경계를 검사한다.

고정 software clock과 socketpair만 사용한다. S 생성·worker 등록·Device·NN·DB를
시작하지 않으며, owner sentinel은 dispatch의 존재 검사만 위한 값이다.
"""

import os
import socket
import unittest
from unittest.mock import patch

import test_pc_channel as channel_fixture
from xenova_vehicle.runtime import pc_resident_runtime, resident_runtime
from xenova_vehicle.runtime.pc_channel import PcActorLink, PcChannelError, pc_actor_pair


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
        runtime._pc_owner = object()
        runtime._pc_links = {role: supervisor}
        runtime.events = []
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

    def test_a_and_m_fence_record_original_metadata_before_same_terminal_error(self):
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

                with self.assertRaisesRegex(RuntimeError, '^PC_'+role+'_SOURCE_FENCE$'):
                    runtime._pc_message(role, received)

                self.assertEqual(runtime.events, [dict(event='PC_SOURCE_FENCE',
                    observed_j_us=str(self.now), role=role, metadata=original)])
                runtime.events[0]['metadata']['reason'] = 'changed diagnostic copy'
                self.assertEqual(packet.metadata, original)
                self.assertEqual((packet.raw, packet.expires_us, packet.request_id, packet.sequence),
                    (raw, expires, request, sequence))

    def test_missing_owner_and_exact_expiry_keep_original_timeout_without_event(self):
        for role in ('A', 'M'):
            with self.subTest(role=role):
                supervisor, received = self.receive_fence(role, dict(reason='DURATION', failure=None))
                runtime = self.runtime(role, supervisor)
                runtime._pc_owner = None
                with self.assertRaisesRegex(TimeoutError,
                        '^PC actor message has no current original source owner/lifetime$'):
                    runtime._pc_message(role, received)
                self.assertEqual(runtime.events, [])

                runtime._pc_owner = object()
                self.now = received.packet.expires_us
                with self.assertRaisesRegex(TimeoutError,
                        '^PC actor message has no current original source owner/lifetime$'):
                    runtime._pc_message(role, received)
                self.assertEqual(runtime.events, [])
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
