"""dom0 Display 설정에 Interaction VM의 현재 주소를 안전하게 반영한다."""
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

from xenova_runtime import atomic_write


GUEST_NETWORK = ipaddress.ip_network('192.168.77.0/24')
RUNTIME_PATH = Path('/etc/xenova/display-runtime.json')
MOBILE_PATH = Path('/etc/xenova/display-mobile.json')
COMBINED_PAGE = '/display/combined'


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
