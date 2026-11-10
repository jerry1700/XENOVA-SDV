"""정지된 관리 runtime의 명시 복구 예산 재승인. 자원 정리·service 시작은 수행하지 않는다.

원 ResourceStore lock 아래 실제 종료/이름/scope를 확인하고 exact target의 최종
AUTHORIZED intent를 먼저 내구 저장한다. UNKNOWN은 승인 취소나 재출판 허가가 아니다.
다음 별도 명시 start는 기존 admission과 _next_episode를 그대로 거쳐야 한다.
"""
from dataclasses import asdict, dataclass
import base64
import hashlib
import json
import os
import stat
import time
import uuid

from .resource_manifest import ResourceManifest, ROLES, U64_MAX, _sha, _uuid
from .resource_name_observation import ResourceNameObserver
from .resource_store import ResourceStore
from .runtime_admission import _canonical, _record, _record_manifest
from .runtime_bootstrap import BootstrapUnavailable, _Directory
from .stopped_service_scope import observe_stopped


AUDIT_CAP = 65536


class RearmUnavailable(RuntimeError):
    """원 source·명시 요청·정리 관측 또는 내구 결과를 확인하지 못했다."""


@dataclass(frozen=True)
class RearmRequest:
    request_id: str
    expected_manifest_sha256: str
    expected_episode_id: str
    reason: str

    def __post_init__(self):
        _uuid(self.request_id)
        _sha(self.expected_manifest_sha256)
        _uuid(self.expected_episode_id)
        if (type(self.reason) is not str or not self.reason.strip() or '\x00' in self.reason
                or len(self.reason.encode('utf-8')) > 512):
            raise ValueError('explicit recovery reason must contain 1..512 UTF-8 bytes')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _target(source, episode_id):
    if source.revision == U64_MAX or episode_id == source.recovery_episode.episode_id:
        raise RearmUnavailable('revision exhausted or replacement episode is not new')
    fields = source.fields
    fields.update(revision=str(source.revision + 1), state='CLEAN', recovery_episode=dict(
        episode_id=episode_id, service_attempts=0, role_attempts=dict(A=0, M=0, D=0)))
    return ResourceManifest(_canonical(fields))


def _valid_source(receipt):
    if receipt.outcome.kind != 'VALID':
        raise RearmUnavailable('current manifest is not VALID')
    source = receipt.outcome.manifest
    if (not source.complete_inventory or source.intents
            or {owner.role for owner in source.owners if owner.phase == 'CURRENT'} != set(ROLES)):
        raise RearmUnavailable('complete S/A/M/D/TRACKER original inventory is required')
    return source


def _observe(store):
    source = _valid_source(store.load())
    anchor_raw, anchor = _record(store.directory)
    if anchor is None or anchor['status'] != 'PUBLISHED' or anchor['scope'] is None:
        raise RearmUnavailable('original managed PUBLISHED execution anchor is required')
    _record_manifest(anchor, source)
    names = ResourceNameObserver(store, str(uuid.uuid4())).observe()
    if (names.original_load.outcome.manifest.raw_bytes != source.raw_bytes
            or names.guard.runtime != source.runtime
            or any(owner.outcome != 'EXITED' for batch in (names.before, names.after) for owner in batch.owners)
            or any(name.outcome != 'ABSENT' for name in names.names)):
        raise RearmUnavailable('same-runtime owners EXITED and every known name ABSENT are required')
    scope = observe_stopped(names.guard, source, anchor)
    after_raw, after = _record(store.directory)
    if after_raw != anchor_raw:
        raise RearmUnavailable('original execution anchor changed during stopped observation')
    _record_manifest(after, source)
    facts = dict(guard=asdict(names.guard), started_ns=names.started_ns, completed_ns=names.completed_ns,
        owners_before=[dict(role=o.owner.role, phase=o.owner.phase, birth=asdict(o.owner.birth), outcome=o.outcome)
                       for o in names.before.owners],
        owners_after=[dict(role=o.owner.role, phase=o.owner.phase, birth=asdict(o.owner.birth), outcome=o.outcome)
                      for o in names.after.owners],
        names=[dict(slot=n.resource.slot, kind=n.resource.kind, name=n.resource.name, outcome=n.outcome)
               for n in names.names], stopped_scope=scope.fields, original_scope=anchor['scope'])
    if len(_canonical(facts)) > 24576:
        raise RearmUnavailable('stopped observation audit exceeds its bound')
    return source, names.latest_load, anchor_raw, facts


