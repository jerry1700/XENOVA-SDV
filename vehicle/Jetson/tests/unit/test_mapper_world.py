"""실제 typed compute·clock·sensor·catalog와 공용 World codec의 집중 회귀.

영상/정합/COMMITTED 사건은 명시한 software fixture다. 실제 장치 보정·Pi 승인·SQL
내구성을 주장하지 않는다. 실제 bytes 소비, 원 시각, 현재 admission과 기하를 검사한다.
"""

from dataclasses import replace
import math
import struct
from types import SimpleNamespace
import unittest

import test_mapper_sensor_state as sensors_fixture
import test_world_adapter as road_fixture
from xenova_drive_world import schema, decode, encode
from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.lane_catalog import LaneCatalog, LaneSource
from xenova_vehicle.mapping.local_map import EvidenceGrid, GridSpec, FREE, OCCUPIED, UNKNOWN
from xenova_vehicle.mapping.odometry import PoseEstimate
from xenova_vehicle.mapping.registration import PoseBox
from xenova_vehicle.mapping.road_basis import RoadBasis
from xenova_vehicle.oak.frames import RawFrame
from xenova_vehicle.perception.inputs import FrameStamp, ImageFrame, PerceptionInput
from xenova_vehicle.perception.pipeline import PerceptionPipeline, PerceptionResult
from xenova_vehicle.perception.free_space import TargetClass
from xenova_vehicle.perception.tape import TapeGraph
from xenova_vehicle.runtime.demo_occupancy import fill_demo_road
from xenova_vehicle.runtime.map_checkpoint import FrameEvidence
from xenova_vehicle.runtime.mapper_catalogs import CatalogDraft, CatalogView, MappingState
from xenova_vehicle.runtime.mapper_compute import MapperInput
from xenova_vehicle.runtime.mapper_mapping import MappingFrameDraft
from xenova_vehicle.runtime.mapper_world import build_mapping_world
from xenova_vehicle.runtime.mapper_profile import StoppingLimits
from xenova_vehicle.perception.temporal_road import RoadLink
from xenova_vehicle.runtime.parking_bridge import ParkingBridge, ParkingPublication
from xenova_vehicle.runtime.resident_samples import CaptureSample
from xenova_vehicle.runtime.scene import SceneEvidenceScope
from xenova_vehicle.storage.contracts import StorageJob, CatalogHeadSnapshot, CommitReceipt, canonical
from xenova_vehicle.storage.records import EvidenceTable, CheckpointPayload


def uid(number):
    return '30000000-0000-4000-8000-%012x' % number


class DemoOccupancyTests(unittest.TestCase):
    def test_fill_reserves_a_current_or_expired_cone_and_its_shadow(self):
        import numpy as np

        spec = GridSpec(32, 32, .05, (0., -.8))
        state = np.zeros((32, 32), dtype=np.uint8)
        age = np.full(state.shape, np.iinfo(np.uint32).max, dtype=np.uint32)
        allowed = np.ones(state.shape, dtype=bool)
        allowed[13, 9] = False
        road = np.zeros_like(allowed)
        road[12:20, 4:28] = True
        camera_hits = np.zeros_like(allowed)
        camera_hits[16, 16] = True
        camera_hits[16, 17] = True
        state[16, 16] = OCCUPIED
        age[16, 16], age[16, 17] = 50, 600
        state[16, 26] = FREE  # A farther cell was free before the cone.
        state[12, 8] = OCCUPIED  # ToF hit, does not cast an OAK shadow.
        age[12, 8] = 40
        snapshot = dict(spec=spec, arrays=dict(state=state, age_ms=age,
            support=np.ones_like(allowed), permission=allowed))
        result = fill_demo_road(snapshot, road, pose=SimpleNamespace(x=0., y=0., yaw=0.),
            camera=SimpleNamespace(translation=(.2, .025, .14)),
            targets=(TargetClass(.09, .18, .1273),), camera_hits=camera_hits)
        outbound, ages = result['arrays']['state'], result['arrays']['age_ms']
        self.assertEqual((outbound[16, 16], ages[16, 16]), (OCCUPIED, 50))
        self.assertEqual((outbound[16, 17], ages[16, 17]), (UNKNOWN, 600))
        self.assertEqual(outbound[16, 26], UNKNOWN)
        self.assertEqual(outbound[12, 8], OCCUPIED)
        self.assertEqual((outbound[12, 9], ages[12, 9]), (FREE, 0))
        self.assertEqual(outbound[8, 9], FREE)  # Grid coverage around the lane cut.
        self.assertEqual(outbound[7, 9], UNKNOWN)
        self.assertEqual(outbound[13, 9], UNKNOWN)
        self.assertEqual(state[16, 26], FREE)


class MapperWorldTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('실제 grid/codec 회귀는 기존 NumPy가 필요함')
        self.fixture = sensors_fixture.MapperSensorStateTests(methodName='runTest')
        self.fixture.setUp()
        f = self.fixture
        self.inputs = f.read()
        self.now = f.now
        self.context, self.source, self.calibration = f.context, self.inputs.source, f.calibration
        self.sensor_owner = f.state.sensors
        self.lease, self.vehicle = self.inputs.clock_lease, self.inputs.vehicle
        raw = f.raw.fields
        stamp = f.stamp
        rgb = RawFrame('rgb_video', stamp.sequence, 400000, 500000, self.now,
            640, 360, 'NV12', bytes(640*360*3//2), 0)
        depth = RawFrame('depth_right', 17, 400001, 500001, self.now,
            640, 480, 'RAW16_LE', bytes(640*480*2), 0)
        depth_stamp = FrameStamp(raw['acquisition_worker'], raw['stream_instances']['depth_right'],
            depth.sequence, stamp.clock_id, stamp.captured_us, self.now)
        observation = PerceptionInput('LIVE', self.calibration.sha256,
            ImageFrame(stamp, 640, 360, 'NV12', rgb.payload),
            ImageFrame(depth_stamp, 640, 480, 'RAW16_LE', depth.payload))
        clocks = {name: dict(bridge_sample=dict(j_before_us=str(self.now), sdk_us=str(self.now),
            j_after_us=str(self.now)), sdk_host_timestamp_j_bracket_us=list(map(str, stamp.captured_us)))
            for name in ('rgb_video', 'depth_right')}
        self.item = MapperInput(uid(1), f.raw.sha256, CaptureSample(rgb, depth, None, None, clocks),
            self.now+150_000, context_bytes=canonical(f.snapshot()))
        scope = SceneEvidenceScope(self.context, f.raw, self.calibration, 'c'*64)
        frame = FrameEvidence(scope, observation, self.lease.j_interval_to_pi(observation.capture_interval, self.now),
            ((raw['stream_instances']['rgb_video'], rgb.sequence, rgb.device_us),
             (raw['stream_instances']['depth_right'], depth.sequence, depth.device_us)),
            71, (), (), self.lease, self.now, reference_vehicle=self.vehicle, registration_valid_for_us=100_000)
        self.grid = EvidenceGrid(GridSpec(20, 20, .1, (-.2, -.7)), frame_id='odom:'+self.context.odom_epoch,
            calibration_id=self.calibration.sha256, clock_id=stamp.clock_id, kind='LIVE')
        domain = np.ones((20, 20), dtype=bool)
        self.grid.set_domain(domain, domain)
        self.pipeline = PerceptionPipeline(self.calibration)
        self.pipeline.grid = self.grid
        self.alignment = MapOdomAlignment((.01, .02, .03), .001, .002, self.inputs.pose.sample.context,
            observation.capture_interval, 9, 100_000, .001, .002)
        self.pipeline._alignment = self.alignment
        road = replace(road_fixture.road(), source_key=stamp.key)
        lane = LaneCatalog(self.context, clock_id=stamp.clock_id, calibration_id=self.calibration.sha256,
            frame_id=self.grid.frame_id)
        lane_source = LaneSource(stamp, self.calibration.sha256, self.context.map_epoch,
            self.grid.frame_id, self.context.odom_epoch, self.grid.revision, 9, True, 'LIVE')
        proof = lane.observe(road, lane_source, PoseBox((0., 0.), (0., 0.), (0., 0.)),
            prior_qualified=True, crossing=b'\0')
        job = StorageJob(self.context, uid(2), self.now-100, self.now+100_000)
        head = CatalogHeadSnapshot(job.job_id, self.context.sha256, self.context.database_uuid,
            self.context.map_epoch, (('LANE', 101, 1), ('SLOT', 1, 0)), self.now-100, job.expires_us)
        need = lane.allocation_need(proof, head, job, reservation_nonce=uid(3), now_us=self.now-100, count=64)
        payload = CheckpointPayload.build(job, evidence=EvidenceTable(reservations=(need.reservation,),
            catalog_highwaters=(165, 1)), intent='CATALOG_RESERVATION')
        receipt = CommitReceipt(self.context.database_uuid, job.job_id, job.digest(payload),
            'COMMITTED', 1, self.now-50, self.context.sha256)
        lane.apply_committed_range(need, job, payload, receipt, current_context_sha256=self.context.sha256, now_us=self.now)
        self.model = lane.update(proof, provenance_ids=(501, 502))
        self.basis_owner = RoadBasis()
        self.basis = self.basis_owner.observe(lane, self.model, current_stamp=stamp,
            resolution_m=self.grid.spec.resolution_m, now_us=self.now, max_age_us=150_000)
        self.assertEqual(self.basis.status, 'READY')
        parking = ParkingBridge(self.context, clock_id=stamp.clock_id, map_frame_id=self.grid.frame_id,
            body=self.calibration.body, method_manifest_sha256='c'*64, input_kind='LIVE', calibration_profile=self.calibration)
        view = CatalogView(proof.status, self.model, None, road_basis=self.basis)
        self.state = MappingState(lane, parking, EvidenceTable(), view, self.basis_owner)
        summary = dict(kind='LIVE', calibration_id=self.calibration.sha256, rgb_source_key=stamp.key,
            now_us=self.now, localization_qualified=True, quarantined=False, alignment_metadata=self.alignment.fields())
        result = PerceptionResult(summary, TapeGraph((), source_key=stamp.key), road, None, None,
            self.grid.snapshot(self.now, clock_id=stamp.clock_id), None,
            mapped_odometry=self.alignment.apply(self.inputs.pose))
        catalog = CatalogDraft(self.item, self.context, 0, self.state)
        self.draft = MappingFrameDraft(self.item, self.context, self.pipeline, None, self.inputs, frame,
            None, 0, True, 'READY', catalog, result)

    def build(self, *, draft=None, state=None, basis=None, **changes):
        arguments = dict(context=self.context, source=self.source, calibration=self.calibration,
            sensors=self.sensor_owner, current_vehicle=self.vehicle, clock_lease=self.lease,
            sequence=901, completed_j_us=self.now+10, published_j_us=self.now+20)
        arguments.update(changes)
        return build_mapping_world(self.draft if draft is None else draft, self.state if state is None else state,
            self.basis if basis is None else basis, **arguments)

    def test_live_world_uses_actual_k_and_neutral_basis_original_metadata_and_stable_ids(self):
        built = self.build()
        self.assertIsNotNone(built)
        value = decode(built.payload)
        expected = self.alignment.apply(PoseEstimate(self.vehicle.pose_sample, self.vehicle.j_interval,
            self.vehicle.j_interval)).sample
        capture_pose = self.alignment.apply(self.inputs.pose).sample
        self.assertNotEqual(expected.x, capture_pose.x)
        self.assertAlmostEqual(value.reference_vehicle_pose.x, expected.x, places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.y, expected.y, places=7)
        self.assertEqual(value.reference_odom_seq, self.vehicle.anchor.sequence)
        self.assertEqual(b''.join(struct.pack('<ff', p.x, p.y) for p in value.reference), self.basis.points_f32)
        self.assertNotEqual(tuple((p.x, p.y) for p in value.reference),
            tuple((p.x, p.y) for p in value.lane_model.lanes[0].centerline))
        self.assertEqual(tuple(l.lane_id for l in value.lane_model.lanes), (101, 102))
        self.assertEqual(value.lane_model.crossing, b'\0')
        self.assertEqual((value.sequence, value.observation_seq), (901, 71))
        self.assertEqual((value.completed_us, value.published_us), (self.now+10, self.now+20))
        loc = value.localization
        self.assertEqual((loc.registration_revision, loc.observed_mapper_us, loc.valid_for_us, loc.timestamp_error_us),
            (9, 1_000_000, 100_000, 200))
        self.assertGreaterEqual(loc.anchor_position_bound_m, expected.position_error_m)
        self.assertAlmostEqual(loc.anchor_position_bound_m, expected.position_error_m, places=7)
        self.assertGreaterEqual(loc.position_drift_bound_mps, self.alignment.position_drift_mps)
        self.assertEqual(value.calibration, self.calibration.vehicle_bundle_sha256)
        self.assertEqual(built.source.calibration_id, self.calibration.sha256)
        self.assertFalse(built.live_authority or built.metric_ready or built.world_valid)

    def test_actual_grid_rectangle_and_priority_do_not_invent_free_or_permission(self):
        import numpy as np
        arrays = self.draft.result.map_snapshot['arrays']
        changed = dict(arrays, support=np.zeros((20, 20), dtype=bool))
        result = replace(self.draft.result, map_snapshot=dict(self.draft.result.map_snapshot, arrays=changed))
        value = self.build(draft=replace(self.draft, result=result)).message()
        self.assertEqual(set(value.occupancy), {255})
        self.assertFalse(any(value.drivable_permission))
        self.assertTrue(any(flag & 8 for flag in value.cell_flags))
        self.assertFalse(any(flag & 2 for flag in value.cell_flags))
        self.assertEqual(value.age_le32, arrays['age_ms'].astype('<u4').tobytes())
        x0, y0 = value.origin.x, value.origin.y
        self.assertEqual(value.left_boundary[0].x, x0)
        self.assertEqual(value.right_boundary[0].y, y0)
        self.assertGreaterEqual(value.left_boundary[-1].x, x0+value.width*value.resolution_m)
        self.assertGreaterEqual(value.left_boundary[0].y, y0+value.height*value.resolution_m)
        self.assertNotAlmostEqual(value.left_boundary[0].y, self.model.road.hypotheses[0].left_outer[0][1])

    def test_current_pose_publishes_prior_lanes_and_does_not_refresh_cell_age(self):
        from xenova_vehicle.mapping.local_map import FREE, OCCUPIED
        self.grid.state[0,0],self.grid.state[0,1] = FREE,OCCUPIED
        self.grid.time_known[0,:2] = True
        self.grid.observed_us[0,0] = self.now-self.grid.free_ttl_us
        self.grid.observed_us[0,1] = self.now-30_000
        points = lambda x: ((x,.3),(x,0.),(x,-.3))
        self.pipeline._temporal_road._store(tuple(
            RoadLink(points(a),points(b),.001,frozenset(),(frozenset(),)*3,False,.002)
            for a,b in ((-.5,0.),(0.,.5),(.5,1.))))
        state = replace(self.state,view=replace(self.state.view,lane_status='UNKNOWN',lane=None,
            road_basis=replace(self.basis,status='WAIT_CURRENT')))
        result = replace(self.draft.result,tape=None,road=None)
        draft = replace(self.draft,result=result,catalog_draft=replace(self.draft.catalog_draft,state=state))
        value = self.build(draft=draft,state=state,stopping=StoppingLimits(.5,1.,10_000,50_000,30_000,.005)).message()
        self.assertEqual(value.observation_seq,71)
        self.assertEqual(tuple(l.lane_id for l in value.lane_model.lanes),(101,102))
        self.assertLess(value.lane_model.divider[0].x,-.2)
        self.assertGreaterEqual(value.lane_model.divider[-1].x,.5)
        self.assertEqual(list(value.occupancy[:2]),[255,100])
        self.assertEqual(struct.unpack_from('<II',value.age_le32),(500,30))

    def test_short_basis_overlap_can_publish_a_stored_window_covering_the_vehicle(self):
        limits = StoppingLimits(.5,1.,10_000,50_000,30_000,.005)
        points = lambda x: ((x,.3),(x,0.),(x,-.3))
        basis = self.state.road_basis
        basis._hold(self.model,.05,'OVERLAP_SPAN_UNRESOLVED')
        held = basis.snapshot()
        self.assertIsNone(self.build(basis=held,stopping=limits))
        self.pipeline._temporal_road._store(tuple(
            RoadLink(points(a),points(b),.001,frozenset(),(frozenset(),)*3,False,.002)
            for a,b in ((-.5,0.),(0.,.5),(.5,1.))))
        self.pipeline._temporal_road.revision += 1
        built = self.build(basis=held,stopping=limits)
        self.assertIsNotNone(built)
        value = built.message()
        self.assertLess(value.lane_model.divider[0].x,value.reference_vehicle_pose.x)
        self.assertEqual(value.reference_revision,held.reference_revision)

    def test_demo_fill_is_only_in_the_published_world(self):
        self.pipeline.targets = (TargetClass(.09, .18, .1273),)
        def station(x):
            return ((x,.3),(x,0.),(x,-.3))
        self.pipeline._temporal_road._store(tuple(
            RoadLink(station(a),station(b),.001,frozenset(),(frozenset(),)*3,False,.002)
            for a,b in ((-.5,0.),(0.,.5),(.5,1.))))
        stopping = StoppingLimits(.5,1.,10_000,50_000,30_000,.005)
        ordinary = self.build(stopping=stopping).message()
        demo = self.build(stopping=stopping,demo_road_fill=True).message()
        self.assertEqual(set(ordinary.occupancy), {255})
        self.assertEqual(set(demo.occupancy), {0,255})
        self.assertEqual(ordinary.drivable_permission, demo.drivable_permission)
        self.assertTrue((self.grid.state == UNKNOWN).all())
        fixed = self.build(stopping=stopping,demo_road_fill=True,demo_pose_bounds=(.001,.002)).message()
        self.assertAlmostEqual(fixed.localization.anchor_position_bound_m,.001,places=7)
        self.assertAlmostEqual(fixed.localization.anchor_heading_bound_rad,.002,places=7)
        self.assertEqual(fixed.reference_vehicle_pose,demo.reference_vehicle_pose)
        self.assertEqual(fixed.localization.valid_for_us,demo.localization.valid_for_us)

    def test_corrected_capture_is_carried_to_the_original_vehicle_sample(self):
        raw = self.inputs.pose.sample
        vehicle = self.vehicle.pose_sample
        mapped = replace(raw,x=raw.x+.02,y=raw.y-.01,yaw=raw.yaw+.025,
                         position_error_m=.018,yaw_error_rad=.02)
        result = replace(self.draft.result,mapped_odometry=replace(self.inputs.pose,sample=mapped),
                         yaw_bias={'applied':True})
        value = self.build(draft=replace(self.draft,result=result)).message()
        dx,dy = vehicle.x-raw.x,vehicle.y-raw.y
        c,s = math.cos(.025),math.sin(.025)
        self.assertAlmostEqual(value.reference_vehicle_pose.x,mapped.x+c*dx-s*dy,places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.y,mapped.y+s*dx+c*dy,places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.yaw,vehicle.yaw+.025,places=7)
        self.assertEqual(value.reference_odom_seq,self.vehicle.anchor.sequence)
        self.assertEqual(value.localization.observed_mapper_us,1_000_000)
        self.assertEqual(value.localization.valid_for_us,100_000)
        self.assertGreaterEqual(value.localization.anchor_position_bound_m,.018)

    def test_uncorrected_capture_uses_the_same_earlier_anchor_as_the_grid(self):
        latest = replace(self.alignment, position_error_m=.045, yaw_error_rad=.01)
        earlier = replace(latest, pose=(latest.pose[0]-.025, *latest.pose[1:]),
                          position_error_m=.035, yaw_error_rad=.002,
                          registration_revision=latest.registration_revision-1)
        self.pipeline._alignment = latest
        self.pipeline._propagation_anchor = earlier
        _, mapped_capture = self.pipeline._propagate_odometry(self.inputs.pose)
        self.assertEqual(mapped_capture, earlier.apply(self.inputs.pose))
        bias = self.pipeline._yaw_bias.summary()
        self.assertFalse(bias['applied'])
        summary = dict(self.draft.result.summary, alignment_metadata=latest.fields())
        result = replace(self.draft.result, summary=summary, mapped_odometry=mapped_capture, yaw_bias=bias)
        value = self.build(draft=replace(self.draft, result=result)).message()
        wire_sample = PoseEstimate(self.vehicle.pose_sample, self.vehicle.j_interval, self.vehicle.j_interval)
        expected = earlier.apply(wire_sample).sample
        self.assertAlmostEqual(latest.apply(wire_sample).sample.x-expected.x, .025, places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.x, expected.x, places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.y, expected.y, places=7)
        self.assertAlmostEqual(value.reference_vehicle_pose.yaw, expected.yaw, places=7)
        self.assertAlmostEqual(value.localization.anchor_position_bound_m, expected.position_error_m, places=7)
        self.assertEqual(value.reference_odom_seq, self.vehicle.anchor.sequence)
        self.assertEqual(value.localization.registration_revision, latest.registration_revision)
        self.assertEqual(value.localization.valid_for_us, latest.valid_for_us)

    def test_missing_capture_pose_holds_world_publication(self):
        result = replace(self.draft.result, mapped_odometry=None)
        self.assertIsNone(self.build(draft=replace(self.draft, result=result)))

    def test_original_alignment_expiry_and_completion_order_cannot_be_renewed(self):
        self.assertIsNone(self.build(published_j_us=self.alignment.observed_j_interval[0]+self.alignment.valid_for_us))
        self.assertIsNone(self.build(completed_j_us=self.now-1))
        self.assertIsNone(self.build(published_j_us=self.item.expires_us))
        self.assertEqual(self.alignment.valid_for_us, 100_000)

    def test_original_alignment_and_capture_uncertainty_are_both_covered_without_new_lease(self):
        original = replace(self.alignment, observed_j_interval=(990_000, 991_000))
        self.pipeline._alignment = original
        result = replace(self.draft.result, summary=dict(self.draft.result.summary, alignment_metadata=original.fields()))
        value = self.build(draft=replace(self.draft, result=result)).message()
        self.assertEqual(value.localization.timestamp_error_us, 500)
        self.assertEqual(value.localization.observed_mapper_us, 990_500)
        self.assertEqual(value.localization.valid_for_us, original.valid_for_us)

    def test_current_vehicle_admission_is_required_instead_of_capture_or_old_anchor(self):
        previous = self.fixture.state.original_pose_bracket(self.inputs.pose).originals[0].evidence
        self.assertIsNone(self.build(current_vehicle=previous))
        self.assertIsNone(self.build(current_vehicle=replace(self.vehicle)))
        self.assertIsNotNone(self.build())

    def test_unconfigured_or_old_lane_and_waiting_basis_are_not_publications(self):
        for status in ('UNCONFIGURED', 'UNKNOWN', 'AMBIGUOUS'):
            state = replace(self.state, view=replace(self.state.view, lane_status=status))
            draft = replace(self.draft, catalog_draft=replace(self.draft.catalog_draft, state=state))
            with self.subTest(status=status): self.assertIsNone(self.build(draft=draft, state=state))
        waiting = replace(self.basis, status='WAIT_CURRENT')
        state = replace(self.state, view=replace(self.state.view, road_basis=waiting))
        draft = replace(self.draft, catalog_draft=replace(self.draft.catalog_draft, state=state))
        self.assertIsNone(self.build(draft=draft, state=state, basis=waiting))
        next_source = replace(self.model.source, stamp=replace(self.model.source.stamp, sequence=13))
        self.state.lane.observe(replace(self.model.road, status='UNKNOWN', hypotheses=(), complete=False,
            source_key=next_source.stamp.key), next_source,
            PoseBox((0., 0.), (0., 0.), (0., 0.)), prior_qualified=True, crossing=b'')
        self.assertIsNone(self.build())

    def test_missing_clock_source_change_and_nonlive_input_do_not_gain_operating_output(self):
        self.assertIsNone(self.build(clock_lease=None))
        self.assertIsNone(self.build(source=dict(self.source, mapper_instance=uid(19))))
        for kind in ('SYNTHETIC', 'REPLAY'):
            result = replace(self.draft.result, summary=dict(self.draft.result.summary, kind=kind))
            with self.subTest(kind=kind): self.assertIsNone(self.build(draft=replace(self.draft, result=result)))
        for field, value in (('localization_qualified', False), ('quarantined', True)):
            result = replace(self.draft.result, summary=dict(self.draft.result.summary, **{field: value}))
            with self.subTest(field=field): self.assertIsNone(self.build(draft=replace(self.draft, result=result)))

    def test_no_current_road_cell_and_stale_candidate_state_are_not_ready(self):
        result = replace(self.draft.result, map_snapshot=dict(self.draft.result.map_snapshot,
            spec=GridSpec(4, 4, .1, (10., 10.))))
        self.assertIsNone(self.build(draft=replace(self.draft, result=result)))
        self.assertIsNone(self.build(state=replace(self.state)))
        result = replace(self.draft.result, map_snapshot=dict(self.draft.result.map_snapshot, revision=999))
        self.assertIsNone(self.build(draft=replace(self.draft, result=result)))

    def test_existing_parking_inventory_keeps_partial_status_and_age_without_scene_claim(self):
        baseline = self.build().message()
        self.assertEqual(baseline.required_features, 11)
        self.assertFalse(baseline.HasField('parking_set'))
        # 명시한 원 publication DTO의 adapter 경계다. 빈 inventory를 COMPLETE로 승격하지 않는다.
        meta = schema.ParkingSetMeta(revision=1, complete=False, track_count_in_roi=0,
            capacity_limited=False, evidence_sequence=0, state=2)
        inventory = ParkingPublication((), meta.SerializeToString(deterministic=True), 0, 'LIVE',
            self.source['clock_epoch'], self.context)
        state = replace(self.state, view=replace(self.state.view, parking=inventory))
        draft = replace(self.draft, catalog_draft=replace(self.draft.catalog_draft, state=state))
        built = self.build(draft=draft, state=state)
        self.assertEqual(built.payload, encode(inventory.apply(baseline)))
        value = built.message()
        self.assertEqual(value.required_features, 15)
        self.assertEqual(value.parking_set, meta)
        self.assertFalse(value.HasField('scene'))


if __name__ == '__main__':
    unittest.main()
