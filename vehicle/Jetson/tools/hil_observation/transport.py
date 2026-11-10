"""개발 전용 PC 관측/clock 원문 framing. 등록·시계 승인·M 변환은 하지 않는다.

caller가 연결한 nonblocking stream socket 하나를 소유/close한다. 이 모듈은 listen,
reconnect, thread를 만들지 않는다. 한 owner가 pump하며 sender는 active1+latest1,
receiver는 partial1만 보유한다. 반환 DTO의 추가 보유 수명은 caller 책임이다.
수신 중 최대960000B buffer와 완료 payload 사본, 최대64KiB scratch가 겹칠 수 있다.
Python 객체/allocator/RSS의 정확한 상한을 주장하지 않는다.
"""

import socket
import struct
import json
import hashlib


PROFILE = 'hil-observation-1'
MAGIC = b'XNVAHIL1'
VERSION = 1
PREFIX = struct.Struct('!8sBBIIQ')
HEADER_CAP = 16 * 1024
CLOCK_CAP = 1024
CLOCK_U63_MAX = (1 << 63) - 1
CLOCK_MAX_PROBE_US = 200_000
MAX_IO = 64 * 1024
RGB_BYTES = 640 * 360 * 3 // 2
DEPTH_BYTES = 640 * 480 * 2
FRAME_BYTES = RGB_BYTES + DEPTH_BYTES
KINDS = {'FRAME': 1, 'CLOCK_PROBE': 2, 'CLOCK_REPLY': 3}
SCOPE_KEYS = {'source_id', 'session_id', 'source_descriptor_sha256', 'calibration_sha256',
              'motion_bundle_sha256', 'pc_clock_epoch'}


class TransportError(ValueError):
    """연결을 계속 해석할 수 없다. caller는 실제 socket을 닫아야 한다."""


class TransportEOF(TransportError):
    """EOF의 partial 원문은 폐기하며 다른 packet으로 재동기화하지 않는다."""


class TransportTimeout(TransportError):
    """caller가 고정한 원 local deadline을 넘었다."""


def _require(value, message):
    if not value:
        raise TransportError(message)


def _uint(value):
    _require(type(value) is int and 0 <= value < 2**64, 'u64 integer required')
    return value


def _decimal(value):
    _require(type(value) is str and 1 <= len(value) <= 19
        and all('0' <= char <= '9' for char in value)
        and (len(value) == 1 or value[0] != '0'), 'canonical decimal clock value required')
    result = int(value)
    _require(result <= CLOCK_U63_MAX, 'clock u63 bound exceeded')
    return result


def _hex(value, size):
    _require(type(value) is str and len(value) == size
        and all(char in '0123456789abcdef' for char in value), 'canonical hex required')
    return value


def _uuid(value):
    _require(type(value) is str and len(value) == 36 and all(value[i] == '-' for i in (8, 13, 18, 23)),
        'canonical source UUID required')
    digits = value.replace('-', '')
    _hex(digits, 32)
    _require(len(digits) == 32 and any(char != '0' for char in digits), 'nonzero source UUID required')
    return value


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise TransportError('invalid canonical header') from error


def _json(raw, cap):
    _require(type(raw) is bytes and 0 < len(raw) <= cap, 'bounded immutable JSON bytes required')
    def pairs(rows):
        value = {}
        for key, item in rows:
            _require(key not in value, 'duplicate JSON key')
            value[key] = item
        return value
    def constant(_):
        raise TransportError('nonfinite JSON value')
    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError, OverflowError) as error:
        raise TransportError('invalid JSON header') from error
    _require(type(value) is dict and _canonical(value) == raw, 'noncanonical JSON rejected')
    return value


def _keys(value, names):
    _require(type(value) is dict and set(value) == set(names), 'missing or unknown protocol field')


def _interval(value):
    _require(type(value) in (tuple, list) and len(value) == 2, 'original PC endpoint pair required')
    low, high = map(_uint, value)
    _require(low <= high, 'reversed original PC interval')
    return low, high


class _Frozen:
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError('immutable transport DTO')


