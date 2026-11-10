"""PC 관측 descriptor 전용 RAW BIND/ACK 진술을 검사한다.

기존 LocalEnvelope·일반 SHM scope를 쓰지만 physical RawBinding으로 변환하지 않는다.
peer/A endpoint/ArenaSpec은 S가 독립 고정한 원 문맥이다. 이 순수 검사는 실제 등록·
SHM attach·송수신·NN start·D bootstrap·Identity/READY 승인을 대신하지 않는다.
그 사건의 증거가 없는 S adapter는 NOT_READY를 유지해야 한다.
"""

from dataclasses import dataclass
import re
from uuid import UUID

from ..simulation.pc_input import PcSourceBinding
from .lifecycle_messages import LifecyclePeer
from .lifecycle_policy import validate_state
from .local_envelope import StructuralPacket
from .native_resources import ArenaSpec
from .raw_binding_messages import NEGATIVE_REASONS, POSITIVE_STATES
from .raw_leases import Endpoint, RawScope


U64_MAX = 2**64 - 1


class PcBindMessageError(ValueError):
    """PC descriptor/등록 문맥 또는 원 BIND/ACK 수명이 맞지 않는다."""


def _exact(value, expected):
    if type(value) is not expected:
        raise PcBindMessageError('expected exact ' + expected.__name__)


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        raise PcBindMessageError('canonical nonzero peer UUID required')
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise PcBindMessageError('invalid peer UUID') from error
    if str(parsed) != value or not parsed.int:
        raise PcBindMessageError('canonical nonzero peer UUID required')


def _u64(value):
    if type(value) is not int or not 0 <= value <= U64_MAX:
        raise PcBindMessageError('exact u64 integer required')
    return value


def _decimal(value, *, positive=False):
    if (type(value) is not str or not 1 <= len(value) <= 20
            or re.fullmatch('0|[1-9][0-9]*', value) is None):
        raise PcBindMessageError('canonical u64 decimal string required')
    number = int(value)
    if number > U64_MAX or positive and number == 0:
        raise PcBindMessageError('u64 string outside range')
    return number


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise PcBindMessageError('missing or unknown PC RAW payload fields')


@dataclass(frozen=True)
class PcBindContext:
    """S의 원 descriptor와 일반 RAW arena 문맥이다. OS 자원 실존을 관측하지 않는다.

    PC source UUID/session과 A writer UUID는 서로 다른 식별자다. descriptor에 가짜
    acquisition/device 필드를 만들지 않고 원 RawScope.writer로 A를 결박한다.
    """

    peer: LifecyclePeer
    acquisition: Endpoint
    expected_binding: PcSourceBinding
    scope: RawScope
    arena_spec: ArenaSpec

    def __post_init__(self):
        _exact(self.peer, LifecyclePeer)
        _exact(self.acquisition, Endpoint)
        _exact(self.expected_binding, PcSourceBinding)
        _exact(self.scope, RawScope)
        _exact(self.arena_spec, ArenaSpec)
        peer, acquisition, scope, spec = self.peer, self.acquisition, self.scope, self.arena_spec
        for value in (peer.service_instance, peer.worker_instance, peer.channel_epoch,
                      acquisition.worker_instance, acquisition.channel_epoch):
            _uuid(value)
        if peer.role not in ('A', 'M'):
            raise PcBindMessageError('PC RAW targets registered A or M only')
        if peer.role == 'A':
            if (peer.worker_instance, peer.channel_epoch) != (acquisition.worker_instance, acquisition.channel_epoch):
                raise PcBindMessageError('A target differs from its registered writer endpoint')
        elif peer.worker_instance == acquisition.worker_instance or peer.channel_epoch == acquisition.channel_epoch:
            raise PcBindMessageError('M reader must differ from A worker and channel')
        if (self.expected_binding.fields['service_instance'] != peer.service_instance
                or scope.service_instance != peer.service_instance or scope.writer != acquisition
                or scope.raw_binding_sha256 != self.expected_binding.sha256):
            raise PcBindMessageError('PC descriptor/service/writer differs from original RAW scope')
        if ((scope.arena_id, scope.arena_name, scope.arena_size_bytes, scope.arena_role)
                != (spec.arena_id, spec.name, spec.size_bytes, spec.role) or spec.role != 'RAW'):
            raise PcBindMessageError('PC RAW scope differs from original registered ArenaSpec')


def _packet_fields(packet, context, *, ack):
    _exact(packet, StructuralPacket)
    _exact(context, PcBindContext)
    value, peer = packet.fields, context.peer
    if (value['kind'] != ('BIND_ACK' if ack else 'BIND') or value['binding_domain'] != 'RAW'
            or value['service_instance'] != peer.service_instance or value['channel_epoch'] != peer.channel_epoch
            or value['sender_instance'] != (peer.worker_instance if ack else peer.service_instance)):
        raise PcBindMessageError('PC RAW kind/direction/current registered peer differs')
    revision = _decimal(value['binding_revision'], positive=True)
    issued, expires = (_decimal(value[name]) for name in ('issued_j_us', 'expires_j_us'))
    if issued >= expires:
        raise PcBindMessageError('PC RAW statement requires its original nonempty lifetime')
    return value, revision, issued, expires


