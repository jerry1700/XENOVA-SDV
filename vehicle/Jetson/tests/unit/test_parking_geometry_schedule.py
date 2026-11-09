"""새 RGB 정적 구획 재사용과 현재 occupancy·draft 채택의 실제 연결 회귀.

선분/metric clock은 명시한 software leaf다. 기존 SQLite allocator/Bridge/추출·점유
코드를 실행하며, clone 비교는 두 동시 D writer나 실제 카메라/FPS 수락을 뜻하지 않는다.
"""

from dataclasses import replace
import math
import sys
import unittest
from unittest.mock import patch

import test_parking_bridge as bridge_fixture
from test_parking import source, CALIBRATION, CLOCK, FRAME
from xenova_vehicle.mapping.lane_catalog import LaneCatalog
from xenova_vehicle.mapping.registration import PoseBox
from xenova_vehicle.mapping.road_basis import RoadBasis
from xenova_vehicle.perception.parking import UNKNOWN, OCCUPIED
from xenova_vehicle.runtime.mapper_catalogs import MappingState, CatalogView, _fork
from xenova_vehicle.runtime.parking_bridge import MetricEvidence
from xenova_vehicle.runtime.parking_geometry_schedule import (
    ParkingGeometrySchedule, ParkingGeometryWork, current_geometry_frame)
from xenova_vehicle.storage.records import EvidenceTable


