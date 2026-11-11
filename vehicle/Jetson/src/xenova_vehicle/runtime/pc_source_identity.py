"""실제 S의 PC source 승인 증거를 모아 SceneNone publication을 한 번 고정한다.

등록 lifecycle/BIND, A의 실제 NN start·원 FILLED, M parser 완료와 D의 원 EMPTY
bootstrap을 소비한다. 최초 cold EMPTY 승인만 지원하며 M 교체에 옛 EMPTY를 재사용하는
복원은 지원하지 않는다. JSON ready나 graph 선언만으로 승인하지 않는다. 승인은 신원
합의이며 clock/pose/현재 frame·FREE/World/추진 권한이 아니다. 물리 source guard는
바꾸지 않는다. EOF/reap/source 교체는 S가 fence로 알려야 하며 자동 재시작은 없다.
"""

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import get_ident

from ..oak.sim_nn_manifest import SimNnManifest, validate_start_receipt
from ..simulation.motion_profile import DevelopmentMotionProfile
from ..simulation.pc_input import PcSourceBinding
from ..storage.contracts import StorageContext, canonical, json_bytes
from ..storage.geometry_scope import PiGeometryOrigin
from .lifecycle_messages import LifecyclePeer, LifecycleReceiver, parse_lifecycle
from .lifecycle_policy import AbsoluteDeadline, REQUIRED_NN_PAIR_COUNT, TIMING_US
from .local_envelope import StructuralPacket
from .pc_binding_messages import PcBindAck
from .raw_binding_messages import POSITIVE_STATES
from .raw_leases import Endpoint
from .pc_channel import PcActorLink, PcActorReceived
from .pc_job_messages import PcParserComplete
from .pc_slot_messages import PcFilledMetadata
from .resident_storage import (StorageProfile, parse_bootstrap_result, parse_storage_bind_ack,
    storage_binding)
from .resident_transport import SentPacket
from .source_identity import SourcePolicy, _label, _sample, _sha, _uuid
from .storage_messages import publication_binding


MAX_PENDING_NN = 8
MAX_RECENT_JOBS = 16
NN_STALL_US = 5_000_000
IDENTITY_VEHICLE_DELIVERY_US = 1_000_000


class PcSourceNotReady(RuntimeError):
    """원 승인 사건이 없거나 현재 등록/source가 철회됐다. 새 사실/E로 보완하지 않는다."""


def _need(value, message):
    if not value:
        raise ValueError(message)


def _time(value):
    _need(type(value) is int and 0 <= value < 2**64, 'exact original J uint64 required')
    return value


def _origin(sample):
    return dict(pi_host_boot=sample.host_boot, gateway_instance=sample.gateway_instance,
        device_session=sample.device_session, pi_session=sample.pi_session)


def _seal(cls, **fields):
    result = object.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(result, name, value)
    return result


@dataclass(frozen=True, slots=True, init=False)
class _BindEvidence:
    ack: PcBindAck
    sent: SentPacket
    received_j_us: int

    def __init__(self, *args, **kwargs): raise TypeError('actual S BIND observation required')


@dataclass(frozen=True, slots=True, init=False)
class _StorageEvidence:
    discover_sent: SentPacket
    result: StructuralPacket
    result_received_j_us: int
    bind_sent: SentPacket
    ack: StructuralPacket
    ack_received_j_us: int
    material_bytes: bytes

    def __init__(self, *args, **kwargs): raise TypeError('actual S D bootstrap observation required')


@dataclass(frozen=True, slots=True, init=False)
class _NnEvidence:
    filled: PcFilledMetadata
    observed_j_us: int

    def __init__(self, *args, **kwargs): raise TypeError('current broker FILLED observation required')


@dataclass(frozen=True, slots=True, init=False)
class _ParseEvidence:
    original: _NnEvidence
    complete: PcParserComplete
    received: PcActorReceived

    def __init__(self, *args, **kwargs): raise TypeError('matched actual M parser observation required')


