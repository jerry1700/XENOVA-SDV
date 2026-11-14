"""F02 장치별 개발 제어. 단일 S-jio owner/기존 lock 안에서만 사용한다.

Receiver의 원 2초 window로 접수하고, 적용 기간은 최초 monotonic deadline에
고정한다. World·UTC 권한을 만들지 않으며 실제 반영은 Main의 locked callback이
담당한다. 반환 Publication은 Q0 송신 후보이지 전달/차량 APPLIED 증거가 아니다.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from uuid import UUID, uuid4


MAX_BYTES = 1024
MAX_RECORDS = 128
RECOVERY_RECORDS = 8
WINDOW_US = 2_000_000
MAX_US = (1 << 63) - 1
HEADER = frozenset(('schema_version', 'message_id', 'source_id', 'boot_id',
    'agent_instance_id', 'type', 'seq', 'sent_at', 'payload'))


@dataclass(frozen=True)
class Publication:
    topic: str
    raw: bytes


@dataclass(frozen=True)
class _Record:
    raw: bytes
    result: Publication


def _uuid(value):
    if type(value) is not str:
        raise ValueError('INVALID_ID')
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError('INVALID_ID') from None
    if str(parsed) != value or not parsed.int:
        raise ValueError('INVALID_ID')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('INVALID_PARAMETERS')
    return value


def _revision(value):
    if (type(value) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value)
            or int(value) > MAX_US):
        raise ValueError('INVALID_REVISION')
    return int(value)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('DUPLICATE_KEY')
        result[key] = value
    return result


def _stamp(value):
    # 이 UTC 필드는 진단용이다. 접수 권한은 receiver의 원 window만 결정한다.
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', value):
        raise ValueError('INVALID_SENT_AT')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('INVALID_SENT_AT') from None


class WorldFaultController:
    """요청128 + 성공 CLEAR128 + 실패 CLEAR 복구8의 고정 기록. eviction은 없다.

    apply_callback(mode, delay_us=..., now_us=...)는 호출자가 이미 보유한 JIO lock
    안에서 실제 core/dispatch fence를 반영해야 한다. callback 실패는 UNKNOWN이며
    같은 요청을 다시 적용하지 않는다. clock/운영 gate·최종 claim은 호출자 책임이다.
    """

    def __init__(self, device, service_instance, apply_callback, *, boot_id, utc_clock=None):
        if type(device) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', device):
            raise ValueError('INVALID_DEVICE')
        self.device, self.service_instance, self.boot_id = device, _uuid(service_instance), _uuid(boot_id)
        if not callable(apply_callback):
            raise ValueError('actual locked apply callback required')
        self._apply = apply_callback
        self._utc_clock = utc_clock if utc_clock is not None else lambda: datetime.now(timezone.utc)
        if not callable(self._utc_clock):
            raise ValueError('UTC diagnostic clock callable required')
        prefix = f'xenova/v1/mapper/{device}/fault/'
        self.command_topic, self.query_topic = prefix + 'command.json', prefix + 'query.json'
        self.result_topic, self.status_topic = prefix + 'result.json', prefix + 'status.json'
        self._rows, self._clears, self._recovery = {}, {}, {}
        self._source = self._window = None
        self._window_end = 0
        self._last = None
        self._seq = self._revision = 0
        self._active = None
        self._deadline = 0
        self._expiry_attempted = False
        self._mode, self._delay_ms, self._known = 'PASS', 0, True

    def _time(self, now_us):
        _integer(now_us, 0, MAX_US)
        if self._last is not None and now_us < self._last:
            raise ValueError('MONOTONIC_REGRESSION')
        self._last = now_us
        return now_us

    def _publication(self, topic, kind, payload):
        if self._seq == MAX_US:
            raise RuntimeError('control result sequence exhausted')
        self._seq += 1
        utc = self._utc_clock()
        if not isinstance(utc, datetime) or utc.tzinfo is None or utc.utcoffset() != timezone.utc.utcoffset(utc):
            raise ValueError('aware UTC diagnostic datetime required')
        sent_at = utc.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        envelope = dict(schema_version='1.0', message_id=str(uuid4()), source_id='jetson',
            boot_id=self.boot_id, agent_instance_id=self.service_instance, type=kind,
            seq=self._seq, sent_at=sent_at, payload=payload)
        raw = json.dumps(envelope, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
        if len(raw) > MAX_BYTES:
            raise RuntimeError('closed F02 result exceeds byte cap')
        return Publication(topic, raw)

    def _result(self, key, raw, status, reason, now, *, query_id=None):
        return self._publication(self.result_topic, 'perception.fault.result', dict(
            command_id=key, request_sha256=sha256(raw).hexdigest() if raw is not None else None,
            status=status, reason=reason, query_id=query_id, service_instance=self.service_instance,
            revision=str(self._revision), active_command_id=self._active, mode=self._mode,
            applied_known=self._known, delay_ms=self._delay_ms,
            remaining_ms=max(0, (self._deadline-now+999)//1000) if self._active else 0,
            observed_us=str(now)))

    def _call(self, mode, delay_ms, now):
        if self._revision == MAX_US:
            raise RuntimeError('control revision exhausted')
        try:
            if self._apply(mode, delay_us=delay_ms*1000, now_us=now) is False:
                raise RuntimeError('locked callback did not confirm application')
        except Exception:
            self._known = False
            raise
        self._mode, self._delay_ms, self._known = mode, delay_ms, True
        self._revision += 1

    def set_source(self, source_key, *, now_us):
        """현재 source 수명만 교체한다. 기적용 S 관리 설정/원 기간은 유지한다.

        Main은 별도 discard hook으로 World 두 칸을 폐기한다. 여기서 callback을
        호출하거나 적용 revision/기간을 다시 발급하지 않는다.
        """
        if source_key is not None and (type(source_key) is not str or not re.fullmatch(r'[0-9a-f]{64}', source_key)):
            raise ValueError('actual lowerhex source lifetime key required')
        now = self._time(now_us)
        if source_key == self._source:
            return None
        self._source, self._window, self._window_end = source_key, None, 0
        return self.status(now_us=now)

    def tick(self, *, now_us):
        """원 적용 기한이 끝나면 두 큐를 폐기하는 PASS callback을 한 번 실행한다."""
        now = self._time(now_us)
        if self._active is None or now < self._deadline or self._expiry_attempted:
            return None
        key = self._active
        self._expiry_attempted = True
        try:
            self._call('PASS', 0, now)
        except Exception:
            # clock/Vehicle owner를 죽이거나 2ms마다 재적용하지 않는다. fresh CLEAR만 복구한다.
            return self._result(key, self._rows[key].raw, 'UNKNOWN', 'EXPIRY_APPLY_FAILED', now)
        self._active, self._deadline = None, 0
        return self._result(key, self._rows[key].raw, 'ENDED', 'DURATION_ELAPSED', now)

    def status(self, *, now_us):
        """살아 있는 window 재조회는 원 UUID/E를 유지한다. 설정 기간은 바꾸지 않는다."""
        now = self._time(now_us)
        self.tick(now_us=now)
        if self._window is None or now >= self._window_end:
            if now > MAX_US-WINDOW_US:
                raise ValueError('MONOTONIC_RANGE')
            self._window, self._window_end = str(uuid4()), now+WINDOW_US
        return self._publication(self.status_topic, 'perception.fault.status', dict(
            service_instance=self.service_instance, source_bound=self._source is not None,
            revision=str(self._revision), active_command_id=self._active, mode=self._mode,
            applied_known=self._known, delay_ms=self._delay_ms,
            remaining_ms=max(0, (self._deadline-now+999)//1000) if self._active else 0,
            window_id=self._window,
            window_remaining_ms=max(0, (self._window_end-now+999)//1000) if self._window else 0,
            observed_us=str(now), cache_size=len(self._rows), cache_limit=MAX_RECORDS,
            recovery_size=len(self._recovery), recovery_limit=RECOVERY_RECORDS))

    def snapshot(self):
        """기존 owner가 볼 불변 값의 사본. 새 window·발행·적용을 만들지 않는다."""
        return dict(mode=self._mode, applied_known=self._known, active_command_id=self._active,
            deadline_us=self._deadline, revision=self._revision, window_id=self._window,
            window_expires_us=self._window_end, source_bound=self._source is not None,
            cache_size=len(self._rows), clear_size=len(self._clears),
            recovery_size=len(self._recovery), recovery_limit=RECOVERY_RECORDS,
            expiry_attempted=self._expiry_attempted)

    def _decode(self, raw, kind):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError('SIZE_OR_TYPE')
        try:
            text = raw.decode('ascii')
            message = json.loads(text, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NONFINITE')))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError('INVALID_JSON') from None
        if type(message) is not dict or set(message) != HEADER:
            raise ValueError('INVALID_ENVELOPE')
        if message['schema_version'] != '1.0' or message['source_id'] != 'dom0-server' or message['type'] != kind:
            raise ValueError('INVALID_ENVELOPE')
        for key in ('message_id', 'boot_id', 'agent_instance_id'):
            _uuid(message[key])
        _integer(message['seq'], 1, MAX_US)
        _stamp(message['sent_at'])
        if type(message['payload']) is not dict:
            raise ValueError('INVALID_PAYLOAD')
        return message

    def _lookup(self, key):
        return self._rows.get(key) or self._clears.get(key) or self._recovery.get(key)

    def _reject(self, key, raw, reason, now):
        result = self._result(key, raw, 'REJECTED', reason, now)
        if len(self._rows) < MAX_RECORDS:
            self._rows[key] = _Record(raw, result)
        return result

    def handle(self, topic, raw, *, qos, retained, now_us):
        """Q1 원 command/query를 처리한다. 반환은 한 Q0 publication 후보이며 I/O는 없다.

        잘못된 framing/retained/식별 불가 입력은 응답 없이 버린다. 정상 형식 요청의
        거절은 REJECTED다. cache replay는 과거 수락 이력이며 현재 상태는 status로 본다.
        """
        now = self._time(now_us)
        self.tick(now_us=now)
        if type(qos) is not int or qos != 1 or type(retained) is not bool or retained:
            return None
        if topic not in (self.command_topic, self.query_topic):
            return None
        try:
            kind = 'perception.fault.query' if topic == self.query_topic else 'perception.fault.command'
            message = self._decode(raw, kind)
            payload = message['payload']
            if topic == self.query_topic:
                if payload == {'action': 'STATUS'}:
                    return self.status(now_us=now)
                if set(payload) != {'action', 'command_id'} or payload['action'] != 'RESULT':
                    return None
                key = _uuid(payload['command_id'])
                found = self._lookup(key)
                return found.result if found else self._result(key, None, 'UNKNOWN', 'NOT_FOUND', now,
                    query_id=message['message_id'])
            key = _uuid(payload.get('command_id'))
        except (ValueError, TypeError, OverflowError):
            return None  # 식별 불가/잘못된 framing에는 응답을 만들지 않는다.

        found = self._lookup(key)
        if found is not None:
            return found.result if found.raw == raw else self._result(key, raw, 'REJECTED', 'IDEMPOTENCY_CONFLICT', now)
        try:
            action = payload.get('action')
            common = {'command_id', 'service_instance', 'window_id', 'action'}
            if action == 'SET':
                if set(payload) != common | {'mode', 'delay_ms', 'duration_s'}:
                    raise ValueError('INVALID_PARAMETERS')
                mode, delay, duration = payload['mode'], payload['delay_ms'], payload['duration_s']
                if type(mode) is not str or mode not in ('DELAY', 'BLOCK'):
                    raise ValueError('INVALID_PARAMETERS')
                _integer(delay, 1 if mode == 'DELAY' else 0, 2000 if mode == 'DELAY' else 0)
                _integer(duration, 1, 120)
            elif action == 'CLEAR':
                if set(payload) != common | {'active_command_id', 'expected_revision'}:
                    raise ValueError('INVALID_PARAMETERS')
                _uuid(payload['active_command_id']);_revision(payload['expected_revision'])
            else:
                raise ValueError('INVALID_ACTION')
            _uuid(payload['service_instance']);_uuid(payload['window_id'])
        except (ValueError, TypeError, OverflowError) as error:
            return self._reject(key, raw, str(error), now)
        reason = None
        if payload['service_instance'] != self.service_instance:
            reason = 'SERVICE_MISMATCH'
        elif self._window is None or payload['window_id'] != self._window or now >= self._window_end:
            reason = 'WINDOW_EXPIRED_OR_MISMATCH'
        elif action == 'SET' and self._source is None:
            reason = 'SOURCE_UNAVAILABLE'
        elif action == 'SET' and self._active is not None:
            reason = 'BUSY'
        elif action == 'SET' and len(self._rows) >= MAX_RECORDS:
            reason = 'CACHE_FULL'
        elif action == 'SET' and now > MAX_US-payload['duration_s']*1_000_000:
            reason = 'MONOTONIC_RANGE'
        elif action == 'CLEAR' and (self._active != payload['active_command_id']
                or self._revision != int(payload['expected_revision'])):
            reason = 'ACTIVE_MISMATCH'
        elif len(self._recovery) >= RECOVERY_RECORDS:
            reason = 'RECOVERY_CACHE_FULL'
        if reason:
            if reason == 'RECOVERY_CACHE_FULL' and action == 'CLEAR':
                return self._result(key, raw, 'UNKNOWN', reason, now)
            return self._reject(key, raw, reason, now)

        # 128행이 차도 현재 active SET의 성공 CLEAR는 별도 예약 기록에 남길 수 있다.
        try:
            if action == 'SET':
                self._call(payload['mode'], payload['delay_ms'], now)
                self._active, self._deadline = key, now+payload['duration_s']*1_000_000
                self._expiry_attempted = False
            else:
                self._call('PASS', 0, now)
                self._active, self._deadline = None, 0
            result = self._result(key, raw, 'APPLIED', None, now)
        except Exception:
            # 일부 적용 여부를 모르면 성공을 만들지 않고 동일 키의 재적용도 막는다.
            if action == 'SET':
                self._active, self._deadline = key, now+payload['duration_s']*1_000_000
                self._expiry_attempted = False
            result = self._result(key, raw, 'UNKNOWN', 'APPLY_FAILED', now)
        record = _Record(raw, result)
        if action == 'SET':
            self._rows[key] = record
        elif self._known:
            self._clears[key] = record
        else:
            self._recovery[key] = record
        return result
