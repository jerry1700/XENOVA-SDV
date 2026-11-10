"""dom0 Display 설정에 Interaction VM의 현재 주소를 안전하게 반영한다."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
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


def _load(path, *, owner_uid, private):
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
    if not isinstance(value, dict):
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


def _interaction_record(records):
    matches = [record for record in records.values()
               if record.get('guest_kind') == 'linux'
               and record.get('template_id') == 'debian-13-arm64']
    if len(matches) != 1:
        raise ValueError('Display binding requires exactly one Debian Interaction VM')
    return matches[0]


def _binding_target(path, owner_uid):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        if not path.parent.is_dir():
            raise ValueError('Display binding parent is unavailable')
        return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError('unsafe Display configuration: display-bindings.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    return value


def sync_bindings(records, *, bindings_path=BINDINGS_PATH, owner_uid=0):
    """현재 등록된 세 VM UUID를 deployment Display binding으로 게시한다."""
    if not isinstance(records, dict):
        raise ValueError('Display binding requires the registered VM map')
    primary = _record_by_id(records, 'zephyr-drive-a')
    backup = _record_by_id(records, 'zephyr-drive-b')
    interaction = _interaction_record(records)
    bindings = {
        'schema_version': 1,
        'scope': 'deployment',
        'bindings': [
            {'vm_id': _identity(primary), 'role': 'primary_drive', 'expected_os': 'zephyr',
             'app_ids': ['autonomous', 'manual'], 'app_topic_scope': 'vm_scoped',
             'report_profile': 'warm-drive-1', 'agent_supported': False},
            {'vm_id': _identity(backup), 'role': 'backup_drive', 'expected_os': 'zephyr',
             'app_ids': ['autonomous', 'manual'], 'app_topic_scope': 'vm_scoped',
             'report_profile': 'warm-drive-1', 'agent_supported': False},
            {'vm_id': _identity(interaction), 'role': 'interaction', 'expected_os': 'debian',
             'app_id': 'interaction', 'agent_supported': True},
        ],
    }
    path = Path(bindings_path)
    previous = _binding_target(path, owner_uid)
    if previous == bindings:
        return {'state': 'unchanged', 'path': str(path), 'vm_ids': [row['vm_id'] for row in bindings['bindings']]}
    atomic_write(path, json.dumps(bindings, indent=2) + '\n', mode=0o644)
    return {'state': 'updated', 'path': str(path), 'vm_ids': [row['vm_id'] for row in bindings['bindings']]}


def sync_after_registration(records, registered, *, runtime_path=RUNTIME_PATH,
                            mobile_path=MOBILE_PATH, bindings_path=BINDINGS_PATH,
                            owner_uid=0):
    """영구 등록이 끝난 뒤 가능한 Display projection만 갱신한다.

    세 역할이 아직 모두 등록되지 않은 최초 설치 중에는 bindings를 만들지 않는다.
    잘못되거나 중복된 역할은 pending으로 숨기지 않고 sync_bindings가 거부한다.
    """
    if not isinstance(registered, dict):
        raise ValueError('Display sync requires the registered VM')
    interaction = (registered.get('guest_kind') == 'linux'
                   and registered.get('template_id') == 'debian-13-arm64')
    ui = (sync_interaction_ui(registered.get('ipv4'), runtime_path=runtime_path,
                              mobile_path=mobile_path, owner_uid=owner_uid)
          if interaction else {'state': 'not-requested'})

    drive_a = [record for key, record in records.items()
               if key == 'zephyr-drive-a' or record.get('id') == 'zephyr-drive-a']
    drive_b = [record for key, record in records.items()
               if key == 'zephyr-drive-b' or record.get('id') == 'zephyr-drive-b']
    interactions = [record for record in records.values()
                    if record.get('guest_kind') == 'linux'
                    and record.get('template_id') == 'debian-13-arm64']
    if not drive_a or not drive_b or not interactions:
        bindings = {'state': 'pending', 'reason': 'three-display-roles-not-registered'}
    else:
        bindings = sync_bindings(records, bindings_path=bindings_path, owner_uid=owner_uid)
    return {'ui': ui, 'bindings': bindings}
