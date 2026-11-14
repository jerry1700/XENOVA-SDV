"""PC BIND/ACK의 원 envelope·독립 descriptor·SHM 문맥 회귀다.

UUID/hash/ArenaSpec은 명시 software fixture다. 자원 생성/attach·실 peer 등록·SDK/NN
start·D bootstrap·송수신·Identity/READY 수락을 실행하거나 증명하지 않는다.
"""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import unittest
from uuid import UUID

from xenova_vehicle.runtime.development_source import DevelopmentSourceBinding, DevelopmentSourceError
from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.local_envelope import decode_structure
from xenova_vehicle.runtime.native_resources import ArenaSpec, DB_COPY_BYTES
from xenova_vehicle.runtime.pc_binding_messages import (
    PcBindAck, PcBindContext, PcBindMessage, PcBindMessageError, parse_pc_bind, parse_pc_bind_ack,
)
from xenova_vehicle.runtime.raw_binding import RawBinding, RawBindingError
from xenova_vehicle.runtime.raw_leases import ARENA_BYTES, Endpoint, RawScope
from xenova_vehicle.simulation.pc_input import PcSourceBinding


def uid(value):
    return str(UUID(int=value))


def descriptor_fields():
    return dict(profile='development-pc-source-1', service_instance=uid(1), source_id=uid(10),
        pc_clock_epoch=uid(11), jetson_session=uid(12),
        stream_instances=dict(rgb_video=uid(13), depth_right=uid(14)), destination_clock_epoch=uid(15),
        calibration_profile_sha256='c'*64, motion_profile_sha256='b'*64, nn_manifest_sha256='d'*64)


def context(role='A'):
    binding = PcSourceBinding(descriptor_fields())
    acquisition = Endpoint(uid(2), uid(3))
    peer = LifecyclePeer(uid(1), uid(2) if role == 'A' else uid(4),
        uid(3) if role == 'A' else uid(5), role, 'f'*64)
    spec = ArenaSpec(uid(40), '/xnv_raw_' + uid(40), ARENA_BYTES, 'RAW')
    scope = RawScope(uid(1), spec.arena_id, acquisition, binding.sha256,
        spec.name, spec.size_bytes, spec.role)
    return PcBindContext(peer, acquisition, binding, scope, spec)


def packet(fields, *, pretty=False):
    return decode_structure(json.dumps(fields, sort_keys=True, ensure_ascii=True,
        indent=2 if pretty else None, separators=None if pretty else (',', ':'), allow_nan=False).encode('ascii'))


def bind_fields(ctx, *, binding=None, issued=100, expires=5_000_100, revision=1, sequence=10):
    value = ctx.expected_binding if binding is None else binding
    return dict(version=1, service_instance=ctx.peer.service_instance, channel_epoch=ctx.peer.channel_epoch,
        sender_instance=ctx.peer.service_instance, message_sequence=str(sequence), kind='BIND', binding_domain='RAW',
        binding_revision=str(revision), request_id=uid(20), issued_j_us=str(issued), expires_j_us=str(expires),
        payload=dict(binding_revision=str(revision), binding_sha256=value.sha256, binding=value.fields))


def bind(ctx=None, **kwargs):
    ctx = context() if ctx is None else ctx
    return PcBindMessage(packet(bind_fields(ctx, **kwargs)), ctx)


def ack_fields(original, *, accepted=True, state=None, reason=None, issued=200, expires=None, sequence=1):
    peer = original.context.peer
    if state is None:
        state = 'WARMUP' if peer.role == 'A' else 'WAIT_INPUT'
    return dict(version=1, service_instance=peer.service_instance, channel_epoch=peer.channel_epoch,
        sender_instance=peer.worker_instance, message_sequence=str(sequence), kind='BIND_ACK', binding_domain='RAW',
        binding_revision=str(original.binding_revision), request_id=original.request_id, issued_j_us=str(issued),
        expires_j_us=str(original.expires_us if expires is None else expires),
        payload=dict(binding_revision=str(original.binding_revision), binding_sha256=original.binding.sha256,
            accepted=accepted, state=state, reason=reason))


