"""PC 개발 compute의 RAM proof/catalog와 실제 비동기 D 요청을 분리한다.

DB/actor/native token/thread를 만들지 않는다. M-main의 기존 MapperStorageClient가
실제 원 DB_JOB·RELEASE를 수행한 뒤 mailbox로 준 결과만 소비한다. 생성자/ready는
등록·Source/Scene/복원·주행 권한이 아니다. 실패한 원 job/nonce를 자동 재발행하지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256
import struct
from threading import get_ident
from uuid import UUID

from .mapper_catalogs import MapperCatalogs
from .mapper_profile import MapperProfile
from .mapper_sensor_state import SensorFrameSnapshot
from .mapper_storage import MapperStorageMailbox, StorageAbort, StorageDelivery
from .parking_bridge import MetricEvidence, WireProvenanceGrant
from ..simulation.motion_profile import DevelopmentMotionProfile
from ..simulation.pc_input import PcFrameInput, PcSourceBinding
from ..storage.contracts import (StorageContext, ProvenanceRequest, LoadRequest, CommitReceipt,
    canonical, json_bytes, selected_checkpoint_reference, uint,uuid,digest)
from ..storage.port import CheckpointRequest
from ..storage.records import (ContextRecord, EvidenceTable, LoadedCheckpoint, PROVENANCE, ProvenanceRecord,
    aggregate_rgb_provenance)


PC_CLOCK_BASIS = 'PC_MONOTONIC_TO_J_FIXED_LEASE_INTERVALS_1'


class PcStorageNotReady(RuntimeError):
    """원 D 결과/범위/head 또는 현재 PC proof가 없다. 새 사실을 만들어 보완하지 않는다."""


@dataclass(frozen=True,init=False)
class _ProofSeal:
    frame: PcFrameInput
    snapshot: SensorFrameSnapshot
    record: ProvenanceRecord
    record_context: ContextRecord
    grant: WireProvenanceGrant
    admitted_j_us: int
    context: StorageContext

    def __init__(self,*args,**kwargs):raise TypeError('original PC storage owner seal required')


@dataclass(frozen=True, init=False)
class PcFrameProofDraft:
    """실제 D-issued ID와 원 frame/snapshot이다. physical MapperInput을 가장하지 않는다."""
    frame: PcFrameInput
    snapshot: SensorFrameSnapshot
    storage_context: StorageContext
    record: ProvenanceRecord
    record_context: ContextRecord
    metric_evidence: MetricEvidence
    evidence: EvidenceTable
    _token: object

    def __init__(self,*args,**kwargs):raise TypeError('PC frame proof requires original storage owner issuance')

    @property
    def job_id(self):return self.snapshot.job_id
    @property
    def expires_us(self):return self.snapshot.expires_us
    @property
    def context_bytes(self):return self.snapshot.source_context_bytes
    @property
    def rgb_sequence(self):return self.frame.observation.rgb.stamp.sequence

    def validate_owner_seal(self,context):
        """최종 참조 채택용 작은 원 object/범위 검사다. 원문 decode/encode는 수행하지 않는다."""
        seal=self._token
        if (type(context) is not StorageContext or context!=self.storage_context
                or type(seal) is not _ProofSeal or self.frame is not seal.frame or self.snapshot is not seal.snapshot
                or self.record is not seal.record or self.record_context is not seal.record_context
                or seal.context is not self.storage_context or seal.grant.request.job.context is not seal.context
                or not seal.grant.request.job.issued_us<=seal.grant.issued_us<=seal.grant.received_us<=seal.admitted_j_us
                    <seal.grant.expires_us<=seal.grant.request.job.expires_us
                or not seal.grant.start_inclusive<=self.record.provenance_id<seal.grant.end_exclusive):
            raise ValueError('PC proof lost its original admitted D range/record references')
        return self

    def validate_for_catalog(self,context,*,now_us):
        """원 immutable closure만 검사한다. 최신 PC owner view는 prepare/최종채택 caller가 공급한다."""
        self.validate_owner_seal(context)
        self.frame.validate_original()
        s=self.snapshot;stamp=self.frame.observation.rgb.stamp
        s.validate_frame(job_id=s.job_id,source_generation=self.frame.binding.sha256,source_key=stamp.key,
            expires_us=self.frame.expires_j_us,now_us=now_us,context_bytes=self.context_bytes)
        if (s.pc_frame_proof!=self.frame.proof_bytes or s.pc_binding_bytes!=self.frame.binding.canonical_bytes
                or s.capture_stamp.captured_us!=self.frame.observation.capture_interval
                or s.metric_inputs.storage_context!=context or self.record.context_sha256!=self.record_context.sha256
                or self.record not in self.evidence.provenance or self.record_context not in self.evidence.contexts
                or self.metric_evidence.parent_ids!=(self.record.provenance_id,)):
            raise ValueError('PC catalog lost original snapshot/record/parent closure')
        return self


@dataclass(frozen=True)
class PcStorageDraft:
    proof: PcFrameProofDraft
    catalog: object
    recent_rgb: dict


@dataclass(frozen=True)
class PcStoragePending:
    role: str
    request: object


@dataclass(frozen=True)
class PcStorageTerminal:
    request: object
    delivery: StorageDelivery | None
    reason: str
    observed_us: int


def _reference(request,receipt,now):
    if (type(request) is not CheckpointRequest or request.payload.intent!='CHECKPOINT'
            or type(receipt) is not CommitReceipt
            or not receipt.current(request.job,request.payload,request.job.context.sha256,now)):
        raise PcStorageNotReady('original current CHECKPOINT receipt required')
    c=request.job.context;p=request.payload
    raw=canonical(dict(checkpoint_job_id=request.job.job_id,durable_revision=str(receipt.durable_revision),
        manifest_sha256=sha256(p.manifest).hexdigest(),manifest_length=len(p.manifest),tile_count=len(p.tile_references),
        map_epoch=c.map_epoch,frame_epoch=c.frame_epoch,odom_epoch=c.odom_epoch,pi_host_boot=c.pi_host_boot,
        configuration=c.configuration,calibration=c.calibration,primitive_sha256=c.primitive_sha256,
        origin_mapper_instance=c.origin_mapper_instance))
    selected_checkpoint_reference(raw)
    return raw


class PcMapperStorage:
    """단일 compute owner. 요청 UUID/nonce/E는 caller가 원 객체로 제공한다.

    EMPTY의 최초 head도 CHECKPOINT→실 COMMITTED ref→실 LOAD로만 얻는다. COMPLETE는
    원 자료를 보존하지만 PC catalog의 엄격 복원 연결 전에는 새 빈 catalog로 바꾸지 않는다.
    """

    def __init__(self,context,mailbox,binding,motion_profile,mapper_profile,*,clock,
                 initial_storage_material,pc_owner_token,complete=None,crossing=None):
        if (type(context) is not StorageContext or type(mailbox) is not MapperStorageMailbox
                or mailbox.context_sha256!=context.sha256 or type(binding) is not PcSourceBinding
                or type(motion_profile) is not DevelopmentMotionProfile or type(mapper_profile) is not MapperProfile
                or pc_owner_token is None or not callable(clock)):
            raise ValueError('exact current PC context/mailbox/profile/registered owner required')
        binding.check_profile(motion_profile)
        mapper_profile.check(motion_profile.bound_calibration,vehicle_configuration=context.configuration,
            vehicle_calibration=context.calibration)
        publication=json_bytes(context.source_binding,2048)
        if (publication['raw_binding_sha256']!=binding.sha256
                or publication['service_instance']!=binding.fields['service_instance']
                or publication['mapper_worker']!=context.mapper_worker
                or publication['mapper_instance']!=context.origin_mapper_instance):
            raise ValueError('PC descriptor differs from actual publication context')
        if type(initial_storage_material) is not tuple or len(initial_storage_material)!=2:
            raise ValueError('actual original D binding/bootstrap required')
        material=json_bytes(canonical(dict(binding=initial_storage_material[0],bootstrap=initial_storage_material[1])),2048)
        db,boot=material['binding'],material['bootstrap']
        if (set(db)!=set(('service_instance','storage_worker','storage_profile_sha256','database_uuid','db_schema_version'))
                or db.get('service_instance')!=binding.fields['service_instance']
                or db.get('database_uuid')!=context.database_uuid or boot.get('database_uuid')!=context.database_uuid
                or db.get('db_schema_version')!=1 or boot.get('schema_version')!=1
                or boot.get('allocator_ready') is not True or boot.get('status') not in ('EMPTY','COMPLETE')
                or boot['status']=='EMPTY' and boot.get('selected_checkpoint') is not None):
            raise ValueError('actual allocator-ready EMPTY/COMPLETE D material required')
        uuid(db['storage_worker']);digest(db['storage_profile_sha256'])
        self.context,self.mailbox,self.binding=context,mailbox,binding
        self.motion_profile,self.profile,self.clock=motion_profile,mapper_profile,clock
        self._pc_owner_token=pc_owner_token;self._thread=get_ident();self._last_now=None
        self._material=canonical(material);self._bootstrap=boot['status'];self._complete=None
        self._checkpoint_ref=None if self._bootstrap=='EMPTY' else canonical(boot['selected_checkpoint'])
        if self._checkpoint_ref is not None:selected_checkpoint_reference(self._checkpoint_ref)
        self._grant=self._pending=self._last_terminal=self._terminal=self._staged=None
        self._range_request=None;self._grant_admitted_us=None
        self._next=0;self._highwater=0;self._submitted=False;self._fault=None
        self._unknown=False
        self._evidence=EvidenceTable();self._retired=None;self.catalogs=None;self._head=None
        self._accepted_frame_key=None;self._recent_rgb={}
        policy=mapper_profile.catalog_policy
        self._crossing=crossing if crossing is not None or policy is None else policy.crossing
        if complete is not None:self.adopt_complete(complete)

    def _owner(self):
        if get_ident()!=self._thread:raise RuntimeError('PC storage belongs to original compute owner')

    def _now(self,now):
        self._owner();uint(now)
        if self._last_now is not None and now<self._last_now:raise ValueError('PC storage J time regressed')
        self._last_now=now

    @property
    def pending(self):self._owner();return self._pending
    @property
    def last_terminal(self):self._owner();return self._last_terminal
    @property
    def checkpoint_ref(self):self._owner();return self._checkpoint_ref
    @property
    def head(self):self._owner();return self._head
    @property
    def range_grant(self):self._owner();return self._grant
    @property
    def provenance_cursor(self):self._owner();return self._next
    @property
    def evidence(self):self._owner();return self._evidence
    @property
    def ready(self):
        self._owner()
        return self._bootstrap=='EMPTY' and self._fault is None and self._grant is not None and self._next<self._grant.end_exclusive
    @property
    def status(self):
        self._owner()
        if self._fault is not None:return self._fault
        if self._unknown:return 'WAIT_ORIGINAL_UNKNOWN'
        if self._pending is not None:return 'WAIT_ORIGINAL_'+self._pending.role
        if self._bootstrap=='COMPLETE':return 'WAIT_SOURCE_COMPLETE' if self._complete is None else 'PC_COMPLETE_RESTORE_NOT_READY'
        return 'RAM_READY' if self.ready else 'NEEDS_PROVENANCE'

    def adopt_complete(self,complete):
        self._owner()
        from .map_checkpoint import SourceComplete
        if (self._bootstrap!='COMPLETE' or type(complete) is not SourceComplete
                or complete.checkpoint_ref!=self._checkpoint_ref
                or complete.original_job.context.database_uuid!=self.context.database_uuid):
            raise ValueError('actual original selected SourceComplete required')
        if self._complete is not None and self._complete is not complete:
            raise ValueError('original SourceComplete cannot be replaced')
        self._complete=complete

    def _queue(self,role,request,now):
        self._now(now)
        if self._fault is not None:raise PcStorageNotReady(self._fault)
        if request.job.context is not self.context or not request.job.current(self.context.sha256,now):
            raise ValueError('current original job/context required')
        if self._pending is not None:
            if self._pending.role==role and self._pending.request is request:return request
            raise PcStorageNotReady('one original storage job is unresolved')
        if (self._staged is not None or self.mailbox.main_peek() is not None
                or self.catalogs is not None and (self.catalogs.pending is not None or self.catalogs._queued is not None)):
            raise PcStorageNotReady('geometry/catalog/mailbox is not idle')
        self._pending=PcStoragePending(role,request);self._submitted=False;self._unknown=False
        return request

    def queue_provenance(self,request,*,now_us):
        if type(request) is not ProvenanceRequest:raise ValueError('exact original ProvenanceRequest required')
        if self._range_request is not None:
            if request is self._range_request and self._pending is not None:return self._queue('PROVENANCE',request,now_us)
            if (self._grant is None or self._next<self._grant.end_exclusive
                    or request.job.job_id==self._range_request.job.job_id
                    or request.reservation_nonce==self._range_request.reservation_nonce):
                raise PcStorageNotReady('new explicit range requires exhausted original and distinct job/nonce')
        result=self._queue('PROVENANCE',request,now_us);self._range_request=request
        return result

    def queue_checkpoint(self,request,*,now_us):
        if (type(request) is not CheckpointRequest or request.payload.intent!='CHECKPOINT'
                or json_bytes(request.payload.manifest,16384)['scene'] is not None):
            raise ValueError('exact original CHECKPOINT request required')
        return self._queue('CHECKPOINT',request,now_us)

    def queue_head_load(self,request,*,now_us):
        if (type(request) is not LoadRequest or request.purpose!='CURRENT_MAP' or self._checkpoint_ref is None
                or request.checkpoint_ref!=self._checkpoint_ref
                or json_bytes(request.selection,512)!=dict(kind='CHECKPOINT_PAGE',cursor=0)):
            raise ValueError('original selected checkpoint page0 LOAD required; no standalone HEAD request')
        return self._queue('HEAD_LOAD',request,now_us)

    def queue_catalog_checkpoint(self,job,*,now_us,tiles=(),tile_references=None,durable_cutoff_pi_us):
        """compute maintenance의 실제 사본/원 job만 받는다. 일반 frame의 동기 fsync가 아니다."""
        self._now(now_us)
        if self._fault is not None or self._pending is not None or self._staged is not None or self.catalogs is None:
            raise PcStorageNotReady('accepted PC catalogs and idle original storage phase required')
        return self.catalogs.prepare_checkpoint(job,base_evidence=self._evidence,now_us=now_us,
            tiles=tiles,tile_references=tile_references,durable_cutoff_pi_us=durable_cutoff_pi_us,scene=None)

    def _consume(self,delivery,now):
        pending=self._pending
        if type(delivery) is not StorageDelivery or pending is None or delivery.request is not pending.request:
            raise ValueError('delivery lost exact original request identity')
        # LOAD decode가 계산 시간을 쓸 수 있으므로 현재 수락은 원 결과를 받은 뒤 다시 잰다.
        now=self.clock();self._now(now)
        result=delivery.admitted_result;request=pending.request
        from .storage_messages import DbMessage
        unknown=(type(result) is CommitReceipt and result.status=='UNKNOWN'
            or type(result) is DbMessage and result.op=='RANGE_RESULT' and result.body['result']=='UNKNOWN')
        current_job=request.job.current(self.context.sha256,now)
        reason='WAIT_ORIGINAL_UNKNOWN' if unknown else delivery.reason if current_job else 'ORIGINAL_STORAGE_EXPIRED'
        terminal=PcStorageTerminal(request,delivery,reason,now);self._last_terminal=self._terminal=terminal
        self._unknown=unknown
        if unknown:return
        if (not delivery.current or not current_job
                or type(delivery.result) is StorageAbort):
            self._fault='ORIGINAL_STORAGE_TERMINAL';return
        if pending.role=='PROVENANCE':
            if (type(result) is not WireProvenanceGrant or result.request is not request
                    or not result.current(request,self.context.sha256,now) or result.start_inclusive<self._highwater):
                self._fault='ORIGINAL_PROVENANCE_NOT_CURRENT';return
            self._grant=result;self._grant_admitted_us=now
            self._next=result.start_inclusive;self._highwater=result.end_exclusive
        elif pending.role=='CHECKPOINT':self._checkpoint_ref=_reference(request,result,now)
        elif pending.role=='HEAD_LOAD':
            if type(result) is not LoadedCheckpoint or result.request is not request or not result.heads.current(request.job,self.context.sha256,now):
                self._fault='ORIGINAL_LOAD_HEAD_NOT_CURRENT';return
            self._head=(result.heads,request.job)
            if self.catalogs is not None:self.catalogs.observe_heads(result.heads,request.job,now_us=now)
        self._pending=None;self._submitted=False

    def maintenance(self,*,now_us,catalog_arguments=None):
        self._now(now_us)
        if self._staged is not None:raise PcStorageNotReady('original geometry draft is not terminal')
        self._retired=None  # 최종 채택 밖 compute 유지 단계에서만 옛 proof 참조를 해제한다.
        self.mailbox.compute_encode_pending(current_context_sha256=self.context.sha256,now_us=now_us,clock_us=self.clock)
        if self.mailbox.context_sha256!=self.context.sha256:
            self._fault='SOURCE_CONTEXT_FENCED'
            head=self.mailbox.main_peek()
            if self._pending is not None and head is not None and head.request is self._pending.request:
                delivery=self.mailbox.compute_take(current_context_sha256=self.mailbox.context_sha256,now_us=now_us)
                if delivery is not None:
                    self._last_terminal=self._terminal=PcStorageTerminal(self._pending.request,delivery,'SOURCE_CONTEXT_FENCED',now_us)
            elif self.catalogs is not None and self.catalogs.pending is not None:
                self.catalogs.maintenance(now_us=now_us)
            return self.status
        if self._pending is not None:
            request=self._pending.request;head=self.mailbox.main_peek()
            if head is not None and head.request is request:
                delivery=self.mailbox.compute_take(current_context_sha256=self.context.sha256,now_us=now_us)
                if delivery is not None:
                    self._consume(delivery,now_us);now_us=self._last_now
            if self._fault is not None:return self.status
            if self._pending is not None:
                if now_us>=request.job.expires_us:
                    self._fault='ORIGINAL_STORAGE_EXPIRED';return self.status
                if self.mailbox.main_peek() is None:
                    self._submitted|=self.mailbox.compute_offer(request,now_us=now_us)
                return self.status
        if self._fault is not None:return self.status
        if self.catalogs is not None:
            result=self.catalogs.maintenance(now_us=now_us,**({} if catalog_arguments is None else catalog_arguments))
            terminal=self.catalogs.take_terminal()
            if terminal is not None:
                self._last_terminal=self._terminal=PcStorageTerminal(terminal.request,None,terminal.reason,terminal.observed_us)
                self._fault='ORIGINAL_CATALOG_TERMINAL'
            delivery=self.catalogs.take_delivery()
            if delivery is not None:
                admitted=delivery.admitted_result;request=delivery.request
                checked=self.clock();self._now(checked)
                from .storage_messages import DbMessage
                unknown=(type(admitted) is CommitReceipt and admitted.status=='UNKNOWN'
                    or type(admitted) is DbMessage and admitted.op=='RANGE_RESULT' and admitted.body['result']=='UNKNOWN')
                self._unknown=unknown
                current_job=request.job.current(self.context.sha256,checked)
                self._last_terminal=self._terminal=PcStorageTerminal(request,delivery,
                    'WAIT_ORIGINAL_UNKNOWN' if unknown else delivery.reason if current_job else 'ORIGINAL_CATALOG_EXPIRED',checked)
                if not unknown:
                    if (not delivery.current or not current_job
                            or type(delivery.result) is StorageAbort):
                        self._fault='ORIGINAL_CATALOG_TERMINAL'
                    elif type(request) is CheckpointRequest:
                        if request.payload.intent=='CHECKPOINT':self._checkpoint_ref=_reference(request,admitted,checked)
                        else:self._head=None  # 다음 CAS는 새 실제 LOAD head가 필요하다.
            if self._fault is not None:return self.status
            return self.status if result=='IDLE' else result
        return self.status

    def prepare_frame(self,frame,snapshot,result,*,current_pc_view,capture_prior,registration_revision,
                      prior_qualified,now_us,roi_bounds,current_sensors=None):
        self._now(now_us)
        if not self.ready or self._staged is not None:raise PcStorageNotReady('current original D range and idle candidate required')
        if type(frame) is not PcFrameInput or type(snapshot) is not SensorFrameSnapshot:
            raise ValueError('exact original PC frame/snapshot required')
        frame.validate_original();frame.validate_current(current_pc_view,now_us,binding=self.binding,owner_token=self._pc_owner_token)
        observation=frame.observation;inputs=snapshot.metric_inputs
        key=observation.rgb.stamp.key
        if self._accepted_frame_key is not None and key[:3]==self._accepted_frame_key[:3] and key[3]<=self._accepted_frame_key[3]:
            raise PcStorageNotReady('original PC frame sequence was already accepted')
        snapshot.validate_frame(job_id=snapshot.job_id,source_generation=self.binding.sha256,source_key=observation.rgb.stamp.key,
            expires_us=frame.expires_j_us,now_us=now_us,context_bytes=snapshot.source_context_bytes)
        if (snapshot.calibration!=self.motion_profile.bound_calibration
                or inputs.storage_context!=self.context or inputs.clock_lease is None or inputs.pose is None
                or inputs.pose.sample.origin!='SYNTHETIC_ODOMETRY'
                or inputs.vehicle is None or snapshot.sensors is None
                or snapshot.sensors.context.domain!='DEVELOPMENT_SYNTHETIC'
                or snapshot.pc_frame_proof!=frame.proof_bytes or snapshot.pc_binding_bytes!=self.binding.canonical_bytes
                or observation.nn is None or observation.nn.packet.context_key!=self.binding.fields['nn_manifest_sha256']):
            raise PcStorageNotReady('original PC sensor/NN/model/context proof required')
        if current_sensors is None:
            if not snapshot.sensors.current_vehicle(inputs.vehicle,now_us):raise PcStorageNotReady('CURRENT_VEHICLE_EXPIRED')
            proof_at=now_us
        else:
            from .pc_current_sensors import PcCurrentSensors
            if type(current_sensors) is not PcCurrentSensors:raise ValueError('actual current sensor factory proof required')
            current_sensors.validate_current(snapshot,owner_token=self._pc_owner_token,
                fence_revision=snapshot.fence_revision,now_us=now_us)
            if current_sensors.storage_context!=self.context:raise PcStorageNotReady('CURRENT_SENSOR_CONTEXT_CHANGED')
            proof_at=snapshot.snapshot_j_us
        nn=observation.nn_key;packet=observation.nn.packet
        if (type(nn) is not tuple or len(nn)!=8 or nn[:4]!=observation.rgb.stamp.key
                or nn[4:6]!=(packet.device_us,packet.sdk_us) or nn[-2]!=packet.context_key
                or nn[-1]!=frame.proof_fields['packet_header']['rgb']['sha256']
                or packet.sequence!=observation.rgb.stamp.sequence or packet.received_j_us>now_us):
            raise PcStorageNotReady('original PC NN full enqueue key/payload differs')
        snapshot.original_pose_bracket(inputs.pose);snapshot.original_vehicle_admission(inputs.vehicle)
        keys=tuple((self.binding.fields['stream_instances'][name],image.stamp.sequence,at)
            for name,image in (('rgb_video',observation.rgb),('depth_right',observation.depth)) for at in image.stamp.captured_us)
        lo,hi=inputs.clock_lease.j_interval_to_pi(observation.capture_interval,now_j_us=proof_at)
        body=dict(profile='pc-frame-evidence-1',input_kind='SYNTHETIC',clock_basis=PC_CLOCK_BASIS,
            pc_source_binding=json_bytes(self.binding.canonical_bytes,2048),pc_frame_proof=json_bytes(frame.proof_bytes,16384),
            storage_context_sha256=self.context.sha256,source_context_sha256=sha256(snapshot.source_context_bytes).hexdigest(),
            frame_job_id=snapshot.job_id,geometry_expires_j_us=str(frame.expires_j_us),nn_source_key=observation.nn_key,
            original_probe_sha256=sha256(inputs.clock_lease.raw_probe_bytes).hexdigest(),original_trust_sha256=sha256(inputs.clock_lease.trust_bytes).hexdigest())
        try:record_context=ContextRecord(canonical(dict(kind='RAW',body=body)))
        except ValueError as error:raise PcStorageNotReady('PC_FRAME_CONTEXT_EXCEEDS_ORIGINAL_REPRESENTATION') from error
        identity=self._next;self._next+=1
        record=ProvenanceRecord(identity,PROVENANCE.pack(bytes.fromhex(record_context.sha256),3,0,4,0,
            UUID(self.context.pi_host_boot).bytes,lo,hi,observation.rgb.stamp.sequence,0,0,0,0,
            b''.join(struct.pack('<16sQQ',UUID(stream).bytes,seq,at) for stream,seq,at in keys),0,bytes(16)))
        active=result.temporal_rgb_sources | result.parking_rgb_sources
        recent={k:pair for k,pair in self._recent_rgb.items() if k in active}
        if key in active:recent[key]=record,record_context
        contexts={c.sha256:c for c in self._evidence.contexts};contexts[record_context.sha256]=record_context
        records={r.provenance_id:r for r in self._evidence.provenance};records[identity]=record
        def temporal_root_for(sources):
            if (key not in sources or not 2<=len(sources)<=12 or not sources.issubset(recent)
                    or self._next+(len(sources)+1)//3>self._grant.end_exclusive):
                return None
            originals=[]
            for source in sorted(sources-{key}):
                parent,parent_context=recent[source];originals.append(parent)
                records[parent.provenance_id]=parent;contexts[parent_context.sha256]=parent_context
            def allocate(_parents):
                identity=self._next;self._next+=1
                return identity
            for aggregate in aggregate_rgb_provenance(record,originals,allocate):
                records[aggregate.provenance_id]=aggregate
            return aggregate.provenance_id
        temporal=result.mapped_road
        temporal_root=(temporal_root_for(temporal.sources) if temporal is not None and prior_qualified
                       and self._crossing is not None and temporal.within(self.profile.pipeline.registration) else None)
        parking_root=(temporal_root_for(frozenset((key,*(source for source,_ in result.parking_tape_history))))
                      if prior_qualified and result.parking_tape_history else None)
        try:evidence=EvidenceTable(tuple(records[k] for k in sorted(records)),tuple(contexts[k] for k in sorted(contexts)))
        except ValueError as error:raise PcStorageNotReady('ORIGINAL_PC_EVIDENCE_CAPACITY') from error
        metric=MetricEvidence((observation.rgb.stamp.key,observation.depth.stamp.key),self.context.pi_host_boot,
            (lo,hi),record.observation_sequence,3,keys,(identity,))
        proof=object.__new__(PcFrameProofDraft)
        seal=object.__new__(_ProofSeal)
        for name,value in dict(frame=frame,snapshot=snapshot,record=record,record_context=record_context,
                grant=self._grant,admitted_j_us=self._grant_admitted_us,context=self.context).items():object.__setattr__(seal,name,value)
        for name,value in dict(frame=frame,snapshot=snapshot,storage_context=self.context,record=record,
                record_context=record_context,metric_evidence=metric,evidence=evidence,_token=seal).items():object.__setattr__(proof,name,value)
        if self.catalogs is None:
            self.catalogs=MapperCatalogs(self.context,mailbox=self.mailbox,clock_id=self.binding.clock_id,
                map_frame_id=result.map_snapshot['frame_id'],body=self.motion_profile.bound_calibration.body,
                method_manifest_sha256=self.profile.sha256,input_kind='SYNTHETIC',calibration_profile=self.motion_profile.bound_calibration,
                parking_aisles=self.profile.parking_aisles,
                aisle_lane_indices=None if self.profile.catalog_policy is None else self.profile.catalog_policy.aisle_lane_indices,
                crossing=self._crossing,lane_profile=self.profile.pipeline.registration,clock_us=self.clock)
            if self._head is not None:self.catalogs.observe_heads(*self._head,now_us=now_us)
        draft=self.catalogs.prepare_pc(proof,result,capture_prior=capture_prior,registration_revision=registration_revision,
            prior_qualified=prior_qualified,now_us=now_us,roi_bounds=roi_bounds,temporal_provenance_id=temporal_root,
            parking_provenance_id=parking_root)
        self._staged=PcStorageDraft(proof,draft,recent)
        return self._staged

    def accepted(self,draft):
        """최종frame/source/PCview 재검산을마친caller만호출한다. 여기서는RAM참조만채택한다."""
        self._owner()
        if draft is not self._staged:raise ValueError('exact original PC storage draft required')
        draft.proof.validate_owner_seal(self.context)
        self._retired=self._evidence
        self.catalogs.accept(draft.catalog);self._evidence=draft.catalog.state.evidence
        self._accepted_frame_key=draft.proof.frame.observation.rgb.stamp.key;self._recent_rgb=draft.recent_rgb;self._staged=None

    def cancelled(self,draft):
        self._owner()
        if draft is not self._staged:raise ValueError('exact original PC storage draft required')
        self.catalogs.discard(draft.catalog);self._staged=None

    def take_terminal(self):
        self._owner();result=self._terminal;self._terminal=None;return result
