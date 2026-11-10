"""Interaction VM 동적 주소의 dom0 Display 설정 반영 계약."""
import json
from pathlib import Path
import stat
import tempfile
import unittest

from first_install_fixture import ROOT  # noqa: F401 - platform module path setup
import xenova_display_sync as display


EDID = 'ce1acb6e187b736d79d8103859f765f9f970863ad45b38fad07f2be5cb4c0275'


class DisplaySyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xenova-display-sync-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'display-runtime.json'
        self.mobile = self.root / 'display-mobile.json'
        self.runtime.write_text(json.dumps({
            'backend': 'x11', 'display': ':5', 'feed_origin': 'http://127.0.0.1:8082',
            'ui_origin': 'http://192.168.77.20:8086',
            'outputs': [{'output_id': 'monitor-1', 'connector': 'HDMI-1',
                         'edid_sha256': EDID, 'x': 0, 'y': 0,
                         'width': 1920, 'height': 1080, 'page': '/display/mobile'}]}))
        self.runtime.chmod(0o644)
        self.mobile.write_text(json.dumps({
            'schema_version': 1, 'usb_interface': 'usb0',
            'phone_ipv4': '10.104.83.148', 'port': 8084}))
        self.mobile.chmod(0o600)

    def sync(self, address='192.168.77.6'):
        return display.sync_interaction_ui(address, runtime_path=self.runtime,
                    mobile_path=self.mobile, owner_uid=self.runtime.stat().st_uid)

    def test_updates_only_dynamic_origin_and_combined_page(self):
        result = self.sync()
        runtime = json.loads(self.runtime.read_text())
        mobile = json.loads(self.mobile.read_text())
        self.assertEqual(result['changed'], ['display-runtime.json', 'display-mobile.json'])
        self.assertEqual(runtime['ui_origin'], 'http://192.168.77.6:8086')
        self.assertEqual(runtime['outputs'][0]['page'], '/display/combined')
        self.assertEqual(runtime['outputs'][0]['edid_sha256'], EDID)
        self.assertEqual((runtime['outputs'][0]['width'], runtime['outputs'][0]['height']), (1920, 1080))
        self.assertEqual(mobile, {'schema_version': 1, 'usb_interface': 'usb0',
            'phone_ipv4': '10.104.83.148', 'port': 8084,
            'ui_origin': 'http://192.168.77.6:8086'})
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.mobile.stat().st_mode), 0o600)

    def test_is_idempotent_and_mobile_is_optional(self):
        self.sync()
        self.assertEqual(self.sync()['state'], 'unchanged')
        self.mobile.unlink()
        self.assertEqual(self.sync('192.168.77.9')['state'], 'updated')

    def test_validates_every_input_before_mutating_any_file(self):
        before = self.runtime.read_bytes()
        self.mobile.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'unsafe Display configuration'):
            self.sync()
        self.assertEqual(self.runtime.read_bytes(), before)

    def test_rejects_uninstalled_hdmi_and_invalid_addresses(self):
        value = json.loads(self.runtime.read_text())
        value['outputs'][0]['edid_sha256'] = '0' * 64
        self.runtime.write_text(json.dumps(value));self.runtime.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'single-HDMI Plan A'):
            self.sync()
        for address in ('192.168.77.1', '192.168.77.255', '8.8.8.8', 'invalid'):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, 'assigned Xen bridge'):
                self.sync(address)

    def test_absent_settings_report_not_configured(self):
        self.runtime.unlink();self.mobile.unlink()
        result = self.sync()
        self.assertEqual(result['state'], 'not-configured')
        self.assertEqual(result['changed'], [])


if __name__ == '__main__':
    unittest.main()
