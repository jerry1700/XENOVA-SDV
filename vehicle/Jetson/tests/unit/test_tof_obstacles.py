"""실제 수락 ToF·odom·World codec으로 HIT-only 보조 관측을 확인한다.

보정값과 영상 지도는 software fixture이며 실제 센서 범위·장착 수락을 주장하지 않는다.
"""

from dataclasses import replace
import unittest
from uuid import UUID

import numpy as np

import test_mapper_world as world_fixture
import test_sensor_samples as sensor_fixture
from xenova_drive_world import encode
from xenova_vehicle.perception.tof import tof_evidence
from xenova_vehicle.runtime.tof_obstacles import TofObstacleOverlay


class TofObstacleTests(unittest.TestCase):
    def setUp(self):
        self.f = world_fixture.MapperWorldTests('runTest')
        self.f.setUp()
        self.overlay = TofObstacleOverlay()
        self.snapshot = self.f.draft.result.map_snapshot
        self.frozen = self.freeze()

    def freeze(self):
        f = self.f
        return f.fixture.state.frame_snapshot(f.draft.frame.observation.rgb.stamp, now_us=f.now,
            expires_us=f.item.expires_us, job_id=f.item.job_id,
            source_generation=f.item.source_generation, source_key=f.item.source_key)

    def apply(self, *, snapshot=None, frozen=None, now=None, alignment=None, static=None):
        return self.overlay.apply(self.snapshot if snapshot is None else snapshot,
            sensor_snapshot=self.frozen if frozen is None else frozen,
            alignment=self.f.alignment if alignment is None else alignment,
            snapshot_us=self.f.now, now_us=self.f.now if now is None else now, static_retained=static)

    def test_actual_world_contains_hits_without_mutating_camera_grid_or_authority(self):
        before = self.f.grid.state.copy()
        value = self.f.build(tof_overlay=self.overlay, sensor_snapshot=self.frozen).message()
        self.assertIn(100, value.occupancy)
        self.assertNotIn(0, value.occupancy)
        np.testing.assert_array_equal(self.f.grid.state, before)
        self.assertEqual(value.observed_us, sum(self.f.draft.frame.observation.capture_interval)//2)
        self.assertEqual((value.sequence, value.reference_odom_seq), (901, self.f.vehicle.anchor.sequence))

    def test_no_sensor_snapshot_leaves_normal_world_unchanged(self):
        plain = self.f.build().payload
        self.assertEqual(self.f.build(tof_overlay=self.overlay).payload, plain)

    def test_rear_mount_measures_behind_the_vehicle_without_claiming_any_free_space(self):
        f = sensor_fixture.SensorSampleTests('runTest')
        f.setUp()
        rear = replace(f.leaf, rotation=((0.,0.,-1.),(1.,0.,0.),(0.,-1.,0.)), translation=(-.15,0.,.1))
        f.leaf = rear
        f.prepare(rear)
        poses = f.pose()
        evidence = f.owner.accept_tof(encode(f.tof()),f.lease,f.now)
        result = tof_evidence(evidence,rear,f.calibration,admission=f.owner,
                              now_us=f.now,pose=poses,motion_qualified=True)
        self.assertEqual(result.status,'HIT_ONLY')
        self.assertLess(result.hit_boxes[0].upper[0],-1.)
        self.assertEqual(len(result.patches),0)

    def test_serial_sample_gateway_codec_and_jetson_world_use_the_same_observation(self):
        import struct
        from xenova_gateway.tof_feedback3 import TOF_SAMPLE3,TofClock,TofInbox,TofReceiver,TofSelection
        f = self.f.fixture
        sample = f.fixture.tof()
        pi, device, epoch = map(lambda text: UUID(text).bytes,
                                (sample.pi_session, sample.device_session, sample.clock_epoch))
        payload = TOF_SAMPLE3.pack(pi,device,sample.source_sample,sample.source_us,sample.sensor_time_error_us,
            epoch,sample.clock_seq,sample.sensor_id,sample.sensor_instance,sample.sensor_generation,
            sample.sensor_config_revision,bytes.fromhex(sample.calibration),1,1,sample.frame_status,
            struct.unpack('<H',sample.distance_mm_le)[0],sample.status[0],sample.quality[0],sample.source_crc32)
        receiver = TofReceiver(TofSelection(((0,sample.calibration,sample.sensor_config_revision),),
            200_000,1000,sample.vehicle,sample.host_boot,sample.gateway_instance))
        inbox = TofInbox()
        clock = TofClock(epoch,sample.clock_seq,699990,700010,1_100_000,2_000_000,0)
        self.assertTrue(inbox.offer(payload,1_125_000,clock))
        loss,items = inbox.drain()
        raws,_ = receiver.process(loss,items,(pi,device,epoch,True),1_150_000)
        self.assertEqual(len(raws),1)
        snapshot = f.snapshot()
        snapshot['tof'] = {'0':f.row(raws[0])}
        owner = f.owner()
        f.read(snapshot,owner=owner)
        frozen = owner.frame_snapshot(f.stamp,now_us=f.now,expires_us=self.f.item.expires_us,
            job_id=self.f.item.job_id,source_generation=self.f.item.source_generation,source_key=self.f.item.source_key)
        result,_ = self.apply(frozen=frozen)
        value = self.f.build(draft=replace(self.f.draft,result=replace(self.f.draft.result,map_snapshot=result))).message()
        self.assertIn(100,value.occupancy)
        evidence = frozen.sensors.latest_tof(0)
        self.assertEqual((evidence.source_sample,evidence.source_us,evidence.source_crc32),
                         (sample.source_sample,sample.source_us,sample.source_crc32))

    def test_hit_never_grants_permission_or_static_retained_membership(self):
        static = np.ones((20, 20), dtype=bool)
        result, retained = self.apply(static=static)
        hits = result['arrays']['state'] == 2
        self.assertTrue(hits.any())
        self.assertFalse(result['arrays']['permission'][hits].any())
        self.assertFalse(retained[hits].any())
        self.assertTrue(static.all())
        np.testing.assert_array_equal(result['arrays']['support'], self.snapshot['arrays']['support'])

    def test_same_observation_does_not_refresh_age_or_original_expiry(self):
        first, _ = self.apply()
        second, _ = self.apply(now=self.f.now+20_000)
        hits = first['arrays']['state'] == 2
        np.testing.assert_array_equal(second['arrays']['age_ms'][hits], first['arrays']['age_ms'][hits]+20)
        expires = max(e.valid_until_j_us for _, e in self.frozen.sensors._tofs if e is not None)
        expired, _ = self.apply(now=expires)
        self.assertFalse((expired['arrays']['state'] == 2).any())

    def test_expired_hit_becomes_unknown_instead_of_exposing_old_free_map(self):
        arrays = dict(self.snapshot['arrays'], state=np.ones((20, 20), dtype=np.uint8),
                      age_ms=np.full((20, 20), 500, dtype=np.uint32))
        snapshot = dict(self.snapshot, arrays=arrays)
        first, _ = self.apply(snapshot=snapshot)
        hit = first['arrays']['state'] == 2
        expires = max(e.valid_until_j_us for _, e in self.frozen.sensors._tofs if e is not None)
        result, _ = self.apply(snapshot=snapshot, now=expires+1)
        self.assertTrue((result['arrays']['state'][hit] == 0).all())
        self.assertTrue((snapshot['arrays']['state'] == 1).all())

    def test_new_camera_observation_can_clear_old_tof_shadow(self):
        first, _ = self.apply()
        hits = first['arrays']['state'] == 2
        snapshot = dict(self.snapshot, arrays=dict(self.snapshot['arrays'],
            state=np.ones((20, 20), dtype=np.uint8), age_ms=np.zeros((20, 20), dtype=np.uint32)))
        result, _ = self.apply(snapshot=snapshot)
        self.assertTrue((result['arrays']['state'][hits] == 1).all())
        # 같은 옛 표본을 새 프레임에서 다시 관측한 것처럼 살리지 않는다.
        self.assertFalse((self.apply(snapshot=snapshot)[0]['arrays']['state'] == 2).any())

    def test_pose_at_tof_time_is_required_and_missing_bracket_only_skips_tof(self):
        frozen = self.freeze()
        object.__setattr__(frozen, 'vehicle_admissions', ())
        result, _ = self.apply(frozen=frozen)
        self.assertIs(result, self.snapshot)
        self.assertIsNotNone(self.f.build(tof_overlay=self.overlay, sensor_snapshot=frozen))

    def test_older_tof_than_registration_keeps_original_time_and_conservative_map_transform(self):
        alignment = replace(self.f.alignment, observed_j_interval=(1_020_000, 1_020_100))
        result, _ = self.apply(alignment=alignment)
        self.assertTrue((result['arrays']['state'] == 2).any())
        self.assertGreater(result['arrays']['age_ms'][result['arrays']['state'] == 2].min(), 0)

    def test_sensor_reset_with_no_valid_return_retires_old_active_hit(self):
        first, _ = self.apply()
        hits = first['arrays']['state'] == 2
        f = self.f.fixture
        snapshot = f.snapshot()
        for sensor, row in snapshot['tof'].items():
            leaf = f.leaves[int(sensor)][1]
            sample = f.fixture.tof(sensor_id=int(sensor), calibration=leaf.sha256,
                source_sample=1, sensor_generation=2, sensor_instance=UUID(int=100+int(sensor)).bytes,
                distance_mm_le=b'\0\0', status=b'\0', quality=b'\0')
            snapshot['tof'][sensor] = f.row(encode(sample))
        f.read(snapshot)
        result, _ = self.apply(frozen=self.freeze())
        self.assertTrue((result['arrays']['state'][hits] == 0).all())

    def test_same_lattice_window_move_preserves_hit_and_new_epoch_drops_old_frame(self):
        first, _ = self.apply()
        spec = self.snapshot['spec']
        moved = replace(spec, origin_xy=(spec.origin_xy[0]+spec.resolution_m, spec.origin_xy[1]))
        snapshot = dict(self.snapshot, spec=moved)
        result, _ = self.apply(snapshot=snapshot)
        np.testing.assert_array_equal(result['arrays']['state'][:, :-1], first['arrays']['state'][:, 1:])
        other = dict(snapshot, frame_id='odom:new-epoch')
        # 좌표계 변경 뒤 이전 관측을 새 좌표계의 입력으로 재사용하지 않는다.
        empty = self.freeze()
        object.__setattr__(empty.sensors, '_tofs', ((0, None), (1, None)))
        result, _ = self.apply(snapshot=other, frozen=empty)
        self.assertIs(result, other)


if __name__ == '__main__':
    unittest.main()