@unittest.skipUnless(sys.platform == 'linux', '기존 실제 SQLite allocator는 대상 Linux에서 검증한다')
class ParkingGeometryScheduleTests(unittest.TestCase):
    def setUp(self):
        f = self.f = bridge_fixture.ParkingBridgeTests(methodName='runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        f.allocate()
        f.bridge._geometry_schedule = ParkingGeometrySchedule(250_000)
        # 기존 allocation이 끝난 실제 bridge에서 새 frame을 full 처리해 anchor를 만든다.
        f.clock.now = 1_020_000
        self.seed = source(2, captured=(1_010_000, 1_010_010), available=f.clock())
        self.assertTrue(self.process(f.bridge, self.seed).geometry_updated)
        self.assertIsNotNone(f.bridge._geometry_schedule.anchor)

    def metric(self, original):
        seq = original.stamp.sequence
        return MetricEvidence((original.stamp.key,), self.f.context.pi_host_boot,
            (100+seq*100, 200+seq*100), seq, 3,
            raw_keys=((self.f.context.mapper_worker, seq, original.stamp.captured_us[0]),))

    def process(self, bridge, original, *, prior=None, aisles=None, result=None, metric=True, registration=1):
        f = self.f
        return bridge.process(f.result(original) if result is None else result, stamp=original.stamp,
            capture_prior=f.prior if prior is None else prior, aisles=f.aisles if aisles is None else aisles,
            registration_revision=registration, prior_qualified=True, now_us=f.clock(),
            metric_evidence=self.metric(original) if metric else None)

    def next_frame(self, seq=3, *, at=1_060_000):
        self.f.clock.now = at
        return source(seq, captured=(at-1000, at-990), available=at)

    def clone(self, *, baseline=False):
        b = _fork(self.f.bridge); b.catalog = _fork(self.f.bridge.catalog)
        b._geometry_work = ParkingGeometryWork()
        if baseline:b._geometry_schedule = ParkingGeometrySchedule(0)
        return b

    def test_distinct_fresh_rgb_skips_actual_extract_and_associate_but_keeps_occupancy(self):
        from xenova_vehicle.runtime import parking_bridge as product
        baseline, reused = self.clone(baseline=True), self.clone()
        original = self.f.bridge.catalog.tracks[0]
        with patch.object(product, 'extract_slots', wraps=product.extract_slots) as calls:
            for seq, at, x in ((3, 1_060_000, .01), (4, 1_100_000, .02)):
                frame = self.next_frame(seq, at=at)
                # 같은 정적 MAP 구획을 다른 실제 capture key/이동한 base pose에서 본 입력 leaf다.
                result = self.f.result(frame)
                graph = replace(result.tape, strips=tuple(replace(s,
                    points_xy=tuple((px-x, py) for px, py in s.points_xy)) for s in result.tape.strips))
                result = replace(result, tape=graph)
                prior = PoseBox((x, x), (0., 0.), (0., 0.))
                self.assertTrue(self.process(baseline, frame, result=result, prior=prior).geometry_updated)
                before = calls.call_count
                update = self.process(reused, frame, result=result, prior=prior)
                self.assertEqual(calls.call_count, before)
                self.assertIn('STATIC_GEOMETRY_REUSED', update.reasons)
                self.assertFalse(update.geometry_updated)
        self.assertEqual(baseline.geometry_work['extract_calls'], 2)
        self.assertEqual(reused.geometry_work['extract_calls'], 0)
        self.assertEqual(reused.geometry_work['association_calls'], 0)
        self.assertEqual(reused.geometry_work['reused_new_rgb'], 2)
        self.assertEqual(reused.geometry_work['occupancy_refresh_calls'], 2)
        track = reused.catalog.tracks[0]
        self.assertIs(track.candidate.source, original.candidate.source)
        self.assertEqual(track.geometry_revision, original.geometry_revision)
        self.assertGreater(track.geometry_age_ms(self.f.clock(), CLOCK), original.geometry_age_ms(1_020_000, CLOCK))
        full = baseline.catalog.tracks[0]
        self.assertEqual(track.slot_id, full.slot_id)
        self.assertTrue(all(math.dist(a, b) <= track.candidate.position_bound_m+full.candidate.position_bound_m
                            for a, b in zip(track.candidate.corners, full.candidate.corners)))
        self.assertEqual(reused.geometry_work['neural_work_saved'], 0)
        self.assertFalse(reused.geometry_work['fps_measured'])

    def test_new_obstacle_is_occupied_without_new_geometry_or_clearance_age_reset(self):
        b = self.f.bridge; frame = self.next_frame()
        self.f.grid.add_hits(((.1, .1, .2, .2),), frame.stamp,
                             view_id='explicit-new-depth-hit', origin_xy=(0., -.1))
        before = b.geometry_work['extract_calls']
        update = self.process(b, frame)
        self.assertIn('STATIC_GEOMETRY_REUSED', update.reasons)
        self.assertEqual(b.geometry_work['extract_calls'], before)
        publication = b.publication_candidates(now_us=self.f.clock(), roi_bounds=(-.2, -.2, .8, .8))
        from xenova_drive_world import schema
        value = schema.ParkingSlot(); value.ParseFromString(publication.slots[0])
        self.assertEqual(value.occupancy_state, OCCUPIED)
        self.assertGreater(value.geometry_age_ms, 0)

    def test_current_missing_coverage_and_expired_free_do_not_reuse_clear(self):
        b = self.f.bridge; frame = self.next_frame()
        self.f.grid.permission[5, 5] = False
        before = b.geometry_work['extract_calls']
        self.process(b, frame)
        self.assertGreater(b.geometry_work['extract_calls'], before)
        self.assertGreater(b.geometry_work['reasons']['COVERAGE_CHANGED'], 0)
        frame = self.next_frame(4, at=1_620_000)
        self.process(b, frame)
        values = b.publication_candidates(now_us=self.f.clock(), roi_bounds=(-.2, -.2, .8, .8))
        from xenova_drive_world import schema
        value = schema.ParkingSlot(); value.ParseFromString(values.slots[0])
        self.assertEqual(value.occupancy_state, UNKNOWN)

    def test_pose_aisle_and_registration_changes_force_real_reconstruction(self):
        changes = (dict(prior=PoseBox((.1, .1), (0., 0.), (0., 0.))),
                   dict(aisles=(replace(self.f.aisles[0], side=-1),)), dict(registration=0))
        for args in changes:
            b = self.clone(); frame = self.next_frame()
            self.process(b, frame, **args)
            self.assertGreater(b.geometry_work['extract_calls'], 0)
            self.assertEqual(b.geometry_work['reused_new_rgb'], 0)

    def test_bool_qualification_without_current_metric_or_source_is_not_reuse(self):
        for change in ('no_metric', 'quarantined', 'source'):
            b = self.clone(); frame = self.next_frame(); result = self.f.result(frame)
            if change == 'quarantined':result = replace(result, summary=dict(result.summary, quarantined=True))
            if change == 'source':result = replace(result, tape=replace(result.tape, source_key=self.seed.stamp.key))
            self.assertIsNone(current_geometry_frame(b, result, frame.stamp, self.f.prior, self.f.aisles, 1,
                None if change == 'no_metric' else self.metric(frame), self.f.clock()))
            self.process(b, frame, result=result, metric=change != 'no_metric')
            self.assertEqual(b.geometry_work['reused_new_rgb'], 0)

    def test_discarded_mapping_fork_cannot_advance_accepted_anchor_but_work_is_counted(self):
        f = self.f; accepted = f.bridge._geometry_schedule.anchor
        state = MappingState(LaneCatalog(f.context, clock_id=CLOCK, calibration_id=CALIBRATION, frame_id=FRAME),
            f.bridge, EvidenceTable(), CatalogView('UNAVAILABLE', None, None), RoadBasis())
        draft = state.fork()
        frame = self.next_frame(at=1_300_000)
        before = f.bridge.geometry_work['extract_calls']
        self.assertTrue(self.process(draft.parking, frame).geometry_updated)
        self.assertIs(state.parking._geometry_schedule.anchor, accepted)
        self.assertIsNot(draft.parking._geometry_schedule.anchor, accepted)
        self.assertGreater(f.bridge.geometry_work['extract_calls'], before)
        # discard 뒤 새 fork도 원 accepted anchor 기준으로 due여야 한다.
        next_draft = state.fork(); frame = self.next_frame(4, at=1_310_000)
        before = f.bridge.geometry_work['extract_calls']
        self.process(next_draft.parking, frame)
        self.assertGreater(f.bridge.geometry_work['extract_calls'], before)
        self.assertIs(state.parking._geometry_schedule.anchor, accepted)
        # 실제 accept의 참조 교환과 같은 state 선택은 채택된 anchor만 보존한다.
        state = next_draft
        self.assertIs(state.parking._geometry_schedule.anchor, next_draft.parking._geometry_schedule.anchor)

    def test_duplicate_rgb_is_distinct_from_new_rgb_geometry_reuse(self):
        b = self.clone(); frame = self.next_frame()
        self.process(b, frame); self.process(b, frame)
        self.assertEqual(b.geometry_work['new_rgb'], 1)
        self.assertEqual(b.geometry_work['duplicate_rgb'], 1)
        self.assertEqual(b.geometry_work['reused_new_rgb'], 1)
        self.assertEqual(b.geometry_work['reused_duplicate_rgb'], 1)
        self.assertIs(b._applied_geometry.source.stamp, self.seed.stamp)

    def test_interval_boundary_rebuilds_without_refunding_geometry_capture(self):
        b = self.clone(); original = b.catalog.tracks[0].candidate.source.stamp
        frame = self.next_frame(at=original.captured_us[0]+250_000)
        self.process(b, frame)
        self.assertGreater(b.geometry_work['extract_calls'], 0)
        self.assertGreater(b.geometry_work['reasons']['INTERVAL'], 0)
        self.assertEqual(original.captured_us, self.seed.stamp.captured_us)


class ParkingGeometryPolicyTests(unittest.TestCase):
    def test_defaults_are_derived_from_existing_age_and_work_bounds(self):
        from xenova_vehicle.perception.parking import ParkingProfile
        p = ParkingProfile()
        self.assertEqual(ParkingGeometrySchedule.live_interval(p), 250_000)
        self.assertEqual(ParkingGeometrySchedule.live_interval(replace(p, free_age_us=100_000)), 50_000)
        self.assertEqual(ParkingGeometrySchedule().interval_us, 0)
        with self.assertRaises(ValueError):ParkingGeometrySchedule(250_001)

    def test_catalog_constructor_can_compare_synthetic_full_and_reuse_without_relabelling(self):
        from test_parking_catalog import context
        from xenova_vehicle.runtime.mapper_catalogs import MapperCatalogs
        from xenova_vehicle.runtime.mapper_storage import MapperStorageMailbox
        from xenova_vehicle.perception.calibration import BodyModel
        ctx = context()
        def owner(**extra):
            return MapperCatalogs(ctx, mailbox=MapperStorageMailbox(ctx.sha256), clock_id=CLOCK,
                map_frame_id=FRAME, body=BodyModel(), method_manifest_sha256='c'*64,
                input_kind='SYNTHETIC', **extra)
        self.assertEqual(owner()._accepted.parking._geometry_schedule.interval_us, 0)
        full = owner(parking_geometry_interval_us=0)
        reuse = owner(parking_geometry_interval_us=250_000)
        self.assertEqual(full._accepted.parking._geometry_schedule.interval_us, 0)
        self.assertEqual(reuse._accepted.parking._geometry_schedule.interval_us, 250_000)
        self.assertEqual(reuse._accepted.parking._kind, 'SYNTHETIC')


@unittest.skipUnless(sys.platform == 'linux', '기존 LIVE typed sensor/collector와 SQLite는 대상에서 검증한다')
class LiveParkingGeometryScheduleTests(unittest.TestCase):
    def test_actual_live_pipeline_registration_and_collector_prior_reuse_two_fresh_frames(self):
        """기존 LIVE fixture를 그대로 쓴다. tape/depth만 명시 측정 leaf이며 승인 bool을 주입하지 않는다."""
        import json
        from uuid import uuid4
        import test_mapper_mapping as mapping_fixture
        from test_parking import graph
        from xenova_drive_world import encode
        from xenova_vehicle.mapping.local_map import GridSpec
        from xenova_vehicle.mapping.registration import RegistrationProfile
        from xenova_vehicle.perception.depth import DepthEvidence
        from xenova_vehicle.perception.parking import ParkingAisle
        from xenova_vehicle.perception.pipeline import PerceptionPipeline
        from xenova_vehicle.runtime.mapper_compute import MapperInput
        from xenova_vehicle.runtime.parking_bridge import ParkingBridge
        from xenova_vehicle.runtime.resident_samples import CaptureSample
        from xenova_vehicle.storage.contracts import StorageJob, CatalogReservation, canonical
        from xenova_vehicle.storage.records import CheckpointPayload
        from xenova_vehicle.runtime import parking_bridge as product

        f = mapping_fixture.MapperMappingTests(methodName='runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        self.assertEqual(f.fixture.calibration.kind, 'LIVE')
        original_calibration = f.fixture.calibration.sha256
        f.grant()
        profile = replace(f.profile.pipeline, grid=GridSpec(20, 20, .05, (.8, -.3)),
            registration=RegistrationProfile(cell_steps=(.0005, .0005, .0005), residual_m=.0005,
                max_position_error_m=.02, max_yaw_error_rad=.01), max_free_cells=1, max_free_ray_tests=16)
        core = PerceptionPipeline(f.fixture.calibration, profile)
        b = ParkingBridge(f.context, clock_id=f.fixture.raw.fields['clock_epoch'],
            map_frame_id='odom:'+f.context.odom_epoch, body=f.fixture.calibration.body,
            method_manifest_sha256=f.profile.sha256, input_kind='LIVE', calibration_profile=f.fixture.calibration,
            geometry_interval_us=250_000)
        def job():return StorageJob(f.context, str(uuid4()), f.now, f.now+5_000_000)
        # approach lane도 같은 실제 D의 명시 예약에서 나온다. 현재 World의 lane을 날조하지 않는다.
        lane_job = job()
        reservation = CatalogReservation(f.context.database_uuid, f.context.map_epoch, 'LANE', 1, 1, 3,
            str(uuid4()), f.context.mapper_worker, f.context.sha256)
        payload = CheckpointPayload.build(lane_job, evidence=EvidenceTable(reservations=(reservation,),
            catalog_highwaters=(3, 1)), intent='CATALOG_RESERVATION')
        self.assertEqual(f.store.commit(lane_job, payload, current_context_sha256=f.context.sha256).status, 'COMMITTED')
        request = b.prepare_provenance(job(), str(uuid4()), block_count=128)
        grant = f.store.reserve_provenance(request.job, request.reservation_nonce, block_count=128,
                                         current_context_sha256=f.context.sha256)
        self.assertTrue(b.accept_provenance(request, grant, now_us=f.now))
        aisles = (ParkingAisle((.9, -.2), (1.9, -.2), 1, .001, approach_lane_id=1),)
        source = json.loads(f.source_bytes)
        # 기존 sensor fixture가 이미 수락한 J 뒤에 새 표본을 생산한다. 고정 절대시각으로 되감지 않는다.
        admitted_j_us = f.now
        first_capture_j_us = admitted_j_us+10_000
        skip_count = 0
        for index in range(4):
            capture = first_capture_j_us+index*20_000; f.now = capture+10_000
            self.assertGreaterEqual(f.now, admitted_j_us)
            # 원 sensor owner에 현재 capture 양쪽의 실제 새 Vehicle bytes를 admit한다.
            for side in range(2):
                # 이 unit leaf는500us bracket의 새 encoder 표본을 명시한다. 실물 sensor rate 수락이 아니다.
                seq = 3+index*2+side; pi = capture+(-250 if side == 0 else 250)+100_000
                value = f.fixture.fixture.vehicle(seq, pi, index*.001)
                value.clock_error_us = 0; value.position_error_m = .00001; value.heading_error_rad = .00001
                value.source_us = pi-600_000; value.velocity_mps = .05
                source['vehicles'].append(f.fixture.row(encode(value), received=f.now-100+side))
            f.source_bytes = canonical(source)
            template, observation = f.item(); seq = 12+index
            rgb_stamp = replace(observation.rgb.stamp, sequence=seq, captured_us=(capture-200, capture+200), available_us=f.now)
            depth_stamp = replace(observation.depth.stamp, sequence=seq, captured_us=rgb_stamp.captured_us, available_us=f.now)
            observation = replace(observation, rgb=replace(observation.rgb, stamp=rgb_stamp),
                                  depth=replace(observation.depth, stamp=depth_stamp))
            f.inputs = f.fixture.state.read(f.source_bytes, rgb_stamp, now_us=f.now)
            self.assertIsNotNone(f.inputs.pose)
            raw = template.sample
            clocks = {name:dict(bridge_sample=dict(j_before_us=str(f.now), sdk_us=str(f.now), j_after_us=str(f.now)),
                sdk_host_timestamp_j_bracket_us=list(map(str, rgb_stamp.captured_us))) for name in ('rgb_video', 'depth_right')}
            sample = CaptureSample(replace(raw.rgb, sequence=seq, device_us=400000+index*20000, sdk_us=500000+index*20000),
                replace(raw.depth, sequence=seq, device_us=400001+index*20000, sdk_us=500001+index*20000), None, None, clocks)
            item = MapperInput(str(uuid4()), f.fixture.raw.sha256, sample, f.now+150_000, context_bytes=f.source_bytes)
            draft = f.mapping.prepare_frame(item, core, observation, f.inputs, f.fixture.state, f.now)
            self.assertTrue(draft.ready, draft.reason)
            measured = graph()
            measured = replace(measured, source_key=rgb_stamp.key,
                calibration_qualified=f.fixture.calibration.metric_qualified, strips=tuple(replace(s,
                points_xy=tuple((x+1.-index*.001, y-.2) for x, y in s.points_xy)) for s in measured.strips))
            count = observation.depth.width*observation.depth.height
            depth = DepthEvidence(depth_stamp, f.fixture.calibration.sha256, 'LIVE', (), (),
                                  0, 0, count, count, 0, 0, False, 'UNKNOWN')
            with patch('xenova_vehicle.perception.pipeline.extract_tape', return_value=measured), \
                    patch('xenova_vehicle.perception.depth.analyze_depth', return_value=depth):
                result = core.process(observation, pose=f.inputs.pose, now_us=f.now, evidence_observer=draft.observer,
                    localization_valid_for_us=100_000, localization_position_drift_mps=.001, localization_yaw_drift_rps=.001)
            # 실제 finish_frame이 Catalogs로 전달한, 등록 후 같은 capture의 MAP prior를 관찰한다.
            with patch.object(f.mapping.catalogs, 'prepare', wraps=f.mapping.catalogs.prepare) as prepare:
                draft = f.mapping.finish_frame(draft, result, now_us=f.now)
                arguments = prepare.call_args.kwargs
            self.assertTrue(draft.ready, draft.reason)
            f.mapping.accept(draft)
            if index == 0:continue  # 실제 첫 reference seed는 아직 localization 권한이 없다.
            self.assertEqual(result.registration.status, 'BOUNDED')
            self.assertTrue(result.summary['localization_qualified'])
            self.assertEqual(result.summary['kind'], 'LIVE')
            self.assertIsNotNone(draft.collector._alignment_record)
            def consume():
                return b.process(result, stamp=rgb_stamp, capture_prior=arguments['capture_prior'].prior,
                    aisles=aisles, registration_revision=arguments['registration_revision'],
                    prior_qualified=arguments['prior_qualified'], metric_evidence=arguments['metric_evidence'], now_us=f.now)
            if index == 1:
                update = consume(); self.assertTrue(update.allocation_wait)
                range_job = job(); head = f.store.catalog_heads(range_job, current_context_sha256=f.context.sha256)
                request = b.prepare_range(head, range_job, str(uuid4()), now_us=f.now)
                receipt = f.store.commit(request.job, request.payload, current_context_sha256=f.context.sha256)
                self.assertTrue(b.accept_range(request, receipt, now_us=f.now))
                self.assertTrue(consume().geometry_updated)
                self.assertIsNotNone(b._geometry_schedule.anchor)
            else:
                with patch.object(product, 'extract_slots', wraps=product.extract_slots) as extract:
                    update = consume()
                    self.assertEqual(extract.call_count, 0)
                self.assertIn('STATIC_GEOMETRY_REUSED', update.reasons)
                skip_count += 1
        self.assertEqual(skip_count, 2)
        self.assertEqual(b.geometry_work['reused_new_rgb'], 2)
        self.assertEqual(f.fixture.calibration.sha256, original_calibration)
        self.assertEqual(b._kind, 'LIVE')
