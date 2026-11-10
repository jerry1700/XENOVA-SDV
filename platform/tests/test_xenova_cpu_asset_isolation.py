"""실패 private 자산의 CPU 준비 격리. 원장·flock·FD는 실제, Xen/loop/mount는 기존 fixture다."""
import errno
import json
import threading
import unittest
from unittest.mock import patch

import test_xenova_creation_reconcile as reconciliation
import test_xenova_cpu as cpu_fixture
import xenova_creation as creation
import xenova_management as management


class CPUAssetIsolationTest(unittest.TestCase):
    def setUp(self):
        self.case = reconciliation.CreationReconcileTest('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p, self.m = self.case.p, self.case.m
        self.hardware = cpu_fixture.CPUTest('runTest')
        self.hardware.xen = self.case.f.xen
        self.hardware.hardware = [0, 1, 2, 3]
        self.hardware.scheduler = 'credit2'
        self.hardware.affinities = {0:[{'vcpu':i, 'pcpus':[0, 1, 2, 3]} for i in range(4)]}
        self.hardware.delegate = self.p.command
        self.hardware.command_hook = self.hardware.fail_pin = None
        self.p.command = self.hardware.command
        self.spec = {'expected_policy_revision':None, 'shared_pcpus':[0, 3], 'placements':[]}
        self.ok(self.m.call('cpu-policy-set', self.spec, execute=True, idempotency_key='cpu-initial'))

    def ok(self, response):
        self.assertTrue(response['ok'], response)
        return response['data']

    def prepare(self, **kwargs):
        return self.m.fork().call('cpu-host-prepare', kwargs, execute=True)

    def pins(self):
        return [c for c in self.case.f.xen.calls if c[:2] == ['xl', 'vcpu-pin']]

    def snapshot(self, op):
        root = self.p.data/op['owned_workspace']
        paths = [self.p.data/'operations'/(op['operation_id']+'.json'),
                 *[p for p in root.rglob('*') if p.is_file()]]
        return {str(path):(path.stat().st_ino, path.read_bytes()) for path in paths}

    def isolated_failure(self, distribution):
        op = self.case.fail_copy(distribution)
        before, policy = self.snapshot(op), self.p.cpu().policy_path.read_bytes()
        self.ok(self.prepare())
        self.assertEqual(len(self.pins()), 4)
        self.assertEqual(self.snapshot(op), before)
        self.assertEqual(self.p.cpu().policy_path.read_bytes(), policy)
        self.assertTrue(creation.holds(self.m, op))
        _, ips = self.m._ip_inventory(self.p.records(validate_assets=False))
        self.assertIn(op['assigned']['ipv4'], ips)
        reserved = []
        with patch.object(self.p, 'reserve_space', side_effect=lambda size, *a, **k:reserved.append(size)):
            self.m._reserve_space(0, self.p.records(validate_assets=False))
        self.assertGreater(reserved[0], 0)
        self.case.error(self.m.call('create', self.case.spec, execute=True), op['error']['code'])
        # 같은 정책을 host에 준비하는 예외를 새 CPU 정책 게시에 사용하지 않는다.
        self.case.error(self.m.call('cpu-policy-set', {**self.spec, 'expected_policy_revision':1},
                                   execute=True, idempotency_key='cpu-new'), 'OPERATION_IN_PROGRESS')
        self.assertEqual(self.snapshot(op), before)
        result = self.m.call('operation-reconcile', self.case.request(op), execute=True)
        self.ok(result)
        self.assertFalse(creation.holds(self.m, self.m._operations()[0]))
        self.ok(self.prepare())

    def test_debian_failed_private_copy_does_not_block_host_prepare_or_release_reservations(self):
        self.isolated_failure('debian')

    def test_ubuntu_failed_private_copy_does_not_block_host_prepare_or_release_reservations(self):
        self.isolated_failure('ubuntu')

    def test_open_fd_loop_and_incomplete_observation_block_then_recover(self):
        op = self.case.fail_copy()
        disk = self.p.data/op['owned_workspace']/op['assigned']['internal_name']/'root.raw'
        before = self.snapshot(op)
        with disk.open('rb'):
            self.case.error(self.prepare(), 'ASSET_IN_USE')
        command = self.p.timed_command
        def in_use(args, *a, **kw):
            if args[0] == 'losetup':
                return json.dumps({'loopdevices':[{'name':'/dev/fixture-loop', 'back-file':str(disk)}]})
            return command(args, *a, **kw)
        with patch.object(self.p, 'timed_command', in_use):
            self.case.error(self.prepare(), 'ASSET_IN_USE')
        def unreadable(args, *a, **kw):
            if args[0] == 'losetup':raise OSError(errno.EIO, 'fixture observation failure')
            return command(args, *a, **kw)
        with patch.object(self.p, 'timed_command', unreadable):
            self.assertFalse(self.prepare()['ok'])
        self.assertEqual(self.pins(), [])
        self.assertEqual(self.snapshot(op), before)
        self.ok(self.prepare())

    def test_active_or_uncertain_original_owner_is_not_isolated(self):
        op = self.case.fail_copy()
        for state in ('active', 'residual', 'reconciling'):
            with self.subTest(state=state), patch.object(management.Manager, '_owner_state', return_value=state):
                self.case.error(self.prepare(), 'OPERATION_IN_PROGRESS')
        self.assertEqual(self.pins(), [])
        self.assertTrue(creation.holds(self.m, op))
        self.ok(self.prepare())

    def test_pending_reconciliation_keeps_guard(self):
        op = self.case.fail_copy()
        request = self.case.request(op)
        def interrupted(phase, current):
            if phase == 'reconcile-before-delete':raise reconciliation.Interrupted()
        self.m.checkpoint = interrupted
        with self.assertRaises(reconciliation.Interrupted):
            self.m.call('operation-reconcile', request, execute=True)
        self.case.error(self.prepare(), 'OPERATION_IN_PROGRESS')
        self.assertEqual(self.pins(), [])
        self.m.checkpoint = lambda *args:None
        self.ok(self.m.call('operation-reconcile', request, execute=True))
        self.ok(self.prepare())

    def test_slow_usage_probe_releases_common_lock_for_other_vm_queries_and_stop(self):
        self.ok(self.prepare())
        self.case.f.xen.calls.clear()
        other = self.case.other_vm()
        op = self.case.fail_copy()
        entered, release = threading.Event(), threading.Event()
        usage = creation.usage
        def slow(*args):
            entered.set()
            if not release.wait(10):raise RuntimeError('fixture probe not released')
            return usage(*args)
        replies = []
        with patch.object(creation, 'usage', slow):
            thread = threading.Thread(target=lambda:replies.append(self.prepare()))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                for action, spec in (('list', {}), ('get', {'vm_id':other}),
                                     ('operation', {'operation_id':op['operation_id']})):
                    self.ok(self.m.fork().call(action, spec, budget_ms=2000))
                self.ok(self.m.fork().call('stop', {'vm_id':other},
                                          idempotency_key='other-stop-during-cpu-probe', budget_ms=2000))
                self.assertTrue(thread.is_alive())
                self.assertEqual(self.pins(), [])
            finally:
                release.set()
                thread.join(15)
        self.assertFalse(thread.is_alive())
        self.ok(replies[0])
        self.assertTrue(creation.holds(self.m, op))

    def test_changed_host_or_policy_or_private_context_rejects_before_pin(self):
        op = self.case.fail_copy()
        usage = creation.usage
        root = self.p.data/op['owned_workspace']
        for kind in ('host', 'gate', 'policy', 'workspace', 'journal', 'shutdown', 'deadline'):
            with self.subTest(kind=kind):
                restore = []
                def changed(*args):
                    usage(*args)
                    if kind == 'host':
                        platform = args[0].p
                        host_reader = platform.boot_id_reader
                        platform.boot_id_reader = lambda:'850e8400-e29b-41d4-a716-446655440000'
                        restore.append(lambda:setattr(platform, 'boot_id_reader', host_reader))
                    elif kind == 'gate':
                        mocked = patch.object(self.p.execution().__class__, 'barrier', return_value={'fixture':'changed'})
                        mocked.start();restore.append(mocked.stop)
                    elif kind == 'workspace':
                        extra = root/'unexpected'
                        extra.write_text('fixture');restore.append(extra.unlink)
                    elif kind == 'shutdown':
                        marker = self.p.runtime/'shutdown-requested'
                        marker.write_text('fixture');restore.append(marker.unlink)
                    elif kind == 'deadline':
                        platform = args[0].p
                        clock = platform.clock
                        platform.clock = lambda:clock()+16
                        restore.append(lambda:setattr(platform, 'clock', clock))
                    else:
                        path = (self.p.cpu().policy_path if kind == 'policy' else
                                self.p.data/'operations'/(op['operation_id']+'.json'))
                        raw = path.read_bytes();value = json.loads(raw)
                        value['policy_revision' if kind == 'policy' else 'status'] = 2 if kind == 'policy' else 'RUNNING'
                        path.write_text(json.dumps(value));restore.append(lambda:path.write_bytes(raw))
                try:
                    with patch.object(creation, 'usage', changed):
                        self.assertFalse(self.prepare()['ok'], kind)
                    self.assertEqual(self.pins(), [])
                finally:
                    for reset in reversed(restore):reset()
        self.ok(self.prepare())

    def test_repeated_prepare_rechecks_usage_and_does_not_cache_permission(self):
        op = self.case.fail_copy()
        self.ok(self.prepare())
        self.case.f.xen.calls.clear()
        disk = self.p.data/op['owned_workspace']/op['assigned']['internal_name']/'root.raw'
        with disk.open('rb'):
            self.case.error(self.prepare(), 'ASSET_IN_USE')
        self.assertEqual(self.pins(), [])
        self.ok(self.prepare())
        self.assertTrue(creation.holds(self.m, op))

    def test_new_host_boot_checks_old_private_failure_without_resuming_original_create(self):
        op = self.case.fail_copy()
        before = self.snapshot(op)
        self.p.boot_id_reader = lambda:'950e8400-e29b-41d4-a716-446655440000'
        self.ok(self.prepare())
        self.assertEqual(self.snapshot(op), before)
        self.assertTrue(creation.holds(self.m, op))
        with self.assertRaises(management.ManagementError) as blocked:
            with creation.work(self.m, op, {'entered':self.p.clock(), 'budget_ms':10000}):pass
        self.assertEqual(blocked.exception.error['code'], 'TARGET_CHANGED')

    def test_running_original_and_owner_reappearance_stay_blocked(self):
        op = self.case.fail_copy()
        path = self.p.data/'operations'/(op['operation_id']+'.json')
        before = path.read_bytes()
        running = dict(op, status='RUNNING')
        path.write_text(json.dumps(running))
        self.case.error(self.prepare(), 'OPERATION_IN_PROGRESS')
        path.write_bytes(before)
        with patch.object(management.Manager, '_owner_state', side_effect=['absent', 'residual']):
            self.case.error(self.prepare(), 'TARGET_CHANGED')
        self.assertEqual(self.pins(), [])
        self.ok(self.prepare())


if __name__ == '__main__':unittest.main()
