"""PC slot의 원 bytes/phase/NN layout 단위 시험이다. 등록·clock·SDK 수락은 아니다.

4시각과 capture/NN metadata는 명시 software fixture이며 실제 producer를 주장하지 않는다.
기존 YOLOP tensor fixture와 실제 typed clock/mapped factory를 재사용한다.
"""

from dataclasses import replace
from hashlib import sha256
import json
import unittest
from uuid import UUID
import zlib

import test_mapped_pc_observation as pc_fixtures
import test_yolop_frames as nn_fixtures
from tools.hil_observation.clock import ClockSource, ClockPolicy, HilObservationClock, reply
from tools.hil_observation.mapped import MappedPcOwner
from tools.hil_observation.transport import SessionScope, ObservationFrame, ReceivedPacket
from xenova_vehicle.oak.yolop_frames import NNInputStamp, MAX_NN_BYTES
from xenova_vehicle.simulation.pc_input import PcSourceBinding
from xenova_vehicle.runtime.raw_leases import RawScope, Endpoint, WriteKey, FilledInfo
from xenova_vehicle.runtime import pc_slot as codec


def identifier(value): return str(UUID(int=value))


def bundle(*, phase='EARLY', generation=1, now=1_100_110, arena_id=None, frame=None, context=None,
           nn_received=None, max_nn=False):
    if frame is None:
        binding = PcSourceBinding(pc_fixtures.binding_fields()); fields = binding.fields
        source = ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['jetson_session'])
        clocks = HilObservationClock(source, ClockPolicy(100, 100, 0, 1_000_000, 500_000))
        for sequence, j1 in ((1, now - 100_000), (2, now - 90_000)):
            probe = clocks.probe(sequence, sequence.to_bytes(16, 'big'), j1, j1 + 200)
            clocks.accept(reply(probe, j1 + 10, j1 + 11), j1 + 21)
        scope = SessionScope(fields['source_id'], fields['jetson_session'], binding.sha256,
            fields['calibration_profile_sha256'], fields['motion_profile_sha256'], fields['pc_clock_epoch'])
        packet = ObservationFrame(scope, 1, rgb_pc_interval=(now - 20_000, now - 19_998),
            depth_pc_interval=(now - 20_001, now - 19_997), render_completed_pc_us=now - 10_000,
            expires_pc_us=now + 479_999, rgb=pc_fixtures.RGB, depth=pc_fixtures.DEPTH)
        owner = MappedPcOwner(binding, scope, clocks, destination_clock_id=binding.clock_id)
        frame = owner.accept_frame(ReceivedPacket(packet, now - 10), now_j_us=now)
    if context is None:
        arena_id = identifier(900) if arena_id is None else arena_id
        scope = RawScope(frame.binding.fields['service_instance'], arena_id, Endpoint(identifier(901), identifier(902)),
            frame.binding.sha256, '/xnv_raw_' + arena_id)
        context = codec.PcSlotContext(WriteKey(scope, 0, generation, identifier(903 + generation)), frame.binding)
    inp = out = None
    if phase == 'NN':
        received = frame.frame_received_j_us + 1 if nn_received is None else nn_received
        inp = NNInputStamp('nn_input', frame.observation.rgb.stamp.sequence, 900_000, 1_000_000,
            received, 320, 320, 'BGR888p', frame.binding.fields['nn_manifest_sha256'])
        out = nn_fixtures.packet(sequence=inp.sequence, device_us=inp.device_us, sdk_us=inp.sdk_us,
            received_j_us=received + 1, context_key=inp.context_key,
            **(dict(payload=bytes(MAX_NN_BYTES)) if max_nn else {}))
    encoding = codec.PcSlotEncoding(context, phase=phase, job_id=identifier(920), expires_j_us=frame.expires_j_us,
        full_key=frame.observation.rgb.stamp.key, pc_frame_proof=frame.proof_bytes,
        pc_packet_header=frame.packet_header_bytes, rgb=frame.observation.rgb.payload,
        depth=frame.observation.depth.payload, nn_input=inp, nn_output=out)
    return frame, encoding, inp, out


