"""실제 Linux Store/CAS/procfs/name 관측과 선언된 systemd leaf를 결합한다.

managed PUBLISHED anchor와 과거 owner는 fixture이며 실제 runtime 시작/종료 이력이 아니다.
카메라·모델·제품 service·IPC constructor는 실행하지 않는다.
"""
from dataclasses import asdict
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from xenova_vehicle import cli
from xenova_vehicle.runtime.process_observation import ProcessObserver
from xenova_vehicle.runtime.recovery_rearm import (RearmRequest, RearmUnavailable, _Audit, apply_rearm,
                                                    check_rearm, query_rearm)
from xenova_vehicle.runtime.resource_manifest import ResourceManifest
from xenova_vehicle.runtime.resource_store import ResourceStore, StoreResult
from xenova_vehicle.runtime.runtime_admission import _canonical, _execution_value, _next_episode
from xenova_vehicle.runtime.runtime_bootstrap import (EXECUTION_RECORD, MARKER, initialize_runtime_state,
                                                     write_execution_record, BootstrapUnavailable)
from xenova_vehicle.runtime.service_scope import ServiceScopeFacts, UNIT, SLICE, SLICE_PATH, LEAF_PATH, EXECUTION_PROFILE

from resource_guard_fixtures import full_fields


@unittest.skipUnless(sys.platform == 'linux', '실제 Linux ResourceStore/procfs/name 관측')
class RearmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='xenova-rearm-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.directory = self.root / 'state'
        initialize_runtime_state(self.directory)
        with ResourceStore(str(self.directory)) as store:
            actual = ProcessObserver(store, str(uuid.uuid4())).observe().guard
            fields = full_fields()
            fields['runtime'] = {k: str(v) if type(v) is int else v for k, v in asdict(actual.runtime).items()}
            # Linux PID 최대값 밖의 선언된 과거 birth. 실제 procfs ENOENT를 관측한다.
            for index, owner in enumerate(fields['owners']):
                owner.update(pid=2**31 + index, start_ticks=str(index + 1))
            fields['cgroup'] = dict(mode='SYSTEMD_V1', path=SLICE_PATH, device='1', inode='2')
            fields['recovery_episode'].update(service_attempts=5, role_attempts=dict(A=5, M=5, D=5))
            for slot, value in fields['resources'].items():
                value.update(state='NOT_ATTEMPTED', name=None, object_id=None, size_bytes=None)
            # 알려진 두 이름만 실제 backing stat으로 ABSENT를 확인한다.
            for slot in ('RAW_CURRENT', 'DB_COPY_0'):
                object_id = str(uuid.uuid4())
                fields['resources'][slot] = dict(state='KNOWN', name=('/xnv_raw_' if slot.startswith('RAW') else '/xnv_db_') + object_id,
                                                object_id=object_id, size_bytes='1')
            self.source = ResourceManifest(_canonical(fields))
            proposal = store.propose(store.load(), self.source, str(uuid.uuid4()))
            self.assertEqual(store.publish(proposal).kind, 'DURABLE')
            scope = ServiceScopeFacts(_canonical(dict(unit=UNIT, slice=SLICE, execution_profile=EXECUTION_PROFILE,
                invocation_id='1' * 32, main_birth=asdict(self.source.known_owners[0].birth),
                manager_birth=dict(pid=1, start_ticks=1), boot_id=self.source.runtime.boot_id,
                control_group=LEAF_PATH, cgroup=asdict(self.source.cgroup))))
            anchor = _execution_value(self.source, scope, dict(kind='DECLARED_TEST_FIXTURE'))
            anchor['status'] = 'PUBLISHED'
            write_execution_record(str(self.directory), anchor)
        self.anchor = (self.directory / EXECUTION_RECORD).read_bytes()
        self.bootstrap = (self.directory / MARKER).read_bytes()
        self.observation = ServiceScopeFacts(_canonical(dict(
            declared_systemd_leaf=True, unit=UNIT, state='inactive/dead', members=[])))
        self.mock_scope = patch('xenova_vehicle.runtime.recovery_rearm.observe_stopped', return_value=self.observation)
        self.scope = self.mock_scope.start()
        self.addCleanup(self.mock_scope.stop)
        self.request = RearmRequest(str(uuid.uuid4()), self.source.raw_sha256,
                                    self.source.recovery_episode.episode_id, '운영자가 원인 수습 뒤 명시 승인')

    def current(self):
        return ResourceManifest((self.directory / ResourceStore.DATA_NAME).read_bytes())

    def test_check_observes_actual_names_and_processes_without_reset(self):
        report = check_rearm(self.directory)
        self.assertEqual(report['status'], 'ELIGIBLE')
        self.assertEqual({n['outcome'] for n in report['observation']['names']}, {'ABSENT'})
        self.assertEqual({o['outcome'] for o in report['observation']['owners_after']}, {'EXITED'})
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)
        self.assertFalse(list(self.directory.glob('runtime-rearm-*')))

    def test_apply_preserves_inventory_anchor_bootstrap_and_only_changes_approved_fields(self):
        original_publish = ResourceStore.publish
        observed = []

        def publish(store, proposal):
            raw = (self.directory / _Audit.name(self.request.request_id, 'intent')).read_bytes()
            intent = json.loads(raw)
            self.assertEqual(intent['state'], 'AUTHORIZED')
            self.assertEqual(base64.b64decode(intent['source_manifest_b64']), self.source.raw_bytes)
            self.assertEqual(intent['previous_episode']['service_attempts'], 5)
            self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)
            observed.append(proposal)
            return original_publish(store, proposal)

        with patch.object(ResourceStore, 'publish', publish):
            report = apply_rearm(self.directory, self.request)
        self.assertEqual(report['status'], 'DURABLE_ACK')
        self.assertEqual(report['effect'], 'TARGET_VISIBLE')
        current = self.current()
        expected, actual = self.source.fields, current.fields
        for key in ('state', 'revision', 'recovery_episode'):
            expected.pop(key)
            actual.pop(key)
        self.assertEqual(expected, actual)
        self.assertEqual(current.revision, self.source.revision + 1)
        self.assertEqual(current.state, 'CLEAN')
        self.assertNotEqual(current.recovery_episode.episode_id, self.source.recovery_episode.episode_id)
        self.assertEqual(current.recovery_episode.service_attempts, 0)
        self.assertEqual(current.recovery_episode.role_attempts, (0, 0, 0))
        self.assertEqual(_next_episode(current.recovery_episode).service_attempts, 1)
        self.assertEqual((self.directory / EXECUTION_RECORD).read_bytes(), self.anchor)
        self.assertEqual((self.directory / MARKER).read_bytes(), self.bootstrap)
        self.assertEqual(len(observed), 1)
        self.assertFalse(report['motion_authorized'])

    def test_same_request_reapply_is_query_without_new_episode_or_publish(self):
        first = apply_rearm(self.directory, self.request)
        before = {p.name: p.read_bytes() for p in self.directory.glob('runtime-rearm-*')}
        with patch.object(ResourceStore, 'publish', side_effect=AssertionError('must not publish twice')):
            replay = apply_rearm(self.directory, self.request)
        self.assertTrue(replay['replay'])
        self.assertEqual(first['target_ref'], replay['target_ref'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.glob('runtime-rearm-*')})

    def test_same_key_changed_reason_is_rejected(self):
        apply_rearm(self.directory, self.request)
        other = RearmRequest(self.request.request_id, self.request.expected_manifest_sha256,
                             self.request.expected_episode_id, 'different approval')
        with self.assertRaisesRegex(RearmUnavailable, 'conflicts'):
            apply_rearm(self.directory, other)

    def test_stale_sha_and_episode_reject_without_audit_or_reset(self):
        for sha, episode in (('f' * 64, self.request.expected_episode_id),
                             (self.request.expected_manifest_sha256, str(uuid.uuid4()))):
            bad = RearmRequest(str(uuid.uuid4()), sha, episode, 'explicit but stale')
            with self.assertRaisesRegex(RearmUnavailable, 'CAS mismatch'):
                apply_rearm(self.directory, bad)
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)
        self.assertFalse(list(self.directory.glob('runtime-rearm-*')))

    def test_prepared_anchor_cannot_authorize_reset(self):
        fields = json.loads(self.anchor)
        fields['status'] = 'PREPARED'
        (self.directory / EXECUTION_RECORD).write_bytes(_canonical(fields))
        with self.assertRaisesRegex(RearmUnavailable, 'PUBLISHED'):
            apply_rearm(self.directory, self.request)
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)

    def test_live_owner_and_present_name_are_independent_rejections(self):
        from xenova_vehicle.runtime.procfs_formats import parse_proc_stat
        current = parse_proc_stat(Path('/proc/self/stat').read_bytes(), os.getpid())
        fields = self.source.fields
        fields['owners'][0].update(pid=current.pid, start_ticks=str(current.start_ticks))
        live = ResourceManifest(_canonical(fields))
        (self.directory / ResourceStore.DATA_NAME).write_bytes(live.raw_bytes)
        anchor = json.loads(self.anchor)
        anchor.update(supervisor=asdict(live.known_owners[0].birth), source_ref=asdict(live.ref))
        anchor['scope']['main_birth'] = anchor['supervisor']
        (self.directory / EXECUTION_RECORD).write_bytes(_canonical(anchor))
        with self.assertRaisesRegex(RearmUnavailable, 'EXITED'):
            check_rearm(self.directory)
        (self.directory / EXECUTION_RECORD).write_bytes(self.anchor)
        (self.directory / ResourceStore.DATA_NAME).write_bytes(self.source.raw_bytes)
        resource = self.source.known_resources[0]
        path = Path('/dev/shm') / resource.name[1:]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            with self.assertRaisesRegex(RearmUnavailable, 'ABSENT'):
                check_rearm(self.directory)
        finally:
            path.unlink()  # 이 시험이 O_EXCL로 만든 일반 fixture leaf만 정리한다.

    def test_stopped_scope_rejection_does_not_write_approval(self):
        self.scope.side_effect = RuntimeError('nonempty tree or pending job')
        with self.assertRaisesRegex(RuntimeError, 'pending job'):
            apply_rearm(self.directory, self.request)
        self.assertFalse(list(self.directory.glob('runtime-rearm-*')))

    def test_stopped_facts_change_after_durable_intent_never_creates_first_proposal(self):
        self.scope.side_effect = [self.observation, RuntimeError('new pending job during fsync')]
        with patch.object(ResourceStore, 'propose', side_effect=AssertionError('must observe again before first proposal')):
            result = apply_rearm(self.directory, self.request)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)
        self.assertEqual(query_rearm(self.directory, self.request.request_id)['effect'], 'SOURCE_UNCHANGED')

    def test_unknown_before_replace_keeps_original_five_and_queries_without_retry(self):
        with patch.object(ResourceStore, 'publish', return_value=StoreResult('CAS_MISMATCH')) as publish:
            report = apply_rearm(self.directory, self.request)
            replay = apply_rearm(self.directory, self.request)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertEqual(report['effect'], 'SOURCE_UNCHANGED')
        self.assertEqual(replay['effect'], 'SOURCE_UNCHANGED')
        self.assertEqual(self.current().recovery_episode.service_attempts, 5)

    def test_unknown_after_visible_replace_has_final_approval_but_no_ack_claim(self):
        original_publish = ResourceStore.publish

        def publish(store, proposal):
            self.assertEqual(original_publish(store, proposal).kind, 'DURABLE')
            return StoreResult('UNCONFIRMED', reason='declared uncertain return')

        with patch.object(ResourceStore, 'publish', publish):
            report = apply_rearm(self.directory, self.request)
        self.assertEqual((report['status'], report['effect']), ('UNKNOWN', 'TARGET_VISIBLE'))
        self.assertFalse(report['durable_ack_record_visible'])
        target = self.current().raw_bytes
        with patch.object(ResourceStore, 'publish', side_effect=AssertionError('query-only replay')):
            replay = apply_rearm(self.directory, self.request)
        self.assertEqual(replay['status'], 'UNKNOWN')
        self.assertEqual(self.current().raw_bytes, target)
        self.assertEqual(_next_episode(self.current().recovery_episode).service_attempts, 1)

    def test_late_ack_audit_failure_is_unknown_and_never_rolls_back(self):
        original_append = _Audit.append

        def append(audit, key, kind, value):
            if kind == 'ack':
                raise OSError('declared ack audit failure')
            return original_append(audit, key, kind, value)

        with patch.object(_Audit, 'append', append):
            result = apply_rearm(self.directory, self.request)
        self.assertEqual(result['status'], 'UNKNOWN')
        queried = query_rearm(self.directory, self.request.request_id)
        self.assertEqual(queried['effect'], 'TARGET_VISIBLE')
        self.assertFalse(queried['durable_ack_record_visible'])
        self.assertEqual(self.current().recovery_episode.service_attempts, 0)

    def test_intent_fsync_failure_never_enters_publish(self):
        with patch('xenova_vehicle.runtime.recovery_rearm.os.fsync', side_effect=OSError('declared fsync failure')), \
                patch.object(ResourceStore, 'publish', side_effect=AssertionError('authorization must be durable first')):
            report = apply_rearm(self.directory, self.request)
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)
        self.assertEqual(query_rearm(self.directory, self.request.request_id)['effect'], 'SOURCE_UNCHANGED')

    def test_malformed_audit_query_is_unknown_and_does_not_publish(self):
        path = self.directory / _Audit.name(self.request.request_id, 'intent')
        path.write_bytes(b'{')
        path.chmod(0o600)
        result = query_rearm(self.directory, self.request.request_id)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)

    def test_audit_symlink_is_not_followed(self):
        original = self.root / 'unrelated.json'
        original.write_text('untouched')
        (self.directory / _Audit.name(self.request.request_id, 'intent')).symlink_to(original)
        with self.assertRaises(OSError):
            apply_rearm(self.directory, self.request)
        self.assertEqual(original.read_text(), 'untouched')
        self.assertEqual(self.current().raw_bytes, self.source.raw_bytes)

    def test_existing_store_lock_fences_concurrent_rearm(self):
        with ResourceStore(str(self.directory)):
            with self.assertRaises(RuntimeError):
                apply_rearm(self.directory, self.request)

    def test_missing_lock_keeps_check_and_query_file_inventory_unchanged(self):
        (self.directory / ResourceStore.LOCK_NAME).unlink()
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for command in (lambda: check_rearm(self.directory),
                        lambda: query_rearm(self.directory, self.request.request_id)):
            with self.assertRaises(FileNotFoundError):
                command()
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})


class RearmCliTests(unittest.TestCase):
    def test_apply_forwards_exact_operator_approval_fields(self):
        import contextlib
        import io
        key, episode = str(uuid.uuid4()), str(uuid.uuid4())
        with patch('xenova_vehicle.runtime.recovery_rearm.apply_rearm', return_value={'status': 'UNKNOWN'}) as apply, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(['runtime-rearm', 'apply', '--state-dir', '/declared/state', '--request-id', key,
                           '--expected-manifest-sha256', 'a' * 64, '--expected-episode-id', episode, '--reason', '수습'])
        self.assertEqual(rc, 3)
        self.assertEqual(apply.call_args.args[1], RearmRequest(key, 'a' * 64, episode, '수습'))


if __name__ == '__main__':
    unittest.main()
