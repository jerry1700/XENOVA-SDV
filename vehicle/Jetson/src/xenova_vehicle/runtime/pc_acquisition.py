"""A main의 PC 관측 active1/latest1와 실제 HOST_SIM enqueue/result를 연결한다.

수신 pump·S control·SHM grant의 실행 loop는 caller가 소유한다. 원 EARLY 전달과
NN 후보 계산을 병행하며 실제 M snapshot ACK 전에는 NN 결과를 게시하지 않는다.
source/모드 변화는 모델을 닫지 않으며 원 NN ticket·frame E를 재발급하지 않는다.
M은 YOLOP 해석을 담당한다.
"""

from dataclasses import dataclass
from collections import OrderedDict
from hashlib import sha256
import threading
from uuid import uuid4

from ..oak.sim_nn import SimNnResult
from ..simulation.pc_input import PcFrameInput
from ..storage.contracts import canonical, json_bytes
from .lifecycle_messages import LifecyclePeer
from .pc_channel import PcActorLink, PcActorSent, decode_forward
from .pc_job_messages import PcSnapshotAck, PcParserComplete
from .pc_slot import PcSlotContext, PcSlotEncoding
from .pc_slot_messages import pc_slot_meta_fields
from .pc_workers import PcWorkerActor
from .raw_leases import WriteGrant
from .resident_transport import SentPacket, j_us
from .storage_messages import publication_binding
from .health_policy import FrameExpired


def _need(value, reason):
    if not value: raise ValueError(reason)


@dataclass(frozen=True, slots=True)
class PcAcquisitionJob:
    """관측을 active로 옮길 때 한 번 만든 frame job이다. copy 거래 ID와 구분한다."""
    job_id: str
    frame: PcFrameInput
    purpose: str
    proof_sha256: str

    @property
    def full_key(self): return self.frame.observation.rgb.stamp.key

    @property
    def expires_us(self): return self.frame.expires_j_us


