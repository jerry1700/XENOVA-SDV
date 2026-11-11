"""등록 S↔A/M의 PC 전용 유한 control 통로다. 영상·SDK/READY 권한은 운반하지 않는다.

packet32768B/header2048B, pending1, poll8을 고정한다. body는 원 owned bytes와 SHA를
보존하며 job/slot/FILLED/NN manifest/parser 의미는 실제 consumer가 검산한다. 원
peer는 S가 별도 등록한다. PC_FORWARD는 원 A/M packet과 S의 full-recv J를 보존하며
원 producer 사실을 새 S 사실로 바꾸지 않는다. 물리 DataLink/RAW parser와 별개다.
"""

from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
import json
import socket
import struct
import threading
import time
from types import MappingProxyType
from uuid import UUID

from ..simulation.pc_input import PcSourceBinding
from .lifecycle_messages import LifecyclePeer
from .local_envelope import _bounded_tree, _pairs


MAGIC = b'XNVPCCH1'
PROFILE = 'pc-actor-channel-1'
PREFIX = struct.Struct('<8sII')
PACKET_CAP = 32768
HEADER_CAP = 2048
U64_MAX = 2**64 - 1
ACTOR_KINDS = MappingProxyType({
    'A': frozenset(('PC_NN_START', 'PC_EARLY_META', 'PC_NN_RESULT_META', 'PC_SOURCE_FENCE')),
    'M': frozenset(('PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE', 'PC_SOURCE_FENCE')),
})
FIELDS = frozenset(('profile', 'service_instance', 'channel_epoch', 'worker_instance', 'worker_role',
    'release_sha256', 'pc_binding_sha256', 'sender', 'sequence', 'kind', 'request_id',
    'issued_j_us', 'expires_j_us', 'body_bytes', 'body_sha256', 'metadata', 'forward_received_j_us'))


class PcChannelError(ValueError):
    """등록 문맥·원 packet·순번 또는 유한 통로의 계약이 맞지 않는다."""


def _need(value, message):
    if not value:
        raise PcChannelError(message)


def _uint(value, *, positive=False):
    _need(type(value) is int and (1 if positive else 0) <= value <= U64_MAX, 'exact u64 integer required')
    return value


def _decimal(value, *, positive=False):
    _need(type(value) is str and 1 <= len(value) <= 20 and value.isascii() and value.isdecimal()
        and str(int(value)) == value, 'canonical u64 decimal required')
    return _uint(int(value), positive=positive)


def _uuid(value):
    _need(type(value) is str and len(value) == 36, 'canonical nonzero UUID required')
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise PcChannelError('invalid UUID') from error
    _need(str(parsed) == value and parsed.int != 0, 'canonical nonzero UUID required')


def _sha(value):
    _need(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value),
        'canonical SHA256 required')


def _canonical(value):
    try:
        _bounded_tree(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
            allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError) as error:
        raise PcChannelError('bounded exact-native header required') from error


def _header(raw):
    def constant(_):
        raise PcChannelError('nonfinite header')
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=constant)
        _bounded_tree(value, enforce_encoded_cost=False)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise PcChannelError('invalid PC header') from error
    _need(type(value) is dict and set(value) == FIELDS and _canonical(value) == raw,
        'exact canonical PC header required')
    return value


def _scope(header, peer, binding, *, sender):
    _need(type(peer) is LifecyclePeer and peer.role in ACTOR_KINDS and type(binding) is PcSourceBinding,
        'original registered A/M peer and PC binding required')
    _need((header['service_instance'], header['channel_epoch'], header['worker_instance'],
        header['worker_role'], header['release_sha256'], header['pc_binding_sha256'], header['sender']) ==
        (peer.service_instance, peer.channel_epoch, peer.worker_instance, peer.role, peer.release_sha256,
         binding.sha256, sender), 'PC packet differs from registered peer/release/descriptor')
    _need(binding.fields['service_instance'] == peer.service_instance, 'PC descriptor service differs')


