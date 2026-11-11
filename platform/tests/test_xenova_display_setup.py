"""실제 설정 writer·첫 설치·HTTP 수신부의 표시 제공 계약. VM/장치 경계만 fixture다."""
import copy
from contextlib import redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from first_install_fixture import ROOT, platform_root
sys.path.insert(0, str(ROOT/'platform/deploy/mosquitto/scripts'))
sys.path.insert(0, str(ROOT/'platform/services/display/router'))
import xenova_display_setup as setup
import xenova_display_sync as display
import xenova_provisioning as provisioning
import xenova_broker as broker
import test_xenova_display_sync as sync_fixture
INTERACTION = sync_fixture.INTERACTION

NEW_VM = '44444444-4444-4444-8444-444444444444'


@unittest.skipUnless(os.geteuid() == 0, 'actual root-owned private fixture files')
class DisplaySetupTest(unittest.TestCase):
    def setUp(self):
        self.bindings = sync_fixture.DisplayBindingsTest('runTest');self.bindings.setUp()
        self.addCleanup(self.bindings.doCleanups)
        self.ui = sync_fixture.DisplaySyncTest('runTest');self.ui.setUp();self.addCleanup(self.ui.doCleanups)
        self.etc = self.bindings.path.parent
        self.records = self.bindings.records
        self.records['vm-interaction']['ipv4'] = '192.168.77.6'
        self.records['vm-interaction']['template'] = 'fixture-display-template'
        self.new = {**self.records['vm-interaction'], 'id': 'additional', 'uuid': NEW_VM, 'ipv4': '192.168.77.9'}
        self.state = {'identities': {'debian-dev': {'uuid': INTERACTION}}}
        self.ui.runtime.replace(self.etc/'display-runtime.json')
        self.ui.mobile.replace(self.etc/'display-mobile.json')
        self.runtime, self.mobile = self.etc/'display-runtime.json', self.etc/'display-mobile.json'
        self.acl = self.etc/'broker';self.acl.mkdir(mode=0o700)
        (self.acl/'acl').write_text('user observer\ntopic read xenova/v1/#\n')
        accounts = {'profiles': {'observer': 'service'}, 'secrets': {'observer': 'fixture-observer'}, 'path': self.acl}
        supplied = patch.object(broker, 'inspect', return_value=accounts)
        supplied.start();self.addCleanup(supplied.stop)

    def configure(self):
        setup.prepare_token(self.etc, self.state)
        return setup.configure_factory(self.etc, self.etc, self.state, self.records)

    def files(self):
        return {p.name: p.read_bytes() for p in (self.bindings.path, self.runtime, self.mobile)}

    def select(self, expected=INTERACTION):
        return display.select_interaction(self.records, NEW_VM, expected, execute=True,
            bindings_path=self.bindings.path, runtime_path=self.runtime, mobile_path=self.mobile)

    def test_additional_debian_preserves_selected_identity_origins_and_services(self):
        self.configure();before = self.files();self.records['additional'] = self.new
        result = display.sync_after_registration(self.records, self.new,
            bindings_path=self.bindings.path, runtime_path=self.runtime, mobile_path=self.mobile)
        self.assertEqual(self.files(), before)
        with patch.object(display.subprocess, 'run', side_effect=AssertionError('unexpected service change')):
            self.assertEqual(display.restart_changed_services(result)['restarted'], [])
        self.assertEqual(result['ui']['state'], 'not-requested')

    def test_explicit_selection_preserves_old_vm_and_replay_is_idempotent(self):
        self.configure();self.records['additional'] = self.new
        result = self.select();self.assertEqual(result['bindings']['state'], 'updated')
        self.assertIn('vm-interaction', self.records)
        self.assertEqual(json.loads(self.runtime.read_text())['ui_origin'], 'http://192.168.77.9:8086')
        before = self.files();self.select();self.assertEqual(before, self.files())
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            display.select_interaction(self.records, INTERACTION, '55555555-5555-4555-8555-555555555555',
                bindings_path=self.bindings.path, execute=True)

    def test_ambiguous_target_and_invalid_later_config_do_not_publish_any_file(self):
        self.configure();self.records['additional'] = self.new;before = self.files()
        self.records['duplicate'] = copy.deepcopy(self.new)
        with self.assertRaises(ValueError):self.select()
        self.assertEqual(before, self.files());self.records.pop('duplicate')
        self.mobile.write_text('{}');before = self.files()
        with self.assertRaises(ValueError):self.select()
        self.assertEqual(before, self.files())

    def test_write_failure_rolls_back_exact_bytes_then_same_selection_recovers(self):
        self.configure();self.records['additional'] = self.new;before = self.files()
        actual = display.atomic_write;once = []
        def fail(path, *args, **kwargs):
            if Path(path) == self.mobile and not once:
                once.append(True);raise OSError(errno.EIO, 'fixture second publication failed')
            return actual(path, *args, **kwargs)
        with patch.object(display, 'atomic_write', fail), self.assertRaises(OSError):self.select()
        self.assertEqual(before, self.files());self.select()
        self.assertEqual(json.loads(self.bindings.path.read_text())['bindings'][2]['vm_id'], NEW_VM)

    def test_configuration_busy_does_not_wait_or_publish(self):
        self.configure();self.records['additional'] = self.new;before = self.files()
        with display.configuration_lock(self.bindings.path), self.assertRaises(BlockingIOError):self.select()
        self.assertEqual(before, self.files());self.select()

    def test_factory_tokens_are_private_distinct_and_preserved_on_repeat(self):
        self.configure();path = self.etc/setup.TOKEN_RELATIVE
        token = path.read_bytes();env = (self.etc/'display.env').read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.etc/'display.env').stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(token.strip().decode(), setup.environment(self.etc/'display.env')['XENOVA_DISPLAY_TOKEN'])
        self.configure();self.assertEqual(path.read_bytes(), token);self.assertEqual((self.etc/'display.env').read_bytes(), env)
        other = self.etc/'another-board';other.mkdir()
        setup.prepare_token(other, {})
        self.assertNotEqual((other/setup.TOKEN_RELATIVE).read_bytes(), token)

    def test_lost_or_changed_token_is_not_regenerated_and_unprotected_file_is_rejected(self):
        self.configure();path = self.etc/setup.TOKEN_RELATIVE;old = path.read_bytes();path.unlink()
        with self.assertRaises(OSError):setup.prepare_token(self.etc, self.state)
        self.assertFalse(path.exists());path.write_bytes(old);path.chmod(0o644)
        with self.assertRaises(ValueError):setup.prepare_token(self.etc, self.state)
        path.chmod(0o600);path.write_text('different-existing-token')
        with self.assertRaisesRegex(ValueError, 'changed or was lost'):setup.prepare_token(self.etc, self.state)
        self.assertEqual(path.read_text(), 'different-existing-token')

    def test_first_install_interruption_keeps_original_token_and_selection(self):
        setup.prepare_token(self.etc, self.state);token = (self.etc/setup.TOKEN_RELATIVE).read_bytes()
        with patch.object(broker, 'inspect', side_effect=OSError(errno.EIO, 'fixture broker unavailable')):
            with self.assertRaises(OSError):setup.configure_factory(self.etc, self.etc, self.state, self.records)
        self.assertEqual((self.etc/setup.TOKEN_RELATIVE).read_bytes(), token)
        self.configure();self.records['additional'] = self.new;self.select()
        self.configure()
        self.assertEqual(json.loads(self.bindings.path.read_text())['bindings'][2]['vm_id'], NEW_VM)

    def test_supply_and_connection_are_distinct_and_error_stage_recovers(self):
        self.configure();self.assertEqual(setup.check(self.records, self.etc)['connection'], 'not-observed')
        def failed(host, port, path, token, deadline):return 503
        result = setup.check(self.records, self.etc, live=True, request=failed)
        self.assertEqual((result['state'], result['failed_stage']), ('unconfirmed', 'feed'))
        def good(host, port, path, token, deadline):return 401 if port == 8085 and token is None else 200
        self.assertEqual(setup.check(self.records, self.etc, live=True, request=good)['state'], 'connected')

    def test_cli_plan_execute_restart_failure_and_same_request_recovery(self):
        self.configure();self.records['additional'] = self.new
        source = self.etc/'templates/fixture-display-template';source.mkdir(parents=True)
        (source/'template.json').write_text(json.dumps({'guest_display': {'fixture': 'installed receipt'}}))
        manager = SimpleNamespace(data=self.etc, p=SimpleNamespace(records=lambda **kw:self.records),
            call=lambda *args, **kw:{'ok':True,'data':{'registrations':[{'vm_id':r['uuid']} for r in self.records.values()]}})
        arguments = ['select', '--vm-id', NEW_VM, '--expected-vm-id', INTERACTION]
        before = self.files()
        import xenova_management
        with patch.object(setup, 'ETC', self.etc), patch.object(xenova_management, 'Manager', return_value=manager):
            with redirect_stdout(io.StringIO()), patch.object(display.subprocess, 'run', side_effect=AssertionError('plan activated service')):
                self.assertEqual(setup.main(arguments), 0)
            self.assertEqual(before, self.files())
            with redirect_stdout(io.StringIO()), patch.object(display.subprocess, 'run', side_effect=OSError(errno.EIO,'service boundary')):
                self.assertEqual(setup.main(arguments+['--execute']), 1)
            self.assertEqual(json.loads(self.bindings.path.read_text())['bindings'][2]['vm_id'], NEW_VM)
            calls = []
            def run(args, **kw):
                calls.append(args);return SimpleNamespace(returncode=3 if args[1]=='is-active' else 0)
            output=io.StringIO()
            with redirect_stdout(output), patch.object(display.subprocess, 'run', run):
                code=setup.main(arguments+['--execute','--restart'])
            self.assertEqual(code, 0, output.getvalue())
            self.assertIn(['systemctl','restart','xenova-display-hdmi.service'], calls)

    def test_inconsistent_origin_is_unconfirmed_without_network_or_mutation(self):
        self.configure();value = json.loads(self.runtime.read_text());value['ui_origin']='http://192.168.77.9:8086'
        self.runtime.write_text(json.dumps(value));before=self.files()
        with self.assertRaisesRegex(ValueError,'UI origin'), patch.object(setup.http.client,'HTTPConnection',side_effect=AssertionError('network before validation')):
            setup.check(self.records,self.etc,live=True)
        self.assertEqual(self.files(),before)

    def test_failed_recorded_check_replaces_old_success_with_unconfirmed(self):
        self.configure();runtime=self.etc/'run';runtime.mkdir(mode=0o700)
        manager=SimpleNamespace(p=SimpleNamespace(records=lambda **kw:self.records),
            call=lambda *args, **kw:{'ok':True,'data':{'registrations':[{'vm_id':r['uuid']} for r in self.records.values()]}})
        import xenova_management, xenova_runtime
        with patch.object(setup,'ETC',self.etc),patch.object(xenova_management,'Manager',return_value=manager),patch.object(xenova_runtime,'ensure_runtime',return_value=runtime):
            with redirect_stdout(io.StringIO()),patch.object(setup,'check',return_value={'state':'connected','connection':'observed'}):
                self.assertEqual(setup.main(['check','--live','--record']),0)
            (self.etc/setup.TOKEN_RELATIVE).unlink()
            with redirect_stdout(io.StringIO()):self.assertEqual(setup.main(['check','--live','--record']),1)
        value=json.loads((runtime/'display-install-check.json').read_text())
        self.assertEqual(value['state'],'unconfirmed');self.assertIn('checked_monotonic_ms',value)

    def test_image_gate_detects_missing_relay_enable_module_and_seeded_secret(self):
        from first_install_fixture import assemble
        root = platform_root(self.etc/'image')
        assemble.platform_root_ok(root)
        link = root/'etc/systemd/system/multi-user.target.wants/xenova-display-guest-relay.service'
        link.unlink()
        with self.assertRaisesRegex(ValueError, 'Display service not enabled'):assemble.platform_root_ok(root)
        link.symlink_to('/usr/lib/systemd/system/xenova-display-guest-relay.service')
        module = root/'usr/lib/python3.14/site-packages/xenova_display/guest_frames.py'
        module.unlink()
        with self.assertRaisesRegex(ValueError, 'source/installed contract mismatch'):assemble.platform_root_ok(root)
        source = ROOT/'platform/services/display/router/xenova_display/guest_frames.py';module.write_bytes(source.read_bytes())
        secret = root/'etc/xenova/guest-display/guest-data.token';secret.parent.mkdir();secret.write_text('forbidden fixture')
        with self.assertRaisesRegex(ValueError, 'board Display'):assemble.platform_root_ok(root)

    def test_actual_first_boot_supplies_token_before_guest_and_reuses_after_interruption(self):
        from test_xenova_default_deployment import DefaultDeploymentTest
        from xenova_vm import load_records
        fixture = DefaultDeploymentTest('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        board, factory = fixture.current_board()
        board.path = board.p.path
        etc = board.root/'etc/xenova';etc.mkdir()
        for item in factory['templates']:
            if item['id'] == factory['instances'][0]['record']['template']:
                item['metadata']['guest_display'] = {'fixture': 'external tiny root mount'}
                (board.data/'templates'/item['id']/'template.json').write_text(json.dumps(item['metadata']))
        record = factory['instances'][0]['record']
        from xenova_guest_profile import from_manifest
        record['provisioning_profile'] = from_manifest({'source': {'distribution': 'debian'},
            'guest_agent': {'verification': {'installed': True}}})
        (board.data/'seeds'/record['id']/'instance.json').write_text(json.dumps(record))
        (board.data/'.xenova-install.json').write_text(json.dumps(factory))
        consumed = []
        def guest_supply(platform, current, **kwargs):
            self.assertEqual(kwargs['display_token_path'], etc/setup.TOKEN_RELATIVE)
            self.assertTrue(kwargs['display_token_path'].is_file())
            # mount는 외부 경계이며 토큰/IP 공급은 실제 제품 함수를 사용한다.
            root = self.etc/'guest-root';(root/'etc/xenova').mkdir(parents=True, exist_ok=True)
            (root/'etc/passwd').write_text('xenova-display:x:123:123::/nonexistent:/usr/sbin/nologin\n')
            (root/'etc/xenova/guest-display-domu.json').write_text(json.dumps({'data_token_file':'/etc/xenova/guest-display/guest-data.token'}))
            provisioning.apply_guest_display(current, root, token_path=kwargs['display_token_path'])
            consumed.append(current['uuid'])
        # Inspectのdata別保存は外部broker境界。exportは実製品でACLを確認する。
        with patch.object(provisioning, 'apply_stopped_linux', side_effect=OSError(errno.EIO, 'fixture interrupted before guest supply')):
            with self.assertRaises(OSError):board.p.register()
        prepared_token = (etc/setup.TOKEN_RELATIVE).read_bytes()
        with patch.object(provisioning, 'apply_stopped_linux', guest_supply):board.p.register()
        self.assertEqual((etc/setup.TOKEN_RELATIVE).read_bytes(), prepared_token)
        self.assertEqual(len(consumed), 1)
        self.assertEqual((self.etc/'guest-root'/('etc/xenova/'+setup.TOKEN_RELATIVE)).read_bytes(), (etc/setup.TOKEN_RELATIVE).read_bytes())
        old = (etc/setup.TOKEN_RELATIVE).read_bytes()
        with patch.object(setup, 'prepare_token', side_effect=AssertionError('completed boot reinitialized')):board.p.register()
        self.assertEqual((etc/setup.TOKEN_RELATIVE).read_bytes(), old)
        self.assertEqual(set(load_records(board.data)), {'debian-dev', 'zephyr-drive-a', 'zephyr-drive-b'})


@unittest.skipUnless(os.geteuid() == 0, 'actual root-owned config')
class DisplayHTTPTest(unittest.TestCase):
    setUp = DisplaySetupTest.setUp
    configure = DisplaySetupTest.configure

    def test_actual_relay_and_guest_http_normal_failure_and_recovery(self):
        previous = {key: value for key, value in sys.modules.items()
                    if key == 'xenova_display' or key.startswith('xenova_display.')}
        def restore_modules():
            for key in list(sys.modules):
                if key == 'xenova_display' or key.startswith('xenova_display.'):
                    del sys.modules[key]
            sys.modules.update(previous)
        self.addCleanup(restore_modules)
        for key in previous:del sys.modules[key]
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import uvicorn
        from xenova_display.guest_frames import create_guest_data_app
        from xenova_display.mobile_upstream import LocalFeed
        from xenova_display.guest_app import DataSource, make_handler
        self.configure();feed_token = setup.environment(self.etc/'display.env')['XENOVA_DISPLAY_TOKEN']
        guest_token = (self.etc/setup.TOKEN_RELATIVE).read_text().strip();failure = [False]
        class Feed(BaseHTTPRequestHandler):
            def log_message(self, *_):pass
            def do_GET(self):
                authorized = self.headers.get('Authorization') == 'Bearer '+feed_token
                self.send_response(503 if failure[0] else 200 if authorized else 401)
                self.send_header('Content-Type', 'application/json');self.end_headers()
                self.wfile.write(b'{"fixture":"upstream presentation"}')
        feed = ThreadingHTTPServer(('127.0.0.1', 0), Feed)
        self.addCleanup(feed.server_close)
        thread = threading.Thread(target=feed.serve_forever);thread.start()
        self.addCleanup(lambda: (feed.shutdown(), thread.join(5)))
        sock = socket.socket();sock.bind(('127.0.0.1', 0));relay_port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_guest_data_app(LocalFeed(feed_token, port=feed.server_port), guest_token),
                                             log_level='error', lifespan='off'))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]));worker.start()
        def close_relay():server.should_exit=True;worker.join(5);sock.close()
        self.addCleanup(close_relay)
        end = time.monotonic()+5
        while not server.started and time.monotonic() < end:time.sleep(.01)
        self.assertTrue(server.started)
        web = self.etc/'web';web.mkdir();(web/'index.html').write_text('<html>fixture build</html>')
        guest = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(web, DataSource('127.0.0.1', relay_port, guest_token)))
        guest_worker = threading.Thread(target=guest.serve_forever);guest_worker.start()
        self.addCleanup(lambda: (guest.shutdown(), guest_worker.join(5), guest.server_close()))
        ports = {8082: feed.server_port, 8085: relay_port, 8086: guest.server_port}
        def request(host, port, path, token, deadline):return setup._http('127.0.0.1', ports[port], path, token, deadline)
        result = setup.check(self.records, self.etc, live=True, request=request)
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(request('',8085,setup.DATA_PATH,feed_token,time.monotonic()+2),401)
        failure[0] = True
        self.assertEqual(setup.check(self.records, self.etc, live=True, request=request)['failed_stage'], 'feed')
        failure[0] = False
        self.assertEqual(setup.check(self.records, self.etc, live=True, request=request)['state'], 'connected')


if __name__ == '__main__':unittest.main()
