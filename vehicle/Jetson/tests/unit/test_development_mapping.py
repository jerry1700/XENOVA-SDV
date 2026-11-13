"""개발 same-core factory의 원 sensor/NN/SQL 및 취소 경계를 검사한다.

Vehicle/clock은 기존 software leaf, NN은 owned FakeNN tensor다. 실제 renderer/OAK,
RPi Xen/Gateway 소비자나 폐루프·물리 READY의 양성 수락 증거가 아니다. 도로 없는 실제
zero-depth core 입력은 UNKNOWN/World 없음이어야 하며 fit·FREE·GT를 주입하지 않는다.
"""

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID

import test_mapper_profile as profile_fixtures
import test_simulation_motion as motion_fixtures
import test_yolop_frames as nn_fixtures
from xenova_vehicle.oak.sim_nn import HostClockSample, SimNnSubmission, SimNnResult
from xenova_vehicle.oak.yolop_frames import NNInputStamp
from xenova_vehicle.perception.pipeline import PerceptionPipeline
from xenova_vehicle.perception.road import LaneHypothesis, RoadResult
from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.lane_catalog import LaneCatalog, LaneSource
from xenova_vehicle.mapping.local_map import EvidenceGrid, GridSpec
from xenova_vehicle.mapping.registration import PointFeature, SegmentFeature, FrozenReference, register, transform_point
from xenova_vehicle.mapping.road_basis import RoadBasis
from xenova_vehicle.runtime.mapper_profile import MapperProfile
from xenova_vehicle.runtime.resident_perception import ResidentPerception
from xenova_vehicle.runtime.resident_storage import _Database, StorageProfile
from xenova_vehicle.simulation.development_mapping import DevelopmentMapping, DevelopmentMappingNotReady, CLOCK_BASIS
from xenova_vehicle.storage.contracts import canonical, json_bytes
from xenova_vehicle.storage.records import PROVENANCE, EvidenceTable, CheckpointPayload
from xenova_vehicle.storage.repository import PersistenceStore


