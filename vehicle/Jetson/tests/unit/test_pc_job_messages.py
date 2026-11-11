"""원 EARLY ACK와 NN parser 완료의 서로 다른 계약을 검사하는 software 회귀다.

원 PC clock/pixel은 기존 명시 fixture다. 실제 actor/SDK/NN/D/Pi나 Source READY를
주장하지 않는다. 특히 metadata 형식의 성공을 실제 full-send/recv 사실로 세지 않는다.
"""

from copy import deepcopy
from hashlib import sha256
import unittest

from test_pc_slot import bundle, identifier
from test_mapped_pc_observation import binding_fields
from test_sim_nn_manifest import structural_fields
from xenova_vehicle.oak.sim_nn_manifest import SimNnManifest
from xenova_vehicle.simulation.pc_input import PcSourceBinding
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.pc_channel import encode_pc_actor_packet
from xenova_vehicle.runtime.pc_job_messages import (PcSnapshotAck, PcParserComplete,
    snapshot_ack_metadata, parser_manifest_sha256)


def packet(binding, kind, metadata, *, request_id=identifier(920), issued=1_100_110, expires=1_500_000):
    peer = LifecyclePeer(binding.fields['service_instance'], identifier(981), identifier(982), 'M', 'a' * 64)
    return encode_pc_actor_packet(peer, binding, 'M', kind, metadata, sequence=1,
        request_id=request_id, issued_us=issued, expires_us=expires)


class PcJobMessageTests(unittest.TestCase):
    def test_parser_warmup_ack_preserves_original_early_frame_but_cannot_claim_metric_snapshot(self):
        frame, _, _, _ = bundle()
        metadata = snapshot_ack_metadata(frame, job_id=identifier(920), purpose='PARSER_WARMUP',
            snapshot=None, now_us=1_100_110)
        original = packet(frame.binding, 'PC_SNAPSHOT_ACK', metadata, expires=frame.expires_j_us)
        ack = PcSnapshotAck(original, frame.binding)
        self.assertIsNone(ack.snapshot_j_us)
        self.assertIsNone(ack.source_context_sha256)
        self.assertIs(ack.validate_original(job_id=identifier(920), expires_us=frame.expires_j_us,
            full_key=frame.observation.rgb.stamp.key, pc_frame_proof_sha256=sha256(frame.proof_bytes).hexdigest(),
            purpose='PARSER_WARMUP', now_us=1_100_111), ack)
        with self.assertRaises(ValueError):
            snapshot_ack_metadata(frame, job_id=identifier(920), purpose='METRIC_MAP', snapshot=None, now_us=1_100_110)

    def test_snapshot_ack_rejects_retimed_job_proof_purpose_and_original_e_boundary(self):
        frame, _, _, _ = bundle()
        metadata = snapshot_ack_metadata(frame, job_id=identifier(920), purpose='PARSER_WARMUP',
            snapshot=None, now_us=1_100_110)
        ack = PcSnapshotAck(packet(frame.binding, 'PC_SNAPSHOT_ACK', metadata, expires=frame.expires_j_us), frame.binding)
        values = dict(job_id=identifier(920), expires_us=frame.expires_j_us, full_key=frame.observation.rgb.stamp.key,
            pc_frame_proof_sha256=sha256(frame.proof_bytes).hexdigest(), purpose='PARSER_WARMUP', now_us=1_100_111)
        for change in (dict(job_id=identifier(921)), dict(expires_us=frame.expires_j_us + 1),
                dict(pc_frame_proof_sha256='f' * 64), dict(purpose='METRIC_MAP'), dict(now_us=frame.expires_j_us - 1)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ack.validate_original(**dict(values, **change))

    def test_metric_ack_cannot_be_forged_by_adding_partial_or_boolean_snapshot_fields(self):
        frame, _, _, _ = bundle()
        metadata = snapshot_ack_metadata(frame, job_id=identifier(920), purpose='PARSER_WARMUP',
            snapshot=None, now_us=1_100_110)
        for change in (dict(snapshot_j_us='1100100'), dict(purpose='METRIC_MAP'),
                dict(purpose='METRIC_MAP', snapshot_j_us=True, sensor_fence_revision='0', source_context_sha256='f' * 64)):
            changed = dict(metadata, **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                PcSnapshotAck(packet(frame.binding, 'PC_SNAPSHOT_ACK', changed, expires=frame.expires_j_us), frame.binding)

    def parser_values(self):
        fields = binding_fields()
        original = PcSourceBinding(fields)
        declaration = structural_fields()
        declaration['sim_profile'].update(source_id=original.source_id, clock_id=original.clock_id,
            calibration_id=fields['calibration_profile_sha256'])
        manifest = SimNnManifest.from_bytes(canonical(declaration))
        fields['nn_manifest_sha256'] = manifest.sha256
        binding = PcSourceBinding(fields)
        full = [binding.source_id, binding.fields['stream_instances']['rgb_video'], binding.clock_id, 1]
        metadata = dict(profile='pc-parser-complete-1', purpose='PARSER_WARMUP', full_key=full,
            nn_key=full + [900_000, 1_000_000, manifest.sha256, 'd' * 64], pc_frame_proof_sha256='e' * 64,
            slot_descriptor_sha256='f' * 64, nn_manifest_sha256=manifest.sha256,
            parser_manifest_sha256=parser_manifest_sha256(manifest), completed_j_us='1100100')
        return binding, manifest, metadata

    def test_parser_completion_is_not_an_early_ack_and_keeps_full_nn_eight_key(self):
        binding, manifest, metadata = self.parser_values()
        original = packet(binding, 'PC_PARSE_COMPLETE', metadata)
        complete = PcParserComplete(original, binding, manifest)
        self.assertEqual(complete.nn_key, tuple(metadata['nn_key']))
        self.assertEqual(complete.completed_j_us, 1_100_100)
        with self.assertRaises(ValueError): PcSnapshotAck(original, binding)
        complete.validate_original(job_id=identifier(920), expires_us=1_500_000, full_key=tuple(metadata['full_key']),
            nn_key=tuple(metadata['nn_key']), pc_frame_proof_sha256='e' * 64,
            slot_descriptor_sha256='f' * 64, purpose='PARSER_WARMUP', now_us=1_100_111)
        with self.assertRaises(ValueError):
            complete.validate_original(job_id=identifier(920), expires_us=1_500_000,
                full_key=tuple(metadata['full_key']), nn_key=tuple(metadata['nn_key'][:-1] + ['0' * 64]),
                pc_frame_proof_sha256='e' * 64, slot_descriptor_sha256='f' * 64,
                purpose='PARSER_WARMUP', now_us=1_100_111)

    def test_parser_completion_rejects_foreign_manifest_context_subset_and_future_completion(self):
        binding, manifest, metadata = self.parser_values()
        for change in (dict(nn_manifest_sha256='a' * 64), dict(parser_manifest_sha256='b' * 64),
                dict(completed_j_us='1100111'), dict(nn_key=metadata['nn_key'][:6] + ['a' * 64, 'd' * 64])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                PcParserComplete(packet(binding, 'PC_PARSE_COMPLETE', dict(metadata, **change)), binding, manifest)
        changed = deepcopy(metadata); changed['extra_ready'] = True
        with self.assertRaises(ValueError):
            PcParserComplete(packet(binding, 'PC_PARSE_COMPLETE', changed), binding, manifest)
