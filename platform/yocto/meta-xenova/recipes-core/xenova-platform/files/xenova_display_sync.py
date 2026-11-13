"""dom0 Display 설정에 Interaction VM의 현재 주소를 안전하게 반영한다."""
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import UUID

from xenova_runtime import atomic_write


GUEST_NETWORK = ipaddress.ip_network('192.168.77.0/24')
RUNTIME_PATH = Path('/etc/xenova/display-runtime.json')
MOBILE_PATH = Path('/etc/xenova/display-mobile.json')
COMBINED_PAGE = '/display/combined'
BINDINGS_PATH = Path('/etc/xenova/display-bindings.json')


def is_display_registration(record):
    """이 등록이 HDMI 화면 주소나 세 역할 binding에 영향을 주는지 판별한다."""
    return bool(isinstance(record, dict) and (
        record.get('id') in ('zephyr-drive-a', 'zephyr-drive-b')
        or (record.get('guest_kind') == 'linux'
            and record.get('template_id') == 'debian-13-arm64')))


def _address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError('display sync requires the assigned Xen bridge IPv4') from error
    if (not isinstance(address, ipaddress.IPv4Address) or address not in GUEST_NETWORK
            or address in (GUEST_NETWORK.network_address, GUEST_NETWORK.broadcast_address,
                           GUEST_NETWORK.network_address + 1)):
        raise ValueError('display sync requires the assigned Xen bridge IPv4')
    return str(address)


def _load(path, *, owner_uid, private, allow_list=False):
    path = Path(path)
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
            or (mode != 0o600 if private else bool(mode & 0o022))):
        raise ValueError(f'unsafe Display configuration: {path.name}')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'invalid Display configuration: {path.name}') from error
    if not isinstance(value, dict) and not (allow_list and isinstance(value, list)):
        raise ValueError(f'invalid Display configuration: {path.name}')
    return value, mode


def _runtime(value, origin):
    outputs = value.get('outputs')
    if (value.get('backend') != 'x11' or value.get('display') != ':5'
            or value.get('feed_origin') != 'http://127.0.0.1:8082'
            or value.get('renderer', 'chromium') != 'chromium'
            or not isinstance(outputs, list) or len(outputs) != 1
            or not isinstance(outputs[0], dict) or outputs[0].get('output_id') != 'monitor-1'
            or outputs[0].get('connector') != 'HDMI-1'
            or not re.fullmatch(r'[0-9a-f]{64}', str(outputs[0].get('edid_sha256', '')))
            or outputs[0]['edid_sha256'] == '0' * 64):
        raise ValueError('display-runtime.json is not an installed single-HDMI Plan A configuration')
    updated = {**value, 'ui_origin': origin}
    updated['outputs'] = [{**outputs[0], 'page': COMBINED_PAGE}]
    return updated


def _mobile(value, origin):
    allowed = {'schema_version', 'usb_interface', 'phone_ipv4', 'port', 'ui_origin'}
    if (set(value) - allowed or value.get('schema_version') != 1
            or not isinstance(value.get('usb_interface'), str)
            or not isinstance(value.get('phone_ipv4'), str)
            or type(value.get('port')) is not int):
        raise ValueError('display-mobile.json does not match the supported schema')
    return {**value, 'ui_origin': origin}


def sync_interaction_ui(address, *, runtime_path=RUNTIME_PATH, mobile_path=MOBILE_PATH,
                        owner_uid=0):
    """이미 설치된 보드별 값은 보존하고 VM3 origin과 통합 화면 경로만 갱신한다.

    모바일 설정은 opt-in이므로 파일이 없으면 건너뛴다. 모든 입력을 먼저 검증한 뒤
    변경 파일만 원자 교체해 잘못된 두 번째 파일 때문에 첫 파일만 바뀌지 않게 한다.
    """
    address = _address(address)
    origin = f'http://{address}:8086'
    prepared = []
    runtime_path, mobile_path = Path(runtime_path), Path(mobile_path)
    if runtime_path.exists() or runtime_path.is_symlink():
        value, mode = _load(runtime_path, owner_uid=owner_uid, private=False)
        prepared.append((runtime_path, _runtime(value, origin), mode, value))
    if mobile_path.exists() or mobile_path.is_symlink():
        value, mode = _load(mobile_path, owner_uid=owner_uid, private=True)
        prepared.append((mobile_path, _mobile(value, origin), mode, value))

    changed = []
    for path, updated, mode, previous in prepared:
        if updated == previous:
            continue
        atomic_write(path, json.dumps(updated, indent=2) + '\n', mode=mode)
        changed.append(path.name)
    return {'state': 'updated' if changed else ('unchanged' if prepared else 'not-configured'),
            'ui_origin': origin, 'page': COMBINED_PAGE, 'changed': changed}


