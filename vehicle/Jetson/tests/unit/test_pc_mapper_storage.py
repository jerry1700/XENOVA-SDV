"""PC compute intent→원 mailbox→실 SQLite 자료/receipt의 제어경계 회귀다.

기존 PC/Pi clock·Vehicle·owned NN은 software leaf다. main_complete/main_release는 이
시험의 명시 producer 사건이며 실제 native SHM/RELEASE 실송신·S/A/M/D 프로세스/HIL
양성이 아니다. 마지막 typed road는 ID 예약 게이트만 검사하며 실제 pixel/NN/World
양성이나 원811 geometry 실패의 수정으로 세지 않는다. 제품 port는 Store를 받지 않는다.
"""

from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import test_pc_development_mapping as pc_fixtures
from envelope_fixtures import fields
from xenova_vehicle.perception.road import LaneHypothesis,RoadResult
from xenova_vehicle.runtime.local_envelope import encode_structure
from xenova_vehicle.runtime.mapper_catalogs import CatalogNotReady
from xenova_vehicle.runtime.mapper_storage import MapperStorageMailbox
from xenova_vehicle.runtime.pc_mapper_storage import PcMapperStorage,PcStorageNotReady,PcFrameProofDraft
from xenova_vehicle.runtime.storage_messages import DbMessage
from xenova_vehicle.storage.contracts import StorageJob,ProvenanceRequest,LoadRequest,canonical
from xenova_vehicle.storage.port import CheckpointRequest
from xenova_vehicle.storage.records import CheckpointPayload


