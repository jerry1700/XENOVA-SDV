"""현재 관측한 열린 도로의 canonical Scope/Section bytes를 준비한다.

compute의 원 draft·센서 snapshot·collector proof만 읽는다. D 저장/COMMITTED,
관리 전송, topology 연결, World 발행, 현재 권위는 만들지 않는다. caller는 최종
adoption 시 source를 재검사하고 이 bytes 자체의 저장·완전 LOAD를 별도로 증명한다.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
import math
import struct
from uuid import UUID, uuid5

from .map_checkpoint import MapCheckpointCollector
from .mapper_catalogs import MappingState
from .mapper_mapping import MappingFrameDraft
from .mapper_profile import MapperProfile
from .mapper_sensor_state import SensorFrameSnapshot
from .mapper_world import _current, _heading_bound, _NotReady
from ..mapping.road_basis import RoadBasisSnapshot
from ..storage.contracts import StorageContext, canonical, json_bytes
from ..storage.geometry_scope import same_catalog_geometry
from ..storage.records import PROVENANCE
from ..transport.world_adapter import lane_model_from_road, _f32, _f32_up


class RoadRecordNotReady(ValueError):
    """원 관측/시계/등록 proof가 없거나 현재 32-station 표현이 불가능하다."""


def _need(condition, reason):
    if not condition:
        raise RoadRecordNotReady(reason)


@dataclass(frozen=True, slots=True)
class PreparedRoadRecord:
    """D에 보존할 원 canonical body다. durable receipt나 전송 envelope가 아니다."""
    kind: int
    body: bytes
    sha256: bytes


@dataclass(frozen=True, slots=True)
class PreparedObservedRoad:
    records: tuple
    original_mapper_reply: bytes
    original_probe_sha256: bytes
    context_sha256: str
    source_binding_revision: int
    frame_provenance_id: int
    alignment_provenance_id: int
    original_geometry_context_bytes: bytes
    # CP closure에 이미 있는 원 records를 식별한다. 이것으로 새 road body의 저장을 주장하지 않는다.
    original_provenance_bytes: tuple
    original_provenance_context_bytes: tuple


def _shared():
    # B10 canonical 계약의 정상 병합/배포가 선행돼야 한다. 다른 codec으로 fallback하지 않는다.
    try:
        from xenova_drive_world import schema, encode, decode
        from xenova_drive_world.route import route_record_sha256
    except ImportError as error:
        raise RoadRecordNotReady('route canonical contract is not installed') from error
    _need(hasattr(schema, 'RoadSection'), 'route canonical contract is not installed')
    return schema, encode, decode, route_record_sha256


def _record(kind, value):
    _, encode, _, digest = _shared()
    body = encode(value)
    return PreparedRoadRecord(kind, body, digest(kind, body))


def _original_reply(lease, sensors, raw, source, completed):
    """frozen Probe R2에 JIO가 생성한 원 Reply R2의 출처만 대조한다.

    R2에 실려 돌아온 trust R1이 capture 매핑의 원 시계 근거다. Reply R2의
    존재/해시는 Pi P4·R2 승인·사용권이 아니며 실제 Gateway witness는 별도다.
    caller는 frame snapshot을 만들 때 같은 peer/source cache의 R2를 고정해야 한다.
    나중 latest Reply나 현재 record/CP/World 해시를 넣어 증거 순환을 만들지 않는다.
    """
    schema, _, decode, _ = _shared()
    _need(type(raw) is bytes and 0 < len(raw) <= 1024, 'original MapperClockReply bytes required')
    probe = decode(lease.raw_probe_bytes, schema.MapperClockProbe)
    reply = decode(raw, schema.MapperClockReply)
    _need(any(p[1] == lease.raw_probe_bytes and p[3] == lease.trust_bytes for p in sensors.clock_proofs),
          'clock lease is not in the original frozen snapshot')
    _need(reply.major == probe.major == 1 and all(getattr(reply, name) == getattr(probe, name)
          for name in ('pi_host_boot', 'pi_client_instance', 'probe_seq', 'nonce', 'pi_send_us', 'device')),
          'reply differs from original Probe/session/nonce/P1')
    _need(all(getattr(reply, key) == source[key] for key in ('device', 'mapper_boot', 'mapper_instance',
          'clock_epoch', 'map_epoch', 'frame_epoch', 'odom_epoch', 'configuration', 'calibration', 'primitive')),
          'reply source differs from current sensor source')
    _need(reply.pi_send_us == lease.probe_pi_send_us and reply.pi_host_boot == lease.pi_host_boot,
          'reply differs from returned lease Probe')
    trust = decode(lease.trust_bytes, schema.WorldTrust)
    _need(0 < reply.j2 <= reply.j3 <= completed and
          reply.j2 + trust.upper_offset_us >= probe.pi_send_us and
          reply.j3 + trust.upper_offset_us < lease.expires_pi_us, 'reply time is outside original clock support')
    lease.j_interval_to_pi((completed, completed), now_j_us=completed)
    return reply


def _accepted_roots(draft, state):
    collector, frame = draft.collector, draft.frame
    _need(type(collector) is MapCheckpointCollector and collector.scope == frame.scope and
          collector._last is frame and collector._grid is draft.pipeline.grid,
          'actual accepted collector frame required')
    capture = sha256(canonical(frame.observation.rgb.stamp.key)).hexdigest()
    identity = collector._reference_geometry.get(capture, collector._rgb_refs.get(capture))
    alignment = collector._alignment_record
    pin = collector._propagation_anchor_record
    _need(identity is not None and alignment is not None and pin is not None,
          'original frame/registration/propagation anchor provenance missing')
    _need(alignment[0] == sha256(canonical(draft.pipeline._alignment.fields())).hexdigest() and
          draft.pipeline._propagation_anchor is not None and
          pin[0] == sha256(canonical(draft.pipeline._propagation_anchor.fields())).hexdigest(),
          'original alignment registration pin changed')
    records = {row.provenance_id: row for row in state.evidence.provenance}
    contexts = {row.sha256: row for row in state.evidence.contexts}
    ids = tuple(dict.fromkeys((identity, alignment[1], pin[1])))
    for key in ids:
        row = records.get(key)
        _need(row is not None and row == collector._records.get(key) and row.context_sha256 in contexts,
              'original accepted provenance is not in catalog closure')
        frame.scope.record_sources(row, contexts[row.context_sha256])
    root, registration, anchor = (records[key] for key in (identity, alignment[1], pin[1]))
    _need(root.source_mask & 1 and root.observation_sequence == frame.observation_sequence and
          root.pi_lower <= frame.pi_interval[0] <= frame.pi_interval[1] <= root.pi_upper,
          'frame provenance differs from original observation')
    _need(PROVENANCE.unpack(registration.payload)[2] == PROVENANCE.unpack(anchor.payload)[2] == 5 and
          sha256(anchor.payload).hexdigest() == pin[2], 'original registration/anchor bytes differ')
    birth = _birth_geometry_context(contexts[anchor.context_sha256], draft.context)
    context_ids = tuple(dict.fromkeys(records[key].context_sha256 for key in ids))
    return (root, registration, pin[2], birth, tuple(records[key].encode() for key in ids),
            tuple(contexts[key].encode() for key in context_ids))


def _birth_geometry_context(original, current):
    """고정 anchor의 원 full context를 보존하고 현재 retained-geometry 관계만 검사한다."""
    value = json_bytes(original.payload, 4096)
    _need(value['kind'] == 'METRIC' and value['body'].get('profile') == 'scene-source-context-1',
          'original anchor METRIC source context required')
    birth = StorageContext.from_fields(value['body']['storage_context'])
    _need(same_catalog_geometry(birth, current, 'LANE'), 'anchor geometry is incompatible with current source')
    return birth


def _geometry_scope(birth, profile_sha, anchor_hash):
    """birth geometry와 현재 관측 문맥을 분리한다. Scene/binding revision은 birth를 바꾸지 않는다."""
    schema, _, _, _ = _shared()
    geometry = birth.geometry_origin_fields
    return schema.RoadGeometryScope(major=1, dataset_uuid=UUID(birth.database_uuid).bytes,
        map_epoch=UUID(birth.map_epoch).bytes, frame_epoch=UUID(birth.frame_epoch).bytes,
        vehicle=geometry['vehicle'], configuration=birth.configuration, calibration=birth.calibration,
        primitive=bytes.fromhex(birth.primitive_sha256), origin_pi_host_boot=UUID(birth.pi_host_boot).bytes,
        origin_gateway_instance=UUID(geometry['gateway_instance']).bytes,
        origin_pi_session=UUID(geometry['pi_session']).bytes, origin_device_session=UUID(geometry['device_session']).bytes,
        origin_odom_epoch=UUID(geometry['odom_epoch']).bytes, alignment_frame_sha256=bytes.fromhex(anchor_hash),
        geometry_policy_sha256=bytes.fromhex(profile_sha), coordinate_policy=1,
        origin_storage_context_sha256=bytes.fromhex(birth.sha256))


def _capture_hull(observation):
    # MapperProcessor._metric가 snapshot에 결박한 원 RGB/depth hull과 같은 규칙이다.
    available = max(observation.rgb.stamp.available_us,
        observation.rgb.stamp.available_us if observation.depth is None else observation.depth.stamp.available_us)
    return replace(observation.rgb.stamp, captured_us=observation.capture_interval, available_us=available)


def _section_record(model, basis, origin, scope_hash, alignment_hash):
    """원 station이 같은 때만 한 Section으로 표현한다. clipping/보간으로 범위를 만들지 않는다."""
    schema, _, _, _ = _shared()
    h = model.road.hypotheses[0]
    n = len(basis.points)
    _need(2 <= n <= 32 and len(h.station_s) == n, 'section exceeds original shared 32-station extent')
    actual = b''.join(struct.pack('<ff', *point) for point in h.divider)
    _need(actual == basis.points_f32, 'current divider has no exact adopted basis station correspondence')
    road = lane_model_from_road(model.road, lane_ids=model.lane_ids, model_revision=model.model_revision,
                               lane_revisions=model.lane_revisions, crossing=model.crossing)
    _need(len(road.divider) == n, 'lane conversion changed section station correspondence')
    result = schema.RoadSection(major=1,
        section_id=uuid5(UUID(model.source.map_epoch), 'road-basis-1/' + basis.reference_id).bytes,
        revision=model.model_revision, geometry_scope_sha256=scope_hash, origin=origin, road=road,
        heading_error_rad=_f32_up(_heading_bound(basis, model.road)),
        alignment_raw_sha256=alignment_hash, observation_evidence_sha256=origin.frame_evidence_sha256)
    _need(len(h.left_outer) == len(h.right_outer) == len(basis.point_error_m) == n and
          len(basis.segment_error_m) == n-1, 'complete original section errors/boundaries required')
    for i, (p, left, right) in enumerate(zip(basis.points, h.left_outer, h.right_outer)):
        wl, wr = tuple(map(_f32, left)), tuple(map(_f32, right))
        quant = max(math.dist(left, wl), math.dist(right, wr))
        bound = max(basis.point_error_m[i], h.error_m + quant,
                    *basis.segment_error_m[max(0, i-1):min(i+1, n-1)])
        result.reference.add(x=p[0], y=p[1])
        result.left_boundary.add(x=wl[0], y=wl[1]); result.right_boundary.add(x=wr[0], y=wr[1])
        result.point_error_m.append(_f32_up(bound))
    return _record(3, result)


def prepare_observed_road_records(draft, state, basis, sensors, *, mapper_profile,
                                  original_mapper_reply, completed_j_us):
    """현재 M 자료에서 Scope/Section 두 body만 준비한다. 실패는 RoadRecordNotReady다.

    원 reply는 S/JIO가 실제 받은 Probe에 대해 생산한 bytes여야 한다. 현재 S의
    인증 peer/channel cache에서 frame snapshot 생성 때 고정한 R2여야 한다. 나중
    latest를 결합하는 것은 허용하지 않는다. 이 전달 hook은 현재 미연결이다.
    R2 hash는 출처 trace이며 capture의 returned trust R1/FRAME/REGISTRATION과
    구분한다. 현재 사용권은 Gateway가 실제 승인한 MapperRouteWitness로 검사한다.
    반환된 bytes의 D 저장·완전 LOAD membership 없이는 RoadData로 발행할 수 없다.
    """
    try:
        _need(type(draft) is MappingFrameDraft and type(state) is MappingState and
              type(basis) is RoadBasisSnapshot and type(sensors) is SensorFrameSnapshot and
              type(mapper_profile) is MapperProfile, 'actual typed M draft/state/profile/snapshot required')
        _need(type(completed_j_us) is int and 0 < completed_j_us < 2**63,
              'original positive mapper completion time required')
        _need(draft.original_job.sensor_snapshot is sensors and sensors.job_id == draft.original_job.job_id and
              sensors.source_generation == draft.original_job.source_generation and
              sensors.source_key == draft.original_job.source_key and
              sensors.capture_stamp == _capture_hull(draft.frame.observation) and completed_j_us < sensors.expires_us,
              'original job/frame snapshot differs or expired')
        inputs = sensors.metric_inputs
        _need(inputs.clock_lease is draft.inputs.clock_lease and inputs.vehicle is draft.inputs.vehicle and
              inputs.pose is draft.inputs.pose and inputs.source == draft.inputs.source,
              'draft replaced original frozen metric inputs')
        calibration = sensors.calibration
        _need(mapper_profile.calibration_profile_sha256 == calibration.sha256 and
              mapper_profile.vehicle_configuration == draft.inputs.source['configuration'] and
              mapper_profile.pipeline == draft.pipeline.profile, 'installed mapper geometry profile differs')
        sensors.original_vehicle_admission(inputs.vehicle)
        _, _, model, capture, _ = _current(draft, state, basis, draft.context, inputs.source,
            calibration, sensors.sensors, inputs.vehicle, inputs.clock_lease, completed_j_us)
        reply = _original_reply(inputs.clock_lease, sensors, original_mapper_reply, inputs.source, completed_j_us)
        frame, registration, anchor_hash, birth, roots, root_contexts = _accepted_roots(draft, state)
        schema, _, _, _ = _shared()
        context = draft.context; source = inputs.source
        scope = _geometry_scope(birth, mapper_profile.sha256, anchor_hash)
        original_alignment = sha256(registration.encode()).digest()
        origin = schema.RoadObservationOrigin(major=1, mapper_boot=UUID(source['mapper_boot']).bytes,
            mapper_instance=UUID(source['mapper_instance']).bytes, clock_epoch=UUID(source['clock_epoch']).bytes,
            observation_sequence=draft.frame.observation_sequence,
            frame_source_sequence=draft.frame.observation.rgb.stamp.sequence,
            capture_mapper_lower_us=capture[0], capture_mapper_upper_us=capture[1], completed_mapper_us=completed_j_us,
            raw_binding_sha256=bytes.fromhex(draft.frame.scope.raw_binding.sha256),
            frame_evidence_sha256=sha256(frame.encode()).digest(), origin_context_sha256=bytes.fromhex(context.sha256),
            # 원 Reply R2의 출처 trace다. Pi P4/승인 또는 CP/현재 World의 hash가 아니다.
            alignment_raw_sha256=original_alignment, mapper_clock_reply_sha256=sha256(original_mapper_reply).digest(),
            vehicle_sequence=inputs.vehicle.anchor.sequence)
        prepared_scope = _record(8, scope)
        section = _section_record(model, basis, origin, prepared_scope.sha256, original_alignment)
        binding = json_bytes(context.source_binding, 2048)
        revision = int(binding['source_binding_revision'])
        _need(0 < revision < 2**63 and reply.probe_seq > 0, 'original source revision required')
        return PreparedObservedRoad((prepared_scope, section), original_mapper_reply,
            sha256(inputs.clock_lease.raw_probe_bytes).digest(), context.sha256, revision,
            frame.provenance_id, registration.provenance_id, birth.bytes, roots, root_contexts)
    except RoadRecordNotReady:
        raise
    except (_NotReady, ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, struct.error) as error:
        raise RoadRecordNotReady('current observed road material rejected: ' + str(error)) from error
