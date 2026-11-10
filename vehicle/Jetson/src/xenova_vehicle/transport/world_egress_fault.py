"""World 전용 개발 장애 필터. 원 객체를 보류할 뿐 송신·권한을 승인하지 않는다.

한 owner가 명시 monotonic J 시각으로 호출한다. held1+최신 pending1만 보관하고
각 원 offer의 due/E를 유지한다. 반환한 객체는 호출자가 실제 JIO의 _claim_world로
다시 검산해야 한다. 운영 gate·MQTT·clock·입력·자동 해제 기간은 이 모듈의 책임이 아니다.
"""

from dataclasses import dataclass
from typing import Protocol


_LIMIT_US = (1 << 63) - 1
_WORLD_BYTES = 32768  # 실제 JIO.offer_world의 원 bytes 상한과 같다.
_COUNTERS = ('offered', 'duplicate', 'replaced', 'blocked', 'expired', 'discarded', 'released')


class WorldPublication(Protocol):
    """JIO의 immutable _World에서 사용하는 두 필드이며 새 wire 형식이 아니다."""

    @property
    def raw(self) -> bytes: ...

    @property
    def expires_us(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _Held:
    original: WorldPublication
    raw: bytes
    expires_us: int
    offered_us: int
    due_us: int


class WorldEgressFault:
    """PASS/DELAY/BLOCK 내부 core. thread·sleep·네트워크·무한 큐가 없다.

    offer 후 take_due를 호출한다. True는 내부 보관일 뿐 발행/사용 승인이 아니다.
    configure는 같은 설정도 명시 갱신으로 취급해 두 칸을 폐기하며, clear는 PASS로
    전환한다. 중복 요청 키·설정 권한·장애 기간 정책은 외부 owner가 처리해야 한다.
    owner는 철회한 옛 publication을 새 offer로 재주입하지 않아야 한다. core에는
    source/sequence HWM이나 이미 반환한 객체를 회수하는 권한이 없다.
    """

    def __init__(self):
        self._mode = 'PASS'
        self._delay_us = 0
        self._held: _Held | None = None
        self._pending: _Held | None = None
        self._last_us: int | None = None
        self._counts = dict.fromkeys(_COUNTERS, 0)

    def _time(self, now_us):
        if (type(now_us) is not int or not 0 <= now_us <= _LIMIT_US
                or self._last_us is not None and now_us < self._last_us):
            raise ValueError('nonregressing original monotonic J time required')
        return now_us

    def _discard(self):
        self._counts['discarded'] += int(self._held is not None) + int(self._pending is not None)
        self._held = self._pending = None

    def configure(self, mode, *, delay_us=0, now_us):
        """검증된 내부 설정을 적용한다. 공개 action/요청 ID/기간 정책을 정의하지 않는다."""
        if (type(mode) is not str or mode not in ('PASS', 'DELAY', 'BLOCK')
                or type(delay_us) is not int or not 0 <= delay_us <= _LIMIT_US
                or (mode == 'DELAY') != (delay_us > 0)):
            raise ValueError('exact internal mode and matching integer delay required')
        now = self._time(now_us)
        self._discard()
        self._mode, self._delay_us, self._last_us = mode, delay_us, now

    def clear(self, *, now_us):
        """보류분을 버리고 PASS로 복귀한다. 옛 결과 flush나 AUTO 재출발을 수행하지 않는다."""
        self.configure('PASS', now_us=now_us)

    @property
    def mode(self):
        return self._mode

    @property
    def pending(self):
        return self._held is not None or self._pending is not None

    def discard(self):
        """source/운영 수명 철회 때 보류본만 버린다. 장애 설정이나 원 due를 갱신하지 않는다."""
        self._discard()

    def offer(self, original: WorldPublication, *, now_us):
        """이미 검산된 원 publication을 보관한다. raw/기한/그 밖의 원 필드는 바꾸지 않는다."""
        raw = getattr(original, 'raw', None)
        expires = getattr(original, 'expires_us', None)
        if (type(raw) is not bytes or not 0 < len(raw) <= _WORLD_BYTES
                or type(expires) is not int or not 0 < expires <= _LIMIT_US):
            raise ValueError('bounded original World bytes and absolute E required')
        now = self._time(now_us)
        due = now + self._delay_us
        if due > _LIMIT_US:
            raise ValueError('original offer plus delay exceeds monotonic range')
        self._last_us = now
        self._counts['offered'] += 1
        if now >= expires:
            self._counts['expired'] += 1
            return False
        if self._mode == 'BLOCK':
            self._counts['blocked'] += 1
            return False
        for item in (self._held, self._pending):
            if item is not None and item.original is original:
                self._unchanged(item)
                self._counts['duplicate'] += 1
                return True  # 같은 pending을 다시 보아도 원 offer/due를 시작하지 않는다.
        item = _Held(original, raw, expires, now, due)
        if self._held is None:
            self._held = item
        else:
            if self._pending is not None:
                self._counts['replaced'] += 1
            self._pending = item
        return True

    @staticmethod
    def _unchanged(item):
        if item.original.raw is not item.raw or item.original.expires_us != item.expires_us:
            raise ValueError('immutable original World bytes/E changed while held')

    def take_due(self, *, now_us):
        """due를 지난 원 객체 최대1개를 반환한다. 현재 source/권한 검산을 대신하지 않는다."""
        now = self._time(now_us)
        self._last_us = now
        while self._held is not None:
            item = self._held
            self._unchanged(item)
            if now >= item.expires_us:
                self._counts['expired'] += 1
                self._held, self._pending = self._pending, None
                continue
            if now < item.due_us:
                return None
            self._held, self._pending = self._pending, None
            self._counts['released'] += 1
            return item.original
        return None

    def snapshot(self):
        """유한 내부 관측값의 사본이다. 외부 API/status 계약이나 적용 ACK가 아니다."""
        return dict(mode=self._mode, delay_us=self._delay_us,
            held=self._held is not None, pending=self._pending is not None,
            held_due_us=None if self._held is None else self._held.due_us,
            pending_due_us=None if self._pending is None else self._pending.due_us,
            counts=dict(self._counts))
