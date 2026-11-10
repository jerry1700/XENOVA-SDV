"""원 Scope/Section의 M-local checkpoint membership만 검산한다.

준비 자료는 D 성공이 아니다. 원 COMMITTED 또는 완전 SourceComplete만 membership을
만들며 Pi의 독립 D owner/capability/현재 World/이동 권위는 만들지 않는다. 원 clock bytes와
provenance closure를 읽되 현재 lease/관측/시각을 발급하지 않는다. SQL/파일 I/O는 없다.
"""

from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
import struct
from uuid import UUID

from .map_checkpoint import SourceComplete
from .mapper_route_records import PreparedObservedRoad, PreparedRoadRecord, _original_reply, _shared
from .mapper_sensor_state import SensorFrameSnapshot
from .scene import SceneEvidenceScope
from ..storage.contracts import CommitReceipt, StorageContext, canonical, json_bytes, uint
from ..storage.geometry_scope import same_catalog_geometry
from ..storage.mapper_state import decode_mapper_state, mapper_state_reference
from ..storage.mapper_route_state import (STATE_KEY, PROFILE, encode_route_state, validate_route_state)
from ..storage.port import CheckpointRequest
from ..storage.records import CheckpointCommitOrigin, EvidenceTable, PROVENANCE, REGISTRATION


class RouteCheckpointNotReady(ValueError):
    """원 route/clock/closure/COMMITTED 또는 완전 LOAD의 membership을 검산하지 못했다."""


def _need(value, reason):
    if not value:
        raise RouteCheckpointNotReady(reason)


def _decoded(value):
    schema, _, decode, _ = _shared()
    scope, section = (decode(b64decode(row['body_b64'], validate=True), cls)
        for row, cls in zip(value['records'], (schema.RoadGeometryScope, schema.RoadSection)))
    clock = value['clock']
    probe, trust, reply = (decode(b64decode(clock[name], validate=True), cls) for name, cls in
        (('probe_b64', schema.MapperClockProbe), ('trust_b64', schema.WorldTrust), ('reply_b64', schema.MapperClockReply)))
    return scope, section, probe, trust, reply


def _closure(value, evidence, scope):
    _need(type(evidence) is EvidenceTable and type(scope) is SceneEvidenceScope,
        'actual evidence table and approved source scope required')
    records = {row.provenance_id: row for row in evidence.provenance}
    contexts = {row.sha256: row for row in evidence.contexts}
    roots = value['roots']
    pending = [row['provenance_id'] for row in roots.values()]
    visited = set()
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        row = records.get(identity)
        _need(row is not None and row.context_sha256 in contexts, 'original route parent/context closure missing')
        _need(len(visited) < 16384 and all(parent in records for parent in row.parents),
            'route ancestry exceeds or loses original parent closure')
        actual, _ = scope.record_sources(row, contexts[row.context_sha256], evidence=evidence)
        inherited = 0
        for parent in row.parents:
            inherited |= records[parent].source_mask
        _need(PROVENANCE.unpack(row.payload)[5] == UUID(scope.context.pi_host_boot).bytes and
            not row.source_mask & ~(actual | inherited), 'route parent source/Pi mask differs from approved closure')
        visited.add(identity)
        pending.extend(row.parents)
    selected = {}
    for role, pin in roots.items():
        row = records[pin['provenance_id']]
        _need(sha256(row.encode()).hexdigest() == pin['record_sha256'] and
            sha256(row.payload).hexdigest() == pin['payload_sha256'], 'route root bytes differ: ' + role)
        selected[role] = row
    return selected, contexts, records, visited


def _context(row, contexts, scope):
    record = contexts[row.context_sha256]
    _, body = scope._record_scope(record)
    _need(body['body']['profile'] == 'scene-source-context-1', 'original camera/registration source context required')
    return StorageContext.from_fields(body['body']['storage_context']), body['body']


