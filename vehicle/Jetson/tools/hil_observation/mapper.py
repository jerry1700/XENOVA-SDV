"""등록된 PC M main의 slot/source/storage와 단일 compute owner를 연결한다.

S가 만든 실제 연결과 boot만 받는다. EARLY pose는 NN 전에 한 번 고정하며 현재
Vehicle/R1은 별도로 갱신한다. public physical 입력으로 cast하거나 SDK/GT/READY를
만들지 않는다. Scene/COMPLETE 복원은 지원하지 않으며 A의 모델 수명은 건드리지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256
import socket
import time
from uuid import uuid4

from xenova_vehicle.oak.sim_nn_manifest import SimNnManifest
from xenova_vehicle.runtime.data_channel import DataLink, DataPeer
from xenova_vehicle.runtime.lifecycle_policy import AbsoluteDeadline
from xenova_vehicle.runtime.local_envelope import decode_structure
from xenova_vehicle.runtime.mapper_bootstrap import material_bytes
from xenova_vehicle.runtime.mapper_mapping import MappingContextPort
from xenova_vehicle.runtime.mapper_operation import MapperOperationController
from xenova_vehicle.runtime.mapper_profile import MapperProfile
from xenova_vehicle.runtime.mapper_publication import MapperPublicationPort
from xenova_vehicle.runtime.mapper_sensor_state import MapperSensorState, SensorSnapshotUnavailable
from xenova_vehicle.runtime.mapper_storage import MapperStorageMailbox
from xenova_vehicle.runtime.mapper_storage_client import MapperStorageClient
from xenova_vehicle.runtime.pc_binding_messages import PcBindContext, PcBindMessage
from xenova_vehicle.runtime.pc_channel import PcActorLink, decode_forward
from xenova_vehicle.runtime.pc_compute import PcMapperCompute, PcMapperInput
from xenova_vehicle.runtime.pc_current_sensors import PcCurrentSensors
from xenova_vehicle.runtime.pc_frame_admission import PcFrameAdmission
from xenova_vehicle.runtime.pc_job_messages import snapshot_ack_metadata
from xenova_vehicle.runtime.pc_mapper_control import PcMapperControl
from xenova_vehicle.runtime.pc_slot_messages import (PcWriterSlotContext, PcReaderSlotContext,
    PcSlotMessage, PcReleaseMessage, parse_pc_slot_descriptor, pc_acquire_payload, expected_pc_slot)
from xenova_vehicle.runtime.pc_source_runtime import PcMapperSource
from xenova_vehicle.runtime.pc_workers import PcWorkerActor
from xenova_vehicle.runtime.resident_transport import j_us
from xenova_vehicle.runtime.resident_workers import _StopWork, apply_compute_operation, relay_publications
from xenova_vehicle.runtime.slot_messages import SlotTimeLimits
from xenova_vehicle.runtime.slot_cleanup import CleanupTimeLimits
from xenova_vehicle.runtime.source_identity import SourcePolicy
from xenova_vehicle.runtime.source_runtime import SourceChanged
from xenova_vehicle.simulation.motion_profile import DevelopmentMotionProfile
from xenova_vehicle.storage.contracts import canonical, uuid
from xenova_vehicle.transport.sensor_samples import SensorContext

from . import clock as capture_clock


@dataclass(frozen=True)
class PcMapperLoop:
    """실제 spawn 설정이다. endpoint/handle 소유권은 호출 뒤 이 M에 있다."""
    metadata_endpoint: object
    data_endpoint: object
    data_peer: DataPeer
    copy_handles: tuple
    motion_profile: DevelopmentMotionProfile
    mapper_profile_bytes: bytes
    source_policy: SourcePolicy
    mapper_instance: str
    device: str
    epochs: tuple
    capture_clock_policy: capture_clock.ClockPolicy
    slot_limits: SlotTimeLimits
    cleanup_limits: CleanupTimeLimits

    def __post_init__(self):
        if (type(self.metadata_endpoint) is not socket.socket or type(self.data_endpoint) is not socket.socket
                or type(self.data_peer) is not DataPeer or type(self.copy_handles) is not tuple or len(self.copy_handles)!=2
                or type(self.motion_profile) is not DevelopmentMotionProfile or type(self.mapper_profile_bytes) is not bytes
                or not 0<len(self.mapper_profile_bytes)<=65536 or type(self.source_policy) is not SourcePolicy
                or type(self.capture_clock_policy) is not capture_clock.ClockPolicy
                or type(self.slot_limits) is not SlotTimeLimits or type(self.cleanup_limits) is not CleanupTimeLimits
                or type(self.epochs) is not tuple or len(self.epochs)!=2
                or type(self.device) is not str or not 1<=len(self.device)<=32 or not self.device.isascii()):
            raise ValueError('actual registered PC M endpoints/profiles/handles and original policies required')
        uuid(self.mapper_instance)
        for value in self.epochs:uuid(value)
        self.source_policy.check_profile(self.motion_profile.bound_calibration)

    def __call__(self,actor):
        if type(actor) is not PcWorkerActor or actor.boot.peer.role!='M':
            raise ValueError('actual bound PC M actor required')
        _MapperMain(actor,self).run()


class _MapperMain:
    """main 전용 상태다. pending OFFER1/meta1, EARLY1, metadata 송신1만 둔다.

    compute의 원 active/pending 입력과 별도로 과거 frame 목록을 쌓지 않는다. EARLY는
    NN submit 때 버리고 parser-completion 송신은 원 metadata만 최신1로 보존한다.
    """

    def __init__(self,actor,settings):
        self.actor,self.settings=actor,settings
        self.link=self.source=self.client=self.compute=self.control=None
        self.mailbox=self.context_port=self.publication_port=self.admission=None
        self.sensor_owner=None;self.sensor_fence=0;self.fenced=False
        self.material=self.context=None;self.source_context=b''
        self.offer=self.offer_typed=self.meta=self.acquire=self.acquire_draft=self.early=self.outgoing=None
        self.outgoing_packet=None

    def _setup(self):
        import cv2
        cv2.setNumThreads(1)
        actor=self.actor;s=self.settings;binding=actor.binding;boot=actor.boot
        if (binding is None or actor.ack_sent is None or actor.bind is None
                or self.settings.data_peer.mapper_worker!=boot.peer.worker_instance
                or self.settings.data_peer.service_instance!=boot.peer.service_instance):
            raise ValueError('actual PC M BIND/ACK and registered data peer required')
        binding.check_profile(s.motion_profile)
        profile=MapperProfile.from_bytes(s.mapper_profile_bytes)
        profile.check(s.motion_profile.bound_calibration,vehicle_configuration=s.source_policy.configuration,
            vehicle_calibration=s.source_policy.calibration)
        manifest=SimNnManifest.from_bytes(boot.nn_manifest_bytes)
        writer_context=PcBindContext(boot.acquisition_peer,boot.raw_scope.writer,binding,boot.raw_scope,boot.arena_spec)
        writer=PcBindMessage(decode_structure(boot.writer_bind_raw),writer_context)
        self.writer=PcWriterSlotContext(writer,boot.raw_scope)
        self.reader_context=PcReaderSlotContext(self.writer,actor.bind)
        self.link=PcActorLink(s.metadata_endpoint,boot.peer,'M',binding)
        self.admission=PcFrameAdmission(self.reader_context,self.link,clock_api=capture_clock,
            clock_policy=s.capture_clock_policy)
        self.mailbox=MapperStorageMailbox(binding.sha256)
        self.client=MapperStorageClient(actor,self.mailbox,s.copy_handles,clock_us=j_us)
        actor.sent_observer=self.client.note_sent
        self.context_port=MappingContextPort();self.publication_port=MapperPublicationPort(self.context_port)
        self.source=PcMapperSource(DataLink(s.data_endpoint,s.data_peer,'M'),binding,s.source_policy,
            mapper_instance=s.mapper_instance,device=s.device,epochs=s.epochs,
            metadata_link=self.link,manifest=manifest,clock=j_us)
        mailbox,context_port,publication_port=self.mailbox,self.context_port,self.publication_port
        motion,owner_token=s.motion_profile,self.admission.owner_token
        processor_ref=[None]
        def factory(generation):
            from xenova_vehicle.runtime.pc_mapper_processor import PcMapperProcessor
            if generation!=binding.sha256:raise ValueError('PC compute factory changed original descriptor')
            processor=PcMapperProcessor(motion,profile,binding,manifest,compute=compute,
                context_port=context_port,mailbox=mailbox,publication_port=publication_port,
                pc_owner_token=owner_token,clock=j_us)
            processor_ref[0]=processor
            return processor
        def maintenance():
            mailbox.compute_encode_pending(current_context_sha256=mailbox.context_sha256,
                now_us=j_us(),clock_us=j_us)
            if processor_ref[0] is not None:processor_ref[0].maintenance()
        compute=PcMapperCompute(factory,source_generation=binding.sha256,binding=binding,
            pc_owner_token=self.admission.owner_token,manifest=manifest,clock=j_us,
            maintenance=maintenance,adoption_lock=self.context_port.adoption_lock)
        self.compute=compute;compute.start()
        deadline=AbsoluteDeadline.after(actor.ack.issued_us,15_000_000,now_us=j_us(),cap_us=boot.startup_deadline_us)
        self.control=PcMapperControl(actor,self.compute,deadline,clock=j_us,
            nn_stall_us=int(boot.profile.oak.nn_stall_timeout_s*1_000_000))
        def apply(action,expires_us,gate_expires_us):
            return apply_compute_operation(self.compute,self.source,action=action,expires_us=expires_us,
                gate_expires_us=gate_expires_us,publication_port=self.publication_port,
                mapping_port=self.context_port,clock=j_us)
        self.source.install_operation(MapperOperationController(s.data_peer,apply=apply,clock=j_us))
        actor.state='WARMUP';actor.idle_hook=self._pump_inputs

    def _fence(self,reason):
        if self.fenced:return
        self.fenced=True;self.actor.state='DEGRADED'
        if self.admission is not None:self.admission.fence(reason)
        if self.compute is not None:self.compute.replace_source(self.actor.binding.sha256)
        if self.context_port is not None:self.context_port.publish(None)
        if self.publication_port is not None:self.publication_port.discard_pending()
        if self.source is not None:self.source.discard_outputs()
        self.early=None
        if self.link is None or self.link.pending is None:self.outgoing=self.outgoing_packet=None

    def _apply_sensor_fence(self):
        if self.sensor_owner is None:return
        revision=self.sensor_owner.fence_revision
        if revision!=self.sensor_fence:
            self.compute.fence_admission(revision)
            self.publication_port.fence_admission(revision)
            self.source.discard_outputs();self.sensor_fence=revision

    def _publish_inputs(self):
        if self.fenced or self.actor.stop is not None:return
        source=self.source;s=self.settings
        # 원 SourceACK/material의 ref 게시가 출력 flush보다 먼저다.
        if source.material is not None and source.material is not self.material:
            self.context_port.publish(source.storage_context,material=material_bytes(source.material))
            self.material=source.material
        elif source.storage_context is not self.context:self.context_port.publish(source.storage_context)
        self.context=source.storage_context
        inputs=None
        if source.publication is not None and self.sensor_owner is None:
            context=SensorContext.for_development(s.motion_profile,source.source,**source.origin)
            self.sensor_owner=MapperSensorState.for_pc_source(s.motion_profile,self.actor.binding,
                s.motion_profile.clock_policy(device=s.device,primitive=s.source_policy.primitive),
                sensor_context=context,mapper_worker=self.actor.boot.peer.worker_instance,
                mapper_instance=s.mapper_instance,pc_owner_token=self.admission.owner_token)
        if self.sensor_owner is not None:
            self.source_context=source.context_bytes()
            inputs=self.sensor_owner.ingest(self.source_context,now_us=j_us())
            self._apply_sensor_fence()
        source.operation_step(inputs=inputs,sensor_state=self.sensor_owner,origin=source.origin)
        self._refresh_current()

    def _pump_inputs(self):
        if self.source is None or self.fenced or self.actor.stop is not None:return
        try:self.source.pump(before_outputs=self._publish_inputs)
        except (SourceChanged,EOFError):
            self._fence('PC_SOURCE_CONTEXT_UNAVAILABLE')
            raise

    def _refresh_current(self):
        if self.compute is None or self.fenced:return
        for item in self.compute.current_inputs():
            try:
                now=j_us();view=self.admission.current_view(item.frame,now_us=now);current=None
                if item.sensor_snapshot is not None:
                    if self.sensor_owner is None:raise ValueError('actual current sensor owner unavailable')
                    current=PcCurrentSensors.from_owner(self.sensor_owner,item.sensor_snapshot,now_us=now,
                        source_context_bytes=self.source_context,fence_revision=self.sensor_owner.fence_revision)
                    self._apply_sensor_fence()
                self.compute.update_current(item,current_pc_view=view,source_context_bytes=self.source_context,
                    fence_revision=0 if self.sensor_owner is None else self.sensor_owner.fence_revision,
                    now_us=j_us(),current_sensors=current)
            except ValueError:
                self._apply_sensor_fence()
                self.compute.invalidate_current(item,reason='PC_CURRENT_INPUT_UNAVAILABLE')

    def _metadata(self):
        for received in self.link.poll():
            self._expire_heads()
            packet=received.packet
            if packet.kind=='PC_SOURCE_FENCE':
                self.link.require_received(received);self._fence('PC_SOURCE_FENCED');continue
            if packet.kind!='PC_FORWARD':raise ValueError('unsupported S PC M metadata')
            forward=decode_forward(packet,self.actor.boot.acquisition_peer,self.actor.binding)
            original=forward.original_packet
            if original.kind=='PC_SOURCE_FENCE':self._fence('PC_SOURCE_FENCED');continue
            if self.fenced:continue
            if j_us()+1>=original.expires_us:continue
            if original.kind not in ('PC_EARLY_META','PC_NN_RESULT_META'):
                raise ValueError('unsupported original A statement at PC M')
            if self.meta is not None:raise RuntimeError('one original PC metadata is already pending')
            descriptor=parse_pc_slot_descriptor(original.body,self.actor.binding)
            meta=original.metadata
            if (set(meta)!={'profile','purpose','descriptor_sha256'} or meta['profile']!='pc-slot-meta-1'
                    or meta['purpose'] not in ('PARSER_WARMUP','METRIC_MAP')
                    or meta['descriptor_sha256']!=descriptor.sha256
                    or original.request_id!=descriptor.job_id or original.expires_us!=descriptor.expires_j_us
                    or original.kind!=('PC_EARLY_META' if descriptor.phase=='EARLY' else 'PC_NN_RESULT_META')):
                raise ValueError('original S-forward PC metadata differs from descriptor/job/E')
            self.meta=(received,descriptor,meta['purpose'])

    def _offer(self,packet):
        now=j_us();issued=int(packet.fields['issued_j_us']);expires=int(packet.fields['expires_j_us'])
        if now>=expires:return
        if now<issued:raise ValueError('original PC OFFER is from the future')
        if self.offer is not None or self.acquire is not None:raise RuntimeError('one original RAW offer/read is already pending')
        # 다른 FD의 actual Source ACK를 기다릴 뿐 wire의 publication hint로 등록하지 않는다.
        self.offer=packet

    def _expire_heads(self):
        now=j_us()
        if self.acquire is not None and now>=self.acquire.expires_us:
            raise TimeoutError('original PC read request expired without ACQUIRED')
        if self.early is not None and now+1>=self.early['frame'].expires_j_us:
            job=self.early['job_id'];self.early=None
            if self.outgoing is not None and self.outgoing['request_id']==job and self.link.pending is None:
                self.outgoing=self.outgoing_packet=None
        if self.acquire is None:
            expired=(self.offer is not None and now>=int(self.offer.fields['expires_j_us'])
                or self.acquire_draft is not None and now>=self.acquire_draft['expires'])
            if expired:self.offer=self.offer_typed=self.acquire_draft=None;self.meta=None
            if self.meta is not None and now+1>=self.meta[1].expires_j_us:self.meta=None

    def _acquire(self):
        if self.fenced or self.offer is None or self.meta is None or self.acquire is not None:return
        received,descriptor,purpose=self.meta;now=j_us()
        if self.offer_typed is None:
            publication=None
            if self.offer.payload.get('purpose')=='METRIC_MAP':
                if self.source.publication is None:return
                publication=sha256(self.source.storage_context.source_binding).hexdigest()
            context=PcReaderSlotContext(self.writer,self.actor.bind,publication)
            self.offer_typed=PcSlotMessage(self.offer,context,limits=self.settings.slot_limits)
        offer=self.offer_typed;p=offer.payload
        if now>=min(offer.expires_us,descriptor.expires_j_us):
            self.offer=self.offer_typed=self.meta=self.acquire_draft=None;return
        if ((p['frame_job_id'],p['phase'],tuple(p['full_key']),int(p['geometry_expires_j_us']),
                p['pc_frame_proof_sha256'],p['descriptor_sha256'],p['payload_length'],p['purpose'])
                !=(descriptor.job_id,descriptor.phase,descriptor.full_key,descriptor.expires_j_us,
                    descriptor.proof_sha256,descriptor.sha256,descriptor.payload_length,purpose)):
            raise ValueError('original RAW OFFER and S-forward A descriptor differ')
        if purpose=='METRIC_MAP' and (self.source.storage_context is None
                or offer.context.publication_sha256!=sha256(self.source.storage_context.source_binding).hexdigest()):
            self.offer=self.offer_typed=self.meta=self.acquire_draft=None;return
        if self.acquire_draft is None:
            expires=min(now+self.settings.slot_limits.read_budget_us,offer.expires_us,descriptor.expires_j_us)
            if now>=expires:return
            self.acquire_draft=dict(payload=pc_acquire_payload(offer),request_id=str(uuid4()),issued=now,expires=expires)
        out=self.actor.slot(**self.acquire_draft)
        if out is not None:
            self.acquire=PcSlotMessage(out,offer.context,offer,limits=self.settings.slot_limits)
            self.actor.leases_released=False

    def _copied(self,packet):
        if self.acquire is None or self.offer is None or self.meta is None:
            raise ValueError('ACQUIRED has no exact original offer/metadata/read request')
        acquired=PcSlotMessage(packet,self.acquire.context,self.acquire,limits=self.settings.slot_limits)
        acquired.record.read_request.request.check(j_us())
        owned,returned=self.actor.arena.copy_owned(acquired.record,expected_pc_slot(self.offer_typed))
        payload=acquired.payload;payload['op']='RELEASE';completed=returned.writable_posted_us
        release=self.actor.send_required('SLOT',payload,domain='RAW',revision=self.actor.bind.binding_revision,
            request_id=acquired.request_id,issued_us=completed,
            expires_us=completed+self.settings.cleanup_limits.transport_budget_us,allow_stop=True)
        PcReleaseMessage(release,acquired,limits=self.settings.cleanup_limits)
        self.actor.await_sent(release,allow_stop=True)
        self.actor.leases_released=True
        received,descriptor,purpose=self.meta
        self.offer=self.offer_typed=self.meta=self.acquire=self.acquire_draft=None
        if self.actor.stop is not None:raise _StopWork()
        if self.fenced or j_us()+1>=owned.expires_j_us:return
        if descriptor.phase=='EARLY':
            if self.early is not None:raise RuntimeError('a previous original EARLY frame is still active')
            frame=self.admission.admit_early(received,owned,now_us=j_us())
            if frame is not None:self.early=dict(frame=frame,job_id=descriptor.job_id,purpose=purpose,snapshot=None,ack=None)
        else:
            early=self.early
            if early is None or early['ack'] is None:raise ValueError('NN has no original EARLY snapshot full-send')
            admission=self.admission.admit_nn(early['frame'],received,owned,snapshot_ack_sent=early['ack'],now_us=j_us())
            if admission is None:return
            self._pump_inputs();now=j_us();snapshot=early['snapshot'];current=None
            view=self.admission.current_view(early['frame'],now_us=now)
            if snapshot is not None:
                try:
                    current=PcCurrentSensors.from_owner(self.sensor_owner,snapshot,now_us=now,
                        source_context_bytes=self.source_context,fence_revision=self.sensor_owner.fence_revision)
                except ValueError:
                    # 원 NN parser 사실과 현재 geometry 자격은 다르다. None은 geometry 취소다.
                    current=None
                self._apply_sensor_fence()
            item=PcMapperInput(admission,source_generation=self.actor.binding.sha256,current_pc_view=view,
                pc_owner_token=self.admission.owner_token,now_us=j_us(),sensor_snapshot=snapshot,current_sensors=current,
                context_bytes=self.source_context,parser_cap_us=None if self.control.ready else self.control.deadline.expires_us)
            self.compute.submit(item);self.early=None

    def _snapshot_ack(self):
        early=self.early
        if (early is None or early['ack'] is not None or self.outgoing is not None or self.link.pending is not None
                or self.fenced):return
        frame=early['frame'];now=j_us()
        if now+1>=frame.expires_j_us:self.early=None;return
        snapshot=early['snapshot']
        if early['purpose']=='METRIC_MAP' and snapshot is None:
            if self.sensor_owner is None:return
            try:
                view=self.admission.current_view(frame,now_us=now)
                candidate=self.sensor_owner.frame_snapshot_pc(frame,current_pc_view=view,now_us=now,
                    expires_us=frame.expires_j_us,job_id=early['job_id'],source_generation=self.actor.binding.sha256,
                    source_key=frame.observation.rgb.stamp.key,clock_replies=self.source.clock_replies)
                self._apply_sensor_fence()
                metadata=snapshot_ack_metadata(frame,job_id=early['job_id'],purpose=early['purpose'],snapshot=candidate,now_us=j_us())
            except (ValueError,SensorSnapshotUnavailable):
                self._apply_sensor_fence();return
            early['snapshot']=snapshot=candidate
        else:metadata=snapshot_ack_metadata(frame,job_id=early['job_id'],purpose=early['purpose'],snapshot=snapshot,now_us=now)
        self.outgoing=dict(kind='PC_SNAPSHOT_ACK',metadata=metadata,body=b'',request_id=early['job_id'],
            issued_us=j_us(),expires_us=frame.expires_j_us)

    def _flush_metadata(self):
        pending=self.link.pending
        if pending is not None and pending is not self.outgoing_packet:
            raise RuntimeError('actual M pending packet differs from its original selected draft')
        if self.outgoing is not None and j_us()>=self.outgoing['expires_us']:
            old=self.outgoing
            if pending is not None:
                if not self._retire_selected_work():raise RuntimeError('expired M work has no exact selected packet')
            elif self.outgoing_packet is not None:
                raise RuntimeError('selected M metadata packet disappeared without actual completion')
            else:
                if self.early is not None and self.early['job_id']==old['request_id']:self.early=None
                self.outgoing=self.outgoing_packet=None
        try:sent=self.link.flush()
        except TimeoutError:
            # preflight 뒤 E를 넘은 경우만 새 포트가 실제 만료/송신불명 아님을 증명한다.
            # socket timeout/Unknown/다른 원 packet이면 원 flush 예외를 그대로 보존한다.
            try:retired=self._retire_selected_work()
            except Exception:retired=False
            if not retired:raise
            sent=None
        if sent is not None:
            if self.outgoing is None or sent.packet is not self.outgoing_packet:
                raise RuntimeError('actual M metadata send has no original selected packet')
            if sent.packet.kind=='PC_SNAPSHOT_ACK':
                if self.early is None or self.early['job_id']!=sent.packet.request_id:
                    raise RuntimeError('actual snapshot ACK lost its original EARLY frame')
                self.early['ack']=sent
            elif sent.packet.kind=='PC_PARSE_COMPLETE':self.source.observe_parser_sent(sent)
            else:raise ValueError('unsupported actual M metadata completion')
            self.outgoing=self.outgoing_packet=None
        if self.outgoing is None:
            completed=self.control.take_parser_completion()
            if completed is not None and j_us()<completed.expires_us:
                self.outgoing=dict(kind='PC_PARSE_COMPLETE',metadata=completed.metadata,body=b'',
                    request_id=completed.job_id,issued_us=completed.completed_us,expires_us=completed.expires_us)
        if self.outgoing is not None and self.link.pending is None:
            self.outgoing_packet=self.link.send(**self.outgoing)

    def _retire_selected_work(self):
        pending=self.link.pending;old=self.outgoing
        if pending is None or old is None or pending is not self.outgoing_packet:return False
        if (pending.kind not in ('PC_SNAPSHOT_ACK','PC_PARSE_COMPLETE')
                or (pending.request_id,pending.kind,pending.issued_us,pending.expires_us)!=(
                    old['request_id'],old['kind'],old['issued_us'],old['expires_us'])):
            return False
        if self.link.discard_expired_work(pending) is not pending:
            raise RuntimeError('expired M work lost original packet identity')
        if self.early is not None and self.early['job_id']==old['request_id']:self.early=None
        self.outgoing=self.outgoing_packet=None
        return True

    def run(self):
        try:
            self._setup()
            while True:
                incoming=self.actor.poll()
                self._expire_heads()
                if not self.fenced:self._metadata()
                self._pump_inputs()
                for notice in self.source.take_aborts():self.client.receive_abort(notice)
                digest=self.source.context_sha256 if not self.fenced and self.source.context_sha256 is not None else self.actor.binding.sha256
                self.mailbox.fence(digest,now_us=j_us());self.client.pump(current_context_sha256=digest)
                self.control.tick()
                if not self.fenced:relay_publications(self.publication_port,self.source)
                if not self.fenced:self._flush_metadata()
                for packet in incoming:
                    if self.client.receive(packet):continue
                    if packet.kind!='SLOT':raise ValueError('unsupported PC M lifecycle/control statement')
                    if packet.payload.get('op')=='OFFER':self._offer(packet)
                    elif packet.payload.get('op')=='ACQUIRED':self._copied(packet)
                    else:raise ValueError('unsupported original PC M RAW operation')
                self._expire_heads()
                self._snapshot_ack();self._acquire()
                time.sleep(.002)
        except _StopWork:
            pass
        finally:
            self.actor.idle_hook=None;self.actor.state='QUIESCING'
            if self.admission is not None:self.admission.fence('PC_MAPPER_EXIT')
            if self.publication_port is not None:self.publication_port.discard_pending()
            if self.source is not None:self.source.discard_outputs()
            try:
                if self.compute is not None:
                    self.compute.request_stop()
                    remaining=2. if self.actor.stop is None else max(0.,min(2.,(self.actor.stop.expires_us-j_us())/1_000_000))
                    self.compute.join(remaining)
            except BaseException as error:
                self.actor.failure=type(error).__name__
                raise
            finally:
                try:
                    if self.client is not None:
                        state=self.client.snapshot()
                        if state['ownership_pinned'] or state['native_held']:
                            self.actor.leases_released=False
                            self.actor.failure=self.actor.failure or 'STORAGE_OWNERSHIP_PINNED'
                        self.client.close()
                except BaseException as error:
                    self.actor.leases_released=False;self.actor.failure=type(error).__name__
                    raise
                finally:
                    self.actor.sent_observer=None
                    try:
                        if self.source is not None:self.source.close()
                        else:self.settings.data_endpoint.close()
                    finally:
                        if self.link is not None:self.link.close()
                        else:self.settings.metadata_endpoint.close()
