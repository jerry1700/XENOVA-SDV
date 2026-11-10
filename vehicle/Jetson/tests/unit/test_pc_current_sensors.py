"""EARLY pose와 현재 Vehicle/R1 분리를 검사하는 명시 software 시각 fixture다.

기존 원 Vehicle codec/admission/PoseBuffer와 PC clock factory를 사용한다. 실제
PC 네트워크, 센서, P4, SDK, World/Scene 또는 주행 수락을 주장하지 않는다.
"""

from base64 import b64encode
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import threading
import unittest
from unittest.mock import patch

from test_simulation_motion import MotionFixture, identifier
from tools.hil_observation.clock import ClockSource, ClockPolicy, HilObservationClock, reply
from tools.hil_observation.mapped import MappedPcOwner
from tools.hil_observation.transport import SessionScope, ObservationFrame, ReceivedPacket
from xenova_drive_world import decode, encode, schema
from xenova_drive_world.mapper_clock import ClockLease
from xenova_vehicle.runtime.pc_current_sensors import (PcCurrentSensors,
    PcCurrentSensorsNotReady, MAX_PC_CURRENT_SENSOR_BYTES)
from xenova_vehicle.simulation.motion import DevelopmentMotion
from xenova_vehicle.simulation.pc_input import PcSourceBinding
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.sensor_samples import SensorContext