def _decode(raw, *, allow_forward):
    _need(type(raw) is bytes and PREFIX.size < len(raw) <= PACKET_CAP, 'bounded immutable PC packet required')
    magic, header_size, body_size = PREFIX.unpack_from(raw)
    _need(magic == MAGIC and 0 < header_size <= HEADER_CAP
        and len(raw) == PREFIX.size + header_size + body_size, 'PC magic/length differs')
    header_raw = raw[PREFIX.size:PREFIX.size + header_size]
    h = _header(header_raw)
    _need(h['profile'] == PROFILE and type(h['profile']) is str, 'unsupported PC channel profile')
    for name in ('service_instance', 'channel_epoch', 'worker_instance', 'request_id'):
        _uuid(h[name])
    for name in ('release_sha256', 'pc_binding_sha256', 'body_sha256'):
        _sha(h[name])
    _need(type(h['worker_role']) is str and h['worker_role'] in ACTOR_KINDS
        and type(h['sender']) is str and h['sender'] in ('S', h['worker_role']), 'unsupported PC direction')
    _decimal(h['sequence'], positive=True)
    issued, expires = (_decimal(h[name]) for name in ('issued_j_us', 'expires_j_us'))
    _need(issued < expires <= issued + 5_000_000, 'PC packet exceeds original five-second control bound')
    _need(type(h['metadata']) is dict and type(h['body_bytes']) is int and h['body_bytes'] == body_size,
        'PC body descriptor differs')
    body = raw[PREFIX.size + header_size:]
    _need(sha256(body).hexdigest() == h['body_sha256'], 'PC original body SHA differs')
    if h['sender'] == 'S' and h['kind'] == 'PC_FORWARD':
        _need(allow_forward and not h['metadata'], 'nested or metadata-repacked PC forward forbidden')
        received = _decimal(h['forward_received_j_us'])
        original, _, _ = _decode(body, allow_forward=False)
        origin_role = 'M' if h['worker_role'] == 'A' else 'A'
        _need(original['sender'] == original['worker_role'] == origin_role
            and original['kind'] in ACTOR_KINDS[origin_role]
            and original['service_instance'] == h['service_instance']
            and original['pc_binding_sha256'] == h['pc_binding_sha256']
            and original['worker_instance'] != h['worker_instance']
            and original['channel_epoch'] != h['channel_epoch']
            and all(original[name] == h[name] for name in ('request_id', 'issued_j_us', 'expires_j_us'))
            and issued <= received and (received < expires or original['kind'] == 'PC_SOURCE_FENCE'),
            'PC forward changed original actor/source/request/lifetime')
    else:
        _need(h['forward_received_j_us'] is None, 'direct packet cannot claim forwarded receive time')
        allowed = ('PC_SOURCE_FENCE', 'PC_SOURCE_STATE') if h['sender'] == 'S' else ACTOR_KINDS[h['worker_role']]
        _need(type(h['kind']) is str and h['kind'] in allowed, 'unsupported direct PC kind/direction')
        if h['kind'] == 'PC_NN_START':
            _need(bool(body), 'original NN start receipt bytes required')
    return h, header_raw, body


@dataclass(frozen=True, slots=True)
class PcActorPacket:
    """원 control bytes만 보존하는 순수 codec다. parsed header는 항상 독립 사본이다."""

    raw: bytes
    _header_bytes: bytes = field(init=False, repr=False)
    body: bytes = field(init=False, repr=False)

    def __post_init__(self):
        _, encoded, body = _decode(self.raw, allow_forward=True)
        object.__setattr__(self, '_header_bytes', encoded)
        object.__setattr__(self, 'body', body)

    @property
    def header(self):
        return json.loads(self._header_bytes)

    @property
    def metadata(self):
        return self.header['metadata']

    @property
    def kind(self):
        return self.header['kind']

    @property
    def sequence(self):
        return int(self.header['sequence'])

    @property
    def request_id(self):
        return self.header['request_id']

    @property
    def issued_us(self):
        return int(self.header['issued_j_us'])

    @property
    def expires_us(self):
        return int(self.header['expires_j_us'])


