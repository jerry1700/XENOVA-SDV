"""소유한 supplicant 명령과 설정 소비를 모델링한다. RF/AP 접속 시험은 아니다."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from first_install_fixture import ROOT
import xenova_wifi as wifi
from xenova_runtime import PlatformError


class SupplicantModel:
    def __init__(self, root, active):
        self.root, self.active = root, active
        self.loaded = (root / 'etc/xenova/wifi/wlan0.conf').read_bytes() if active else None
        self.calls = []
        self.fail = None

    def __call__(self, args):
        self.calls.append(args)
        if self.fail and args[:2] == self.fail:
            raise PlatformError('synthetic secret must not reach output')
        if args[:2] == ['systemctl', 'show']:
            return 'active\n' if self.active else 'inactive\n'
        if args[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) or (
            args[:2] == ['systemctl', 'try-restart'] and self.active
        ):
            self.active = True
            self.loaded = (self.root / 'etc/xenova/wifi/wlan0.conf').read_bytes()
        if args[0] == 'ip':
            return '[{"addr_info":[]}]'
        if args[0] == 'wpa_cli':
            return 'wpa_state=SCANNING\n'
        return ''


class WifiReapplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'sys/class/net/wlan0/wireless').mkdir(parents=True)
        self.wifi = wifi.Wifi(self.root)
        self.wifi.configure('wlan0', 'old-fixture', 'old-fixture-password', 'KR')

    def run_cli(self, model, action, *args):
        self.wifi.run = model
        output = io.StringIO()
        with (patch.object(wifi, 'Wifi', return_value=self.wifi),
              patch.object(wifi.sys.stdin, 'isatty', return_value=True),
              patch('builtins.input', side_effect=['new-fixture', 'new-fixture-password']),
              redirect_stdout(output)):
            code = wifi.main([action, *args, '--interface', 'wlan0'])
        return code, json.loads(output.getvalue())

    def activate_cli(self, model):
        return self.run_cli(model, 'activate')

    def test_configure_enables_at_boot_and_starts_service_on_first_profile(self):
        (self.root / 'etc/xenova/wifi/wlan0.conf').unlink()
        model = SupplicantModel(self.root, False)

        code, result = self.run_cli(model, 'configure')

        self.assertEqual(code, 0)
        self.assertTrue(result['configured'])
        self.assertFalse(result['activation_required'])
        self.assertEqual(result['activation']['action'], 'start')
        self.assertEqual(result['authentication'], 'SCANNING')
        self.assertEqual(model.calls[1:3], [
            ['systemctl', 'enable', 'xenova-wifi@wlan0.service'],
            ['systemctl', 'start', 'xenova-wifi@wlan0.service'],
        ])
        self.assertEqual(model.loaded, (self.root / 'etc/xenova/wifi/wlan0.conf').read_bytes())

    def test_active_service_reads_changed_credentials_and_only_owned_service_restarts(self):
        model = SupplicantModel(self.root, True)
        before = model.loaded
        code, result = self.run_cli(model, 'configure')
        self.assertNotEqual(model.loaded, before)
        self.assertEqual(model.loaded, (self.root / 'etc/xenova/wifi/wlan0.conf').read_bytes())
        self.assertEqual(model.loaded.count(b'network={\n'), 2)
        self.assertIn(before.split(b'network={\n', 1)[1], model.loaded)
        self.assertIn(b'\ndisable_scan_offload=1\n',model.loaded)
        self.assertEqual(code, 0)
        self.assertEqual(result['activation']['action'], 'restart')
        self.assertEqual(result['activation']['result'], 'requested')
        self.assertEqual(result['authentication'], 'SCANNING')
        self.assertEqual([a for a in model.calls if a[:2] == ['systemctl', 'restart']],
                         [['systemctl', 'restart', 'xenova-wifi@wlan0.service']])
        self.assertFalse(any('xenova-dhcp' in ' '.join(a) or 'xenbr0' in a for a in model.calls))
        self.assertNotIn('new-fixture', json.dumps(result))

    def test_same_ssid_updates_only_its_credentials(self):
        self.wifi.configure('wlan0', 'backup-fixture', 'backup-fixture-password', 'KR')
        self.wifi.configure('wlan0', 'old-fixture', 'replacement-password', 'KR')

        config = (self.root / 'etc/xenova/wifi/wlan0.conf').read_text()
        _, primary, backup = config.split('network={\n')
        self.assertEqual(primary, wifi.settings(
            'old-fixture', 'replacement-password', 'KR').split('network={\n', 1)[1])
        self.assertEqual(backup, wifi.settings(
            'backup-fixture', 'backup-fixture-password', 'KR').split('network={\n', 1)[1])

    def test_unsupported_existing_config_is_preserved(self):
        credentials = self.root / 'etc/xenova/wifi/wlan0.conf'
        before = credentials.read_bytes() + b'# custom configuration\n'
        credentials.write_bytes(before)

        with self.assertRaises(PlatformError):
            self.wifi.configure('wlan0', 'new-fixture', 'new-fixture-password', 'KR')
        self.assertEqual(credentials.read_bytes(), before)

    def test_legacy_config_retains_country_and_credentials(self):
        credentials = self.root / 'etc/xenova/wifi/wlan0.conf'
        legacy = wifi.settings('old-fixture', 'old-fixture-password', 'US').replace(
            'disable_scan_offload=1\n', '')
        credentials.write_text(legacy)

        self.wifi.configure('wlan0', 'new-fixture', 'new-fixture-password', None)

        config = credentials.read_text()
        self.assertIn('country=US\ndisable_scan_offload=1\n', config)
        self.assertEqual(config.count('network={\n'), 2)
        self.assertIn(legacy.split('network={\n', 1)[1], config)

    def test_list_shows_saved_ssids_without_running_supplicant_or_disclosing_psks(self):
        self.wifi.configure('wlan0', '휴대폰 핫스팟', 'backup-fixture-password', 'KR')
        model = SupplicantModel(self.root, False)

        code, result = self.run_cli(model, 'list')

        self.assertEqual(code, 0)
        self.assertEqual(result, {'interface': 'wlan0', 'ssids': ['old-fixture', '휴대폰 핫스팟']})
        self.assertEqual(model.calls, [])

    def test_list_with_no_saved_profiles_is_empty(self):
        self.assertEqual(self.wifi.list_saved('wlan1'), {'interface': 'wlan1', 'ssids': []})

    def test_scan_before_configuration_decodes_ssids_and_restores_link(self):
        (self.root / 'etc/xenova/wifi/wlan0.conf').unlink()
        (self.root / 'sys/class/net/wlan0/flags').write_text('0x1002\n')
        calls = []

        def run(args):
            calls.append(args)
            if args[0] == 'iw':
                return ('BSS 12:34:56:78:90:ab(on wlan0)\n'
                        '\tfreq: 2412\n\tsignal: -52.00 dBm\n'
                        '\tSSID: \\xed\\x95\\xab Spot\n')
            return ''

        code, result = self.run_cli(run, 'scan')

        self.assertEqual(code, 0)
        self.assertEqual(result['networks'], [{
            'ssid': '핫 Spot', 'bssid': '12:34:56:78:90:ab',
            'frequency_mhz': 2412, 'signal_dbm': -52.0,
        }])
        self.assertEqual(calls, [
            ['ip', 'link', 'set', 'dev', 'wlan0', 'up'],
            ['iw', 'dev', 'wlan0', 'scan'],
            ['ip', 'link', 'set', 'dev', 'wlan0', 'down'],
        ])

    def connect_model(self, *, authenticates):
        priority = '0'
        associated = False

        def run(args):
            nonlocal priority, associated
            if args[0] == 'systemctl':
                return 'active\n'
            command, *params = args[3:]
            if command == 'list_networks':
                return ('network id / ssid / bssid / flags\n'
                        '7\told-fixture\tany\t[CURRENT]\n'
                        '2\t\\xed\\x95\\xab spot\tany\t\n')
            if command == 'get_network':
                self.assertEqual(params, ['2', 'priority'])
                return priority
            if command == 'set_network':
                self.assertEqual(params[:2], ['2', 'priority'])
                priority = params[2]
                return 'OK\n'
            if command == 'reassociate':
                self.assertEqual(priority, '1')
                associated = authenticates
                return 'OK\n'
            if command == 'status':
                return f'id={2 if associated else 7}\nwpa_state=COMPLETED\n'
            self.fail(f'unexpected command {command}')

        return run

    def test_connect_reports_target_authentication_and_preserves_saved_profiles(self):
        self.wifi.configure('wlan0', '핫 spot', 'backup-fixture-password', 'KR')
        config = self.root / 'etc/xenova/wifi/wlan0.conf'
        before = config.read_bytes()
        run = self.connect_model(authenticates=True)

        code, result = self.run_cli(run, 'connect', '핫 spot')

        self.assertEqual(code, 0)
        self.assertEqual(result, {'interface': 'wlan0', 'ssid': '핫 spot', 'authentication': 'COMPLETED'})
        self.assertEqual(run(['wpa_cli', '-i', 'wlan0', 'get_network', '2', 'priority']), '0')
        self.assertEqual(config.read_bytes(), before)

    def test_connect_timeout_restores_priority_when_only_old_network_authenticates(self):
        self.wifi.configure('wlan0', '핫 spot', 'backup-fixture-password', 'KR')
        run = self.connect_model(authenticates=False)
        with patch.object(wifi, 'CONNECT_TIMEOUT', 0):
            with self.assertRaisesRegex(PlatformError, 'authentication did not complete'):
                self.run_cli(run, 'connect', '핫 spot')

        self.assertEqual(run(['wpa_cli', '-i', 'wlan0', 'get_network', '2', 'priority']), '0')

    def test_forget_applies_removal_and_allows_registering_after_last_profile(self):
        self.wifi.configure('wlan0', 'backup-fixture', 'backup-fixture-password', 'KR')
        model = SupplicantModel(self.root, True)

        self.assertEqual(self.run_cli(model, 'forget', 'old-fixture')[1]['forgotten'], True)
        self.assertEqual(self.wifi.list_saved('wlan0')['ssids'], ['backup-fixture'])
        self.assertNotIn(b'old-fixture'.hex().encode(), model.loaded)
        self.assertEqual(self.run_cli(model, 'forget', 'backup-fixture')[0], 0)
        self.assertEqual(self.wifi.list_saved('wlan0')['ssids'], [])
        self.assertNotIn(b'psk=', model.loaded)
        self.assertEqual(self.run_cli(model, 'configure')[0], 0)
        self.assertEqual(self.wifi.list_saved('wlan0')['ssids'], ['new-fixture'])

    def test_inactive_service_starts_once_without_restart(self):
        model = SupplicantModel(self.root, False)
        code, result = self.activate_cli(model)
        self.assertEqual(code, 0)
        self.assertEqual(result['activation']['action'], 'start')
        self.assertEqual([a for a in model.calls if a[:2] == ['systemctl', 'start']],
                         [['systemctl', 'start', 'xenova-wifi@wlan0.service']])
        self.assertFalse(any(a[:2] == ['systemctl', 'restart'] for a in model.calls))

    def test_reapply_failure_keeps_saved_config_reports_phase_and_skips_network_changes(self):
        model = SupplicantModel(self.root, True)
        previous = model.loaded
        model.fail = ['systemctl', 'restart']
        code, result = self.run_cli(model, 'configure')
        self.assertEqual(code, 1)
        self.assertTrue(result['configured'])
        self.assertTrue(result['activation_required'])
        self.assertEqual(result['activation']['action'], 'restart')
        self.assertEqual(result['activation']['result'], 'failed')
        self.assertEqual(result['activation']['stage'], 'supplicant')
        self.assertEqual(model.loaded, previous)
        saved = (self.root / 'etc/xenova/wifi/wlan0.conf').read_bytes()
        self.assertEqual(saved.count(b'network={\n'), 2)
        self.assertIn(b'    ssid=' + b'new-fixture'.hex().encode() + b'\n', saved)
        self.assertFalse(any(a[0] == 'networkctl' for a in model.calls))
        self.assertNotIn('synthetic secret', json.dumps(result))
        self.assertNotIn('new-fixture', json.dumps(result))
        model.fail = None
        retry_code, _ = self.activate_cli(model)
        self.assertEqual(retry_code, 0)
        self.assertEqual(model.loaded, saved)

    def test_network_reconfiguration_failure_is_not_connection_success(self):
        model = SupplicantModel(self.root, True)
        model.fail = ['networkctl', 'reconfigure']
        code, result = self.activate_cli(model)
        self.assertEqual(code, 1)
        self.assertEqual(result['activation']['stage'], 'network-reconfigure')
        self.assertEqual(result['activation']['result'], 'failed')

    def test_activation_query_failure_does_not_start_or_restart(self):
        model = SupplicantModel(self.root, True)
        model.fail = ['systemctl', 'show']
        code, result = self.activate_cli(model)
        self.assertEqual(code, 1)
        self.assertEqual(result['activation']['stage'], 'state-query')
        self.assertFalse(any(a[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) for a in model.calls))

    def test_first_start_failure_is_distinct_from_reapply_failure(self):
        model = SupplicantModel(self.root, False)
        model.fail = ['systemctl', 'start']
        code, result = self.activate_cli(model)
        self.assertEqual(code, 1)
        self.assertEqual(result['activation'], {'action': 'start', 'result': 'failed', 'stage': 'supplicant'})
        self.assertFalse(any(a[0] == 'networkctl' for a in model.calls))

    def test_transitional_or_malformed_service_state_never_starts_second_supplicant(self):
        for state in ('activating', 'deactivating', '', 'invalid'):
            with self.subTest(state=state):
                model = SupplicantModel(self.root, True)
                def run(args):
                    if args[:2] == ['systemctl', 'show']:
                        model.calls.append(args)
                        return state
                    return model(args)
                self.wifi.run = run
                with self.assertRaises(wifi.ActivationError) as caught:
                    self.wifi.activate('wlan0')
                self.assertEqual(caught.exception.activation['stage'], 'service-state')
                self.assertFalse(any(a[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) for a in model.calls))

    def test_networkd_configuration_is_readable_and_credentials_remain_private(self):
        network = self.root / 'etc/systemd/network/25-xenova-wifi.network'
        credentials = self.root / 'etc/xenova/wifi/wlan0.conf'
        self.assertEqual(network.stat().st_mode & 0o777, 0o644)
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(credentials.parent.stat().st_mode & 0o777, 0o700)
        settings = network.read_text()
        for expected in ('RequiredForOnline=routable', 'DHCP=ipv4',
                         'IPv6AcceptRA=no', 'RouteMetric=600'):
            self.assertIn(expected, settings)

    def configure_with_private_umask(self):
        previous = os.umask(0o077)
        try:
            self.wifi.configure('wlan0', 'private-umask-fixture', 'private-umask-password', 'KR')
        finally:
            os.umask(previous)

    def test_private_caller_umask_does_not_make_public_network_file_unreadable(self):
        self.configure_with_private_umask()
        self.test_networkd_configuration_is_readable_and_credentials_remain_private()

    @unittest.skipUnless(os.geteuid() == 0, 'actual consumer UID test requires isolated root invocation')
    def test_actual_unprivileged_consumer_reads_network_file_but_not_psk(self):
        import pwd
        self.configure_with_private_umask()
        consumer = pwd.getpwnam('systemd-network')
        self.root.chmod(0o755)
        network = self.root / 'etc/systemd/network/25-xenova-wifi.network'
        credentials = self.root / 'etc/xenova/wifi/wlan0.conf'
        script = """import os,sys
os.setgroups([])
os.setgid(int(sys.argv[1]))
os.setuid(int(sys.argv[2]))
assert '[Match]' in open(sys.argv[3]).read()
try:
    open(sys.argv[4]).read()
except PermissionError:
    pass
else:
    raise AssertionError('PSK readable by network consumer')
"""
        result = subprocess.run([sys.executable, '-c', script, str(consumer.pw_gid), str(consumer.pw_uid),
                                 str(network), str(credentials)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