def _identity(record):
    value = record.get('uuid')
    try:
        identity = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError('Display binding requires a valid VM UUID') from error
    if identity.int == 0:
        raise ValueError('Display binding cannot use the dom0 UUID')
    return str(identity)


def _record_by_id(records, instance_id):
    matches = [record for key, record in records.items()
               if key == instance_id or record.get('id') == instance_id]
    if len(matches) != 1 or matches[0].get('guest_kind') != 'zephyr':
        raise ValueError(f'Display binding requires exactly one {instance_id}')
    return matches[0]


def _interaction_record(records, identity):
    matches = [row for row in records.values() if row.get('uuid') == identity]
    if (len(matches) != 1 or matches[0].get('guest_kind', 'linux') != 'linux'
            or matches[0].get('template_id', 'debian-13-arm64' if matches[0].get('template') == 'debian-13-20261015' else None) != 'debian-13-arm64'):
        raise ValueError('selected Interaction VM must be a registered Debian display VM')
    return matches[0]


def _binding_target(path, owner_uid):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    value, _ = _load(path, owner_uid=owner_uid, private=False, allow_list=True)
    if isinstance(value, list):
        value = {'schema_version': 1, 'scope': 'configured', 'bindings': value}
    rows = value.get('bindings')
    roles = {'primary_drive': 'zephyr', 'backup_drive': 'zephyr', 'interaction': 'debian'}
    if (value.get('schema_version') != 1 or value.get('scope') not in ('deployment', 'configured')
            or not isinstance(rows, list) or len(rows) != 3
            or any(not isinstance(row, dict) for row in rows)
            or {row.get('role') for row in rows} != set(roles)):
        raise ValueError('invalid Display deployment bindings; explicit repair required')
    identities = [_identity({'uuid': row.get('vm_id')}) for row in rows]
    if len(set(identities)) != 3 or any(row.get('expected_os') != roles[row['role']] for row in rows):
        raise ValueError('invalid Display role identity or OS')
    return value