class PcAcquisitionDriver:
    """실제 A actor와 같은 thread에서만 호출한다. 잠금/새 thread/Device를 만들지 않는다.

    pc_owner는 실제 수신 pump가 소유한 MappedPcOwner 원객체다. typed input issuer와
    owner_token을 함께 확인하며 원 owner의 current_view를 매 사용 전에 다시 읽는다.
    SHM/metadata 전송 완료와 parser 완료는 각각 다른 실제 포트로 기록한다.
    """

    def __init__(self, actor, link, mapper_peer, pc_owner):
        _need(type(actor) is PcWorkerActor and actor.boot.peer.role == 'A'
            and actor.model_start is not None and type(link) is PcActorLink
            and link.owner_role == 'A' and link.peer == actor.boot.peer
            and link.binding.canonical_bytes == actor.binding.canonical_bytes
            and type(mapper_peer) is LifecyclePeer and mapper_peer.role == 'M'
            and mapper_peer.service_instance == actor.boot.peer.service_instance
            and mapper_peer.worker_instance != actor.boot.peer.worker_instance
            and mapper_peer.channel_epoch != actor.boot.peer.channel_epoch
            and pc_owner.binding.canonical_bytes == actor.binding.canonical_bytes,
            'actual started A, registered M/link and original PC owner required')
        self._actor, self._link, self._mapper_peer, self._owner = actor, link, mapper_peer, pc_owner
        self._thread = threading.get_ident()
        self._latest = self._active = self._submission = self._result = None
        self._encoding = self._filled = self._filled_sent = self._meta_sent = None
        self._ack = None
        self._publication = None
        self._phase = 'IDLE'
        self._fenced = False
        self._retired = OrderedDict()
        self._parse_raw = None
        self.last_enqueue_timing = None
        self.counts = dict(offered=0, replaced=0, expired=0, enqueued=0, completed=0, parsed=0, late=0)

    def _owned(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('PC acquisition belongs to the original A main thread')

    def _open(self):
        self._owned()
        if self._fenced or self.actor.stop is not None:
            raise RuntimeError('original PC acquisition source is fenced or stopping')

    @property
    def active(self): return self._active

    @property
    def actor(self): return self._actor

    @property
    def link(self): return self._link

    @property
    def mapper_peer(self): return self._mapper_peer

    @property
    def owner(self): return self._owner

    @property
    def phase(self): return self._phase

    @property
    def result(self): return self._result

    @property
    def waiting_enqueue(self):
        return self._active is not None and self._phase in ('EARLY','WAIT_SNAPSHOT') and self._submission is None

    def offer(self, frame, owner):
        """수신 callback의 latest1 교환이다. 영상 복사/SDK/대기/모델 호출은 없다."""
        self._open()
        _need(type(frame) is PcFrameInput and owner is self.owner
            and frame.binding.canonical_bytes == self.actor.binding.canonical_bytes
            and frame.observation.nn is None, 'original unprocessed frame from this actual PC receiver required')
        self.counts['offered'] += 1
        if self._latest is not None: self.counts['replaced'] += 1
        self._latest = frame
        return True

    def _current(self, frame, now):
        self._open()
        if now + 1 >= frame.expires_j_us:
            raise FrameExpired('PC frame expired before current-view use')
        view = self.owner.current_view(frame, now_j_us=now)
        frame.validate_current(view, now, binding=self.actor.binding, owner_token=self.owner.owner_token)
        return view

    def source_state(self, received):
        """실제 S publication 전달만 채택한다. 이 사건은 metric snapshot/World 권한이 아니다."""
        self._open()
        self.link.require_received(received)
        packet = received.packet
        meta = packet.metadata
        now = j_us()
        _need(packet.kind == 'PC_SOURCE_STATE' and packet.header['sender'] == 'S'
            and received.received_j_us <= now
            and set(meta) == {'profile', 'state', 'publication_sha256'}
            and meta['profile'] == 'pc-source-state-1' and meta['state'] in ('WARMUP', 'BOUND'),
            'current exact S PC source-state delivery required')
        if meta['state'] == 'WARMUP':
            _need(packet.body == b'' and meta['publication_sha256'] is None and self._publication is None,
                'a bound source requires an explicit fence/recovery rather than warmup reset')
            return
        value = publication_binding(json_bytes(packet.body, 2048))
        _need(canonical(value) == packet.body and meta['publication_sha256'] == sha256(packet.body).hexdigest()
            and value['service_instance'] == self.actor.boot.peer.service_instance
            and value['mapper_worker'] == self.mapper_peer.worker_instance
            and value['raw_binding_sha256'] == self.actor.binding.sha256
            and value['scene_admission_ref'] is None
            and (self._publication is None or self._publication == packet.body),
            'S source-state differs from original PC descriptor/registered M/publication')
        self._publication = packet.body

    def promote(self):
        """비어 있는 active로 최신 관측을 옮긴다. 원 frame E는 유지한다."""
        self._open()
        if self._active is not None or self._latest is None: return None
        frame, self._latest = self._latest, None
        now = j_us()
        if now + 1 >= frame.expires_j_us:
            self.counts['expired'] += 1
            return None
        try:self._current(frame, now)
        except FrameExpired:
            self.counts['expired'] += 1
            return None
        frame.validate_original()
        purpose = 'PARSER_WARMUP' if self._publication is None else 'METRIC_MAP'
        self._active = PcAcquisitionJob(str(uuid4()), frame, purpose, sha256(frame.proof_bytes).hexdigest())
        self._phase = 'EARLY'
        return self._active

    def publish(self, grant):
        """원 GRANT의 writable 취득·copy·ready post를 실제 수행한다. 송신 사실은 별도다."""
        self._open()
        job = self._active
        _need(job is not None and type(grant) is WriteGrant and self._phase in ('EARLY', 'NN')
            and self._encoding is None and grant.key.scope == self.actor.boot.raw_scope,
            'one current original PC write grant and active phase required')
        self._current(job.frame, j_us())
        frame = job.frame; original = frame.observation
        encoding = PcSlotEncoding(PcSlotContext(grant.key, self.actor.binding), phase=self._phase,
            job_id=job.job_id, expires_j_us=job.expires_us, full_key=job.full_key,
            pc_frame_proof=frame.proof_bytes, pc_packet_header=frame.packet_header_bytes,
            rgb=original.rgb.payload, depth=original.depth.payload,
            nn_input=None if self._phase=='EARLY' or self._result is None else self._result.nn_input,
            nn_output=None if self._phase=='EARLY' or self._result is None else self._result.nn_output)
        self.actor.leases_released = False
        filled = self.actor.arena.publish(grant, encoding)
        self.actor.leases_released = True
        self._encoding, self._filled = encoding, filled
        return encoding, filled

    def note_filled_sent(self, sent):
        """원 FILLED packet의 실제 control full-send를 기록한다. queue 수락은 받지 않는다."""
        self._open()
        _need(type(sent) is SentPacket and self._encoding is not None and self._filled_sent is None,
            'actual original FILLED control send required')
        packet = sent.packet; h = packet.fields; p = packet.payload; info = self._filled
        _need(packet.kind == 'SLOT' and p.get('op') == 'FILLED'
            and h['service_instance'] == self.actor.boot.peer.service_instance
            and h['binding_domain'] == 'RAW' and int(h['binding_revision']) == self.actor.bind.binding_revision
            and h['sender_instance'] == self.actor.boot.peer.worker_instance
            and h['channel_epoch'] == self.actor.boot.peer.channel_epoch
            and (p.get('arena_id'), p.get('slot'), int(p.get('generation', '0')), p.get('write_token'))
                == (info.key.scope.arena_id, info.key.slot, info.key.generation, info.key.write_token)
            and p.get('frame_job_id') == self._active.job_id and p.get('phase') == self._phase
            and p.get('full_key') == list(self._active.full_key) and p.get('purpose') == self._active.purpose
            and p.get('pc_frame_proof_sha256') == self._active.proof_sha256
            and int(p.get('geometry_expires_j_us', '0')) == self._active.expires_us
            and p.get('descriptor_sha256') == info.descriptor_sha256
            and p.get('payload_length') == info.payload_length
            and int(h['issued_j_us']) == info.completed_us
            and info.completed_us <= sent.started_us <= sent.completed_us
            , 'FILLED actual send changed original phase/content/lifetime')
        self._filled_sent = sent

    def note_metadata_sent(self, sent):
        """실제 A descriptor metadata full-send 뒤만 M snapshot/parser 응답을 기다린다."""
        self._open()
        _need(type(sent) is PcActorSent and self._filled_sent is not None and self._meta_sent is None,
            'actual FILLED and original PC metadata full-send required')
        self.link.require_sent(sent)
        p = sent.packet; job = self._active
        _need(p.kind == ('PC_EARLY_META' if self._phase == 'EARLY' else 'PC_NN_RESULT_META')
            and p.header['sender'] == 'A' and p.header['worker_instance'] == self.actor.boot.peer.worker_instance
            and p.header['channel_epoch'] == self.actor.boot.peer.channel_epoch
            and p.header['pc_binding_sha256'] == self.actor.binding.sha256
            and p.request_id == job.job_id and p.expires_us == job.expires_us
            and p.metadata == pc_slot_meta_fields(self._encoding, purpose=job.purpose)
            and p.body == self._encoding.descriptor_bytes
            and self._filled.completed_us <= p.issued_us <= sent.started_us <= sent.completed_us,
            'PC metadata actual send differs from original filled descriptor/job/E')
        if sent.completed_us >= job.expires_us:
            self.counts['expired'] += 1
            self._retire()
            return
        self._meta_sent = sent
        self._phase = 'WAIT_SNAPSHOT' if self._phase == 'EARLY' else 'WAIT_PARSE'

    def _late(self, message, now):
        """기존 작업의 응답만 무효과로 종결한다. 최근 기록은 pixel 없이 최대16개다."""
        old = self._retired.get(message.job_id)
        if old is None:
            if now < message.expires_us:
                raise ValueError('response has no original active or retained PC job')
            self.counts['late'] += 1
            return
        values = dict(old['identity'], now_us=now)
        if isinstance(message, PcParserComplete):
            _need(old['nn'] is not None, 'retired frame has no original NN publication')
            values.update(old['nn'])
        message.validate_identity(**values)
        key = 'ack_raw' if isinstance(message, PcSnapshotAck) else 'parse_raw'
        raw = old.get(key)
        _need(raw is None or raw == message.packet.raw, 'retired PC response changed original bytes')
        old[key] = message.packet.raw
        self.counts['late'] += 1

    def snapshot_ack(self, received):
        """원 M snapshot ACK를 확인한 뒤에만 같은 frame의 NN 결과 게시로 진행한다."""
        self._open(); self.link.require_received(received)
        job = self._active
        forward = decode_forward(received.packet, self.mapper_peer, self.actor.binding)
        ack = PcSnapshotAck(forward.original_packet, self.actor.binding)
        now = j_us()
        if job is None or ack.job_id != job.job_id:
            self._late(ack, now)
            return None
        _need(self._phase == 'WAIT_SNAPSHOT' and self._meta_sent is not None,
            'original EARLY actual send must precede snapshot ACK')
        ack.validate_identity(job_id=job.job_id, expires_us=job.expires_us, full_key=job.full_key,
            pc_frame_proof_sha256=job.proof_sha256, purpose=job.purpose, now_us=now)
        _need(self._meta_sent.started_us <= ack.packet.issued_us <= forward.received_j_us
            <= received.received_j_us <= now, 'snapshot ACK relay changed original actual event order')
        _need(self._ack is None or self._ack.packet.raw == ack.packet.raw, 'one original snapshot ACK per frame')
        self._ack = ack
        if self.expire(): return None
        if self._submission is not None:
            self._select_nn_phase()
            return self._submission
        return self.enqueue()

    def _select_nn_phase(self):
        self._encoding = self._filled = self._filled_sent = self._meta_sent = None
        self._phase = 'NN' if self._result is not None else 'NN_PENDING'

    def enqueue(self):
        """NN 후보와 EARLY 전달을 병행한다. busy 때도 같은 job/E이며 제출은 한 번뿐이다."""
        self._open(); job = self._active
        _need(job is not None and self._phase in ('EARLY','WAIT_SNAPSHOT') and self._submission is None,
            'one original active EARLY frame required before NN enqueue')
        try:
            self._current(job.frame, j_us())
            submission = self.actor.nn_source.capture(job.frame.observation,
                captured_j_us=job.frame.observation.capture_interval, expires_j_us=job.expires_us)
        except FrameExpired:
            self.counts['expired'] += 1
            self._retire()
            return None
        if submission is None: return None
        _need(submission.observation is job.frame.observation
            and submission.expires_j_us == job.expires_us
            and job.frame.frame_received_j_us <= submission.clock_sample.j_before_us,
            'actual SDK enqueue changed original received EARLY frame/E')
        self._submission = submission
        self.last_enqueue_timing=dict(captured_j_us=job.frame.observation.capture_interval,
            frame_received_j_us=job.frame.frame_received_j_us,filled_completed_j_us=getattr(self._filled,'completed_us',None),
            filled_sent_j_us=getattr(self._filled_sent,'completed_us',None),metadata_sent_j_us=getattr(self._meta_sent,'completed_us',None),
            snapshot_issued_j_us=None if self._ack is None else self._ack.packet.issued_us,
            enqueued_j_us=submission.clock_sample.j_after_us,
            expires_j_us=job.expires_us)
        if self._ack is not None:self._select_nn_phase()
        self.counts['enqueued'] += 1
        return submission

    def poll_nn(self):
        """실제 A source의 원 ticket과 일치한 owned NN 결과만 다음 slot에 게시한다."""
        self._open()
        for result in self.actor.nn_source.poll():
            _need(type(result) is SimNnResult,'actual typed SDK NN result required')
            if j_us()+1 >= result.submission.expires_j_us:
                self.counts['expired'] += 1
                continue
            _need(type(result) is SimNnResult and self._active is not None and self._phase in ('EARLY','WAIT_SNAPSHOT','NN_PENDING')
                and result.submission is self._submission
                and result.submission.observation is self._active.frame.observation,
                'actual NN result differs from original active SDK submission')
            try:self._current(self._active.frame, j_us())
            except FrameExpired:
                self.counts['expired'] += 1
                self._retire()
                continue
            self._result = result
            if self._ack is not None:self._phase = 'NN'
            self.counts['completed'] += 1
        return self._result

    def parser_complete(self, received, manifest):
        """M 실제 parser 완료 relay를 원 NN slot과 대조한다. Source 승인은 S만 수행한다."""
        self._open(); self.link.require_received(received)
        job = self._active
        forward = decode_forward(received.packet, self.mapper_peer, self.actor.binding)
        report = PcParserComplete(forward.original_packet, self.actor.binding, manifest)
        now = j_us()
        if job is None or report.job_id != job.job_id:
            self._late(report, now)
            return None
        _need(self._phase == 'WAIT_PARSE' and self._meta_sent is not None,
            'actual NN slot and metadata sends required before parser completion')
        report.validate_identity(job_id=job.job_id, expires_us=job.expires_us, full_key=job.full_key,
            nn_key=self._encoding.nn_key, pc_frame_proof_sha256=job.proof_sha256,
            slot_descriptor_sha256=self._encoding.descriptor_sha256, purpose=job.purpose, now_us=now)
        _need(self._filled.completed_us <= report.completed_j_us <= forward.received_j_us
            <= received.received_j_us <= now, 'actual M parse completion changed original NN availability/relay order')
        self._parse_raw=report.packet.raw
        if self.expire(): return None
        self.counts['parsed'] += 1
        self._retire()
        return report

    def _retire(self):
        job = self._active
        if job is not None:
            nn = None if self._encoding is None or self._encoding.nn_key is None else dict(
                nn_key=self._encoding.nn_key, slot_descriptor_sha256=self._encoding.descriptor_sha256)
            self._retired[job.job_id] = dict(identity=dict(job_id=job.job_id, expires_us=job.expires_us,
                full_key=job.full_key, pc_frame_proof_sha256=job.proof_sha256, purpose=job.purpose),
                nn=nn, ack_raw=None if self._ack is None else self._ack.packet.raw,parse_raw=self._parse_raw)
            while len(self._retired) > 16:self._retired.popitem(last=False)
        self._active = self._submission = self._result = self._ack = None
        self._encoding = self._filled = self._filled_sent = self._meta_sent = None
        self._phase = 'IDLE'
        self._parse_raw = None

    def expire(self):
        """copy 거래가 없는 caller 경계에서 원 frame만 버린다. model은 재적재하지 않는다.

        caller는 원 RAW REQUEST/GRANT가 미해결이면 이 포트 대신 해당 원 거래의 실패를
        처리한다. WRITING/token 상태를 여기서 정상화하거나 S에 반환했다고 진술하지 않는다.
        """
        self._open()
        _need(self.actor.leases_released, 'an unresolved actual writer must retain its original failure')
        if self._active is not None and j_us() + 1 >= self._active.expires_us:
            self.counts['expired'] += 1
            self._retire()
            return True
        return False

    def fence(self):
        """입력 owner만 영구 fence한다. 실제 model close와 S reap 사실은 만들지 않는다."""
        self._owned()
        self.owner.fence()
        self._fenced = True; self._latest = None
        self._retire()
