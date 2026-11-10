"""원 Probe에 대한 JIO Reply 생성 사실만 보존하는 bounded local 자료다.

Pi P4, MQTT 송신 완료, clock 수락이나 이동 권한을 증명하지 않는다. local relay E는
최초 전달에만 쓰며, 이미 받은 원 bytes를 새 관측/lease로 갱신하지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256

from ..storage.contracts import canonical, json_bytes, uuid


SOURCE_FIELDS = frozenset(('vehicle', 'device', 'mapper_boot', 'mapper_instance', 'clock_epoch',
    'map_epoch', 'frame_epoch', 'odom_epoch', 'configuration', 'calibration', 'primitive'))
ORIGIN_FIELDS = frozenset(('pi_host_boot', 'gateway_instance', 'device_session', 'pi_session'))
REPLY_METADATA = frozenset(('probe_sha256', 'source', 'origin', 'probe_received_j_us'))
MAX_CLOCK_BYTES = 1024
MAX_REPLY_TRACES = 8


def _bindings(source, origin):
    if type(source) is not dict or set(source) != SOURCE_FIELDS or type(origin) is not dict or set(origin) != ORIGIN_FIELDS:
        raise ValueError('exact original clock source and Pi origin required')
    for key, value in source.items():
        if type(value) is not str or not 1 <= len(value) <= 128 or any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError('bounded original source scalar required')
        if key.endswith('_epoch') or key in ('mapper_boot', 'mapper_instance'): uuid(value)
    for value in origin.values(): uuid(value)
    return canonical(source), canonical(origin)


def reply_fields(raw, metadata, *, issued_us, expires_us):
    """local packet의 원 Reply/body와 exact metadata를 검사한다. Probe 결합은 별도다."""
    from xenova_drive_world import decode, schema
    from xenova_drive_world.mapper_clock import TIMEOUT_US
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_CLOCK_BYTES:
        raise ValueError('original bounded MapperClockReply bytes required')
    if type(metadata) is not dict or set(metadata) != REPLY_METADATA:
        raise ValueError('exact original clock reply metadata required')
    key = metadata['probe_sha256']; received = metadata['probe_received_j_us']
    if type(key) is not str or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('original Probe SHA required')
    if (type(received) is not str or not 1 <= len(received) <= 20 or not received.isascii()
            or not received.isdecimal() or str(int(received)) != received):
        raise ValueError('original Probe callback J must be canonical decimal')
    source, origin = _bindings(metadata['source'], metadata['origin'])
    reply = decode(raw, schema.MapperClockReply)
    if (type(issued_us) is not int or type(expires_us) is not int
            or not 0 < reply.j2 == int(received) <= reply.j3 == issued_us < expires_us < 2**63
            or expires_us > reply.j2 + TIMEOUT_US or reply.major != 1
            or reply.pi_host_boot != metadata['origin']['pi_host_boot']
            or any(getattr(reply, k) != v for k, v in metadata['source'].items() if k != 'vehicle')):
        raise ValueError('Reply changed original source/callback/generation time/local expiry')
    return reply, source, origin


@dataclass(frozen=True, slots=True, init=False)
class MapperReplyTrace:
    """원 Probe/Reply의 불변 비교 결과다. factory 호출 자체는 실제 JIO 실행 증거가 아니다."""
    raw_probe_bytes: bytes
    raw_reply_bytes: bytes
    source_bytes: bytes
    origin_bytes: bytes
    probe_sha256: str
    generated_j_us: int
    expires_j_us: int
    probe_received_j_us: int

    def __init__(self, *args, **kwargs):
        raise TypeError('MapperReplyTrace requires original Probe/Reply factory validation')

    @classmethod
    def from_original(cls, probe_raw, reply_raw, *, source, origin, probe_received_j_us, expires_j_us):
        from xenova_drive_world import decode, schema
        if type(probe_raw) is not bytes or not 0 < len(probe_raw) <= MAX_CLOCK_BYTES:
            raise ValueError('original bounded MapperClockProbe bytes required')
        if type(probe_received_j_us) is not int:
            raise ValueError('original Probe callback J required')
        probe = decode(probe_raw, schema.MapperClockProbe)
        reply = decode(reply_raw, schema.MapperClockReply) if type(reply_raw) is bytes and len(reply_raw) <= MAX_CLOCK_BYTES else None
        if reply is None: raise ValueError('original bounded MapperClockReply bytes required')
        key = sha256(probe_raw).hexdigest()
        metadata = dict(probe_sha256=key, source=source, origin=origin,
            probe_received_j_us=str(probe_received_j_us))
        reply, source_raw, origin_raw = reply_fields(reply_raw, metadata,
            issued_us=reply.j3, expires_us=expires_j_us)
        if (probe.major != 1 or probe.vehicle != source['vehicle']
                or any(getattr(probe, k) != getattr(reply, k) for k in (
                    'pi_host_boot', 'pi_client_instance', 'probe_seq', 'nonce', 'pi_send_us', 'device'))
                or any(probe.HasField('expected_' + k) and getattr(probe, 'expected_' + k) != source[k]
                    for k in ('mapper_boot', 'mapper_instance', 'clock_epoch'))):
            raise ValueError('Reply is not for the exact original Probe/source')
        result = object.__new__(cls)
        values = dict(raw_probe_bytes=probe_raw, raw_reply_bytes=reply_raw, source_bytes=source_raw,
            origin_bytes=origin_raw, probe_sha256=key, generated_j_us=reply.j3,
            expires_j_us=expires_j_us, probe_received_j_us=probe_received_j_us)
        for name, value in values.items(): object.__setattr__(result, name, value)
        return result

    @property
    def source(self): return json_bytes(self.source_bytes, 2048)

    @property
    def origin(self): return json_bytes(self.origin_bytes, 2048)

    @property
    def metadata(self):
        return dict(probe_sha256=self.probe_sha256, source=self.source, origin=self.origin,
            probe_received_j_us=str(self.probe_received_j_us))

    def __copy__(self): return self

    def __deepcopy__(self, memo): return self
