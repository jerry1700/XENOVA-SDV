"""관측 geometry preparation 회귀. capture·등록·D range는 명시한 typed unit leaf다.

실제 collector/센서 snapshot/공용 codec을 사용한다. 원 reply의 네트워크 admission,
D SQL COMMITTED/완전 LOAD, 관리 전송, topology·FREE·주행 수락을 주장하지 않는다.
"""

from dataclasses import asdict, replace
from hashlib import sha256
import struct
import unittest

import test_mapper_world as world_fixture
import test_mapper_profile as profile_fixture
from xenova_drive_world import schema, encode, decode
if hasattr(schema, 'RoadSection'):
    from xenova_drive_world.route import route_record_sha256
from xenova_vehicle.mapping.registration import FrozenReference, SegmentFeature
from xenova_vehicle.runtime.map_checkpoint import FrameEvidence, MapCheckpointCollector
from xenova_vehicle.runtime.mapper_profile import MapperProfile
from xenova_vehicle.runtime.mapper_route_records import (
    RoadRecordNotReady, prepare_observed_road_records, _section_record, _original_reply,
    _capture_hull, _birth_geometry_context, _geometry_scope)
from xenova_vehicle.storage.contracts import StorageJob, ProvenanceRange, canonical, json_bytes
from xenova_vehicle.storage.records import EvidenceTable