def _semantic(value, evidence, scope, context):
    """시점 승인이 아니라 원 body와 실제 승인된 원 source/REG/전체 ancestry의 동일성이다."""
    _need(type(context) is StorageContext, 'original checkpoint storage context required')
    value = validate_route_state(value)
    geometry, section, probe, trust, reply = _decoded(value)
    roots, contexts, records, _ = _closure(value, evidence, scope)
    frame, registration, anchor = (roots[name] for name in ('frame', 'registration', 'anchor'))
    frame_context, frame_body = _context(frame, contexts, scope)
    registration_context, _ = _context(registration, contexts, scope)
    birth, _ = _context(anchor, contexts, scope)
    origin = section.origin
    common = ('database_uuid', 'map_epoch', 'frame_epoch', 'odom_epoch', 'pi_host_boot',
        'configuration', 'calibration', 'primitive_sha256')
    _need(all(getattr(frame_context, name) == getattr(context, name) for name in common) and
        frame_context.geometry_origin == context.geometry_origin, 'route differs from selected checkpoint geometry/origin')
    _need(same_catalog_geometry(registration_context, frame_context, 'LANE') and
        same_catalog_geometry(birth, frame_context, 'LANE'),
        'original frame/registration/anchor storage relation differs')
    binding = json_bytes(frame_context.source_binding, 2048)
    raw = frame_body['raw_binding']
    source = dict(vehicle=frame_context.geometry_origin_fields['vehicle'], device=probe.device,
        mapper_boot=str(UUID(bytes=origin.mapper_boot)),
        mapper_instance=frame_context.origin_mapper_instance, clock_epoch=raw['clock_epoch'],
        map_epoch=frame_context.map_epoch, frame_epoch=frame_context.frame_epoch, odom_epoch=frame_context.odom_epoch,
        configuration=frame_context.configuration, calibration=frame_context.calibration, primitive=frame_context.primitive_sha256)
    _need(origin.origin_context_sha256 == bytes.fromhex(frame_context.sha256) and
        origin.raw_binding_sha256 == bytes.fromhex(binding['raw_binding_sha256']) and
        origin.mapper_instance == UUID(frame_context.origin_mapper_instance).bytes and
        origin.clock_epoch == UUID(raw['clock_epoch']).bytes and
        all(getattr(trust, name) == value for name, value in source.items() if name != 'device') and
        all(getattr(reply, name) == value for name, value in source.items() if name != 'vehicle'),
        'route original source/clock/full context changed')
    _need(frame.source_mask & 1 and PROVENANCE.unpack(frame.payload)[2] != 5 and
        frame.observation_sequence == origin.observation_sequence and
        origin.frame_evidence_sha256 == bytes.fromhex(value['roots']['frame']['record_sha256']),
        'original frame root differs from Section origin')
    rgb = UUID(raw['stream_instances']['rgb_video']).bytes
    # RGB 원 raw key는 frame root의 전체 부모 중에 있을 수 있다. device_us를 J로 바꾸지 않는다.
    matched = vehicle_matched = False
    frame_pending, frame_seen = [frame.provenance_id], set()
    while frame_pending:
        identity = frame_pending.pop()
        if identity in frame_seen:
            continue
        frame_seen.add(identity)
        row = records[identity]; data = PROVENANCE.unpack(row.payload)
        if data[2] != 5:
            for at in range(data[3]):
                instance, sequence, _ = struct.unpack_from('<16sQQ', data[-3], at * 32)
                matched |= instance == rgb and sequence == origin.frame_source_sequence
                vehicle_matched |= (data[2] == 0 and row.source_mask & 16 and
                    row.observation_sequence == origin.vehicle_sequence and
                    instance == UUID(frame_context.geometry_origin_fields['device_session']).bytes)
        frame_pending.extend(row.parents)
    _need(matched and vehicle_matched, 'original RGB/Vehicle source sequence is not in frame ancestry')
    for role, row, old in (('registration', registration, registration_context), ('anchor', anchor, birth)):
        data = PROVENANCE.unpack(row.payload)
        _need(data[2] == 5 and row.parents, 'original REGISTRATION union required: ' + role)
        reg = REGISTRATION.unpack(data[-3])
        _need(reg[2:5] == tuple(UUID(getattr(old, name)).bytes for name in ('map_epoch', 'frame_epoch', 'odom_epoch'))
            and reg[11] == UUID(old.origin_mapper_instance).bytes, 'original registration source differs')
    registered, pending = set(), [registration.provenance_id]
    while pending:
        identity = pending.pop()
        if identity not in registered:
            registered.add(identity); pending.extend(records[identity].parents)
    _need(anchor.provenance_id in registered, 'current registration lost original propagation anchor ancestry')
    _need(origin.alignment_raw_sha256 == bytes.fromhex(value['roots']['registration']['record_sha256']) and
        section.alignment_raw_sha256 == origin.alignment_raw_sha256 and
        geometry.alignment_frame_sha256 == bytes.fromhex(value['roots']['anchor']['payload_sha256']) and
        geometry.origin_storage_context_sha256 == bytes.fromhex(birth.sha256), 'registration/anchor hash domains differ')
    geo = birth.geometry_origin_fields
    expected = dict(dataset_uuid=UUID(birth.database_uuid).bytes, map_epoch=UUID(birth.map_epoch).bytes,
        frame_epoch=UUID(birth.frame_epoch).bytes, vehicle=geo['vehicle'], configuration=birth.configuration,
        calibration=birth.calibration, primitive=bytes.fromhex(birth.primitive_sha256),
        origin_pi_host_boot=UUID(birth.pi_host_boot).bytes, origin_gateway_instance=UUID(geo['gateway_instance']).bytes,
        origin_pi_session=UUID(geo['pi_session']).bytes, origin_device_session=UUID(geo['device_session']).bytes,
        origin_odom_epoch=UUID(birth.odom_epoch).bytes)
    _need(all(getattr(geometry, name) == item for name, item in expected.items()), 'original Scope geometry/Pi origin differs')
    capture = origin.capture_mapper_lower_us + trust.lower_offset_us, origin.capture_mapper_upper_us + trust.upper_offset_us
    _need(0 <= capture[0] <= capture[1] < 2**63 and
        frame.pi_lower <= capture[0] <= capture[1] <= frame.pi_upper,
        'original capture Pi hull differs from frame proof')
    return value