@contextmanager
def configuration_lock(path, owner_uid=0):
    """표시 설정만 무대기 직렬화한다. VM 관리/admission 잠금은 사용하지 않는다."""
    parent = Path(path).parent
    info = parent.stat()
    if (parent.is_symlink() or parent.resolve() != parent or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid or info.st_mode & 0o022):
        raise ValueError('Display configuration parent is unsafe')
    fd = os.open(parent / '.display-config.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Display configuration lock is unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _entry(path, value, mode):
    path = Path(path)
    old = path.read_bytes() if path.exists() else None
    return path, (json.dumps(value, indent=2) + '\n').encode(), mode, old


def _publish(entries):
    """전체 검증 뒤 게시한다. 쓰기 실패는 자기 변경만 되돌리며 전원 단절의 다중 파일 원자성을 주장하지 않는다."""
    changed = []
    for path, _, _, old in entries:
        if (path.read_bytes() if path.exists() else None) != old:
            raise ValueError('Display configuration changed before publication')
    try:
        for path, content, mode, old in entries:
            if content == old:
                continue
            changed.append((path, content, mode, old))
            atomic_write(path, content.decode("utf-8"), mode=mode)
    except OSError as error:
        rollback_failed = False
        for path, content, mode, old in reversed(changed):
            try:
                current = path.read_bytes() if path.exists() else None
                if current == old:
                    continue
                if current != content:
                    rollback_failed = True
                elif old is None:
                    path.unlink()
                    from xenova_runtime import sync_directory
                    sync_directory(path.parent)
                else:
                    atomic_write(path, old.decode("utf-8"), mode=mode)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise OSError('Display configuration publication/rollback incomplete; inspect before activation') from error
        raise
    return [path.name for path, _, _, _ in changed]


def _project(records, bindings, *, runtime_path, mobile_path, bindings_path, owner_uid, execute=True):
    rows = {row['role']: row for row in bindings['bindings']}
    for role in ('primary_drive', 'backup_drive'):
        matches = [r for r in records.values() if r.get('uuid') == rows[role]['vm_id']]
        if len(matches) != 1 or matches[0].get('guest_kind') != 'zephyr':
            raise ValueError('selected Display drive role is not registered')
    interaction = _interaction_record(records, rows['interaction']['vm_id'])
    origin = f"http://{_address(interaction.get('ipv4'))}:8086"
    entries = []
    for path, private, transform in ((Path(runtime_path), False, _runtime), (Path(mobile_path), True, _mobile)):
        if path.exists() or path.is_symlink():
            value, mode = _load(path, owner_uid=owner_uid, private=private)
            entries.append(_entry(path, transform(value, origin), mode))
    # 역할과 모든 UI 입력을 확인한 뒤 bindings를 마지막에 게시한다.
    _binding_target(bindings_path, owner_uid)
    entries.append(_entry(bindings_path, bindings, 0o644))
    changed = (_publish(entries) if execute else
               [path.name for path, content, _, old in entries if content != old])
    ui_changed = [name for name in changed if name != Path(bindings_path).name]
    return {'ui': {'state': 'updated' if ui_changed else ('unchanged' if len(entries) > 1 else 'not-configured'),
                   'ui_origin': origin, 'page': COMBINED_PAGE, 'changed': ui_changed},
            'bindings': {'state': 'updated' if Path(bindings_path).name in changed else 'unchanged',
                         'path': str(bindings_path), 'vm_ids': [row['vm_id'] for row in bindings['bindings']]}}


def sync_bindings(records, *, bindings_path=BINDINGS_PATH, owner_uid=0, interaction_vm_id=None):
    """factory의 명시 UUID 또는 기존 선택만 사용한다. 템플릿으로 역할을 추측하지 않는다."""
    with configuration_lock(bindings_path, owner_uid):
        previous = _binding_target(bindings_path, owner_uid)
        if previous is not None:
            bindings = previous
        else:
            if interaction_vm_id is None:
                raise ValueError('explicit initial Interaction VM UUID required')
            primary = _record_by_id(records, 'zephyr-drive-a')
            backup = _record_by_id(records, 'zephyr-drive-b')
            interaction = _interaction_record(records, str(UUID(interaction_vm_id)))
            bindings = {'schema_version': 1, 'scope': 'deployment', 'bindings': [
                {'vm_id': _identity(primary), 'role': 'primary_drive', 'expected_os': 'zephyr',
                 'app_ids': ['autonomous', 'manual'], 'app_topic_scope': 'vm_scoped',
                 'report_profile': 'warm-drive-1', 'agent_supported': False},
                {'vm_id': _identity(backup), 'role': 'backup_drive', 'expected_os': 'zephyr',
                 'app_ids': ['autonomous', 'manual'], 'app_topic_scope': 'vm_scoped',
                 'report_profile': 'warm-drive-1', 'agent_supported': False},
                {'vm_id': _identity(interaction), 'role': 'interaction', 'expected_os': 'debian',
                 'app_id': 'interaction', 'agent_supported': True}]}
        return _project(records, bindings, runtime_path=Path(bindings_path).parent/'display-runtime.json',
                        mobile_path=Path(bindings_path).parent/'display-mobile.json',
                        bindings_path=bindings_path, owner_uid=owner_uid)['bindings']


def select_interaction(records, identity, expected_identity, *, runtime_path=RUNTIME_PATH,
                       mobile_path=MOBILE_PATH, bindings_path=BINDINGS_PATH, owner_uid=0, execute=False):
    """현재 선택 UUID를 조건으로 화면 담당만 명시 교체한다. VM이나 출력 권한은 조작하지 않는다."""
    identity, expected_identity = str(UUID(identity)), str(UUID(expected_identity))
    with configuration_lock(bindings_path, owner_uid):
        previous = _binding_target(bindings_path, owner_uid)
        if previous is None:
            raise ValueError('initial Display bindings required; no implicit role assignment')
        current = next(row['vm_id'] for row in previous['bindings'] if row['role'] == 'interaction')
        if current not in (expected_identity, identity):
            raise ValueError('Interaction selection changed; read current bindings before retrying')
        _interaction_record(records, identity)
        updated = {**previous, 'bindings': [({**row, 'vm_id': identity} if row['role'] == 'interaction' else row)
                                            for row in previous['bindings']]}
        if not execute:
            _project(records, updated, runtime_path=runtime_path, mobile_path=mobile_path,
                     bindings_path=bindings_path, owner_uid=owner_uid, execute=False)
            return {'plan': True, 'previous_vm_id': current, 'interaction_vm_id': identity}
        return _project(records, updated, runtime_path=runtime_path, mobile_path=mobile_path,
                        bindings_path=bindings_path, owner_uid=owner_uid)


def sync_after_registration(records, registered, *, runtime_path=RUNTIME_PATH,
                            mobile_path=MOBILE_PATH, bindings_path=BINDINGS_PATH, owner_uid=0):
    """등록만으로 역할을 바꾸지 않는다. 선택된 같은 UUID의 투영만 동기화한다."""
    with configuration_lock(bindings_path, owner_uid):
        previous = _binding_target(bindings_path, owner_uid)
        if previous is None:
            return {'ui': {'state': 'not-requested'},
                    'bindings': {'state': 'pending', 'reason': 'explicit-display-roles-not-configured'}}
        if registered.get('uuid') not in {row['vm_id'] for row in previous['bindings']}:
            return {'ui': {'state': 'not-requested'}, 'bindings': {'state': 'unchanged'}}
        return _project(records, previous, runtime_path=runtime_path, mobile_path=mobile_path,
                        bindings_path=bindings_path, owner_uid=owner_uid)


def _systemctl(arguments, *, run):
    result = run(['systemctl', *arguments], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 timeout=30, check=False)
    if result.returncode != 0:
        raise OSError(f'systemctl {arguments[0]} failed for {arguments[-1]}')


def restart_changed_services(sync_result, *, run=subprocess.run):
    """실제로 바뀐 설정의 소비자만 고정된 systemd unit 이름으로 재시작한다."""
    if not isinstance(sync_result, dict):
        raise ValueError('Display service restart requires a sync result')
    ui = sync_result.get('ui', {})
    bindings = sync_result.get('bindings', {})
    changed = ui.get('changed', []) if isinstance(ui, dict) else []
    if (not isinstance(changed, list) or any(not isinstance(item, str) for item in changed)
            or not isinstance(bindings, dict)):
        raise ValueError('invalid Display sync result')

    restarted, skipped = [], []
    # feed가 새 UUID를 먼저 읽은 뒤 새 Chromium 화면을 열어 presentation 공백을 줄인다.
    if bindings.get('state') == 'updated':
        unit = 'xenova-display-feed.service'
        _systemctl(['restart', unit], run=run);restarted.append(unit)
    if 'display-runtime.json' in changed:
        unit = 'xenova-display-hdmi.service'
        _systemctl(['restart', unit], run=run);restarted.append(unit)
    if 'display-mobile.json' in changed:
        unit = 'xenova-display-mobile.service'
        active = run(['systemctl', 'is-active', '--quiet', unit], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     timeout=10, check=False)
        if active.returncode == 0:
            _systemctl(['restart', unit], run=run);restarted.append(unit)
        elif active.returncode == 3:
            skipped.append({'unit': unit, 'reason': 'inactive'})
        else:
            raise OSError(f'systemctl is-active failed for {unit}')
    return {'state': 'restarted' if restarted else 'unchanged',
            'restarted': restarted, 'skipped': skipped}