class SessionScope(_Frozen):
    """원 producer/session과 descriptor·두 보정 ID를 pin한다. 수락 권한은 없다."""
    __slots__ = ('_raw',)

    def __init__(self, source_id, session_id, source_descriptor_sha256, calibration_sha256,
                 motion_bundle_sha256, pc_clock_epoch):
        value = dict(source_id=_uuid(source_id), session_id=_uuid(session_id),
            source_descriptor_sha256=_hex(source_descriptor_sha256, 64),
            calibration_sha256=_hex(calibration_sha256, 64), motion_bundle_sha256=_hex(motion_bundle_sha256, 64),
            pc_clock_epoch=_uuid(pc_clock_epoch))
        object.__setattr__(self, '_raw', _canonical(value))

    @property
    def fields(self):
        return json.loads(self._raw)

    @property
    def raw_bytes(self):
        return self._raw


class _Packet(_Frozen):
    __slots__ = ()

    @property
    def header_bytes(self):
        return self._header

    @property
    def fields(self):
        return json.loads(self._header)

    @property
    def wire_parts(self):
        """검산된 immutable parts다. 전체960000B를 join한 사본은 만들지 않는다."""
        return self._parts


def _finish_packet(packet, header, payloads):
    raw = _canonical(header)
    _require(len(raw) <= HEADER_CAP, 'control header exceeds16KiB')
    prefix = PREFIX.pack(MAGIC, VERSION, KINDS[packet.kind], len(raw), sum(map(len, payloads)), packet.sequence)
    object.__setattr__(packet, '_header', raw)
    object.__setattr__(packet, '_parts', (prefix, raw) + payloads)
    object.__setattr__(packet, 'fingerprint', hashlib.sha256(prefix + raw).hexdigest())


class ObservationFrame(_Packet):
    """원 PC4endpoint·render시각·E와 두 payload만 보유한다. M 입력 변환은 제공하지 않는다."""
    __slots__ = ('scope', 'sequence', 'kind', 'rgb', 'depth', 'rgb_pc_interval', 'depth_pc_interval',
                 'render_completed_pc_us', 'expires_pc_us', '_header', '_parts', 'fingerprint')

    def __init__(self, scope, sequence, *, rgb_pc_interval, depth_pc_interval,
                 render_completed_pc_us, expires_pc_us, rgb, depth):
        _require(type(scope) is SessionScope, 'original SessionScope required')
        _require(type(rgb) is bytes and len(rgb) == RGB_BYTES
            and type(depth) is bytes and len(depth) == DEPTH_BYTES, 'fixed owned NV12/RIGHT RAW16LE required')
        rgb_time, depth_time = _interval(rgb_pc_interval), _interval(depth_pc_interval)
        completed, expires = _uint(render_completed_pc_us), _uint(expires_pc_us)
        _require(max(rgb_time[1], depth_time[1]) <= completed < expires, 'original PC completion/E order required')
        for name, value in dict(scope=scope, sequence=_uint(sequence), kind='FRAME', rgb=rgb, depth=depth,
                rgb_pc_interval=rgb_time, depth_pc_interval=depth_time,
                render_completed_pc_us=completed, expires_pc_us=expires).items():
            object.__setattr__(self, name, value)
        header = dict(profile=PROFILE, kind=self.kind, sequence=self.sequence, scope=scope.fields,
            rgb=dict(width=640, height=360, encoding='NV12', pc_interval_us=list(rgb_time), sha256=hashlib.sha256(rgb).hexdigest()),
            depth=dict(width=640, height=480, encoding='RAW16LE', pc_interval_us=list(depth_time), sha256=hashlib.sha256(depth).hexdigest()),
            render_completed_pc_us=completed, expires_pc_us=expires)
        _finish_packet(self, header, (rgb, depth))


def _clock_probe(fields, scope):
    _keys(fields, ('profile', 'source', 'sequence', 'nonce', 'j1_us', 'expires_j_us'))
    _require(fields['profile'] == 'hil-observation-clock-probe-1', 'clock probe profile differs')
    value = scope.fields
    _keys(fields['source'], ('source_id', 'pc_session', 'jetson_session'))
    _require(fields['source'] == dict(source_id=value['source_id'], pc_session=value['pc_clock_epoch'],
        jetson_session=value['session_id']), 'original clock source differs')
    sequence = _decimal(fields['sequence'])
    _require(sequence >= 1, 'positive original clock sequence required')
    nonce = _hex(fields['nonce'], 32)
    _require(any(char != '0' for char in nonce), 'nonzero original clock nonce required')
    started, expires = _decimal(fields['j1_us']), _decimal(fields['expires_j_us'])
    _require(started < expires <= started + CLOCK_MAX_PROBE_US, 'original bounded J probe deadline required')
    return sequence