def expected(encoding, *, completed=None):
    if completed is None:
        meta = encoding.descriptor()
        completed = max(meta['proof']['frame_received_j_us'], *(s['j4_us'] for s in meta['proof']['clock']['samples'])) + 3
    info = FilledInfo(encoding.context.write_key, encoding.payload_length, encoding.descriptor_sha256, completed)
    return codec.ExpectedPcSlot(encoding.context, info, encoding.job_id, encoding.phase, encoding.expires_j_us,
        encoding.full_key, sha256(encoding.pc_frame_proof).hexdigest())


def independent_record(encoding, metadata, payload):
    descriptor = json.dumps(metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    key = encoding.context.write_key
    header = codec.HEADER_STRUCT.pack(codec.MAGIC, 1, 128, len(descriptor), UUID(key.scope.arena_id).bytes,
        key.generation, UUID(key.write_token).bytes, bytes.fromhex(encoding.binding.sha256), len(payload),
        zlib.crc32(descriptor), zlib.crc32(payload), bytes(28))
    raw = header + descriptor + bytes(codec.MAX_DESCRIPTOR_BYTES - len(descriptor)) + payload
    original = expected(encoding)
    info = replace(original.original_filled_info, descriptor_sha256=sha256(descriptor).hexdigest(), payload_length=len(payload))
    return raw, replace(original, original_filled_info=info)


class PcSlotTests(unittest.TestCase):
    def test_early_owned_facts_keep_exact_proof_four_endpoints_job_e_and_payload(self):
        frame, encoding, _, _ = bundle(); wanted = expected(encoding)
        owned = codec.decode_owned(encoding.raw, wanted, codec.prefix(encoding.raw[:128], encoding.context))
        self.assertEqual((owned.phase, owned.job_id, owned.expires_j_us, owned.full_key),
            ('EARLY', encoding.job_id, frame.expires_j_us, frame.observation.rgb.stamp.key))
        self.assertEqual((owned.rgb, owned.depth), (frame.observation.rgb.payload, frame.observation.depth.payload))
        self.assertEqual(owned.pc_frame_proof, frame.proof_bytes)
        self.assertEqual(owned.pc_packet_header, frame.packet_header_bytes)
        self.assertEqual(owned.binding.canonical_bytes, frame.binding.canonical_bytes)
        self.assertIsNone(owned.nn_input); self.assertIsNone(owned.nn_output); self.assertIsNone(owned.nn_key)
        self.assertFalse(hasattr(owned, 'observation')); self.assertFalse(hasattr(owned, 'current_view'))
        metadata = owned.descriptor(); metadata['proof']['mapped_j_us'][0][0] += 1
        self.assertEqual(owned.pc_frame_proof, frame.proof_bytes)
        self.assertEqual(len(owned.raw), codec.METADATA_BYTES + 960000)
        self.assertLessEqual(len(owned.descriptor_bytes) + 128, 16 * 1024)

    def test_nn_roundtrip_preserves_real_dto_layout_metadata_and_max_buffer(self):
        frame, encoding, inp, out = bundle(phase='NN', generation=2, max_nn=True)
        owned = codec.decode_owned(encoding.raw, expected(encoding))
        self.assertEqual(owned.nn_input, inp)
        self.assertEqual(owned.nn_output.payload, out.payload)
        self.assertEqual(owned.nn_output.tensors, out.tensors)
        self.assertEqual((owned.nn_output.device_us, owned.nn_output.sdk_us, owned.nn_output.received_j_us),
            (out.device_us, out.sdk_us, out.received_j_us))
        self.assertEqual(owned.nn_key, frame.observation.rgb.stamp.key +
            (out.device_us, out.sdk_us, out.context_key, sha256(frame.observation.rgb.payload).hexdigest()))
        self.assertEqual((owned.pc_frame_proof, owned.expires_j_us), (frame.proof_bytes, frame.expires_j_us))
        self.assertEqual(encoding.payload_length, 960000 + MAX_NN_BYTES)
        self.assertLessEqual(len(encoding.raw), 2 * 1024 * 1024)

    def test_expected_job_phase_key_e_proof_and_generation_cannot_be_rebound(self):
        _, encoding, _, _ = bundle(); original = expected(encoding)
        changes = [dict(job_id=identifier(930)), dict(phase='NN'), dict(expires_j_us=encoding.expires_j_us + 1),
            dict(full_key=encoding.full_key[:3] + (2,)), dict(pc_frame_proof_sha256='f' * 64)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(codec.PcSlotError):
                codec.decode_owned(encoding.raw, replace(original, **change))
        wrong_context = replace(encoding.context, write_key=replace(encoding.context.write_key, generation=2))
        wrong_info = replace(original.original_filled_info, key=wrong_context.write_key)
        with self.assertRaises(codec.PcSlotError):
            codec.decode_owned(encoding.raw, replace(original, context=wrong_context, original_filled_info=wrong_info))

    def test_crc_used_extent_metadata_slack_physical_magic_and_prefix_cap_reject(self):
        _, encoding, _, _ = bundle(); wanted = expected(encoding)
        for offset in (0, 128, 16 * 1024, 128 + len(encoding.descriptor_bytes)):
            changed = bytearray(encoding.raw); changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(codec.PcSlotError):
                codec.decode_owned(bytes(changed), wanted)
        for raw in (encoding.raw[:-1], encoding.raw + b'\0', b'XNVRAW1\0' + encoding.raw[8:]):
            with self.assertRaises(codec.PcSlotError): codec.decode_owned(raw, wanted)
        header = list(codec.HEADER_STRUCT.unpack(encoding.raw[:128])); header[3] = codec.MAX_DESCRIPTOR_BYTES + 1
        with self.assertRaises(codec.PcSlotError): codec.prefix(codec.HEADER_STRUCT.pack(*header), encoding.context)
        with self.assertRaises(codec.PcSlotError): codec.decode_owned(memoryview(encoding.raw), wanted)

    def test_consistent_checksums_do_not_hide_nn_nonfinite_or_foreign_manifest(self):
        _, encoding, _, _ = bundle(phase='NN')
        metadata = encoding.descriptor(); payload = bytearray(encoding.raw[codec.METADATA_BYTES:])
        payload[960000:960002] = b'\x00\x7c'
        metadata['planes']['nn_output0']['sha256'] = sha256(payload[960000:]).hexdigest()
        raw, wanted = independent_record(encoding, metadata, bytes(payload))
        with self.assertRaises(codec.PcSlotError): codec.decode_owned(raw, wanted)
        metadata = encoding.descriptor(); metadata['nn']['input']['context_key'] = 'd' * 64
        metadata['nn']['output']['context_key'] = 'd' * 64
        raw, wanted = independent_record(encoding, metadata, encoding.raw[codec.METADATA_BYTES:])
        with self.assertRaises(codec.PcSlotError): codec.decode_owned(raw, wanted)

    def test_original_proof_source_and_plane_paths_cannot_be_changed_or_omitted(self):
        _, encoding, _, _ = bundle()
        for change in ('source', 'plane', 'unknown', 'nn'):
            metadata = encoding.descriptor()
            if change == 'source': metadata['proof']['packet_header']['scope']['source_id'] = identifier(999)
            elif change == 'plane': metadata['planes']['depth_right']['offset'] = codec.METADATA_BYTES
            elif change == 'unknown': metadata['fabricated_sdk_bridge_epoch'] = identifier(999)
            else: metadata['nn'] = {}
            raw, wanted = independent_record(encoding, metadata, encoding.raw[codec.METADATA_BYTES:])
            # fixture가 원 proofSHA를 바꾸어 주지 않아도 원 기대값 단계에서 먼저 거절한다.
            with self.subTest(change=change), self.assertRaises(codec.PcSlotError): codec.decode_owned(raw, wanted)
