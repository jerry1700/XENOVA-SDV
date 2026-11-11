"""generic RAW arena/token을 재사용하는 PC 전용 copy 경계다.

create/unlink/recovery나 peer/현재 clock 승인은 수행하지 않는다. 원 grant/lease와
실 writable/ready 취득 뒤에만 게시·복사하며 모든 SHM view를 끝낸 뒤 writable을 post한다.
decoder 결과는 소유 facts이고 EARLY ACK/NN enqueue/READY는 상위 actor 책임이다.
"""

from hashlib import sha256

from .native_resources import _RawAttachment, _now, NativeResourceError
from .raw_leases import SLOT_BYTES, WriteGrant, ReadLease, FilledInfo, ViewReturnFact
from .pc_slot import PcSlotEncoding, ExpectedPcSlot, HEADER_BYTES, prefix, decode_owned


class PcArenaWriter(_RawAttachment):
    """등록된 A-dev scope만 attach한다. encoding 자체는 writer 허가가 아니다."""

    def publish(self, grant, encoding):
        if type(grant) is not WriteGrant or type(encoding) is not PcSlotEncoding:
            raise NativeResourceError('exact original PC write grant/encoding required')
        self._check(grant.key)
        if encoding.context.write_key != grant.key or grant.request.expires_us > encoding.expires_j_us:
            raise NativeResourceError('PC encoding differs from original grant/frame E')
        slot = grant.key.slot
        digest = sha256(encoding.raw).digest()
        previous = self._seen[slot]
        if previous is not None:
            if previous[0] == grant and previous[1] == digest: return previous[2]
            if grant.key.generation <= previous[0].key.generation:
                raise NativeResourceError('PC write generation was already consumed')
        now = _now()
        grant.request.check(now)
        if now < grant.granted_us or now >= encoding.expires_j_us:
            raise NativeResourceError('PC write grant/frame is not current')
        writable, ready = self._tokens[slot]
        if not writable.acquire(False): raise NativeResourceError('PC writable token missing for actual grant')
        start = slot * SLOT_BYTES
        view = self._shm.buf[start:start + len(encoding.raw)]
        try: view[:] = encoding.raw
        finally: view.release()
        grant.request.check(_now())
        ready.release()
        completed = _now()
        grant.request.check(completed)
        if completed >= encoding.expires_j_us:
            raise NativeResourceError('PC publish completed after original frame E')
        info = FilledInfo(grant.key, encoding.payload_length, encoding.descriptor_sha256, completed)
        self._seen[slot] = (grant, digest, info)
        return info


class PcArenaReader(_RawAttachment):
    """M-dev가 실제 ready 취득 후 owned bytes만 반환한다. slot view는 반환하지 않는다."""

    def copy_owned(self, lease, expected):
        if type(lease) is not ReadLease or type(expected) is not ExpectedPcSlot:
            raise NativeResourceError('exact original PC read lease/expected slot required')
        key = lease.key.write_key
        self._check(key)
        if (expected.context.write_key != key or expected.original_filled_info != lease.read_request.offer.content
                or lease.read_request.request.expires_us > expected.expires_j_us):
            raise NativeResourceError('PC reader expectation differs from original lease/FILLED/frame E')
        slot = key.slot
        previous = self._seen[slot]
        if previous is not None and key.generation <= previous.key.write_key.generation:
            raise NativeResourceError('PC read generation was already consumed')
        now = _now()
        lease.read_request.request.check(now)
        if now < lease.reserved_us or now >= expected.expires_j_us:
            raise NativeResourceError('PC read lease/frame is not current')
        writable, ready = self._tokens[slot]
        if not ready.acquire(False): raise NativeResourceError('PC ready token missing for actual acquired lease')
        self._seen[slot] = lease
        start = slot * SLOT_BYTES
        try:
            header = prefix(self._owned(start, HEADER_BYTES), expected.context)
            owned = decode_owned(self._owned(start, header.used_length), expected, header)
        finally:
            views_ended = _now()
            writable.release()
            posted = _now()
        lease.read_request.request.check(posted)
        if posted >= expected.expires_j_us:
            raise NativeResourceError('PC copy returned after original frame E')
        return owned, ViewReturnFact(lease, views_ended, posted)
