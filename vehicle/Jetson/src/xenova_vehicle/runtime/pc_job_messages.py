"""원 PC job의 EARLY snapshot ACK와 실제 M NN parser 완료를 구분한다.

ACK는 A의 enqueue 전 동기화, parser 완료는 S의 원 NN 결과 상관관계 검사에 쓴다.
둘 다 motion/Source/World READY가 아니다. 현재 peer·실제 full-send/recv·slot 사용권은
actor가 별도로 검산한다. 물리 capture/SDK 시각이나 원 submission을 복원하지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID

from ..oak.sim_nn_manifest import SimNnManifest
from ..perception.yolop import decode_yolop
from ..simulation.pc_input import PcFrameInput, PcSourceBinding
from .mapper_sensor_state import SensorFrameSnapshot
from .pc_channel import PcActorPacket
from .pc_slot import PcOwnedSlot
from .resident_transport import j_us
from .health_policy import FrameExpired


PURPOSES = frozenset(('PARSER_WARMUP', 'METRIC_MAP'))
ACK_FIELDS = frozenset(('profile', 'purpose', 'full_key', 'pc_frame_proof_sha256',
    'snapshot_j_us', 'sensor_fence_revision', 'source_context_sha256'))
PARSE_FIELDS = frozenset(('profile', 'purpose', 'full_key', 'nn_key', 'pc_frame_proof_sha256',
    'slot_descriptor_sha256', 'nn_manifest_sha256', 'parser_manifest_sha256', 'completed_j_us'))


def _need(value, reason):
    if not value: raise ValueError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _uint(value):
    _need(type(value) is int and 0 <= value < 2**63, 'exact nonnegative signed-64 timestamp required')
    return value


def _decimal(value):
    _need(type(value) is str and 1 <= len(value) <= 19 and value.isascii() and value.isdecimal()
        and str(int(value)) == value, 'canonical timestamp decimal required')
    return _uint(int(value))


def _sha(value):
    _need(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value),
        'canonical SHA256 required')
    return value


def _job(value):
    _need(type(value) is str and len(value) == 36, 'original job UUID required')
    parsed = UUID(value)
    _need(str(parsed) == value and parsed.int != 0, 'canonical nonzero job UUID required')


def _key(value, binding):
    _need(type(binding) is PcSourceBinding and type(value) is list and len(value) == 4,
        'exact original PC descriptor/full frame key required')
    _need(value[:3] == [binding.source_id, binding.fields['stream_instances']['rgb_video'], binding.clock_id],
        'PC key differs from original descriptor')
    _uint(value[3])
    return tuple(value)


def _packet(packet, binding, kind, fields):
    _need(type(packet) is PcActorPacket and type(binding) is PcSourceBinding
        and packet.kind == kind and packet.header['sender'] == packet.header['worker_role'] == 'M'
        and packet.header['service_instance'] == binding.fields['service_instance']
        and packet.header['pc_binding_sha256'] == binding.sha256 and packet.body == b'',
        'original M PC packet/descriptor required')
    meta = packet.metadata
    _need(type(meta) is dict and set(meta) == fields and meta['purpose'] in PURPOSES,
        'exact PC job metadata and original purpose required')
    _job(packet.request_id)
    _key(meta['full_key'], binding)
    _sha(meta['pc_frame_proof_sha256'])
    _uint(packet.issued_us); _uint(packet.expires_us)
    return meta


def snapshot_ack_metadata(frame, *, job_id, purpose, snapshot, now_us):
    """M가 원 EARLY frame의 snapshot을 pin한 뒤 호출한다. warmup은 snapshot이 없다."""
    _need(type(frame) is PcFrameInput and purpose in PURPOSES, 'actual original PC EARLY input/purpose required')
    _job(job_id); _uint(now_us)
    frame.validate_original()
    _need(frame.observation.nn is None and frame.frame_received_j_us <= now_us
        and now_us + 1 < frame.expires_j_us, 'original EARLY frame expired or already joined')
    meta = dict(profile='pc-snapshot-ack-1', purpose=purpose, full_key=list(frame.observation.rgb.stamp.key),
        pc_frame_proof_sha256=sha256(frame.proof_bytes).hexdigest(), snapshot_j_us=None,
        sensor_fence_revision=None, source_context_sha256=None)
    if purpose == 'PARSER_WARMUP':
        _need(snapshot is None, 'parser warmup never claims a metric snapshot')
    else:
        _need(type(snapshot) is SensorFrameSnapshot, 'actual EARLY metric snapshot required before NN enqueue')
        snapshot.validate_frame(job_id=job_id, source_generation=frame.binding.sha256,
            source_key=frame.observation.rgb.stamp.key, expires_us=frame.expires_j_us, now_us=now_us,
            context_bytes=snapshot.source_context_bytes)
        inputs = snapshot.metric_inputs
        _need(snapshot.pc_frame_proof == frame.proof_bytes
            and snapshot.pc_binding_bytes == frame.binding.canonical_bytes
            and all(value is not None for value in (inputs.storage_context, inputs.source,
                inputs.clock_lease, inputs.pose, inputs.vehicle)), 'original full-capture metric snapshot is not ready')
        meta.update(snapshot_j_us=str(snapshot.snapshot_j_us), sensor_fence_revision=str(snapshot.fence_revision),
            source_context_sha256=sha256(snapshot.source_context_bytes).hexdigest())
    return meta


@dataclass(frozen=True, slots=True, init=False)
class PcSnapshotAck:
    """M의 원 ACK 진술이다. 실제 M 수신/송신·최신 source 승인은 별도다."""
    packet: PcActorPacket
    purpose: str
    full_key: tuple
    pc_frame_proof_sha256: str
    snapshot_j_us: int | None
    sensor_fence_revision: int | None
    source_context_sha256: str | None

    def __init__(self, packet, binding):
        meta = _packet(packet, binding, 'PC_SNAPSHOT_ACK', ACK_FIELDS)
        _need(meta['profile'] == 'pc-snapshot-ack-1', 'unknown PC snapshot ACK profile')
        stamp, revision, context = (meta[name] for name in
            ('snapshot_j_us', 'sensor_fence_revision', 'source_context_sha256'))
        if meta['purpose'] == 'PARSER_WARMUP':
            _need(stamp is revision is context is None, 'warmup ACK cannot claim a metric snapshot')
        else:
            stamp, revision, context = _decimal(stamp), _decimal(revision), _sha(context)
            _need(stamp <= packet.issued_us < packet.expires_us, 'snapshot must precede original ACK issue/E')
        values = dict(packet=packet, purpose=meta['purpose'], full_key=tuple(meta['full_key']),
            pc_frame_proof_sha256=meta['pc_frame_proof_sha256'], snapshot_j_us=stamp,
            sensor_fence_revision=revision, source_context_sha256=context)
        for name, value in values.items(): object.__setattr__(self, name, value)

    @property
    def job_id(self): return self.packet.request_id

    @property
    def expires_us(self): return self.packet.expires_us

    def validate_original(self, *, job_id, expires_us, full_key, pc_frame_proof_sha256, purpose, now_us):
        self.validate_identity(job_id=job_id, expires_us=expires_us, full_key=full_key,
            pc_frame_proof_sha256=pc_frame_proof_sha256, purpose=purpose, now_us=now_us)
        if now_us + 1 >= expires_us:
            raise FrameExpired('snapshot ACK frame expired')
        return self

    def validate_identity(self, *, job_id, expires_us, full_key, pc_frame_proof_sha256, purpose, now_us):
        _need((self.job_id, self.expires_us, self.full_key, self.pc_frame_proof_sha256, self.purpose)
            == (job_id, expires_us, full_key, pc_frame_proof_sha256, purpose)
            and self.packet.issued_us <= _uint(now_us),
            'snapshot ACK changed original EARLY job/E/key/proof/purpose')
        return self


def parser_manifest_sha256(manifest):
    _need(type(manifest) is SimNnManifest, 'original pinned PC NN manifest required')
    return sha256(_canonical(manifest.fields['parser'])).hexdigest()


def _manifest_binding(manifest, binding):
    _need(type(manifest) is SimNnManifest and type(binding) is PcSourceBinding
        and manifest.sha256 == binding.fields['nn_manifest_sha256'], 'original PC descriptor/NN manifest required')
    profile = manifest.fields['sim_profile']
    _need((profile['source_id'], profile['clock_id'], profile['calibration_id']) ==
        (binding.source_id, binding.clock_id, binding.fields['calibration_profile_sha256']),
        'PC NN manifest source/clock/calibration differs from original descriptor')


def parse_pc_nn(owned, manifest, *, purpose, clock=j_us):
    """실제 owned NN packet을 decode한 뒤 완료 metadata를 만든다. Source READY는 만들지 않는다."""
    _need(type(owned) is PcOwnedSlot and owned.phase == 'NN' and purpose in PURPOSES
        and type(manifest) is SimNnManifest and callable(clock), 'actual owned PC NN slot/purpose/manifest required')
    return _parse_pc_nn_values(owned.binding, owned.nn_input, owned.nn_output, owned.full_key, owned.nn_key,
        sha256(owned.pc_frame_proof).hexdigest(), owned.descriptor_sha256, owned.expires_j_us,
        owned.expected.original_filled_info.completed_us, manifest, purpose, clock)


def parse_pc_nn_admission(admission, manifest, *, clock=j_us):
    """M-main이 실제 ACK/slot/relay로 채택한 원 NN만 compute에서 해석한다."""
    from .pc_frame_admission import PcNnAdmission
    _need(type(admission) is PcNnAdmission and callable(clock), 'actual original M NN admission required')
    frame = admission.frame
    frame.validate_original()
    _need(frame.observation.nn is None and admission.expires_j_us == frame.expires_j_us,
        'NN admission changed original EARLY frame/E')
    return _parse_pc_nn_values(frame.binding, admission.nn_input, admission.nn_output,
        frame.observation.rgb.stamp.key, admission.nn_key, sha256(frame.proof_bytes).hexdigest(),
        admission.slot_descriptor_sha256, frame.expires_j_us, admission.filled_completed_j_us,
        manifest, admission.purpose, clock)


def _parse_pc_nn_values(binding, nn_input, nn_output, full_key, nn_key, proof_sha,
                        descriptor_sha, expires, filled_completed, manifest, purpose, clock):
    _need(purpose in PURPOSES and callable(clock), 'actual original NN purpose/clock required')
    _manifest_binding(manifest, binding)
    began = _uint(clock())
    _need(nn_input.context_key == nn_output.context_key == manifest.sha256
        and nn_key[:4] == full_key and nn_key[4:7] == (nn_output.device_us, nn_output.sdk_us, manifest.sha256)
        and max(nn_input.received_j_us, nn_output.received_j_us, filled_completed) <= began
        and began + 1 < expires, 'NN parser lost original manifest/key/availability/E')
    candidates = decode_yolop(nn_output)
    completed = _uint(clock())
    _need(candidates.packet is nn_output and began <= completed and completed + 1 < expires
        and candidates.config.to_dict() == manifest.fields['parser']['configuration'],
        'actual parser result/configuration/completion differs from original NN job')
    meta = dict(profile='pc-parser-complete-1', purpose=purpose, full_key=list(full_key),
        nn_key=list(nn_key), pc_frame_proof_sha256=proof_sha,
        slot_descriptor_sha256=descriptor_sha, nn_manifest_sha256=manifest.sha256,
        parser_manifest_sha256=parser_manifest_sha256(manifest), completed_j_us=str(completed))
    return candidates, meta


@dataclass(frozen=True, slots=True, init=False)
class PcParserComplete:
    """원 M parser 완료 진술이다. S는 실제 A NN slot/FILLED와 아래 원 key들을 전부 대조한다."""
    packet: PcActorPacket
    purpose: str
    full_key: tuple
    nn_key: tuple
    pc_frame_proof_sha256: str
    slot_descriptor_sha256: str
    nn_manifest_sha256: str
    parser_manifest_sha256: str
    completed_j_us: int

    def __init__(self, packet, binding, manifest):
        meta = _packet(packet, binding, 'PC_PARSE_COMPLETE', PARSE_FIELDS)
        _manifest_binding(manifest, binding)
        _need(type(manifest) is SimNnManifest and meta['profile'] == 'pc-parser-complete-1'
            and meta['nn_manifest_sha256'] == binding.fields['nn_manifest_sha256'] == manifest.sha256
            and meta['parser_manifest_sha256'] == parser_manifest_sha256(manifest),
            'parser completion differs from pinned actual NN/parser manifest')
        key = meta['nn_key']
        _need(type(key) is list and len(key) == 8 and key[:4] == meta['full_key'], 'original full NN8 key required')
        _uint(key[4]); _uint(key[5]); _sha(key[7])
        _need(key[6] == manifest.sha256, 'NN result context differs from original manifest')
        completed = _decimal(meta['completed_j_us'])
        _need(completed <= packet.issued_us < packet.expires_us, 'actual parser completion must precede original issue/E')
        _sha(meta['slot_descriptor_sha256'])
        values = dict(packet=packet, **{name: meta[name] for name in
            ('purpose', 'pc_frame_proof_sha256', 'slot_descriptor_sha256', 'nn_manifest_sha256', 'parser_manifest_sha256')},
            full_key=tuple(meta['full_key']), nn_key=tuple(key), completed_j_us=completed)
        for name, value in values.items(): object.__setattr__(self, name, value)

    @property
    def job_id(self): return self.packet.request_id

    @property
    def expires_us(self): return self.packet.expires_us

    def validate_original(self, *, job_id, expires_us, full_key, nn_key,
                          pc_frame_proof_sha256, slot_descriptor_sha256, purpose, now_us):
        self.validate_identity(job_id=job_id, expires_us=expires_us, full_key=full_key, nn_key=nn_key,
            pc_frame_proof_sha256=pc_frame_proof_sha256, slot_descriptor_sha256=slot_descriptor_sha256,
            purpose=purpose, now_us=now_us)
        if now_us >= expires_us:
            raise FrameExpired('parser completion frame expired')
        return self

    def validate_identity(self, *, job_id, expires_us, full_key, nn_key,
                          pc_frame_proof_sha256, slot_descriptor_sha256, purpose, now_us):
        _need((self.job_id, self.expires_us, self.full_key, self.nn_key, self.pc_frame_proof_sha256,
                self.slot_descriptor_sha256, self.purpose) ==
            (job_id, expires_us, full_key, nn_key, pc_frame_proof_sha256, slot_descriptor_sha256, purpose)
            and self.packet.issued_us <= _uint(now_us),
            'parser completion changed original A NN job/E/key/proof/slot/purpose')
        return self
