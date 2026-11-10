"""Scope1/Section1의 원 body·clock trace·proof roots를 닫는 선택형 META codec.

canonical JSON/공용 B10 body 관계만 검사한다. 실제 evidence membership, snapshot
admission, JIO 생성 출처, COMMITTED/LOAD/current 사용권은 runtime owner의 책임이다.
Prepared factory나 SQL·시계·ID 발급은 없고 B10은 이 profile 검산 때만 import한다.
"""

from base64 import b64decode, b64encode
from hashlib import sha256
from uuid import UUID

from .contracts import PersistenceError, canonical, digest, json_bytes, uint


STATE_KEY = 'route_records'
PROFILE = 'mapper-route-records-1'
MAX_ROUTE_STATE_BYTES = 16 * 1024
_KINDS = (8, 3)
_BODY_CAPS = (768, 4096)
_ROLES = ('frame', 'registration', 'anchor')
_ROOT_FIELDS = frozenset(('provenance_id', 'record_sha256', 'payload_sha256'))


def _need(condition, message):
    if not condition:
        raise PersistenceError(message)


def _exact(value, names):
    _need(type(value) is dict and all(type(key) is str for key in value) and set(value) == set(names),
          'exact native mapper route metadata fields required')


def _raw(value, cap):
    _need(type(value) is str and value.isascii() and 0 < len(value) <= 4 * ((cap + 2) // 3),
          'bounded original mapper route base64 required')
    try:
        raw = b64decode(value, validate=True)
    except ValueError as error:
        raise PersistenceError('invalid mapper route base64') from error
    _need(0 < len(raw) <= cap and b64encode(raw).decode('ascii') == value,
          'canonical original mapper route base64 required')
    return raw


def _shared():
    try:
        from xenova_drive_world import schema, decode, encode
        from xenova_drive_world.route import route_record_sha256
    except ImportError as error:
        raise PersistenceError('coherent B10 route codec required for mapper route metadata') from error
    _need(hasattr(schema, 'RoadGeometryScope') and hasattr(schema, 'RoadSection'),
          'coherent B10 route schema required for mapper route metadata')
    return schema, decode, encode, route_record_sha256


def _message(raw, message_type, decode, encode):
    value = decode(raw, message_type)
    # 검산용 roundtrip일 뿐 원 body를 재생성·교체하거나 geometry를 재투영하지 않는다.
    _need(encode(value) == raw, 'noncanonical original mapper route protobuf body')
    return value


def _relations(value, record_raw, clock_raw):
    schema, decode, encode, record_hash = _shared()
    scope = _message(record_raw[0], schema.RoadGeometryScope, decode, encode)
    section = _message(record_raw[1], schema.RoadSection, decode, encode)
    for item, raw in zip(value['records'], record_raw):
        _need(record_hash(item['kind'], raw).hex() == item['sha256'], 'mapper route record hash differs')
    roots = value['roots']
    _need(section.geometry_scope_sha256.hex() == value['records'][0]['sha256']
          and section.origin.frame_evidence_sha256.hex() == roots['frame']['record_sha256']
          and section.observation_evidence_sha256 == section.origin.frame_evidence_sha256
          and section.origin.alignment_raw_sha256.hex() == roots['registration']['record_sha256']
          and section.alignment_raw_sha256 == section.origin.alignment_raw_sha256
          and scope.alignment_frame_sha256.hex() == roots['anchor']['payload_sha256'],
          'mapper route body differs from original scope/frame/registration/anchor hashes')
    probe = _message(clock_raw[0], schema.MapperClockProbe, decode, encode)
    trust = _message(clock_raw[1], schema.WorldTrust, decode, encode)
    reply = _message(clock_raw[2], schema.MapperClockReply, decode, encode)
    _need(probe.HasField('mapper_trust') and encode(probe.mapper_trust) == clock_raw[1],
          'mapper route trust differs from original returned Probe trust')
    _need(section.origin.mapper_clock_reply_sha256 == sha256(clock_raw[2]).digest(),
          'mapper route origin differs from original Reply trace')
    _need(probe.major == reply.major == 1 and all(getattr(reply, key) == getattr(probe, key)
          for key in ('pi_host_boot', 'pi_client_instance', 'probe_seq', 'nonce', 'pi_send_us', 'device')),
          'mapper route Reply differs from original Probe/session/nonce/P1')
    source_fields = ('mapper_boot', 'mapper_instance', 'clock_epoch', 'map_epoch', 'frame_epoch',
                     'odom_epoch', 'configuration', 'calibration', 'primitive')
    _need(all(getattr(reply, key) == getattr(trust, key) for key in source_fields),
          'mapper route Reply differs from returned trust source')
    _need(probe.vehicle == trust.vehicle == scope.vehicle
          and UUID(probe.pi_host_boot).bytes == scope.origin_pi_host_boot
          and UUID(trust.map_epoch).bytes == scope.map_epoch
          and UUID(trust.frame_epoch).bytes == scope.frame_epoch
          and UUID(trust.odom_epoch).bytes == scope.origin_odom_epoch
          and trust.configuration == scope.configuration and trust.calibration == scope.calibration
          and bytes.fromhex(trust.primitive) == scope.primitive
          and UUID(trust.mapper_boot).bytes == section.origin.mapper_boot
          and UUID(trust.mapper_instance).bytes == section.origin.mapper_instance
          and UUID(trust.clock_epoch).bytes == section.origin.clock_epoch,
          'mapper route scope/origin differs from original clock source')
    _need(0 < reply.j2 <= reply.j3 <= section.origin.completed_mapper_us
          and reply.j2 + trust.upper_offset_us >= probe.pi_send_us
          and reply.j3 + trust.upper_offset_us < trust.expires_pi_us,
          'mapper route Reply trace is outside original clock support')


def validate_route_state(fields):
    """exact tree를 검산해 독립 native 사본을 반환한다. evidence/current 성공은 아니다.

    records는 원 Scope8/Section3 순서이며 각 sha256은 공용 route record domain이다.
    roots의 record_sha256은 encode() 전체, payload_sha256은 원256B payload의 SHA다.
    """
    _exact(fields, ('profile', 'records', 'clock', 'roots'))
    _need(type(fields['profile']) is str and fields['profile'] == PROFILE, 'unsupported mapper route metadata profile')
    records = fields['records']
    _need(type(records) is list and len(records) == 2, 'exact Scope1/Section1 mapper route records required')
    owned_records = []
    record_raw = []
    for item, kind, cap in zip(records, _KINDS, _BODY_CAPS):
        _exact(item, ('kind', 'body_b64', 'sha256'))
        _need(type(item['kind']) is int and item['kind'] == kind, 'ordered Scope8/Section3 records required')
        key = digest(item['sha256'])
        raw = _raw(item['body_b64'], cap)
        owned_records.append(dict(kind=kind, body_b64=item['body_b64'], sha256=key))
        record_raw.append(raw)
    clock = fields['clock']
    clock_names = ('probe_b64', 'trust_b64', 'reply_b64')
    _exact(clock, clock_names)
    owned_clock = {key: clock[key] for key in clock_names}
    clock_raw = tuple(_raw(owned_clock[key], 1024) for key in clock_names)
    roots = fields['roots']
    _exact(roots, _ROLES)
    owned_roots = {}
    identities = {}
    for role in _ROLES:
        root = roots[role]
        _exact(root, _ROOT_FIELDS)
        identity = uint(root['provenance_id'], positive=True)
        hashes = digest(root['record_sha256']), digest(root['payload_sha256'])
        _need(identity not in identities or identities[identity] == hashes,
              'same mapper route provenance ID changed original hashes')
        identities[identity] = hashes
        owned_roots[role] = dict(provenance_id=identity, record_sha256=hashes[0], payload_sha256=hashes[1])
    owned = dict(profile=PROFILE, records=owned_records, clock=owned_clock, roots=owned_roots)
    _need(len(canonical(owned)) <= MAX_ROUTE_STATE_BYTES, 'mapper route metadata exceeds16KiB')
    try:
        _relations(owned, tuple(record_raw), clock_raw)
    except PersistenceError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise PersistenceError('invalid original mapper route protobuf relations') from error
    return owned


def encode_route_state(fields):
    """검산한 원 tree의 canonical bytes다. 원 protobuf bytes를 바꾸지 않는다."""
    return canonical(validate_route_state(fields))


def decode_route_state(raw):
    """16KiB 안의 exact canonical JSON만 읽는다. malformed를 legacy로 바꾸지 않는다."""
    return validate_route_state(json_bytes(raw, MAX_ROUTE_STATE_BYTES))


def route_state_roots(fields):
    """검산된 frame/current REG/anchor의 sorted unique u63 ID만 반환한다."""
    value = validate_route_state(fields)
    return tuple(sorted({root['provenance_id'] for root in value['roots'].values()}))
