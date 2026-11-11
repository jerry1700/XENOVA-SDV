"""S-main과 M-main의 실제 source 통로를 연결한다.

원 Vehicle/Probe/ToF bytes와 callback 시각만 전달한다. identity ACK는 metric/scene/
주행 승인이 아니며, COMPLETE의 LOAD 의미 검사가 없는 경우 복원을 기다린다.
큰 영상·DB copy·센서 기하 검산은 이 main-thread 경로에서 하지 않는다.
"""

from base64 import b64decode,b64encode
from collections import Counter, OrderedDict
from dataclasses import replace
from hashlib import sha256
import json
from uuid import uuid4

from .data_channel import DataLink
from .source_identity import (IdentityOwner, proposal_metadata, _sample, scene_ref_fields,
    current_scene_grant, validate_survey_summary, PROPOSAL_FIELDS, _sha)
from .storage_messages import publication_binding
from .mapper_bootstrap import BootstrapReport,SupervisorBootstrap,load_body,material_bytes
from ..storage.contracts import StorageContext, canonical, json_bytes
from ..storage.geometry_scope import PiGeometryOrigin


def expected_source(binding, policy, *, device, mapper_boot, clock_epoch):
    """S가 아는 OS boot/J epoch와 수락한 M 지도 ID를 분리해 결박한다."""
    return dict(vehicle=policy.vehicle,device=device,mapper_boot=mapper_boot,
        mapper_instance=binding['mapper_instance'],clock_epoch=clock_epoch,
        map_epoch=binding['map_epoch'],frame_epoch=binding['frame_epoch'],odom_epoch=binding['odom_epoch'],
        configuration=binding['configuration'],calibration=binding['calibration'],primitive=binding['primitive'])


def _origin(sample):
    return dict(pi_host_boot=sample.host_boot,gateway_instance=sample.gateway_instance,
        device_session=sample.device_session,pi_session=sample.pi_session)


class SourceChanged(ValueError):
    """새 실제 Pi origin은 현재 M/source의 입력으로 계속 사용할 수 없다."""


