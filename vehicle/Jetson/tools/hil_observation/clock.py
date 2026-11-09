"""PC renderer 원 시각을 Jetson J 구간으로 옮기는 개발 전용 4시각 검산이다.

caller가 실제 송신 전 J1, PC 완전수신 P2/송신 전 P3, Jetson 완전수신 J4를
floor(monotonic_ns/1000)로 한 번 기록한다. 각 tick의 [t,t+1us]를 보수 포함한다.
소켓/시계 읽기/thread는 없고, lease나 원문은 READY·관측·Pi P4·World 권위가 아니다.
source_id는 producer UUID이며 FrameStamp의 sim-wall:UUID label과 다른 식별자다.
"""

from dataclasses import dataclass
import json
from uuid import UUID


U63_MAX = (1 << 63) - 1
MAX_PROBE_US = 200_000
MAX_LEASE_US = 1_000_000
MAX_LOOKBACK_US = 500_000
MAX_CONTROL_BYTES = 1024
_PPM = 1_000_000


class HilClockError(ValueError):
    """원 source/요청/4시각/보수 window가 맞지 않는 개발 clock 자료다."""


def _need(value, reason):
    if not value: raise HilClockError(reason)


def _integer(value, minimum=0, maximum=U63_MAX):
    _need(type(value) is int and minimum <= value <= maximum, 'CLOCK_INTEGER')
    return value


def _uuid(value):
    try:
        _need(type(value) is str and len(value) == 36 and str(UUID(value)) == value and UUID(value).int != 0,
            'CLOCK_UUID')
    except (ValueError, AttributeError) as error:
        raise HilClockError('CLOCK_UUID') from error
    return value


def _decimal(value):
    _need(type(value) is str and value.isascii() and value.isdecimal() and 1 <= len(value) <= 19,
        'CLOCK_DECIMAL')
    result = _integer(int(value))
    _need(str(result) == value, 'CLOCK_DECIMAL')
    return result


def _exact(value, fields):
    _need(type(value) is dict and set(value) == set(fields), 'CLOCK_EXACT_FIELDS')


