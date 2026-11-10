"""실제 Linux S/M binary 왕복으로 identity와 원 source 전달의 수명을 확인한다."""

from base64 import b64decode
from collections import deque
from hashlib import sha256
import json
import socket
from types import SimpleNamespace
import unittest

from test_source_identity import fixture,uid
from xenova_drive_world import encode
from xenova_vehicle.runtime.data_channel import DataLink,data_pair
from xenova_vehicle.runtime.source_runtime import SupervisorSource,MapperSource,SourceChanged
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.jetson_jio import JioInput


class JioPort:
    def __init__(self,policy,device):
        self.profile=SimpleNamespace(source_policy=policy,device=device)
        self.inputs=deque();self.bound=None;self.fenced=False;self.worlds=[]
        self.current_scene=self.current_lease=None;self.summaries=[]
        self.publication=None
        self.world_enabled=True

    def take_inputs(self):
        result=tuple(self.inputs);self.inputs.clear();return result

    def replace_source(self,source,**origin):self.bound=(source,origin)
    def replace_publication(self,binding):self.publication=canonical(binding)
    def fence_source(self):self.fenced=True
    def set_world_dispatch_enabled(self,enabled):
        changed=self.world_enabled!=enabled;self.world_enabled=enabled;return changed
    def offer_world(self,raw,*,expires_j_us,publication_sha256):
        if self.world_enabled:self.worlds.append((raw,expires_j_us))
    def offer_survey_summary(self,raw,*,grant_sha256,expires_j_us):
        self.summaries.append((raw,grant_sha256,expires_j_us));return True