class SupervisorSource:
    """현재 M의 binary endpoint owner. S-jio의 작은 불변 참조만 소비한다."""

    def __init__(self, link, raw_binding, jio, *, mapper_instance, mapper_boot, clock_epoch,
                 revision_floor=0, operation_owner=None, clock):
        if type(link) is not DataLink or link.role!='S':
            raise ValueError('actual registered S binary link required')
        self.link,self.raw,self.jio,self.clock=link,raw_binding,jio,clock
        self.policy=jio.profile.source_policy
        self.mapper_boot,self.clock_epoch=mapper_boot,clock_epoch
        self.owner=self._make_identity_owner(raw_binding,mapper_instance,revision_floor)
        self.pending=OrderedDict();self.vehicles=OrderedDict()
        self.current_vehicle=None;self._material=None;self._material_sha=None
        self._early_identity=None;self._acks=OrderedDict();self._turn=0
        self.context=None;self.source=None;self.origin=None
        self.bootstrap=SupervisorBootstrap(link.peer,mapper_instance);self._bootstrap_complete=None
        self.counts=Counter()
        self._last_survey=None
        self._abort_notice=None
        from .mapper_operation import SupervisorOperation
        self.operation_owner=SupervisorOperation(link.peer.service_instance,clock=clock) if operation_owner is None else operation_owner
        if type(self.operation_owner) is not SupervisorOperation:raise ValueError('actual S operation owner required')
        self.operation_owner.bind_peer(link.peer,world_dispatch=jio.set_world_dispatch_enabled)

    def _make_identity_owner(self, raw_binding, mapper_instance, revision_floor):
        """기존 physical owner를 만든다. 다른 source variant는 별도 owner를 명시한다."""
        return IdentityOwner(self.link.peer,raw_binding,self.policy,
            mapper_instance=mapper_instance,revision_floor=revision_floor)

    def _flush(self):
        original=self.link.pending
        if original is not None and self.clock()>=original.expires_us:
            self._discard_expired_delivery(original)
        try:sent=self.link.flush()
        except TimeoutError as error:
            original=self.link.pending
            if original is None or self.clock()<original.expires_us:raise
            # discard는 exact pending/미송신을 다시 증명한다. unknown 송신은 원 예외다.
            try:self._discard_expired_delivery(original)
            except Exception:raise error
            return None
        if sent is not None and sent.packet.kind=='SOURCE_OPERATION':self.operation_owner.sent(sent)
        if sent is not None and sent.packet.kind=='SOURCE_STORAGE_ABORT':
            notice=self._abort_notice
            if (notice is None or sent.packet.body!=canonical(notice.fields)
                    or sent.packet.header['request_id']!=notice.request_id):
                raise RuntimeError('storage terminal send lost its original notice')
            self._abort_notice=None
        return sent

    def _discard_expired_delivery(self, original):
        self.link.discard_expired(original)
        self.counts['expired_unsent']+=1
        self._retry_current_delivery(original.kind)

    def _retry_current_delivery(self, kind):
        """설치 자료는 현재 snapshot으로 재전달한다. 관측/명령의 원 E는 갱신하지 않는다."""
        now=self.clock()
        if kind=='SOURCE_STORAGE' and self._material is not None:
            self._stage('STORAGE','SOURCE_STORAGE',{},canonical(dict(binding=self._material[0],bootstrap=self._material[1])),
                issued=now,expires=now+5_000_000)
        elif kind=='SOURCE_STORAGE_ABORT' and self._abort_notice is not None:
            self._stage_abort(now)
        elif kind=='SOURCE_PUBLICATION' and self.owner.binding is not None and self.context is not None:
            binding=self.owner.binding
            if binding['scene_admission_ref'] is not None:
                item=self.jio.current_scene
                try:
                    grant,_=current_scene_grant(None if item is None else item.raw,self.jio.current_lease,
                        self.source,self.origin,now)
                    if grant.lifecycle_state!=2 or scene_ref_fields(grant.scene)!=binding['scene_admission_ref']:return
                except (ValueError,TypeError,OverflowError):return
            self._stage_publication(binding,self.context,now)

    def _stage_publication(self,binding,context,now):
        # full snapshot은 최신 하나면 충분하다. 중간 scene 전이는 재실행하지 않는다.
        for key in tuple(self.pending):
            if type(key) is tuple and key[0]=='PUBLICATION':del self.pending[key]
        metadata=dict(source=self.source,origin=self.origin,geometry_origin=context.geometry_origin_fields,
            context_sha256=context.sha256)
        self._stage(('PUBLICATION',int(binding['source_binding_revision'])),'SOURCE_PUBLICATION',metadata,canonical(binding),
            issued=now,expires=now+5_000_000,request_id=str(uuid4()))

    def _stage(self,key,kind,metadata,body,*,issued,expires,request_id=None):
        if issued>=expires:return
        if key not in self.pending and len(self.pending)>=8:
            raise RuntimeError('bounded source relay heads are full')
        self.pending[key]=(kind,metadata,body,issued,expires,request_id)

    def _input(self,item):
        now=self.clock()
        if item.received_j_us>now:raise ValueError('future JIO delivery is not original transport evidence')
        if item.kind=='CLOCK_REPLY':
            from .mapper_reply_trace import MapperReplyTrace
            trace=item.reply_trace
            if (type(trace) is not MapperReplyTrace or trace.raw_reply_bytes!=item.raw
                    or trace.generated_j_us!=item.received_j_us or self.source!=trace.source or self.origin!=trace.origin):
                self.counts['clock_reply_rejected']+=1;return
            if now>=trace.expires_j_us:
                self.counts['clock_reply_expired']+=1;return
            if ('CLOCK_REPLY',None) not in self.pending and len(self.pending)>=8:
                self.counts['clock_reply_dropped']+=1;return
            self._stage(('CLOCK_REPLY',None),'SOURCE_CLOCK_REPLY',trace.metadata,item.raw,
                issued=trace.generated_j_us,expires=trace.expires_j_us)
            return
        if item.kind=='VEHICLE':
            sample=_sample(item.raw)
            if (sample.vehicle,sample.calibration)!=(self.policy.vehicle,self.policy.calibration):
                raise ValueError('original vehicle differs from installed source policy')
            if self.origin is not None:
                origin=_origin(sample)
                changed=[key for key in self.origin if origin[key]!=self.origin[key]]
                if sample.odom_epoch!=self.source['odom_epoch']:changed.append('odom_epoch')
                if changed:
                    self.jio.fence_source()
                    raise SourceChanged('actual Pi/Gateway/device/odom source changed: '+','.join(changed))
            if self.bootstrap.original is not None and self.owner.binding is None and self.current_vehicle is not None:
                previous=_sample(self.current_vehicle)
                if (_origin(sample),sample.odom_epoch,sample.calibration,sample.vehicle)!=(
                        _origin(previous),previous.odom_epoch,previous.calibration,previous.vehicle):
                    self.jio.fence_source();raise SourceChanged('Pi source changed during original bootstrap LOAD')
            key=sha256(item.raw).hexdigest()
            if key not in self.vehicles:
                self.vehicles[key]=(item.raw,item.received_j_us)
                while len(self.vehicles)>128:self.vehicles.popitem(last=False)
            self.current_vehicle=item.raw
        names={'VEHICLE':'SOURCE_VEHICLE','TOF':'SOURCE_TOF','SCENE':'SOURCE_SCENE','PROBE':'SOURCE_CLOCK'}
        if item.kind not in names:raise ValueError('unsupported actual JIO input')
        metadata=dict(received_j_us=str(item.received_j_us),sensor_id=item.sensor_id,
            clock_probe_sha256=None if item.lease is None else sha256(item.lease.raw_probe_bytes).hexdigest())
        if item.kind=='PROBE':
            metadata['clock_probe_sha256']=None if item.lease is None else sha256(item.raw).hexdigest()
        # local delivery lifetime does not replace Pi measurement age or the original trust expiry.
        expires=item.received_j_us+1_000_000
        self._stage((item.kind,item.sensor_id),names[item.kind],metadata,item.raw,
            issued=item.received_j_us,expires=expires)

    def _identity(self,packet,*,bind_ready,on_publication):
        if not bind_ready or self._material is None or self.current_vehicle is None:
            if self._early_identity is not None and self._early_identity.raw!=packet.raw:
                raise ValueError('one original early identity cannot be replaced')
            self._early_identity=packet;return
        proposal=packet.header['metadata']
        if set(proposal)!=PROPOSAL_FIELDS:raise ValueError('identity proposal has unsupported fields')
        vehicle=self.vehicles.get(_sha(proposal['vehicle_sample_sha256']))
        if vehicle is None:
            self.counts['identity_wait_current_input']+=1
            return
        bootstrap=self._material[1]
        if bootstrap['status']=='COMPLETE' and self.owner.binding is None and self.bootstrap.evidence is None:
            if self._early_identity is not None and self._early_identity.raw!=packet.raw:
                raise ValueError('one original COMPLETE identity cannot be replaced')
            self._early_identity=packet;self.counts['restore_wait']+=1;return
        from .health_policy import FrameExpired
        try:binding=self.owner.accept(packet,bootstrap=bootstrap,vehicle_wire=vehicle[0],vehicle_received_us=vehicle[1],
            current_vehicle_wire=self.current_vehicle,now_us=self.clock(),restore_material=self.bootstrap.evidence)
        except FrameExpired:
            self.counts['identity_wait_current_input']+=1
            return
        sample=_sample(vehicle[0]);origin=_origin(sample)
        context=StorageContext(bootstrap['database_uuid'],self.link.peer.mapper_worker,binding['mapper_instance'],
            binding['map_epoch'],binding['frame_epoch'],binding['odom_epoch'],sample.host_boot,
            binding['configuration'],binding['calibration'],binding['primitive'],canonical(binding),
            geometry_origin=PiGeometryOrigin(sample.vehicle,sample.host_boot,sample.gateway_instance,
                sample.device_session,sample.pi_session,sample.odom_epoch,sample.calibration).fields)
        if self.context is None:
            on_publication(binding,context)
            self.context=context
            self.source=expected_source(binding,self.policy,device=self.jio.profile.device,
                mapper_boot=self.mapper_boot,clock_epoch=self.clock_epoch)
            self.origin=origin
            self.jio.replace_source(self.source,**origin)
            self.jio.replace_publication(binding)
        elif self.context.sha256!=context.sha256:
            raise ValueError('same source incarnation changed its storage context')
        request=packet.header['request_id']
        ack=(dict(source=self.source,origin=origin,geometry_origin=context.geometry_origin_fields,
                  context_sha256=context.sha256),canonical(binding),
             packet.issued_us,packet.expires_us,request)
        previous=self._acks.get(request)
        if previous is not None:ack=previous  # scene 전환도 원 proposal ACK를 새 사건으로 다시 쓰지 않는다.
        if previous is None:
            if len(self._acks)>=16:self._acks.popitem(last=False)
            self._acks[request]=ack
        self._stage('ACK','SOURCE_IDENTITY_ACK',ack[0],ack[1],issued=ack[2],expires=ack[3],request_id=request)
        self.counts['identity_accepted']+=1

    def _publish_scene(self,on_publication,*,now,expires):
        if now>=expires:
            self.counts['scene_publication_expired']+=1
            return
        binding=self.owner.binding
        context=replace(self.context,source_binding=canonical(binding))
        on_publication(binding,context)
        self.context=context
        self.jio.replace_source(self.source,**self.origin)  # 같은 source이므로 token/clock/odom은 유지한다.
        self.jio.replace_publication(binding)
        self._stage_publication(binding,context,now)
        self.counts['publication_changed']+=1

    def _scene_current(self,on_publication,now):
        if self.owner.binding is None or self.owner.binding['scene_admission_ref'] is None:return
        item=self.jio.current_scene
        try:
            if item is None:raise ValueError('scene grant absent')
            grant,_=current_scene_grant(item.raw,self.jio.current_lease,self.source,self.origin,now)
            if grant.lifecycle_state!=2 or scene_ref_fields(grant.scene)!=self.owner.binding['scene_admission_ref']:
                raise ValueError('scene grant revoked or changed')
        except (ValueError,TypeError,OverflowError):
            if self.owner.clear_scene():self._publish_scene(on_publication,now=now,expires=now+5_000_000)

    def _survey(self,packet,on_publication,now):
        item=self.jio.current_scene
        if item is None:raise ValueError('survey report has no current original grant')
        fingerprint=(packet.body,canonical(packet.header['metadata']),packet.issued_us,packet.expires_us)
        if fingerprint==self._last_survey:
            validate_survey_summary(packet.body,item.raw,self.jio.current_lease,self.source,self.origin,
                issued_us=packet.issued_us,expires_us=packet.expires_us,now_us=now)
            self.counts['survey_duplicate']+=1;return
        changed,summary=self.owner.accept_survey(packet,grant_raw=item.raw,lease=self.jio.current_lease,
            source=self.source,origin=self.origin,now_us=now)
        self.jio.offer_survey_summary(packet.body,grant_sha256=sha256(item.raw).hexdigest(),expires_j_us=packet.expires_us)
        self._last_survey=fingerprint
        if changed:self._publish_scene(on_publication,now=now,expires=packet.expires_us if summary.status==2 else now+5_000_000)
        self.counts['survey_accepted']+=1

    def pump(self,material,*,bind_ready,on_publication,on_bootstrap_load=None,bootstrap_completion=None):
        """한 회차 최대 JIO7/packet4. bind_ready는 실제 현재 RAW ACK 수락 사실이다."""
        now=self.clock()
        if material is not None:
            wire=canonical(dict(binding=material[0],bootstrap=material[1]))
            if len(wire)>2048:raise ValueError('actual source storage material exceeds local body cap')
            key=sha256(wire).hexdigest()
            if key!=self._material_sha:
                if self.owner.binding is None and self.bootstrap.original is not None and wire!=self.bootstrap.original:
                    raise SourceChanged('D material changed during original bootstrap LOAD')
                self._material,self._material_sha=material,key
                self._stage('STORAGE','SOURCE_STORAGE',{},wire,issued=now,expires=now+5_000_000)
        for item in self.jio.take_inputs():self._input(item)
        self._scene_current(on_publication,now)
        self.bootstrap.observe(None if bootstrap_completion is None else bootstrap_completion(),now)
        if self._early_identity is not None and now>=self._early_identity.expires_us:
            self._early_identity=None;self.counts['identity_expired']+=1
        if self._early_identity is not None and bind_ready and self._material is not None and self.current_vehicle is not None:
            packet=self._early_identity;self._early_identity=None
            self._identity(packet,bind_ready=True,on_publication=on_publication)
        for packet in self.link.poll():
            now=self.clock()
            if packet.kind=='OPERATION_RESULT':
                # 설치/적용의 원 완료 사실을 소비한다. local 전달 E는 설정의 수명이 아니다.
                try:self.operation_owner.receive(packet,self.link.peer)
                except (ValueError,TypeError,OverflowError):self.counts['operation_rejected']+=1
                continue
            if (now<packet.issued_us or now>=packet.expires_us
                    and packet.kind!='SOURCE_BOOTSTRAP_COMPLETE'):
                self.counts['expired']+=1;continue
            if packet.kind=='SOURCE_IDENTITY':
                self._identity(packet,bind_ready=bind_ready,on_publication=on_publication)
            elif packet.kind=='WORLD_CANDIDATE':
                metadata=packet.header['metadata']
                if set(metadata)!={'publication_sha256'}:raise ValueError('world candidate metadata fields differ')
                if (not self.operation_owner.world_enabled or self.owner.binding is None
                        or metadata['publication_sha256']!=self.owner.binding_sha256):
                    self.counts['world_fenced']+=1;continue
                self.jio.offer_world(packet.body,expires_j_us=packet.expires_us,
                    publication_sha256=metadata['publication_sha256'])
            elif packet.kind=='SURVEY_SUMMARY':
                try:self._survey(packet,on_publication,now)
                except (ValueError,TypeError,OverflowError):self.counts['survey_rejected']+=1
            elif packet.kind=='SOURCE_BOOTSTRAP_LOAD':
                if self._material is None:raise ValueError('bootstrap LOAD has no original D material')
                self.bootstrap.offer_load(packet,self._material)
            elif packet.kind=='SOURCE_BOOTSTRAP_COMPLETE':
                self.bootstrap._registered(packet,packet.kind)
                if self._bootstrap_complete is not None and self._bootstrap_complete.raw!=packet.raw:
                    raise ValueError('one original bootstrap completion pending')
                self._bootstrap_complete=packet
            else:raise ValueError('unsupported M source statement')
        # 이미 도착한 원 APPLIED ACK는 먼저 수락한다. 실패는 기존 M recovery로 넘기며 E를 재발급하지 않는다.
        self.operation_owner.check_bootstrap_expiry()
        self.bootstrap.dispatch(on_bootstrap_load if bind_ready else None,now)
        if self._bootstrap_complete is not None:
            packet=self._bootstrap_complete
            if self.bootstrap.requests and len(self.bootstrap.physical)==len(self.bootstrap.requests) and self.bootstrap.pending is None:
                self.bootstrap.accept_complete(packet,self._material,now);self._bootstrap_complete=None
        self._flush()
        operation=self.operation_owner.next_message(self.link.peer,ready=bind_ready and self.link.pending is None)
        if operation is not None:
            metadata,body,request,issued,expires=operation
            packet=self.link.send('SOURCE_OPERATION',metadata,body,request_id=request,issued_us=issued,expires_us=expires)
            if packet is not None:self.operation_owner.selected(packet)
            self._flush()
        for key in tuple(self.pending):
            if key in self.pending and now>=self.pending[key][4]:
                expired=self.pending.pop(key);self.counts['expired']+=1
                self._retry_current_delivery(expired[0])
        if self.pending:
            keys=tuple(self.pending)
            publications=[key for key in keys if type(key) is tuple and key[0]=='PUBLICATION']
            key=('ABORT' if 'ABORT' in self.pending else 'ACK' if 'ACK' in self.pending
                 else min(publications,key=lambda item:item[1]) if publications
                 else 'STORAGE' if 'STORAGE' in self.pending else keys[self._turn%len(keys)])
            kind,metadata,body,issued,expires,request=self.pending[key]
            if self.link.send(kind,metadata,body,request_id=request,issued_us=issued,expires_us=expires) is not None:
                del self.pending[key];self._turn+=1
            self._flush()

    def offer_abort(self,notice):
        """원 terminal 하나를 실제 full-send까지 보존한다. 새 DB RESULT/성공은 발급하지 않는다."""
        from .resident_db import DbAbort
        if type(notice) is not DbAbort or notice.mapper.worker_instance!=self.link.peer.mapper_worker:
            raise ValueError('storage terminal belongs to another original M owner')
        if self._abort_notice is not None:
            if self._abort_notice!=notice:raise RuntimeError('one original storage terminal is already pending')
            return
        self._abort_notice=notice
        self._stage_abort(self.clock())

    def _stage_abort(self,now):
        notice=self._abort_notice;body=canonical(notice.fields)
        if len(body)>8192:raise ValueError('bounded original DB abort history exceeded')
        self._stage('ABORT','SOURCE_STORAGE_ABORT',{},body,issued=now,expires=now+1_000_000,request_id=notice.request_id)

    def close(self):
        self.operation_owner.detach_peer(self.link.peer)
        self.link.close()