class ClockPacket(_Packet):
    """Oak clock의 canonical 원 bytes를 보존한다. J/P 교차시각을 임의 비교·승인하지 않는다."""
    __slots__ = ('scope', 'sequence', 'kind', 'clock_raw_bytes', '_header', '_parts', 'fingerprint')

    def __init__(self, scope, kind, raw_clock_bytes):
        _require(type(scope) is SessionScope and kind in ('CLOCK_PROBE', 'CLOCK_REPLY'), 'typed clock envelope required')
        fields = _json(raw_clock_bytes, CLOCK_CAP)
        if kind == 'CLOCK_PROBE':
            sequence = _clock_probe(fields, scope)
        else:
            _keys(fields, ('profile', 'probe', 'p2_us', 'p3_us'))
            _require(fields['profile'] == 'hil-observation-clock-reply-1', 'clock reply profile differs')
            sequence = _clock_probe(fields['probe'], scope)
            p2, p3 = _decimal(fields['p2_us']), _decimal(fields['p3_us'])
            _require(p2 <= p3 <= CLOCK_U63_MAX - 1, 'original bounded PC P2/P3 order required')
        for name, value in dict(scope=scope, sequence=sequence, kind=kind, clock_raw_bytes=raw_clock_bytes).items():
            object.__setattr__(self, name, value)
        _finish_packet(self, dict(profile=PROFILE, kind=kind, sequence=sequence, scope=scope.fields,
            clock_raw=raw_clock_bytes.decode('ascii')), ())

    @property
    def clock_fields(self):
        return json.loads(self.clock_raw_bytes)


class ReceivedPacket(_Frozen):
    """완전수신 직후 local stamp다. PC P2/J J4에 사용하며 capture 시각으로 전용하지 않는다."""
    __slots__ = ('packet', 'received_local_us')

    def __init__(self, packet, received_local_us):
        _require(type(packet) in (ObservationFrame, ClockPacket), 'typed original received packet required')
        object.__setattr__(self, 'packet', packet)
        object.__setattr__(self, 'received_local_us', _uint(received_local_us))


def _header(raw, kind, sequence, scope):
    value = _json(raw, HEADER_CAP)
    common = {'profile', 'kind', 'sequence', 'scope'}
    _keys(value, common | ({'rgb', 'depth', 'render_completed_pc_us', 'expires_pc_us'}
        if kind == 'FRAME' else {'clock_raw'}))
    _require(value['profile'] == PROFILE and value['kind'] == kind and type(value['sequence']) is int
        and value['sequence'] == sequence, 'prefix/header kind or sequence differs')
    _keys(value['scope'], SCOPE_KEYS)
    _require(value['scope'] == scope.fields, 'foreign source/session/descriptor/calibration')
    if kind == 'FRAME':
        for name, height, encoding in (('rgb', 360, 'NV12'), ('depth', 480, 'RAW16LE')):
            row = value[name]
            _keys(row, ('width', 'height', 'encoding', 'pc_interval_us', 'sha256'))
            _require(type(row['width']) is int and row['width'] == 640
                and type(row['height']) is int and row['height'] == height and row['encoding'] == encoding,
                'fixed image shape/format differs')
            _interval(row['pc_interval_us']); _hex(row['sha256'], 64)
        _uint(value['render_completed_pc_us']); _uint(value['expires_pc_us'])
        _require(max(value['rgb']['pc_interval_us'][1], value['depth']['pc_interval_us'][1])
            <= value['render_completed_pc_us'] < value['expires_pc_us'], 'PC header time order differs')
    else:
        _require(type(value['clock_raw']) is str, 'original clock ASCII bytes required')
        try:
            packet = ClockPacket(scope, kind, value['clock_raw'].encode('ascii'))
        except UnicodeError as error:
            raise TransportError('non-ASCII clock bytes') from error
        _require(packet.sequence == sequence and packet.header_bytes == raw, 'original clock envelope differs')
    return value


def _endpoint(endpoint):
    _require(isinstance(endpoint, socket.socket) and endpoint.getblocking() is False
        and endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM,
        'caller-owned nonblocking stream socket required')
    return endpoint


