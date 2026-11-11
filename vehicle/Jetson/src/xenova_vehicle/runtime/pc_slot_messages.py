"""PC RAW control의 별도 정적 profile와 원 A descriptor 수신 사실이다.

physical SlotMessage를 넓히지 않는다. descriptor 검사는 pixel/NN tensor 본문을 읽지
않으며 실제 copy/hash는 PcArenaReader 책임이다. PcFilledMetadata도 S broker가 보유한
원 FILLED와 대조해야 하며 source/clock/Scene/주행 승인을 스스로 발급하지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256
import json

from .local_envelope import StructuralPacket
from .pc_binding_messages import PcBindMessage
from .pc_channel import PcActorLink, PcActorReceived
from .pc_slot import (PcSlotEncoding, PcSlotContext, ExpectedPcSlot, MAX_DESCRIPTOR_BYTES,
    MAX_PAYLOAD_BYTES, METADATA_BYTES, RGB_BYTES, DEPTH_BYTES, PROFILE as DESCRIPTOR_PROFILE,
    _json, _canonical, _proof, _frame_key)
from .raw_leases import (RawScope, Endpoint, LeaseRequest, WriteGrant, FilledInfo, Offer,
    ReadRequest, ReadKey, ReadLease, RemoteReturnStatement)
from .slot_messages import SlotTimeLimits
from .slot_cleanup import CleanupTimeLimits
from ..simulation.pc_input import PcSourceBinding


PROFILE='pc-raw-slot-1'
META_PROFILE='pc-slot-meta-1'
PAYLOAD_FIELDS=frozenset(('profile','op','arena_id','slot','generation','write_token','read_token',
    'producer','consumer','raw_binding_sha256','publication_binding_sha256','purpose','payload_length',
    'descriptor_sha256','frame_job_id','phase','geometry_expires_j_us','full_key','pc_frame_proof_sha256'))
FRAME_FIELDS=('frame_job_id','phase','geometry_expires_j_us','full_key','pc_frame_proof_sha256','purpose')


class PcSlotMessageError(ValueError):pass


def _need(value,reason):
    if not value:raise PcSlotMessageError(reason)


def _uint(value,*,positive=False):
    _need(type(value) is int and int(positive)<=value<2**63,'bounded original native integer required')
    return value


def _decimal(value,*,positive=False):
    _need(type(value) is str and 1<=len(value)<=19 and value.isascii() and value.isdecimal()
        and str(int(value))==value,'canonical original decimal required')
    return _uint(int(value),positive=positive)


def _sha(value):
    _need(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'canonical SHA required')
    return value


def _uuid(value):
    from uuid import UUID
    _need(type(value) is str and len(value)==36,'canonical UUID required')
    try:_need(str(UUID(value))==value and UUID(value).int!=0,'canonical nonzero UUID required')
    except ValueError as error:raise PcSlotMessageError('invalid UUID') from error
    return value


def _same(value,expected):
    _need(type(value) is type(expected) and value==expected,'original PC field/type changed')


@dataclass(frozen=True)
class PcWriterSlotContext:
    bind: PcBindMessage
    scope: RawScope

    def __post_init__(self):
        _need(type(self.bind) is PcBindMessage and type(self.scope) is RawScope,'actual PC writer BIND/scope required')
        p=self.bind.context.peer
        _need(p.role=='A' and self.scope==self.bind.context.scope
            and self.scope.writer==Endpoint(p.channel_epoch,p.worker_instance)
            and self.bind.binding.sha256==self.scope.raw_binding_sha256,'registered PC writer differs')


@dataclass(frozen=True)
class PcReaderSlotContext:
    writer: PcWriterSlotContext
    bind: PcBindMessage
    publication_sha256: str | None = None

    def __post_init__(self):
        _need(type(self.writer) is PcWriterSlotContext and type(self.bind) is PcBindMessage,'actual PC reader BIND required')
        _need(self.bind.context.peer.role=='M' and self.bind.context.scope==self.writer.scope
            and self.bind.binding.canonical_bytes==self.writer.bind.binding.canonical_bytes
            and self.bind.binding_revision==self.writer.bind.binding_revision,'registered PC reader differs')
        if self.publication_sha256 is not None:_sha(self.publication_sha256)

    @property
    def reader(self):
        p=self.bind.context.peer
        return Endpoint(p.channel_epoch,p.worker_instance)


def _writer(context):
    if type(context) is PcWriterSlotContext:return context
    _need(type(context) is PcReaderSlotContext,'exact PC SLOT context required')
    return context.writer


@dataclass(frozen=True,init=False)
class PcSlotDescriptor:
    raw: bytes
    binding: PcSourceBinding
    job_id: str
    phase: str
    expires_j_us: int
    full_key: tuple
    proof_bytes: bytes
    proof_sha256: str
    nn_key: tuple | None
    payload_length: int

    def __init__(self,*args,**kwargs):raise TypeError('use parse_pc_slot_descriptor')
    @property
    def fields(self):return json.loads(self.raw)
    @property
    def sha256(self):return sha256(self.raw).hexdigest()


def parse_pc_slot_descriptor(raw,binding):
    """원 descriptor만 검사한다. 원 수신·실 payload·현재 clock 승인과 별개다."""
    _need(type(binding) is PcSourceBinding,'exact PC binding required')
    d=_json(raw,MAX_DESCRIPTOR_BYTES)
    _need(set(d)=={'profile','phase','binding','job_id','expires_j_us','full_key','proof','planes','nn'}
        and d['profile']==DESCRIPTOR_PROFILE and d['phase'] in ('EARLY','NN')
        and _canonical(d['binding'])==binding.canonical_bytes,'exact original PC descriptor required')
    _uuid(d['job_id']);expires=_uint(d['expires_j_us'])
    _need(type(d['full_key']) is list,'original full key list required')
    key=tuple(d['full_key']);_frame_key(key,binding)
    _need(type(d['proof']) is dict and 'packet_header' in d['proof'],'original PC proof/header required')
    proof_raw=_canonical(d['proof'])
    proof,header=_proof(proof_raw,_canonical(d['proof']['packet_header']),binding,key,expires)
    names={'rgb_video','depth_right'}|({'nn_output0'} if d['phase']=='NN' else set())
    _need(type(d['planes']) is dict and set(d['planes'])==names,'exact PC plane declaration required')
    offset=METADATA_BYTES
    for name,length in (('rgb_video',RGB_BYTES),('depth_right',DEPTH_BYTES),
            *((('nn_output0',None),) if d['phase']=='NN' else ())):
        row=d['planes'][name]
        _need(type(row) is dict and set(row)=={'offset','length','sha256'},'exact PC plane extent required')
        _same(row['offset'],offset);size=_uint(row['length'],positive=True);_sha(row['sha256'])
        if length is not None:
            _same(size,length);_same(row['sha256'],header['rgb' if name=='rgb_video' else 'depth']['sha256'])
        offset+=size
    length=offset-METADATA_BYTES
    _need(length<=MAX_PAYLOAD_BYTES,'PC plane extents exceed original slot')
    nn_key=None
    if d['phase']=='EARLY':_same(d['nn'],None)
    else:
        n=d['nn'];_need(type(n) is dict and set(n)=={'input','output'},'original NN descriptor required')
        inp,out=n['input'],n['output']
        _need(type(inp) is dict and set(inp)=={'stream','sequence','device_us','sdk_us','received_j_us','width','height','encoding','context_key'}
            and type(out) is dict and set(out)=={'stream','sequence','device_us','sdk_us','received_j_us','context_key','tensors','golden'},
            'exact original NN input/output metadata required')
        _same(inp['stream'],'nn_input');_same(out['stream'],'nn_output')
        _same(inp['width'],320);_same(inp['height'],320);_same(inp['encoding'],'BGR888p')
        for x in (inp,out):
            _same(x['sequence'],key[3]);_same(x['context_key'],binding.fields['nn_manifest_sha256'])
            for field in ('device_us','sdk_us','received_j_us'):_uint(x[field])
            _need(proof['frame_received_j_us']<=x['received_j_us']<expires,'original NN receipt outside frame E')
        _need(all(inp[k]==out[k] for k in ('sequence','device_us','sdk_us','context_key')),'original NN enqueue keys differ')
        _need((out['golden'] is None or type(out['golden']) is dict)
            and type(out['tensors']) is list and len(out['tensors'])==3,'bounded original NN layout required')
        for row in out['tensors']:
            _need(type(row) is dict and set(row)=={'name','data_type_code','order_code','num_dimensions','dims','strides','offset','data_type_text','order_text'},
                'exact original runtime tensor layout required')
        nn_key=key+(out['device_us'],out['sdk_us'],out['context_key'],header['rgb']['sha256'])
    result=object.__new__(PcSlotDescriptor)
    for name,value in dict(raw=raw,binding=binding,job_id=d['job_id'],phase=d['phase'],expires_j_us=expires,
            full_key=key,proof_bytes=proof_raw,proof_sha256=sha256(proof_raw).hexdigest(),nn_key=nn_key,payload_length=length).items():
        object.__setattr__(result,name,value)
    return result


def pc_slot_meta_fields(encoding,*,purpose):
    _need(type(encoding) is PcSlotEncoding and purpose in ('PARSER_WARMUP','METRIC_MAP'),'original PC encoding/purpose required')
    return dict(profile=META_PROFILE,purpose=purpose,descriptor_sha256=encoding.descriptor_sha256)


@dataclass(frozen=True,init=False)
class PcSlotMetadata:
    received: PcActorReceived
    writer_context: PcWriterSlotContext
    descriptor: PcSlotDescriptor
    purpose: str

    def __init__(self,received,origin_link,writer_context,*,now_us):
        _need(type(origin_link) is PcActorLink and type(writer_context) is PcWriterSlotContext,'actual A link/writer context required')
        origin_link.require_received(received);_uint(now_us)
        p=received.packet;h=p.header;w=writer_context.bind.context.peer
        _need(origin_link.owner_role=='S' and origin_link.peer==w
            and origin_link.binding.canonical_bytes==writer_context.bind.binding.canonical_bytes
            and p.kind in ('PC_EARLY_META','PC_NN_RESULT_META') and received.received_j_us<=now_us<p.expires_us,
            'original current registered A metadata required')
        m=p.metadata
        _need(type(m) is dict and set(m)=={'profile','purpose','descriptor_sha256'} and m['profile']==META_PROFILE
            and m['purpose'] in ('PARSER_WARMUP','METRIC_MAP') and _sha(m['descriptor_sha256'])==sha256(p.body).hexdigest(),
            'exact original PC meta fields/body SHA required')
        d=parse_pc_slot_descriptor(p.body,writer_context.bind.binding)
        _need((p.request_id,p.expires_us,p.kind)==(d.job_id,d.expires_j_us,'PC_EARLY_META' if d.phase=='EARLY' else 'PC_NN_RESULT_META'),
            'original metadata job/phase/E differs')
        _need(d.nn_key is None or max(d.fields['nn'][k]['received_j_us'] for k in ('input','output'))<=p.issued_us,
            'NN metadata precedes original NN receipts')
        for name,value in dict(received=received,writer_context=writer_context,descriptor=d,purpose=m['purpose']).items():
            object.__setattr__(self,name,value)

    @property
    def job_id(self):return self.descriptor.job_id
    @property
    def phase(self):return self.descriptor.phase
    @property
    def expires_j_us(self):return self.descriptor.expires_j_us
    @property
    def full_key(self):return self.descriptor.full_key
    @property
    def nn_key(self):return self.descriptor.nn_key
    @property
    def proof_bytes(self):return self.descriptor.proof_bytes
    @property
    def proof_sha256(self):return self.descriptor.proof_sha256
    @property
    def descriptor_bytes(self):return self.descriptor.raw
    @property
    def descriptor_sha256(self):return self.descriptor.sha256
    @property
    def payload_length(self):return self.descriptor.payload_length


@dataclass(frozen=True,init=False)
class PcFilledMetadata:
    metadata: PcSlotMetadata
    original_grant: object
    original_filled_info: FilledInfo
    _broker_token: object

    def __init__(self,*args,**kwargs):raise TypeError('original S broker FILLED binding required')
    def __getattr__(self,name):
        if name in ('received','job_id','phase','purpose','expires_j_us','full_key','nn_key','proof_bytes',
                'proof_sha256','descriptor_bytes','descriptor_sha256','payload_length'):
            return getattr(self.metadata,name)
        raise AttributeError(name)


def pc_write_request_payload(binding,scope,*,job_id,phase,expires_j_us,full_key,pc_frame_proof_sha256,purpose):
    _need(type(binding) is PcSourceBinding and type(scope) is RawScope and scope.raw_binding_sha256==binding.sha256,'original PC scope required')
    _uuid(job_id);_uint(expires_j_us);_frame_key(full_key,binding);_sha(pc_frame_proof_sha256)
    _need(phase in ('EARLY','NN') and purpose in ('PARSER_WARMUP','METRIC_MAP'),'explicit original phase/purpose required')
    return dict(profile=PROFILE,op='REQUEST',arena_id=scope.arena_id,slot=None,generation=None,write_token=None,read_token=None,
        producer=scope.writer.worker_instance,consumer=None,raw_binding_sha256=binding.sha256,publication_binding_sha256=None,
        purpose=purpose,payload_length=None,descriptor_sha256=None,frame_job_id=job_id,phase=phase,
        geometry_expires_j_us=str(expires_j_us),full_key=list(full_key),pc_frame_proof_sha256=pc_frame_proof_sha256)


@dataclass(frozen=True,init=False)
class PcSlotMessage:
    packet: StructuralPacket
    context: object
    parent: object
    limits: SlotTimeLimits
    record: object
    op: str
    request_id: str
    issued_us: int
    expires_us: int
    cap_us: int | None

    def __init__(self,packet,context,parent=None,*,limits,cap_us=None):
        _need(type(packet) is StructuralPacket and type(limits) is SlotTimeLimits,'original SLOT packet/limits required')
        w=_writer(context);h=packet.fields;p=packet.payload
        _need(type(p) is dict and set(p)==PAYLOAD_FIELDS and p['profile']==PROFILE,'exact19 PC SLOT fields required')
        op=p['op'];_need(op in ('REQUEST','GRANT','FILLED','OFFER','ACQUIRE','ACQUIRED'),'unsupported PC SLOT operation')
        reader=op in ('OFFER','ACQUIRE','ACQUIRED')
        _need(type(context) is (PcReaderSlotContext if reader else PcWriterSlotContext),'PC SLOT direction/context differs')
        peer=context.bind.context.peer
        for name,value in dict(kind='SLOT',binding_domain='RAW',service_instance=w.scope.service_instance,
                channel_epoch=peer.channel_epoch,sender_instance=peer.worker_instance if op in ('REQUEST','FILLED','ACQUIRE') else w.scope.service_instance).items():
            _same(h[name],value)
        _same(_decimal(h['binding_revision'],positive=True),w.bind.binding_revision)
        _same(p['arena_id'],w.scope.arena_id);_same(p['producer'],w.scope.writer.worker_instance)
        _same(p['raw_binding_sha256'],w.scope.raw_binding_sha256)
        _same(p['consumer'],context.reader.worker_instance if reader else None)
        _need(p['purpose'] in ('PARSER_WARMUP','METRIC_MAP') and p['phase'] in ('EARLY','NN'),'original PC phase/purpose required')
        _uuid(p['frame_job_id']);geometry=_decimal(p['geometry_expires_j_us']);_sha(p['pc_frame_proof_sha256'])
        _need(type(p['full_key']) is list,'original full key list required');_frame_key(tuple(p['full_key']),w.bind.binding)
        publication=context.publication_sha256 if reader else None
        _same(p['publication_binding_sha256'],publication)
        if reader and p['purpose']=='METRIC_MAP':_need(publication is not None,'metric read requires actual current publication')
        if reader and p['purpose']=='PARSER_WARMUP':_same(publication,None)
        issued,expires=(_decimal(h[k]) for k in ('issued_j_us','expires_j_us'))
        _need(issued<expires<=geometry,'PC statement exceeds original geometry E')
        parents={'GRANT':'REQUEST','FILLED':'GRANT','ACQUIRE':'OFFER','ACQUIRED':'ACQUIRE'}
        if op in parents:
            _need(type(parent) is PcSlotMessage and parent.op==parents[op] and parent.context==context and parent.limits==limits,'original PC parent required')
            for name in FRAME_FIELDS:_same(p[name],parent.payload[name])
        else:_same(parent,None)
        if cap_us is not None:_uint(cap_us)
        local_cap=cap_us if op in ('REQUEST','ACQUIRE') else None if parent is None else parent.cap_us
        if op not in ('REQUEST','ACQUIRE'):_same(cap_us,None)
        if op=='REQUEST':
            for name in ('slot','generation','write_token','read_token','payload_length','descriptor_sha256'):_same(p[name],None)
            _need(expires-issued<=limits.copy_budget_us,'PC copy request exceeds original bound')
            record=LeaseRequest(h['request_id'],issued,expires,cap_us)
        else:
            _need(type(p['slot']) is int and p['slot'] in (0,1),'original PC slot required')
            from .raw_leases import WriteKey
            key=WriteKey(w.scope,p['slot'],_decimal(p['generation'],positive=True),p['write_token'])
            if op=='GRANT':
                for name in ('read_token','payload_length','descriptor_sha256'):_same(p[name],None)
                _need(h['request_id']==parent.request_id and expires==parent.expires_us,'original write request changed')
                record=WriteGrant(key,parent.record,issued)
            elif op in ('FILLED','OFFER'):
                _same(p['read_token'],None);length=_uint(p['payload_length'],positive=True);_sha(p['descriptor_sha256'])
                _need(length<=MAX_PAYLOAD_BYTES,'PC used length exceeds slot')
                if op=='FILLED':
                    _need(key==parent.record.key and h['request_id']==parent.request_id and expires==parent.expires_us
                        and parent.issued_us<=issued,'original FILLED differs from grant')
                else:_same(expires,min(issued+limits.offer_budget_us,geometry))
                info=FilledInfo(key,length,p['descriptor_sha256'],issued)
                record=info if op=='FILLED' else Offer(info)
            else:
                info=parent.record.content if op=='ACQUIRE' else parent.record.offer.content
                _need(key==info.key and p['payload_length']==info.payload_length and type(p['payload_length']) is int
                    and p['descriptor_sha256']==info.descriptor_sha256,'original read content changed')
                if op=='ACQUIRE':
                    _same(p['read_token'],None)
                    _need(h['request_id']!=parent.request_id and parent.issued_us<=issued<parent.expires_us
                        and expires<=parent.expires_us and expires-issued<=limits.read_budget_us,'original read budget/order changed')
                    record=ReadRequest(parent.record,context.reader,LeaseRequest(h['request_id'],issued,expires,cap_us),
                        p['purpose'],publication)
                else:
                    _need(h['request_id']==parent.request_id and expires==parent.expires_us
                        and parent.issued_us<=issued<parent.parent.expires_us,'original acquired read changed')
                    record=ReadLease(ReadKey(key,context.reader,p['read_token']),parent.record,issued)
        for name,value in dict(packet=packet,context=context,parent=parent,limits=limits,record=record,op=op,
                request_id=h['request_id'],issued_us=issued,expires_us=expires,cap_us=local_cap).items():object.__setattr__(self,name,value)

    @property
    def raw(self):return self.packet.raw_bytes
    @property
    def fields(self):return self.packet.fields
    @property
    def payload(self):return self.packet.payload


def pc_filled_payload(grant,info):
    _need(type(grant) is PcSlotMessage and grant.op=='GRANT' and type(info) is FilledInfo and info.key==grant.record.key,'original PC grant/FILLED required')
    value=grant.payload;value.update(op='FILLED',payload_length=info.payload_length,descriptor_sha256=info.descriptor_sha256)
    return value


def pc_acquire_payload(offer):
    _need(type(offer) is PcSlotMessage and offer.op=='OFFER','original PC OFFER required')
    value=offer.payload;value['op']='ACQUIRE';return value


def expected_pc_slot(offer):
    _need(type(offer) is PcSlotMessage and offer.op=='OFFER','original PC OFFER required')
    p=offer.payload;info=offer.record.content
    return ExpectedPcSlot(PcSlotContext(info.key,_writer(offer.context).bind.binding),info,p['frame_job_id'],p['phase'],
        _decimal(p['geometry_expires_j_us']),tuple(p['full_key']),p['pc_frame_proof_sha256'])


@dataclass(frozen=True,init=False)
class PcReleaseMessage:
    packet: StructuralPacket
    parent: PcSlotMessage
    record: RemoteReturnStatement
    issued_us: int
    expires_us: int

    def __init__(self,packet,parent,*,limits):
        _need(type(packet) is StructuralPacket and type(parent) is PcSlotMessage and parent.op=='ACQUIRED'
            and type(limits) is CleanupTimeLimits,'original PC ACQUIRED/cleanup limit required')
        h=packet.fields;old=parent.fields
        for name in ('kind','binding_domain','service_instance','channel_epoch','binding_revision','request_id'):_same(h[name],old[name])
        _same(h['sender_instance'],parent.context.reader.worker_instance)
        expected=parent.payload;expected['op']='RELEASE';_same(packet.payload,expected)
        _same(_canonical(packet.payload),_canonical(expected))
        for name,value in expected.items():_same(packet.payload[name],value)
        issued,expires=(_decimal(h[k]) for k in ('issued_j_us','expires_j_us'))
        _same(expires,issued+limits.transport_budget_us)
        record=RemoteReturnStatement(parent.record,issued)
        for name,value in dict(packet=packet,parent=parent,record=record,issued_us=issued,expires_us=expires).items():object.__setattr__(self,name,value)
