"""PC mapped 원본→admitted snapshot→개발 core/SQLite의 software 경계 회귀다.

PC/J 네 시각과 Pi Vehicle/returned trust는 명시 software leaf다. NN은 owned FakeNN
tensor이며 실제 PC 네트워크·renderer·OAK·RPi/HIL/READY 또는 World 양성을 주장하지 않는다.
기존 SYNTHETIC fixture를 재사용하며 LIVE 입력의 kind·calibration을 바꾸지 않는다.
"""

from dataclasses import FrozenInstanceError, replace
import os
import unittest
from unittest.mock import patch

import test_development_mapping as mapping_fixtures
import test_simulation_motion as motion_fixtures
import test_yolop_frames as nn_fixtures
from tools.hil_observation.clock import ClockSource, ClockPolicy, HilObservationClock, reply
from tools.hil_observation.mapped import MappedPcOwner
from tools.hil_observation.transport import SessionScope, ObservationFrame, ReceivedPacket
from xenova_vehicle.oak.sim_nn import HostClockSample, SimNnSubmission, SimNnResult
from xenova_vehicle.oak.yolop_frames import NNInputStamp
from xenova_vehicle.simulation.pc_input import PcSourceBinding, PcFrameInput
from xenova_vehicle.simulation.motion import DevelopmentMotion
from xenova_vehicle.simulation.development_mapping import DevelopmentMapping, DevelopmentMappingNotReady
from xenova_vehicle.storage.contracts import canonical, json_bytes