class _Audit:
    """원 Store directory 안의 request별 두 append-only record. 기존 파일 교체/삭제는 없다."""
    def __init__(self, directory):
        self.root = _Directory(directory)

    def close(self):
        self.root.close()

    @staticmethod
    def name(key, kind):
        _uuid(key)
        if kind not in ('intent', 'ack'):
            raise ValueError('unsupported rearm audit kind')
        return 'runtime-rearm-' + key + '.' + kind + '.json'

    @staticmethod
    def _safe(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RearmUnavailable('audit must be an owned single-link regular 0600 file')

    def read(self, key, kind):
        self.root.check()
        name = self.name(key, kind)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.root.fd)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            self._safe(before)
            raw = bytearray()
            while len(raw) <= AUDIT_CAP:
                part = os.read(fd, AUDIT_CAP + 1 - len(raw))
                if not part:
                    break
                raw.extend(part)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=self.root.fd, follow_symlinks=False)
            self._safe(after)
            self._safe(named)
            fingerprint = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns, x.st_mode, x.st_nlink)
            if (len(raw) > AUDIT_CAP or fingerprint(before) != fingerprint(after)
                    or fingerprint(after) != fingerprint(named)):
                raise RearmUnavailable('bounded original audit changed during read')
            value = json.loads(raw)
            if type(value) is not dict or _canonical(value) != bytes(raw):
                raise RearmUnavailable('audit must preserve canonical original bytes')
            self.root.check()
            return bytes(raw), value
        finally:
            os.close(fd)

    def append(self, key, kind, value):
        raw = _canonical(value)
        if len(raw) > AUDIT_CAP:
            raise RearmUnavailable('audit record exceeds 64KiB')
        self.root.check()
        name = self.name(key, kind)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.root.fd)
        try:
            os.fchmod(fd, 0o600)
            self._safe(os.fstat(fd))
            position = 0
            while position < len(raw):
                count = os.write(fd, raw[position:])
                if count <= 0:
                    raise RearmUnavailable('audit write made no progress')
                position += count
            os.fsync(fd)
        finally:
            os.close(fd)
        self.root.check()
        os.fsync(self.root.fd)
        observed = self.read(key, kind)
        if observed is None or observed[0] != raw:
            raise RearmUnavailable('durable audit post-read differs from original bytes')
        return raw


def _intent(record, key):
    required = {'version', 'state', 'request', 'source_ref', 'source_manifest_b64', 'previous_episode',
                'target_ref', 'target_manifest_b64', 'anchor_sha256', 'observation', 'authorized_j_us'}
    if set(record) != required or record['version'] != 1 or record['state'] != 'AUTHORIZED':
        raise RearmUnavailable('unsupported final authorization intent')
    request = RearmRequest(**record['request'])
    if request.request_id != key:
        raise RearmUnavailable('intent request identity differs')
    source = ResourceManifest(base64.b64decode(record['source_manifest_b64'], validate=True))
    target = ResourceManifest(base64.b64decode(record['target_manifest_b64'], validate=True))
    if (asdict(source.ref) != record['source_ref'] or asdict(target.ref) != record['target_ref']
            or request.expected_manifest_sha256 != source.raw_sha256
            or request.expected_episode_id != source.recovery_episode.episode_id
            or _canonical(asdict(source.recovery_episode)) != _canonical(record['previous_episode'])
            or _target(source, target.recovery_episode.episode_id).raw_bytes != target.raw_bytes):
        raise RearmUnavailable('authorization source/target or preserved original episode differs')
    _sha(record['anchor_sha256'])
    return request, source, target