@unittest.skipUnless(os.name=='posix','actual existing SQLite owner requires POSIX')
class PcMapperStorageTests(unittest.TestCase):
    def setUp(self):
        self.pc=pc_fixtures.PcDevelopmentMappingTests(
            'test_explicit_pc_snapshot_keeps_original_endpoint_proof_and_readonly_sensor_membership')
        self.pc.setUp();self.addCleanup(self.pc.doCleanups)
        c=self.pc.case;f=c.f
        self.context=f.context;self.mailbox=MapperStorageMailbox(self.context.sha256)
        self.serial=800
        self.material=(dict(service_instance=self.pc.binding.fields['service_instance'],storage_worker=self.uid(),
            storage_profile_sha256=c.db.profile.sha256,database_uuid=self.context.database_uuid,db_schema_version=1),
            dict(status='EMPTY',allocator_ready=True,schema_version=1,database_uuid=self.context.database_uuid,
                allocator_highwater=c.db.metadata['allocator_highwater'],selected_checkpoint=None))
        self.owner=PcMapperStorage(self.context,self.mailbox,self.pc.binding,f.profile,c.config,
            clock=lambda:c.now,initial_storage_material=self.material,pc_owner_token=self.pc.pc_owner.owner_token,
            crossing=b'\x00')
        self.inputs=self.pc.snapshot.metric_inputs
        self.result=self.pc.mapping.pipeline.process(self.pc.frame.observation,pose=self.inputs.pose,now_us=c.now,
            completed_clock=lambda:c.now,motion_qualified=c.config.motion_qualified)

    def uid(self):
        self.serial+=1
        return '00000000-0000-0000-0000-%012x'%self.serial

    def job(self):return StorageJob(self.context,self.uid(),self.pc.case.now,self.pc.case.now+100_000)

    def empty_payload(self,job):
        now=self.pc.case.now
        cutoff=self.inputs.clock_lease.j_interval_to_pi((now,now),now_j_us=now)[1]
        return CheckpointPayload.build(job,durable_cutoff_pi_us=cutoff)

    def main_select(self):
        value=self.mailbox.main_take(current_context_sha256=self.context.sha256,now_us=self.pc.case.now)
        self.assertIsNotNone(value)
        return value

    def main_result(self,dispatch,result,*,release=True):
        self.mailbox.main_complete(dispatch,result,completed_us=self.pc.case.now)
        if release:self.mailbox.main_release(dispatch)

    def grant(self):
        request=ProvenanceRequest(self.job(),self.uid(),16)
        self.owner.queue_provenance(request,now_us=self.pc.case.now)
        self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();self.assertIs(dispatch.request,request)
        actual=self.pc.case.store.reserve_provenance(request.job,request.reservation_nonce,block_count=request.block_count,
            current_context_sha256=self.context.sha256)
        body=actual.fields
        outer=fields('DB_JOB',payload=dict(op='RANGE_RESULT',body=body),domain='STORAGE')
        outer.update(service_instance=self.pc.binding.fields['service_instance'],
            sender_instance=self.pc.binding.fields['service_instance'],request_id=request.job.job_id,
            issued_j_us=str(self.pc.case.now),expires_j_us=str(request.job.expires_us))
        message=DbMessage(encode_structure(outer),'RANGE_RESULT',canonical(body),self.pc.case.now,request.job.expires_us)
        self.assertFalse(self.owner.ready)
        self.main_result(dispatch,message)
        self.owner.maintenance(now_us=self.pc.case.now)
        return request

    def seed_head(self):
        job=self.job();request=CheckpointRequest(job,self.empty_payload(job))
        self.owner.queue_checkpoint(request,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select()
        receipt=self.pc.case.store.commit(job,request.payload,current_context_sha256=self.context.sha256)
        self.assertIsNone(self.owner.checkpoint_ref);self.assertIsNone(self.owner.head)
        self.main_result(dispatch,receipt);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertIsNotNone(self.owner.checkpoint_ref);self.assertIsNone(self.owner.head)
        load=LoadRequest(self.job(),self.owner.checkpoint_ref,canonical(dict(kind='CHECKPOINT_PAGE',cursor=0)))
        self.owner.queue_head_load(load,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();loaded=self.pc.case.store.load(load,current_context_sha256=self.context.sha256)
        self.main_result(dispatch,loaded);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertIs(self.owner.head[0],loaded.heads)
        self.assertIs(self.owner.last_terminal.delivery.request,load)
        return loaded

    def prepare(self,result=None):
        p=self.pc
        return self.owner.prepare_frame(p.frame,p.snapshot,self.result if result is None else result,
            current_pc_view=p.pc_owner.current_view(p.frame,now_j_us=p.case.now),capture_prior=self.inputs.pose,
            registration_revision=0,prior_qualified=False,now_us=p.case.now,roi_bounds=(-.2,-.2,.2,.2))

    def test_no_range_before_actual_original_wire_grant_and_wrong_receipt_cannot_be_adopted(self):
        self.assertFalse(self.owner.ready)
        with self.assertRaises(PcStorageNotReady):self.prepare()
        original=self.grant()
        self.assertTrue(self.owner.ready)
        self.assertIs(self.owner.range_grant.request,original)
        self.assertIs(self.owner.last_terminal.delivery.request,original)
        self.assertEqual(self.owner.provenance_cursor,self.owner.range_grant.start_inclusive)
        self.assertEqual(self.owner.last_terminal.delivery.admitted_result.reply_bytes,self.owner.range_grant.reply_bytes)
        with self.assertRaises(PcStorageNotReady):
            self.owner.queue_provenance(ProvenanceRequest(self.job(),self.uid(),16),now_us=self.pc.case.now)
        job=self.job();request=CheckpointRequest(job,self.empty_payload(job))
        self.owner.queue_checkpoint(request,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();receipt=self.pc.case.store.commit(job,request.payload,current_context_sha256=self.context.sha256)
        with self.assertRaises(ValueError):self.main_result(dispatch,replace(receipt,job_id=self.uid()))
        self.assertIs(self.owner.pending.request,request)

    def test_checkpoint_receipt_is_not_a_catalog_head_and_actual_load_is_required(self):
        self.grant();self.seed_head()
        self.assertEqual(tuple(x[0] for x in self.owner.head[0].heads),('LANE','SLOT'))
        self.assertTrue(self.owner.head[0].current(self.owner.head[1],self.context.sha256,self.pc.case.now))
        self.assertIsNone(self.owner.catalogs)
        self.assertTrue(self.owner.ready)

    def test_unknown_keeps_one_original_request_and_only_original_resolution_advances(self):
        self.grant();job=self.job();request=CheckpointRequest(job,self.empty_payload(job))
        self.owner.queue_checkpoint(request,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();actual=self.pc.case.store.commit(job,request.payload,current_context_sha256=self.context.sha256)
        unknown=replace(actual,status='UNKNOWN',durable_revision=None,committed_j_us=None)
        self.main_result(dispatch,unknown,release=False);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertEqual(self.owner.status,'WAIT_ORIGINAL_UNKNOWN')
        self.assertIs(self.owner.pending.request,request);self.assertIsNone(self.owner.checkpoint_ref)
        with self.assertRaises(PcStorageNotReady):
            other=self.job();self.owner.queue_checkpoint(CheckpointRequest(other,self.empty_payload(other)),now_us=self.pc.case.now)
        self.owner.maintenance(now_us=self.pc.case.now)
        self.assertIs(self.mailbox.main_peek(),dispatch)
        self.mailbox.main_resolve(dispatch,actual,completed_us=self.pc.case.now);self.mailbox.main_release(dispatch)
        self.owner.maintenance(now_us=self.pc.case.now)
        self.assertIsNone(self.owner.pending);self.assertIsNotNone(self.owner.checkpoint_ref)
        self.assertIs(self.owner.last_terminal.delivery.request,request)

    def test_actual_pc_ram_proof_cancel_burns_ids_and_accept_keeps_only_original_refs(self):
        self.grant();start=self.owner.provenance_cursor
        original=self.owner.evidence;draft=self.prepare()
        self.assertIs(type(draft.proof),PcFrameProofDraft)
        self.assertEqual(draft.proof.record.provenance_id,start)
        self.assertEqual(draft.proof.record_context.sha256,draft.proof.record.context_sha256)
        self.assertIsNone(self.mailbox.main_peek())
        self.owner.cancelled(draft)
        self.assertIs(self.owner.evidence,original);self.assertEqual(self.owner.provenance_cursor,start+1)
        repeated=self.prepare();self.assertEqual(repeated.proof.record.provenance_id,start+1)
        record=repeated.proof.record
        object.__setattr__(repeated.proof,'record',replace(record,provenance_id=start+2))
        try:
            with self.assertRaises(ValueError):self.owner.accepted(repeated)
        finally:object.__setattr__(repeated.proof,'record',record)
        with patch.object(self.mailbox,'compute_offer',side_effect=AssertionError('accept must not offer I/O')):
            self.owner.accepted(repeated)
        self.assertIn(record,self.owner.evidence.provenance)
        self.assertIsNone(self.owner.catalogs.view.lane)
        with self.assertRaises(PcStorageNotReady):self.prepare()

    def test_late_original_completion_and_source_fence_cannot_mint_new_jobs_or_ids(self):
        request=ProvenanceRequest(self.job(),self.uid(),16)
        self.owner.queue_provenance(request,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select()
        actual=self.pc.case.store.reserve_provenance(request.job,request.reservation_nonce,block_count=16,
            current_context_sha256=self.context.sha256)
        outer=fields('DB_JOB',payload=dict(op='RANGE_RESULT',body=actual.fields),domain='STORAGE')
        outer.update(request_id=request.job.job_id,issued_j_us=str(self.pc.case.now),expires_j_us=str(request.job.expires_us))
        self.main_result(dispatch,DbMessage(encode_structure(outer),'RANGE_RESULT',canonical(actual.fields),self.pc.case.now,request.job.expires_us))
        self.pc.case.now=request.job.expires_us-1
        with patch.object(self.owner,'clock',return_value=request.job.expires_us):
            self.owner.maintenance(now_us=self.pc.case.now)
        self.pc.case.now=request.job.expires_us
        self.assertFalse(self.owner.ready);self.assertIs(self.owner.pending.request,request)
        with self.assertRaises(PcStorageNotReady):
            self.owner.queue_provenance(ProvenanceRequest(self.job(),self.uid(),16),now_us=self.pc.case.now)
        self.mailbox.fence('f'*64,now_us=self.pc.case.now)
        self.assertEqual(self.owner.maintenance(now_us=self.pc.case.now),'SOURCE_CONTEXT_FENCED')

    def test_typed_joint_geometry_ids_wait_for_actual_committed_reservation_then_next_observation(self):
        """원 raw-odom 좌표계의 독립typedgeometry다. 실제pixel/alignment/World 양성은아니다."""
        self.grant();self.seed_head()
        frame=self.pc.frame;stamp=frame.observation.rgb.stamp;snapshot=self.result.map_snapshot
        self.assertEqual(snapshot['frame_id'],'odom:'+self.inputs.pose.sample.epoch)
        self.assertEqual(snapshot['clock_id'],self.inputs.pose.sample.clock_id)
        self.assertEqual(snapshot['calibration_id'],self.inputs.pose.sample.calibration_id)
        h=LaneHypothesis(0,((0,),(1,),(2,)),(0.,1.),((0.,0.),(1.,0.)),
            ((0.,.3),(1.,.3)),((0.,-.3),(1.,-.3)),((0.,.15),(1.,.15)),
            ((0.,-.15),(1.,-.15)),(False,False),((.29,.31),(.29,.31)),.001,.002)
        road=RoadResult('VALID',(h,),True,(),stamp.key,True)
        literal=replace(self.result,road=road,summary=dict(self.result.summary,geometric_model_qualified=True))
        draft=self.owner.prepare_frame(frame,self.pc.snapshot,literal,
            current_pc_view=self.pc.pc_owner.current_view(frame,now_j_us=self.pc.case.now),capture_prior=self.inputs.pose,
            registration_revision=0,prior_qualified=True,now_us=self.pc.case.now,roi_bounds=(-.2,-.2,.2,.2))
        self.assertEqual(draft.catalog.view.lane_status,'NEW')
        self.assertIsNone(draft.catalog.view.lane);self.assertEqual(draft.catalog.state.lane.available_ids,0)
        self.owner.accepted(draft)
        self.owner.maintenance(now_us=self.pc.case.now,catalog_arguments=dict(reservation_nonce=self.uid()))
        dispatch=self.main_select();request=dispatch.request
        self.assertEqual(request.payload.intent,'CATALOG_RESERVATION')
        self.assertEqual(self.owner.catalogs._accepted.lane.available_ids,0)
        receipt=self.pc.case.store.commit(request.job,request.payload,current_context_sha256=self.context.sha256)
        self.assertEqual(self.owner.catalogs._accepted.lane.available_ids,0)
        self.main_result(dispatch,receipt);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertGreaterEqual(self.owner.catalogs._accepted.lane.available_ids,2)
        self.assertIsNone(self.owner.catalogs.view.lane)  # COMMITTED는 새 관측을 만들지 않는다.
        self.assertIs(self.owner.last_terminal.delivery.request,request)
        # 별도 원 software 측정이다. 이전 관측 객체의 seq/time/E를 고쳐 재사용하지 않는다.
        old=self.pc.packet
        packet=pc_fixtures.ObservationFrame(old.scope,old.sequence+1,
            rgb_pc_interval=tuple(v+1000 for v in old.rgb_pc_interval),
            depth_pc_interval=tuple(v+1000 for v in old.depth_pc_interval),
            render_completed_pc_us=old.render_completed_pc_us+1000,expires_pc_us=old.expires_pc_us+1000,
            rgb=bytes(len(old.rgb)),depth=bytes(len(old.depth)))
        original=self.pc.pc_owner.accept_frame(pc_fixtures.ReceivedPacket(packet,self.pc.case.now),now_j_us=self.pc.case.now)
        next_snapshot=self.pc.motion.frame_snapshot_pc(original,
            current_pc_view=self.pc.pc_owner.current_view(original,now_j_us=self.pc.case.now),now_us=self.pc.case.now,
            expires_j_us=original.expires_j_us,job_id=self.uid(),source_generation=self.pc.binding.sha256,
            source_key=original.observation.rgb.stamp.key)
        joined=self.pc.join(original,self.pc.binding.fields['nn_manifest_sha256'])
        next_inputs=next_snapshot.metric_inputs
        calculated=self.pc.mapping.pipeline.process(joined.observation,pose=next_inputs.pose,now_us=self.pc.case.now,
            completed_clock=lambda:self.pc.case.now,motion_qualified=self.pc.case.config.motion_qualified)
        next_stamp=joined.observation.rgb.stamp
        next_literal=replace(calculated,road=RoadResult('VALID',(h,),True,(),next_stamp.key,True),
            summary=dict(calculated.summary,geometric_model_qualified=True))
        next_draft=self.owner.prepare_frame(joined,next_snapshot,next_literal,
            current_pc_view=self.pc.pc_owner.current_view(joined,now_j_us=self.pc.case.now),capture_prior=next_inputs.pose,
            registration_revision=0,prior_qualified=True,now_us=self.pc.case.now,roi_bounds=(-.2,-.2,.2,.2))
        model=next_draft.catalog.view.lane
        self.assertIsNotNone(model)
        reservation=request.payload.evidence.reservations[0]
        self.assertEqual(model.lane_ids,(reservation.start_inclusive,reservation.start_inclusive+1))
        self.assertEqual(model.source.stamp,next_stamp)
        self.owner.accepted(next_draft)
        # 실제 dirty catalog CP도 같은 mailbox/원 receipt를 거친다. 동기 Store adapter는 없다.
        cutoff=max(p.pi_upper for p in self.owner.evidence.provenance)
        old_ref=self.owner.checkpoint_ref
        checkpoint=self.owner.queue_catalog_checkpoint(self.job(),now_us=self.pc.case.now,
            durable_cutoff_pi_us=cutoff)
        self.assertEqual(tuple(r.stable_id for r in checkpoint.payload.evidence.catalog),model.lane_ids)
        self.assertIsNone(self.mailbox.main_peek())
        self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();self.assertIs(dispatch.request,checkpoint)
        committed=self.pc.case.store.commit(checkpoint.job,checkpoint.payload,current_context_sha256=self.context.sha256)
        self.assertEqual(self.owner.checkpoint_ref,old_ref)
        self.main_result(dispatch,committed);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertIs(self.owner.last_terminal.delivery.admitted_result,committed)
        load=LoadRequest(self.job(),self.owner.checkpoint_ref,canonical(dict(kind='CHECKPOINT_PAGE',cursor=0)))
        self.owner.queue_head_load(load,now_us=self.pc.case.now);self.owner.maintenance(now_us=self.pc.case.now)
        dispatch=self.main_select();loaded=self.pc.case.store.load(load,current_context_sha256=self.context.sha256)
        self.main_result(dispatch,loaded);self.owner.maintenance(now_us=self.pc.case.now)
        self.assertEqual(tuple(r.stable_id for r in loaded.payload.evidence.catalog),model.lane_ids)
        self.assertIn(next_draft.proof.record,loaded.payload.evidence.provenance)
        self.assertIn(next_draft.proof.record_context,loaded.payload.evidence.contexts)
