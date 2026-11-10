"""원 single-return wire/시계·sensor source와 HIT 기하 반례. 보정/자격은 software fixture다."""

from dataclasses import replace
import math
import struct
import unittest
from uuid import UUID,uuid4
import zlib

from xenova_drive_world import schema,encode
from xenova_drive_world.mapper_clock import ClockPolicy,restore_returned_probe
from xenova_vehicle.mapping.odometry import PoseBuffer
from xenova_vehicle.perception.calibration import CalibrationProfile,synthetic_calibration
from xenova_vehicle.perception.tof import TofLeaf,tof_evidence
from xenova_vehicle.transport.sensor_samples import SensorContext,SensorSamples,SampleRejected,tof_crc32


def identity():return str(uuid4())


def source_crc(sample):
    # 제품 Struct를 재사용하지 않는 원141B 순서 oracle.
    data=UUID(sample.pi_session).bytes+UUID(sample.device_session).bytes
    for value,size in ((sample.source_sample,8),(sample.source_us,8),(sample.sensor_time_error_us,4)):
        data+=value.to_bytes(size,'little')
    data+=UUID(sample.clock_epoch).bytes+sample.clock_seq.to_bytes(8,'little')+bytes((sample.sensor_id,))+sample.sensor_instance
    data+=sample.sensor_generation.to_bytes(4,'little')+sample.sensor_config_revision.to_bytes(4,'little')+bytes.fromhex(sample.calibration)
    data+=bytes((sample.rows,sample.cols))+sample.frame_status.to_bytes(2,'little')
    distances=struct.unpack('<H',sample.distance_mm_le)
    for distance,status,quality in zip(distances,sample.status,sample.quality):data+=distance.to_bytes(2,'little')+bytes((status,quality))
    assert len(data)==141
    return zlib.crc32(data)


