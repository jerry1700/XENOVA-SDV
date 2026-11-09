"""S 단일 owner의 명시 PC RAW broker다. physical broker/parser는 바꾸지 않는다.

원 A metadata와 실제 ledger FILLED가 모두 있어야 OFFER한다. METRIC_MAP은 현재
S publication을 별도로 고정한 경우에만 공통 원장 예약 본문에 들어간다. token 동작은
기존 broker의 같은 bounded replacement를 재사용하며 Device/SHM attach는 하지 않는다.
read 이후 source가 바뀌어도 원 cleanup은 보존하고, 최종 M geometry/source fence는
caller가 검산한다. 원 read 예약은 이후 source 변경을 미래 권한으로 연장하지 않는다.
"""

from collections import Counter, deque
from dataclasses import dataclass
from hashlib import sha256
from threading import get_ident
from uuid import uuid4

from .local_envelope import StructuralPacket
from .pc_binding_messages import PcBindMessage
from .pc_slot_messages import (PcWriterSlotContext, PcReaderSlotContext, PcSlotMessage,
    PcSlotMetadata, PcFilledMetadata, PcReleaseMessage, PAYLOAD_FIELDS, FRAME_FIELDS)
from .raw_leases import Endpoint, RawScope, RawLeaseLedger, LeaseStatus, SlotState, RawRecoveryEvidence
from .resident_broker import ResidentRawBroker, BrokerError
from .slot_messages import SlotTimeLimits
from .slot_cleanup import CleanupTimeLimits
from .storage_messages import publication_binding
from ..storage.contracts import json_bytes


@dataclass(frozen=True)
class _PcDraft:
    op: str
    payload: dict
    request_id: str
    issued_us: int
    expires_us: int
    parent: PcSlotMessage | None
    context: PcReaderSlotContext | None = None