@dataclass(frozen=True, slots=True, init=False)
class PcSourceAdmission:
    """원 source 승인 기록이다. 현재 사용권은 owner와 실제 frame consumer가 재검산한다.

    publication의 기존 raw_binding_sha256는 이 명시 variant에서만 PC descriptor SHA다.
    이 객체의 생성/SceneNone은 metric/READY·LIVE·clock 승인을 만들지 않는다.
    """

    binding: PcSourceBinding
    publication_bytes: bytes
    source_bytes: bytes
    origin_bytes: bytes
    storage_context: StorageContext
    storage_material_bytes: bytes
    vehicle_wire: bytes
    vehicle_received_j_us: int
    admitted_j_us: int
    warmup: tuple
    _token: object

    def __init__(self, *args, **kwargs): raise TypeError('actual PcSourceIdentityOwner admission required')

    @property
    def publication(self): return json.loads(self.publication_bytes)

    @property
    def publication_sha256(self): return sha256(self.publication_bytes).hexdigest()

    @property
    def source(self): return json.loads(self.source_bytes)

    @property
    def origin(self): return json.loads(self.origin_bytes)

    @property
    def context_sha256(self): return self.storage_context.sha256

    @property
    def material(self):
        value = json.loads(self.storage_material_bytes)
        return value['binding'], value['bootstrap']

    def context_bytes(self, *, vehicles=(), probes=None, active_probe_sha256=None):
        """기존 v1 snapshot의 작은 사본이다. 원 입력 행의 수락은 SensorState가 담당한다.

        caller는 실제 callback 원문/received J를 넘긴다. 시각·raw·probe를 만들거나
        retime하지 않는다. 아직 없는 Scene/ToF는 명시 비어 있는 값이다.
        """
        _need(type(vehicles) is tuple and len(vehicles) <= 24 and (probes is None or type(probes) is dict)
            and (probes is None or len(probes) <= 8), 'bounded original sensor snapshot rows required')
        if active_probe_sha256 is not None: _sha(active_probe_sha256)
        value = dict(version=1, publication=self.publication, source=self.source, origin=self.origin,
            context_sha256=self.context_sha256, active_probe_sha256=active_probe_sha256,
            storage_context=self.storage_context.fields, vehicles=list(vehicles),
            probes={} if probes is None else probes, tof={}, scene=None)
        raw = canonical(value)
        _need(len(raw) <= 65536, 'original source snapshot exceeds64KiB')
        return raw