def from_prepared(prepared, snapshot, evidence, *, scope):
    """동일 원 frame의 선택 META만 반환한다. None은 명시 부재이며 D 작업/성공이 아니다."""
    if prepared is None:
        return None
    try:
        _need(type(prepared) is PreparedObservedRoad and type(snapshot) is SensorFrameSnapshot and
            type(scope) is SceneEvidenceScope and type(evidence) is EvidenceTable, 'actual prepared/snapshot/evidence/scope required')
        _need(type(prepared.records) is tuple and len(prepared.records) == 2 and
            all(type(row) is PreparedRoadRecord for row in prepared.records), 'original bounded Scope/Section pair required')
        _need(type(snapshot.mapper_reply_bytes) is bytes and
            prepared.original_mapper_reply == snapshot.mapper_reply_bytes,
            'prepared Reply R2 is not the original frozen snapshot trace')
        inputs = snapshot.metric_inputs
        _need(inputs.storage_context == scope.context and prepared.context_sha256 == scope.context.sha256,
            'prepared snapshot changed original full context')
        rows = {row.provenance_id: row for row in evidence.provenance}
        originals = prepared.original_provenance_bytes
        _need(type(originals) is tuple and len(originals) in (2, 3), 'original frame/REG/anchor roots required')
        selected = []
        for raw in originals:
            _need(type(raw) is bytes and len(raw) == 272, 'original encoded provenance record required')
            identity = struct.unpack_from('<Q', raw)[0]
            _need(identity in rows and rows[identity].encode() == raw, 'prepared root is not the original evidence record')
            selected.append(rows[identity])
        _need(selected[0].provenance_id == prepared.frame_provenance_id and
            selected[1].provenance_id == prepared.alignment_provenance_id, 'prepared root roles changed')
        expected_contexts = tuple(dict.fromkeys(row.context_sha256 for row in selected))
        by_context = {row.sha256: row for row in evidence.contexts}
        _need(all(key in by_context for key in expected_contexts), 'prepared original root context missing')
        birth_context = StorageContext.from_fields(json_bytes(
            by_context[selected[-1].context_sha256].payload, 4096)['body']['storage_context'])
        _need(birth_context.bytes == prepared.original_geometry_context_bytes, 'prepared original geometry context changed')
        _need(tuple(by_context[key].encode() for key in expected_contexts) == prepared.original_provenance_context_bytes,
            'prepared original context bytes changed')
        value = dict(profile=PROFILE, records=[dict(kind=row.kind, body_b64=b64encode(row.body).decode('ascii'),
            sha256=row.sha256.hex()) for row in prepared.records],
            clock=dict(probe_b64=b64encode(inputs.clock_lease.raw_probe_bytes).decode('ascii'),
                trust_b64=b64encode(inputs.clock_lease.trust_bytes).decode('ascii'),
                reply_b64=b64encode(prepared.original_mapper_reply).decode('ascii')),
            roots={name:dict(provenance_id=row.provenance_id, record_sha256=sha256(row.encode()).hexdigest(),
                payload_sha256=sha256(row.payload).hexdigest()) for name, row in
                zip(('frame', 'registration', 'anchor'), (selected[0], selected[1], selected[-1]))})
        value = _semantic(value, evidence, scope, scope.context)
        _, section, _, _, _ = _decoded(value)
        origin = section.origin; completed = origin.completed_mapper_us
        snapshot.validate_frame(job_id=snapshot.job_id, source_generation=snapshot.source_generation,
            source_key=snapshot.source_key, expires_us=snapshot.expires_us, now_us=completed)
        stamp = snapshot.capture_stamp
        _need(stamp.captured_us == (origin.capture_mapper_lower_us, origin.capture_mapper_upper_us) and
            stamp.source_id == scope.raw_binding.fields['acquisition_worker'] and
            stamp.stream_id == scope.raw_binding.fields['stream_instances']['rgb_video'] and
            stamp.clock_id == scope.raw_binding.fields['clock_epoch'] and
            stamp.sequence == origin.frame_source_sequence and snapshot.source_generation == scope.raw_binding.sha256 and
            prepared.source_binding_revision == int(json_bytes(scope.context.source_binding, 2048)['source_binding_revision']) and
            sha256(inputs.clock_lease.raw_probe_bytes).digest() == prepared.original_probe_sha256,
            'prepared source/frame/revision differs from original frozen snapshot')
        snapshot.original_vehicle_admission(inputs.vehicle)
        _original_reply(inputs.clock_lease, snapshot, prepared.original_mapper_reply, inputs.source, completed)
        return {STATE_KEY: value}
    except RouteCheckpointNotReady:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
        raise RouteCheckpointNotReady('prepared route rejected: ' + str(error)) from error