class SensorSampleTests(unittest.TestCase):
    def setUp(self):
        self.now=1_050_000
        self.leaf=TofLeaf(0,1,'EXPLICIT_SOFTWARE_FIXTURE','f'*64,((0.,0.,1.),(-1.,0.,0.),(0.,-1.,0.)),(.15,0.,.1),
            ((0.,0.,0.,0.),),'AXIAL_Z',.1,3.,.001,extrinsics_qualified=True,time_qualified=True)
        self.pi_boot,self.gateway,self.pi_session,self.device=identity(),identity(),identity(),identity()
        self.instance=UUID(identity()).bytes;self.sensor_clock=identity()
        self.source=dict(vehicle='rc-car',device='oak-front',mapper_boot=identity(),mapper_instance=identity(),clock_epoch=identity(),
            map_epoch=identity(),frame_epoch=identity(),odom_epoch=identity(),configuration='explicit-config',calibration='a'*64,primitive='b'*64)
        self.prepare(self.leaf)

    def prepare(self,leaf):
        raw=synthetic_calibration().to_dict();raw.update(kind='LIVE',vehicle_bundle_sha256='a'*64,
            provenance=dict(measured=False,purpose='SOFTWARE_TEST_ONLY'),external_leaves={'front-test-leaf':leaf.sha256})
        self.calibration=CalibrationProfile.from_dict(raw)
        self.context=SensorContext(self.calibration,self.source,pi_host_boot=self.pi_boot,gateway_instance=self.gateway,
            pi_session=self.pi_session,device_session=self.device)
        self.owner=SensorSamples(self.context,tof_leaves={0:('front-test-leaf',leaf)},vehicle_max_age_us=200_000,tof_max_age_us=200_000,
            max_speed_mps=.5,max_yaw_rate_rps=2.)
        policy=ClockPolicy(vehicle=self.source['vehicle'],device=self.source['device'],configuration=self.source['configuration'],
            calibration=self.source['calibration'],primitive=self.source['primitive'],timestamp_error_us=0,drift_ppm=0,
            max_offset_width_us=100,lease_us=1_000_000,vehicle_max_age_us=200_000,lookback_us=500_000)
        trust=schema.WorldTrust(**{k:self.source[k] for k in self.source if k!='device'},lower_offset_us=99990,upper_offset_us=100010,
            expires_pi_us=2_000_000,max_error_us=100)
        probe=schema.MapperClockProbe(major=1,vehicle=self.source['vehicle'],device=self.source['device'],pi_host_boot=self.pi_boot,
            pi_client_instance=identity(),probe_seq=4,nonce=b'n'*16,pi_send_us=1_100_000,admission_revision=7,
            expected_mapper_boot=self.source['mapper_boot'],expected_mapper_instance=self.source['mapper_instance'],expected_clock_epoch=self.source['clock_epoch'])
        probe.mapper_trust.CopyFrom(trust)
        self.lease=restore_returned_probe(encode(probe),policy,self.source,self.pi_boot,self.now)

    def vehicle(self,sequence=1,pi_us=1_090_000,x=0.):
        value=schema.VehicleSample(major=1,vehicle=self.source['vehicle'],host_boot=self.pi_boot,device_session=self.device,odom_epoch=self.source['odom_epoch'],
            sequence=sequence,source_sample=sequence+10,source_us=400000+sequence*20000,pi_sample_us=pi_us,clock_error_us=200,left_ticks=sequence*10,right_ticks=sequence*10,
            velocity_mps=.1,position_error_m=.005,heading_error_rad=.01,quality=3,calibration=self.source['calibration'],gateway_instance=self.gateway,pi_session=self.pi_session)
        value.odom_pose.CopyFrom(schema.Pose(x=x,y=0.,yaw=0.))
        return value

    def tof(self,**changes):
        value=schema.TofObservation(major=1,vehicle=self.source['vehicle'],host_boot=self.pi_boot,gateway_instance=self.gateway,pi_session=self.pi_session,
            device_session=self.device,sensor_id=0,source_sample=7,source_us=400000,pi_sample_us=1_100_000,clock_error_us=200,sensor_time_error_us=100,
            calibration=self.leaf.sha256,sensor_config_revision=1,rows=1,cols=1,distance_mm_le=struct.pack('<H',1000),
            status=bytes((1,)),quality=bytes((11,)),frame_status=7,source_crc32=0,clock_epoch=self.sensor_clock,clock_seq=5,
            complete=True,sensor_instance=self.instance,sensor_generation=1)
        for key,value0 in changes.items():setattr(value,key,value0)
        value.source_crc32=source_crc(value)
        return value

    def pose(self):
        buffer=PoseBuffer(max_speed_mps=.5,max_yaw_rate_rps=2.)
        for value in (self.vehicle(),self.vehicle(2,1_110_000,.002)):
            buffer.add(self.owner.accept_vehicle(encode(value),self.lease,self.now).pose_sample)
        return buffer

    def test_vehicle_bundle_profile_exact_anchor_and_original_interval(self):
        original=self.vehicle();raw=encode(original);accepted=self.owner.accept_vehicle(raw,self.lease,self.now)
        self.assertEqual(accepted.wire,raw);self.assertEqual(accepted.anchor.sequence,original.sequence)
        self.assertEqual(accepted.anchor.odom_pose,(original.odom_pose.x,original.odom_pose.y,original.odom_pose.yaw))
        self.assertEqual(accepted.anchor.calibration_bundle_sha256,'a'*64)
        self.assertEqual(accepted.pose_sample.calibration_id,self.calibration.sha256)
        self.assertNotEqual(self.calibration.sha256,'a'*64)
        self.assertEqual(accepted.pi_interval,(1_089_800,1_090_200));self.assertEqual(accepted.j_interval,(989790,990210))
        self.assertGreater(accepted.pose_sample.position_error_m,original.position_error_m)
        self.assertIsNone(self.owner.accept_vehicle(raw,self.lease,self.now+1))
        original.calibration=self.calibration.sha256
        with self.assertRaises(SampleRejected):self.owner.accept_vehicle(encode(original),self.lease,self.now)
        original=self.vehicle(2,1_110_000);original.odom_epoch=identity()
        with self.assertRaises(SampleRejected):self.owner.accept_vehicle(encode(original),self.lease,self.now)

    def test_source_crc_pi_integration_and_distinct_sensor_clock_are_preserved(self):
        original=self.tof();self.assertEqual(tof_crc32(original),source_crc(original))
        wire=encode(original);accepted=self.owner.accept_tof(wire,self.lease,self.now)
        self.assertEqual(accepted.pi_interval,(1_099_700,1_100_300));self.assertEqual(accepted.j_interval,(999690,1000310))
        self.assertEqual(accepted.source_crc32,original.source_crc32);self.assertEqual(accepted.source_clock_epoch,self.sensor_clock)
        self.assertEqual(accepted.stamp.clock_id,self.source['clock_epoch']);self.assertNotEqual(self.sensor_clock,accepted.stamp.clock_id)
        self.assertIsNone(self.owner.accept_tof(wire,self.lease,self.now+1000))
        self.assertIs(self.owner.latest_tof(0),accepted)
        original.source_sample+=1;original.source_crc32^=1
        with self.assertRaises(SampleRejected):self.owner.accept_tof(encode(original),self.lease,self.now)

    def test_sensor_reset_revokes_only_that_sensor_even_when_new_clock_is_unavailable(self):
        vehicle=self.owner.accept_vehicle(encode(self.vehicle()),self.lease,self.now)
        old=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        new_instance=UUID(identity()).bytes
        reset=self.tof(source_sample=1,sensor_instance=new_instance,sensor_generation=2,frame_status=1)
        with self.assertRaises(SampleRejected):self.owner.accept_tof(encode(reset),self.lease,self.now)
        self.assertIsNone(self.owner.latest_tof(0));self.assertFalse(self.owner.current_tof(old,self.now))
        event,=self.owner.take_resets();self.assertEqual(event.previous_instance,old.sensor_instance)
        self.assertEqual(event.sensor_generation,2);self.assertIs(self.owner.latest_vehicle,vehicle)
        self.assertTrue(self.owner.current_vehicle(vehicle,self.now));self.assertEqual(self.context.source['odom_epoch'],self.source['odom_epoch'])
        for invalid in (self.tof(source_sample=8),self.tof(source_sample=2,sensor_instance=UUID(identity()).bytes,sensor_generation=2),
                self.tof(source_sample=1,sensor_instance=self.instance,sensor_generation=3)):
            with self.assertRaises(SampleRejected):self.owner.accept_tof(encode(invalid),self.lease,self.now)

    def test_axial_sensor_z_accepts_horizontal_base_hit_with_actual_pose_bracket(self):
        pose=self.pose();evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now,pose=pose,motion_qualified=True)
        self.assertEqual(len(result.patches),0);self.assertTrue(result.mapping_ready)
        hit,=result.hit_boxes;self.assertTrue(hit.qualified)
        self.assertLess(hit.lower[0],1.15);self.assertGreater(hit.upper[0],1.15)
        self.assertLess(hit.lower[2],.1);self.assertGreater(hit.upper[2],.1)
        self.assertEqual(result.status,'HIT_ONLY');self.assertFalse(result.world_valid)

    def test_zone_footprint_bounds_include_interior_and_radial_convention_is_not_double_converted(self):
        self.leaf=replace(self.leaf,zone_bearings=((-0.2,.2,-.1,.1),));self.prepare(self.leaf)
        evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now)
        hit=result.hit_boxes[0];self.assertFalse(hit.qualified)
        for az in (-.2,-.1,0.,.1,.2):
            for el in (-.1,0.,.1):
                h=(math.cos(el)*math.sin(az),-math.sin(el),math.cos(el)*math.cos(az));radial=1/h[2]
                point=(.15+h[2]*radial,-h[0]*radial,.1-h[1]*radial)
                for low,value,high in zip(hit.lower,point,hit.upper):self.assertLessEqual(low,value);self.assertGreaterEqual(high,value)
        self.leaf=replace(self.leaf,distance_convention='RADIAL');self.prepare(self.leaf)
        evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now)
        self.assertLess(result.hit_boxes[0].upper[0],1.16)
        self.assertEqual(len(result.patches),0)

    def test_all_valid_quality_bits_remain_hit_only_without_any_free_patch(self):
        pose=self.pose()
        for index,quality in enumerate(range(1,32,2)):
            original=self.tof(quality=bytes((quality,)));original.source_sample+=index;original.source_us+=index*1000;original.source_crc32=source_crc(original)
            evidence=self.owner.accept_tof(encode(original),self.lease,self.now)
            result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now,pose=pose,motion_qualified=True)
            self.assertEqual(result.status,'HIT_ONLY');self.assertEqual(len(result.patches),0)

    def test_no_target_and_invalid_new_measurement_revoke_only_the_sensor_input(self):
        for changes in (dict(quality=b'\x00'),dict(status=b'\x00',quality=b'\x00'),
                dict(distance_mm_le=b'\x00\x00',status=b'\x00',quality=b'\x00'),
                dict(distance_mm_le=b'\xff\xff',status=b'\x00',quality=b'\x00')):
            with self.subTest(changes=changes):
                self.prepare(self.leaf)
                vehicle=self.owner.accept_vehicle(encode(self.vehicle()),self.lease,self.now)
                old=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
                no_hit=self.tof(source_sample=8,source_us=401000,**changes)
                with self.assertRaisesRegex(SampleRejected,'no valid single return'):
                    self.owner.accept_tof(encode(no_hit),self.lease,self.now)
                self.assertIsNone(self.owner.latest_tof(0));self.assertFalse(self.owner.current_tof(old,self.now))
                self.assertIs(self.owner.latest_vehicle,vehicle)

    def test_hit_requires_each_time_and_extrinsics_qualification(self):
        original_leaf=self.leaf
        for field in ('extrinsics_qualified','time_qualified'):
            self.leaf=replace(original_leaf,**{field:False});self.prepare(self.leaf);pose=self.pose()
            evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
            result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now,pose=pose,motion_qualified=True)
            self.assertFalse(result.mapping_ready);self.assertFalse(result.hit_boxes[0].qualified)
            self.assertEqual(len(result.patches),0)

    def test_single_leaf_roundtrip_rejects_legacy_grid_and_free_qualification_fields(self):
        self.assertEqual(TofLeaf.from_dict(self.leaf.to_dict()),self.leaf)
        self.assertEqual(self.leaf.to_dict()['schema_version'],2)
        with self.assertRaises(ValueError):replace(self.leaf,zone_bearings=((0.,0.,0.,0.),)*64)
        for fields in (dict(self.leaf.to_dict(),schema_version=1),
                dict(self.leaf.to_dict(),first_surface_qualified=True)):
            with self.assertRaises(ValueError):TofLeaf.from_dict(fields)

    def test_crc_rejects_legacy_multizone_layout_instead_of_truncating_it(self):
        legacy=self.tof();legacy.rows=legacy.cols=8
        legacy.distance_mm_le=struct.pack('<64H',*([1000]*64))
        legacy.status=b'\x01'*64;legacy.quality=b'\x0b'*64
        with self.assertRaisesRegex(SampleRejected,'exact1x1'):tof_crc32(legacy)
        with self.assertRaises(ValueError):self.owner.accept_tof(legacy.SerializeToString(),self.lease,self.now)

    def test_delayed_or_unbracketed_tof_cannot_become_free(self):
        evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now,motion_qualified=True,pose=PoseBuffer())
        self.assertFalse(result.mapping_ready);self.assertFalse(any(result.patches.qualified))
        with self.assertRaises(ValueError):tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=1_300_000)

    def test_unsupported_leaf_crc_conflict_and_sensor_axial_zero_crossing(self):
        for changes in (dict(calibration='c'*64),dict(sensor_id=1),dict(sensor_config_revision=2)):
            with self.assertRaises(SampleRejected):self.owner.accept_tof(encode(self.tof(**changes)),self.lease,self.now)
        self.leaf=replace(self.leaf,zone_bearings=((1.4,1.7,0.,0.),));self.prepare(self.leaf)
        evidence=self.owner.accept_tof(encode(self.tof()),self.lease,self.now)
        result=tof_evidence(evidence,self.leaf,self.calibration,admission=self.owner,now_us=self.now)
        self.assertEqual(len(result.patches),0);self.assertEqual(len(result.hit_boxes),0)
