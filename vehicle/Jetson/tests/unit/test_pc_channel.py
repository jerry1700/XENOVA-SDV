"""PC private seqpacket의 실제 socketpair 회귀다. SDK/NN/Source/READY 수락은 아니다.

peer/descriptor/receipt는 명시 software leaf이며 app body의 의미를 대신 승인하지 않는다.
시간은 고정 단위 fixture다. PC↔Jetson 네트워크나 물리 장치/주행 성능은 검사하지 않는다.
"""

from array import array
from dataclasses import FrozenInstanceError, replace
import json
import os
import socket
import threading
import unittest
from uuid import UUID

from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.pc_channel import (
    HEADER_CAP, MAGIC, PACKET_CAP, PREFIX, PcActorLink, PcActorPacket, PcActorReceived,
    PcChannelError, decode_forward, encode_pc_actor_packet, forward_received, pc_actor_pair,
)
from xenova_vehicle.simulation.pc_input import PcSourceBinding


def uid(value):
    return str(UUID(int=value))


def binding():
    return PcSourceBinding(dict(profile='development-pc-source-1', service_instance=uid(1),
        source_id=uid(10), pc_clock_epoch=uid(11), jetson_session=uid(12),
        stream_instances=dict(rgb_video=uid(13), depth_right=uid(14)), destination_clock_epoch=uid(15),
        calibration_profile_sha256='c'*64, motion_profile_sha256='b'*64, nn_manifest_sha256='d'*64))


def peer(role):
    return LifecyclePeer(uid(1), uid(2) if role == 'A' else uid(4),
        uid(3) if role == 'A' else uid(5), role, 'e'*64 if role == 'A' else 'f'*64)


def changed_packet(packet, *, changes=None, body=None, magic=MAGIC, pretty=False):
    header = packet.header
    if changes:
        header.update(changes)
    encoded = json.dumps(header, sort_keys=True, ensure_ascii=True,
        separators=None if pretty else (',', ':'), indent=1 if pretty else None).encode('ascii')
    body = packet.body if body is None else body
    return PREFIX.pack(magic, len(encoded), len(body)) + encoded + body


class PcChannelTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000
        self.binding = binding()

    def links(self, role='A'):
        left, right = pc_actor_pair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        registered = peer(role)
        supervisor = PcActorLink(left, registered, 'S', self.binding, clock=lambda: self.now)
        worker = PcActorLink(right, registered, role, self.binding, clock=lambda: self.now)
        self.addCleanup(supervisor.close); self.addCleanup(worker.close)
        return supervisor, worker

    def packet(self, role='A', kind=None, *, sequence=1, body=b'original control bytes', **kwargs):
        if kind is None:
            kind = 'PC_NN_START' if role == 'A' else 'PC_PARSE_COMPLETE'
        values = dict(sequence=sequence, request_id=uid(100), issued_us=self.now, expires_us=self.now+100_000)
        values.update(kwargs)
        return encode_pc_actor_packet(peer(role), self.binding, role, kind, dict(job_id=uid(101)), body, **values)

    def test_codec_preserves_owned_bytes_and_only_role_specific_control_kinds(self):
        for role, kinds in (('A', ('PC_NN_START', 'PC_EARLY_META', 'PC_NN_RESULT_META', 'PC_SOURCE_FENCE')),
                            ('M', ('PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE', 'PC_SOURCE_FENCE'))):
            for kind in kinds:
                packet = self.packet(role, kind)
                self.assertEqual(PcActorPacket(packet.raw).raw, packet.raw)
                self.assertEqual(packet.body, b'original control bytes')
                self.assertEqual((packet.kind, packet.sequence, packet.request_id), (kind, 1, uid(100)))
                copy = packet.header; copy['metadata'].clear(); copy['expires_j_us'] = '0'
                self.assertEqual(packet.metadata, dict(job_id=uid(101)))
                self.assertEqual(packet.expires_us, self.now+100_000)
                with self.assertRaises(FrozenInstanceError):
                    packet.body = b'changed'
        for role, kind in (('A', 'PC_PARSE_COMPLETE'), ('M', 'PC_EARLY_META'), ('A', 'WORLD_CANDIDATE')):
            with self.assertRaises(PcChannelError):
                self.packet(role, kind)
        with self.assertRaises(PcChannelError):
            encode_pc_actor_packet(peer('A'), self.binding, 'S', 'PC_NN_START', {}, b'receipt',
                sequence=1, request_id=uid(100), issued_us=self.now, expires_us=self.now+1)
        with self.assertRaises(PcChannelError):
            self.packet(body=b'')

    def test_codec_rejects_corrupt_noncanonical_oversize_and_non_native_headers(self):
        packet = self.packet()
        for changes in ({'profile': 'pc-actor-channel-2'}, {'sequence': True}, {'sequence': '0'},
                {'issued_j_us': '02000000'}, {'expires_j_us': str(self.now+5_000_001)},
                {'body_sha256': '0'*64}, {'body_bytes': True}, {'unknown': 1},
                {'metadata': {'nan': float('nan')}}, {'forward_received_j_us': str(self.now)}):
            with self.subTest(changes=changes), self.assertRaises(PcChannelError):
                PcActorPacket(changed_packet(packet, changes=changes))
        for raw in (changed_packet(packet, magic=b'XNVRAW1\0'), changed_packet(packet, body=b'corrupt'),
                    changed_packet(packet, pretty=True), packet.raw[:-1], packet.raw+b'x'):
            with self.assertRaises(PcChannelError):
                PcActorPacket(raw)
        header = packet._header_bytes.replace(b'"sequence":"1"', b'"sequence":"1","sequence":"1"')
        with self.assertRaises(PcChannelError):
            PcActorPacket(PREFIX.pack(MAGIC, len(header), len(packet.body)) + header + packet.body)
        for metadata in ({'padding': 'x'*HEADER_CAP}, {'callback': lambda: self.fail('must not execute')}):
            with self.assertRaises(PcChannelError):
                encode_pc_actor_packet(peer('A'), self.binding, 'A', 'PC_EARLY_META', metadata,
                    sequence=1, request_id=uid(100), issued_us=self.now, expires_us=self.now+1)
        with self.assertRaises(PcChannelError):
            self.packet(sequence=True)
        with self.assertRaises(PcChannelError):
            self.packet(issued_us=True)

    def test_real_fullsend_and_fullreceive_are_distinct_from_queue_and_keep_original_time(self):
        supervisor, worker = self.links()
        original = worker.send('PC_NN_START', {'manifest': 'd'*64}, b'original receipt bytes',
            request_id=uid(100), issued_us=self.now, expires_us=self.now+100_000)
        self.assertIs(worker.pending, original)
        with self.assertRaises(BlockingIOError):
            supervisor.endpoint.recv(PACKET_CAP+1)
        sent = worker.flush()
        self.assertIs(sent.packet, original)
        self.assertEqual((sent.started_us, sent.completed_us), (self.now, self.now))
        self.assertIs(worker.require_sent(sent), sent)
        with self.assertRaises(PcChannelError): supervisor.require_sent(sent)
        self.assertIsNone(worker.pending)
        self.now += 17
        received, = supervisor.poll()
        self.assertEqual(received.packet.raw, original.raw)
        self.assertEqual(received.received_j_us, self.now)
        self.assertEqual(received.packet.issued_us, self.now-17)
        self.assertIs(supervisor.require_received(received), received)
        with self.assertRaises(PcChannelError): worker.require_received(received)
        with self.assertRaises(TypeError):
            PcActorReceived(original, self.now)
        with self.assertRaises(FrozenInstanceError):
            received.received_j_us = self.now+1
        self.assertEqual(supervisor.poll(), ())

    def test_expired_receive_burns_original_hwm_and_never_relabels_old_or_changed_replays(self):
        supervisor, worker = self.links()
        expired = self.packet(sequence=2, expires_us=self.now+1)
        worker.endpoint.send(expired.raw)
        self.now += 1
        self.assertEqual(supervisor.poll(), ())
        self.assertEqual(supervisor.counts['expired'], 1)
        worker.endpoint.send(expired.raw)
        self.assertEqual(supervisor.poll(), ())
        self.assertEqual(supervisor.counts['duplicate_no_effect'], 1)
        worker.endpoint.send(self.packet(sequence=1).raw)
        with self.assertRaises(PcChannelError):
            supervisor.poll()
        with self.assertRaises(PcChannelError):
            supervisor.poll()
        for changed in (False, True):
            receiver, sender = self.links('M')
            current = self.packet('M')
            sender.endpoint.send(current.raw); self.assertEqual(len(receiver.poll()), 1)
            replay = self.packet('M', body=b'changed') if changed else current
            sender.endpoint.send(replay.raw)
            if changed:
                with self.assertRaises(PcChannelError):
                    receiver.poll()
            else:
                self.assertEqual(receiver.poll(), ())

    def test_current_registered_release_worker_channel_binding_and_future_time_are_required(self):
        for change in ({'release_sha256': 'a'*64}, {'service_instance': uid(99)}, {'channel_epoch': uid(99)},
                {'worker_instance': uid(99)}, {'pc_binding_sha256': 'a'*64}, {'sender': 'S'},
                {'issued_j_us': str(self.now+1), 'expires_j_us': str(self.now+2)}):
            supervisor, worker = self.links()
            worker.endpoint.send(changed_packet(self.packet(), changes=change))
            with self.subTest(change=change), self.assertRaises(PcChannelError):
                supervisor.poll()
        receiver, sender = self.links()
        sender.endpoint.send(b'{"kind":"BIND"}')
        with self.assertRaises(PcChannelError):
            receiver.poll()
        for field in ('peer', 'binding', 'owner_role'):
            with self.assertRaises(AttributeError):
                setattr(receiver, field, object())

    def test_poll_is_bounded_to_eight_and_eof_does_not_forge_completion(self):
        supervisor, worker = self.links()
        for seq in range(1, 11):
            worker.endpoint.send(self.packet(sequence=seq).raw)
        first, second = supervisor.poll(), supervisor.poll()
        self.assertEqual([v.packet.sequence for v in first], list(range(1, 9)))
        self.assertEqual([v.packet.sequence for v in second], [9, 10])
        worker.close()
        with self.assertRaises(EOFError):
            supervisor.poll()
        self.assertEqual(first[0].packet.sequence, 1)

    def test_datagram_truncation_ancillary_and_peer_close_are_explicit_faults(self):
        supervisor, worker = self.links()
        worker.endpoint.send(b'x'*(PACKET_CAP+2))
        with self.assertRaises(PcChannelError):
            supervisor.poll()
        supervisor, worker = self.links()
        read_fd, write_fd = os.pipe()
        try:
            worker.endpoint.sendmsg([self.packet().raw],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array('i', [read_fd]))])
            with self.assertRaises(PcChannelError):
                supervisor.poll()
        finally:
            os.close(read_fd); os.close(write_fd)
        supervisor, worker = self.links()
        worker.close()
        packet = supervisor.send('PC_SOURCE_FENCE', {}, request_id=uid(100),
            issued_us=self.now, expires_us=self.now+100_000)
        with self.assertRaises(OSError):
            supervisor.flush()
        self.assertIs(supervisor.pending, packet)
        with self.assertRaises(PcChannelError):
            supervisor.flush()
        self.assertIs(supervisor.abort(), packet)
        left, right = pc_actor_pair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        stamps = iter((self.now, self.now+5, self.now+4))
        sender = PcActorLink(left, peer('A'), 'S', self.binding, clock=lambda: next(stamps))
        self.addCleanup(sender.close)
        original = sender.send('PC_SOURCE_FENCE', {}, request_id=uid(102),
            issued_us=self.now, expires_us=self.now+100_000)
        with self.assertRaises(PcChannelError):
            sender.flush()
        self.assertEqual(right.recv(PACKET_CAP+1), original.raw)
        self.assertIs(sender.pending, original)
        with self.assertRaises(PcChannelError):
            sender.flush()

    def test_actual_eagain_busy_and_expiry_keep_original_pending_sequence_request_and_deadline(self):
        supervisor, worker = self.links()
        blocked = False
        filler = b'x'*PACKET_CAP
        for _ in range(1024):
            try:
                supervisor.endpoint.send(filler)
            except BlockingIOError:
                blocked = True
                break
        self.assertTrue(blocked, 'bounded real send queue must become busy')
        original = supervisor.send('PC_SOURCE_FENCE', {'event': 'original'}, request_id=uid(100),
            issued_us=self.now, expires_us=self.now+100_000)
        self.assertIsNone(supervisor.flush())
        self.assertIsNone(supervisor.send('PC_SOURCE_FENCE', {'event': 'changed'}, request_id=uid(101),
            issued_us=self.now+1, expires_us=self.now+200_000))
        self.assertIs(supervisor.pending, original)
        for _ in range(1024):
            try:
                worker.endpoint.recv(PACKET_CAP+1)
            except BlockingIOError:
                break
        else:
            self.fail('bounded kernel queue did not drain')
        self.assertIs(supervisor.flush().packet, original)
        worker.poll()
        second = supervisor.send('PC_SOURCE_FENCE', {}, request_id=uid(102), issued_us=self.now,
            expires_us=self.now+10)
        self.assertEqual((original.sequence, second.sequence), (1, 2))
        self.now += 10
        with self.assertRaises(TimeoutError):
            supervisor.flush()
        self.assertIs(supervisor.pending, second)
        self.assertIsNone(supervisor.send('PC_SOURCE_FENCE', {}, request_id=uid(103),
            issued_us=self.now, expires_us=self.now+100_000))
        self.assertIs(supervisor.abort(), second)

    def test_forward_preserves_actual_actor_raw_receive_j_and_requires_independent_origin_registration(self):
        for origin_role, target_role, kind in (('A', 'M', 'PC_NN_START'), ('M', 'A', 'PC_PARSE_COMPLETE')):
            origin_s, origin = self.links(origin_role)
            target_s, target = self.links(target_role)
            original = origin.send(kind, {'job_id': uid(101)}, b'original app bytes', request_id=uid(100),
                issued_us=self.now, expires_us=self.now+100_000)
            origin.flush(); self.now += 3
            received, = origin_s.poll()
            forwarded = forward_received(received, target_s, origin_link=origin_s)
            self.assertEqual((forwarded.kind, forwarded.request_id, forwarded.issued_us, forwarded.expires_us),
                ('PC_FORWARD', original.request_id, original.issued_us, original.expires_us))
            self.assertEqual(forwarded.body, original.raw)
            target_s.flush(); outer, = target.poll()
            decoded = decode_forward(outer.packet, peer(origin_role), self.binding)
            self.assertEqual(decoded.original_packet.raw, original.raw)
            self.assertEqual(decoded.original_packet.body, b'original app bytes')
            self.assertEqual(decoded.received_j_us, received.received_j_us)
            for expected in (replace(peer(origin_role), release_sha256='0'*64), peer(target_role), object()):
                with self.assertRaises(PcChannelError):
                    decode_forward(outer.packet, expected, self.binding)
            with self.assertRaises(PcChannelError):
                target_s.send('PC_FORWARD', {}, original.raw, request_id=uid(100),
                    issued_us=original.issued_us, expires_us=original.expires_us)
            origin_s.close()
            with self.assertRaises(EOFError):
                forward_received(received, target_s, origin_link=origin_s)

    def test_expired_m_work_discard_keeps_original_sequence_burn_and_creates_no_send_fact(self):
        for kind in ('PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE'):
            with self.subTest(kind=kind):
                supervisor, worker = self.links('M')
                original = worker.send(kind, {}, request_id=uid(100),
                    issued_us=self.now, expires_us=self.now+10)
                raw = original.raw
                with self.assertRaises(PcChannelError):
                    worker.discard_expired_work(original)
                self.assertIs(worker.pending, original)
                self.now += 10
                with self.assertRaises(PcChannelError):
                    worker.discard_expired_work(PcActorPacket(raw))
                self.assertIs(worker.pending, original)
                self.assertIs(worker.discard_expired_work(original), original)
                self.assertIsNone(worker.pending)
                self.assertEqual(worker.counts['expired_work_discarded'], 1)
                self.assertEqual(worker.counts['sent'], 0)
                self.assertEqual(supervisor.poll(), ())
                self.assertEqual(original.raw, raw)
                next_packet = worker.send(kind, {}, request_id=uid(101),
                    issued_us=self.now, expires_us=self.now+100_000)
                self.assertEqual((original.sequence, next_packet.sequence), (1, 2))
                self.assertIs(worker.require_sent(worker.flush()).packet, next_packet)
                received, = supervisor.poll()
                self.assertEqual(received.packet.raw, next_packet.raw)
                with self.assertRaises(PcChannelError):
                    worker.discard_expired_work(original)

    def test_expired_work_discard_refuses_unknown_send_other_roles_and_control_kinds(self):
        for role, kind in (('M', 'PC_SOURCE_FENCE'), ('A', 'PC_EARLY_META'),
                ('A', 'PC_NN_RESULT_META'), ('A', 'PC_NN_START')):
            with self.subTest(role=role, kind=kind):
                _, worker = self.links(role)
                original = worker.send(kind, {}, b'original metadata', request_id=uid(100),
                    issued_us=self.now, expires_us=self.now+10)
                self.now += 10
                with self.assertRaises(PcChannelError):
                    worker.discard_expired_work(original)
                self.assertIs(worker.pending, original)
                self.assertEqual(worker.counts['expired_work_discarded'], 0)
        supervisor, worker = self.links('M')
        original = worker.send('PC_PARSE_COMPLETE', {}, request_id=uid(100),
            issued_us=self.now, expires_us=self.now+10)
        supervisor.close()
        with self.assertRaises(OSError):
            worker.flush()
        self.now += 10
        with self.assertRaises(PcChannelError):
            worker.discard_expired_work(original)
        self.assertIs(worker.pending, original)
        self.assertEqual(worker.counts['expired_work_discarded'], 0)
        self.assertEqual(worker.counts['sent'], 0)

    def test_expired_original_s_frame_relay_is_retired_without_sending_or_resetting_source_state(self):
        origin_s, origin = self.links()
        target_s, target = self.links('M')
        origin.send('PC_EARLY_META', {}, b'original descriptor fixture', request_id=uid(110),
            issued_us=self.now, expires_us=self.now+1)
        origin.flush(); received, = origin_s.poll()
        original = forward_received(received, target_s, origin_link=origin_s)
        self.now += 1
        self.assertIs(target_s.discard_expired_work(original), original)
        self.assertIsNone(target_s.pending)
        self.assertEqual(target.poll(), ())
        self.assertEqual(target_s.counts['sent'], 0)
        state = target_s.send('PC_SOURCE_STATE', {}, request_id=uid(111),
            issued_us=self.now, expires_us=self.now+1)
        self.assertEqual(state.sequence, original.sequence+1)
        self.now += 1
        with self.assertRaises(PcChannelError): target_s.discard_expired_work(state)
        self.assertIs(target_s.pending, state)

    def test_max_packet_relay_rejects_extra_extent_and_owner_thread_cannot_be_bypassed(self):
        origin_s, origin = self.links()
        target_s, target = self.links('M')
        sample = self.packet(body=b'x'*30_000)
        size = PACKET_CAP - (len(sample.raw)-len(sample.body))
        maximum = self.packet(body=b'x'*size)
        self.assertEqual(len(maximum.raw), PACKET_CAP)
        with self.assertRaises(PcChannelError):
            self.packet(body=b'x'*(size+1))
        origin.endpoint.send(maximum.raw); received, = origin_s.poll()
        with self.assertRaises(PcChannelError):
            forward_received(received, target_s, origin_link=origin_s)
        self.assertIsNone(target_s.pending)
        original = target_s.send('PC_SOURCE_FENCE', {}, request_id=uid(102),
            issued_us=self.now, expires_us=self.now+100_000)
        self.assertEqual(original.sequence, 1)
        outcomes = []
        def foreign_owner():
            outcomes.append(received.packet.raw == maximum.raw)
            for operation in (origin_s.poll, origin_s.flush, lambda: origin_s.pending, origin_s.close):
                try:
                    operation()
                except RuntimeError:
                    outcomes.append(True)
                else:
                    outcomes.append(False)
        thread = threading.Thread(target=foreign_owner)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [True]*5)
        self.assertEqual(received.packet.raw, maximum.raw)