@dataclass(frozen=True, slots=True, init=False)
class RouteCheckpointMembership:
    """역사 membership만 보존한다. 큰 CP payload/tiles/evidence와 현재 lease를 보유하지 않는다."""
    checkpoint_ref: bytes
    receipt_bytes: bytes
    receipt_sha256: str
    checkpoint_payload_sha256: str
    state_sha256: str
    route_state_bytes: bytes
    records: tuple

    def __init__(self, *args, **kwargs):
        raise TypeError('route membership requires original COMMITTED or complete LOAD factories')

    def contains(self, checkpoint_ref, kind, record_raw):
        return (type(checkpoint_ref) is bytes and checkpoint_ref == self.checkpoint_ref and type(kind) is int
            and type(record_raw) is bytes and any(row.kind == kind and row.body == record_raw for row in self.records))


def _member(value, manifest, reference, receipt, payload_sha):
    route = encode_route_state(value)
    ref = mapper_state_reference(json_bytes(manifest, 16384)['scene'])
    _need(ref is not None, 'original referenced mapper-state pages required')
    result = object.__new__(RouteCheckpointMembership)
    receipt_raw = canonical(receipt.fields)
    records = tuple(PreparedRoadRecord(row['kind'], b64decode(row['body_b64'], validate=True), bytes.fromhex(row['sha256']))
        for row in value['records'])
    for key, item in dict(checkpoint_ref=reference, receipt_bytes=receipt_raw, receipt_sha256=sha256(receipt_raw).hexdigest(),
        checkpoint_payload_sha256=payload_sha, state_sha256=ref['state_sha256'], route_state_bytes=route, records=records).items():
        object.__setattr__(result, key, item)
    return result


