"""정지 observer의 정책·anchor·유한 tree 검사를 합성 OS leaf로 확인한다.

systemd/proc/cgroup leaf는 명시 fixture이며 실제 서비스 정지나 실제 host 수락이 아니다.
기존 ResourceManifest/_record_manifest 및 scanner의 정책 본문은 그대로 실행한다.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from resource_guard_fixtures import encode, guard, minimal_fields
from xenova_vehicle.runtime.resource_manifest import ResourceManifest
from xenova_vehicle.runtime import stopped_service_scope as scope


def fixture():
    fields = minimal_fields()
    fields['cgroup']['path'] = scope.SLICE_PATH
    manifest = ResourceManifest(encode(fields))
    current = guard(manifest)
    file = dict(path='/etc/systemd/system/' + scope.UNIT, device=1, inode=5, sha256='a' * 64)
    original = dict(unit=scope.UNIT, slice=scope.SLICE, execution_profile=scope.EXECUTION_PROFILE,
        invocation_id='a' * 32, boot_id=current.runtime.boot_id,
        main_birth=asdict(next(owner.birth for owner in manifest.owners if owner.role == 'S')),
        manager_birth=dict(pid=1, start_ticks=9), uid=1000, control_group=scope.LEAF_PATH,
        cgroup=asdict(manifest.cgroup), fragment=file, dropins=[], slice_fragment=file, slice_dropins=[])
    record = dict(version=1, status='PUBLISHED', source_ref=asdict(manifest.ref),
        service_instance=manifest.service_instance, resource_epoch=manifest.resource_epoch,
        supervisor=original['main_birth'], runtime=asdict(manifest.runtime),
        recovery_episode=asdict(manifest.recovery_episode), scope=original, admission={})
    unit = dict(Id=scope.UNIT, LoadState='loaded', ActiveState='inactive', SubState='dead', Type='simple',
        MainPID='0', ControlPID='0', InvocationID='', ControlGroup='', Slice=scope.SLICE,
        KillMode='control-group', KillSignal='2', Delegate='no', UID='1000',
        FragmentPath=file['path'], DropInPaths='', Restart='on-failure', Job='')
    slice_values = dict(Id=scope.SLICE, LoadState='loaded', ActiveState='active',
        ControlGroup=scope.SLICE_PATH, StopWhenUnneeded='no', FragmentPath=file['path'], DropInPaths='')
    return current, manifest, record, unit, slice_values


class StoppedServiceScopeTests(unittest.TestCase):
    def manager(self, current, original, unit, slice_values, *, birth=None, file=None):
        def show(name, properties):
            values = unit if name == scope.UNIT else slice_values
            self.assertEqual(set(values), set(properties))
            return values
        with patch.object(scope, '_current_guard', return_value=original['manager_birth'] if birth is None else birth), \
                patch.object(scope, '_show', side_effect=show), \
                patch.object(scope, '_root_file', return_value=original['fragment'] if file is None else file):
            return scope._stopped_manager(current, original)

    def test_published_anchor_accepts_later_counter_revision_without_mirroring_it(self):
        current, manifest, record, _, _ = fixture()
        fields = manifest.fields
        fields['revision'] = '2'
        fields['recovery_episode']['service_attempts'] = 4
        changed = ResourceManifest(encode(fields))
        self.assertIs(scope._anchor(current, changed, record), record['scope'])
        self.assertEqual(record['recovery_episode']['service_attempts'], 0)

    def test_prepared_unmanaged_and_foreign_runtime_anchors_are_rejected(self):
        current, manifest, record, _, _ = fixture()
        variants = [dict(record, status='PREPARED'), dict(record, version=True), dict(record, future=1),
                    dict(record, scope=None),
                    dict(record, supervisor=dict(pid=100, start_ticks=999))]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(scope.ServiceScopeUnavailable):
                scope._anchor(current, manifest, value)
        other = replace(current, runtime=replace(current.runtime, boot_id='00000000-0000-4000-8000-000000000999'))
        with self.assertRaises(scope.ServiceScopeUnavailable):
            scope._anchor(other, manifest, record)

    def test_same_revision_different_anchor_bytes_and_future_revision_are_rejected(self):
        current, manifest, record, _, _ = fixture()
        for source in (dict(record['source_ref'], raw_sha256='f' * 64),
                       dict(record['source_ref'], revision=manifest.revision + 1)):
            with self.subTest(source=source), self.assertRaises(scope.ServiceScopeUnavailable):
                scope._anchor(current, manifest, dict(record, source_ref=source))

    def test_stopped_invocation_and_removed_leaf_forms_keep_original_provenance(self):
        current, _, record, unit, slices = fixture()
        for invocation in ('', '0' * 32, record['scope']['invocation_id'], 'b' * 32):
            for group in ('', scope.LEAF_PATH):
                with self.subTest(invocation=invocation, group=group):
                    result = self.manager(current, record['scope'], dict(unit, InvocationID=invocation,
                                          ControlGroup=group), slices)
                    self.assertEqual(result['properties']['MainPID'], '0')
        self.assertEqual(record['scope']['invocation_id'], 'a' * 32)

    def test_actual_failed_pair_empty_job_is_stopped_without_pinning_restart_policy(self):
        current, _, record, unit, slices = fixture()
        # stopped-properties-reapproval01 원문(7ed0743d...)의 관측값이다.
        actual = dict(line.split('=', 1) for line in (
            'Restart=no\nMainPID=0\nControlPID=0\nActiveState=failed\nSubState=failed\nJob=\n').splitlines())
        for restart in ('no', 'on-failure'):
            with self.subTest(restart=restart):
                observed = dict(unit, **actual)
                observed['Restart'] = restart
                result = self.manager(current, record['scope'], observed, slices)
                self.assertEqual(result['properties']['Job'], '')
                self.assertEqual(result['properties']['Restart'], restart)

    def test_live_control_pid_invalid_invocation_and_weakened_unit_are_rejected(self):
        current, _, record, unit, slices = fixture()
        for key, value in (('ActiveState', 'active'), ('SubState', 'failed'), ('MainPID', '10'),
                           ('ControlPID', '11'), ('InvocationID', 'not-a-canonical-invocation'),
                           ('ControlGroup', '/system.slice/other.service'), ('UID', '0'),
                           ('KillMode', 'process'), ('Delegate', 'yes'), ('Job', 'DECLARED_PENDING_JOB')):
            with self.subTest(key=key), self.assertRaises(scope.ServiceScopeUnavailable):
                self.manager(current, record['scope'], dict(unit, **{key: value}), slices)

    def test_unconfirmed_actual_job_format_does_not_authorize_stopped_scope(self):
        current, _, record, unit, slices = fixture()
        with patch.object(scope, '_current_guard', return_value=record['scope']['manager_birth']), \
                patch.object(scope, '_show', return_value=unit), \
                patch.object(scope, '_ABSENT_JOB_VALUE', None), \
                self.assertRaisesRegex(scope.ServiceScopeUnavailable, 'Job format is unconfirmed'):
            scope._stopped_manager(current, record['scope'])

    def test_manager_replacement_and_nonpersistent_slice_are_rejected(self):
        current, _, record, unit, slices = fixture()
        with self.assertRaises(scope.ServiceScopeUnavailable):
            self.manager(current, record['scope'], unit, slices, birth=dict(pid=1, start_ticks=10))
        for key, value in (('ActiveState', 'inactive'), ('StopWhenUnneeded', 'yes'),
                           ('ControlGroup', '/another.slice')):
            with self.subTest(key=key), self.assertRaises(scope.ServiceScopeUnavailable):
                self.manager(current, record['scope'], unit, dict(slices, **{key: value}))

    def test_current_root_owned_configuration_can_change_without_rewriting_old_anchor(self):
        current, _, record, unit, slices = fixture()
        before = deepcopy(record)
        updated = dict(record['scope']['fragment'], inode=8, sha256='b' * 64)
        result = self.manager(current, record['scope'], unit, slices, file=updated)
        self.assertEqual(result['fragment'], updated)
        self.assertEqual(record, before)

    def test_observe_binds_both_empty_endpoints_and_keeps_configuration_audit(self):
        current, manifest, record, _, _ = fixture()
        manager = dict(manager_birth=record['scope']['manager_birth'], fragment={'sha256': 'b' * 64})
        tree = ((1, 1), ((scope.SLICE_PATH, 1, 2, 1, 3, 1, 4),))
        with patch.object(scope, '_stopped_manager', return_value=manager) as observe_manager, \
                patch.object(scope, '_empty_tree', return_value=tree) as observe_tree:
            facts = scope.observe_stopped(current, manifest, record)
        self.assertEqual(observe_manager.call_count, 2)
        self.assertEqual(observe_tree.call_count, 2)
        self.assertEqual(facts.cgroup, manifest.cgroup)
        self.assertEqual(facts.fields['original_configuration']['fragment'], record['scope']['fragment'])
        self.assertEqual(facts.fields['members'], [])
        self.assertEqual(facts.fields['source_ref'], asdict(manifest.ref))

    def test_changed_manager_or_tree_between_endpoints_is_not_empty_proof(self):
        current, manifest, record, _, _ = fixture()
        for managers, trees in (([{'n': 1}, {'n': 2}], [(), ()]),
                                ([{}, {}], [('first',), ('replacement',)])):
            with self.subTest(managers=managers), patch.object(scope, '_stopped_manager', side_effect=managers), \
                    patch.object(scope, '_empty_tree', side_effect=trees), self.assertRaises(scope.ServiceScopeUnavailable):
                scope.observe_stopped(current, manifest, record)

    def tree(self, manifest, *, members=b'', child=None, replaced=False, mount_changed=False):
        top = SimpleNamespace(st_dev=manifest.cgroup.device, st_ino=manifest.cgroup.inode)
        basefile = SimpleNamespace(st_dev=1, st_ino=3)
        def protected(path, directory=False):
            return top if directory else basefile
        class Entries:
            def __enter__(self):
                return iter([] if child is None else [SimpleNamespace(name=child, is_dir=lambda **_: True)])
            def __exit__(self, *unused):
                return False
        def inode(path, **unused):
            return SimpleNamespace(st_dev=9, st_ino=9) if replaced else protected(path, not path.endswith(('/tasks', '/cgroup.procs')))
        mounts = [((1, 2), 3), ((1, 2), 4 if mount_changed else 3)]
        with patch.object(scope.os, 'open', return_value=9), patch.object(scope.os, 'close'), \
                patch.object(scope, '_mount_root', side_effect=mounts), \
                patch.object(scope, '_protected', side_effect=protected), \
                patch.object(scope, '_read', return_value=members), \
                patch.object(scope.os, 'scandir', side_effect=lambda _: Entries()), \
                patch.object(scope.os, 'stat', side_effect=inode):
            return scope._empty_tree(manifest.cgroup)

    def test_actual_bounded_scanner_accepts_empty_slice_and_rejects_members(self):
        _, manifest, _, _, _ = fixture()
        self.assertEqual(len(self.tree(manifest)[1]), 1)
        for members in (b'123\n', b'bad\n', b' '.join(str(n).encode() for n in range(1, 18))):
            with self.subTest(members=members), self.assertRaises(scope.ServiceScopeUnavailable):
                self.tree(manifest, members=members)

    def test_scanner_rejects_foreign_unit_inode_race_mount_replacement_and_unbounded_tree(self):
        _, manifest, _, _, _ = fixture()
        for kwargs in (dict(child='other.service'), dict(replaced=True), dict(mount_changed=True),
                       dict(child=scope.UNIT)):
            with self.subTest(kwargs=kwargs), self.assertRaises(scope.ServiceScopeUnavailable):
                self.tree(manifest, **kwargs)

    def test_effective_root_is_not_an_administrative_guard_shortcut(self):
        current, _, _, _, _ = fixture()
        with patch.object(scope.os, 'getuid', return_value=1000), \
                patch.object(scope.os, 'geteuid', return_value=0), \
                patch.object(scope, '_show', side_effect=AssertionError('root must fail before manager lookup')), \
                self.assertRaises(scope.ServiceScopeUnavailable):
            scope._current_guard(current)


if __name__ == '__main__':
    unittest.main()