class _PacketAccess:
    @property
    def raw(self):
        return self.packet.raw_bytes

    @property
    def fields(self):
        return self.packet.fields

    @property
    def payload(self):
        return self.packet.payload


@dataclass(frozen=True, init=False)
class PcBindMessage(_PacketAccess):
    """원 PC BIND 진술이다. 현재 now·실송신·순번 HWM은 실제 actor가 검산한다."""

    packet: StructuralPacket
    context: PcBindContext
    binding: PcSourceBinding
    binding_revision: int
    message_sequence: int
    issued_us: int
    expires_us: int
    request_id: str
    cap_us: int | None

    def __init__(self, packet, context, cap_us=None):
        value, revision, issued, expires = _packet_fields(packet, context, ack=False)
        payload = value['payload']
        _keys(payload, ('binding_revision', 'binding_sha256', 'binding'))
        if _decimal(payload['binding_revision'], positive=True) != revision:
            raise PcBindMessageError('PC BIND payload/outer revision differs')
        if expires - issued > 5_000_000:
            raise PcBindMessageError('PC BIND exceeds original issued plus five seconds')
        if cap_us is not None and expires > _u64(cap_us):
            raise PcBindMessageError('PC BIND exceeds external original cap')
        try:
            binding = PcSourceBinding(payload['binding'])
        except (ValueError, TypeError) as error:
            raise PcBindMessageError('PC BIND requires exact PC descriptor') from error
        if binding.canonical_bytes != context.expected_binding.canonical_bytes:
            raise PcBindMessageError('PC BIND differs from independently pinned descriptor')
        if type(payload['binding_sha256']) is not str or payload['binding_sha256'] != binding.sha256:
            raise PcBindMessageError('PC BIND SHA must cover its exact canonical descriptor')
        for name, item in dict(packet=packet, context=context, binding=binding, binding_revision=revision,
                message_sequence=_decimal(value['message_sequence'], positive=True), issued_us=issued,
                expires_us=expires, request_id=value['request_id'], cap_us=cap_us).items():
            object.__setattr__(self, name, item)


@dataclass(frozen=True, init=False)
class PcBindAck(_PacketAccess):
    """원 PC BIND의 ACK 진술이다. accepted=True도 NN/Identity/READY 사실이 아니다."""

    packet: StructuralPacket
    original_bind: PcBindMessage
    context: PcBindContext
    binding: PcSourceBinding
    binding_revision: int
    message_sequence: int
    issued_us: int
    expires_us: int
    request_id: str
    accepted: bool
    state: str
    reason: str | None

    def __init__(self, packet, original_bind):
        _exact(original_bind, PcBindMessage)
        context = original_bind.context
        value, revision, issued, expires = _packet_fields(packet, context, ack=True)
        payload = value['payload']
        _keys(payload, ('binding_revision', 'binding_sha256', 'accepted', 'state', 'reason'))
        if (revision != original_bind.binding_revision or _decimal(payload['binding_revision'], positive=True) != revision
                or type(payload['binding_sha256']) is not str or payload['binding_sha256'] != original_bind.binding.sha256
                or value['request_id'] != original_bind.request_id):
            raise PcBindMessageError('PC ACK changed original BIND identity/revision/SHA')
        if not original_bind.issued_us <= issued < expires <= original_bind.expires_us:
            raise PcBindMessageError('PC ACK must remain inside original BIND lifetime')
        accepted, state, reason = (payload[name] for name in ('accepted', 'state', 'reason'))
        _exact(accepted, bool)
        _exact(state, str)
        try:
            validate_state(context.peer.role, state)
        except ValueError as error:
            raise PcBindMessageError('unsupported PC role state') from error
        if accepted:
            if reason is not None or state not in POSITIVE_STATES[context.peer.role]:
                raise PcBindMessageError('positive PC ACK requires active state and null reason')
        elif type(reason) is not str or reason not in NEGATIVE_REASONS:
            raise PcBindMessageError('negative PC ACK requires an existing explicit refusal reason')
        for name, item in dict(packet=packet, original_bind=original_bind, context=context,
                binding=original_bind.binding, binding_revision=revision,
                message_sequence=_decimal(value['message_sequence'], positive=True), issued_us=issued,
                expires_us=expires, request_id=value['request_id'], accepted=accepted, state=state, reason=reason).items():
            object.__setattr__(self, name, item)


def parse_pc_bind(packet, context, cap_us=None):
    return PcBindMessage(packet, context, cap_us)


def parse_pc_bind_ack(packet, original_bind):
    return PcBindAck(packet, original_bind)
