"""제품 create 후속·수습·원장과 실제 process/flock/FD. Xen·loop·mount만 기존 외부 fixture다."""
from contextlib import contextmanager, nullcontext
import copy
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_xenova_creation_reconcile as fixtures
import xenova_creation as creation
import xenova_creation_auto as automatic
import xenova_management as management
import xenova_preparation as ownership
import xenova_runtime as runtime
import xenova_vm as vm
import xenova_zephyr as zephyr


class AutomaticCreationTest(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.CreationReconcileTest('runTest');self.case.manual_only=False;self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.p,self.m=self.case.p,self.case.m
        self.p.automatic_cleanup_policy_path=self.p.data/'creation-reconcile-policy.json'
        self.p.automatic_cleanup_policy_path.write_text(json.dumps({'schema_version':'1.0','profile':'trial-1-plus-1'}))
        self.p.automatic_cleanup_policy_path.chmod(0o600)

    def ok(self,response):
        self.assertTrue(response['ok'],response)
        return response['data']

    def failure(self,distribution='debian'):
        with patch.object(automatic,'start',return_value=None) as launch:op=self.case.fail_copy(distribution)
        launch.assert_called_once()
        self.assertEqual(op['automatic_cleanup']['state'],'pending')
        self.assertEqual(op['automatic_cleanup']['observations'],0)
        return op

    def current(self,op):return self.m._read(self.p.data/'operations'/(op['operation_id']+'.json'))

    def descriptor(self,op):
        with ownership.request_output(),ownership.ReconciliationSlot(self.p,op) as slot:return os.dup(slot.fd)

    def run_once(self,op):
        automatic.run(self.m.fork(),op['operation_id'],self.descriptor(op))
        return self.current(op)

    def receipt(self,op,**changes):
        path=self.p.data/op['owned_workspace']/'.preparation-resources.json'
        value=json.loads(path.read_text());value.update(changes);path.write_text(json.dumps(value))

    def preserved(self,original,current):
        for field in ('operation_id','vm_id','idempotency_key','request','initial_spec','spec_hash','creation','assigned','template','error'):
            self.assertEqual(current.get(field),original.get(field),field)
        self.assertEqual(len(self.m._operations()),1)

    def test_debian_automatic_discard_release_reservations_keep_original_failure(self):
        op=self.failure('debian');original=copy.deepcopy(op)
        with patch.object(creation,'diagnose',wraps=creation.diagnose) as diagnosis:
            current=self.run_once(op)
        self.assertEqual(diagnosis.call_count,1)
        self.preserved(original,current)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        self.assertEqual(current['automatic_cleanup']['observations'],1)
        self.assertEqual(current['reconciliation']['request']['reconciliation_id'],op['operation_id'])
        self.assertFalse((self.p.data/op['owned_workspace']).exists())
        self.assertFalse(creation.holds(self.m,current))
        self.case.error(self.m.call('create',self.case.spec,execute=True),op['error']['code'])
        self.assertEqual(self.current(op),current)

    def test_ubuntu_automatic_discard(self):
        self.p.automatic_cleanup_policy_path.unlink()
        op=self.failure('ubuntu');current=self.run_once(op)
        self.preserved(op,current);self.assertFalse(creation.holds(self.m,current))
        self.assertEqual(current['automatic_cleanup']['state'],'complete')

    def test_completed_automatic_cleanup_allows_fresh_creation_without_reusing_original(self):
        op=self.failure();self.run_once(op)
        spec={**self.case.spec,'vm_id':'850e8400-e29b-41d4-a716-446655440000'}
        with self.case.linux_boundaries():fresh=self.ok(self.m.call('create',spec,execute=True))
        self.assertEqual(fresh['ipv4'],op['assigned']['ipv4'])
        self.assertNotEqual(fresh['vm_id'],op['vm_id'])
        self.assertEqual(self.current(op)['error'],op['error'])

    def test_optional_create_keeps_journal_and_bidirectional_original_meaning_after_cleanup(self):
        self.case.spec.pop('vcpus');self.case.spec.pop('memory_mib')
        op=self.failure();self.assertEqual(op['schema_version'],'1.2')
        current=self.run_once(op)
        self.assertEqual(current['schema_version'],'1.2');self.preserved(op,current)
        explicit={**self.case.spec,**{k:op['initial_spec'][k] for k in ('vcpus','memory_mib')}}
        self.case.error(self.m.fork().call('create',explicit,execute=True),op['error']['code'])
        self.assertEqual(self.current(op),current)

    def test_missing_policy_enrolls_new_create_by_default_without_rewriting_legacy_failure(self):
        self.p.automatic_cleanup_policy_path.unlink()
        op=self.failure()
        self.assertEqual(op['automatic_cleanup']['profile'],'bounded-1-plus-1')
        path=self.p.data/'operations'/(op['operation_id']+'.json')
        legacy=copy.deepcopy(op);legacy.pop('automatic_cleanup');path.write_text(json.dumps(legacy))
        with patch.object(automatic.subprocess,'Popen',side_effect=AssertionError('legacy cleanup spawned')):
            automatic.start(self.m,op['operation_id'])
        self.assertEqual(self.current(op),legacy)
        path.write_text(json.dumps(op))
        self.assertEqual(self.run_once(op)['automatic_cleanup']['state'],'complete')

    def firmware_failure(self,*,network=False,complete=False,optional=False,launch=False):
        from test_xenova_management import bundle,application_bundle,catalog
        from test_xenova_network_zephyr import network_bundle
        source=application_bundle(bundle(self.p.data/'legacy-firmware'),self.p.data/'rtos-templates'/'cleanup-fixture') if not network else network_bundle(
            bundle(self.p.data/'legacy-firmware'),self.p.data/'rtos-templates'/'cleanup-fixture')
        manifest=source/'firmware.json';meta=json.loads(manifest.read_text())
        meta.update(id='cleanup-fixture',kind='zephyr-xen-firmware',version='1.0.0')
        manifest.chmod(0o644);manifest.write_text(json.dumps(meta));manifest.chmod(0o444)
        descriptor={'schema_version':'1.0','template_id':'zephyr-xenvm-starter-arm64','template_version':'1.0.0',
            'guest_kind':'zephyr','asset_directory':'rtos-templates/cleanup-fixture','source_template_id':'cleanup-fixture',
            'manifest_sha256':catalog.sha(manifest)}
        directory=self.p.data/'catalog'/descriptor['template_id'];directory.mkdir()
        (directory/'1.0.0.json').write_text(json.dumps(descriptor))
        self.case.spec={'vm_id':'550e8400-e29b-41d4-a716-446655440001','name':'firmware-cleanup','template_id':descriptor['template_id'],
            'template_version':'1.0.0',**({} if optional else {'vcpus':1,'memory_mib':16})}
        self.m.preparer=self.m._prepare
        copyfile=zephyr.shutil.copyfile;once=[]
        def broken(source,target):
            if not once:
                once.append(True);Path(target).write_bytes(b'partial');raise OSError(errno.EIO,'fixture firmware copy failure')
            return copyfile(source,target)
        if complete:
            copyfile_patch=patch.object(zephyr.PreparedFirmware,'publish',side_effect=OSError(errno.EIO,'fixture before publish'))
        else:copyfile_patch=patch.object(zephyr.shutil,'copyfile',broken)
        with (nullcontext() if launch else patch.object(automatic,'start',return_value=None)),copyfile_patch:
            response=self.m.call('create',self.case.spec,execute=True)
        self.assertFalse(response['ok'],response)
        op=next(o for o in self.m._operations() if o['vm_id']==self.case.spec['vm_id'])
        self.assertEqual(op['automatic_cleanup']['state'],'pending',response)
        return op

    def test_zephyr_partial_firmware_default_cleanup_then_new_normal_create(self):
        self.p.automatic_cleanup_policy_path.unlink()
        op=self.firmware_failure();original=copy.deepcopy(op)
        current=self.run_once(op);self.preserved(original,current)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        self.assertFalse(creation.holds(self.m,current))
        self.assertFalse((self.p.data/op['owned_workspace']).exists())
        self.case.error(self.m.fork().call('create',self.case.spec,execute=True),op['error']['code'])
        fresh={**self.case.spec,'vm_id':'850e8400-e29b-41d4-a716-446655440000'}
        self.ok(self.m.fork().call('create',fresh,execute=True))
        self.assertTrue((self.p.data/'instances'/management.internal_name(fresh['vm_id'])).exists())

    def test_network_zephyr_optional_request_keeps_identity_and_original_meaning(self):
        op=self.firmware_failure(network=True,optional=True)
        current=self.run_once(op);self.preserved(op,current)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        explicit={**self.case.spec,'vcpus':1,'memory_mib':16}
        self.case.error(self.m.fork().call('create',explicit,execute=True),op['error']['code'])
        self.assertEqual(self.current(op),current)

    def test_zephyr_complete_private_firmware_can_be_explicitly_reconciled(self):
        op=self.firmware_failure(complete=True)
        self.ok(self.m.call('operation-reconcile',self.case.request(op),execute=True))
        current=self.current(op)
        self.assertFalse(creation.holds(self.m,current));self.preserved(op,current)

    def test_zephyr_active_asset_owner_is_not_discarded(self):
        op=self.firmware_failure()
        with ownership.request_output(),ownership.AssetLease(self.p,op['vm_id'],'create',op['idempotency_key']) as lease:
            lease.bind({**op,'expected_generation':0})
            current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['state'],'deferred')
        self.assertTrue((self.p.data/op['owned_workspace']).exists())
        self.assertEqual(self.run_once(op)['automatic_cleanup']['state'],'complete')

    def test_zephyr_observation_limit_survives_new_manager_and_boot(self):
        op=self.firmware_failure()
        with ownership.request_output(),ownership.AssetLease(self.p,op['vm_id'],'create',op['idempotency_key']) as lease:
            lease.bind({**op,'expected_generation':0});self.run_once(op)
        with patch.object(creation,'usage',side_effect=OSError(errno.EIO,'fixture unresolved use observation')):
            current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],2)
        self.p.boot_id_reader=lambda:'950e8400-e29b-41d4-a716-446655440000'
        self.assertFalse(automatic.eligible(self.m.fork(),current))
        self.assertEqual(self.run_once(op),current)
        self.assertTrue((self.p.data/op['owned_workspace']).exists())

    def test_zephyr_live_execution_is_not_discarded(self):
        from test_xenova_platform import domain
        op=self.firmware_failure()
        self.case.f.xen.rows=[domain({'uuid':op['vm_id'],'name':op['assigned']['internal_name']},self.p.data/op['owned_workspace'])]
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['last_error']['code'],'ASSET_IN_USE')
        self.assertTrue((self.p.data/op['owned_workspace']).exists())

    def test_zephyr_unknown_file_and_open_firmware_fd_preserve_assets(self):
        op=self.firmware_failure();root=self.p.data/op['owned_workspace']
        payload=next(path for path in root.rglob('*') if path.is_file())
        with payload.open('rb'):
            current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['last_error']['code'],'ASSET_IN_USE')
        foreign=root/'unknown-user-file';foreign.write_text('preserve')
        refused=self.m.call('operation-reconcile',{'operation_id':op['operation_id'],'vm_id':op['vm_id'],
            'expected_spec_sha256':op['spec_hash'],'purpose':'discard-private'})
        self.case.error(refused,'ASSET_IN_USE')
        self.assertEqual(foreign.read_text(),'preserve')

    def test_zephyr_deferred_replay_does_not_resume_or_overwrite_original_failure(self):
        op=self.firmware_failure();root=self.p.data/op['owned_workspace']
        payload=next(path for path in root.rglob('*') if path.is_file())
        with payload.open('rb'):current=self.run_once(op)
        with patch.object(zephyr,'copy_revision',side_effect=AssertionError('failed original recopied')):
            response=self.m.fork().call('create',self.case.spec,execute=True)
        self.case.error(response,op['error']['code'])
        self.assertEqual(self.current(op),current)
        self.ok(self.m.call('operation-reconcile',self.case.request(current),execute=True))

    def test_zephyr_publication_before_record_failure_keeps_existing_registration_resume(self):
        self.firmware_failure()  # 공급된 실제 bundle/catalog와 동일 요청을 재사용한다.
        self.case.spec={**self.case.spec,'vm_id':'850e8400-e29b-41d4-a716-446655440000'}
        save=self.m._save_op;seen=[]
        def interrupted(op,phase=None):
            if phase=='published' and not seen:
                seen.append(True);raise OSError(errno.EIO,'fixture published result recording failure')
            return save(op,phase)
        with patch.object(self.m,'_save_op',interrupted),patch.object(automatic,'start',return_value=None):
            response=self.m.call('create',self.case.spec,execute=True)
        self.assertFalse(response['ok'],response)
        op=next(value for value in self.m._operations() if value['vm_id']==self.case.spec['vm_id'])
        self.assertEqual(op['automatic_cleanup']['state'],'enrolled')
        self.ok(self.m.fork().call('create',self.case.spec,execute=True))
        self.assertEqual(self.current(op)['status'],'SUCCEEDED')

    def test_legacy_trial_record_remains_readable_and_finishes_with_original_budget(self):
        op=self.failure();value=op['automatic_cleanup'];value.update(version=1,profile='trial-1-plus-1');value.pop('cleanup_ms')
        path=self.p.data/'operations'/(op['operation_id']+'.json');path.write_text(json.dumps(op))
        self.assertEqual(self.run_once(op)['automatic_cleanup']['state'],'complete')

    def test_zephyr_cleanup_does_not_take_linux_preparation_slot(self):
        op=self.firmware_failure()
        with ownership.request_output(),ownership.LinuxSlot(self.p):
            self.assertEqual(self.run_once(op)['automatic_cleanup']['state'],'complete')

    def test_invalid_trial_policy_is_rejected_before_creation_acceptance(self):
        self.case.select()
        self.p.automatic_cleanup_policy_path.write_text(json.dumps({'schema_version':'1.0','profile':'trial-1-plus-1','retry_forever':True}))
        with self.case.linux_boundaries():result=self.m.call('create',self.case.spec,execute=True)
        self.case.error(result,'INVALID_SPEC')
        self.assertFalse(result['error']['details']['accepted'])
        self.assertEqual(self.m._operations(),[])

    def test_launch_error_keeps_first_create_error_and_has_bounded_diagnostic(self):
        op=self.failure()
        with patch.object(automatic.subprocess,'Popen',side_effect=OSError(errno.EAGAIN,'fixture spawn failure')):
            response=self.m.fork().call('create',self.case.spec,execute=True)
        self.case.error(response,op['error']['code'])
        warning=response['error']['details']['diagnostic_warnings'][-1]
        self.assertEqual(warning['code'],'AUTOMATIC_CLEANUP_LAUNCH_UNCONFIRMED')
        self.assertEqual(warning['errno'],errno.EAGAIN)
        self.assertEqual(self.current(op),op)

    def test_installation_gate_rejects_missing_or_stale_automatic_module(self):
        from first_install_fixture import platform_root
        import assemble
        root=self.p.data/'assembly-fixture';platform_root(root)
        assemble.platform_root_ok(root)
        module=root/'usr/lib/xenova/xenova_creation_auto.py'
        raw=module.read_bytes();module.unlink()
        with self.assertRaisesRegex(ValueError,'xenova_creation_auto.py'):assemble.platform_root_ok(root)
        module.write_text('stale')
        with self.assertRaisesRegex(ValueError,'xenova_creation_auto.py'):assemble.platform_root_ok(root)
        module.write_bytes(raw);assemble.platform_root_ok(root)

    def test_unchanged_deferred_requests_do_not_probe_rewrite_or_spawn_then_changed_receipt_finishes(self):
        op=self.failure();self.receipt(op,clean=False,pending=True)
        with patch.object(creation,'diagnose',wraps=creation.diagnose) as diagnosis:
            current=self.run_once(op)
            self.assertEqual(diagnosis.call_count,0)
            raw=(self.p.data/'operations'/(op['operation_id']+'.json')).read_bytes()
            with patch.object(automatic.subprocess,'Popen',side_effect=AssertionError('unchanged cleanup spawned')):
                for _ in range(5):self.case.error(self.m.fork().call('create',self.case.spec,execute=True),op['error']['code'])
            self.assertEqual(diagnosis.call_count,0)
            self.assertEqual((self.p.data/'operations'/(op['operation_id']+'.json')).read_bytes(),raw)
        self.assertEqual(current['automatic_cleanup']['observations'],1)
        self.receipt(op,clean=True,pending=False)
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],2)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        self.preserved(op,current)

    def test_two_observations_remain_exhausted_across_new_boot_until_explicit_reconcile(self):
        op=self.failure();self.receipt(op,clean=False,pending=True)
        self.run_once(op)
        self.receipt(op,loops=['/dev/fixture-loop'])
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],2)
        self.receipt(op,clean=True,pending=False,loops=[])
        self.p.boot_id_reader=lambda:'950e8400-e29b-41d4-a716-446655440000'
        self.assertFalse(automatic.eligible(self.m,current))
        self.assertEqual(self.run_once(op),current)
        self.assertTrue(creation.holds(self.m,current))
        self.ok(self.m.call('operation-reconcile',self.case.request(current),execute=True))
        self.assertFalse(creation.holds(self.m,self.current(op)))

    def test_open_fd_and_changed_private_inventory_are_not_automatically_discarded(self):
        op=self.failure();root=self.p.data/op['owned_workspace'];disk=root/op['assigned']['internal_name']/'root.raw'
        with disk.open('rb'):
            current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['last_error']['code'],'ASSET_IN_USE')
        self.assertTrue(disk.exists());self.assertTrue(creation.holds(self.m,current))
        # 닫혔다는 근거 없는 같은 원 호출은 전체 FD scan을 다시 실행하지 않는다.
        self.assertFalse(automatic.eligible(self.m,current))
        self.ok(self.m.call('operation-reconcile',self.case.request(current),execute=True))

    def test_publication_does_not_become_automatic_garbage(self):
        op=self.failure();root=self.p.data/op['owned_workspace']
        target=self.p.data/'instances'/op['assigned']['internal_name']
        target.mkdir();(target/'user-data').write_text('preserve')
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['last_error']['code'],'TARGET_CHANGED')
        self.assertEqual((target/'user-data').read_text(),'preserve')
        self.assertTrue(root.exists());self.assertTrue(creation.holds(self.m,current))

    def test_partial_cleanup_io_failure_resumes_same_plan_on_one_changed_condition(self):
        op=self.failure();seen=[]
        def fail_once(phase,current):
            if phase=='reconcile-after-delete' and not seen:
                seen.append(phase);raise OSError(errno.EIO,'fixture after owned unlink')
        self.m.checkpoint=fail_once
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],1)
        self.assertEqual(current['automatic_cleanup']['last_error']['errno'],errno.EIO)
        request=copy.deepcopy(current['reconciliation']['request'])
        self.assertTrue(automatic.eligible(self.m,current))
        self.m.checkpoint=lambda *a:None
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],2)
        self.assertEqual(current['reconciliation']['request'],request)
        self.assertEqual(current['automatic_cleanup']['first_error']['errno'],errno.EIO)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        self.preserved(op,current)

    def test_interrupted_cleanup_restarts_with_same_plan_and_consumed_observation(self):
        op=self.failure()
        def interrupt(phase,current):
            if phase=='reconcile-after-delete':raise fixtures.Interrupted()
        self.m.checkpoint=interrupt
        with self.assertRaises(fixtures.Interrupted):self.run_once(op)
        current=self.current(op)
        self.assertEqual(current['automatic_cleanup']['observations'],1)
        self.assertEqual(current['automatic_cleanup']['state'],'observing')
        self.assertTrue(creation.holds(self.m,current))
        request=copy.deepcopy(current['reconciliation']['request'])
        self.m.checkpoint=lambda *a:None
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],2)
        self.assertEqual(current['automatic_cleanup']['state'],'complete')
        self.assertEqual(current['reconciliation']['request'],request)

    def test_invalid_receipt_is_preserved_then_one_verified_change_can_recover(self):
        op=self.failure();path=self.p.data/op['owned_workspace']/'.preparation-resources.json'
        valid=path.read_bytes();path.write_text('[]')
        current=self.run_once(op)
        self.assertEqual(current['automatic_cleanup']['observations'],1)
        self.assertEqual(current['automatic_cleanup']['last_error']['cause_type'],'ValueError')
        self.assertTrue((self.p.data/op['owned_workspace']).exists())
        path.write_bytes(valid)
        self.assertEqual(self.run_once(op)['automatic_cleanup']['state'],'complete')

    @contextmanager
    def slow_cleanup(self,kind,*,firmware=False):
        other=self.case.other_vm();op=self.firmware_failure() if firmware else self.failure()
        entered,release=threading.Event(),threading.Event();results=[];errors=[]
        original=os.fsync;worker=None
        def fsync(fd):
            path=os.readlink('/proc/self/fd/'+str(fd))
            if kind=='visible-complete' and path==str(self.p.data/'operations'/(op['operation_id']+'.json')) and threading.current_thread() is not worker:
                raise AssertionError('another operation tried to fsync cleanup result')
            if (kind=='visible-complete' and threading.current_thread() is worker and path==str(self.p.data/'operations')
                    and not entered.is_set() and self.current(op).get('reconciliation',{}).get('phase')=='complete'):
                entered.set()
                if not release.wait(15):raise RuntimeError('fixture visible result sync not released')
            if (kind=='directory' and threading.current_thread() is worker and
                    path.startswith(str(self.p.data/op['owned_workspace'])) and Path(path).is_dir() and not entered.is_set()):
                entered.set()
                if not release.wait(15):raise RuntimeError('fixture slow directory sync not released')
            if (threading.current_thread() is worker and op['operation_id']+'.json.tmp-' in path
                    and not entered.is_set()):
                raw=Path(path).read_text()
                if kind=='record' or '"phase": "complete"' in raw:
                    entered.set()
                    if not release.wait(15):raise RuntimeError('fixture slow fsync not released')
            return original(fd)
        descriptor=self.descriptor(op)
        def run():
            try:automatic.run(self.m.fork(),op['operation_id'],descriptor)
            except BaseException as error:errors.append(error)
        with patch.object(runtime.os,'fsync',fsync):
            worker=threading.Thread(target=run,name='owned-automatic-cleanup');worker.start()
            try:
                self.assertTrue(entered.wait(5))
                yield op,other,worker,release
            finally:release.set();worker.join(20)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.current(op)['automatic_cleanup']['state'],'complete')

    def other_progress(self,other):
        for action,spec in (('list',{}),('get',{'vm_id':other}),('intents',{})):
            self.ok(self.m.fork().call(action,spec,budget_ms=2000))
        self.ok(self.m.fork().call('stop',{'vm_id':other},idempotency_key='cleanup-other-stop',budget_ms=2000))
        original=self.ok(self.m.fork().call('control-operation',{'idempotency_key':'cleanup-other-stop'}))
        self.assertEqual(original['operation']['phase'],'completed')
        self.ok(self.m.fork().call('start',{'vm_id':other},idempotency_key='cleanup-other-start',budget_ms=2000))
        self.case.f.xen.rows=[row for row in self.case.f.xen.rows if row['config']['c_info']['uuid']!=other]
        state=self.ok(self.m.fork().call('get',{'vm_id':other},budget_ms=2000))['vm']
        request={'vm_id':other,'recovery_attempt_id':'650e8400-e29b-41d4-a716-446655440001',
            'expected_host_boot_id':self.p.boot_id_reader(),'expected_lifecycle_generation':state['generation'],
            'expected_intent_revision':state['intent_revision'],
            'expected_execution':{'absent_observation_revision':state['observation_revision']},
            'mode':'start-if-absent','deadline_monotonic_ms':int((self.p.clock()+10)*1000)}
        recovered=self.ok(self.m.fork().call('recover_once',request,execute=True))
        self.assertEqual(recovered['attempt']['phase'],'completed')

    def test_slow_required_record_does_not_block_other_management_or_recovery(self):
        with self.slow_cleanup('record') as (op,other,worker,release):
            self.other_progress(other)
            self.assertTrue(worker.is_alive());self.assertFalse(release.is_set())

    def test_slow_final_record_keeps_reservations_without_blocking_other_recovery(self):
        with self.slow_cleanup('complete') as (op,other,worker,release):
            current=self.current(op)
            self.assertTrue(creation.holds(self.m,current))
            self.other_progress(other)
            self.assertTrue(worker.is_alive());self.assertFalse(release.is_set())

    def test_slow_private_directory_sync_does_not_block_other_stop_or_recovery(self):
        with self.slow_cleanup('directory') as (op,other,worker,release):
            self.other_progress(other)
            self.assertTrue(worker.is_alive());self.assertFalse(release.is_set())

    def test_visible_completion_does_not_release_or_make_other_recovery_fsync_it(self):
        with self.slow_cleanup('visible-complete') as (op,other,worker,release):
            self.assertTrue(creation.holds(self.m,self.current(op)))
            replay=self.ok(self.m.fork().call('create',self.case.spec,execute=True))
            self.assertEqual(replay['effective_status'],'RECONCILING')
            self.assertEqual(replay['operation']['automatic_cleanup']['observations'],1)
            self.other_progress(other)
            self.assertTrue(worker.is_alive());self.assertFalse(release.is_set())

    def test_zephyr_slow_cleanup_keeps_other_vm_management_and_recovery_independent(self):
        with self.slow_cleanup('directory',firmware=True) as (op,other,worker,release):
            self.other_progress(other)
            self.assertTrue(worker.is_alive());self.assertFalse(release.is_set())

    def test_real_failed_create_launches_separate_process_and_returns_before_probe(self):
        self.separate_process()

    def test_real_zephyr_failure_launches_separate_process_and_returns_before_probe(self):
        self.separate_process(firmware=True)

    def separate_process(self,*,firmware=False):
        self.p.automatic_cleanup_policy_path.unlink()
        ready_r,ready_w=os.pipe();release_r,release_w=os.pipe();children=[]
        popen=subprocess.Popen
        code='''
import json,os,select,sys
from pathlib import Path
from unittest.mock import patch
from test_xenova_platform import FakeXen
import xenova_creation as creation
import xenova_creation_auto as automatic
import xenova_management as management
import xenova_vm as vm
data,runtime,defaults,identity,fd,ready,release=sys.argv[1:]
xen=FakeXen(Path(data),{})
def command(args,**kw):
 if args[0]=='losetup':return json.dumps({'loopdevices':[]})
 return xen(args,**kw)
p=vm.Platform(Path(data),Path(runtime),command=command,defaults_path=Path(defaults))
usage=creation.usage;entered=[]
def paused(*args):
 if not entered:
  entered.append(True);os.write(int(ready),b'1')
  if not select.select([int(release)],[],[],10)[0]:raise RuntimeError('fixture release timeout')
  os.read(int(release),1)
 return usage(*args)
iterate=Path.iterdir
def processes(path):
 if path==Path('/proc'):
  yield path/str(os.getpid());yield path/str(os.getppid())
 else:yield from iterate(path)
with patch.object(vm,'storage_check'),patch.object(creation,'usage',paused),patch.object(Path,'iterdir',processes):
 automatic.run(management.Manager(p),identity,int(fd))
'''
        def launch(args,**kwargs):
            self.assertEqual(Path(args[2]).name,'xenova_creation_auto.py')
            kwargs['pass_fds']=(*kwargs['pass_fds'],ready_w,release_r)
            kwargs['stderr']=subprocess.PIPE
            child=popen([sys.executable,'-B','-c',code,*args[3:],str(ready_w),str(release_r)],**kwargs)
            children.append(child);return child
        try:
            with patch.object(automatic.subprocess,'Popen',launch):
                op=self.firmware_failure(launch=True) if firmware else self.case.fail_copy()
            self.assertEqual(len(children),1)
            self.assertTrue(select.select([ready_r],[],[],5)[0]);self.assertEqual(os.read(ready_r,1),b'1')
            self.assertIsNone(children[0].poll())
            self.assertEqual(self.current(op)['automatic_cleanup']['observations'],1)
            self.ok(self.m.fork().call('operation',{'operation_id':op['operation_id']},budget_ms=2000))
            if firmware:
                before=self.current(op)
                with patch.object(automatic,'start',return_value=None):
                    replay=self.m.fork().call('create',self.case.spec,execute=True)
                self.case.error(replay,op['error']['code'])
                self.assertEqual(self.current(op),before)
            os.write(release_w,b'1')
            self.assertEqual(children[0].wait(10),0,children[0].stderr.read())
            current=self.current(op)
            self.assertEqual(current['automatic_cleanup']['state'],'complete')
            self.preserved(op,current)
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait(5)
            for fd in (ready_r,ready_w,release_r,release_w):os.close(fd)


if __name__=='__main__':unittest.main()