class PcSourceIdentityOwner:
    """S-main의 등록 사건 owner다. 같은 source의 재호출은 원 승인/ID를 그대로 돌려준다.

    lifecycle receivers와 links는 S가 실제 spawn/등록한 원 객체다. 이 모듈은 OS의
    생존 여부를 추측하지 않는다. S는 EOF/reap/교체/STOP 경계에서 fence를 호출한다.
    parser_deadline/startup_deadline_us는 실제 boot/phase의 원본이며 여기서 만들지 않는다.
    parser_deadline=None이면 원 A start와 M ACK 뒤 install_parser_deadline으로 한 번 고정한다.
    """

    def __init__(self, acquisition_link, mapper_link, binding, policy, motion_profile, manifest, raw_broker, *,
                 acquisition_receiver, mapper_receiver, storage_receiver, storage_peer, storage_profile,
                 mapper_instance, mapper_boot, device, map_epoch, frame_epoch,
                 startup_deadline_us, parser_deadline=None, revision_floor=0):
        from .pc_raw_broker import PcRawBroker
        _need(type(acquisition_link) is PcActorLink and type(mapper_link) is PcActorLink
            and acquisition_link.owner_role == mapper_link.owner_role == 'S'
            and acquisition_link.peer.role == 'A' and mapper_link.peer.role == 'M'
            and type(binding) is PcSourceBinding and type(policy) is SourcePolicy
            and type(motion_profile) is DevelopmentMotionProfile and type(manifest) is SimNnManifest
            and type(raw_broker) is PcRawBroker and type(storage_peer) is LifecyclePeer and storage_peer.role == 'D'
            and type(storage_profile) is StorageProfile, 'actual registered PC A/M/D owners and profiles required')
        for value in (mapper_instance, mapper_boot, map_epoch, frame_epoch): _uuid(value)
        _label(device)
        _time(startup_deadline_us)
        _need(startup_deadline_us > 0, 'original positive startup deadline required')
        _need(parser_deadline is None or type(parser_deadline) is AbsoluteDeadline
            and parser_deadline.started_us < parser_deadline.expires_us <= startup_deadline_us
            and parser_deadline.expires_us - parser_deadline.started_us <= TIMING_US['warmup'],
            'original bounded parser/boot deadlines required')
        _need(type(revision_floor) is int and 0 <= revision_floor < 2**64-1, 'original publication revision floor required')
        binding.check_profile(motion_profile)
        policy.check_profile(motion_profile.bound_calibration)
        identity = motion_profile.fields['identity']
        _need((policy.vehicle, policy.configuration, policy.calibration) ==
            (identity['vehicle'], identity['configuration'], motion_profile.sha256), 'PC source policy differs from motion identity')
        _need(manifest._asset_receipt is not None and manifest._sdk_metadata is not None
            and manifest.sha256 == binding.fields['nn_manifest_sha256'], 'S from_current original NN asset closure required')
        declared = manifest.fields['sim_profile']
        _need((declared['source_id'], declared['clock_id'], declared['calibration_id']) ==
            (binding.source_id, binding.clock_id, binding.fields['calibration_profile_sha256']), 'PC NN manifest source differs')
        service = binding.fields['service_instance']
        peers = (acquisition_link.peer, mapper_link.peer, storage_peer)
        _need(all(peer.service_instance == service for peer in peers)
            and len({peer.worker_instance for peer in peers}) == 3
            and len({peer.channel_epoch for peer in peers}) == 3
            and all(link.binding.canonical_bytes == binding.canonical_bytes for link in (acquisition_link, mapper_link)),
            'PC A/M/D service, independent worker/channel or descriptor differs')
        _need(raw_broker.writer.bind.context.peer == acquisition_link.peer
            and raw_broker.writer.bind.binding.canonical_bytes == binding.canonical_bytes
            and raw_broker.reader == Endpoint(mapper_link.peer.channel_epoch, mapper_link.peer.worker_instance),
            'actual PC broker differs from registered A/M writer/reader')
        receivers = (acquisition_receiver, mapper_receiver, storage_receiver)
        for receiver, peer in zip(receivers, peers):
            _need(type(receiver) is LifecycleReceiver and receiver.snapshot.peer == peer
                and receiver.snapshot.receiver_role == 'S', 'actual S registered lifecycle receiver required')
        self._thread = get_ident()
        self._links = dict(A=acquisition_link, M=mapper_link)
        self._receivers = dict(zip(('A', 'M', 'D'), receivers))
        self._peers = dict(zip(('A', 'M', 'D'), peers))
        self._binding, self._policy, self._motion, self._manifest, self._broker = binding, policy, motion_profile, manifest, raw_broker
        self._storage_profile = storage_profile
        self._mapper_instance, self._mapper_boot, self._device = mapper_instance, mapper_boot, device
        self._map_epoch, self._frame_epoch = map_epoch, frame_epoch
        self._startup_deadline, self._parser_deadline, self._revision = startup_deadline_us, parser_deadline, revision_floor+1
        self._binds = {}
        self._storage = self._start = self._start_received = self._admission = None
        self._pending = OrderedDict()
        self._expired_pending_nn = 0
        self._completed = OrderedDict()
        self._warmup = []
        self._warmup_originals = None
        self._last_key = self._last_completed = None
        self._last_now = None
        self._fenced = None
        self._token = object()

    def _owner(self):
        if get_ident() != self._thread:
            raise RuntimeError('PC source identity belongs to original S-main owner')

    def _now(self, now):
        self._owner(); _time(now)
        _need(self._last_now is None or self._last_now <= now, 'PC source J time regressed')
        self._last_now = now
        if self._fenced is not None: raise PcSourceNotReady(self._fenced)

    def _registered(self, role, now):
        snapshot = self._receivers[role].snapshot
        if (snapshot.peer != self._peers[role] or not snapshot.hello_seen or snapshot.last_packet is None
                or snapshot.pending_stop is not None or snapshot.stopped_report is not None):
            raise PcSourceNotReady('PC_'+role+'_LIFECYCLE_NOT_CURRENT')
        message = parse_lifecycle(snapshot.last_packet, self._peers[role], 'S')
        if not message.issued_us <= now < message.expires_us or message.kind not in ('HELLO', 'HEARTBEAT'):
            raise PcSourceNotReady('PC_'+role+'_LIFECYCLE_NOT_CURRENT')
        active = POSITIVE_STATES[role] if role in ('A', 'M') else ('STARTING', 'IDLE', 'ACTIVE_JOB', 'RECOVERING')
        if message.kind == 'HEARTBEAT' and message.payload['worker_state'] not in active:
            raise PcSourceNotReady('PC_'+role+'_LIFECYCLE_NOT_CURRENT')
        if role != 'D' and self._links[role].closed:
            raise PcSourceNotReady('PC_'+role+'_CHANNEL_CLOSED')

    def _received(self, value, role, now, *, current=True):
        _need(type(value) is PcActorReceived, 'actual PC actor receive fact required')
        self._links[role].require_received(value)
        _need(value.received_j_us <= now and (not current or now < value.packet.expires_us),
            'original PC actor event expired')
        self._registered(role, now)
        return value.packet

    @staticmethod
    def _sent(value, received):
        _need(type(value) is SentPacket and type(value.packet) is StructuralPacket
            and type(value.started_us) is int and type(value.completed_us) is int
            and int(value.packet.fields['issued_j_us']) <= value.started_us <= value.completed_us <= received
            < int(value.packet.fields['expires_j_us']), 'original actual control full-send/receive bounds required')

    @property
    def admission(self):
        """원 승인 기록이다. current frame 권한이나 암묵 clock 갱신은 아니다."""
        self._owner()
        return None if self._fenced is not None else self._admission

    @property
    def binding(self): return self._binding

    @property
    def parser_deadline(self):
        self._owner()
        return self._parser_deadline

    @property
    def warmup_count(self):
        self._owner()
        return REQUIRED_NN_PAIR_COUNT if self._warmup_originals is not None else len(self._warmup)

    @property
    def expired_pending_nn(self):
        """원 E를 넘겨 버린 미완료 NN 작업 수다. parser 성공/READY 수가 아니다."""
        self._owner()
        return self._expired_pending_nn

    def _prune_pending(self, now):
        self._owner(); _time(now)
        expired = tuple(job for job, fact in self._pending.items() if now >= fact.filled.expires_j_us)
        for job in expired:
            del self._pending[job]
        self._expired_pending_nn = min(2**64-1, self._expired_pending_nn+len(expired))
        return len(expired)

    def fence(self, reason):
        self._owner()
        _label(reason)
        if self._fenced is None: self._fenced = reason

    def _phase(self, deadline, *, start=None, mapper_ack=None):
        if start is None: start = self._start_received
        if mapper_ack is None:
            fact = self._binds.get('M')
            mapper_ack = None if fact is None else fact.ack
        if start is None or mapper_ack is None:
            raise PcSourceNotReady('PC_ORIGINAL_START_AND_M_BIND_REQUIRED')
        anchor = max(start.packet.issued_us, mapper_ack.issued_us)
        _need(type(deadline) is AbsoluteDeadline and deadline.started_us == anchor
            and anchor < deadline.expires_us <= min(anchor + TIMING_US['warmup'], self._startup_deadline),
            'parser phase must retain original A start/M ACK anchor and boot cap')

    def install_parser_deadline(self, original_deadline):
        """A cold start와 M ACK 뒤 원 phase timer를 한 번만 받는다. 새 E를 계산하지 않는다."""
        self._owner()
        if self._fenced is not None: raise PcSourceNotReady(self._fenced)
        _need(self._parser_deadline is None, 'original parser deadline is already installed')
        self._phase(original_deadline)
        _need(self._last_now is not None and self._last_now < original_deadline.expires_us,
            'original parser deadline expired before installation')
        self._parser_deadline = original_deadline

    def observe_bind(self, role, ack, sent, *, received_j_us, now_us):
        self._now(now_us)
        _need(_time(received_j_us) <= now_us, 'future original BIND receive fact')
        _need(role in ('A', 'M') and type(ack) is PcBindAck and ack.accepted is True,
            'positive original PC BIND ACK required')
        self._registered(role, now_us)
        self._sent(sent, received_j_us)
        _need(ack.context.peer == self._peers[role] and sent.packet is ack.original_bind.packet
            and ack.binding.canonical_bytes == self._binding.canonical_bytes
            and ack.context.scope == self._broker.writer.scope
            and ack.context.arena_spec == self._broker.writer.bind.context.arena_spec
            and sent.started_us <= ack.issued_us <= received_j_us < ack.expires_us,
            'PC ACK differs from actual registered BIND/send/receive')
        other = self._binds.get('M' if role == 'A' else 'A')
        _need(other is None or other.ack.binding_revision == ack.binding_revision, 'A/M PC BIND revisions differ')
        if role == 'M' and self._start_received is not None and self._parser_deadline is not None:
            self._phase(self._parser_deadline, mapper_ack=ack)
        original = self._binds.get(role)
        if original is not None:
            _need(original.ack.raw == ack.raw and original.sent is sent
                and original.received_j_us == received_j_us, 'one original PC BIND fact cannot be replaced')
            return False
        self._binds[role] = _seal(_BindEvidence, ack=ack, sent=sent, received_j_us=received_j_us)
        return True

    def observe_storage(self, discover_sent, result, bind_sent, ack, *, result_received_j_us, ack_received_j_us, now_us):
        self._now(now_us)
        _need(_time(result_received_j_us) <= _time(ack_received_j_us) <= now_us,
            'future or reversed original D receive facts')
        self._registered('D', now_us)
        self._sent(discover_sent, result_received_j_us); self._sent(bind_sent, ack_received_j_us)
        body = parse_bootstrap_result(result, self._peers['D'], self._storage_profile,
            discover_sent.packet, now_us=result_received_j_us)
        if body['status'] != 'EMPTY':
            raise PcSourceNotReady('PC_COMPLETE_RESTORE_NOT_READY' if body['status'] == 'COMPLETE' else 'PC_D_BOOTSTRAP_NOT_READY')
        declared = storage_binding(self._peers['D'], self._storage_profile, body)
        parse_storage_bind_ack(ack, self._peers['D'], bind_sent.packet, now_us=ack_received_j_us)
        _need(bind_sent.packet.payload['binding'] == declared
            and result_received_j_us <= bind_sent.started_us <= ack_received_j_us,
            'D BIND does not follow the original actual EMPTY discovery')
        material = canonical(dict(binding=declared, bootstrap=body))
        _need(len(material) <= 2048, 'actual D material exceeds2048 bytes')
        if self._storage is not None:
            old = self._storage
            _need(old.discover_sent is discover_sent and old.result is result and old.bind_sent is bind_sent
                and old.ack is ack and (old.result_received_j_us, old.ack_received_j_us)
                == (result_received_j_us, ack_received_j_us), 'original D bootstrap cannot be replaced or retimed')
            return False
        self._storage = _seal(_StorageEvidence, discover_sent=discover_sent, result=result,
            result_received_j_us=result_received_j_us, bind_sent=bind_sent, ack=ack,
            ack_received_j_us=ack_received_j_us, material_bytes=material)
        return True

    def observe_nn_start(self, received, *, now_us):
        self._now(now_us)
        # Graph start는 원 E 안에 실제 수신한 역사 사건이다. 늦은 M 설치가 그 E를 갱신하지 않는다.
        packet = self._received(received, 'A', now_us, current=False)
        if 'A' not in self._binds: raise PcSourceNotReady('PC_A_BIND_REQUIRED')
        _need(packet.kind == 'PC_NN_START' and packet.metadata == dict(nn_manifest_sha256=self._manifest.sha256)
            and self._binds['A'].ack.issued_us <= packet.issued_us < self._startup_deadline,
            'original A NN start differs from its BIND/manifest/boot cap')
        if self._start_received is not None:
            _need(self._start_received is received, 'actual original A model start cannot be replaced')
            return False
        receipt = json_bytes(packet.body, 32768)
        checked = validate_start_receipt(self._manifest, receipt, context_key=self._manifest.sha256,
            startup_deadline_us=self._startup_deadline)
        _need(packet.issued_us < int(checked.fields['startup_deadline_us']), 'actual NN start missed original cap')
        if 'M' in self._binds and self._parser_deadline is not None:
            self._phase(self._parser_deadline, start=received)
        self._start, self._start_received = checked, received
        return True

    def observe_nn_result(self, filled, *, now_us):
        self._now(now_us)
        _need(type(filled) is PcFilledMetadata and filled.phase == 'NN', 'actual bound A NN FILLED metadata required')
        if self._start is None: raise PcSourceNotReady('PC_NN_START_REQUIRED')
        self._prune_pending(now_us)
        old = self._pending.get(filled.job_id)
        if old is not None:
            _need(old.filled is filled, 'original NN job cannot replace its FILLED evidence')
            return False
        if filled.job_id in self._completed:
            raise ValueError('completed NN job cannot be reissued')
        if len(self._pending) >= MAX_PENDING_NN: raise PcSourceNotReady('PC_NN_PENDING_FULL')
        _need(self._broker.require_filled(filled, now_us=now_us) is filled, 'original broker FILLED identity changed')
        self._received(filled.received, 'A', now_us)
        _need(filled.received.packet.kind == 'PC_NN_RESULT_META' and filled.nn_key is not None
            and filled.nn_key[6] == self._manifest.sha256, 'NN result lost original actual A manifest key')
        # 원 current FILLED를 지금 수락한 역사 사실이다. slot 반환 뒤 current로 다시 주장하지 않는다.
        self._pending[filled.job_id] = _seal(_NnEvidence, filled=filled, observed_j_us=now_us)
        return True

    def observe_parse_complete(self, received, *, now_us):
        self._now(now_us)
        packet = self._received(received, 'M', now_us)
        if set(self._binds) != {'A', 'M'} or self._start is None:
            raise PcSourceNotReady('PC_BIND_OR_NN_START_REQUIRED')
        complete = PcParserComplete(packet, self._binding, self._manifest)
        old = self._completed.get(complete.job_id)
        if old is not None:
            _need(old == sha256(packet.raw).hexdigest(), 'same parser job changed original bytes')
            return False
        original = self._pending.get(complete.job_id)
        if original is None: raise PcSourceNotReady('PC_ORIGINAL_NN_FILLED_REQUIRED')
        filled = original.filled
        complete.validate_original(job_id=filled.job_id, expires_us=filled.expires_j_us,
            full_key=filled.full_key, nn_key=filled.nn_key, pc_frame_proof_sha256=filled.proof_sha256,
            slot_descriptor_sha256=filled.descriptor_sha256, purpose=filled.purpose, now_us=now_us)
        _need(filled.original_filled_info.completed_us <= complete.completed_j_us <= received.received_j_us
            and self._binds['M'].ack.issued_us <= complete.completed_j_us,
            'parser completion precedes original actual NN FILLED/M BIND')
        key = complete.nn_key
        if self._last_key is not None:
            _need(key[:3] == self._last_key[:3] and all(key[index] > self._last_key[index] for index in (3, 4, 5))
                and key[6] == self._last_key[6] and self._last_completed <= complete.completed_j_us,
                'actual parser input/key/completion did not advance')
        fact = _seal(_ParseEvidence, original=original, complete=complete, received=received)
        if self._warmup_originals is None:
            if self._parser_deadline is None: raise PcSourceNotReady('PC_ORIGINAL_PARSER_DEADLINE_REQUIRED')
            self._phase(self._parser_deadline)
            _need(self._parser_deadline.started_us <= complete.completed_j_us < self._parser_deadline.expires_us
                and now_us < self._parser_deadline.expires_us, 'initial parser missed its original warmup deadline')
            self._warmup.append(fact)
            if len(self._warmup) == REQUIRED_NN_PAIR_COUNT:
                self._warmup_originals = tuple(self._warmup)
                self._warmup.clear()
        self._last_key, self._last_completed = key, complete.completed_j_us
        del self._pending[complete.job_id]
        self._completed[complete.job_id] = sha256(packet.raw).hexdigest()
        while len(self._completed) > MAX_RECENT_JOBS: self._completed.popitem(last=False)
        return True

    def admit(self, vehicle_wire, *, vehicle_received_j_us, current_vehicle_wire, now_us):
        self._now(now_us)
        for role in ('A', 'M', 'D'): self._registered(role, now_us)
        if set(self._binds) != {'A', 'M'} or self._start is None or self._storage is None:
            raise PcSourceNotReady('PC_ORIGINAL_BIND_NN_START_D_REQUIRED')
        if self._warmup_originals is None or self._last_completed is None or now_us-self._last_completed >= NN_STALL_US:
            raise PcSourceNotReady('PC_ACTUAL_PARSER_WARMUP_OR_PROGRESS_REQUIRED')
        _time(vehicle_received_j_us)
        _need(0 <= now_us-vehicle_received_j_us < IDENTITY_VEHICLE_DELIVERY_US,
            'original Vehicle delivery is no longer current for identity')
        original, current = _sample(vehicle_wire), _sample(current_vehicle_wire)
        identity = ('vehicle', 'host_boot', 'gateway_instance', 'device_session', 'pi_session', 'odom_epoch', 'calibration')
        _need(all(getattr(original, key) == getattr(current, key) for key in identity)
            and (original.vehicle, original.calibration) == (self._policy.vehicle, self._policy.calibration),
            'Vehicle changed actual Pi/device/odom or approved whole motion bundle')
        if self._admission is not None:
            old_source = self._admission.source
            _need(self._admission.origin == _origin(current) and old_source['odom_epoch'] == current.odom_epoch,
                'admitted PC source changed; explicit fence/new owner required')
            return self._admission
        if now_us >= self._startup_deadline: raise PcSourceNotReady('PC_ORIGINAL_STARTUP_EXPIRED')
        material = json.loads(self._storage.material_bytes)
        publication = dict(service_instance=self._binding.fields['service_instance'],
            mapper_worker=self._peers['M'].worker_instance, mapper_instance=self._mapper_instance,
            raw_binding_sha256=self._binding.sha256, map_epoch=self._map_epoch, frame_epoch=self._frame_epoch,
            odom_epoch=original.odom_epoch, configuration=self._policy.configuration, calibration=self._policy.calibration,
            primitive=self._policy.primitive, scene_admission_ref=None, source_binding_revision=str(self._revision))
        publication_binding(publication)
        source = dict(vehicle=self._policy.vehicle, device=self._device, mapper_boot=self._mapper_boot,
            mapper_instance=self._mapper_instance, clock_epoch=self._binding.fields['destination_clock_epoch'],
            map_epoch=self._map_epoch, frame_epoch=self._frame_epoch, odom_epoch=original.odom_epoch,
            configuration=self._policy.configuration, calibration=self._policy.calibration, primitive=self._policy.primitive)
        origin = _origin(original)
        context = StorageContext(material['bootstrap']['database_uuid'], self._peers['M'].worker_instance,
            self._mapper_instance, self._map_epoch, self._frame_epoch, original.odom_epoch, original.host_boot,
            self._policy.configuration, self._policy.calibration, self._policy.primitive, canonical(publication),
            geometry_origin=PiGeometryOrigin(original.vehicle, original.host_boot, original.gateway_instance,
                original.device_session, original.pi_session, original.odom_epoch, original.calibration).fields)
        self._admission = _seal(PcSourceAdmission, binding=self._binding, publication_bytes=canonical(publication),
            source_bytes=canonical(source), origin_bytes=canonical(origin), storage_context=context,
            storage_material_bytes=self._storage.material_bytes, vehicle_wire=vehicle_wire,
            vehicle_received_j_us=vehicle_received_j_us, admitted_j_us=now_us,
            warmup=self._warmup_originals, _token=self._token)
        return self._admission
