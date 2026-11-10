"""실제 PC M compute processor와 원 비동기 D/bootstrap·World 발행 접점이다.

main은 원 SourceACK/context/material, mailbox I/O와 parser/World 실제 송신을 소유한다.
이 owner는 parse/core·encode/intent만 계산한다. local Store/SDK/Device/actor/thread는
생성하지 않는다. CHECKPOINT 성공은 catalog proof의 내구 사실이며 full map/Scene
복원이나 RPi 주행 성공을 뜻하지 않는다. ordinary Scene 없는 World만 다룬다.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import get_ident
from uuid import uuid4

from .mapper_compute import MapperCancelled, MAX_RESULT_BYTES
from .mapper_mapping import MappingContextPort
from .mapper_profile import MapperProfile
from .mapper_publication import MapperPublicationPort, PublicationReport
from .mapper_storage import MapperStorageMailbox
from .mapper_catalogs import CatalogNotReady
from .pc_compute import PcMapperCompute, PcMapperInput
from .pc_mapper_core import PcMapperCore, PcCoreNotReady
from .pc_mapper_storage import PcMapperStorage, PcStorageNotReady
from ..oak.sim_nn_manifest import SimNnManifest
from ..simulation.motion_profile import DevelopmentMotionProfile
from ..simulation.pc_input import PcSourceBinding
from ..storage.contracts import (StorageJob, ProvenanceRequest, LoadRequest, CommitReceipt,
    MAX_U63, canonical, json_bytes, uint)
from ..storage.port import CheckpointRequest
from ..storage.records import CheckpointPayload, LoadedCheckpoint


@dataclass(frozen=True)
class _Stage:
    job_id: str
    candidate: object
    report: PublicationReport | None
    processed: int


class PcMapperProcessor:
    """factory와 모든 메서드는 실제 compute thread 하나에서만 호출한다.

    job/nonce는 maintenance의 새 명시 정책 선택에서만 한 번 만든다. 실패·UNKNOWN·
    원 expiry를 새 거래로 덮지 않는다. 성공한 head LOAD의 기한이 끝났을 때만 다음
    실제 head 요청을 만들 수 있다. per-frame 동기 fsync나 즉시 receipt adapter는 없다.
    """

    def __init__(self,motion_profile,mapper_profile,binding,manifest,*,compute,context_port,mailbox,
                 publication_port,pc_owner_token,clock,checkpoint_interval_us=2_000_000,
                 provenance_block_count=1<<20,job_lifetime_us=5_000_000):
        if (type(motion_profile) is not DevelopmentMotionProfile or type(mapper_profile) is not MapperProfile
                or type(binding) is not PcSourceBinding or type(manifest) is not SimNnManifest
                or type(compute) is not PcMapperCompute or type(context_port) is not MappingContextPort
                or type(mailbox) is not MapperStorageMailbox or type(publication_port) is not MapperPublicationPort
                or publication_port.context_port is not context_port or not callable(clock) or pc_owner_token is None
                or manifest.sha256!=binding.fields['nn_manifest_sha256']):
            raise ValueError('actual PC compute/context/mailbox/publication/profile references required')
        binding.check_profile(motion_profile)
        uint(checkpoint_interval_us,5_000_000,positive=True);uint(provenance_block_count,1<<20,positive=True)
        uint(job_lifetime_us,5_000_000,positive=True)
        self.motion,self.profile,self.binding,self.manifest=motion_profile,mapper_profile,binding,manifest
        self.compute,self.context_port,self.mailbox,self.publication_port=compute,context_port,mailbox,publication_port
        self.clock,self._pc_owner_token=clock,pc_owner_token;self._thread=get_ident()
        self._interval,self._block_count,self._job_lifetime=checkpoint_interval_us,provenance_block_count,job_lifetime_us
        self.storage=self.core=None;self._context=None;self._material=None
        self._range_request=self._seed_request=self._head_request=None;self._head_completed=False
        self._range_head=self._range_nonce=self._parking_job=self._parking_nonce=None
        self._checkpoint=self._checkpoint_revision=None;self._prepare_revision=None
        self._dirty_revision=self._saved_revision=0;self._dirty_since=self._last_commit_us=None
        self._last_terminal=None
        self._staged=self._retired_result=None;self._processed=0;self._admission_revision=0
        self._status='WAIT_SOURCE_CONTEXT';self._fault=None;self._last_now=None

    def _owner(self):
        if get_ident()!=self._thread:raise RuntimeError('PC processor belongs to original compute owner')

    def _now(self):
        now=self.clock();uint(now)
        if self._last_now is not None and now<self._last_now:raise ValueError('PC processor J time regressed')
        self._last_now=now;return now

    @property
    def status(self):self._owner();return self._fault or self._status
    @property
    def last_terminal(self):self._owner();return self._last_terminal

    def _job(self,now):
        if now>MAX_U63-self._job_lifetime:raise OverflowError('original storage job E exceeds bound')
        return StorageJob(self._context,str(uuid4()),now,now+self._job_lifetime)

    def _ensure_storage(self):
        with self.context_port.adoption_lock:
            context,raw=self.context_port.read(),self.context_port.read_material()
        if context is None or raw is None:self._status='WAIT_SOURCE_CONTEXT';return False
        if self.storage is not None:
            if context is not self._context:
                self._fault='SOURCE_CONTEXT_FENCED';return False
            return True
        if self.mailbox.context_sha256!=context.sha256:self._status='WAIT_MAILBOX_CONTEXT';return False
        value=json_bytes(raw,2048)
        if set(value)!={'binding','bootstrap'}:raise ValueError('original D binding/bootstrap material required')
        self.storage=PcMapperStorage(context,self.mailbox,self.binding,self.motion,self.profile,clock=self.clock,
            initial_storage_material=(value['binding'],value['bootstrap']),pc_owner_token=self._pc_owner_token,
            crossing=None if self.profile.catalog_policy is None else self.profile.catalog_policy.crossing)
        self._context,self._material=context,raw
        return True

    def _ensure_core(self,item,facts):
        if not self._ensure_storage():raise PcCoreNotReady(self.status)
        # D의 원 terminal은 새 proof/ID/World를 막지만 유효한 RAM 관측까지 버리지 않는다.
        if self._fault=='SOURCE_CONTEXT_FENCED':raise PcCoreNotReady(self._fault)
        if self.core is None:
            source=canonical(item.sensor_snapshot.metric_inputs.source)
            self.core=PcMapperCore(self.motion,self.profile,self.binding,self._context,self.storage,
                source_bytes=source,clock=self.clock,pc_owner_token=self._pc_owner_token)
        return self.core

    def parse(self,item,checkpoint):
        self._owner()
        if type(item) is not PcMapperInput:raise ValueError('original PcMapperInput required')
        from .pc_job_messages import parse_pc_nn_admission
        checkpoint()
        try:candidates,metadata=parse_pc_nn_admission(item.nn_admission,self.manifest,clock=self.clock)
        except ValueError:
            checkpoint()  # expiry/source 취소를 parser/config 오류와 구분한다.
            raise
        checkpoint('PC_NN_DECODE_COMPLETED')
        self.compute.record_parser_completion(item,metadata)
        return item.nn_admission.with_candidates(candidates)

    def _world_report(self,item,candidate):
        built=candidate.world
        if built is None:return None,','.join(candidate.reasons) or 'WORLD_NOT_READY'
        from xenova_drive_world import decode,schema
        alignment=candidate._pipeline._alignment
        if alignment is None:return None,'LOCALIZATION_NOT_READY'
        # World를 실제 만든 원 lease만 E 상한에 쓴다. 뒤 renewal로 후보 E를 늘리지 않는다.
        lease=candidate._world_clock_lease
        if lease is None:return None,'WORLD_CLOCK_UNAVAILABLE'
        trust=decode(lease.trust_bytes,schema.WorldTrust)
        issued=built.source.snapshot_us
        expires=min(item.expires_us,alignment.observed_j_interval[0]+alignment.valid_for_us,
            lease.expires_pi_us-trust.upper_offset_us)
        if not issued<=self._now()<expires<2**63:return None,'WORLD_EXPIRED'
        try:return PublicationReport(self._context,'WORLD_CANDIDATE',built.payload,issued,expires,
            sha256(self._context.source_binding).hexdigest()),'READY'
        except ValueError:return None,'WORLD_REPORT_NOT_READY'

    def geometry(self,item,parsed,checkpoint):
        self._owner();checkpoint()
        if self._staged is not None:raise RuntimeError('previous PC geometry candidate is not terminal')
        if item.purpose=='PARSER_WARMUP':return None
        facts=self.compute.current_facts(item)
        if facts.current_sensors is None:raise MapperCancelled('CURRENT_SENSORS_UNAVAILABLE')
        try:core=self._ensure_core(item,facts)
        except PcCoreNotReady as error:raise MapperCancelled(str(error)[:64]) from error
        facts=None  # stage provider와 별개로 초기 current proof를 붙잡지 않는다.
        candidate=None
        try:
            candidate=core.prepare(parsed,item.sensor_snapshot,current_pc_view=None,checkpoint=checkpoint,
                current_provider=lambda:self.compute.current_facts(item))
            facts=self.compute.current_facts(item)
            if facts.current_sensors is None:raise MapperCancelled('CURRENT_SENSORS_UNAVAILABLE')
            report,reason=self._world_report(item,candidate)
            summary=dict(candidate.result.summary,mapper_profile_sha256=self.profile.sha256,
                metric_input_reasons=list(item.sensor_snapshot.metric_inputs.reasons),
                source_vehicle_sequence=facts.current_sensors.vehicle.anchor.sequence,
                sensor_fence_revision=facts.fence_revision,storage_status=self.storage.status,
                world_candidate_ready=report is not None,world_candidate_reason=reason,
                scene_status='PC_SCENE_NOT_SUPPORTED',metric_ready=False,live_authority=False)
            processed=self._processed+1
            payload=json.dumps(dict(processed=processed,latest=summary),sort_keys=True,
                ensure_ascii=True,separators=(',',':'),allow_nan=False).encode('ascii')
            if len(payload)>MAX_RESULT_BYTES:raise ValueError('PC control summary exceeds original result bound')
            checkpoint()
            self._staged=_Stage(item.job_id,candidate,report,processed)
            return payload
        except ValueError as error:
            if candidate is not None:core.cancelled(candidate)
            checkpoint()  # 실제 current/원 E 상실이면 scheduler의 취소 사유를 보존한다.
            if isinstance(error,PcCoreNotReady):raise MapperCancelled(str(error)[:64]) from error
            raise
        except BaseException:
            if candidate is not None:core.cancelled(candidate)
            raise

    def accepted_at(self,item,checked_j_us):
        self._owner();uint(checked_j_us)
        if item.purpose=='PARSER_WARMUP':return
        staged=self._staged
        if staged is None or staged.job_id!=item.job_id:raise RuntimeError('original PC geometry stage required')
        if self.context_port.read() is not self._context:raise MapperCancelled('PUBLICATION_CHANGED')
        try:self.core.accepted_at(staged.candidate,compute=self.compute,item=item,checked_j_us=checked_j_us)
        except ValueError as error:raise MapperCancelled('PC_CURRENT_ADOPTION_FENCED') from error
        report=staged.report
        if report is not None and report.issued_us<=checked_j_us<report.expires_us:
            self.publication_port.offer(report,admission_revision=self._admission_revision)
        if staged.candidate.storage_draft is not None:
            self._dirty_revision+=1
            if self._dirty_since is None:self._dirty_since=checked_j_us
        self._retired_result=staged.candidate.result
        self._processed=staged.processed;self._staged=None

    def cancelled(self,item,reason):
        self._owner()
        if self._staged is not None:
            if self._staged.job_id!=item.job_id:raise RuntimeError('PC cancellation changed original job')
            self.core.cancelled(self._staged.candidate,reason);self._staged=None

    def admission_fenced(self,revision,reason):
        self._owner();uint(revision)
        if revision<self._admission_revision:raise ValueError('PC admission fence regressed')
        self._admission_revision=revision
        self.publication_port.fence_admission(revision)

    def _consume_terminal(self):
        terminal=self.storage.take_terminal()
        if terminal is None:return
        self._last_terminal=terminal;delivery=terminal.delivery
        if self.storage.status=='WAIT_ORIGINAL_UNKNOWN':return
        if delivery is None or not delivery.current or not terminal.request.job.current(self._context.sha256,terminal.observed_us):
            self._fault='ORIGINAL_STORAGE_TERMINAL';return
        request=terminal.request;result=delivery.admitted_result
        if request is self._head_request and type(result) is LoadedCheckpoint:self._head_completed=True
        if self._range_head is not None and request.job is self._range_head[1] and type(request) is CheckpointRequest:
            self._range_head=self._range_nonce=None
        if self._parking_job is request.job:self._parking_job=self._parking_nonce=None
        if request is self._checkpoint and type(result) is CommitReceipt and result.status in ('COMMITTED','DUPLICATE'):
            self._saved_revision=self._checkpoint_revision;self._last_commit_us=terminal.observed_us
            self._dirty_since=None;self._checkpoint=self._checkpoint_revision=None

    def _idle(self):
        catalog=self.storage.catalogs
        return (self.storage.pending is None and self.mailbox.main_peek() is None and self._staged is None
            and (catalog is None or catalog.pending is None and catalog._queued is None))

    def _queue_original(self,method,request,now):
        if not request.job.current(self._context.sha256,now):
            self._fault='ORIGINAL_UNOFFERED_STORAGE_EXPIRED';return False
        try:method(request,now_us=now)
        except PcStorageNotReady:return False
        return True

    def _current_cutoff(self,now):
        # 실제 held input만 본다. 끝난 frame을 보존해 다음 Vehicle과 결합하지 않는다.
        rows=self.compute.current_metric_facts()
        for snapshot,facts in sorted(rows,key=lambda row:row[1].updated_j_us,reverse=True):
            current=facts.current_sensors
            try:
                checked=self._now()
                if current.storage_context!=self._context:continue
                if self.core is not None and current.source_bytes!=self.core._source_bytes:continue
                snapshot.validate_frame(job_id=snapshot.job_id,source_generation=self.binding.sha256,
                    source_key=snapshot.source_key,expires_us=snapshot.expires_us,now_us=checked,
                    fence_revision=facts.fence_revision,context_bytes=facts.source_context_bytes)
                current.validate_current(snapshot,owner_token=self._pc_owner_token,
                    fence_revision=facts.fence_revision,now_us=checked)
                return current.clock_lease.j_interval_to_pi((checked,checked),now_j_us=checked)[1]
            except ValueError:continue
        return None

    def maintenance(self):
        """실제 mailbox 완료를 소비하고 새 명시 D 거래만 선택한다. 자체 I/O는 없다."""
        self._owner();self._retired_result=None;now=self._now()
        if self._staged is not None:raise RuntimeError('PC geometry stage is not terminal')
        ready=self._ensure_storage()
        if self.storage is None:return self.status
        arguments={}
        if self._range_nonce is not None:arguments['reservation_nonce']=self._range_nonce
        if self._parking_job is not None:arguments.update(provenance_job=self._parking_job,provenance_nonce=self._parking_nonce)
        status=(self.core.maintenance(now_us=now,catalog_arguments=arguments) if self.core is not None
            else self.storage.maintenance(now_us=now,catalog_arguments=arguments))
        self._consume_terminal()
        if not ready or self._fault is not None:return self.status
        if self.storage.status in ('WAIT_SOURCE_COMPLETE','PC_COMPLETE_RESTORE_NOT_READY'):
            self._status='PC_COMPLETE_RESTORE_NOT_READY';return self.status
        if self.storage.status in ('ORIGINAL_STORAGE_TERMINAL','ORIGINAL_STORAGE_EXPIRED','ORIGINAL_CATALOG_TERMINAL',
                'ORIGINAL_PROVENANCE_NOT_CURRENT','ORIGINAL_LOAD_HEAD_NOT_CURRENT','SOURCE_CONTEXT_FENCED'):
            self._fault=self.storage.status;return self.status
        if self.storage.status=='WAIT_ORIGINAL_UNKNOWN':self._status='WAIT_ORIGINAL_UNKNOWN';return self.status
        if not self._idle():self._status='WAIT_ORIGINAL_STORAGE';return self.status
        now=self._now()
        if self.storage.range_grant is None:
            if self._range_request is None:self._range_request=ProvenanceRequest(self._job(now),str(uuid4()),self._block_count)
            self._queue_original(self.storage.queue_provenance,self._range_request,now)
            self._status='PROVENANCE_QUEUED';return self.status
        if self.storage.checkpoint_ref is None:
            if self._seed_request is None:
                cutoff=self._current_cutoff(now)
                if cutoff is None:self._status='WAIT_ORIGINAL_R1_FOR_EMPTY_CP';return self.status
                job=self._job(now)
                self._seed_request=CheckpointRequest(job,CheckpointPayload.build(job,durable_cutoff_pi_us=cutoff))
            self._queue_original(self.storage.queue_checkpoint,self._seed_request,now)
            self._status='EMPTY_CHECKPOINT_QUEUED';return self.status
        head=self.storage.head
        head_current=head is not None and head[0].current(head[1],self._context.sha256,now)
        needs_head=not self._head_completed or status=='NEEDS_CURRENT_HEAD' and not head_current
        if needs_head:
            if self._head_request is not None and self._head_completed:
                self._head_request=None;self._head_completed=False
            if self._head_request is None:
                self._head_request=LoadRequest(self._job(now),self.storage.checkpoint_ref,canonical(dict(kind='CHECKPOINT_PAGE',cursor=0)))
            self._queue_original(self.storage.queue_head_load,self._head_request,now)
            self._status='HEAD_LOAD_QUEUED';return self.status
        if status=='NEEDS_CURRENT_HEAD':
            if self._range_head is None:self._range_head,self._range_nonce=head,str(uuid4())
            elif self._range_head!=head:self._fault='ORIGINAL_CATALOG_HEAD_CHANGED'
            self._status='CATALOG_RESERVATION_SELECTED';return self.status
        if status=='NEEDS_EXCLUSIVE_PARKING_PROVENANCE':
            if self._parking_job is None:self._parking_job,self._parking_nonce=self._job(now),str(uuid4())
            self._status=status;return self.status
        if self._dirty_revision<=self._saved_revision:self._status='IDLE';return self.status
        began=self._dirty_since if self._last_commit_us is None else self._last_commit_us
        if began is None or now-began<self._interval:self._status='DIRTY_BATCH_WAIT';return self.status
        if self._prepare_revision==self._dirty_revision:self._status='CHECKPOINT_REVISION_NOT_READY';return self.status
        self._prepare_revision=self._dirty_revision
        if not self.storage.evidence.provenance:self._status='CHECKPOINT_REVISION_NOT_READY';return self.status
        cutoff=max(record.pi_upper for record in self.storage.evidence.provenance)
        try:request=self.storage.queue_catalog_checkpoint(self._job(now),now_us=now,durable_cutoff_pi_us=cutoff)
        except (PcStorageNotReady,CatalogNotReady):self._status='CHECKPOINT_REVISION_NOT_READY';return self.status
        self._checkpoint,self._checkpoint_revision=request,self._dirty_revision
        self._status='CHECKPOINT_QUEUED';return self.status
