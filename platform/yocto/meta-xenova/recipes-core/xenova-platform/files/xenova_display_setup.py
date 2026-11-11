"""표시 역할의 명시 선택·새 factory 자격 공급·설치 후 읽기 전용 연결 확인.

VM 생성/기동·출력 권한·토큰 회전은 수행하지 않는다. 기존 설정은 보존하고,
첫 설치의 공급과 live HTTP 확인을 분리한다. 비밀은 반환값/예외에 넣지 않는다.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from xenova_runtime import atomic_write, sync_directory
import xenova_display_sync as display

TOKEN_RELATIVE = 'guest-display/guest-data.token'
DATA_PATH = '/api/v1/display/presentation'
ETC = Path('/etc/xenova')
RELAY = {'guest': None, 'guest_data_token_file': '/etc/xenova/guest-display/guest-data.token',
         'data_bind': '192.168.77.1', 'data_port': 8085, 'feed_port': 8082}


def environment(path):
    value = {}
    # EnvironmentFile은 shell로 실행하지 않는다. 필요한 기존 키만 읽는다.
    from xenova_provisioning import private
    private(path)
    for line in path.read_text().splitlines():
        key, separator, text = line.partition('=')
        if separator and key.startswith('XENOVA_DISPLAY_'):
            text = text.strip().strip('"')
            if not text or any(c in text for c in '\r\n\0'):
                raise ValueError('invalid Display environment')
            value[key] = text
    if not value.get('XENOVA_DISPLAY_TOKEN'):
        raise ValueError('Display feed token is not supplied')
    return value


def _create(path, content, mode=0o600):
    """원자적인 최초 게시만 허용한다. 기존 비밀/운영 override를 덮지 않는다."""
    if os.path.lexists(path):
        return False
    with tempfile.TemporaryDirectory(prefix='.display-prepare-', dir=path.parent) as directory:
        temporary = Path(directory)/'new'
        atomic_write(temporary, content, mode=mode)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        sync_directory(path.parent)
    return True


def prepare_token(etc, state):
    """신규/중단된 factory만 호출한다. 공급 전 원 토큰 hash를 progress에 고정한다."""
    from xenova_provisioning import _display_secret
    etc = Path(etc)
    with display.configuration_lock(etc/'display-bindings.json'):
        directory = etc/'guest-display'
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
            sync_directory(etc)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('unsafe Display private directory')
        path = etc/TOKEN_RELATIVE
        previous = state.get('display_bootstrap')
        if previous is not None and (not isinstance(previous, dict) or set(previous) != {'guest_token_sha256'}):
            raise ValueError('invalid factory Display progress; explicit repair required')
        if previous is None:
            _create(path, os.urandom(32).hex()+'\n')
        token = _display_secret(path)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if previous is not None and previous.get('guest_token_sha256') != fingerprint:
            raise ValueError('factory Display token changed or was lost; explicit repair required')
        state['display_bootstrap'] = {'guest_token_sha256': fingerprint}
    return {'state': 'token-prepared'}


def configure_factory(data, etc, state, records):
    """UUID·자격 공급 뒤 초기 binding과 relay를 준비한다. 기존 선택은 유지한다."""
    import xenova_broker as broker
    etc = Path(etc)
    from xenova_provisioning import _display_secret
    with display.configuration_lock(etc/'display-bindings.json'):
        token = _display_secret(etc/TOKEN_RELATIVE)
        if hashlib.sha256(token.encode()).hexdigest() != state['display_bootstrap']['guest_token_sha256']:
            raise ValueError('factory Display credential changed before configuration')
        env = etc/'display.env'
        if not os.path.lexists(env):
            # 기존 observer의 검증된 read-only export를 재사용한다. 계정 생성/암호 회전 없음.
            accounts = broker.inspect(data=Path(data), runtime=True)
            with tempfile.TemporaryDirectory(prefix='.display-export-', dir=etc) as directory:
                exported = Path(directory)/'observer.env'
                broker.export(accounts, 'observer', 'display-env', exported, execute=True)
                _create(env, exported.read_text()+'XENOVA_DISPLAY_TOKEN='+os.urandom(32).hex()+'\n')
        environment(env)
        relay = etc/'guest-display.json'
        _create(relay, json.dumps(RELAY, indent=2)+'\n')
        validate_relay(relay)
    result = display.sync_bindings(records, bindings_path=etc/'display-bindings.json',
                                   interaction_vm_id=state['identities']['debian-dev']['uuid'])
    return {'state': 'configured', 'bindings': result}


def validate_relay(path):
    value, _ = display._load(path, owner_uid=0, private=True)
    # 기존 Plan B 설정은 덮지 않되 data 통로의 승인된 고정 목적지는 확인한다.
    for key in ('guest_data_token_file', 'data_bind', 'data_port', 'feed_port'):
        if value.get(key, RELAY[key]) != RELAY[key]:
            raise ValueError('Display relay data path differs from the supported deployment')
    return value


def _http(host, port, path, token, deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Display connection check budget expired')
    connection = http.client.HTTPConnection(host, port, timeout=min(2, remaining))
    try:
        connection.request('GET', path, headers={'Authorization': 'Bearer '+token} if token else {})
        response = connection.getresponse()
        parts, size = [], 0
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Display connection check budget expired')
            if connection.sock is not None:
                connection.sock.settimeout(min(2, remaining))
            part = response.read1(min(65536, 2*1024*1024+1-size))
            if not part:
                break
            parts.append(part);size += len(part)
            if size > 2*1024*1024:
                raise ValueError('Display check response exceeded its limit')
        body = b''.join(parts)
        if response.status == 200 and path == DATA_PATH:
            if not isinstance(json.loads(body), dict):
                raise ValueError('Display presentation must be a JSON object')
        return response.status
    finally:
        connection.close()


def check(records, etc, *, live=False, request=_http, budget_s=15):
    """설정 공급과 실제 연결을 구분한다. live도 POST/재시도/서비스 조작을 하지 않는다."""
    from xenova_provisioning import _display_secret
    etc = Path(etc)
    if type(budget_s) not in (int, float) or not 0 < budget_s <= 15:
        raise ValueError('Display check budget must be in (0, 15] seconds')
    deadline = time.monotonic()+budget_s
    bindings = display._binding_target(etc/'display-bindings.json', 0)
    if bindings is None:
        raise ValueError('Display roles are not configured')
    identity = next(row['vm_id'] for row in bindings['bindings'] if row['role'] == 'interaction')
    record = display._interaction_record(records, identity)
    address = display._address(record.get('ipv4'))
    env = environment(etc/'display.env')
    token = _display_secret(etc/TOKEN_RELATIVE)
    validate_relay(etc/'guest-display.json')
    for name in ('display-runtime.json', 'display-mobile.json'):
        path = etc/name
        if os.path.lexists(path):
            value, _ = display._load(path, owner_uid=0, private=name=='display-mobile.json')
            if value.get('ui_origin') != f'http://{address}:8086':
                raise ValueError('Display UI origin and selected VM differ; explicit synchronization required')
    result = {'state': 'configured', 'interaction_vm_id': identity, 'connection': 'not-observed'}
    if live:
        stages = [('feed', '127.0.0.1', 8082, DATA_PATH, env['XENOVA_DISPLAY_TOKEN'], 200),
                  ('relay-auth', '192.168.77.1', 8085, DATA_PATH, token, 200),
                  ('relay-no-auth', '192.168.77.1', 8085, DATA_PATH, None, 401),
                  ('guest-data', address, 8086, DATA_PATH, None, 200),
                  ('guest-ui', address, 8086, '/display/combined', None, 200)]
        result['checks'] = []
        for stage, host, port, path, credential, expected in stages:
            try:
                status = request(host, port, path, credential, deadline)
                if status != expected:
                    raise ValueError('unexpected HTTP status '+str(status))
            except (OSError, ValueError, http.client.HTTPException) as error:
                result.update(state='unconfirmed', connection='failed', failed_stage=stage,
                              cause_type=type(error).__name__)
                if isinstance(error, ValueError):result['reason'] = str(error)
                return result
            result['checks'].append({'stage': stage, 'status': status})
        result.update(state='connected', connection='observed')
    return result


def registered_records(manager):
    response = manager.call('registrations', {})
    if not response['ok']:
        raise ValueError('registered Display targets cannot be verified')
    ids = {row['vm_id'] for row in response['data']['registrations']}
    return {key: row for key, row in manager.p.records(validate_assets=False).items() if row['uuid'] in ids}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('show')
    select = sub.add_parser('select')
    select.add_argument('--vm-id', required=True)
    select.add_argument('--expected-vm-id', required=True)
    select.add_argument('--execute', action='store_true')
    select.add_argument('--restart', action='store_true')
    inspect = sub.add_parser('check')
    inspect.add_argument('--live', action='store_true')
    inspect.add_argument('--record', action='store_true')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('root required')
    if args.action == 'show':
        try:
            value = display._binding_target(ETC/'display-bindings.json', 0)
            print(json.dumps({'state': 'selected' if value else 'not-configured', 'registration': 'not-observed',
                              'bindings': [{'role': row['role'], 'vm_id': row['vm_id']} for row in value['bindings']] if value else []}))
            return 0
        except (OSError, ValueError) as error:
            print(json.dumps({'state': 'unconfirmed', 'cause_type': type(error).__name__}))
            return 1
    from xenova_runtime import ensure_runtime
    target = None
    if args.action == 'check' and args.record:
        target = ensure_runtime(Path('/run/xenova'))/'display-install-check.json'
        atomic_write(target, json.dumps({'state': 'checking', 'connection': 'not-observed',
                     'checked_monotonic_ms': int(time.monotonic()*1000)})+'\n')
    from xenova_management import Manager
    phase, result = 'registration', {}
    try:
        manager = Manager()
        records = registered_records(manager)
        if args.action == 'select':
            phase = 'selection'
            if args.restart and not args.execute:
                raise ValueError('--restart requires --execute')
            record = display._interaction_record(records, args.vm_id)
            from xenova_catalog import plain
            manifest = plain(manager.data/'templates'/record['template']/'template.json')
            if not json.loads(manifest.read_text()).get('guest_display'):
                raise ValueError('selected template has no installed display application receipt')
            result = display.select_interaction(records, args.vm_id, args.expected_vm_id, execute=args.execute,
                bindings_path=ETC/'display-bindings.json', runtime_path=ETC/'display-runtime.json', mobile_path=ETC/'display-mobile.json')
            if args.execute:
                phase = 'services'
                result['services'] = display.restart_changed_services(result, run=display.subprocess.run)
                if args.restart and not result['services']['restarted']:
                    result['services'] = display.restart_changed_services({'bindings': {'state': 'updated'},
                        'ui': {'changed': [name for name in ('display-runtime.json','display-mobile.json') if (ETC/name).is_file()]}},
                        run=display.subprocess.run)
        else:
            phase = 'configuration'
            result = check(records, ETC, live=args.live)
        result['checked_monotonic_ms'] = int(time.monotonic()*1000)
        if target is not None:atomic_write(target, json.dumps(result)+'\n')
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('state') != 'unconfirmed' else 1
    except (OSError, ValueError, http.client.HTTPException) as error:
        result.update(state='unconfirmed', connection='not-observed', cause_type=type(error).__name__,
                      failed_stage=phase, checked_monotonic_ms=int(time.monotonic()*1000))
        if isinstance(error, OSError):result['errno'] = error.errno
        if isinstance(error, ValueError):result['reason'] = str(error)
        if target is not None:atomic_write(target, json.dumps(result)+'\n')
        print(json.dumps(result))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