@unittest.skipUnless(hasattr(schema, 'RoadSection'), 'NOT_SUPPORTED: coherent B10 route contract required')
class MapperRouteRecordsTests(unittest.TestCase):
    def setUp(self):
        f = self.world = world_fixture.MapperWorldTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        owner = f.fixture.state
        self.sensors = owner.frame_snapshot(_capture_hull(f.draft.frame.observation), now_us=f.now,
            expires_us=f.item.expires_us, job_id=f.item.job_id, source_generation=f.item.source_generation,
            source_key=f.item.source_key)
        self.inputs = self.sensors.metric_inputs
        item = f.item.with_sensor_snapshot(self.sensors, now_us=f.now)
        job = StorageJob(f.context, world_fixture.uid(71), f.now-100, f.now+150_000)
        # 실제 SQL 실행이 아닌 독립 provenance allocator의 명시 typed leaf다.
        grant = ProvenanceRange(job, world_fixture.uid(72), 1000, 1256, 1256, f.now-50)
        collector = MapCheckpointCollector(f.draft.frame.scope, grant, now_us=f.now)
        parent, context = collector.vehicle_parent(self.inputs.vehicle, admission=f.sensor_owner, now_us=f.now)
        observation = f.draft.frame.observation
        frame = FrameEvidence.from_capture(f.draft.frame.scope, observation, item.sample,
            pi_interval=f.lease.j_interval_to_pi(observation.capture_interval, f.now),
            observation_sequence=f.draft.frame.observation_sequence, parents=(parent,), contexts=(context,),
            clock_lease=self.inputs.clock_lease, now_us=f.now, reference_vehicle=self.inputs.vehicle,
            admission=f.sensor_owner, registration_valid_for_us=f.alignment.valid_for_us)
        capture = sha256(canonical(observation.rgb.stamp.key)).hexdigest()
        points = f.basis.points
        f.pipeline.reference = FrozenReference(f.alignment.registration_revision, f.calibration.sha256,
            (SegmentFeature(points[0], points[-1], f.basis.geometry_error_m, 'OBSERVED_DIVIDER', capture),))
        summary = dict(f.draft.result.summary, rgb_updated=True)
        result = replace(f.draft.result, summary=summary)
        # 원 RGB/reference 수집 뒤 같은 실제 frame에 등록 증거를 연결하는 collector 순서다.
        f.pipeline._alignment = None
        collector.observe_result(frame, replace(result, summary=dict(summary, alignment_metadata=None)),
                                 f.pipeline, raw_pose=self.inputs.pose)
        f.pipeline._alignment = f.pipeline._propagation_anchor = f.alignment
        collector.observe_result(frame, result, f.pipeline, raw_pose=self.inputs.pose)
        evidence = EvidenceTable(tuple(collector._records.values()), tuple(collector._contexts.values()))
        self.state = replace(f.state, evidence=evidence)
        catalog = replace(f.draft.catalog_draft, original_job=item, state=self.state)
        self.draft = replace(f.draft, original_job=item, inputs=self.inputs, collector=collector,
                             frame=frame, result=result, catalog_draft=catalog)
        settings = profile_fixture.profile_fields(f.calibration)
        settings.update(vehicle_configuration=f.source['configuration'], pipeline=asdict(f.pipeline.profile))
        self.profile = MapperProfile(settings)
        p = decode(f.lease.raw_probe_bytes, schema.MapperClockProbe)
        source = f.source
        self.reply = schema.MapperClockReply(major=1, **{key: getattr(p, key) for key in
            ('pi_host_boot', 'pi_client_instance', 'probe_seq', 'nonce', 'pi_send_us', 'device')},
            **{key: source[key] for key in ('mapper_boot', 'mapper_instance', 'clock_epoch', 'map_epoch',
                'frame_epoch', 'odom_epoch', 'configuration', 'calibration', 'primitive')},
            j2=1_000_000, j3=1_000_001)
        self.raw_reply = encode(self.reply)

    def prepare(self, **changes):
        f = self.world
        args = dict(mapper_profile=self.profile, original_mapper_reply=self.raw_reply, completed_j_us=f.now+10)
        args.update(changes)
        return prepare_observed_road_records(self.draft, self.state, f.basis, self.sensors, **args)

    def test_actual_current_material_produces_only_scope_and_section_original_bodies(self):
        f = self.world
        prepared = self.prepare()
        self.assertEqual(tuple(r.kind for r in prepared.records), (8, 3))
        scope, section = (decode(r.body, kind) for r, kind in zip(prepared.records,
            (schema.RoadGeometryScope, schema.RoadSection)))
        for record in prepared.records:
            self.assertEqual(record.sha256, route_record_sha256(record.kind, record.body))
        self.assertEqual(scope.geometry_policy_sha256, bytes.fromhex(self.profile.sha256))
        self.assertEqual(scope.origin_storage_context_sha256, bytes.fromhex(f.context.sha256))
        self.assertEqual(section.geometry_scope_sha256, prepared.records[0].sha256)
        self.assertEqual(b''.join(struct.pack('<ff', p.x, p.y) for p in section.reference), f.basis.points_f32)
        self.assertEqual(tuple((p.x, p.y) for p in section.left_boundary),
            tuple((struct.unpack('<f', struct.pack('<f', p[0]))[0],
                   struct.unpack('<f', struct.pack('<f', p[1]))[0]) for p in f.model.road.hypotheses[0].left_outer))
        self.assertEqual(section.origin.mapper_clock_reply_sha256, sha256(self.raw_reply).digest())
        self.assertEqual(section.origin.capture_mapper_lower_us, f.draft.frame.observation.capture_interval[0])
        self.assertEqual(section.origin.completed_mapper_us, f.now+10)
        self.assertFalse(section.origin.HasField('independent_world_sha256'))
        self.assertEqual(prepared.original_mapper_reply, self.raw_reply)
        self.assertEqual(prepared.original_geometry_context_bytes, f.context.bytes)
        self.assertFalse(hasattr(prepared, 'durable'))
        self.assertFalse(hasattr(prepared, 'world_valid'))

    def test_missing_raw_reply_is_not_replaced_by_probe_or_trust_hash(self):
        for raw in (b'', self.inputs.clock_lease.raw_probe_bytes, sha256(self.raw_reply).digest()):
            with self.subTest(size=len(raw)), self.assertRaises(RoadRecordNotReady):
                self.prepare(original_mapper_reply=raw)

    def test_wrong_nonce_p1_source_and_future_reply_are_rejected(self):
        for field, value in (('nonce', b'x'*16), ('pi_send_us', self.reply.pi_send_us+1),
                             ('mapper_instance', world_fixture.uid(99)), ('j3', self.world.now+11)):
            reply = schema.MapperClockReply(); reply.CopyFrom(self.reply); setattr(reply, field, value)
            with self.subTest(field=field), self.assertRaises(RoadRecordNotReady):
                self.prepare(original_mapper_reply=encode(reply))

    def test_reply_must_belong_to_frozen_original_probe_and_lease(self):
        # wire를 새로 라벨링하지 않고 다른 실제 snapshot의 proof 집합을 명시한다.
        class MissingProofs:
            clock_proofs = ()
        with self.assertRaisesRegex(RoadRecordNotReady, 'original frozen snapshot'):
            _original_reply(self.inputs.clock_lease, MissingProofs(), self.raw_reply,
                            self.world.source, self.world.now+10)

    def test_expired_source_does_not_refresh_original_e_or_alignment(self):
        original_e = self.draft.original_job.expires_us
        original_alignment = self.draft.pipeline._alignment
        with self.assertRaises(RoadRecordNotReady):
            self.prepare(completed_j_us=original_e)
        self.assertEqual(self.draft.original_job.expires_us, original_e)
        self.assertIs(self.draft.pipeline._alignment, original_alignment)

    def test_frame_and_registration_membership_cannot_be_missing_from_catalog_closure(self):
        state = replace(self.state, evidence=EvidenceTable())
        draft = replace(self.draft, catalog_draft=replace(self.draft.catalog_draft, state=state))
        with self.assertRaisesRegex(RoadRecordNotReady, 'catalog closure'):
            prepare_observed_road_records(draft, state, self.world.basis, self.sensors,
                mapper_profile=self.profile, original_mapper_reply=self.raw_reply, completed_j_us=self.world.now+10)

    def test_station_mismatch_and_over_cap_never_clip_or_invent_window(self):
        prepared = self.prepare()
        section = decode(prepared.records[1].body, schema.RoadSection)
        basis = self.world.basis
        changed = bytearray(basis.points_f32); changed[:4] = struct.pack('<f', basis.points[0][0]+.01)
        with self.assertRaisesRegex(RoadRecordNotReady, 'exact adopted basis'):
            _section_record(self.world.model, replace(basis, points_f32=bytes(changed)), section.origin,
                            section.geometry_scope_sha256, section.alignment_raw_sha256)
        with self.assertRaisesRegex(RoadRecordNotReady, '32-station'):
            _section_record(self.world.model, replace(basis, points_f32=struct.pack('<ff', 0., 0.)*33),
                            section.origin, section.geometry_scope_sha256, section.alignment_raw_sha256)

    def test_other_profile_is_not_relabelled_as_same_geometry_policy(self):
        settings = self.profile.to_dict(); settings['vehicle_configuration'] += '-other'
        with self.assertRaisesRegex(RoadRecordNotReady, 'geometry profile'):
            self.prepare(mapper_profile=MapperProfile(settings))

    def test_native_rgb_depth_hull_preserves_both_original_intervals_and_availability(self):
        observation = self.draft.frame.observation
        rgb, depth = observation.rgb.stamp, observation.depth.stamp
        depth = replace(depth, captured_us=(rgb.captured_us[0]-1, rgb.captured_us[1]+1),
                        available_us=rgb.available_us+5)
        changed = replace(observation, depth=replace(observation.depth, stamp=depth))
        hull = _capture_hull(changed)
        self.assertEqual(hull.captured_us, changed.capture_interval)
        self.assertEqual(hull.available_us, depth.available_us)
        self.assertEqual(hull.key, rgb.key)
        self.assertNotEqual(hull, rgb)
        self.assertEqual(observation.rgb.stamp, rgb)
        self.assertEqual(changed.depth.stamp, depth)

    def anchor_context(self):
        collector = self.draft.collector
        row = collector._records[collector._propagation_anchor_record[1]]
        return collector._contexts[row.context_sha256]

    def changed_context(self, **changes):
        context = self.world.context
        binding = json_bytes(context.source_binding, 2048); binding.update(changes)
        replacements = {key: value for key, value in changes.items() if key in
                        ('map_epoch', 'frame_epoch', 'calibration')}
        if 'primitive' in changes:
            replacements['primitive_sha256'] = changes['primitive']
        origin = context.geometry_origin_fields
        if 'calibration' in changes:
            origin = dict(origin, calibration=changes['calibration'])
        return replace(context, source_binding=canonical(binding), geometry_origin=origin, **replacements)

    def test_normal_scene_and_binding_revision_preserve_birth_scope_and_goal_scope_reference(self):
        original = self.anchor_context()
        birth = _birth_geometry_context(original, self.world.context)
        pin = self.draft.collector._propagation_anchor_record
        scope = _geometry_scope(birth, self.profile.sha256, pin[2])
        goal_scope_sha = route_record_sha256(8, scope)
        current = self.changed_context(source_binding_revision='2',
            scene_admission_ref=dict(admission_id=world_fixture.uid(81), revision='7'))
        self.assertNotEqual(current.sha256, birth.sha256)
        same_birth = _birth_geometry_context(original, current)
        adopted = _geometry_scope(same_birth, self.profile.sha256, pin[2])
        self.assertEqual(route_record_sha256(8, adopted), goal_scope_sha)
        self.assertEqual(adopted.origin_storage_context_sha256, bytes.fromhex(birth.sha256))
        self.assertNotEqual(adopted.origin_storage_context_sha256, bytes.fromhex(current.sha256))
        # 현재 관측은 현재 full context를 기록한다. geometry birth로 소급 치환하지 않는다.
        prepared = self.prepare()
        section = decode(prepared.records[1].body, schema.RoadSection)
        self.assertEqual(section.origin.origin_context_sha256, bytes.fromhex(self.draft.context.sha256))

    def test_changed_map_frame_calibration_and_primitive_are_not_same_birth_geometry(self):
        for key, value in (('map_epoch', world_fixture.uid(82)), ('frame_epoch', world_fixture.uid(83)),
                           ('calibration', '9'*64), ('primitive', '8'*64)):
            with self.subTest(key=key), self.assertRaisesRegex(RoadRecordNotReady, 'incompatible'):
                _birth_geometry_context(self.anchor_context(), self.changed_context(**{key: value}))
