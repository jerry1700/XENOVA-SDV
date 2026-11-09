"""고정 실행자가 정지한 상태를 원 PUBLISHED 실행 anchor와 결박한다.

UID1000/Cap0 관리 명령의 관측이며 service 시작·자원 생성·rearm 소비는 하지 않는다.
live service_scope의 현재 S-only admission과 별도로, root 소유 전용 slice가 원
incarnation 그대로 비었는지 양끝에서 검사한다. 협력 same-UID 실행 정책만 지원한다.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re

from .procfs_formats import parse_boot_id, parse_proc_stat
from .resource_guard import CurrentGuardScope
from .resource_manifest import CgroupIdentity, ProcessBirth, ResourceManifest
from .service_scope import (
    CGROUP_ROOT, EXECUTION_PROFILE, LEAF_PATH, SLICE, SLICE_PATH, UNIT,
    ServiceScopeUnavailable, _canonical, _identity, _mount_root, _pids, _protected,
    _PROPERTIES, _read, _root_file, _show, _uid,
)


# 실제 systemd237의 Job= 빈 출력이 확인됐다. Restart 정책은 감사 값으로 보존한다.
_ABSENT_JOB_VALUE = ''


def _anchor(guard, manifest, record):
    from .runtime_admission import BootstrapUnavailable, _episode, _record_manifest

    if type(guard) is not CurrentGuardScope or type(manifest) is not ResourceManifest:
        raise ServiceScopeUnavailable('stopped scope requires exact current guard and manifest')
    fields = {'version', 'status', 'source_ref', 'service_instance', 'resource_epoch', 'supervisor',
              'runtime', 'recovery_episode', 'scope', 'admission'}
    if (type(record) is not dict or set(record) != fields or type(record.get('version')) is not int
            or record['version'] != 1 or record.get('status') != 'PUBLISHED'
            or type(record.get('scope')) is not dict or manifest.runtime != guard.runtime
            or manifest.cgroup.mode != 'SYSTEMD_V1' or manifest.cgroup.path != SLICE_PATH):
        raise ServiceScopeUnavailable('stopped scope requires same-runtime published managed history')
    try:
        _episode(record['recovery_episode'])
        _record_manifest(record, manifest)
    except (BootstrapUnavailable, KeyError, TypeError, ValueError) as error:
        raise ServiceScopeUnavailable('published execution does not bind the current catalogue') from error
    scope = record['scope']
    invocation = scope.get('invocation_id')
    if (scope.get('unit') != UNIT or scope.get('slice') != SLICE
            or scope.get('execution_profile') != EXECUTION_PROFILE or scope.get('uid') != 1000
            or scope.get('control_group') != LEAF_PATH
            or scope.get('boot_id') != guard.runtime.boot_id
            or type(invocation) is not str or re.fullmatch('[0-9a-f]{32}', invocation) is None
            or invocation == '0' * 32 or type(scope.get('manager_birth')) is not dict
            or any(key not in scope for key in ('fragment', 'dropins', 'slice_fragment', 'slice_dropins'))):
        raise ServiceScopeUnavailable('published execution lacks the fixed managed scope anchor')
    return scope


def _current_guard(guard):
    if os.getuid() != 1000 or os.geteuid() != 1000:
        raise ServiceScopeUnavailable('the supported administrative observation UID is 1000')
    if re.fullmatch(r'237(?:[^0-9].*)?', _show(None, ('Version',))['Version']) is None:
        raise ServiceScopeUnavailable('the actual system manager is not the pinned systemd237 profile')
    if _read('/proc/1/comm', 32) != b'systemd\n':
        raise ServiceScopeUnavailable('actual PID1 is not systemd')
    _uid(_read('/proc/1/status', 65_536), 0)
    manager = parse_proc_stat(_read('/proc/1/stat', 4096), 1)
    current = parse_proc_stat(_read('/proc/self/stat', 4096), os.getpid())
    if (ProcessBirth(current.pid, current.start_ticks) != guard.birth
            or parse_boot_id(_read('/proc/sys/kernel/random/boot_id', 64)) != guard.runtime.boot_id
            or os.stat('/proc/self/ns/pid').st_ino != guard.runtime.pid_ns_inode
            or os.stat('/proc/self/ns/mnt').st_ino != guard.runtime.mnt_ns_inode):
        raise ServiceScopeUnavailable('current administrative guard process/runtime changed')
    status = _read('/proc/self/status', 65_536)
    _uid(status, 1000)
    for label in (b'CapEff:', b'CapPrm:', b'CapAmb:'):
        values = [line.split()[1] for line in status.splitlines() if line.startswith(label)]
        if len(values) != 1 or int(values[0], 16) != 0:
            raise ServiceScopeUnavailable('administrative observation must not hold override capabilities')
    return asdict(ProcessBirth(1, manager.start_ticks))


def _stopped_manager(guard, original):
    manager_birth = _current_guard(guard)
    if manager_birth != original['manager_birth']:
        raise ServiceScopeUnavailable('system manager birth differs from the published execution')
    values = _show(UNIT, _PROPERTIES + ('Restart', 'Job'))
    expected = dict(Id=UNIT, LoadState='loaded', Type='simple', MainPID='0', ControlPID='0', Slice=SLICE,
                    KillMode='control-group', KillSignal='2', Delegate='no', UID='1000')
    if (any(values[key] != value for key, value in expected.items())
            or (values['ActiveState'], values['SubState']) not in (('inactive', 'dead'), ('failed', 'failed'))
            or values['InvocationID'] != '' and re.fullmatch('[0-9a-f]{32}', values['InvocationID']) is None
            or values['ControlGroup'] not in ('', LEAF_PATH)):
        raise ServiceScopeUnavailable('actual unit is not the fixed fully stopped execution')
    if _ABSENT_JOB_VALUE is None or values['Job'] != _ABSENT_JOB_VALUE:
        raise ServiceScopeUnavailable('actual absent manager Job format is unconfirmed or a job is pending')
    fragment = _root_file(values['FragmentPath'])
    drops = [_root_file(name) for name in values['DropInPaths'].split()]
    slice_values = _show(SLICE, ('Id', 'LoadState', 'ActiveState', 'ControlGroup',
                                'StopWhenUnneeded', 'FragmentPath', 'DropInPaths'))
    if any(slice_values[key] != value for key, value in dict(Id=SLICE, LoadState='loaded',
            ActiveState='active', ControlGroup=SLICE_PATH, StopWhenUnneeded='no').items()):
        raise ServiceScopeUnavailable('dedicated persistent slice is not active and fixed')
    slice_fragment = _root_file(slice_values['FragmentPath'])
    slice_drops = [_root_file(name) for name in slice_values['DropInPaths'].split()]
    return dict(unit=UNIT, slice=SLICE, manager_birth=manager_birth, properties=values,
                slice_properties=slice_values, fragment=fragment, dropins=drops,
                slice_fragment=slice_fragment, slice_dropins=slice_drops)


def _empty_tree(expected):
    """전용 tree를 유한 순회한다. 빈 leaf 부재는 허용하되 slice incarnation은 보존한다."""
    root_fd = os.open(CGROUP_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        root_identity = _mount_root(root_fd)
        _protected(CGROUP_ROOT, directory=True)
        stack, rows = [SLICE_PATH], []
        while stack:
            relative = stack.pop()
            if len(rows) >= 16:
                raise ServiceScopeUnavailable('dedicated slice exceeds 16 directories')
            if relative != SLICE_PATH and not (relative == LEAF_PATH or relative.startswith(LEAF_PATH + '/')):
                raise ServiceScopeUnavailable('another execution unit entered the dedicated slice')
            path = CGROUP_ROOT + relative
            directory = _protected(path, directory=True)
            if relative == SLICE_PATH and (directory.st_dev, directory.st_ino) != (expected.device, expected.inode):
                raise ServiceScopeUnavailable('persistent slice incarnation differs from the original catalogue')
            procs, tasks = _protected(path + '/cgroup.procs'), _protected(path + '/tasks')
            if _pids(_read(path + '/cgroup.procs', 16_384)) or _pids(_read(path + '/tasks', 16_384)):
                raise ServiceScopeUnavailable('stopped dedicated slice still contains process or thread members')
            children = []
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        children.append(entry.name)
                        if len(children) + len(stack) + len(rows) >= 16:
                            raise ServiceScopeUnavailable('dedicated slice exceeds 16 directories')
            rows.append((relative, directory.st_dev, directory.st_ino,
                         procs.st_dev, procs.st_ino, tasks.st_dev, tasks.st_ino))
            stack.extend(relative + '/' + name for name in children)
            if (_identity(os.stat(path, follow_symlinks=False)) != _identity(directory)
                    or _identity(os.stat(path + '/cgroup.procs', follow_symlinks=False)) != _identity(procs)
                    or _identity(os.stat(path + '/tasks', follow_symlinks=False)) != _identity(tasks)):
                raise ServiceScopeUnavailable('empty cgroup directory or basefile changed during scan')
        if _mount_root(root_fd) != root_identity:
            raise ServiceScopeUnavailable('held cgroup mount root changed')
        return root_identity, tuple(sorted(rows))
    finally:
        os.close(root_fd)


@dataclass(frozen=True)
class StoppedServiceFacts:
    canonical_bytes: bytes

    @property
    def fields(self):
        return json.loads(self.canonical_bytes)

    @property
    def cgroup(self):
        value = self.fields['cgroup']
        return CgroupIdentity(value['mode'], value['path'], value['device'], value['inode'])


def observe_stopped(guard, manifest, published_record):
    """원 guard/manifest/PUBLISHED anchor에 묶인 양끝 정지·빈 tree 사실만 반환한다.

    caller가 같은 ResourceStore lock/CAS 아래 원 관측과 소모를 결합해야 한다.
    이 사실은 runtime 생성·counter 초기화·서비스 시작의 독립 권한이 아니다.
    """
    original = _anchor(guard, manifest, published_record)
    first_manager = _stopped_manager(guard, original)
    first_tree = _empty_tree(manifest.cgroup)
    second_manager = _stopped_manager(guard, original)
    second_tree = _empty_tree(manifest.cgroup)
    if first_manager != second_manager or first_tree != second_tree:
        raise ServiceScopeUnavailable('stopped manager or empty dedicated tree changed during observation')
    value = dict(second_manager, execution_profile=EXECUTION_PROFILE, boot_id=guard.runtime.boot_id,
                 guard_id=guard.guard_id, observer_birth=asdict(guard.birth),
                 original_invocation_id=original['invocation_id'], source_ref=asdict(manifest.ref),
                 original_configuration={key: original[key] for key in
                    ('fragment', 'dropins', 'slice_fragment', 'slice_dropins')},
                 cgroup=asdict(manifest.cgroup), members=[],
                 tree_sha256=hashlib.sha256(_canonical(second_tree)).hexdigest())
    return StoppedServiceFacts(_canonical(value))