class PacketSender:
    """한 send/step≤64KiB. offer는 송신·수신·수락 성공을 뜻하지 않는다.

    FRAME/CLOCK이 latest slot 하나를 공유하므로 서로 미송신 pending을 교체할 수 있다.
    caller가 원 pending identity와 control 순서를 관리한다. 숨은 우선순위 큐/재시도는 없다.
    """

    def __init__(self, endpoint, scope):
        _require(type(scope) is SessionScope, 'fixed SessionScope required')
        self.endpoint, self.scope = _endpoint(endpoint), scope
        self._pending = self._active = None
        self._part = self._offset = 0
        self._transfer_end = None
        self._heads = {}
        self.closed, self.failure = False, None
        self._stats = dict(offered=0, replaced=0, sent_packets=0, sent_bytes=0, expired=0, failed=0)

    @property
    def pending(self):
        return None if self._pending is None else self._pending[0]

    @property
    def active(self):
        return None if self._active is None else self._active[0]

    @property
    def stats(self):
        return dict(self._stats)

    def _fail(self, message, cls=TransportError):
        self.failure, self.closed = message, True
        self._pending = self._active = None
        self._stats['failed'] += 1
        raise cls(message)

    def offer(self, packet, *, expires_us, now_us):
        _require(not self.closed and type(packet) in (ObservationFrame, ClockPacket), 'sender unavailable or untyped packet')
        _require(packet.scope.raw_bytes == self.scope.raw_bytes, 'sender foreign scope')
        _require(_uint(now_us) < _uint(expires_us), 'original current local send deadline required')
        if type(packet) is ObservationFrame:
            _require(expires_us == packet.expires_pc_us, 'original PC frame E cannot be repackaged')
        head = self._heads.get(packet.kind)
        if head is not None:
            _require(packet.sequence >= head[0], 'send sequence regressed')
            if packet.sequence == head[0]:
                _require((packet.fingerprint, expires_us) == head[1:], 'same send sequence changed original body/E')
                return False
        self._heads[packet.kind] = packet.sequence, packet.fingerprint, expires_us
        self._stats['offered'] += 1
        self._stats['replaced'] += int(self._pending is not None)
        self._pending = packet, expires_us
        return True

    def discard_pending(self, original):
        if self._pending is None or self._pending[0] is not original:
            return False
        self._pending = None
        return True

    def step(self, now_us):
        """완료 반환은 local socket write 완료다. peer가 받았거나 승인했다는 뜻이 아니다."""
        _require(not self.closed, 'sender connection is fenced')
        now = _uint(now_us)
        if self._active is None:
            self._active, self._pending = self._pending, None
            self._part = self._offset = 0
            from xenova_vehicle.runtime.health_policy import health_policy
            self._transfer_end = now + health_policy().control_stall_us
        if self._active is None:
            return None
        packet, expires = self._active
        if now >= expires:
            if self._part == 0 and self._offset == 0:
                self._stats['expired'] += 1
                self._active = None
                return None
            # 이미 전송한 header 뒤에는 같은 body를 끝내야 한다. 관측 E는 그대로여서
            # 수신자는 늦은 프레임을 폐기하며 이 시간은 새 관측 유효기간이 아니다.
            if now >= self._transfer_end:
                self._fail('partial transfer stalled beyond connection budget', TransportTimeout)
        part = packet.wire_parts[self._part]
        try:
            sent = self.endpoint.send(memoryview(part)[self._offset:self._offset + MAX_IO])
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as error:
            self._fail('stream send failed: ' + type(error).__name__)
        if sent <= 0:
            self._fail('stream send made no progress', TransportEOF)
        self._stats['sent_bytes'] += sent
        self._offset += sent
        if self._offset == len(part):
            self._part += 1
            self._offset = 0
        if self._part == len(packet.wire_parts):
            self._active = None
            self._stats['sent_packets'] += 1
            return packet
        return None

    def close(self):
        """참조만 놓는다. 실제 socket.close는 caller 책임이며 HWM은 환급하지 않는다."""
        self.closed = True
        self._pending = self._active = None