@unittest.skipUnless(os.name == 'posix', 'actual existing SQLite owner requires POSIX')
class DevelopmentMappingTests(unittest.TestCase):
    def setUp(self):
        self.f = motion_fixtures.MotionFixture()
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.db = _Database(StorageProfile(str(Path(temporary.name) / 'development.sqlite3'), 'CREATE_IF_ABSENT'),
            time.monotonic_ns() // 1000 + 5_000_000)
        self.addCleanup(self.db.close)
        f = self.f; f.context = replace(f.context, database_uuid=self.db.metadata['database_uuid'])
        self.now = f.now; self.source_bytes = canonical(f.snapshot())
        self.motion = f.owner(); self.motion.ingest(self.source_bytes, now_us=self.now)
        original = f.observation(); self.expires = 1_499_800
        self.snapshot = self.motion.frame_snapshot(original, now_us=self.now, expires_j_us=self.expires,
            job_id=motion_fixtures.identifier(99), source_generation=f.raw.sha256, source_key=original.rgb.stamp.key)
        # SDK/Device 실행을 대체하는 원 명시 software enqueue/packet leaf다.
        sample = HostClockSample(1_020_001, 900_900, 1_020_002)
        submission = SimNnSubmission(original, original.capture_interval, self.expires, sample, 'c' * 64)
        seq, device, sdk = submission.key
        nn_input = NNInputStamp('nn_input', seq, device, sdk, 1_020_010, 320, 320, 'BGR888p', 'c' * 64)
        output = nn_fixtures.packet(sequence=seq, device_us=device, sdk_us=sdk,
            received_j_us=1_020_011, context_key='c' * 64)
        self.observation = SimNnResult(submission, nn_input, output).to_perception_input(original, clock=lambda: self.now)
        settings = profile_fixtures.profile_fields(f.profile.bound_calibration)
        settings['vehicle_configuration'] = f.source['configuration']
        settings['pipeline']['grid'].update(width=4, height=4, resolution_m=.1, origin_xy=(-.2, -.2))
        settings['pipeline']['tape'].update(x_min_m=.15, x_max_m=.25, y_min_m=-.05, y_max_m=.05, cell_m=.05)
        settings['pipeline']['depth'].update(max_patches=0, max_hit_boxes=0)
        settings['parking_aisles'] = []
        self.config = MapperProfile(settings)
        self.store = PersistenceStore(self.db.connection, f.context.database_uuid,
            expires_us=self.now + 5_000_000, clock_us=lambda: self.now)
        self.mapping = self.factory()

    def factory(self, **changes):
        f = self.f
        args = dict(source=f.source, clock=lambda: self.now); args.update(changes)
        return DevelopmentMapping(f.profile, self.config, f.raw, f.context, self.store, **args)

    def accept(self, candidate, **changes):
        args = dict(current_source_context_bytes=self.source_bytes, fence_revision=self.snapshot.fence_revision)
        args.update(changes); self.mapping.accepted(candidate, **args)

    def rows(self):
        return self.db.connection.execute('SELECT COUNT(*) FROM persistence_jobs').fetchone()[0]

    def test_explicit_development_factory_keeps_production_rejection_and_source_binding(self):
        with self.assertRaises(ValueError): ResidentPerception(canonical(self.f.profile.bound_calibration.to_dict()), self.f.raw)
        with self.assertRaises(ValueError): self.factory(source=dict(self.f.source, calibration='f' * 64))
        self.assertEqual(self.mapping.calibration.kind, 'SYNTHETIC')
        self.assertEqual(self.mapping.calibration.vehicle_bundle_sha256, self.f.profile.sha256)
        self.assertNotEqual(self.mapping.calibration.sha256, self.f.profile.sha256)
        self.assertEqual(self.rows(), 0)

    def test_actual_core_unknown_and_sql_proof_preserve_original_four_j_endpoint_keys(self):
        original = self.mapping.pipeline
        candidate = self.mapping.prepare(self.observation, self.snapshot)
        self.assertIs(self.mapping.pipeline, original)
        self.assertIsNone(candidate.world)
        self.assertEqual(candidate.result.summary['kind'], 'SYNTHETIC')
        self.assertFalse(candidate.result.summary['localization_qualified'])
        self.assertEqual(candidate.result.summary['input_odom_origin'], 'SYNTHETIC_ODOMETRY')
        self.assertEqual(candidate.result.summary['free_cells_applied'], 0)
        self.assertEqual(candidate.proof_receipt.status, 'COMMITTED')
        record = candidate.frame_provenance
        self.assertIsNotNone(record)
        stored = bytes(self.db.connection.execute('SELECT payload FROM provenance_records WHERE provenance_id=?',
            (record.provenance_id,)).fetchone()[0])
        self.assertEqual(stored, record.payload)
        context = bytes(self.db.connection.execute('SELECT payload FROM source_contexts WHERE context_sha256=?',
            (record.context_sha256,)).fetchone()[0])
        body = json_bytes(context, 4096)['body']
        self.assertEqual((body['profile'], body['input_kind'], body['clock_basis']),
            ('development-frame-evidence-1', 'SYNTHETIC', CLOCK_BASIS))
        expected = tuple((self.f.raw.fields['stream_instances'][name], image.stamp.sequence, at)
            for name, image in (('rgb_video', self.observation.rgb), ('depth_right', self.observation.depth))
            for at in image.stamp.captured_us)
        wire = PROVENANCE.unpack(record.payload)
        self.assertEqual(wire[2:5], (0, 4, 0))
        self.assertEqual(tuple((str(UUID(bytes=instance)), seq, at) for instance, seq, at in
            struct.iter_unpack('<16sQQ', wire[-3])), expected)
        self.assertEqual(body['source_context_sha256'], sha256(self.source_bytes).hexdigest())
        self.assertEqual((record.pi_lower, record.pi_upper), self.snapshot.metric_inputs.clock_lease.j_interval_to_pi(
            self.observation.capture_interval, now_j_us=self.now))
        self.assertIsNone(self.snapshot.p4_evidence)
        self.accept(candidate)
        self.assertIs(self.mapping.pipeline, candidate._state[0])

    def test_cancel_keeps_accepted_core_and_committed_original_proof_without_id_refund_or_reissue(self):
        original = self.mapping.pipeline
        candidate = self.mapping.prepare(self.observation, self.snapshot)
        cursor, rows = self.mapping._next, self.rows()
        self.mapping.cancelled(candidate)
        self.assertIs(self.mapping.pipeline, original)
        self.assertEqual(self.mapping._next, cursor)
        again = self.mapping.prepare(self.observation, self.snapshot)
        self.assertIs(again.frame_provenance, candidate.frame_provenance)
        self.assertIs(again.proof_receipt, candidate.proof_receipt)
        self.assertEqual((self.mapping._next, self.rows()), (cursor, rows))
        self.mapping.cancelled(again)

    def test_expired_or_changed_final_frame_never_adopts_and_new_source_context_is_not_a_relabel(self):
        candidate = self.mapping.prepare(self.observation, self.snapshot); original = self.mapping.pipeline
        with self.assertRaises(ValueError): self.accept(candidate, fence_revision=self.snapshot.fence_revision + 1)
        changed = json_bytes(self.source_bytes, 65536); changed['source']['mapper_instance'] = motion_fixtures.identifier(100)
        with self.assertRaises(ValueError): self.accept(candidate, current_source_context_bytes=canonical(changed))
        # COMMIT 전 검사는 통과해도 원 range 수신/adoption이 E 이후면 다음 frame에 쓰지 않는다.
        fresh = self.factory(); actual_reserve = self.store.reserve_provenance
        def late_range(*args, **kwargs):
            original = actual_reserve(*args, **kwargs)
            self.now = self.expires
            return original
        with patch.object(self.store, 'reserve_provenance', side_effect=late_range):
            with self.assertRaises(ValueError): fresh.prepare(self.observation, self.snapshot)
        self.assertIsNone(fresh._grant)
        self.assertEqual(fresh._next, 0)
        self.assertIsNotNone(fresh._fault)
        rows = self.db.connection.execute('SELECT COUNT(*) FROM provenance_ranges').fetchone()[0]
        with self.assertRaisesRegex(DevelopmentMappingNotReady, 'ORIGINAL_STORAGE_FAILURE'):
            fresh.prepare(self.observation, self.snapshot)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM provenance_ranges').fetchone()[0], rows)
        self.now = self.expires
        with self.assertRaises(ValueError): self.accept(candidate)
        self.assertIs(self.mapping.pipeline, original)
        self.mapping.cancelled(candidate)

    def test_missing_or_changed_nn_original_parent_is_rejected_before_core_or_sql(self):
        empty = replace(self.observation, nn=None, nn_key=None)
        bad = replace(self.observation, nn_key=(*self.observation.nn_key[:-1], 'f' * 64))
        with patch.object(PerceptionPipeline, 'process', side_effect=AssertionError('invalid source reached core')):
            for observation in (empty, bad):
                with self.subTest(nn=observation.nn is not None), self.assertRaises(DevelopmentMappingNotReady):
                    self.mapping.prepare(observation, self.snapshot)
        self.assertEqual(self.rows(), 0)

    def test_actual_commit_lost_result_is_sticky_and_never_automatically_reissued(self):
        actual = self.store.commit
        def lost_result(job, payload, **kwargs):
            receipt = actual(job, payload, **kwargs)
            return replace(receipt, status='UNKNOWN', durable_revision=None, committed_j_us=None)
        with patch.object(self.store, 'commit', side_effect=lost_result):
            candidate = self.mapping.prepare(self.observation, self.snapshot)
        self.assertIsNone(candidate.world)
        self.assertIn('ORIGINAL_STORAGE_NOT_COMMITTED', candidate.reasons)
        self.assertIsNotNone(self.mapping._fault)
        rows = self.rows(); self.mapping.cancelled(candidate)
        with self.assertRaisesRegex(DevelopmentMappingNotReady, 'ORIGINAL_STORAGE_FAILURE'):
            self.mapping.prepare(self.observation, self.snapshot)
        self.assertEqual(self.rows(), rows)

    def test_typed_synthetic_geometry_actual_registration_and_sql_lane_ids_build_world_without_free_promotion(self):
        """독립 유한 geometry leaf의 helper 양성이다. zero pixels가 도로를 검출했다는 주장이 아니다."""
        import numpy as np
        from xenova_drive_world import decode
        settings = self.config.to_dict()
        settings['pipeline']['grid'].update(width=8, height=8, resolution_m=.125, origin_xy=(0., -.5))
        self.config = MapperProfile(settings); self.mapping = self.factory()
        inputs = self.mapping._validate(self.observation, self.snapshot, self.now)
        raw_pose = inputs.pose; capture = self.observation.rgb.stamp
        # 원 encoder nominal에 첫 map seed를 놓는다. GT pose/관측 밖 endpoint는 쓰지 않는다.
        pose = raw_pose.sample.x, raw_pose.sample.y, raw_pose.sample.yaw
        segments = (SegmentFeature(transform_point((0., 0.), pose), transform_point((1., 0.), pose),
                .001, 'declared-longitudinal-leaf', 'previous-independent-leaf'),
            SegmentFeature(transform_point((.5, -.3), pose), transform_point((.5, .3), pose),
                .001, 'declared-transverse-leaf', 'previous-independent-leaf'))
        reference = FrozenReference(1, self.mapping.calibration.sha256, segments)
        points = tuple(PointFeature(p, .001, s.component, 'current-independent-leaf', (s.component,))
            for s, endpoints in zip(segments, (((0., 0.), (1., 0.)), ((.5, -.3), (.5, .3)))) for p in endpoints)
        fit = register(points, reference, raw_pose.prior, self.config.pipeline.registration,
            calibration_id=self.mapping.calibration.sha256,position_radius_m=raw_pose.sample.position_error_m)
        self.assertEqual(fit.status, 'BOUNDED'); self.assertTrue(fit.complete)
        fitted = replace(raw_pose, sample=replace(raw_pose.sample, x=fit.representative[0], y=fit.representative[1],
            yaw=fit.representative[2], position_error_m=fit.position_error_m, yaw_error_rad=fit.yaw_error_rad))
        alignment = MapOdomAlignment.from_match(raw_pose, fitted, observed_j_interval=raw_pose.captured_us,
            registration_revision=2, valid_for_us=100_000, position_drift_mps=.001, yaw_drift_rps=.001)
        core = self.mapping.pipeline
        spec = GridSpec(8, 8, .125, (0., -.5))
        core.grid = EvidenceGrid(spec, frame_id='odom:' + raw_pose.sample.epoch,
            calibration_id=self.mapping.calibration.sha256, clock_id=capture.clock_id, kind='SYNTHETIC')
        core._alignment = alignment
        h = LaneHypothesis(0, ((0,), (1,), (2,)), (0., 1.), ((0., 0.), (1., 0.)),
            ((0., .3), (1., .3)), ((0., -.3), (1., -.3)), ((0., .15), (1., .15)),
            ((0., -.15), (1., -.15)), (False, False), ((.29, .31), (.29, .31)), .001, .002)
        road = RoadResult('VALID', (h,), True, (), capture.key, True)
        lane = LaneCatalog(self.f.context, clock_id=capture.clock_id,
            calibration_id=self.mapping.calibration.sha256, frame_id=core.grid.frame_id)
        source = LaneSource(capture, self.mapping.calibration.sha256, self.f.context.map_epoch,
            core.grid.frame_id, self.f.context.odom_epoch, core.grid.revision, alignment.registration_revision,
            fit.status == 'BOUNDED' and alignment.valid_at(self.now), 'SYNTHETIC')
        association = lane.observe(road, source, fitted.prior, prior_qualified=source.qualified,
            crossing=b'\x00', profile=self.config.pipeline.registration,position_radius_m=fitted.sample.position_error_m)
        self.assertEqual(association.status, 'NEW')
        record, proof = self.mapping._frame_record(self.observation, self.snapshot, inputs)
        self.assertEqual(proof.status, 'COMMITTED')
        job = self.mapping._job(self.snapshot)
        head = self.store.catalog_heads(job, current_context_sha256=self.f.context.sha256)
        need = lane.allocation_need(association, head, job, reservation_nonce=motion_fixtures.identifier(101), now_us=self.now)
        slot_hwm = next(value for kind, value, _ in head.heads if kind == 'SLOT')
        payload = CheckpointPayload.build(job, evidence=EvidenceTable(reservations=(need.reservation,),
            catalog_highwaters=(need.reservation.end_exclusive, slot_hwm)), intent='CATALOG_RESERVATION')
        receipt = self.store.commit(job, payload, current_context_sha256=self.f.context.sha256)
        self.assertEqual(receipt.status, 'COMMITTED')
        lane.apply_committed_range(need, job, payload, receipt, current_context_sha256=self.f.context.sha256, now_us=self.now)
        model = lane.update(association, provenance_ids=(record.provenance_id, record.provenance_id))
        self.assertEqual(model.lane_ids, (need.reservation.start_inclusive, need.reservation.start_inclusive + 1))
        basis = RoadBasis().observe(lane, model, current_stamp=capture, resolution_m=spec.resolution_m,
            now_us=self.now, max_age_us=self.expires - capture.captured_us[0])
        self.assertEqual(basis.status, 'READY')
        before = core.grid.snapshot(self.now, clock_id=capture.clock_id)
        world = self.mapping._world(core, None, model, basis, self.snapshot, inputs)
        wire = decode(world.payload)
        self.assertTrue(any(flag & 8 for flag in wire.cell_flags))
        self.assertEqual(wire.occupancy, bytes([255]) * 64)
        self.assertEqual(wire.drivable_permission, bytes(8))
        self.assertTrue(np.array_equal(before['arrays']['state'], core.grid.state))
        self.assertEqual((world.input_kind, world.source.captured_us), ('SYNTHETIC', self.observation.capture_interval))
        self.assertEqual((wire.observation_seq, wire.reference_odom_seq), (capture.sequence, inputs.vehicle.anchor.sequence))
        self.assertEqual((wire.observed_us, wire.completed_us, wire.published_us),
            (sum(self.observation.capture_interval)//2, self.now, self.now))
        self.assertEqual((wire.calibration, world.source.calibration_id),
            (self.f.profile.sha256, self.f.profile.bound_calibration.sha256))
        self.assertFalse(world.world_valid or world.metric_ready or world.live_authority)
        # 관측 road와 교차하지 않는 명시 grid는 semantic costmask 표현 불가다.
        far = PerceptionPipeline(self.mapping.calibration, self.config.pipeline)
        far.grid = EvidenceGrid(GridSpec(8, 8, .125, (9999., -.5)), frame_id=core.grid.frame_id,
            calibration_id=self.mapping.calibration.sha256, clock_id=capture.clock_id, kind='SYNTHETIC')
        far._alignment = alignment
        with self.assertRaisesRegex(DevelopmentMappingNotReady, 'WORLD_DOMAIN_OR_ROAD_NOT_REPRESENTABLE'):
            self.mapping._world(far, None, model, basis, self.snapshot, inputs)


    def test_generic_snapshot_factory_cannot_bypass_nonzero_capture_original_expiry_or_epoch(self):
        """실제 generic query 결과도 개발 capture 계약을 대신 승인하지 않는다."""
        original = self.observation
        zero = (1_000_000, 1_000_000)
        zero_frame = replace(original,
            rgb=replace(original.rgb, stamp=replace(original.rgb.stamp, captured_us=zero)),
            depth=replace(original.depth, stamp=replace(original.depth.stamp, captured_us=zero)))
        foreign_source = 'sim-wall:' + motion_fixtures.identifier(123)
        foreign_rgb = replace(original.rgb.stamp, source_id=foreign_source)
        foreign_frame = replace(original, rgb=replace(original.rgb, stamp=foreign_rgb),
            depth=replace(original.depth, stamp=replace(original.depth.stamp, source_id=foreign_source)),
            nn_key=(*foreign_rgb.key, *original.nn_key[4:]))
        cases = (('zero-width', zero_frame, self.expires),
            ('over-original-500ms', original, original.capture_interval[0] + 500_001),
            ('foreign-capture-epoch', foreign_frame, self.expires))
        for index, (name, observation, expires) in enumerate(cases):
            with self.subTest(name=name):
                stamp = replace(observation.rgb.stamp, captured_us=observation.capture_interval,
                    available_us=max(observation.rgb.stamp.available_us, observation.depth.stamp.available_us))
                # wrapper나 Snapshot 필드를 patch하지 않고 실제 generic owner factory를 호출한다.
                snapshot = self.motion.sensor_state.frame_snapshot(stamp, now_us=self.now, expires_us=expires,
                    job_id=motion_fixtures.identifier(120 + index), source_generation=self.f.raw.sha256,
                    source_key=observation.rgb.stamp.key)
                self.assertIsNotNone(snapshot.metric_inputs.pose)
                self.assertIsNotNone(snapshot.metric_inputs.vehicle)
                self.assertEqual(snapshot.capture_stamp.captured_us, observation.capture_interval)
                self.assertEqual(snapshot.expires_us, expires)
                with patch.object(PerceptionPipeline, 'process', side_effect=AssertionError('invalid capture reached core')), \
                        patch.object(self.store, 'reserve_provenance', side_effect=AssertionError('invalid capture reserved IDs')), \
                        patch.object(self.store, 'catalog_heads', side_effect=AssertionError('invalid capture queried D head')), \
                        patch.object(self.store, 'commit', side_effect=AssertionError('invalid capture reached COMMIT')):
                    with self.assertRaisesRegex(DevelopmentMappingNotReady, 'ORIGINAL_DEVELOPMENT_CAPTURE_LIFETIME_OR_SOURCE_REQUIRED'):
                        self.mapping.prepare(observation, snapshot)
        self.assertEqual(self.rows(), 0)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM provenance_ranges').fetchone()[0], 0)
        self.assertIsNone(self.mapping._grant)
        self.assertEqual(self.mapping._next, 0)


if __name__ == '__main__': unittest.main()