def _state(manifest, evidence, context, scope):
    state, pages = decode_mapper_state(json_bytes(manifest, 16384)['scene'], evidence.contexts)
    _need(type(state) is dict and state.get('profile') == 'mapper-pipeline-state-1' and
        state.get('calibration_id') == scope.calibration.sha256 and pages and STATE_KEY in state and
        StorageContext.from_fields(state['storage_context']) == context, 'original route META/state/full context required')
    return _semantic(state[STATE_KEY], evidence, scope, context)


def from_committed(original_request, receipt, *, scope, observed_us, current_context_sha256, now_us):
    """현재 원 CP COMMITTED를 검산한다. observed_us는 실제 M의 원 receipt 소비 J다.

    D의 COMMIT 반환시각을 새로 만드는 값이 아니다. 원 sampled commit stamp와 별도로
    소비/현재 J가 원 job E 안인지 검사한다. DUPLICATE/UNKNOWN/QUERY는 이 factory가 아니다.
    """
    try:
        _need(type(original_request) is CheckpointRequest and type(receipt) is CommitReceipt and
            type(scope) is SceneEvidenceScope and receipt.status == 'COMMITTED', 'original checkpoint and COMMITTED receipt required')
        uint(observed_us); uint(now_us)
        job, payload = original_request.job, original_request.payload
        _need(payload.intent == 'CHECKPOINT' and scope.context == job.context and
            job.issued_us <= receipt.committed_j_us <= observed_us <= now_us < job.expires_us and
            receipt.current(job, payload, current_context_sha256, now_us), 'original receipt observation/source/deadline differs')
        value = _state(payload.manifest, payload.evidence, job.context, scope)
        from .mapper_mapping import checkpoint_reference
        reference = checkpoint_reference(original_request, receipt, now_us=now_us)
        return _member(value, payload.manifest, reference, receipt, payload.sha256)
    except RouteCheckpointNotReady:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
        raise RouteCheckpointNotReady('original route commit rejected: ' + str(error)) from error


def from_complete(complete, *, scope, now_us):
    """모든 원 LOAD/원 COMMITTED descriptor의 역사 membership이다. merged SHA를 원 CP로 쓰지 않는다."""
    try:
        _need(type(complete) is SourceComplete and type(scope) is SceneEvidenceScope and
            type(complete.original_commit) is CheckpointCommitOrigin, 'complete pages with original COMMITTED descriptor required')
        # 이 helper가 complete_restore_head를 호출해 모든 원 page/head와 source를 검산한다.
        historical = scope.with_complete_history(complete, now_us=now_us)
        original = complete.original_commit
        payload_sha = original.validate(complete.original_job, complete.manifest, complete.checkpoint_ref)
        value = _state(complete.manifest, complete.evidence, complete.original_job.context, historical)
        return _member(value, complete.manifest, complete.checkpoint_ref, original.receipt, payload_sha)
    except RouteCheckpointNotReady:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
        raise RouteCheckpointNotReady('complete route membership rejected: ' + str(error)) from error