class PacketReceiver:
    """begin이 원 local E를 고정한다. 각 step은 한 recv≤64KiB이며 자동 deadline 갱신은 없다."""

    def __init__(self, endpoint, scope, *, completed_clock):
        _require(type(scope) is SessionScope and callable(completed_clock), 'fixed scope and actual full-read clock required')
        self.endpoint, self.scope, self._clock = _endpoint(endpoint), scope, completed_clock
        self._deadline = None
        self._phase, self._buffer, self._used = 'prefix', bytearray(PREFIX.size), 0
        self._kind = self._sequence = self._header_raw = None
        self._body_length = 0
        self._heads = {}
        self.closed, self.failure = False, None
        self._stats = dict(received_packets=0, received_bytes=0, duplicate=0, expired=0, failed=0)

    @property
    def stats(self):
        return dict(self._stats)

    @property
    def partial_bytes(self):
        return self._used

    @property
    def deadline_us(self):
        return self._deadline

    def _fail(self, message, cls=TransportError):
        self.failure, self.closed = message, True
        self._buffer = self._header_raw = None
        self._deadline = None
        self._used = 0
        self._stats['failed'] += 1
        raise cls(message)

    def begin(self, *, expires_us, now_us):
        _require(not self.closed and self._deadline is None, 'original receive operation already armed or fenced')
        _require(_uint(now_us) < _uint(expires_us), 'original current local receive deadline required')
        self._deadline = expires_us

    def _complete(self, now):
        stamp = _uint(self._clock())
        _require(stamp >= now, 'full-read local clock regressed')
        if stamp >= self._deadline:
            self._stats['expired'] += 1
            self._fail('original receive deadline expired at full read', TransportTimeout)
        if self._kind == 'FRAME':
            fields = _header(self._header_raw, self._kind, self._sequence, self.scope)
            packet = ObservationFrame(self.scope, self._sequence,
                rgb_pc_interval=fields['rgb']['pc_interval_us'], depth_pc_interval=fields['depth']['pc_interval_us'],
                render_completed_pc_us=fields['render_completed_pc_us'], expires_pc_us=fields['expires_pc_us'],
                rgb=bytes(memoryview(self._buffer)[:RGB_BYTES]), depth=bytes(memoryview(self._buffer)[RGB_BYTES:]))
        else:
            fields = _header(self._header_raw, self._kind, self._sequence, self.scope)
            packet = ClockPacket(self.scope, self._kind, fields['clock_raw'].encode('ascii'))
        _require(packet.header_bytes == self._header_raw, 'original payload/header SHA differs')
        head = self._heads.get(packet.kind)
        if head is not None:
            _require(packet.sequence >= head[0], 'receive sequence regressed')
            if packet.sequence == head[0]:
                _require(packet.fingerprint == head[1], 'same received sequence changed original body')
                self._stats['duplicate'] += 1
                packet = None
        if packet is not None:
            self._heads[packet.kind] = packet.sequence, packet.fingerprint
            self._stats['received_packets'] += 1
        self._deadline = None
        self._phase, self._buffer, self._used = 'prefix', bytearray(PREFIX.size), 0
        self._header_raw = None
        return None if packet is None else ReceivedPacket(packet, stamp)

    def step(self, now_us):
        _require(not self.closed, 'receiver connection is fenced')
        now = _uint(now_us)
        if self._deadline is None:
            return None
        if now >= self._deadline:
            self._stats['expired'] += 1
            self._fail('original receive deadline expired; caller must close connection', TransportTimeout)
        try:
            raw = self.endpoint.recv(min(MAX_IO, len(self._buffer) - self._used))
        except (BlockingIOError, InterruptedError):
            return None
        except OSError as error:
            self._fail('stream receive failed: ' + type(error).__name__)
        if not raw:
            self._fail('EOF; partial packet discarded, caller must close connection', TransportEOF)
        self._stats['received_bytes'] += len(raw)
        self._buffer[self._used:self._used + len(raw)] = raw
        self._used += len(raw)
        if self._used != len(self._buffer):
            return None
        try:
            if self._phase == 'prefix':
                magic, version, kind_id, header_size, body_size, sequence = PREFIX.unpack(self._buffer)
                _require(magic == MAGIC and version == VERSION and kind_id in KINDS.values()
                    and 0 < header_size <= HEADER_CAP, 'invalid prefix/version/oversize header')
                kind = next(name for name, value in KINDS.items() if value == kind_id)
                _require(body_size == (FRAME_BYTES if kind == 'FRAME' else 0), 'fixed body length differs')
                self._kind, self._sequence, self._body_length = kind, sequence, body_size
                self._phase, self._buffer, self._used = 'header', bytearray(header_size), 0
            elif self._phase == 'header':
                self._header_raw = bytes(self._buffer)
                if not self._body_length:
                    return self._complete(now)
                _header(self._header_raw, self._kind, self._sequence, self.scope)
                self._phase, self._buffer, self._used = 'body', bytearray(self._body_length), 0
            else:
                return self._complete(now)
        except TransportTimeout:
            raise
        except Exception as error:
            self._fail('invalid original packet: ' + type(error).__name__)
        return None

    def close(self):
        """partial만 폐기한다. socket과 이미 반환한 immutable DTO는 caller 소유다."""
        self.closed = True
        self._deadline = None
        self._buffer = self._header_raw = None
        self._used = 0