class MapperSource:
    """M-main source 수신 owner. compute에는 최대64KiB 원본 snapshot만 넘긴다."""

    def __init__(self,link,raw_binding,policy,*,mapper_instance,device,clock):
        if type(link) is not DataLink or link.role!='M':raise ValueError('actual registered M binary link required')
        self.link,self.raw,self.policy,self.clock=link,raw_binding,policy,clock
        self.mapper_instance=mapper_instance
        self.device=device
        self.map_epoch,self.frame_epoch=self._identity_epochs()
        self.material=self.publication=self.source=self.origin=None
        self.storage_context=None
        self._publication_pending=None
        self.context_sha256=None;self._proposal=None;self._proposal_packet=None
        self.vehicles=OrderedDict();self.probes=OrderedDict();self.tof={};self.scene=None
        self._clock_reply_packets=OrderedDict()
        self._outputs=OrderedDict();self._output_heads={}
        self.active_probe=None;self._vehicle_head=None;self.counts=Counter()
        self._abort=None;self._abort_history=OrderedDict()
        self._bootstrap_pending=OrderedDict();self._bootstrap_report=None;self._bootstrap_origin=None;self._bootstrap_started=False
        self._pumping=False
        self._operation_controller=None;self._operation_pending=None;self._operation_result=None;self._operation_ready=None

    def _identity_epochs(self):
        return str(uuid4()),str(uuid4())

    def _binding_clock_epoch(self):
        return self.raw.fields['clock_epoch']

    def _identity_may_propose(self):
        return True

    def _send_identity_proposal(self,bootstrap,now):
        self._proposal=proposal_metadata(raw_binding=self.raw,mapper_instance=self.mapper_instance,
            bootstrap=bootstrap,vehicle_wire=self._vehicle_head,policy=self.policy,nonce=str(uuid4()),
            map_epoch=self.map_epoch,frame_epoch=self.frame_epoch)
        expires=now+5_000_000
        self._proposal_packet=self.link.send('SOURCE_IDENTITY',self._proposal,issued_us=now,expires_us=expires)
        self._flush()

    def install_operation(self, controller):
        """실제 M-main의 compute 적용 callback을 한 번 등록한다. 설치 전 World는 막는다."""
        from .mapper_operation import MapperOperationController
        if (type(controller) is not MapperOperationController or controller.peer!=self.link.peer
                or self._operation_controller is not None):raise ValueError('one actual registered M operation consumer required')
        self._operation_controller=controller
        self._refresh_operation_marker()

    def _refresh_operation_marker(self):
        """현재 M 채널의 설치 사실을 다시 전달한다. 운행 명령이나 과거 적용 ACK가 아니다."""
        from .operation_request import CONSUMER_READY_PROFILE,TRANSACTION_US
        issued=self.clock()
        body=canonical(dict(profile=CONSUMER_READY_PROFILE,service_instance=self.link.peer.service_instance))
        self._operation_result=(body,str(uuid4()),issued,min(2**64-1,issued+TRANSACTION_US))
        self._operation_ready=self._operation_result

    @property
    def operation_ready(self):
        return self._operation_controller is not None and self._operation_controller.synchronized

    @property
    def operation_world_enabled(self):
        return self.operation_ready and self._operation_controller.state=='RUN'

    def operation_step(self, *, inputs=None, sensor_state=None, origin=None):
        """같은 main의 fresh ingest 뒤에 호출한다. 원 CONFIG/요청만 처리하고 설정 E를 만들지 않는다."""
        if self._operation_pending is None or self._operation_controller is None:return
        if self._operation_result is not None:return  # 원 result가 선택될 때까지 pending1을 그대로 보존한다.
        from .operation_request import operation_result
        packet=self._operation_pending
        raw=self._operation_controller.process(packet,inputs=inputs,sensor_state=sensor_state,origin=origin)
        value=operation_result(raw)
        self._retry_operation_result(raw,value['request_id'])
        self._operation_pending=None

    def _retry_operation_result(self,raw,request):
        """원 ACK 본문은 불변이다. 새 local delivery만 만들며 consumer를 다시 호출하지 않는다."""
        from .operation_request import TRANSACTION_US
        now=self.clock()
        self._operation_result=(raw,request,now,min(2**64-1,now+TRANSACTION_US))

    def offer_bootstrap_load(self,request):
        self._bootstrap_started=True
        if self._vehicle_head is not None and self._bootstrap_origin is None:
            sample=_sample(self._vehicle_head)
            self._bootstrap_origin=(_origin(sample),sample.odom_epoch,sample.calibration,sample.vehicle)
        self._bootstrap_stage('SOURCE_BOOTSTRAP_LOAD',load_body(request),request.job.job_id,
                              request.job.issued_us,request.job.expires_us)

    def accept_bootstrap(self,report):
        """compute 소유 SourceComplete의 작은 원 보고만 받는다. restore/metric 승인이 아니다."""
        if (type(report) is not BootstrapReport or self.material is None
                or report.material_sha256!=sha256(material_bytes(self.material)).hexdigest()
                or (report.mapper_worker,report.mapper_instance)!=(self.link.peer.mapper_worker,self.mapper_instance)):
            raise ValueError('bootstrap complete belongs to another original source')
        if self._bootstrap_report is not None and self._bootstrap_report is not report:
            raise ValueError('one original complete source report required')
        self._bootstrap_report=report
        now=self.clock()
        self._bootstrap_stage('SOURCE_BOOTSTRAP_COMPLETE',report.body,report.request_id,now,now+5_000_000)

    def _bootstrap_stage(self,kind,body,request_id,issued,expires):
        key=(kind,request_id);value=(kind,body,request_id,issued,expires)
        if key in self._bootstrap_pending and self._bootstrap_pending[key]!=value:raise ValueError('bootstrap retry changed original packet')
        if key not in self._bootstrap_pending and len(self._bootstrap_pending)>=2:raise ValueError('bounded bootstrap transport heads full')
        self._bootstrap_pending[key]=value

    def _delivery(self,packet):
        meta=packet.header['metadata']
        if set(meta)!= {'received_j_us','sensor_id','clock_probe_sha256'}:
            raise ValueError('source delivery metadata fields differ')
        received=int(meta['received_j_us'])
        if str(received)!=meta['received_j_us'] or not 0<=received<=packet.issued_us:
            raise ValueError('source callback time changed in local transport')
        return dict(raw=b64encode(packet.body).decode('ascii'),received_j_us=str(received),
                    clock_probe_sha256=meta['clock_probe_sha256'])

    def _clock_reply(self,packet):
        from .mapper_reply_trace import reply_fields,MAX_REPLY_TRACES
        meta=packet.header['metadata']
        reply_fields(packet.body,meta,issued_us=packet.issued_us,expires_us=packet.expires_us)
        if self.source is not None and (meta['source']!=self.source or meta['origin']!=self.origin):
            self.counts['clock_reply_rejected']+=1;return
        key=meta['probe_sha256'];old=self._clock_reply_packets.get(key)
        if old is not None:
            if (old.body,old.header['metadata'],old.issued_us,old.expires_us,old.header['request_id'])!=(
                    packet.body,meta,packet.issued_us,packet.expires_us,packet.header['request_id']):
                raise ValueError('one original Probe Reply cannot be replaced or retimed')
            return
        now=self.clock()
        if now>=packet.expires_us:
            self.counts['clock_reply_expired']+=1;return
        if now<packet.issued_us:
            self.counts['clock_reply_rejected']+=1;return
        self._clock_reply_packets[key]=packet
        while len(self._clock_reply_packets)>MAX_REPLY_TRACES:self._clock_reply_packets.popitem(last=False)

    @property
    def clock_replies(self):
        """현재 peer/source의 원 Probe와 결합된 생성 사실이다. context v1은 변경하지 않는다."""
        self.link.pending  # mutable cache는 M-main만 읽고 compute에는 원 tuple만 넘긴다.
        from .mapper_reply_trace import MapperReplyTrace
        if self.source is None or self.origin is None:return ()
        result=[]
        for key,packet in self._clock_reply_packets.items():
            meta=packet.header['metadata'];probe=self.probes.get(key)
            if probe is None or meta['source']!=self.source or meta['origin']!=self.origin:continue
            if probe['received_j_us']!=meta['probe_received_j_us']:
                self.counts['clock_reply_rejected']+=1;continue
            try:
                result.append(MapperReplyTrace.from_original(b64decode(probe['raw'],validate=True),packet.body,
                    source=self.source,origin=self.origin,probe_received_j_us=int(meta['probe_received_j_us']),
                    expires_j_us=packet.expires_us))
            except (ValueError,TypeError,OverflowError):
                self.counts['clock_reply_rejected']+=1
        return tuple(result)

    def _offer_output(self,kind,raw,metadata,issued_us,expires_us):
        now=self.clock()
        if (type(issued_us) is not int or type(expires_us) is not int
                or not issued_us<=now or not issued_us<expires_us<=issued_us+5_000_000):
            raise ValueError('original mapper output event/expiry required')
        if now>=expires_us:
            self.counts['output_expired']+=1;return False
        original=(raw,canonical(metadata),issued_us,expires_us)
        previous=self._output_heads.get(kind)
        if previous is not None and previous[0]==raw and previous[1]==original[1]:
            if previous!=original:raise ValueError('retry renewed original mapper output time')
            return False
        self._output_heads[kind]=original
        self._outputs[kind]=(raw,metadata,issued_us,expires_us,str(uuid4()))
        return True

    def _flush(self):
        """기한/문맥을 잃은 미송신 출력만 폐기한다. input/identity/DB 오류는 그대로 전파한다."""
        original=self.link.pending
        if original is not None and self.clock()>=original.expires_us and original.kind!='OPERATION_RESULT':
            self._discard_expired_delivery(original)
        original=self.link.pending
        if original is not None and original.kind in ('WORLD_CANDIDATE','SURVEY_SUMMARY'):
            now=self.clock()
            expired=now>=original.expires_us
            current=None if self.publication is None else sha256(canonical(self.publication)).hexdigest()
            if (expired or original.header['metadata'].get('publication_sha256')!=current
                    or original.kind=='WORLD_CANDIDATE' and not self.operation_world_enabled):
                self.link.discard_pending(original)
                self.counts['output_expired' if expired else 'output_fenced']+=1
        original=self.link.pending
        self._discard_expired_operation(original)
        try:return self.link.flush()
        except TimeoutError as error:
            original=self.link.pending
            if original is None or self.clock()<original.expires_us:raise
            try:
                if self._discard_expired_operation(original):return None
                self._discard_expired_delivery(original)
            except Exception:raise error
            return None

    def _discard_expired_delivery(self, original):
        self.link.discard_expired(original)
        key='output_expired' if original.kind in ('WORLD_CANDIDATE','SURVEY_SUMMARY') else 'expired_unsent'
        self.counts[key]+=1
        if original.kind=='SOURCE_BOOTSTRAP_COMPLETE':
            now=self.clock()
            self._bootstrap_stage(original.kind,original.body,original.header['request_id'],now,now+5_000_000)

    def _discard_expired_operation(self, original):
        if original is None or original.kind!='OPERATION_RESULT' or self.clock()<original.expires_us:return False
        identity=(original.body,original.header['request_id'],original.issued_us,original.expires_us)
        if identity==self._operation_ready:
            self.link.discard_operation(original)
            self._refresh_operation_marker()
            self.counts['consumer_marker_retried']+=1
            return True
        self.link.discard_operation(original)
        self._retry_operation_result(original.body,original.header['request_id'])
        self.counts['operation_result_expired']+=1
        return True

    def discard_outputs(self):
        """미송신 World/summary만 철회한다. 원 identity/bootstrap·기한·송신 순번은 보존한다.

        이미 실제 송신한 packet은 회수하지 않는다. partial/unknown pending은 기존
        DataLink가 실패로 남기며 이를 정상 폐기나 native 정리로 바꾸지 않는다.
        retry의 원 body/issued/E 기록도 유지해 폐기가 기한 재발급 입구가 되지 않게 한다.
        """
        return self._discard_outputs(('WORLD_CANDIDATE','SURVEY_SUMMARY'))

    def discard_world(self):
        """운영 gate는 미송신 World만 철회한다. Scene 음성/clock 보고는 그대로 둔다."""
        return self._discard_outputs(('WORLD_CANDIDATE',))

    def _discard_outputs(self,kinds):
        original=self.link.pending
        discarded=0
        if original is not None and original.kind in kinds:
            if not self.link.discard_pending(original):
                raise RuntimeError('original pending output changed during same-owner discard')
            discarded+=1
        for kind in kinds:
            if self._outputs.pop(kind,None) is not None:discarded+=1
        self.counts['output_fenced']+=discarded
        return discarded

    def offer_world(self,raw,*,publication_sha256,issued_us,expires_us):
        """현재 원 publication의 최신 world1개를 stage한다. 이미 선택된 packet은 바꾸지 않는다."""
        from xenova_drive_world import decode,schema
        if not self.operation_world_enabled:
            self.counts['world_operation_blocked']+=1;return False
        if self.publication is None or publication_sha256!=sha256(canonical(self.publication)).hexdigest():return False
        value=decode(raw,schema.WorldSnapshot)
        if any(getattr(value,key)!=self.source[key] for key in self.source if key!='device'):
            raise ValueError('world output changed original source')
        return self._offer_output('WORLD_CANDIDATE',raw,dict(publication_sha256=publication_sha256),issued_us,expires_us)

    def offer_survey_summary(self,raw,*,publication_sha256,grant_sha256,issued_us,expires_us):
        """compute가 고정한 원 보고/완료/절대기한만 최신1에 둔다. main은 새 관측을 만들지 않는다."""
        from xenova_drive_world import decode,schema
        if self.publication is None or publication_sha256!=sha256(canonical(self.publication)).hexdigest():return False
        if self.scene is None or grant_sha256!=sha256(b64decode(self.scene['raw'],validate=True)).hexdigest():return False
        summary=decode(raw,schema.SurveySummary)
        if summary.completed_peer_us!=issued_us:raise ValueError('survey event changed actual completed time')
        return self._offer_output('SURVEY_SUMMARY',raw,
            dict(publication_sha256=publication_sha256,grant_sha256=grant_sha256),issued_us,expires_us)

    def _publication(self,packet):
        """scene-only 새 publication을 검산한다. 초기 제안이나 raw/source 신원을 다시 발급하지 않는다."""
        from xenova_drive_world import decode,schema
        if self.publication is None or self.storage_context is None:raise ValueError('no original identity publication')
        binding=publication_binding(json_bytes(packet.body,2048));meta=packet.header['metadata']
        if (meta['source']!=self.source or meta['origin']!=self.origin
                or meta['geometry_origin']!=self.storage_context.geometry_origin_fields):
            raise ValueError('scene publication changed original source/geometry origin')
        stable=set(self.publication)-{'scene_admission_ref','source_binding_revision'}
        if any(binding[key]!=self.publication[key] for key in stable):
            raise ValueError('scene publication changed original raw/map/configuration')
        context=replace(self.storage_context,source_binding=canonical(binding))
        if context.sha256!=meta['context_sha256']:raise ValueError('scene publication changed actual context SHA')
        if binding==self.publication:
            self.counts['publication_duplicate']+=1;return True
        if int(binding['source_binding_revision'])<=int(self.publication['source_binding_revision']):
            raise ValueError('scene publication revision did not advance')
        scene=binding['scene_admission_ref']
        if scene is not None:
            if self.scene is None:return False
            grant=decode(b64decode(self.scene['raw'],validate=True),schema.SceneGrant)
            if grant.lifecycle_state!=2 or scene_ref_fields(scene)!=scene_ref_fields(grant.scene):
                return False
            if (grant.pi_host_boot!=self.origin['pi_host_boot'] or grant.gateway_instance!=self.origin['gateway_instance']
                    or grant.mapper_device!=self.device or any(getattr(grant,key)!=self.source[key] for key in
                        ('vehicle','map_epoch','frame_epoch','odom_epoch','configuration','calibration','primitive'))):
                raise ValueError('positive scene publication changed grant source')
            probe_delivery=self.probes.get(self.active_probe)
            if probe_delivery is None:return False
            probe=decode(b64decode(probe_delivery['raw'],validate=True),schema.MapperClockProbe)
            if not probe.HasField('mapper_trust'):return False
            trust=probe.mapper_trust;now=self.clock()
            if (probe.pi_host_boot!=self.origin['pi_host_boot'] or probe.admission_revision!=grant.source_admission_revision
                    or any(getattr(trust,key)!=self.source[key] for key in self.source if key!='device')):
                raise ValueError('positive scene publication changed original clock/grant source')
            if (grant.issued_pi_us>now+trust.lower_offset_us
                    or not now<min(grant.valid_until_pi_us,trust.expires_pi_us)-trust.upper_offset_us):return False
        self.publication,self.storage_context,self.context_sha256=binding,context,context.sha256
        self._outputs.clear()  # 옛 publication에 결박한 미선택 출력만 철회한다. 원 frame/관측은 유지한다.
        self.counts['publication_changed']+=1
        return True

    def _receive_publication(self,packet):
        """최신 full snapshot 하나만 보류한다. 원 Scene/clock이 도착해도 E를 갱신하지 않는다."""
        pending=self._publication_pending
        try:
            if pending is not None and pending is not packet:
                before=publication_binding(json_bytes(pending.body,2048))
                after=publication_binding(json_bytes(packet.body,2048))
                old,new=int(before['source_binding_revision']),int(after['source_binding_revision'])
                if new<old or new==old and pending.body!=packet.body:
                    raise ValueError('pending full publication revision regressed or conflicted')
            if self._publication(packet):self._publication_pending=None
            else:
                if pending is not packet:self.counts['publication_wait_current_input']+=1
                self._publication_pending=packet
        except (ValueError,TypeError,OverflowError):
            if pending is packet:self._publication_pending=None
            self.counts['publication_rejected']+=1

    def pump(self,*,before_outputs=None):
        """같은 main owner에서 원 입력을 반영하고 출력 전 metadata/fence를 확인한다.

        callback은 무인자이며 초기 flush 전과 이번 poll 전체 반영 후에 호출한다.
        ingest/fence만 수행하고 pump에 재진입하지 않는다. 새 입력 수락 전 이미 시작한
        송신의 recall은 아니다. None은 기존 순서이며 callback 예외는 그대로 전파한다.
        """
        if before_outputs is not None and not callable(before_outputs):
            raise TypeError('before_outputs must be an optional zero-argument callable')
        self.link.pending  # callback도 실제 endpoint의 원 owner에서만 실행한다.
        if self.link.closed:raise EOFError('binary data endpoint is closed')
        if self._pumping:raise RuntimeError('mapper source pump cannot be reentered')
        self._pumping=True
        try:return self._pump(before_outputs)
        finally:self._pumping=False

    def _pump(self,before_outputs):
        now=self.clock()
        if before_outputs is not None:
            before_outputs();now=self.clock()
        self._flush()
        for packet in self.link.poll():
            now=self.clock()
            if (now<packet.issued_us or now>=packet.expires_us
                    and packet.kind not in ('SOURCE_STORAGE','SOURCE_PUBLICATION','SOURCE_STORAGE_ABORT')):
                self.counts['expired']+=1;continue
            if packet.kind=='SOURCE_STORAGE':
                value=json_bytes(packet.body,2048)
                if type(value) is not dict or set(value)!= {'binding','bootstrap'}:raise ValueError('exact D material required')
                self.material=(value['binding'],value['bootstrap'])
            elif packet.kind=='SOURCE_STORAGE_ABORT':
                from .resident_db import DbAbort
                notice=DbAbort.from_fields(json_bytes(packet.body,8192))
                # binary source와 STORAGE control의 channel epoch는 별개다. 원 Endpoint는
                # 변형 없이 넘기고 실제 control Endpoint 대조는 storage client가 수행한다.
                if (notice.mapper.worker_instance!=self.link.peer.mapper_worker
                        or packet.header['request_id']!=notice.request_id or notice.observed_us>packet.issued_us):
                    raise ValueError('storage terminal changed original M/request')
                previous=self._abort_history.get(notice.request_id)
                if previous is not None:
                    if previous!=notice:raise ValueError('storage terminal retry changed its original notice')
                    self.counts['storage_terminal_duplicate']+=1
                    continue
                if self._abort is not None:raise RuntimeError('bounded M storage terminal slot is full')
                self._abort=notice
                self._abort_history[notice.request_id]=notice
                while len(self._abort_history)>16:self._abort_history.popitem(last=False)
            elif packet.kind=='SOURCE_OPERATION':
                if self._operation_pending is not None:
                    self.counts['operation_backpressure_rejected']+=1  # 적용0/원 ACK0이며 S는 UNKNOWN을 보존한다.
                else:self._operation_pending=packet
            elif packet.kind=='SOURCE_PUBLICATION':
                self._receive_publication(packet)
            elif packet.kind=='SOURCE_IDENTITY_ACK':
                if self._proposal_packet is None or packet.header['request_id']!=self._proposal_packet.header['request_id']:
                    self.counts['identity_ack_unmatched']+=1
                    continue
                if (packet.issued_us,packet.expires_us)!=(self._proposal_packet.issued_us,self._proposal_packet.expires_us):
                    raise ValueError('identity ACK renewed its original proposal')
                binding=publication_binding(json_bytes(packet.body,2048));meta=packet.header['metadata']
                if set(meta)!= {'source','origin','geometry_origin','context_sha256'}:raise ValueError('exact source ACK metadata required')
                expected=self._proposal
                if any(binding[key]!=expected[key] for key in ('map_epoch','frame_epoch','odom_epoch','mapper_instance','raw_binding_sha256','configuration','calibration','primitive')):
                    raise ValueError('identity ACK differs from original M geometry proposal')
                if self.publication is not None and self.publication!=binding:
                    if (int(binding['source_binding_revision'])<int(self.publication['source_binding_revision'])
                            and all(binding[key]==self.publication[key] for key in binding
                                if key not in ('scene_admission_ref','source_binding_revision'))):
                        self.counts['identity_duplicate']+=1;continue
                    raise ValueError('identity ACK silently changed source')
                source=meta['source'];origin=meta['origin']
                expected_source_fields=set(('vehicle','device','mapper_boot','mapper_instance','clock_epoch','map_epoch',
                    'frame_epoch','odom_epoch','configuration','calibration','primitive'))
                if type(source) is not dict or set(source)!=expected_source_fields:
                    raise ValueError('identity ACK has incomplete clock source')
                if (source['vehicle']!=self.policy.vehicle or source['device']!=self.device
                        or source['clock_epoch']!=self._binding_clock_epoch()
                        or any(source[key]!=binding[key] for key in ('mapper_instance','map_epoch','frame_epoch','odom_epoch','configuration','calibration','primitive'))):
                    raise ValueError('identity ACK clock source differs from actual bound raw/publication')
                if origin!={key:expected[key] for key in ('pi_host_boot','gateway_instance','device_session','pi_session')}:
                    raise ValueError('identity ACK changed original Pi origin')
                geometry=PiGeometryOrigin.from_fields(meta['geometry_origin'])
                if (geometry.vehicle,geometry.pi_host_boot,geometry.gateway_instance,geometry.device_session,
                        geometry.pi_session,geometry.odom_epoch,geometry.calibration)!=(
                        expected['vehicle'],expected['pi_host_boot'],expected['gateway_instance'],expected['device_session'],
                        expected['pi_session'],expected['odom_epoch'],expected['calibration']):
                    raise ValueError('identity ACK changed original geometry origin')
                if self.material is None:raise ValueError('identity ACK has no admitted original D material')
                context=StorageContext(self.material[1]['database_uuid'],self.link.peer.mapper_worker,binding['mapper_instance'],
                    binding['map_epoch'],binding['frame_epoch'],binding['odom_epoch'],origin['pi_host_boot'],
                    binding['configuration'],binding['calibration'],binding['primitive'],canonical(binding),geometry_origin=geometry.fields)
                if context.sha256!=meta['context_sha256']:raise ValueError('identity ACK context SHA differs from actual original material')
                if self.source is not None and (self.source!=meta['source'] or self.origin!=meta['origin']):
                    self._clock_reply_packets.clear()
                self.publication=binding;self.source=meta['source'];self.origin=meta['origin'];self.context_sha256=meta['context_sha256']
                self.storage_context=context
            elif packet.kind=='SOURCE_CLOCK_REPLY':
                try:self._clock_reply(packet)
                except (ValueError,TypeError,OverflowError):self.counts['clock_reply_rejected']+=1
            elif packet.kind in ('SOURCE_VEHICLE','SOURCE_CLOCK','SOURCE_TOF','SOURCE_SCENE'):
                delivery=self._delivery(packet)
                if packet.kind=='SOURCE_VEHICLE':
                    sample=_sample(packet.body)
                    origin=(_origin(sample),sample.odom_epoch,sample.calibration,sample.vehicle)
                    if self._bootstrap_origin is not None and origin!=self._bootstrap_origin:
                        raise SourceChanged('Pi source changed during original bootstrap LOAD')
                    if self._bootstrap_started and self._bootstrap_origin is None:self._bootstrap_origin=origin
                    key=sha256(packet.body).hexdigest();self._vehicle_head=packet.body
                    self.vehicles[key]=delivery
                    while len(self.vehicles)>24:self.vehicles.popitem(last=False)
                elif packet.kind=='SOURCE_CLOCK':
                    from xenova_drive_world import decode,schema
                    decode(packet.body,schema.MapperClockProbe)
                    key=sha256(packet.body).hexdigest();self.probes[key]=delivery
                    while len(self.probes)>8:self.probes.popitem(last=False)
                    self.active_probe=packet.header['metadata']['clock_probe_sha256']
                elif packet.kind=='SOURCE_TOF':
                    sensor=packet.header['metadata']['sensor_id']
                    if type(sensor) is not int or sensor not in (0,1):raise ValueError('unsupported source ToF index')
                    self.tof[sensor]=delivery
                else:self.scene=delivery
            else:raise ValueError('unsupported S source statement')
        if self._publication_pending is not None:self._receive_publication(self._publication_pending)
        if before_outputs is not None:
            before_outputs();now=self.clock()
        else:
            # 설치된 실제 controller의 CONFIG/RUN도 처리한다. 새 STANDBY에 필요한 센서는 발명하지 않는다.
            self.operation_step();now=self.clock()
        if self._operation_result is not None:
            raw,request,issued,expires=self._operation_result
            if now>=expires:
                if self._operation_result is self._operation_ready:
                    self._refresh_operation_marker()
                else:self._retry_operation_result(raw,request)
                self.counts['operation_result_expired']+=1
            elif self.link.send('OPERATION_RESULT',{},raw,request_id=request,issued_us=issued,expires_us=expires) is not None:
                self._operation_result=None
            self._flush()
        if self._bootstrap_pending:
            key=next(iter(self._bootstrap_pending));kind,body,request,issued,expires=self._bootstrap_pending[key]
            if not issued<=now<expires:
                if kind=='SOURCE_BOOTSTRAP_COMPLETE':
                    self._bootstrap_pending[key]=(kind,body,request,now,now+5_000_000)
                else:
                    del self._bootstrap_pending[key]
                    self.counts['bootstrap_request_expired']+=1
                return
            if self.link.send(kind,{},body,request_id=request,issued_us=issued,expires_us=expires) is not None:
                del self._bootstrap_pending[key]
            self._flush();return
        if (self.publication is None and self._proposal_packet is None and self.material is not None
                and self._vehicle_head is not None and self._identity_may_propose()):
            bootstrap=self.material[1]
            # Restore requires actual compute LOAD validation; initial copy alone does not choose identity.
            if bootstrap['status']=='COMPLETE' and self._bootstrap_report is None:self.counts['restore_wait']+=1;return
            self._send_identity_proposal(bootstrap,now)
        if self._proposal_packet is not None and self.publication is None and now>=self._proposal_packet.expires_us:
            self._proposal_packet=None
            if hasattr(self,'_pc_identity_draft'):self._pc_identity_draft=None
            self.counts['identity_retried']+=1
        for kind in tuple(self._outputs):
            raw,metadata,issued,expires,request=self._outputs[kind]
            if kind=='WORLD_CANDIDATE' and not self.operation_world_enabled:
                del self._outputs[kind];self.counts['world_operation_blocked']+=1;continue
            if now>=expires:
                del self._outputs[kind];self.counts['output_expired']+=1;continue
            if self.link.send(kind,metadata,raw,request_id=request,issued_us=issued,expires_us=expires) is not None:
                del self._outputs[kind]
            self._flush()
            break

    def context_bytes(self):
        """원 시각의 immutable context다. 공개 dict 직접 갱신도 반영하므로 identity 캐시는 쓰지 않는다."""
        value=dict(version=1,publication=self.publication,source=self.source,origin=self.origin,
            context_sha256=self.context_sha256,active_probe_sha256=self.active_probe,
            storage_context=None if self.storage_context is None else self.storage_context.fields,
            vehicles=list(self.vehicles.values()),probes=dict(self.probes),
            tof={str(key):item for key,item in self.tof.items()},scene=self.scene)
        result=canonical(value)
        if len(result)>65536:raise ValueError('bounded original source snapshot exceeds64KiB')
        return result

    def close(self):
        self.link.pending
        self._clock_reply_packets.clear()
        self.link.close()

    def take_aborts(self):
        result=() if self._abort is None else (self._abort,)
        self._abort=None
        return result