class PcBindingMessageTests(unittest.TestCase):
    def test_both_targets_keep_original_packets_context_descriptor_and_independent_sequences(self):
        for role in ('A', 'M'):
            ctx = context(role)
            original_packet = packet(bind_fields(ctx), pretty=True)
            for factory in (PcBindMessage, parse_pc_bind):
                original = factory(original_packet, ctx, cap_us=5_000_100)
                response_packet = packet(ack_fields(original, sequence=1))
                for ack_factory in (PcBindAck, parse_pc_bind_ack):
                    response = ack_factory(response_packet, original)
                    self.assertIs(original.packet, original_packet)
                    self.assertIs(original.context, ctx)
                    self.assertIs(response.packet, response_packet)
                    self.assertIs(response.original_bind, original)
                    self.assertIs(response.binding, original.binding)
                    self.assertEqual(original.binding.canonical_bytes, ctx.expected_binding.canonical_bytes)
                    self.assertEqual((original.message_sequence, response.message_sequence), (10, 1))
                    self.assertEqual((original.issued_us, response.issued_us, response.expires_us), (100, 200, 5_000_100))
                    self.assertEqual(response.request_id, original.request_id)
                    self.assertTrue(response.accepted)
                    self.assertIs(original.raw, original_packet.raw_bytes)
                    self.assertIs(response.raw, response_packet.raw_bytes)
                    self.assertFalse(any(hasattr(response, key) for key in
                        ('registered', 'nn_ready', 'metric_ready', 'capture_ready', 'authorized', 'identity')))
                    copied = response.fields; copied['payload']['accepted'] = False
                    self.assertTrue(response.payload['accepted'])
                    with self.assertRaises(FrozenInstanceError):
                        response.accepted = False
            self.assertNotEqual(ctx.expected_binding.fields['source_id'], ctx.acquisition.worker_instance)
            self.assertEqual(ctx.scope.writer, ctx.acquisition)

    def test_context_requires_exact_writer_reader_service_descriptor_and_arena(self):
        for role in ('A', 'M'):
            ctx = context(role)
            for field in ('worker_instance', 'channel_epoch'):
                other = uid(99) if role == 'A' else getattr(ctx.peer, field)
                endpoint = replace(ctx.acquisition, **{field: other})
                with self.subTest(role=role, field=field), self.assertRaises(PcBindMessageError):
                    PcBindContext(ctx.peer, endpoint, ctx.expected_binding, ctx.scope, ctx.arena_spec)
            for peer in (replace(ctx.peer, role='D'), replace(ctx.peer, service_instance=uid(99)),
                         replace(ctx.peer, worker_instance=uid(0))):
                with self.assertRaises(PcBindMessageError):
                    PcBindContext(peer, ctx.acquisition, ctx.expected_binding, ctx.scope, ctx.arena_spec)
            for scope in (replace(ctx.scope, service_instance=uid(99)),
                    replace(ctx.scope, writer=Endpoint(uid(98), uid(99))),
                    replace(ctx.scope, raw_binding_sha256='e'*64), replace(ctx.scope, arena_id=uid(99)),
                    replace(ctx.scope, arena_name='/xnv_raw_' + uid(99))):
                with self.assertRaises(PcBindMessageError):
                    PcBindContext(ctx.peer, ctx.acquisition, ctx.expected_binding, scope, ctx.arena_spec)
            for spec in (ArenaSpec(uid(99), '/xnv_raw_' + uid(99), ARENA_BYTES, 'RAW'),
                         ArenaSpec(uid(40), '/xnv_db_' + uid(40), DB_COPY_BYTES, 'DB_COPY')):
                with self.assertRaises(PcBindMessageError):
                    PcBindContext(ctx.peer, ctx.acquisition, ctx.expected_binding, ctx.scope, spec)
            for name in ('peer', 'acquisition', 'expected_binding', 'scope', 'arena_spec'):
                values = dict(peer=ctx.peer, acquisition=ctx.acquisition, expected_binding=ctx.expected_binding,
                    scope=ctx.scope, arena_spec=ctx.arena_spec)
                values[name] = object()
                with self.subTest(type_field=name), self.assertRaises(PcBindMessageError):
                    PcBindContext(**values)

    def test_self_consistent_changed_descriptor_hash_cannot_replace_s_pins(self):
        ctx = context()
        changes = dict(service_instance=uid(90), source_id=uid(91), pc_clock_epoch=uid(92),
            jetson_session=uid(93), destination_clock_epoch=uid(94),
            calibration_profile_sha256='1'*64, motion_profile_sha256='2'*64, nn_manifest_sha256='3'*64)
        for name, value in changes.items():
            fields = descriptor_fields(); fields[name] = value
            changed = PcSourceBinding(fields)
            with self.subTest(field=name), self.assertRaises(PcBindMessageError):
                PcBindMessage(packet(bind_fields(ctx, binding=changed)), ctx)
        fields = descriptor_fields(); fields['stream_instances']['rgb_video'] = uid(95)
        with self.assertRaises(PcBindMessageError):
            PcBindMessage(packet(bind_fields(ctx, binding=PcSourceBinding(fields))), ctx)
        copied = ctx.expected_binding.fields; copied['stream_instances'].clear()
        self.assertEqual(ctx.expected_binding.fields, descriptor_fields())

    def test_bind_rejects_wrong_envelope_revision_hash_and_payload_without_fallback(self):
        ctx = context()
        base = bind_fields(ctx)
        for name, value in (('service_instance', uid(99)), ('channel_epoch', uid(99)),
                ('sender_instance', ctx.peer.worker_instance), ('kind', 'BIND_ACK'),
                ('binding_domain', 'PUBLICATION'), ('binding_revision', '0')):
            fields = deepcopy(base); fields[name] = value
            with self.subTest(envelope=name), self.assertRaises(PcBindMessageError):
                parse_pc_bind(packet(fields), ctx)
        for name, value in (('binding_revision', '2'), ('binding_sha256', 'e'*64), ('unknown', None)):
            fields = deepcopy(base); fields['payload'][name] = value
            with self.subTest(payload=name), self.assertRaises(PcBindMessageError):
                parse_pc_bind(packet(fields), ctx)
        fields = deepcopy(base); del fields['payload']['binding']
        with self.assertRaises(PcBindMessageError):
            parse_pc_bind(packet(fields), ctx)
        fields = deepcopy(base); fields['payload']['binding']['profile'] = 'development-owned-source-1'
        with self.assertRaises(PcBindMessageError):
            parse_pc_bind(packet(fields), ctx)
        with self.assertRaises(RawBindingError):
            RawBinding(ctx.expected_binding.fields)
        with self.assertRaises(DevelopmentSourceError):
            DevelopmentSourceBinding(ctx.expected_binding.fields)

    def test_original_five_second_external_caps_and_full_u64_edges_are_preserved(self):
        ctx = context()
        for issued, expires in ((100, 100), (101, 100), (100, 5_000_101)):
            with self.subTest(lifetime=(issued, expires)), self.assertRaises(PcBindMessageError):
                bind(ctx, issued=issued, expires=expires)
        original_packet = packet(bind_fields(ctx))
        for cap in (True, -1, 2**64, 5_000_099):
            with self.subTest(cap=cap), self.assertRaises(PcBindMessageError):
                PcBindMessage(original_packet, ctx, cap_us=cap)
        upper = 2**64 - 1
        original = bind(ctx, issued=upper-3, expires=upper, revision=upper, sequence=upper)
        response = PcBindAck(packet(ack_fields(original, issued=upper-2, expires=upper, sequence=upper)), original)
        self.assertEqual((original.binding_revision, original.message_sequence, response.expires_us), (upper, upper, upper))
        self.assertEqual(response.raw, response.packet.raw_bytes)

    def test_ack_matches_original_request_peer_revision_hash_and_nonextended_lifetime(self):
        for role in ('A', 'M'):
            original = bind(context(role)); base = ack_fields(original)
            for name, value in (('service_instance', uid(99)), ('channel_epoch', uid(99)),
                    ('sender_instance', original.context.peer.service_instance), ('request_id', uid(99)),
                    ('kind', 'BIND'), ('binding_domain', 'PUBLICATION'), ('binding_revision', '2'),
                    ('issued_j_us', '99'), ('expires_j_us', str(original.expires_us+1))):
                fields = deepcopy(base); fields[name] = value
                with self.subTest(role=role, field=name), self.assertRaises(PcBindMessageError):
                    parse_pc_bind_ack(packet(fields), original)
            for name, value in (('binding_revision', '2'), ('binding_sha256', 'e'*64), ('unknown', None)):
                fields = deepcopy(base); fields['payload'][name] = value
                with self.subTest(role=role, payload=name), self.assertRaises(PcBindMessageError):
                    parse_pc_bind_ack(packet(fields), original)
            for issued, expires in ((200, 200), (201, 200)):
                with self.assertRaises(PcBindMessageError):
                    PcBindAck(packet(ack_fields(original, issued=issued, expires=expires)), original)

    def test_ack_uses_existing_role_states_and_explicit_negative_reasons_only(self):
        for role, active, terminal in (('A', 'STREAMING', 'CLOSED'), ('M', 'RUNNING', 'STOPPED')):
            original = bind(context(role))
            self.assertTrue(PcBindAck(packet(ack_fields(original, state=active)), original).accepted)
            for reason in ('UNSUPPORTED_PROFILE', 'ASSET_UNAVAILABLE', 'LOCAL_SETUP_FAILED'):
                response = PcBindAck(packet(ack_fields(original, accepted=False, state=terminal, reason=reason)), original)
                self.assertFalse(response.accepted)
                self.assertIs(response.original_bind, original)
            for accepted, state, reason in ((1, active, None), (True, terminal, None),
                    (True, active, 'LOCAL_SETUP_FAILED'), (False, terminal, None),
                    (False, terminal, 'READY'), (False, 'NOT_A_STATE', 'LOCAL_SETUP_FAILED')):
                with self.subTest(role=role, value=(accepted, state, reason)), self.assertRaises(PcBindMessageError):
                    PcBindAck(packet(ack_fields(original, accepted=accepted, state=state, reason=reason)), original)

    def test_structural_caps_and_exact_factory_types_do_not_create_identity_or_ready(self):
        ctx = context(); original = bind(ctx)
        with self.assertRaises(PcBindMessageError):
            PcBindMessage(original.raw, ctx)
        with self.assertRaises(PcBindMessageError):
            PcBindAck(packet(ack_fields(original)), object())
        base = bind_fields(ctx)
        for name, value in (('message_sequence', '0'), ('binding_revision', True), ('issued_j_us', str(2**64))):
            fields = deepcopy(base); fields[name] = value
            with self.subTest(structural=name), self.assertRaises(ValueError):
                PcBindMessage(packet(fields), ctx)
        raw = json.dumps(base, sort_keys=True, separators=(',', ':')).encode('ascii')
        duplicate = raw.replace(b'"binding_revision":"1"', b'"binding_revision":"1","binding_revision":"1"', 1)
        with self.assertRaises(ValueError):
            decode_structure(duplicate)
        fields = deepcopy(base); fields['payload']['oversize'] = 'x'*4096
        with self.assertRaises(ValueError):
            packet(fields)
        from xenova_vehicle.runtime.data_channel import DataPeer
        from xenova_vehicle.runtime.source_identity import IdentityOwner, SourcePolicy
        policy = SourcePolicy('rc-sim-01', 'course-sim-1', 'a'*64, 'b'*64, 'c'*64)
        peer = DataPeer(ctx.peer.service_instance, uid(5), uid(4))
        with self.assertRaises(ValueError):
            IdentityOwner(peer, original.binding, policy, mapper_instance=uid(90))