class PcRawBroker(ResidentRawBroker):
    """PC BIND/profile만 받는다. 원장·token 정책을 복제하거나 parser로 relabel하지 않는다.

    inherited _replace는 generic key/token/state 연산뿐이다. 실제 A pause,
    old M exit와 native 정규화가 없는 reader 교체는 이 클래스도 승인하지 않는다.
    """

    def __init__(self,writer_bind,scope,reader,raw_tokens,*,
                 limits=SlotTimeLimits(150_000,500_000,150_000),
                 cleanup_limits=CleanupTimeLimits(1_000_000)):
        if (type(writer_bind) is not PcBindMessage or type(scope) is not RawScope
                or type(reader) is not Endpoint or type(limits) is not SlotTimeLimits
                or type(cleanup_limits) is not CleanupTimeLimits or type(raw_tokens) is not tuple
                or len(raw_tokens)!=2 or any(type(pair) is not tuple or len(pair)!=2 for pair in raw_tokens)):
            raise BrokerError('registered PC writer/two-slot scope and real token pairs required')
        self.writer=PcWriterSlotContext(writer_bind,scope);self.reader=reader;self.reader_context=None
        self.limits,self.cleanup_limits=limits,cleanup_limits;self._tokens=raw_tokens
        self._ledger=RawLeaseLedger(scope,reader)
        self._request=self._writer_draft=self._reader_draft=None
        self._grants=[None,None];self._offers=[None,None];self._previous_offers=[None,None]
        self._active_offer=None;self._reads=[deque(maxlen=2),deque(maxlen=2)]
        self._counts=Counter();self._quiescing=False;self._paused=None;self._last_reader_recovery=None
        self._metadata=[None,None];self._unjoined=None;self._publication=None;self._publication_bytes=None
        self._joined=deque()
        self._broker_token=object();self._thread=get_ident()

    def _owner(self):
        if get_ident()!=self._thread:raise RuntimeError('PC RAW broker belongs to original S owner')

    def bind_reader(self,reader_bind):
        self._owner();context=PcReaderSlotContext(self.writer,reader_bind)
        if context.reader!=self.reader:raise BrokerError('PC reader differs from registered current M')
        if self.reader_context is not None:
            if self.reader_context.bind.raw!=reader_bind.raw:raise BrokerError('PC reader BIND changed without recovery')
            return False
        self.reader_context=context
        return True

    def set_publication(self,raw):
        """S의 실제 current canonical binding만 공급한다. 설정 문자열 자체가 READY는 아니다."""
        self._owner()
        if raw is None:key=None
        else:
            if type(raw) is not bytes:raise BrokerError('original canonical publication bytes required')
            fields=publication_binding(json_bytes(raw,2048))
            from ..storage.contracts import canonical
            if (canonical(fields)!=raw or fields['service_instance']!=self.writer.scope.service_instance
                    or fields['raw_binding_sha256']!=self.writer.scope.raw_binding_sha256
                    or fields['mapper_worker']!=self.reader.worker_instance):
                raise BrokerError('PC publication differs from registered service/descriptor/current M')
            key=sha256(raw).hexdigest()
        if raw==self._publication_bytes:return False
        self._publication_bytes,self._publication=raw,key
        # 아직 예약하지 않은 안내만 철회한다. 원 IN_USE/ACQUIRED와 exact cleanup은 보존한다.
        self._active_offer=None
        if self._reader_draft is not None and self._reader_draft.op=='OFFER':self._reader_draft=None
        return True

    @staticmethod
    def _matches(meta,payload):
        return (payload['frame_job_id']==meta.job_id and payload['phase']==meta.phase
            and payload['geometry_expires_j_us']==str(meta.expires_j_us)
            and tuple(payload['full_key'])==meta.full_key and payload['pc_frame_proof_sha256']==meta.proof_sha256
            and payload['purpose']==meta.purpose)

    def _join(self,meta,grant,info,now_us):
        if (info is not self._ledger.snapshot.slots[info.key.slot].filled or info.key!=grant.record.key
                or not grant.issued_us<=info.completed_us<grant.expires_us
                or not self._matches(meta,grant.payload) or info.payload_length!=meta.payload_length
                or info.descriptor_sha256!=meta.descriptor_sha256
                or not info.completed_us<=meta.received.packet.issued_us<=meta.received.received_j_us<=now_us<meta.expires_j_us):
            raise BrokerError('PC descriptor lost actual original ledger FILLED/grant/full frame binding')
        old=self._metadata[info.key.slot]
        if old is not None and old.original_filled_info.key==info.key:
            if old.metadata.received.packet.raw!=meta.received.packet.raw:raise BrokerError('same PC FILLED changed original metadata event')
            return old
        result=object.__new__(PcFilledMetadata)
        for name,value in dict(metadata=meta,original_grant=grant,original_filled_info=info,_broker_token=self._broker_token).items():
            object.__setattr__(result,name,value)
        if len(self._joined)>=2:
            self.fence(now_us);raise BrokerError('original PC metadata delivery backlog is full')
        self._metadata[info.key.slot]=result
        self._joined.append(result)
        if self._unjoined is meta:self._unjoined=None
        self._counts['metadata_joined']+=1
        return result

    def observe_metadata(self,received,*,origin_link,now_us):
        """실제 등록 A endpoint의 원 full-recv ticket만 받는다. 순서가 뒤집히면 한 개만 기다린다."""
        self._owner();self._now(now_us)
        meta=PcSlotMetadata(received,origin_link,self.writer,now_us=now_us)
        grants=[g for g in self._grants if g is not None and self._matches(meta,g.payload)]
        if len(grants)!=1:raise BrokerError('A metadata has no unique original selected PC write grant')
        grant=grants[0];slot=grant.record.key.slot;current=self._ledger.snapshot.slots[slot]
        if current.write is None or current.write.key!=grant.record.key:raise BrokerError('A metadata belongs to an old slot generation')
        if current.filled is not None and current.filled.key==grant.record.key:return self._join(meta,grant,current.filled,now_us)
        if self._unjoined is not None:
            if self._unjoined.received.packet.raw!=received.packet.raw:raise BrokerError('one original PC metadata/FILLED join is unresolved')
            return None
        self._unjoined=meta;self._counts['metadata_wait_filled']+=1
        return None

    def metadata_for(self,write_key):
        self._owner()
        for value in self._metadata:
            if value is not None and value.original_filled_info.key==write_key:return value
        return None

    def take_filled_metadata(self):
        """새 실제 join당 원 참조를 한 번 전달한다. EMPTY/expiry를 새 fact로 만들지 않는다."""
        self._owner()
        return self._joined.popleft() if self._joined else None

    def require_filled(self,value,*,now_us):
        self._owner();self._now(now_us)
        if type(value) is not PcFilledMetadata or value._broker_token is not self._broker_token:
            raise BrokerError('original same-broker PC FILLED metadata required')
        key=value.original_filled_info.key;state=self._ledger.snapshot;slot=state.slots[key.slot]
        if (state.fenced or self._metadata[key.slot] is not value or slot.filled is not value.original_filled_info
                or self._grants[key.slot] is not value.original_grant or now_us>=value.expires_j_us):
            raise BrokerError('original PC FILLED metadata is no longer current')
        return value

    def _expire_join(self,now_us):
        if self._unjoined is not None and now_us>=self._unjoined.expires_j_us:
            self._counts['metadata_expired_without_filled']+=1;self.fence(now_us)
            raise BrokerError('original PC metadata expired before matching FILLED')
        for slot in self._ledger.snapshot.slots:
            if slot.state is SlotState.READY and self.metadata_for(slot.filled.key) is None:
                grant=self._grants[slot.filled.key.slot]
                if grant is not None and now_us>=int(grant.payload['geometry_expires_j_us']):
                    self._counts['filled_expired_without_metadata']+=1;self.fence(now_us)
                    raise BrokerError('original PC FILLED expired before matching metadata')

    def receive(self,role,packet,*,now_us):
        self._owner();self._now(now_us)
        if type(packet) is not StructuralPacket or packet.kind!='SLOT' or role not in ('A','M'):
            raise BrokerError('actual registered PC SLOT packet required')
        p=packet.payload
        if type(p) is not dict or set(p)!=PAYLOAD_FIELDS:raise BrokerError('exact19 PC SLOT payload required')
        op=p['op']
        if role=='A' and op=='REQUEST':
            m=PcSlotMessage(packet,self.writer,limits=self.limits);self._live(m,now_us)
            if self._quiescing:self._counts['requests_paused']+=1;return None
            if self._ledger.snapshot.fenced:raise BrokerError('PC write request after arena fence')
            retained=[self._request]+[g.parent for g in self._grants if g is not None]
            for old in retained:
                if old is not None and old.request_id==m.request_id:
                    if old.raw!=m.raw:raise BrokerError('PC write request ID changed original bytes')
                    self._counts['request_duplicate']+=1;return None
            if self._request is not None or any(s.state is SlotState.WRITING for s in self._ledger.snapshot.slots):
                raise BrokerError('one original PC write is unresolved')
            self._request=m;self._counts['requests']+=1;return None
        if role=='A' and op=='FILLED':
            index=p['slot']
            if type(index) is not int or index not in (0,1) or self._grants[index] is None:
                raise BrokerError('PC FILLED has no original selected grant')
            m=PcSlotMessage(packet,self.writer,self._grants[index],limits=self.limits);self._live(m,now_us)
            result=self._ledger.filled(m.record,now_us=now_us)
            if self._unjoined is not None and self._matches(self._unjoined,m.payload):
                self._join(self._unjoined,self._grants[index],self._ledger.snapshot.slots[index].filled,now_us)
            self._counts['filled']+=result.status is LeaseStatus.APPLIED
            return result
        if role!='M' or self.reader_context is None:raise BrokerError('registered current PC reader is not bound')
        if op=='ACQUIRE':
            if self._quiescing:self._counts['acquires_paused']+=1;return None
            index=p['slot']
            if type(index) is not int or index not in (0,1):raise BrokerError('original PC read slot required')
            offer=None
            for candidate in (self._offers[index],self._previous_offers[index]):
                if candidate is not None:
                    expected=candidate.payload
                    if all(type(p[k]) is type(expected[k]) and p[k]==expected[k] for k in PAYLOAD_FIELDS-{'op','read_token'}):
                        offer=candidate;break
            if offer is None:self._counts['stale_acquire']+=1;return None
            m=PcSlotMessage(packet,offer.context,offer,limits=self.limits);self._live(m,now_us)
            for queue in self._reads:
                for old in queue:
                    if old.request_id==m.request_id:
                        if old.parent.raw!=m.raw:raise BrokerError('PC read request ID changed original bytes')
                        self._counts['acquire_duplicate']+=1;return None
            state=self._ledger.snapshot
            if (state.fenced or now_us>=offer.expires_us or state.slots[index].state is not SlotState.READY
                    or state.slots[index].filled!=m.record.offer.content):
                self._counts['stale_acquire']+=1;return None
            bound=self.require_filled(self.metadata_for(m.record.offer.content.key),now_us=now_us)
            if not self._matches(bound,m.payload):raise BrokerError('PC read differs from original metadata/FILLED')
            publication=m.record.publication_sha256
            if m.record.purpose=='METRIC_MAP' and (self._publication is None or publication!=self._publication):
                self._counts['metric_source_not_current']+=1;return None
            if self._reader_draft is not None or any(s.state is SlotState.IN_USE for s in state.slots):
                raise BrokerError('PC reader already holds an outstanding read')
            # 물리 public acquire는 계속 METRIC_MAP를 거절한다. 여기까지 원 metadata/source를 닫았다.
            result=self._ledger._acquire(m.record,str(uuid4()),now_us=now_us,publication_sha256=self._publication)
            lease=result.value;values=m.payload;values.update(op='ACQUIRED',read_token=lease.key.read_token)
            self._reader_draft=_PcDraft('ACQUIRED',values,m.request_id,lease.reserved_us,m.expires_us,m,m.context)
            self._active_offer=None;self._counts['reads_reserved']+=1
            return result
        if op=='RELEASE':
            for queue in self._reads:
                for parent in queue:
                    old=parent.payload
                    if parent.request_id==packet.fields['request_id'] and all(p.get(k)==old[k] for k in ('slot','generation','write_token','read_token')):
                        m=PcReleaseMessage(packet,parent,limits=self.cleanup_limits)
                        result=self._ledger.release_remote(m.record,now_us=now_us)
                        self._counts['released']+=result.status is LeaseStatus.APPLIED
                        self._counts['release_duplicate']+=result.status is LeaseStatus.DUPLICATE_NO_EFFECT
                        return result
            self._counts['release_unknown']+=1;return None
        raise BrokerError('unsupported PC RAW role/operation')

    def _reserve_writer(self,now_us):
        request=self._request
        if request is None or self._writer_draft is not None:return
        self._live(request,now_us);state=self._ledger.snapshot
        if any(s.state is SlotState.WRITING for s in state.slots):return
        free=next((i for i,s in enumerate(state.slots) if s.state is SlotState.FREE),None)
        if free is not None:grant=self._ledger.reserve_write(free,request.record,str(uuid4()),now_us=now_us).value
        else:
            protected=None if self._active_offer is None else self._active_offer.record.content.key.slot
            if self._reader_draft is not None and self._reader_draft.op=='OFFER':protected=self._reader_draft.payload['slot']
            candidates=[(s.filled.completed_us,i) for i,s in enumerate(state.slots)
                if s.state is SlotState.READY and i!=protected]
            if not candidates:return
            grant=self._replace(min(candidates)[1],request,now_us)
        values=request.payload
        values.update(op='GRANT',slot=grant.key.slot,generation=str(grant.key.generation),write_token=grant.key.write_token)
        self._writer_draft=_PcDraft('GRANT',values,request.request_id,grant.granted_us,request.expires_us,request)

    def _send(self,draft,link,context,now_us):
        if now_us>=draft.expires_us:
            if draft.op=='OFFER':self._counts['offer_expired_before_selection']+=1;return None,True
            self.fence(now_us);raise BrokerError('original reserved PC statement expired before selection')
        try:
            packet=link.send('SLOT',draft.payload,domain='RAW',revision=self.writer.bind.binding_revision,
                request_id=draft.request_id,issued_us=draft.issued_us,expires_us=draft.expires_us)
        except BaseException:self.fence(now_us);raise
        if packet is None:self._counts['send_busy']+=1;return None,False
        m=PcSlotMessage(packet,context,draft.parent,limits=self.limits)
        if draft.op=='GRANT':
            index=m.record.key.slot;self._grants[index]=m;self._metadata[index]=None
            self._request=None;self._counts['grants_selected']+=1
        elif draft.op=='OFFER':
            index=m.record.content.key.slot;self._previous_offers[index]=self._offers[index]
            self._offers[index]=m;self._active_offer=m;self._counts['offers_selected']+=1
        else:
            self._reads[m.record.key.write_key.slot].append(m);self._counts['acquired_selected']+=1
        return packet,True

    def pump(self,writer_link,reader_link=None,*,now_us):
        """원 draft를 유지하며 유한 선택만 한다. 반환은 실제 full-send 증명과 다르다."""
        self._owner();self._now(now_us);selected=[]
        if self._ledger.snapshot.fenced:return selected
        self._expire_join(now_us)
        if self._active_offer is not None and now_us>=self._active_offer.expires_us:self._active_offer=None
        if not self._quiescing and not any(s.state is SlotState.READY and self.metadata_for(s.filled.key) is None
                for s in self._ledger.snapshot.slots):self._reserve_writer(now_us)
        if self._writer_draft is not None:
            packet,done=self._send(self._writer_draft,writer_link,self.writer,now_us)
            if packet is not None:selected.append(packet)
            if done:self._writer_draft=None
        if self._quiescing or self.reader_context is None or reader_link is None:return selected
        if self._reader_draft is None and self._active_offer is None:
            state=self._ledger.snapshot
            if not any(s.state is SlotState.IN_USE for s in state.slots):
                candidates=[]
                for index,slot in enumerate(state.slots):
                    if slot.state is not SlotState.READY:continue
                    meta=self.metadata_for(slot.filled.key)
                    if meta is None or now_us>=min(meta.expires_j_us,slot.filled.completed_us+self.limits.offer_budget_us):continue
                    if meta.purpose=='METRIC_MAP' and self._publication is None:continue
                    candidates.append((slot.filled.completed_us,index,meta))
                if candidates:
                    _,index,meta=max(candidates,key=lambda item:item[:2]);info=self._ledger.offer(index,now_us=now_us).content
                    values=meta.original_grant.payload
                    publication=self._publication if meta.purpose=='METRIC_MAP' else None
                    values.update(op='OFFER',consumer=self.reader.worker_instance,publication_binding_sha256=publication,
                        payload_length=info.payload_length,descriptor_sha256=info.descriptor_sha256)
                    context=PcReaderSlotContext(self.writer,self.reader_context.bind,publication)
                    self._reader_draft=_PcDraft('OFFER',values,str(uuid4()),info.completed_us,
                        min(info.completed_us+self.limits.offer_budget_us,meta.expires_j_us),None,context)
        if self._reader_draft is not None:
            draft=self._reader_draft
            packet,done=self._send(draft,reader_link,draft.context,now_us)
            if packet is not None:selected.append(packet)
            if done:self._reader_draft=None
        return selected

    def rebind_reader(self,reader,evidence,*,now_us):
        """S의 실제 A pause·old M exit·native 정규화 증거가 있어야 기존 원장을 교체한다."""
        self._owner();self._now(now_us)
        if (not self._quiescing or type(reader) is not Endpoint or type(evidence) is not RawRecoveryEvidence
                or evidence.barrier.scope!=self.writer.scope or evidence.barrier.old_reader!=self.reader):
            raise BrokerError('PC reader recovery requires original quiescence/exit/normalization facts')
        changed=self._ledger.rebind_reader(reader,evidence,now_us=now_us)
        if changed:
            self.reader=reader;self.reader_context=None;self._request=self._writer_draft=self._reader_draft=None
            self._offers,self._previous_offers=[None,None],[None,None];self._active_offer=None
            self._metadata=[None,None];self._joined.clear();self._unjoined=None;self._publication=self._publication_bytes=None
            self._last_reader_recovery=(reader,evidence);self._counts['reader_recoveries']+=1
        return changed

    def resume(self):
        self._owner()
        if not self._quiescing or self.reader_context is None or self._last_reader_recovery is None:
            raise BrokerError('PC resume requires actual recovered and rebound reader')
        self._quiescing=False

    def fence(self,now_us):
        self._owner();return self._ledger.fence(now_us=now_us)
