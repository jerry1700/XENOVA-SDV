"""보드별 WPA2-Personal 설정과 비밀을 제외한 상태 조회. VM 내부망/NAT는 변경하지 않는다."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from xenova_runtime import PlatformError, atomic_write, operation_lock
from xenova_catalog import plain

INTERFACE=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}\Z')
SUPPLICANT_HEADER = (
    'ctrl_interface=/run/wpa_supplicant\n'
    'update_config=0\n'
    'country={country}\n'
    'disable_scan_offload=1\n'
)
NETWORK_START = 'network={\n'
NETWORK_BODY = re.compile(
    r'    ssid=((?:[0-9a-f]{2}){1,32})\n'
    r'    psk=([0-9a-f]{64})\n'
    r'    key_mgmt=WPA-PSK\n    proto=RSN\n}\n'
)
SSID_ESCAPE = re.compile(rb'\\(?:x([0-9a-fA-F]{2})|(["\\nret]))')
ESCAPED_BYTES = {b'"': b'"', b'\\': b'\\', b'n': b'\n', b'r': b'\r',
                 b'e': b'\x1b', b't': b'\t'}
CONNECT_TIMEOUT = 20


def command(args, timeout=15):
    p=subprocess.run(args,encoding='utf-8',errors='surrogateescape',capture_output=True,timeout=timeout)
    if p.returncode:
        raise PlatformError('Wi-Fi command failed: '+args[0])
    return p.stdout


def read_settings(path):
    """Xenova가 저장한 설정을 읽고 기존 단일 프로필 형식도 허용한다."""
    plain(path)
    if path.stat().st_mode & 0o077:
        raise PlatformError('Wi-Fi configuration must be root-only')
    header, *blocks = path.read_text(encoding='ascii', errors='surrogateescape').split(NETWORK_START)
    saved_country = re.search(r'^country=([A-Z]{2})$', header, re.MULTILINE)
    if not saved_country:
        raise PlatformError('unsupported saved Wi-Fi configuration')
    country = saved_country[1]
    expected_header = SUPPLICANT_HEADER.format(country=country)
    # 이전 Xenova 이미지는 disable_scan_offload 설정을 쓰기 전에 자격증명을 저장했다.
    if header not in (expected_header, expected_header.removesuffix('disable_scan_offload=1\n')):
        raise PlatformError('unsupported saved Wi-Fi configuration')
    profiles = {}
    for block in blocks:
        match = NETWORK_BODY.fullmatch(block)
        if not match or match[1] in profiles:
            raise PlatformError('invalid saved Wi-Fi profile')
        profiles[match[1]] = match[2]
    return country, profiles


def ssid_bytes(ssid):
    if not isinstance(ssid, str):
        raise PlatformError('SSID must be text')
    encoded = ssid.encode('utf-8', errors='surrogateescape')
    if not 1 <= len(encoded) <= 32 or any(ord(c) < 32 for c in ssid):
        raise PlatformError('SSID must be 1..32 UTF-8 bytes without control characters')
    try:
        encoded.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PlatformError('SSID must be valid UTF-8') from exc
    return encoded


def decode_ssid(value):
    """iw 및 wpa_cli의 이스케이프된 SSID를 원래 바이트로 복원한다."""
    def unescape(match):
        if match[1] is not None:
            return bytes.fromhex(match[1].decode('ascii'))
        return ESCAPED_BYTES[match[2]]

    return SSID_ESCAPE.sub(unescape, value.encode('utf-8', errors='surrogateescape'))


def render_settings(country, profiles):
    return SUPPLICANT_HEADER.format(country=country) + ''.join(
        f'{NETWORK_START}    ssid={name}\n    psk={key}\n    key_mgmt=WPA-PSK\n    proto=RSN\n}}\n'
        for name, key in profiles.items()
    )


def settings(ssid, password, country, saved=None):
    """기존 Xenova 설정에서 SSID 하나를 추가하거나 같은 SSID의 PSK를 교체한다."""
    encoded_ssid = ssid_bytes(ssid)
    if country is not None and (not isinstance(country, str) or not re.fullmatch('[A-Z]{2}', country)):
        raise PlatformError('two-letter uppercase regulatory country required')
    if re.fullmatch('[0-9a-fA-F]{64}',password):
        psk=password.lower()
    elif 8<=len(password)<=63 and all(32<=ord(c)<=126 for c in password):
        psk=hashlib.pbkdf2_hmac('sha1',password.encode('ascii'),encoded_ssid,4096,32).hex()
    else:
        raise PlatformError('WPA2 PSK must be 8..63 printable ASCII characters or 64 hex digits')
    previous_country, profiles = read_settings(saved) if saved is not None else ('KR', {})
    profiles[encoded_ssid.hex()] = psk
    return render_settings(country or previous_country, profiles)


class ActivationError(PlatformError):
    """자격증명/하위 명령 출력을 포함하지 않는 활성화 단계 오류."""
    def __init__(self, action, stage):
        self.activation = {'action': action, 'result': 'failed', 'stage': stage}
        super().__init__('Wi-Fi activation failed during ' + stage + '; saved configuration retained')


class Wifi:
    def __init__(self, root=Path('/'), run=command):
        self.root=Path(root);self.run=run

    def config_path(self, name):
        if not isinstance(name,str) or not INTERFACE.fullmatch(name):
            raise PlatformError('invalid wireless interface name')
        return self.root / 'etc/xenova/wifi' / (name + '.conf')

    def interface(self, name):
        self.config_path(name)
        path=self.root/'sys/class/net'/name
        if not ((path/'wireless').is_dir() or (path/'uevent').is_file() and 'DEVTYPE=wlan' in (path/'uevent').read_text().splitlines()):
            raise PlatformError('interface is not an existing wireless device')
        if (path/'master').exists() or (path/'master').is_symlink():
            raise PlatformError('wireless device already has a master; no bridge changes performed')

    def configure(self, name, ssid, password, country, *, activate=False):
        """프로필을 저장하고 요청 시 같은 잠금 안에서 전체 목록을 적용한다."""
        self.interface(name)
        with operation_lock(self.root/'run/xenova-wifi'):
            directory=self.root/'etc/xenova/wifi'
            current=self.root
            for component in ('etc','xenova'):
                current=current/component
                if not current.exists() and not current.is_symlink():
                    current.mkdir()
                plain(current,directory=True)
            directory.mkdir(mode=0o700,exist_ok=True)
            plain(directory,directory=True);directory.chmod(0o700)
            target=directory/(name+'.conf')
            saved = target if target.exists() or target.is_symlink() else None
            content = settings(ssid, password, country, saved)
            network=self.root/'etc/systemd/network'
            systemd=self.root/'etc/systemd'
            if not systemd.exists() and not systemd.is_symlink():
                systemd.mkdir()
            plain(systemd,directory=True)
            if not network.exists() and not network.is_symlink():
                network.mkdir()
            plain(network,directory=True)
            path=network/'25-xenova-wifi.network'
            if path.exists() or path.is_symlink():
                plain(path)
            if [p for p in directory.glob('*.conf') if p.name!=target.name]:
                raise PlatformError('another wireless interface is configured; explicit removal required')
            atomic_write(target,content,0o600)
            atomic_write(path,'[Match]\nName='+name+'\nType=wlan\n\n[Link]\nRequiredForOnline=routable\n\n[Network]\nDHCP=ipv4\nIPv6AcceptRA=no\n\n[DHCPv4]\nRouteMetric=600\n', 0o644)
            # root 셸의 umask가 0077이어도 비밀 없는 networkd 입력은 읽을 수 있어야 한다.
            path.chmod(0o644)
            result = self._activate_locked(name) if activate else {'configured': True, 'interface': name}
            result.update(credentials='stored root-only', activation_required=not activate)
            return result

    def activate(self, name):
        """소유한 단일 서비스를 시작/재시작한다. 요청 성공은 AP 인증/IP 성공과 별개다."""
        self.interface(name)
        with operation_lock(self.root / 'run/xenova-wifi'):
            conf = plain(self.root / 'etc/xenova/wifi' / (name + '.conf'))
            if conf.stat().st_mode & 0o077:
                raise PlatformError('Wi-Fi configuration must be root-only')
            return self._activate_locked(name)

    def _activate_locked(self, name):
        unit = 'xenova-wifi@' + name + '.service'
        action, stage = None, 'state-query'
        try:
            state = self.run(['systemctl', 'show', '--property=ActiveState', '--value', unit]).strip()
            if state not in ('active', 'inactive', 'failed'):
                raise ActivationError(None, 'service-state')
            action = 'restart' if state == 'active' else 'start'
            stage = 'enable'
            self.run(['systemctl', 'enable', unit])
            stage = 'supplicant'
            self.run(['systemctl', action, unit])
            stage = 'network-reload'
            self.run(['networkctl', 'reload'])
            stage = 'network-reconfigure'
            self.run(['networkctl', 'reconfigure', name])
        except ActivationError:
            raise
        except (PlatformError, OSError, subprocess.SubprocessError) as exc:
            raise ActivationError(action, stage) from exc
        result = self.status(name)
        result['activation'] = {'action': action, 'result': 'requested', 'stage': 'complete'}
        return result

    def scan(self, name):
        """새 스캔 결과를 읽고 스캔 때문에 올린 인터페이스는 원래 상태로 돌린다."""
        self.interface(name)
        with operation_lock(self.root / 'run/xenova-wifi'):
            flags = self.root / 'sys/class/net' / name / 'flags'
            was_up = int(flags.read_text(), 16) & 1
            if not was_up:
                self.run(['ip', 'link', 'set', 'dev', name, 'up'])
            try:
                output = self.run(['iw', 'dev', name, 'scan'])
                if output.strip() == 'scan aborted!':
                    raise PlatformError('Wi-Fi scan aborted; try scanning again')
            finally:
                if not was_up:
                    self.run(['ip', 'link', 'set', 'dev', name, 'down'])

        networks = []
        for block in re.split(r'^BSS ', output, flags=re.MULTILINE)[1:]:
            bssid = re.match(r'((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\b', block)
            ssid = re.search(r'^\tSSID: (.*)$', block, re.MULTILINE)
            if not bssid or not ssid or not ssid[1]:
                continue
            signal = re.search(r'^\tsignal: (-?\d+(?:\.\d+)?) dBm$', block, re.MULTILINE)
            frequency = re.search(r'^\tfreq: (\d+)$', block, re.MULTILINE)
            networks.append({
                'ssid': decode_ssid(ssid[1]).decode('utf-8', errors='replace'),
                'bssid': bssid[1],
                'signal_dbm': float(signal[1]) if signal else None,
                'frequency_mhz': int(frequency[1]) if frequency else None,
            })
        return {'interface': name, 'networks': networks}

    def _wpa_ok(self, name, action, *args):
        if self.run(['wpa_cli', '-i', name, action, *args]).strip() != 'OK':
            raise PlatformError('Wi-Fi supplicant rejected ' + action)

    def _wpa_status(self, name):
        return dict(line.split('=', 1) for line in
                    self.run(['wpa_cli', '-i', name, 'status']).splitlines() if '=' in line)

    def connect(self, name, ssid):
        """저장된 SSID로 인증을 시도하고 임시 우선순위를 원래 값으로 돌린다."""
        self.interface(name)
        target = ssid_bytes(ssid)
        path = self.config_path(name)
        with operation_lock(self.root / 'run/xenova-wifi'):
            _, profiles = read_settings(path) if path.exists() or path.is_symlink() else (None, {})
            if target.hex() not in profiles:
                raise PlatformError('Wi-Fi SSID is not saved. Use configure first.')

            unit = 'xenova-wifi@' + name + '.service'
            if self.run(['systemctl', 'show', '--property=ActiveState', '--value', unit]).strip() != 'active':
                self._activate_locked(name)

            rows = self.run(['wpa_cli', '-i', name, 'list_networks']).splitlines()
            network_ids = []
            for row in rows[1:]:
                fields = row.split('\t', 3)
                if len(fields) == 4 and re.fullmatch('[0-9]+', fields[0]) and decode_ssid(fields[1]) == target:
                    network_ids.append(fields[0])
            if len(network_ids) != 1:
                raise PlatformError('cannot identify saved SSID in the running Wi-Fi service; use activate to reload')
            network_id, = network_ids

            status = self._wpa_status(name)
            if status.get('id') != network_id or status.get('wpa_state') != 'COMPLETED':
                priority = self.run(['wpa_cli', '-i', name, 'get_network', network_id, 'priority']).strip()
                if not re.fullmatch(r'-?[0-9]+', priority):
                    raise PlatformError('cannot read Wi-Fi network priority')
                # SELECT_NETWORK는 다른 후보를 비활성화하므로 우선순위만 잠시 조정한다.
                try:
                    self._wpa_ok(name, 'set_network', network_id, 'priority', '1')
                    self._wpa_ok(name, 'reassociate')
                    deadline = time.monotonic() + CONNECT_TIMEOUT
                    while True:
                        status = self._wpa_status(name)
                        if status.get('id') == network_id and status.get('wpa_state') == 'COMPLETED':
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise PlatformError('Wi-Fi authentication did not complete for the requested SSID')
                        time.sleep(min(0.25, remaining))
                finally:
                    self._wpa_ok(name, 'set_network', network_id, 'priority', priority)
        return {'interface': name, 'ssid': ssid, 'authentication': 'COMPLETED'}

    def forget(self, name, ssid):
        """저장 프로필을 삭제하고 실행 중인 서비스에 반영한다."""
        path = self.config_path(name)
        target = ssid_bytes(ssid).hex()
        with operation_lock(self.root / 'run/xenova-wifi'):
            country, profiles = read_settings(path) if path.exists() or path.is_symlink() else (None, {})
            if target not in profiles:
                raise PlatformError('Wi-Fi SSID is not saved')
            del profiles[target]
            atomic_write(path, render_settings(country, profiles), 0o600)
            try:
                self.run(['systemctl', 'try-restart', 'xenova-wifi@' + name + '.service'])
            except (PlatformError, OSError, subprocess.SubprocessError) as exc:
                raise ActivationError('try-restart', 'supplicant') from exc
        return {'interface': name, 'ssid': ssid, 'forgotten': True}

    def status(self,name):
        self.interface(name)
        configured=(self.root/'etc/xenova/wifi'/(name+'.conf')).is_file()
        result={'interface':name,'configured':configured,'authentication':'not-configured',
                'ipv4':[],'internal_subnet_overlap':False,'errors':[]}
        if configured:
            try:
                values=self._wpa_status(name)
                result['authentication']=values.get('wpa_state','unknown')
            except (PlatformError,subprocess.SubprocessError,OSError):
                result['authentication']='unavailable';result['errors'].append('supplicant status unavailable')
        try:
            rows=json.loads(self.run(['ip','-j','-4','address','show','dev',name]))
            for row in rows:
                for address in row.get('addr_info',[]):
                    local,prefix=address['local'],address['prefixlen']
                    result['ipv4'].append(local+'/'+str(prefix))
                    network=ipaddress.ip_network(local+'/'+str(prefix),strict=False)
                    result['internal_subnet_overlap'] |= network.overlaps(ipaddress.ip_network('192.168.77.0/24'))
        except (KeyError,TypeError,ValueError,PlatformError,subprocess.SubprocessError,OSError):
            result['errors'].append('address query failed')
        if result['internal_subnet_overlap']:
            result['errors'].append('management network overlaps guest subnet; network is not changed')
        return result

    def list_saved(self, name):
        """디스크에 저장된 SSID를 서비스 상태와 무관하게 조회한다."""
        path = self.config_path(name)
        ssids = []
        if path.exists() or path.is_symlink():
            _, profiles = read_settings(path)
            try:
                ssids = [bytes.fromhex(ssid).decode('utf-8') for ssid in profiles]
            except UnicodeDecodeError as exc:
                raise PlatformError('invalid saved Wi-Fi SSID') from exc
        return {'interface': name, 'ssids': ssids}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['configure','status','activate','list','scan','connect','forget'])
    p.add_argument('ssid', nargs='?', metavar='SSID')
    p.add_argument('--interface',default='wlan0')
    p.add_argument('--country')
    args=p.parse_intermixed_args(argv)
    if args.action in ('connect', 'forget') and args.ssid is None:
        p.error('connect and forget require an SSID')
    if args.ssid is not None and args.action not in ('connect', 'forget'):
        p.error('only connect and forget accept an SSID argument')
    wifi=Wifi()
    try:
        if args.action == 'configure':
            if not sys.stdin.isatty():
                raise PlatformError('configure requires an interactive terminal; no password arguments accepted')
            ssid = input('Wi-Fi SSID: ')
            password = input('Wi-Fi PSK: ')
            result = wifi.configure(args.interface, ssid, password, args.country, activate=True)
        elif args.action == 'activate':
            result = wifi.activate(args.interface)
        elif args.action == 'list':
            result = wifi.list_saved(args.interface)
        elif args.action == 'scan':
            result = wifi.scan(args.interface)
        elif args.action == 'connect':
            result = wifi.connect(args.interface, args.ssid)
        elif args.action == 'forget':
            result = wifi.forget(args.interface, args.ssid)
        else:
            result = wifi.status(args.interface)
    except ActivationError as exc:
        result = {'interface': args.interface, 'activation': exc.activation, 'error': str(exc)}
        if args.action == 'configure':
            result.update(configured=True, credentials='stored root-only', activation_required=True)
        elif args.action == 'forget':
            result.update(forgotten=True, ssid=args.ssid, activation_required=True)
        print(json.dumps(result, indent=2))
        return 1
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except PlatformError as exc:
        print(f'[xenova-wifi] {exc}', file=sys.stderr)
        sys.exit(1)
    except (OSError,subprocess.SubprocessError):
        print('[xenova-wifi] operation failed; check interface, root-only configuration and service status',file=sys.stderr)
        sys.exit(1)