@unittest.skipUnless(hasattr(socket,'SOCK_SEQPACKET'),'actual Linux binary seqpacket required')
class SourceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.raw,self.policy,self.sample,self.bootstrap,self.peer,self.incarnation=fixture()
        self.now=1000;self.jio=JioPort(self.policy,'oak-front')
        left,right=data_pair()
        self.mapper=MapperSource(DataLink(left,self.peer,'M',clock=lambda:self.now),self.raw,self.policy,
            mapper_instance=self.incarnation,device=self.jio.profile.device,clock=lambda:self.now)
        # 이 shared fixture는 software source 통로만 검산하며 실제 compute STANDBY를 승인하지 않는다.
        from xenova_vehicle.runtime.mapper_operation import MapperOperationController
        def apply_run_only(action,expires,gate):
            if action!='RUN':raise ValueError('source fixture has no STANDBY compute consumer')
            if self.now>=expires:raise ValueError('expired source fixture CONFIG')
            return self.now
        self.mapper.install_operation(MapperOperationController(self.peer,apply=apply_run_only,clock=lambda:self.now))
        self.supervisor=SupervisorSource(DataLink(right,self.peer,'S',clock=lambda:self.now),self.raw,self.jio,
            mapper_instance=self.incarnation,mapper_boot=uid(),clock_epoch=self.raw.fields['clock_epoch'],clock=lambda:self.now)
        self.material=({'service_instance':self.peer.service_instance,'storage_worker':uid(),
            'storage_profile_sha256':'c'*64,'database_uuid':self.bootstrap['database_uuid'],'db_schema_version':1},self.bootstrap)
        self.publications=[]

    def tearDown(self):self.mapper.close();self.supervisor.close()

    def cycle(self,*,ready=True,count=8):
        for _ in range(count):
            self.supervisor.pump(self.material,bind_ready=ready,on_publication=lambda binding,context:self.publications.append((binding,context)))
            self.mapper.pump(before_outputs=lambda:self.mapper.operation_step())

    def vehicle(self):
        wire=encode(self.sample);self.jio.inputs.append(JioInput('VEHICLE',wire,self.now));return wire

    def storage_abort(self):
        from xenova_vehicle.runtime.raw_leases import Endpoint
        from xenova_vehicle.runtime.resident_db import DbAbort
        from xenova_vehicle.runtime.storage_messages import prepare_payload
        from xenova_vehicle.storage.contracts import StorageJob
        from xenova_vehicle.storage.port import CheckpointRequest
        from xenova_vehicle.storage.records import CheckpointPayload
        context=self.mapper.storage_context;job=StorageJob(context,uid(),100,200)
        request=CheckpointRequest(job,CheckpointPayload.build(job,durable_cutoff_pi_us=1))
        return DbAbort('CHECKPOINT',canonical(prepare_payload(request)['body']),job.job_id,100,200,900,
            Endpoint(uid(),self.peer.mapper_worker),context.sha256,'D_OWNER_REBOUND_UNKNOWN')

    def test_empty_original_vehicle_proposal_ack_and_snapshot_keep_ids_bytes_and_callback_time(self):
        original=self.vehicle();self.cycle()
        self.assertEqual(len(self.publications),1)
        binding,context=self.publications[0]
        self.assertEqual(self.mapper.publication,binding)
        self.assertEqual(binding['mapper_instance'],self.incarnation)
        self.assertEqual((binding['map_epoch'],binding['frame_epoch']),(self.mapper.map_epoch,self.mapper.frame_epoch))
        self.assertEqual(context.source_binding,canonical(binding))
        self.assertEqual(context.calibration,self.policy.calibration)
        self.assertNotEqual(context.calibration,self.profile.sha256)
        snapshot=self.mapper.context_bytes();value=json.loads(snapshot)
        self.assertEqual(b64decode(value['vehicles'][0]['raw']),original)
        self.assertEqual(value['vehicles'][0]['received_j_us'],'1000')
        self.assertIsNone(value['active_probe_sha256'])
        self.assertEqual(self.jio.bound[0],self.mapper.source)
        self.assertLessEqual(len(snapshot),65536)
        self.now=1100;self.cycle()
        self.assertEqual(len(self.publications),1)
        self.assertEqual(self.mapper.context_bytes(),snapshot)

    def test_no_original_vehicle_does_not_generate_identity_or_fake_odom(self):
        self.cycle()
        self.assertIsNone(self.mapper.publication)
        self.assertIsNone(self.mapper._proposal_packet)
        self.assertEqual(self.publications,[])

    def test_expired_storage_delivery_retries_current_material_from_queue_or_selected_packet(self):
        for selected in (False,True):
            with self.subTest(selected=selected):
                self.supervisor._material=self.material
                body=canonical(dict(binding=self.material[0],bootstrap=self.material[1]))
                self.supervisor._material_sha=sha256(body).hexdigest()
                if selected:
                    old=self.supervisor.link.send('SOURCE_STORAGE',{},body,issued_us=self.now,expires_us=self.now+1)
                else:
                    old=None
                    self.supervisor._stage('STORAGE','SOURCE_STORAGE',{},body,issued=self.now,expires=self.now+1)
                self.now+=1
                self.cycle()
                self.assertEqual(self.mapper.material,self.material)
                if old is not None:self.assertEqual(old.body,body)

    def test_storage_expiry_between_precheck_and_flush_retries_but_unknown_send_is_preserved(self):
        self.supervisor._material=self.material
        body=canonical(dict(binding=self.material[0],bootstrap=self.material[1]))
        old=self.supervisor.link.send('SOURCE_STORAGE',{},body,issued_us=self.now,expires_us=self.now+1)
        flush=self.supervisor.link.flush
        def delayed_flush():
            self.now=old.expires_us
            return flush()
        self.supervisor.link.flush=delayed_flush
        try:self.assertIsNone(self.supervisor._flush())
        finally:self.supervisor.link.flush=flush
        self.assertIsNone(self.supervisor.link.pending)
        retry=self.supervisor.pending['STORAGE']
        self.assertEqual(retry[2],old.body);self.assertEqual(retry[3],old.expires_us)
        unknown=self.supervisor.link.send('SOURCE_STORAGE',{},body,issued_us=self.now,expires_us=self.now+1)
        self.supervisor.link._send_unknown=True;self.now=unknown.expires_us
        with self.assertRaises(RuntimeError):self.supervisor._flush()
        self.assertIs(self.supervisor.link.pending,unknown)
        self.assertEqual(self.supervisor.counts['expired_unsent'],1)

    def test_transport_timeout_with_unknown_send_is_not_treated_as_local_delivery_expiry(self):
        body=canonical(dict(binding=self.material[0],bootstrap=self.material[1]))
        original=self.supervisor.link.send('SOURCE_STORAGE',{},body,issued_us=self.now,expires_us=self.now+1)
        endpoint=self.supervisor.link.endpoint
        failure=TimeoutError('actual transport outcome is unknown')
        class UnknownSend:
            def __getattr__(self,name):return getattr(endpoint,name)
            def send(port,raw):
                self.now=original.expires_us
                raise failure
        self.supervisor.link.endpoint=UnknownSend()
        try:
            with self.assertRaises(TimeoutError) as caught:self.supervisor._flush()
            self.assertIs(caught.exception,failure)
        finally:self.supervisor.link.endpoint=endpoint
        self.assertIs(self.supervisor.link.pending,original)
        self.assertTrue(self.supervisor.link._send_unknown)
        self.assertEqual(self.supervisor.counts['expired_unsent'],0)

    def test_selected_bootstrap_completion_retries_delivery_without_a_new_load(self):
        body=b'original completed bootstrap evidence'
        request=uid()
        original=self.mapper.link.send('SOURCE_BOOTSTRAP_COMPLETE',{},body,request_id=request,
            issued_us=self.now,expires_us=self.now+1)
        self.now=original.expires_us
        self.mapper._flush()
        retry,=self.mapper._bootstrap_pending.values()
        self.assertEqual(retry[:3],('SOURCE_BOOTSTRAP_COMPLETE',body,request))
        self.assertEqual(retry[3],original.expires_us)
        self.assertFalse(self.mapper._bootstrap_started)

    def test_late_received_storage_is_installation_material_and_not_a_new_observation(self):
        body=canonical(dict(binding=self.material[0],bootstrap=self.material[1]))
        old=self.supervisor.link.send('SOURCE_STORAGE',{},body,issued_us=self.now,expires_us=self.now+1)
        self.supervisor.link.flush();self.now=old.expires_us
        self.mapper.pump()
        self.assertEqual(self.mapper.material,self.material)
        self.assertIsNone(self.mapper.publication);self.assertIsNone(self.mapper._vehicle_head)

    def test_publication_retry_preserves_full_identity_context_and_original_scene_reference(self):
        self.vehicle();self.cycle()
        original=self.mapper.publication;context=self.mapper.storage_context
        for selected in (False,True):
            with self.subTest(selected=selected):
                self.supervisor._stage_publication(self.supervisor.owner.binding,self.supervisor.context,self.now)
                if selected:
                    key=next(key for key in self.supervisor.pending if type(key) is tuple and key[0]=='PUBLICATION')
                    kind,metadata,body,issued,expires,request=self.supervisor.pending.pop(key)
                    old=self.supervisor.link.send(kind,metadata,body,request_id=request,issued_us=issued,expires_us=expires)
                    self.now=old.expires_us
                else:self.now+=5_000_000
                self.cycle()
                self.assertEqual(self.mapper.publication,original)
                self.assertEqual(self.mapper.storage_context,context)
                self.assertEqual(self.supervisor.owner.binding,original)
                self.assertEqual(len(self.publications),1)
        self.assertEqual(self.mapper.counts['publication_duplicate'],2)

    def test_evicted_vehicle_proposal_waits_for_fresh_attempt_without_source_recovery(self):
        self.vehicle();self.cycle(ready=False)
        original=self.mapper._proposal_packet
        self.supervisor.vehicles.clear()
        self.cycle(ready=True)
        self.assertEqual(self.supervisor.counts['identity_wait_current_input'],1)
        self.assertEqual(self.publications,[])
        self.now=original.expires_us;self.mapper.pump()
        self.vehicle();self.cycle()
        self.assertEqual(len(self.publications),1)
        self.assertNotEqual(self.mapper._proposal_packet.header['request_id'],original.header['request_id'])

    def test_expired_malformed_ready_does_not_claim_current_peer_bootstrap_failure(self):
        from xenova_vehicle.runtime.operation_request import CONSUMER_READY_PROFILE
        # 등록된 transport라도 다른 service/추가 필드인 body는 원 설치 사실이 아니다.
        for index,body in enumerate((dict(profile=CONSUMER_READY_PROFILE,service_instance=uid()),
                                    dict(profile=CONSUMER_READY_PROFILE,service_instance=self.peer.service_instance,extra=True))):
            self.now=1000+10*index
            packet=self.mapper.link.send('OPERATION_RESULT',{},canonical(body),issued_us=self.now,expires_us=self.now+1)
            self.assertIsNotNone(packet);self.assertIsNotNone(self.mapper.link.flush())
            self.now+=1
            self.supervisor.pump(self.material,bind_ready=True,on_publication=lambda *_:None)
            self.assertIsNone(self.supervisor.operation_owner._consumer_ready)
            self.assertFalse(self.supervisor.operation_owner.status()['mapper_synchronized'])
        self.assertEqual(self.supervisor.counts['operation_rejected'],2)
        self.cycle()
        self.assertTrue(self.supervisor.operation_owner.status()['mapper_synchronized'])

    def test_actual_supervisor_publication_callback_passes_original_context_and_origin_to_d_broker(self):
        from xenova_vehicle.runtime.resident_runtime import _Runtime
        from xenova_vehicle.runtime.resident_db import ResidentDbBroker
        from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
        from xenova_vehicle.runtime.native_resources import ArenaSpec,DB_COPY_BYTES
        runtime=_Runtime.__new__(_Runtime)
        mapper=LifecyclePeer(self.peer.service_instance,uid(),self.peer.mapper_worker,'M','a'*64)
        storage=LifecyclePeer(self.peer.service_instance,uid(),self.material[0]['storage_worker'],'D','a'*64)
        arena=uid()
        runtime.db_broker=ResidentDbBroker(storage,mapper,self.material[0],
            ArenaSpec(arena,'/xnv_db_'+arena,DB_COPY_BYTES,'DB_COPY'),clock_us=lambda:self.now)
        runtime._ensure_db_broker=lambda:None
        runtime.binding=self.raw;runtime.workers={'M':SimpleNamespace(peer=mapper)}
        runtime.db_outbox=deque();runtime.publication_context=None
        self.vehicle()
        for _ in range(8):
            self.supervisor.pump(self.material,bind_ready=True,on_publication=runtime.set_publication)
            self.mapper.pump()
        self.assertEqual(runtime.publication_context,self.mapper.storage_context)
        draft,=runtime.db_outbox
        self.assertEqual(draft.kind,'BIND')
        self.assertEqual(draft.domain,'PUBLICATION')
        self.assertEqual(draft.payload['binding'],self.mapper.publication)
        self.assertEqual(draft.payload['geometry_origin'],self.mapper.storage_context.geometry_origin_fields)
        self.assertEqual(runtime.db_broker.context_sha256,self.mapper.context_sha256)

    def test_expired_storage_terminal_keeps_original_job_and_uses_only_local_delivery_lifetime(self):
        from xenova_vehicle.runtime.raw_leases import Endpoint
        from xenova_vehicle.runtime.resident_db import DbAbort
        from xenova_vehicle.runtime.storage_messages import prepare_payload
        from xenova_vehicle.storage.contracts import StorageJob
        from xenova_vehicle.storage.port import CheckpointRequest
        from xenova_vehicle.storage.records import CheckpointPayload
        self.vehicle();self.cycle()
        context=self.mapper.storage_context
        job=StorageJob(context,uid(),100,200)
        request=CheckpointRequest(job,CheckpointPayload.build(job,durable_cutoff_pi_us=1))
        notice=DbAbort('CHECKPOINT',canonical(prepare_payload(request)['body']),job.job_id,100,200,900,
            Endpoint(uid(),self.peer.mapper_worker),context.sha256,'D_OWNER_REBOUND_UNKNOWN')
        self.supervisor.offer_abort(notice);self.cycle()
        result,=self.mapper.take_aborts()
        self.assertEqual(result,notice)
        self.assertEqual(result.expires_us,200)
        self.assertLess(result.expires_us,self.now)
        self.assertEqual(self.mapper.take_aborts(),())

    def test_storage_terminal_retries_queue_and_selected_delivery_without_changing_original_job(self):
        self.vehicle();self.cycle()
        for selected in (False,True):
            with self.subTest(selected=selected):
                notice=self.storage_abort();self.supervisor.offer_abort(notice)
                self.supervisor.offer_abort(notice)  # 동일 원 알림의 재접수는 bounded1 무효과다.
                if selected:
                    kind,metadata,body,issued,expires,request=self.supervisor.pending.pop('ABORT')
                    original=self.supervisor.link.send(kind,metadata,body,request_id=request,issued_us=issued,expires_us=expires)
                    self.now=original.expires_us
                else:self.now+=1_000_000
                self.cycle()
                delivered,=self.mapper.take_aborts()
                self.assertEqual(delivered,notice)
                self.assertEqual(delivered.expires_us,200)
                self.assertEqual(delivered.observed_us,900)
                self.assertFalse(delivered.current)
                self.assertIsNone(self.supervisor._abort_notice)

    def test_late_and_duplicate_storage_terminal_does_not_overwrite_or_redeliver_consumed_slot(self):
        self.vehicle();self.cycle();notice=self.storage_abort()
        self.supervisor.offer_abort(notice)
        self.supervisor.pump(self.material,bind_ready=True,on_publication=lambda *_:None)
        self.now+=1_000_000
        self.mapper.pump()
        self.assertEqual(self.mapper._abort,notice)
        for consumed in (False,True):
            if consumed:self.assertEqual(self.mapper.take_aborts(),(notice,))
            self.supervisor.offer_abort(notice);self.cycle()
            self.assertEqual(self.mapper._abort,None if consumed else notice)
        self.assertEqual(self.mapper.take_aborts(),())
        self.assertEqual(self.mapper.counts['storage_terminal_duplicate'],2)

    def test_distinct_unconsumed_storage_terminals_preserve_the_first_bounded_slot(self):
        self.vehicle();self.cycle();first=self.storage_abort();second=self.storage_abort()
        self.supervisor.offer_abort(first)
        with self.assertRaises(RuntimeError):self.supervisor.offer_abort(second)
        self.cycle()
        self.assertEqual(self.mapper._abort,first)
        self.supervisor.offer_abort(second)
        self.supervisor.pump(self.material,bind_ready=True,on_publication=lambda *_:None)
        with self.assertRaises(RuntimeError):self.mapper.pump()
        self.assertEqual(self.mapper.take_aborts(),(first,))

    def test_early_proposal_waits_actual_raw_ack_without_changing_original_job(self):
        self.vehicle();self.cycle(ready=False)
        original=self.mapper._proposal_packet
        self.assertIsNotNone(original);self.assertIsNone(self.mapper.publication)
        self.assertEqual(self.supervisor._early_identity.raw,original.raw)
        self.cycle(ready=True)
        self.assertEqual(self.mapper._proposal_packet.raw,original.raw)
        self.assertEqual(len(self.publications),1)

    def test_expired_identity_attempt_is_retired_and_fresh_transaction_can_retry(self):
        self.vehicle();self.cycle(ready=False)
        original=self.mapper._proposal_packet
        self.now=original.expires_us
        self.mapper.pump()
        self.assertIsNone(self.mapper._proposal_packet)
        self.vehicle()
        self.mapper.pump()
        retry=self.mapper._proposal_packet
        self.assertIsNotNone(retry)
        self.assertNotEqual(retry.header['request_id'],original.header['request_id'])
        self.assertGreaterEqual(retry.issued_us,original.expires_us)
        self.assertEqual(original.expires_us,self.now)

    def test_new_gateway_or_odom_source_immediately_fences_jio_before_m_recovery(self):
        self.vehicle();self.cycle()
        self.sample.gateway_instance=uid();self.sample.sequence+=1;self.vehicle()
        with self.assertRaises(SourceChanged):self.cycle(count=1)
        self.assertTrue(self.jio.fenced)
        self.assertEqual(len(self.publications),1)

    def test_current_db_material_is_kept_separate_and_no_complete_restore_success_is_invented(self):
        self.vehicle();self.cycle()
        old=self.mapper.publication
        self.bootstrap=dict(self.bootstrap,status='COMPLETE',selected_checkpoint=dict(checkpoint_job_id=uid(),
            durable_revision='1',manifest_sha256='d'*64,manifest_length=10,tile_count=0,
            map_epoch=old['map_epoch'],frame_epoch=old['frame_epoch'],odom_epoch=old['odom_epoch'],
            pi_host_boot=self.sample.host_boot,configuration=self.policy.configuration,calibration=self.policy.calibration,
            primitive_sha256=self.policy.primitive,origin_mapper_instance=self.incarnation))
        self.material=(self.material[0],self.bootstrap)
        self.cycle()
        self.assertEqual(self.mapper.publication,old)
        self.assertEqual(self.mapper.material[1]['status'],'COMPLETE')
        self.assertEqual(len(self.publications),1)


if __name__=='__main__':unittest.main()