class PcCurrentSensorsTests(unittest.TestCase):
    def setUp(self):
        f=self.f=MotionFixture();uid=identifier
        self.binding=PcSourceBinding(dict(profile='development-pc-source-1',
            service_instance=f.raw.fields['service_instance'],source_id=uid(601),pc_clock_epoch=uid(602),
            jetson_session=uid(603),stream_instances=dict(rgb_video=uid(604),depth_right=uid(605)),
            destination_clock_epoch=f.source['clock_epoch'],calibration_profile_sha256=f.profile.bound_calibration.sha256,
            motion_profile_sha256=f.profile.sha256,nn_manifest_sha256='e'*64))
        clock=HilObservationClock(ClockSource(uid(601),uid(602),uid(603)),
            ClockPolicy(max_rtt_us=100,max_offset_width_us=32,drift_ppm=0,lease_us=1_000_000,lookback_us=500_000))
        for sequence,j1 in ((1,990_000),(2,990_100)):
            original=clock.probe(sequence,sequence.to_bytes(16,'big'),j1,j1+200)
            clock.accept(reply(original,j1-996,j1-995),j1+10)
        scope=SessionScope(uid(601),uid(603),self.binding.sha256,f.profile.bound_calibration.sha256,
            f.profile.sha256,uid(602))
        self.pc_owner=MappedPcOwner(self.binding,scope,clock,destination_clock_id=f.sensor_context.pose_clock_id)
        observation=f.observation()
        packet=ObservationFrame(scope,observation.rgb.stamp.sequence,rgb_pc_interval=(998_805,999_095),
            depth_pc_interval=(998_905,999_195),render_completed_pc_us=999_300,expires_pc_us=1_499_000,
            rgb=observation.rgb.payload,depth=observation.depth.payload)
        self.frame=self.pc_owner.accept_frame(ReceivedPacket(packet,f.now),now_j_us=f.now)
        f.binding=dict(f.binding,raw_binding_sha256=self.binding.sha256)
        f.context=replace(f.context,source_binding=canonical(f.binding))
        self.motion=self.new_owner(f.sensor_context);self.owner=self.motion.sensor_state
        self.context=canonical(f.snapshot());self.motion.ingest(self.context,now_us=f.now)
        self.early=self.take_snapshot()

    def new_owner(self,context):
        f=self.f
        return DevelopmentMotion.for_pc_source(f.profile,self.binding,f.policy,sensor_context=context,
            mapper_worker=f.mapper_worker,mapper_instance=f.source['mapper_instance'],
            pc_owner_token=self.pc_owner.owner_token)

    def take_snapshot(self):
        f=self.f
        return self.motion.frame_snapshot_pc(self.frame,
            current_pc_view=self.pc_owner.current_view(self.frame,now_j_us=f.now),now_us=f.now,
            expires_j_us=self.frame.expires_j_us,job_id=identifier(699),source_generation=self.binding.sha256,
            source_key=self.frame.observation.rgb.stamp.key)

    def fresh_vehicle(self,*,count=1,expires_pi_us=2_300_000):
        f=self.f;probe=decode(f.probe,schema.MapperClockProbe)
        probe.probe_seq+=1;probe.pi_send_us=1_370_000;probe.mapper_trust.expires_pi_us=expires_pi_us
        raw=encode(probe);key=sha256(raw).hexdigest();value=f.snapshot()
        value['probes'][key]=dict(raw=b64encode(raw).decode('ascii'),received_j_us='1270000',clock_probe_sha256=key)
        value['active_probe_sha256']=key
        # 실제 main ingest를 계속하는 software 입력이다. 원 EARLY 시각/Vehicle은 바꾸지 않는다.
        for index in range(count):
            at=1_390_000+index*100
            wire=f.vehicle(index+3,at,.003)
            value['vehicles'].append(dict(raw=b64encode(wire).decode('ascii'),
                received_j_us=str(1_295_000+index*100),clock_probe_sha256=key))
        now=1_300_000+count*100
        self.context=canonical(value);self.motion.ingest(self.context,now_us=now)
        return now

    def current(self,now,**changes):
        arguments=dict(now_us=now,source_context_bytes=self.context,fence_revision=self.owner.fence_revision)
        arguments.update(changes)
        return PcCurrentSensors.from_owner(self.owner,self.early,**arguments)

    def validate(self,current,now,**changes):
        arguments=dict(owner_token=self.pc_owner.owner_token,fence_revision=self.early.fence_revision,now_us=now)
        arguments.update(changes)
        return current.validate_current(self.early,**arguments)

    def test_late_nn_uses_new_current_heads_without_requerying_early_capture_or_clock(self):
        early_inputs=self.early.metric_inputs;bracket=self.early.original_pose_bracket(early_inputs.pose)
        now=self.fresh_vehicle()
        self.assertFalse(self.early.sensors.current_vehicle(early_inputs.vehicle,now))
        original_clock_method=ClockLease.pi_interval_to_j
        def current_only(lease,interval,at):
            if lease is early_inputs.clock_lease:raise AssertionError('old capture clock remapped at NN completion')
            return original_clock_method(lease,interval,at)
        with (patch.object(self.owner.poses,'at',side_effect=AssertionError('late pose query')),
                patch.object(ClockLease,'pi_interval_to_j',current_only)):
            current=self.current(now);self.assertIs(self.validate(current,now),current)
        self.assertIs(current.early_snapshot,self.early)
        self.assertIs(self.early.metric_inputs.pose,early_inputs.pose)
        self.assertIs(self.early.original_pose_bracket(early_inputs.pose),bracket)
        self.assertIs(self.early.metric_inputs.clock_lease,early_inputs.clock_lease)
        self.assertIs(current.vehicle,self.owner.sensors.latest_vehicle)
        self.assertIs(current.vehicle_admission,self.owner.original_vehicle_admission(current.vehicle))
        self.assertIsNot(current.vehicle,early_inputs.vehicle)
        self.assertIsNot(current.clock_lease,early_inputs.clock_lease)
        self.assertIsNone(current.clock_lease.completed_pi_us)
        self.assertEqual(current.expires_us,self.frame.expires_j_us)
        self.assertLessEqual(current.byte_length,MAX_PC_CURRENT_SENSOR_BYTES)
        self.assertFalse(hasattr(current,'rgb'));self.assertFalse(hasattr(current,'source_context_bytes'))
        copy=current.source;copy['vehicle']='foreign';self.assertEqual(current.source,self.f.source)
        with self.assertRaises(FrozenInstanceError):current.vehicle=early_inputs.vehicle
        with self.assertRaises(TypeError):PcCurrentSensors()

    def test_final_age_clock_frame_deadline_and_foreign_tokens_remain_strict(self):
        now=self.fresh_vehicle();current=self.current(now)
        self.assertIs(self.validate(current,current.vehicle.valid_until_j_us-1),current)
        for at in (current.vehicle.valid_until_j_us,current.expires_us-1,current.expires_us,now-1,True):
            with self.subTest(at=at),self.assertRaises(ValueError):self.validate(current,at)
        with self.assertRaises(ValueError):self.validate(current,now,owner_token=object())
        with self.assertRaises(ValueError):self.validate(current,now,fence_revision=current.fence_revision+1)
        # 원 clock은 actual current R1의 정해진 window에서만 쓴다. E를 갱신하지 않는다.
        self.assertEqual(current.clock_lease.raw_probe_bytes,self.owner._active.raw_probe_bytes)
        self.assertLess(current.checked_j_us,current.vehicle.valid_until_j_us)

    def test_current_clock_expiry_is_independent_of_early_clock_and_vehicle_age(self):
        now=self.fresh_vehicle(expires_pi_us=1_450_000);current=self.current(now)
        # 원 trust upper=100010이므로 current R1의 strict J 상한은1349990이다.
        boundary=1_450_000-100_010
        self.assertLess(boundary,current.vehicle.valid_until_j_us)
        self.assertLess(boundary,self.early.expires_us)
        self.assertIs(self.validate(current,boundary-1),current)
        with self.assertRaises(ValueError):self.validate(current,boundary)
        self.assertEqual(current.clock_lease.expires_pi_us,1_450_000)
        self.assertEqual(self.early.metric_inputs.clock_lease.expires_pi_us,2_000_000)

    def test_original_snapshot_identity_current_registry_and_context_ownership_are_closed(self):
        foreign_snapshot=self.take_snapshot();now=self.fresh_vehicle(count=17)
        current=self.current(now)
        # EARLY admission은 정상 capacity eviction 뒤에도 원 snapshot 안에서만 역사로 남는다.
        old=self.early.metric_inputs.vehicle
        with self.assertRaises(ValueError):self.owner.original_vehicle_admission(old)
        self.assertIs(self.early.original_vehicle_admission(old).evidence,old)
        self.assertIs(self.validate(current,now),current)
        with self.assertRaises(ValueError):current.validate_current(foreign_snapshot,
            owner_token=self.pc_owner.owner_token,fence_revision=current.fence_revision,now_us=now)
        clone=SensorContext.for_development(self.f.profile,self.f.source,**self.f.origin)
        other=self.new_owner(clone);other.ingest(self.context,now_us=now)
        with self.assertRaisesRegex(PcCurrentSensorsNotReady,'context/calibration'):
            PcCurrentSensors.from_owner(other.sensor_state,self.early,now_us=now,
                source_context_bytes=self.context,fence_revision=current.fence_revision)
        with self.assertRaises(ValueError):self.current(now,source_context_bytes=canonical(self.f.snapshot()))
        self.motion.ingest(b'',now_us=now+1)
        with self.assertRaises(ValueError):self.current(now+1,source_context_bytes=b'')

    def test_factory_requires_main_owner_and_unavailable_current_vehicle_is_not_promoted(self):
        with self.assertRaises(PcCurrentSensorsNotReady):self.current(1_300_000)
        self.assertIsNotNone(self.early.metric_inputs.pose)
        failures=[]
        def foreign_thread():
            try:self.current(self.f.now)
            except BaseException as error:failures.append(error)
        thread=threading.Thread(target=foreign_thread);thread.start();thread.join(1.)
        self.assertFalse(thread.is_alive());self.assertEqual(len(failures),1)
        self.assertIsInstance(failures[0],RuntimeError)
        with self.assertRaises(ValueError):PcCurrentSensors.from_owner(object(),self.early,
            now_us=self.f.now,source_context_bytes=self.context,fence_revision=0)