def _existing_store(directory):
    # check/query도 설치된 두 leaf가 없으면 lock을 새로 만들지 않는다.
    # 상위 경로와 same-UID writer의 협력 전제는 기존 ResourceStore와 동일하다.
    root = _Directory(str(directory))
    try:
        for name in (ResourceStore.DATA_NAME, ResourceStore.LOCK_NAME):
            _Audit._safe(os.stat(name, dir_fd=root.fd, follow_symlinks=False))
        root.check()
    finally:
        root.close()
    return ResourceStore(str(directory))


def _query(store, audit, key):
    loaded = audit.read(key, 'intent')
    if loaded is None:
        return dict(contract='runtime-rearm-1', request_id=key, status='NOT_FOUND', effect='NOT_OBSERVED')
    raw, value = loaded
    request, source, target = _intent(value, key)
    receipt = store.load()
    current = receipt.outcome.manifest if receipt.outcome.kind == 'VALID' else None
    effect = ('SOURCE_UNCHANGED' if current is not None and current.raw_bytes == source.raw_bytes else
              'TARGET_VISIBLE' if current is not None and current.raw_bytes == target.raw_bytes else 'OTHER_OR_UNREADABLE')
    anchor_raw, _ = _record(store.directory)
    anchor_matches = anchor_raw is not None and _hash(anchor_raw) == value['anchor_sha256']
    complete = audit.read(key, 'ack')
    durable_ack = False
    if complete is not None:
        ack = complete[1]
        expected = dict(version=1, status='DURABLE_ACK', request_id=key, intent_sha256=_hash(raw),
                        source_ref=value['source_ref'], target_ref=value['target_ref'], anchor_sha256=value['anchor_sha256'])
        if ack != expected:
            raise RearmUnavailable('completion audit does not bind its original authorization')
        durable_ack = True
    return dict(contract='runtime-rearm-1', request_id=key, status='DURABLE_ACK' if durable_ack else 'UNKNOWN',
                effect=effect, durable_ack_record_visible=durable_ack, anchor_matches=anchor_matches,
                source_ref=value['source_ref'], target_ref=value['target_ref'],
                current_ref=None if current is None else asdict(current.ref),
                previous_episode=value['previous_episode'], new_episode_id=target.recovery_episode.episode_id,
                motion_authorized=False)


def check_rearm(directory):
    """관측 결과만 반환한다. manifest·bootstrap·anchor·DB·native 자원을 변경하지 않는다."""
    with _existing_store(directory) as store:
        source, _, anchor, facts = _observe(store)
        return dict(contract='runtime-rearm-1', status='ELIGIBLE', source_ref=asdict(source.ref),
                    previous_episode=asdict(source.recovery_episode), anchor_sha256=_hash(anchor), observation=facts,
                    motion_authorized=False)


def query_rearm(directory, request_id):
    """원 key 상태 조회만 수행한다. visible target을 ACK로 승격하거나 재출판하지 않는다."""
    _uuid(request_id)
    with _existing_store(directory) as store:
        audit = _Audit(store.directory)
        try:
            try:
                return _query(store, audit, request_id)
            except (OSError, ValueError, RuntimeError) as error:
                return dict(contract='runtime-rearm-1', request_id=request_id, status='UNKNOWN',
                            effect='UNCONFIRMED', reason=str(error)[:256], motion_authorized=False)
        finally:
            audit.close()