def encode_pc_actor_packet(peer, binding, sender, kind, metadata, body=b'', *, sequence,
                           request_id, issued_us, expires_us, forward_received_j_us=None):
    """형식 packet만 만든다. 명시 PC_FORWARD도 실제 수신 사실은 별도 owner가 확인한다."""
    _need(type(peer) is LifecyclePeer and type(binding) is PcSourceBinding, 'exact peer/PC binding required')
    _need(type(body) is bytes and len(body) < PACKET_CAP, 'owned bounded control body required')
    _uint(sequence, positive=True); _uint(issued_us); _uint(expires_us)
    if forward_received_j_us is not None:
        _uint(forward_received_j_us)
    h = dict(profile=PROFILE, service_instance=peer.service_instance, channel_epoch=peer.channel_epoch,
        worker_instance=peer.worker_instance, worker_role=peer.role, release_sha256=peer.release_sha256,
        pc_binding_sha256=binding.sha256, sender=sender, sequence=str(sequence), kind=kind,
        request_id=request_id, issued_j_us=str(issued_us), expires_j_us=str(expires_us),
        body_bytes=len(body), body_sha256=sha256(body).hexdigest(), metadata=metadata,
        forward_received_j_us=None if forward_received_j_us is None else str(forward_received_j_us))
    _scope(h, peer, binding, sender=sender)
    encoded = _canonical(h)
    _need(len(encoded) <= HEADER_CAP and PREFIX.size + len(encoded) + len(body) <= PACKET_CAP,
        'PC header/packet cap exceeded; no splitting or truncation')
    return PcActorPacket(PREFIX.pack(MAGIC, len(encoded), len(body)) + encoded + body)


@dataclass(frozen=True, slots=True, init=False)
class PcActorReceived:
    packet: PcActorPacket
    received_j_us: int
    _link_token: object = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError('original receive fact requires PcActorLink.poll')


@dataclass(frozen=True, slots=True, init=False)
class PcActorSent:
    packet: PcActorPacket
    started_us: int
    completed_us: int
    _link_token: object = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError('original full-send fact requires PcActorLink.flush')


@dataclass(frozen=True, slots=True, init=False)
class PcActorForward:
    relay_packet: PcActorPacket
    original_packet: PcActorPacket
    origin_peer: LifecyclePeer
    received_j_us: int

    def __init__(self, *args, **kwargs):
        raise TypeError('use decode_forward with separately registered origin peer')


def _fact(cls, **fields):
    result = object.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(result, name, value)
    return result


def decode_forward(packet, origin_peer, binding):
    """원 peer를 wire에서 등록하지 않는다. 전달된 원 producer bytes/J만 반환한다."""
    _need(type(packet) is PcActorPacket and packet.kind == 'PC_FORWARD', 'exact PC_FORWARD required')
    _need(type(origin_peer) is LifecyclePeer and type(binding) is PcSourceBinding,
        'separately registered origin peer and PC descriptor required')
    original = PcActorPacket(packet.body)
    _scope(original.header, origin_peer, binding, sender=origin_peer.role)
    _need(packet.header['pc_binding_sha256'] == binding.sha256, 'forward descriptor differs')
    return _fact(PcActorForward, relay_packet=packet, original_packet=original,
        origin_peer=origin_peer, received_j_us=int(packet.header['forward_received_j_us']))


