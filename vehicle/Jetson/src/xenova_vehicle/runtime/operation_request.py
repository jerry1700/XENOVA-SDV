"""명시 Jetson 운영 요청의 원 ASCII bytes를 보존하는 닫힌 local 형식이다.

검산은 수신·설정 적용·Pi Gate 수락이 아니다. 원 transaction E와 S 설정의 수명은
별개이며 이 객체는 시계나 actor를 읽지 않는다. public Pi protobuf를 확장하지 않는다.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID


REQUEST_PROFILE = 'jetson-operation-request-1'
REQUEST_CAP = 4096
TRANSACTION_US = 5_000_000
FIELDS = frozenset(('profile', 'service_instance', 'request_id', 'action', 'issued_j_us', 'expires_j_us'))
CONFIG_PROFILE = 'jetson-operation-config-1'
RESULT_PROFILE = 'jetson-operation-result-1'
CONFIG_ACK_PROFILE = 'jetson-operation-config-ack-1'
STATUS_PROFILE = 'jetson-operation-status-1'
CONSUMER_READY_PROFILE = 'jetson-operation-consumer-ready-1'


class OperationRequestError(ValueError):
    """원 요청의 형식·수명·식별자가 local 계약과 다르다."""


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        raise OperationRequestError('canonical nonzero operation UUID required')
    try: parsed = UUID(value)
    except ValueError as error:
        raise OperationRequestError('canonical nonzero operation UUID required') from error
    if str(parsed) != value or not parsed.int:
        raise OperationRequestError('canonical nonzero operation UUID required')
    return value


def _decimal(value):
    if (type(value) is not str or not 1 <= len(value) <= 20 or not value.isascii()
            or not value.isdecimal() or str(int(value)) != value or not 0 <= int(value) < 2**64):
        raise OperationRequestError('canonical operation uint64 decimal string required')
    return int(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise OperationRequestError('duplicate operation request key')
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        allow_nan=False).encode('ascii')


def _object(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= REQUEST_CAP:
        raise OperationRequestError('bounded original operation bytes required')
    try:
        return json.loads(raw.decode('ascii'), object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(OperationRequestError('nonfinite operation JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise OperationRequestError('bounded ASCII operation JSON required') from error


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise OperationRequestError('original lowercase operation SHA256 required')
    return value


@dataclass(frozen=True, slots=True, init=False)
class OperationRequest:
    """원문 SHA와 정규 사본은 별개다. 같은 key의 공백 변경도 원문 변경으로 판정한다."""
    raw_bytes: bytes
    canonical_bytes: bytes
    sha256: str
    service_instance: str
    request_id: str
    action: str
    issued_us: int
    expires_us: int

    def __init__(self, raw_bytes):
        if type(raw_bytes) is not bytes or not 0 < len(raw_bytes) <= REQUEST_CAP:
            raise OperationRequestError('original operation request bytes must fit4096')
        value = _object(raw_bytes)
        if type(value) is not dict or set(value) != FIELDS or value['profile'] != REQUEST_PROFILE:
            raise OperationRequestError('exact operation request profile/fields required')
        service, request = _uuid(value['service_instance']), _uuid(value['request_id'])
        if type(value['action']) is not str or value['action'] not in ('RUN', 'STANDBY'):
            raise OperationRequestError('operation action must be exact RUN or STANDBY')
        issued, expires = _decimal(value['issued_j_us']), _decimal(value['expires_j_us'])
        if not issued < expires <= issued + TRANSACTION_US:
            raise OperationRequestError('original operation transaction exceeds five seconds')
        for name, item in (('raw_bytes', raw_bytes), ('canonical_bytes', canonical(value)),
                ('sha256', sha256(raw_bytes).hexdigest()), ('service_instance', service),
                ('request_id', request), ('action', value['action']), ('issued_us', issued), ('expires_us', expires)):
            object.__setattr__(self, name, item)

    @property
    def fields(self): return json.loads(self.canonical_bytes)

    def __deepcopy__(self, memo): return self


def configuration(raw):
    """현재 S의 확인 설정을 새 M에 전달한다. 원 operator 요청을 재실행하지 않는다."""
    value = _object(raw)
    keys = {'profile', 'service_instance', 'config_revision', 'state', 'accepted_request_id', 'accepted_request_sha256'}
    if type(value) is not dict or set(value) != keys or value['profile'] != CONFIG_PROFILE:
        raise OperationRequestError('exact operation CONFIG fields required')
    _uuid(value['service_instance']); revision = _decimal(value['config_revision'])
    if value['state'] not in ('RUN', 'STANDBY'):
        raise OperationRequestError('exact operation CONFIG state required')
    if revision == 0:
        if value['state'] != 'RUN' or value['accepted_request_id'] is not None or value['accepted_request_sha256'] is not None:
            raise OperationRequestError('initial CONFIG must be original RUN without an invented request')
    else:
        _uuid(value['accepted_request_id']); _sha(value['accepted_request_sha256'])
    return value


def result_fields(request, *, status, reason, completed_us, revision):
    """원 요청 결과를 만든다. APPLIED의 실제 수락 시각은 원 transaction 안이어야 한다."""
    value = dict(profile=RESULT_PROFILE, service_instance=request.service_instance, request_id=request.request_id,
        request_sha256=request.sha256, action=request.action, status=status, reason=reason,
        issued_j_us=str(request.issued_us), expires_j_us=str(request.expires_us),
        completed_j_us=None if completed_us is None else str(completed_us),
        config_revision=None if revision is None else str(revision))
    return value


def operation_result(raw):
    """등록된 M의 원 적용/거절 또는 CONFIG ACK 형식만 검사한다. currentness는 owner가 대조한다."""
    value = _object(raw)
    if type(value) is not dict or value.get('profile') not in (RESULT_PROFILE, CONFIG_ACK_PROFILE):
        raise OperationRequestError('exact M operation result profile required')
    state_key = 'action' if value['profile'] == RESULT_PROFILE else 'state'
    keys = {'profile', 'service_instance', 'request_id', 'request_sha256', state_key, 'status', 'reason',
        'issued_j_us', 'expires_j_us', 'completed_j_us', 'config_revision'}
    if set(value) != keys or value[state_key] not in ('RUN', 'STANDBY') or value['status'] not in ('APPLIED', 'REJECTED', 'EXPIRED'):
        raise OperationRequestError('exact original M operation result fields required')
    _uuid(value['service_instance']); _uuid(value['request_id']); _sha(value['request_sha256'])
    issued, expires, completed = (_decimal(value[key]) for key in ('issued_j_us', 'expires_j_us', 'completed_j_us'))
    _decimal(value['config_revision'])
    if (not issued < expires <= issued+TRANSACTION_US or completed < issued
            or value['status'] == 'APPLIED' and completed >= expires
            or type(value['reason']) is not str or not 1 <= len(value['reason']) <= 64
            or any(not 33 <= ord(c) <= 126 for c in value['reason'])):
        raise OperationRequestError('original operation result time/reason differs')
    return value
