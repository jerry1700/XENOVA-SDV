"""Interaction VM 동적 주소의 dom0 Display 설정 반영 계약."""
import json
from pathlib import Path
import stat
import tempfile
import unittest

from first_install_fixture import ROOT  # noqa: F401 - platform module path setup
import xenova_display_sync as display


EDID = 'ce1acb6e187b736d79d8103859f765f9f970863ad45b38fad07f2be5cb4c0275'
PRIMARY = '11111111-1111-4111-8111-111111111111'
BACKUP = '22222222-2222-4222-8222-222222222222'
INTERACTION = '33333333-3333-4333-8333-333333333333'


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


class DisplayBindingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xenova-display-bindings-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'display-bindings.json'
        self.records = {
            'zephyr-drive-a': {'id': 'zephyr-drive-a', 'uuid': PRIMARY, 'guest_kind': 'zephyr'},
            'zephyr-drive-b': {'id': 'zephyr-drive-b', 'uuid': BACKUP, 'guest_kind': 'zephyr'},
            'vm-interaction': {'id': 'vm-interaction', 'uuid': INTERACTION,
                'guest_kind': 'linux', 'template_id': 'debian-13-arm64'},
        }

    def sync(self, records=None):
        return display.sync_bindings(records or self.records, bindings_path=self.path,
                                     owner_uid=self.path.parent.stat().st_uid)

    def test_publishes_three_current_deployment_bindings(self):
        result = self.sync()
        value = json.loads(self.path.read_text())
        self.assertEqual(result['state'], 'updated')
        self.assertEqual(value['scope'], 'deployment')
        self.assertEqual([row['vm_id'] for row in value['bindings']],
                         [PRIMARY, BACKUP, INTERACTION])
        self.assertEqual([row['role'] for row in value['bindings']],
                         ['primary_drive', 'backup_drive', 'interaction'])
        for row in value['bindings'][:2]:
            self.assertEqual(row['app_ids'], ['autonomous', 'manual'])
            self.assertEqual(row['app_topic_scope'], 'vm_scoped')
            self.assertEqual(row['report_profile'], 'warm-drive-1')
        self.assertEqual(value['bindings'][2]['app_id'], 'interaction')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertEqual(self.sync()['state'], 'unchanged')

    def test_replaces_stale_safe_binding_with_new_interaction_uuid(self):
        self.sync()
        replacement = '44444444-4444-4444-8444-444444444444'
        self.records['vm-interaction']['uuid'] = replacement
        self.assertEqual(self.sync()['state'], 'updated')
        value = json.loads(self.path.read_text())
        self.assertEqual(value['bindings'][2]['vm_id'], replacement)

    def test_requires_all_three_unambiguous_registered_roles(self):
        for missing in ('zephyr-drive-a', 'zephyr-drive-b', 'vm-interaction'):
            records = {key: value for key, value in self.records.items() if key != missing}
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'requires exactly one'):
                self.sync(records)
        duplicate = {**self.records, 'another-interaction': {
            'id': 'another-interaction', 'uuid': '55555555-5555-4555-8555-555555555555',
            'guest_kind': 'linux', 'template_id': 'debian-13-arm64'}}
        with self.assertRaisesRegex(ValueError, 'exactly one Debian'):
            self.sync(duplicate)

    def test_rejects_invalid_identity_and_unsafe_existing_target(self):
        self.records['zephyr-drive-a']['uuid'] = 'not-a-uuid'
        with self.assertRaisesRegex(ValueError, 'valid VM UUID'):
            self.sync()
        self.records['zephyr-drive-a']['uuid'] = PRIMARY
        self.path.write_text('{}');self.path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'unsafe Display configuration'):
            self.sync()


if __name__ == '__main__':
    unittest.main()