def pc_actor_pair():
    """S가 등록 peer 하나에 배분할 unnamed socket 쌍이다. 자동 등록/listen/reconnect는 없다."""
    if not hasattr(socket, 'SOCK_SEQPACKET'):
        raise OSError('PC actor control requires AF_UNIX SOCK_SEQPACKET')
    endpoints = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        for endpoint in endpoints:
            endpoint.setblocking(False)
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * PACKET_CAP)
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * PACKET_CAP)
    except BaseException:
        for endpoint in endpoints:
            endpoint.close()
        raise
    return endpoints


def _static_delivery(kind, body=b''):
    # 신원·철회 통지는 주행 사용권이 아니다. 오래된 frame과 달리 반드시 전달한다.
    return kind in ('PC_NN_START', 'PC_SOURCE_STATE', 'PC_SOURCE_FENCE') or (
        kind == 'PC_FORWARD' and PcActorPacket(body).kind == 'PC_SOURCE_FENCE')


class PcActorLink:
    """endpoint 한 끝의 단일 thread owner다. send는 예약, flush만 actual full-send다.

    원 request/issued/E는 필수다. busy는 새 순번·원 시각·nonce를 만들지 않는다.
    expired RX는 원 순번 HWM을 보존한 채 버리고, malformed/foreign/replay는 fault다.
    실제 actor가 lifecycle·BIND·현재 source·각 body 의미를 별도로 검산해야 한다.
    """

    def __init__(self, endpoint, peer, owner_role, binding, *, clock=lambda: time.monotonic_ns() // 1000):
        _need(type(endpoint) is socket.socket and endpoint.family == socket.AF_UNIX
            and endpoint.type == getattr(socket, 'SOCK_SEQPACKET', -1), 'actual Unix seqpacket endpoint required')
        _need(type(peer) is LifecyclePeer and peer.role in ACTOR_KINDS
            and type(owner_role) is str and owner_role in ('S', peer.role)
            and type(binding) is PcSourceBinding and callable(clock), 'registered peer/owner/PC binding required')
        for value in (peer.service_instance, peer.channel_epoch, peer.worker_instance):
            _uuid(value)
        _need(peer.service_instance == binding.fields['service_instance'], 'registered PC service differs')
        endpoint.setblocking(False)
        self._endpoint, self._peer, self._role, self._binding, self._clock = endpoint, peer, owner_role, binding, clock
        self._owner = threading.current_thread()
        self._token = object()
        self._pending, self._next, self._highwater, self._last = None, 1, 0, None
        self._send_unknown, self._eof, self._rx_fault = False, False, False
        self._last_now = None
        self._closed = False
        self.counts = Counter()

    @property
    def endpoint(self): return self._endpoint

    @property
    def peer(self): return self._peer

    @property
    def binding(self): return self._binding

    @property
    def owner_role(self): return self._role

    @property
    def closed(self): return self._closed

    def _owned(self):
        if threading.current_thread() is not self._owner:
            raise RuntimeError('PC actor endpoint belongs to its original owner thread')

    def _open(self):
        self._owned()
        if self._closed or self._eof:
            raise EOFError('PC actor channel is closed')
        if self._rx_fault:
            raise PcChannelError('PC actor receive channel faulted')

    def _now(self):
        value = _uint(self._clock())
        _need(self._last_now is None or self._last_now <= value, 'local PC channel clock regressed')
        self._last_now = value
        return value

    @property
    def pending(self):
        self._owned()
        return self._pending

    def _reserve(self, kind, metadata, body, *, request_id, issued_us, expires_us, received=None):
        self._open()
        if self._pending is not None:
            self.counts['busy_no_effect'] += 1
            return None
        if self._next > U64_MAX:
            raise OverflowError('PC actor message sequence exhausted')
        now = self._now()
        _need(_uint(issued_us) <= now and issued_us < _uint(expires_us), 'outgoing original PC packet has invalid time order')
        if now>=expires_us and not _static_delivery(kind,body):
            return None
        packet = encode_pc_actor_packet(self._peer, self._binding, self._role, kind, metadata, body,
            sequence=self._next, request_id=request_id, issued_us=issued_us, expires_us=expires_us,
            forward_received_j_us=received)
        self._pending = packet
        self._next += 1
        return packet

    def send(self, kind, metadata, body=b'', *, request_id, issued_us, expires_us):
        self._open()
        if self._pending is not None:
            self.counts['busy_no_effect'] += 1
            return None
        _need(kind != 'PC_FORWARD', 'forward requires original receive ticket and origin link')
        return self._reserve(kind, metadata, body, request_id=request_id, issued_us=issued_us, expires_us=expires_us)

    def discard_expired_work(self, packet):
        """미송신 M 프레임 사실과 S의 해당 relay만 버린다. 순번/사용권은 만들지 않는다.

        RAW/DB 반환과 달리 ACK/parse 완료는 원 frame E 뒤 새 사용권이 없다. 송신 결과가
        불명인 packet은 이 경로로 지우지 않으며, caller는 원 pending 참조를 제공해야 한다.
        """
        self._open()
        _need(type(packet) is PcActorPacket and self._pending is packet,
            'exact original pending frame work required')
        local = self._role == 'M' and packet.kind in ('PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE')
        local = local or self._role == 'A' and packet.kind in ('PC_EARLY_META', 'PC_NN_RESULT_META')
        relay = (self._role == 'S' and packet.kind == 'PC_FORWARD'
            and PcActorPacket(packet.body).kind in ('PC_EARLY_META', 'PC_NN_RESULT_META',
                'PC_SNAPSHOT_ACK', 'PC_PARSE_COMPLETE'))
        _need(local or relay, 'only original M frame facts or their S frame relay may be discarded')
        _need(not self._send_unknown, 'unknown PC actor send cannot be discarded')
        _need(self._now() >= packet.expires_us, 'original frame work has not expired')
        self._pending = None
        self.counts['expired_work_discarded'] += 1
        return packet

    def flush(self):
        self._open()
        packet = self._pending
        if packet is None:
            return None
        if self._send_unknown:
            raise PcChannelError('PC actor send outcome is unknown')
        started = self._now()
        _need(packet.issued_us<=started,'pending PC packet has a future issue time')
        if started>=packet.expires_us and not _static_delivery(packet.kind,packet.body):
            return None
        try:
            size = self._endpoint.send(packet.raw)
        except BlockingIOError:
            return None
        except OSError:
            self._send_unknown = True
            raise
        if size != len(packet.raw):
            self._send_unknown = True
            raise PcChannelError('partial PC seqpacket send is unknown')
        # bytes가 송신된 뒤 clock 검산이 실패해도 원 packet을 다시 보내지 않는다.
        self._send_unknown = True
        completed = self._now()
        self._send_unknown = False
        self._pending = None
        self.counts['sent'] += 1
        return _fact(PcActorSent, packet=packet, started_us=started, completed_us=completed, _link_token=self._token)

    def poll(self):
        self._open()
        result = []
        for _ in range(8):
            try:
                raw, ancillary, flags, _ = self._endpoint.recvmsg(PACKET_CAP + 1, 0)
            except BlockingIOError:
                break
            except OSError:
                self._rx_fault = True
                raise
            if not raw:
                self._eof = True
                if result:
                    break
                raise EOFError('registered PC actor peer closed')
            try:
                completed = self._now()
            except PcChannelError:
                self._rx_fault = True
                raise
            try:
                _need(not ancillary and not flags & (getattr(socket, 'MSG_TRUNC', 0)
                    | getattr(socket, 'MSG_CTRUNC', 0)) and len(raw) <= PACKET_CAP,
                    'truncated/ancillary/oversized PC datagram')
                packet = PcActorPacket(raw)
                _scope(packet.header, self._peer, self._binding,
                    sender=self._peer.role if self._role == 'S' else 'S')
                sequence = packet.sequence
                if sequence <= self._highwater:
                    if sequence == self._highwater and raw == self._last:
                        self.counts['duplicate_no_effect'] += 1
                        continue
                    raise PcChannelError('PC actor sequence regressed or changed original bytes')
                self._highwater, self._last = sequence, raw
                _need(packet.issued_us <= completed, 'future PC packet issue time')
                if completed >= packet.expires_us and not _static_delivery(packet.kind,packet.body):
                    self.counts['expired'] += 1
                    continue
                self.counts['received'] += 1
                result.append(_fact(PcActorReceived, packet=packet, received_j_us=completed, _link_token=self._token))
            except (PcChannelError, ValueError):
                self._rx_fault = True
                raise
        return tuple(result)

    def require_received(self, received):
        """이 endpoint에서 실제 full-recv한 원 사실만 돌려준다. 현재 E/의미 승인은 별도다.

        relay/slot/source consumer는 외부에서 구성한 packet을 실제 수신으로 받아들이지
        않는다. EOF 뒤에도 원 수신 사실 검산은 가능하며 사용권을 다시 열지는 않는다.
        """
        self._owned()
        _need(type(received) is PcActorReceived and received._link_token is self._token,
            'actual receive fact belongs to another PC endpoint')
        _scope(received.packet.header, self.peer, self.binding,
            sender=self.peer.role if self.owner_role == 'S' else 'S')
        _need(received.packet.issued_us <= received.received_j_us
            and (received.received_j_us < received.packet.expires_us
                or _static_delivery(received.packet.kind,received.packet.body)),
            'actual receive fact differs from its original lifetime')
        return received

    def require_sent(self, sent):
        """이 endpoint의 원 full-send 사실을 대조한다. 기한 내 원격 수신을 뜻하지 않는다.

        completed_us는 syscall 뒤 local clock 상한이다. peer가 먼저 수신한 경우보다
        뒤에 측정될 수 있으므로 caller는 서로 다른 사건의 두 상한을 인과관계로 쓰지 않는다.
        """
        self._owned()
        _need(type(sent) is PcActorSent and sent._link_token is self._token,
            'actual send fact belongs to another PC endpoint')
        _scope(sent.packet.header, self.peer, self.binding, sender=self.owner_role)
        _need(sent.packet.issued_us <= sent.started_us <= sent.completed_us
            and (sent.started_us < sent.packet.expires_us
                or _static_delivery(sent.packet.kind,sent.packet.body)),
            'actual send fact changed its original local bounds')
        return sent

    def abort(self):
        """실패 endpoint만 닫고 원 pending 진단은 남긴다. 순번/lease를 환불하지 않는다."""
        self._owned()
        if not self._closed:
            self._endpoint.close()
            self._closed = True
        return self._pending

    def close(self):
        self._owned()
        if not self._closed:
            self._endpoint.close()
            self._closed = True
        self._pending = None


def forward_received(received, target_link, *, origin_link):
    """S가 실제 받은 원 actor bytes만 다른 등록 worker에 전달한다. source 승인은 아니다."""
    _need(type(target_link) is PcActorLink and type(origin_link) is PcActorLink, 'actual PC links required')
    target_link._open()
    if target_link._pending is not None:
        target_link.counts['busy_no_effect'] += 1
        return None
    origin_link._open()
    origin_link.require_received(received)
    _need(origin_link.owner_role == target_link.owner_role == 'S'
        and origin_link is not target_link and origin_link.peer.role != target_link.peer.role
        and origin_link.binding.canonical_bytes == target_link.binding.canonical_bytes,
        'forward requires original S receive ticket and distinct registered A/M links')
    original = received.packet
    _scope(original.header, origin_link.peer, origin_link.binding, sender=origin_link.peer.role)
    return target_link._reserve('PC_FORWARD', {}, original.raw, request_id=original.request_id,
        issued_us=original.issued_us, expires_us=original.expires_us, received=received.received_j_us)