def apply_rearm(directory, request):
    """원 key 한 번의 명시 승인. 같은 key는 원 intent 조회만 하며 target을 다시 발급하지 않는다."""
    if type(request) is not RearmRequest:
        raise ValueError('exact RearmRequest required')
    phase = {'intent_entered': False}
    try:
        return _apply_rearm(directory, request, phase)
    except (OSError, ValueError, RuntimeError) as error:
        # context 종료의 audit/Store FD 오류도 이미 발생한 효과를 NOT_ACCEPTED로 바꾸지 않는다.
        if not phase['intent_entered']:
            raise
        return dict(contract='runtime-rearm-1', request_id=request.request_id, status='UNKNOWN',
                    effect='UNCONFIRMED', reason=str(error)[:256], motion_authorized=False)


def _apply_rearm(directory, request, phase):
    with _existing_store(directory) as store:
        audit = _Audit(store.directory)
        intent_entered = False
        try:
            existing = audit.read(request.request_id, 'intent')
            if existing is not None:
                original, _, _ = _intent(existing[1], request.request_id)
                if original != request:
                    raise RearmUnavailable('request UUID conflicts with its immutable original approval')
                phase['intent_entered'] = True
                return dict(_query(store, audit, request.request_id), replay=True)
            source, receipt, anchor_raw, facts = _observe(store)
            if (source.raw_sha256 != request.expected_manifest_sha256
                    or source.recovery_episode.episode_id != request.expected_episode_id):
                raise RearmUnavailable('expected original manifest SHA/episode CAS mismatch')
            target = _target(source, str(uuid.uuid4()))
            intent = dict(version=1, state='AUTHORIZED', request=asdict(request), source_ref=asdict(source.ref),
                source_manifest_b64=base64.b64encode(source.raw_bytes).decode('ascii'),
                previous_episode=asdict(source.recovery_episode), target_ref=asdict(target.ref),
                target_manifest_b64=base64.b64encode(target.raw_bytes).decode('ascii'),
                anchor_sha256=_hash(anchor_raw), observation=facts, authorized_j_us=time.monotonic_ns() // 1000)
            intent_entered = True
            phase['intent_entered'] = True
            raw = audit.append(request.request_id, 'intent', intent)
            # 위 file+parent fsync 완료가 이 exact target에 대한 최종 운영 승인이다.
            fresh, receipt, fresh_anchor, current_facts = _observe(store)
            if (fresh.raw_bytes != source.raw_bytes or fresh_anchor != anchor_raw
                    or current_facts['stopped_scope'] != facts['stopped_scope']):
                raise RearmUnavailable('source/anchor/manager/cgroup/configuration changed after authorization fsync')
            # audit I/O 뒤 실제 새 receipt로 첫 proposal을 만든다. 기존 pending의 retarget은 없다.
            proposal = store.propose(receipt, target, request.request_id)
            result = store.publish(proposal)
            if result.kind != 'DURABLE':
                return dict(_query(store, audit, request.request_id), publish_result=result.kind)
            current = store.load()
            current_anchor, _ = _record(store.directory)
            if (current.outcome.kind != 'VALID' or current.outcome.manifest.raw_bytes != target.raw_bytes
                    or current_anchor != anchor_raw):
                raise RearmUnavailable('publication ACK/post-load/original anchor differs')
            ack = dict(version=1, status='DURABLE_ACK', request_id=request.request_id,
                       intent_sha256=_hash(raw), source_ref=asdict(source.ref), target_ref=asdict(target.ref),
                       anchor_sha256=_hash(anchor_raw))
            audit.append(request.request_id, 'ack', ack)
            return _query(store, audit, request.request_id)
        except (OSError, ValueError, RuntimeError) as error:
            if not intent_entered:
                raise
            # 실패한 append도 지우지 않는다. 원 key의 효과/내구 결과는 UNKNOWN이며 재출판하지 않는다.
            return dict(contract='runtime-rearm-1', request_id=request.request_id, status='UNKNOWN',
                        effect='UNCONFIRMED', reason=str(error)[:256], motion_authorized=False)
        finally:
            audit.close()