def _canonical(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    _need(len(raw) <= MAX_CONTROL_BYTES, 'CLOCK_CONTROL_CAP')
    return raw


def _raw_fields(raw):
    _need(type(raw) is bytes and 0 < len(raw) <= MAX_CONTROL_BYTES, 'CLOCK_CONTROL_CAP')
    def pairs(values):
        result = {}
        for key, value in values:
            _need(key not in result, 'CLOCK_DUPLICATE_KEY')
            result[key] = value
        return result
    def constant(value): raise HilClockError('CLOCK_NONFINITE')
    try:
        result = json.loads(raw.decode('ascii'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HilClockError('CLOCK_JSON') from error
    _need(_canonical(result) == raw, 'CLOCK_NONCANONICAL')
    return result


@dataclass(frozen=True, slots=True)
class ClockSource:
    source_id: str
    pc_session: str
    jetson_session: str

    def __post_init__(self):
        for value in (self.source_id, self.pc_session, self.jetson_session): _uuid(value)

    @property
    def fields(self): return dict(source_id=self.source_id, pc_session=self.pc_session, jetson_session=self.jetson_session)

    @classmethod
    def from_fields(cls, value):
        _exact(value, ('source_id', 'pc_session', 'jetson_session'))
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ClockPolicy:
    """caller의 명시 개발 가정이다. drift_ppm은 |dPC/dJ-1|의 상한이다."""
    max_rtt_us: int
    max_offset_width_us: int
    drift_ppm: int
    lease_us: int
    lookback_us: int

    def __post_init__(self):
        _integer(self.max_rtt_us, 1, MAX_PROBE_US)
        _integer(self.max_offset_width_us, 1)
        _integer(self.drift_ppm, 0, _PPM-1)
        _integer(self.lease_us, 1, MAX_LEASE_US)
        _integer(self.lookback_us, 0, MAX_LOOKBACK_US)


@dataclass(frozen=True, slots=True)
class ClockProbe:
    source: ClockSource
    sequence: int
    nonce: bytes
    j1_us: int
    expires_j_us: int

    def __post_init__(self):
        _need(type(self.source) is ClockSource, 'CLOCK_SOURCE')
        _integer(self.sequence, 1); _integer(self.j1_us); _integer(self.expires_j_us)
        _need(type(self.nonce) is bytes and len(self.nonce) == 16 and any(self.nonce), 'CLOCK_NONCE')
        _need(self.j1_us < self.expires_j_us <= self.j1_us+MAX_PROBE_US, 'CLOCK_PROBE_WINDOW')

    @property
    def fields(self):
        return dict(profile='hil-observation-clock-probe-1', source=self.source.fields,
            sequence=str(self.sequence), nonce=self.nonce.hex(), j1_us=str(self.j1_us), expires_j_us=str(self.expires_j_us))

    @property
    def raw_bytes(self): return _canonical(self.fields)

    @classmethod
    def from_fields(cls, value):
        _exact(value, ('profile', 'source', 'sequence', 'nonce', 'j1_us', 'expires_j_us'))
        _need(value['profile'] == 'hil-observation-clock-probe-1', 'CLOCK_PROBE_PROFILE')
        nonce = value['nonce']
        _need(type(nonce) is str and len(nonce) == 32 and all(c in '0123456789abcdef' for c in nonce), 'CLOCK_NONCE')
        return cls(ClockSource.from_fields(value['source']), _decimal(value['sequence']), bytes.fromhex(nonce),
            _decimal(value['j1_us']), _decimal(value['expires_j_us']))

    @classmethod
    def from_raw(cls, raw): return cls.from_fields(_raw_fields(raw))


@dataclass(frozen=True, slots=True)
class ClockReply:
    probe: ClockProbe
    p2_us: int
    p3_us: int

    def __post_init__(self):
        _need(type(self.probe) is ClockProbe, 'CLOCK_ORIGINAL_PROBE')
        _integer(self.p2_us, maximum=U63_MAX-1); _integer(self.p3_us, maximum=U63_MAX-1)
        _need(self.p2_us <= self.p3_us, 'CLOCK_PC_ORDER')

    @property
    def fields(self):
        return dict(profile='hil-observation-clock-reply-1', probe=self.probe.fields,
            p2_us=str(self.p2_us), p3_us=str(self.p3_us))

    @property
    def raw_bytes(self): return _canonical(self.fields)

    @classmethod
    def from_fields(cls, value):
        _exact(value, ('profile', 'probe', 'p2_us', 'p3_us'))
        _need(value['profile'] == 'hil-observation-clock-reply-1', 'CLOCK_REPLY_PROFILE')
        return cls(ClockProbe.from_fields(value['probe']), _decimal(value['p2_us']), _decimal(value['p3_us']))

    @classmethod
    def from_raw(cls, raw): return cls.from_fields(_raw_fields(raw))


def reply(probe, p2_us, p3_us):
    """PC caller의 원 완전수신/송신 전 tick만 결합한다. 여기서 시각을 읽지 않는다."""
    return ClockReply(probe, p2_us, p3_us)


@dataclass(frozen=True, slots=True)
class _Sample:
    reply: ClockReply
    j4_us: int
    lower: int
    upper: int
    support: int
    expires: int
    admitted: bool


@dataclass(frozen=True, slots=True, init=False)
class ClockLease:
    """원 두 표본 일관성 뒤 승인된 고정 clock window다. 관측/현재 사용권은 별도다."""
    source: ClockSource
    sequence: int
    nonce: bytes
    j1_us: int
    p2_us: int
    p3_us: int
    j4_us: int
    probe_expires_j_us: int
    offset_lower_us: int
    offset_upper_us: int
    support_lower_j_us: int
    expires_j_us: int
    drift_ppm: int
    lookback_us: int
    raw_probe_bytes: bytes
    raw_reply_bytes: bytes

    def __init__(self, *args, **kwargs): raise TypeError('clock lease requires original four-time acceptance')

    def map_interval(self, pc_lo, pc_hi, now_j):
        """원 PC pair는 보존하고 J hull만 반환한다. caller는 현재 source를 별도로 대조한다.

        PC pair는 최초 producer의 닫힌 capture hull이다. floor 표본만 있다면 producer가
        원문에 (before_floor, after_floor+1)을 기록하며 여기서 다시 +1하지 않는다.
        drift는 원 J1-lookback부터 J1+lease까지 이미 포함됐다. 도착/호출 시각으로
        support를 굴리거나 기한을 늘리지 않는다. now_j도 원 floor tick이다.
        """
        _integer(pc_lo); _integer(pc_hi); _integer(now_j, maximum=U63_MAX-1)
        _need(pc_lo <= pc_hi, 'CLOCK_INTERVAL_ORDER')
        _need(self.j4_us <= now_j and now_j+1 < self.expires_j_us, 'CLOCK_LEASE_EXPIRED_OR_FUTURE')
        lower, upper = pc_lo+self.offset_lower_us, pc_hi+self.offset_upper_us
        _integer(lower); _integer(upper)
        _need(self.support_lower_j_us <= lower <= upper < self.expires_j_us, 'CLOCK_INTERVAL_OUTSIDE_WINDOW')
        _need(upper <= now_j, 'CLOCK_INTERVAL_FUTURE')
        return lower, upper


    def deadline_lower(self, pcE, nowJ):
        """원 exclusive PC E의 안전한 J 기한이다. 미래 E를 관측으로 map하지 않는다.

        fixed window에서 O(J)=J-PC(J)>=lower이면 J<pcE+lower는 PC(J)<pcE다.
        반환값은 lease E로 clip하며 caller는 원 captureLower+500ms와 다시 min하고
        closed captureUpper<최종E를 검사한다. 원 PC E에 quantization +1은 붙이지 않는다.
        """
        _integer(pcE); _integer(nowJ, maximum=U63_MAX-1)
        _need(self.j4_us <= nowJ and nowJ+1 < self.expires_j_us, 'CLOCK_LEASE_EXPIRED_OR_FUTURE')
        expires = min(pcE+self.offset_lower_us, self.expires_j_us)
        _integer(expires)
        _need(nowJ+1 < expires, 'CLOCK_DEADLINE_EXPIRED')
        return expires


@dataclass(frozen=True, slots=True, init=False)
class ClockSampleEvidence:
    """lease 발급 당시 실제 owner가 소비한 원 Reply와 Jetson 완전수신 tick이다."""
    reply: ClockReply
    j4_us: int

    def __init__(self, *args, **kwargs): raise TypeError('clock sample evidence requires owner acceptance')


@dataclass(frozen=True, slots=True, init=False)
class ClockLeaseEvidence:
    """원 발급 lease/연속2표본/당시 fence의 불변 read port다. 새 admission은 아니다."""
    lease: ClockLease
    samples: tuple
    fence_revision: int

    def __init__(self, *args, **kwargs): raise TypeError('clock lease evidence requires owner acceptance')


def _issuance_evidence(lease, samples, fence_revision):
    _need(len(samples) == 2 and all(item.reply.probe.source == lease.source for item in samples)
        and samples[-1].reply.raw_bytes == lease.raw_reply_bytes, 'CLOCK_ISSUANCE_ORIGINALS')
    originals = []
    for item in samples:
        original = object.__new__(ClockSampleEvidence)
        object.__setattr__(original, 'reply', item.reply)
        object.__setattr__(original, 'j4_us', item.j4_us)
        originals.append(original)
    result = object.__new__(ClockLeaseEvidence)
    for name, value in dict(lease=lease, samples=tuple(originals), fence_revision=fence_revision).items():
        object.__setattr__(result, name, value)
    return result


def _lease(sample, policy):
    result = object.__new__(ClockLease); body = sample.reply; probe = body.probe
    fields = dict(source=probe.source, sequence=probe.sequence, nonce=probe.nonce, j1_us=probe.j1_us,
        p2_us=body.p2_us, p3_us=body.p3_us, j4_us=sample.j4_us, probe_expires_j_us=probe.expires_j_us,
        offset_lower_us=sample.lower, offset_upper_us=sample.upper, support_lower_j_us=sample.support,
        expires_j_us=sample.expires, drift_ppm=policy.drift_ppm, lookback_us=policy.lookback_us,
        raw_probe_bytes=probe.raw_bytes, raw_reply_bytes=body.raw_bytes)
    for name, value in fields.items(): object.__setattr__(result, name, value)
    return result


class HilObservationClock:
    """Jetson 단일 caller owner의 pending1/sample2/lease1이다. 자동 clock/I/O/retry는 없다.

    원 bytes 충돌·PC 역전·공통 유효 window의 offset 모순은 명시 revoke다.
    foreign/late/폭 실패는 warmup만 끊고 기존 lease 원 사실은 보존한다.
    source 교체는 전부 fence한다. 이 owner 밖에 보관한 lease의 source 검사는 caller 몫이다.
    """
    def __init__(self, source, policy):
        _need(type(source) is ClockSource and type(policy) is ClockPolicy, 'CLOCK_SOURCE_POLICY')
        self._source, self._policy = source, policy
        self._pending = self._lease = self._lease_evidence = None
        self._fence_revision = 0
        self._samples = (); self._hwm = self._streak = 0; self._fenced = False
        self._reason = 'WARMUP_REQUIRED'

    @property
    def source(self): return self._source

    @property
    def policy(self): return self._policy

    @property
    def lease(self): return self._lease

    @property
    def lease_evidence(self): return self._lease_evidence

    @property
    def fence_revision(self): return self._fence_revision

    @property
    def sample_count(self): return len(self._samples)

    @property
    def reason(self): return self._reason

    def replace_source(self, source):
        _need(type(source) is ClockSource, 'CLOCK_SOURCE')
        if source == self._source: return False
        self._fence()
        self._source = source; self._hwm = 0
        self._fenced = False; self._reason = 'SOURCE_REPLACED'
        return True

    def _fence(self):
        self._pending = self._lease = self._lease_evidence = None
        self._samples = (); self._streak = 0; self._fenced = True
        if self._fence_revision == U63_MAX:
            self._reason = 'CLOCK_FENCE_EXHAUSTED'
            raise HilClockError(self._reason)
        self._fence_revision += 1

    def _revoke(self, reason):
        self._fence(); self._reason = reason
        raise HilClockError(reason)

    def probe(self, sequence, nonce, j1_us, expires_j_us):
        _need(not self._fenced, 'CLOCK_REVOKED')
        value = ClockProbe(self.source, sequence, nonce, j1_us, expires_j_us)
        originals = tuple(x.reply.probe for x in self._samples)
        if self._pending is not None: originals += (self._pending,)
        for old in originals:
            if old.sequence == sequence:
                if old.raw_bytes != value.raw_bytes: self._revoke('CLOCK_PROBE_CONFLICT')
                return old
            if old.nonce == nonce: self._revoke('CLOCK_NONCE_CONFLICT')
        _need(sequence > self._hwm, 'CLOCK_SEQUENCE_REPLAY')
        if self._pending is not None:
            _need(j1_us >= self._pending.expires_j_us, 'CLOCK_PROBE_BUSY')
            self._streak = 0
        if self._samples and j1_us < self._samples[-1].j4_us: self._revoke('CLOCK_J_REGRESSION')
        self._hwm = sequence; self._pending = value
        return value

    def accept(self, value, j4_us):
        _need(type(value) is ClockReply, 'CLOCK_REPLY')
        _integer(j4_us, maximum=U63_MAX-1)
        _need(not self._fenced, 'CLOCK_REVOKED')
        if value.probe.source != self.source:
            self._streak = 0; self._reason = 'CLOCK_FOREIGN_SOURCE'
            raise HilClockError(self._reason)
        for old in self._samples:
            if old.reply.probe.sequence == value.probe.sequence:
                if old.reply.raw_bytes != value.raw_bytes: self._revoke('CLOCK_REPLY_CONFLICT')
                if not old.admitted: return None
                if self._lease is not None and self._lease.raw_reply_bytes == value.raw_bytes: return self._lease
                return _lease(old, self.policy)  # 원 J4/E만 재현하며 새 도착시각은 쓰지 않는다.
        _need(self._pending is not None and value.probe.sequence == self._pending.sequence, 'CLOCK_UNREQUESTED_REPLY')
        if value.probe.raw_bytes != self._pending.raw_bytes: self._revoke('CLOCK_PROBE_CONFLICT')
        self._pending = None
        try:
            probe = value.probe; policy = self.policy
            _need(probe.j1_us <= j4_us and j4_us+1 < probe.expires_j_us, 'CLOCK_PROBE_EXPIRED_OR_J_ORDER')
            rtt = j4_us+1-probe.j1_us
            _need(rtt <= policy.max_rtt_us, 'CLOCK_RTT')
            _need(max(0, value.p3_us-value.p2_us-1)*_PPM <= rtt*(_PPM+policy.drift_ppm), 'CLOCK_CAUSALITY')
            support = max(0, probe.j1_us-policy.lookback_us)
            expires = probe.j1_us+policy.lease_us
            _integer(expires); _need(j4_us+1 < expires, 'CLOCK_LEASE_TOO_SHORT')
            # |dPC/dJ-1|<=rho. J 길이의 drift 상한을 원 window 전체에 한 번 포함한다.
            # 기존 mapper_clock.drift_pad의 PC 길이식 /(1-rho)와 달리 여기 길이는 J다.
            pad = (policy.drift_ppm*(expires-support)+_PPM-1)//_PPM
            lower = probe.j1_us-value.p2_us-1-pad; upper = j4_us+1-value.p3_us+pad
            _need(lower <= upper and upper-lower <= policy.max_offset_width_us, 'CLOCK_OFFSET_WIDTH')
            if self._samples and value.p2_us < self._samples[-1].reply.p3_us: self._revoke('CLOCK_PC_REGRESSION')
            previous = self._samples[-1] if self._samples else None
            common_window = previous is not None and j4_us+1 < previous.expires
            overlap = previous is not None and max(lower, previous.lower) <= min(upper, previous.upper)
            if common_window and not overlap: self._revoke('CLOCK_OFFSET_CONFLICT')
            consistent = self._streak > 0 and common_window and overlap
            self._streak = min(2, self._streak+1) if consistent else 1
            sample = _Sample(value, j4_us, lower, upper, support, expires, self._streak == 2)
            self._samples = (self._samples+(sample,))[-2:]
            if not sample.admitted:
                self._reason = 'WARMUP_REQUIRED' if previous is None else 'WARMUP_INCONSISTENT_OR_GAP'
                return None
            issued = _lease(sample, policy)
            evidence = _issuance_evidence(issued, self._samples, self._fence_revision)
            self._lease = issued; self._lease_evidence = evidence; self._reason = 'FOUR_TIME_CLOCK_LEASE'
            return self._lease
        except HilClockError as error:
            self._streak = 0; self._reason = str(error)
            raise