@unittest.skipUnless(os.name == 'posix', 'actual existing SQLite owner requires POSIX')
class PcDevelopmentMappingTests(unittest.TestCase):
    def setUp(self):
        self.case=mapping_fixtures.DevelopmentMappingTests(
            'test_actual_core_unknown_and_sql_proof_preserve_original_four_j_endpoint_keys')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        c=self.case;f=c.f;uid=motion_fixtures.identifier
        self.binding=PcSourceBinding(dict(profile='development-pc-source-1',
            service_instance=f.raw.fields['service_instance'],source_id=uid(601),pc_clock_epoch=uid(602),
            jetson_session=uid(603),stream_instances=dict(rgb_video=uid(604),depth_right=uid(605)),
            destination_clock_epoch=f.source['clock_epoch'],calibration_profile_sha256=f.profile.bound_calibration.sha256,
            motion_profile_sha256=f.profile.sha256,nn_manifest_sha256='e'*64))
        self.clock_owner=HilObservationClock(ClockSource(uid(601),uid(602),uid(603)),
            ClockPolicy(max_rtt_us=100,max_offset_width_us=32,drift_ppm=0,lease_us=1_000_000,lookback_us=500_000))
        for sequence,j1 in ((1,990_000),(2,990_100)):
            original=self.clock_owner.probe(sequence,sequence.to_bytes(16,'big'),j1,j1+200)
            self.clock_owner.accept(reply(original,j1-996,j1-995),j1+10)
        scope=SessionScope(uid(601),uid(603),self.binding.sha256,f.profile.bound_calibration.sha256,
            f.profile.sha256,uid(602))
        self.pc_owner=MappedPcOwner(self.binding,scope,self.clock_owner,
            destination_clock_id=f.sensor_context.pose_clock_id)
        old=f.observation()
        self.packet=ObservationFrame(scope,old.rgb.stamp.sequence,rgb_pc_interval=(998_805,999_095),
            depth_pc_interval=(998_905,999_195),render_completed_pc_us=999_300,expires_pc_us=1_499_000,
            rgb=old.rgb.payload,depth=old.depth.payload)
        self.received=ReceivedPacket(self.packet,f.now)
        frame=self.pc_owner.accept_frame(self.received,now_j_us=f.now)
        f.binding=dict(f.binding,raw_binding_sha256=self.binding.sha256)
        f.context=replace(f.context,source_binding=canonical(f.binding))
        self.source_bytes=canonical(f.snapshot())
        self.motion=DevelopmentMotion.for_pc_source(f.profile,self.binding,f.policy,
            sensor_context=f.sensor_context,mapper_worker=f.mapper_worker,mapper_instance=f.source['mapper_instance'],
            pc_owner_token=self.pc_owner.owner_token)
        self.motion.ingest(self.source_bytes,now_us=f.now)
        # 원 PC 수신 직후 full bracket을 먼저 고정한다. NN 뒤 과거 pose를 다시 query하지 않는다.
        self.view=self.pc_owner.current_view(frame,now_j_us=f.now)
        self.snapshot=self.motion.frame_snapshot_pc(frame,current_pc_view=self.view,now_us=f.now,
            expires_j_us=frame.expires_j_us,job_id=uid(699),source_generation=self.binding.sha256,
            source_key=frame.observation.rgb.stamp.key)
        self.original_frame=frame
        self.frame=self.join(frame,self.binding.fields['nn_manifest_sha256'])
        self.mapping=DevelopmentMapping.for_pc_source(f.profile,c.config,self.binding,f.context,c.store,
            source=f.source,clock=lambda:c.now,pc_owner_token=self.pc_owner.owner_token)

    def join(self,frame,context):
        # SDK/Device를 실행한 것처럼 세지 않는 명시 owned tensor/host enqueue leaf다.
        c=self.case;now=c.now
        observation=frame.observation;sample=HostClockSample(now+1,900_900,now+2)
        submission=SimNnSubmission(observation,observation.capture_interval,frame.expires_j_us,sample,context)
        seq,device,sdk=submission.key
        nn_input=NNInputStamp('nn_input',seq,device,sdk,now+10,320,320,'BGR888p',context)
        output=nn_fixtures.packet(sequence=seq,device_us=device,sdk_us=sdk,received_j_us=now+11,context_key=context)
        c.now=now+20
        return frame.with_observation(SimNnResult(submission,nn_input,output).to_perception_input(
            observation,clock=lambda:c.now))

    def prepare(self):
        return self.mapping.prepare_pc(self.frame,self.snapshot,
            current_pc_view=self.pc_owner.current_view(self.frame,now_j_us=self.case.now))

    def accept(self,candidate,**changes):
        arguments=dict(pc_frame=self.frame,current_pc_view=self.pc_owner.current_view(self.frame,now_j_us=self.case.now),
            current_source_context_bytes=self.source_bytes,fence_revision=self.snapshot.fence_revision)
        arguments.update(changes);return self.mapping.accepted_pc(candidate,**arguments)

    def test_explicit_pc_snapshot_keeps_original_endpoint_proof_and_readonly_sensor_membership(self):
        frame=self.frame;snapshot=self.snapshot;inputs=snapshot.metric_inputs
        self.assertEqual(frame.rgb_pc_interval,self.packet.rgb_pc_interval)
        self.assertEqual(frame.depth_pc_interval,self.packet.depth_pc_interval)
        self.assertNotEqual(frame.rgb_pc_interval,frame.mapped_rgb_j_us)
        self.assertEqual(snapshot.capture_stamp.captured_us,frame.observation.capture_interval)
        self.assertEqual(inputs.pose.captured_us,frame.observation.capture_interval)
        self.assertEqual(inputs.pose.sample.origin,'SYNTHETIC_ODOMETRY')
        self.assertEqual(snapshot.pc_frame_proof,frame.proof_bytes)
        self.assertEqual(snapshot.pc_binding_bytes,self.binding.canonical_bytes)
        self.assertGreaterEqual(snapshot.byte_length,len(frame.proof_bytes)+len(self.binding.canonical_bytes))
        self.assertLessEqual(snapshot.byte_length,512*1024)
        self.assertIsNone(snapshot.p4_evidence)
        self.assertIsNone(snapshot.mapper_reply)
        self.assertIsNone(snapshot.mapper_reply_bytes)
        self.assertIsNone(inputs.clock_lease.completed_pi_us)
        self.assertEqual(tuple(r.evidence.wire for r in snapshot.original_pose_bracket(inputs.pose).originals),self.case.f.vehicles)
        with self.assertRaises(FrozenInstanceError):snapshot.pc_frame_proof=b'{}'
        source=inputs.source;source['vehicle']='foreign'
        self.assertEqual(snapshot.metric_inputs.source['vehicle'],self.case.f.source['vehicle'])

    def test_actual_core_and_sql_keep_full_pc_proof_without_unknown_promotion(self):
        candidate=self.prepare()
        self.assertIsNone(candidate.world)
        self.assertEqual(candidate.result.summary['kind'],'SYNTHETIC')
        self.assertEqual(candidate.result.summary['free_cells_applied'],0)
        self.assertEqual(candidate.proof_receipt.status,'COMMITTED')
        record=candidate.frame_provenance
        raw=bytes(self.case.db.connection.execute('SELECT payload FROM source_contexts WHERE context_sha256=?',
            (record.context_sha256,)).fetchone()[0])
        body=json_bytes(raw,4096)['body']
        self.assertLessEqual(len(raw),4096)
        self.assertEqual(set(body),set(('profile','input_kind','clock_basis','pc_source_binding','pc_frame_proof',
            'storage_context_sha256','source_context_sha256','frame_job_id','geometry_expires_j_us',
            'nn_source_key','original_probe_sha256','original_trust_sha256')))
        self.assertEqual(body['profile'],'pc-frame-evidence-1')
        self.assertEqual(body['clock_basis'],'PC_MONOTONIC_TO_J_FIXED_LEASE_INTERVALS_1')
        self.assertEqual(canonical(body['pc_source_binding']),self.binding.canonical_bytes)
        self.assertEqual(canonical(body['pc_frame_proof']),self.frame.proof_bytes)
        self.assertEqual(body['geometry_expires_j_us'],str(self.frame.expires_j_us))
        self.assertEqual(body['nn_source_key'],list(self.frame.observation.nn_key))
        self.accept(candidate)
        self.assertEqual(self.mapping.pipeline.grid.snapshot(self.case.now,
            clock_id=self.frame.observation.rgb.stamp.clock_id)['arrays']['state'].tobytes(),bytes(16))

    def test_samehost_and_pc_entrypoints_cannot_mix_and_reject_before_core_or_sql(self):
        with (patch.object(self.mapping.pipeline,'process',side_effect=AssertionError('unexpected core')),
                patch.object(self.case.store,'reserve_provenance',side_effect=AssertionError('unexpected SQL'))):
            with self.assertRaises(ValueError):self.mapping.prepare(self.frame.observation,self.snapshot)
            with self.assertRaises(ValueError):self.case.mapping.prepare_pc(self.frame,self.snapshot,current_pc_view=self.view)
            with self.assertRaises(ValueError):self.mapping.prepare_pc(self.frame,self.case.snapshot,current_pc_view=self.view)
            wrong=self.join(self.original_frame,'c'*64)
            with self.assertRaisesRegex(DevelopmentMappingNotReady,'NN_MANIFEST'):
                self.mapping.prepare_pc(wrong,self.snapshot,
                    current_pc_view=self.pc_owner.current_view(wrong,now_j_us=self.case.now))
            original=self.frame.proof_bytes;changed=json_bytes(original,16384)
            changed['mapped_j_us'][0][0]+=1
            object.__setattr__(self.frame,'proof_bytes',canonical(changed))
            try:
                with self.assertRaises(ValueError):self.prepare()
            finally:object.__setattr__(self.frame,'proof_bytes',original)
        with self.assertRaises(ValueError):
            self.motion.frame_snapshot(self.frame.observation,now_us=self.case.now,expires_j_us=self.frame.expires_j_us,
                job_id=self.snapshot.job_id,source_generation=self.binding.sha256,source_key=self.snapshot.source_key)
        with self.assertRaises(ValueError):
            self.motion.sensor_state.frame_snapshot(self.snapshot.capture_stamp,now_us=self.case.now,
                expires_us=self.frame.expires_j_us,job_id=self.snapshot.job_id,source_generation=self.binding.sha256,
                source_key=self.snapshot.source_key)
        with self.assertRaises(ValueError):
            self.motion.sensor_state.read(self.source_bytes,self.snapshot.capture_stamp,now_us=self.case.now)

    def test_cancel_keeps_accepted_state_and_original_durable_ids_without_issuing_again(self):
        original=self.mapping.pipeline;candidate=self.prepare();count=self.case.rows()
        self.mapping.cancelled(candidate)
        self.assertIs(self.mapping.pipeline,original)
        repeated=self.prepare()
        self.assertIs(repeated.frame_provenance,candidate.frame_provenance)
        self.assertEqual(self.case.rows(),count)
        self.mapping.cancelled(repeated)

    def test_final_original_context_and_expiry_fence_preserve_accepted_state(self):
        original=self.mapping.pipeline;candidate=self.prepare()
        with self.assertRaises(ValueError):self.mapping.accepted(candidate,
            current_source_context_bytes=self.source_bytes,fence_revision=self.snapshot.fence_revision)
        value=json_bytes(self.source_bytes,65536)
        value['source']['map_epoch']=motion_fixtures.identifier(700)
        with self.assertRaises(ValueError):self.accept(candidate,current_source_context_bytes=canonical(value))
        self.assertIs(self.mapping.pipeline,original)
        self.case.now=self.frame.expires_j_us
        with self.assertRaises(ValueError):
            self.mapping.accepted_pc(candidate,pc_frame=self.frame,current_pc_view=self.view,
                current_source_context_bytes=self.source_bytes,fence_revision=self.snapshot.fence_revision)
        self.assertIs(self.mapping.pipeline,original)
        self.mapping.cancelled(candidate)
