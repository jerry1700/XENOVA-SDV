"""제품 계정 writer·private 전달·소비 parser. mount/Store/서비스는 격리 외부 경계다."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import socket
import time
from types import SimpleNamespace
from uuid import uuid4
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from first_install_fixture import ROOT

# Exercise the checked-out broker and Agent rather than host-installed copies.
sys.path[:0] = [str(ROOT / 'platform/deploy/mosquitto/scripts'),
                str(ROOT / 'platform/services/vm-agent/agent')]
import xenova_broker as broker
import xenova_provisioning as provision
from xenova_execution import ExecutionError
from xenova_guest_profile import from_manifest, validate
from xenova_agent.config import AgentSettings
import guest_identity

A='850e8400-e29b-41d4-a716-446655440001'
B='850e8400-e29b-41d4-a716-446655440002'
ROOT=Path(__file__).resolve().parents[2]
REAL_RUN=subprocess.run


class DisplayProvisioningTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='xenova-display-provisioning-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'root';self.root.mkdir()
        (self.root/'etc/xenova').mkdir(parents=True)
        (self.root/'etc/passwd').write_text(
            'root:x:0:0:root:/root:/bin/bash\n'
            'xenova-display:x:998:998::/nonexistent:/usr/sbin/nologin\n')
        self.config=self.root/'etc/xenova/guest-display-domu.json'
        self.config.write_text(json.dumps({
            'web_root':'/opt/xenova/display-web','bind':'192.168.77.20','app_port':8086,
            'allowed_clients':['192.168.77.1'],'data_host':'192.168.77.1','data_port':8085,
            'data_token_file':'/etc/xenova/guest-display/guest-data.token','page':'/display/combined'}))
        self.token=self.base/'relay.token';self.token.write_text('shared-secret\n');self.token.chmod(0o600)

    def apply(self, address='192.168.77.6'):
        return provision.apply_guest_display(
            {'ipv4':address},self.root,token_path=self.token,token_owner_uid=os.geteuid())

    def test_private_root_gets_actual_ip_and_relay_token(self):
        with patch.object(provision.os,'chown') as chown:
            result=self.apply()
        self.assertEqual(result['bind'],'192.168.77.6')
        self.assertEqual(json.loads(self.config.read_text())['bind'],'192.168.77.6')
        supplied=self.root/'etc/xenova/guest-display/guest-data.token'
        self.assertEqual(supplied.read_text(),'shared-secret\n')
        self.assertEqual(supplied.stat().st_mode&0o777,0o600)
        self.assertEqual((self.root/'etc/xenova/guest-display').stat().st_mode&0o777,0o700)
        self.assertEqual(chown.call_count,2)

    def test_full_private_subnet_address_is_accepted(self):
        with patch.object(provision.os,'chown'):
            self.assertEqual(self.apply('192.168.77.200')['bind'],'192.168.77.200')

    def test_missing_or_public_token_refuses_broken_guest_without_config_mutation(self):
        before=self.config.read_bytes();self.token.unlink()
        with self.assertRaises(FileNotFoundError):self.apply()
        self.assertEqual(self.config.read_bytes(),before)
        self.token.write_text('shared-secret\n');self.token.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'root-owned mode 0600'):self.apply()
        self.assertEqual(self.config.read_bytes(),before)

    def test_gateway_and_outside_addresses_are_rejected(self):
        for address in ('192.168.77.1','192.168.77.255','192.168.78.6','not-an-ip'):
            with self.subTest(address=address),self.assertRaisesRegex(ValueError,'assigned private IPv4'):
                self.apply(address)

    def test_non_display_linux_guest_is_unchanged(self):
        self.config.unlink()
        self.assertEqual(provision.apply_guest_display({'ipv4':'192.168.77.6'},self.root,
                         token_path=self.base/'absent'),{'state':'not-requested'})


class ProvisioningTest(unittest.TestCase):
    def setUp(self):
        if os.geteuid()!=0:self.skipTest('root-owned local fixtures required')
        self.temp=tempfile.TemporaryDirectory(prefix='xenova-provisioning-');self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name);self.rules=broker.policy();self.rules['services']={}
        self.profile=from_manifest({'source':{'distribution':'debian'},'guest_agent':{'verification':{'installed':True}}})
        self.record={'uuid':A,'provisioning_profile':self.profile}
        for context in (patch.object(broker,'storage',lambda data=broker.DATA:Path(data)/'mqtt'),
                        patch.object(broker,'policy',return_value=self.rules),
                        patch.object(broker.grp,'getgrnam',return_value=type('Group',(),{'gr_gid':0})()),
                        patch.object(broker,'reload_broker',return_value='next_start')):
            context.start();self.addCleanup(context.stop)

    def supplied(self,record=None):return provision.ensure(self.data,record or self.record,gid=0,rules=self.rules)

    def value(self):return broker.load(self.data/'mqtt',0,self.rules)

    def test_real_hash_publication_reuse_and_clone_keep_separate_credentials(self):
        with patch.object(broker.subprocess,'run',wraps=REAL_RUN) as run:
            first=self.supplied();original=self.value();secret=original['secrets'][A]
            self.assertEqual(run.call_count,1)
            second=self.supplied();self.assertEqual(run.call_count,1)
            self.assertEqual(first,second);self.assertEqual(original['path'],self.value()['path'])
            self.assertTrue(self.value()['secrets'][A]==secret)
            self.supplied({'uuid':B,'provisioning_profile':self.profile})
            self.assertTrue(self.value()['secrets'][A]!=self.value()['secrets'][B])
            self.assertEqual(run.call_count,2)
        self.assertNotIn(secret,json.dumps(first))
        self.assertEqual(provision.credential_file(self.data,A,'agent').stat().st_mode&0o777,0o600)

    def test_hash_start_interruption_resumes_original_plaintext_without_new_password(self):
        with patch.object(broker.subprocess,'run',side_effect=OSError(errno.EIO,'fixture hash launch')):
            with self.assertRaises(OSError):self.supplied()
        stage=next((self.data/'mqtt').glob('.pending-*'))
        before=(stage/'credentials.json').read_bytes()
        original=json.loads(before)['accounts'][A]
        self.supplied()
        self.assertTrue(self.value()['secrets'][A]==original)
        self.assertTrue((self.value()['path']/'credentials.json').read_bytes()==before)

    def test_interruption_after_hash_does_not_hash_the_hash_again(self):
        write=broker.write
        def fail(path,*args,**kwargs):
            if path.name=='passwd':raise OSError(errno.ENOSPC,'fixture publication')
            return write(path,*args,**kwargs)
        with patch.object(broker,'write',fail),self.assertRaises(OSError):self.supplied()
        stage=next((self.data/'mqtt').glob('.pending-*'));before=(stage/'credentials.json').read_bytes()
        with patch.object(broker.subprocess,'run',side_effect=AssertionError('redundant hashing')):self.supplied()
        self.assertTrue((self.value()['path']/'credentials.json').read_bytes()==before)

    def test_revoked_account_is_not_automatically_recreated_and_operator_can_restore(self):
        self.supplied()
        broker.change('remove-account',A,execute=True,data=self.data,gid=0,rules=self.rules)
        before=self.value()['revision']
        with self.assertRaisesRegex(ExecutionError,'revoked'):self.supplied()
        self.assertEqual(before,self.value()['revision'])
        broker.change('add-account',A,execute=True,data=self.data,gid=0,rules=self.rules)
        self.assertEqual(self.supplied()['credential_state'],'stored')

    def root(self):
        root=self.data/'guest';(root/'etc/xenova').mkdir(parents=True)
        unit=root/'usr/lib/systemd/system/xenova-agent.service';unit.parent.mkdir(parents=True)
        shutil.copyfile(ROOT/'platform/services/vm-agent/agent/deploy/xenova-agent.service',unit)
        return root

    def test_actual_agent_parser_consumes_generated_private_environment(self):
        root=self.root();(root/'etc/xenova/instance-id').write_text(A+'\n')
        result=provision.apply_linux(self.data,self.record,root,gid=0,rules=self.rules)
        env=dict(line.split('=',1) for line in (root/'etc/xenova/agent.env').read_text().splitlines())
        settings=AgentSettings.from_env(env,instance_id_path=root/'etc/xenova/instance-id')
        self.assertEqual(settings.vm_id,A);self.assertEqual(settings.username,A)
        self.assertNotIn('XENOVA_AGENT_DEMO_COMMANDS',env)
        unit=root/'usr/lib/systemd/system/xenova-agent.service'
        self.assertIn('Environment=XENOVA_AGENT_DEMO_COMMANDS=1',unit.read_text().splitlines())
        self.assertTrue(settings.password==self.value()['secrets'][A]);self.assertTrue(settings.telemetry_enabled)
        self.assertEqual(result['guest_application'],'private-root-configured')
        self.assertEqual(result['authentication'],'unobserved')

    def test_factory_host_uuid_is_used_by_real_guest_identity_not_machine_id_uuid(self):
        root=self.root();(root/'etc/machine-id').write_text('123456781234123412341234567890ab\n')
        (root/'etc/xenova/deployment-seed.json').write_text('{"schema":1}\n')
        for kind in ('rsa','ecdsa','ed25519'):
            p=root/f'etc/ssh/ssh_host_{kind}_key';p.parent.mkdir(exist_ok=True);p.write_text('external key fixture')
        provision.apply_linux(self.data,self.record,root,factory=True,gid=0,rules=self.rules)
        with patch.object(guest_identity.os,'sync'):guest_identity.ensure(root,command=lambda *a,**k:None)
        self.assertEqual((root/'etc/xenova/instance-id').read_text().strip(),A)
        original=(root/'etc/xenova/agent.env').read_bytes()
        with patch.object(guest_identity.os,'sync'):guest_identity.ensure(root,command=lambda *a,**k:None)
        self.assertEqual((root/'etc/xenova/agent.env').read_bytes(),original)

    def test_broker_unavailable_is_not_local_credential_failure_and_missing_source_never_rotates(self):
        with patch.object(broker,'reload_broker',side_effect=broker.SetupError('fixture offline')):
            first=self.supplied()
        self.assertEqual(first['broker_application'],'unconfirmed')
        pointer=self.data/'mqtt/current';pointer.unlink()
        with self.assertRaises(ExecutionError):self.supplied()
        self.assertFalse(pointer.exists())

    def test_unsupported_undeclared_and_invalid_profiles_stay_distinct(self):
        starter=from_manifest({'source':{'app':'xenova-starter'}})
        self.assertIs(starter['signals']['app_heartbeat'],False)
        unknown=from_manifest({'source':{'distribution':'ubuntu'}})
        self.assertIsNone(unknown['signals']['app_heartbeat'])
        self.assertEqual(provision.ensure(self.data,{'uuid':A,'provisioning_profile':unknown})['state'],'not-requested')
        self.assertFalse((self.data/'mqtt').exists())
        with self.assertRaises(ValueError):validate(starter|{'channels':['invalid']})
        absent=from_manifest({'source':{'distribution':'debian'},'guest_agent':{'verification':{'installed':False}}})
        self.assertIs(absent['signals']['agent_heartbeat'],False)
        for invalid in (None,0,'false'):
            with self.assertRaises(ValueError):from_manifest({'source':{'distribution':'ubuntu'},'guest_agent':{'verification':{'installed':invalid}}})


class ProvisioningHooksTest(unittest.TestCase):
    def setUp(self):
        self.provider=ProvisioningTest('runTest');self.provider.setUp();self.addCleanup(self.provider.doCleanups)

    def guest(self,root,distribution='debian'):
        from test_xenova_guest_ssh_graph import extract
        import prepare_rootfs as guest
        extract(root,{'debian':'debian-13-20261015','ubuntu':'ubuntu-24.04-20260926'}[distribution])
        unit=root/'usr/lib/systemd/system/xenova-agent.service'
        shutil.copyfile(ROOT/'platform/services/vm-agent/agent/deploy/xenova-agent.service',unit)
        guest.enable(root,'xenova-agent.service')

    @staticmethod
    def declare_agent(case, source):
        import xenova_catalog as catalog

        descriptor=case.f.data/'catalog'/case.spec['template_id']/(case.spec['template_version']+'.json')
        info=json.loads(descriptor.read_text())
        manifest=source/'template.json'
        meta=json.loads(manifest.read_text())
        meta['guest_agent']={'verification':{'installed':True}}
        manifest.chmod(0o644);manifest.write_text(json.dumps(meta));manifest.chmod(0o444)
        info['manifest_sha256']=catalog.sha(manifest)
        descriptor.write_text(json.dumps(info))

    def test_both_linux_create_hooks_use_real_preparation_supplier_and_agent_parser(self):
        from test_xenova_creation_reconcile import CreationReconcileTest
        import prepare_instance as preparation
        import prepare_rootfs as guest
        import xenova_preparation as ownership
        for distribution in ('debian','ubuntu'):
            with self.subTest(os=distribution):
                c=CreationReconcileTest('runTest');c.setUp();self.addCleanup(c.doCleanups)
                _, source=c.select(distribution);self.declare_agent(c,source)
                captured=[]
                def rootfs(root,record,command):
                    self.guest(root,distribution)
                    def keys(args,**kw):Path(args[-1]).write_text('external ssh key fixture')
                    guest.prepare_instance(root,record,command=keys)
                actual=provision.apply_linux
                def supplied(data,record,root,**kwargs):
                    result=actual(data,record,root,**kwargs)
                    env=dict(line.split('=',1) for line in (root/'etc/xenova/agent.env').read_text().splitlines())
                    captured.append(AgentSettings.from_env(env,instance_id_path=root/'etc/xenova/instance-id'))
                    return result
                def external(args,**kw):
                    if args[0]=='systemctl':return 'enabled\n'
                    if args[0]=='chroot':return '{"python":"guest-boundary","paho_mqtt":"2.1.0"}'
                    raise AssertionError('unexpected guest command')
                with c.linux_boundaries(),patch.object(preparation,'prepare_instance',rootfs),patch.object(provision,'apply_linux',supplied),patch.object(ownership,'check_output',external):
                    result=c.m.call('create',c.spec,execute=True)
                self.assertTrue(result['ok'],result);self.assertEqual(captured[0].vm_id,c.spec['vm_id'])
                before=provision.credential_file(c.f.data,c.spec['vm_id'],'agent').read_bytes()
                with patch.object(provision,'ensure',side_effect=AssertionError('completed replay resupplied')):
                    replay=c.m.fork().call('create',{k:v for k,v in c.spec.items() if k not in ('vcpus','memory_mib')},execute=True)
                self.assertTrue(replay['ok'],replay)
                self.assertTrue(provision.credential_file(c.f.data,c.spec['vm_id'],'agent').read_bytes()==before)

    def test_linux_creation_deadlines_preserve_persistent_accounts_across_cleanup_and_replay(self):
        from contextlib import ExitStack
        from test_xenova_broker_accounts import fixture_hash
        from test_xenova_creation_reconcile import CreationReconcileTest
        import prepare_instance as preparation
        import xenova_creation as creation
        import xenova_preparation as ownership

        # The product owns the account, supply, copy, publish and journal paths.
        # The existing hash double replaces only the external mosquitto_passwd.
        for distribution in ('debian','ubuntu'):
            for boundary in ('before-publish','published','dhcp-applied','start-confirmation'):
                with self.subTest(os=distribution,boundary=boundary), ExitStack() as stack:
                    case=CreationReconcileTest('runTest');case.setUp();stack.callback(case.doCleanups)
                    _, source=case.select(distribution);self.declare_agent(case,source)
                    ticks=[10.0];case.p.clock=lambda:ticks[0]
                    case.p.creation_policy_path.write_text(json.dumps({
                        'schema_version':'1.0','call_ms':30000,'execution_ms':10000,'cleanup_ms':100,
                    }))

                    def prepare_guest(root,record,command):
                        self.guest(root,distribution)
                        (root/'etc/xenova/instance-id').write_text(record['uuid']+'\n')

                    def external(argv, **kwargs):
                        if argv[0]=='systemctl':return 'enabled\n'
                        if argv[0]=='chroot':return '{"python":"guest-boundary","paho_mqtt":"2.1.0"}'
                        raise AssertionError('unexpected guest command')

                    def expire(phase, _):
                        if phase==boundary:
                            ticks[0]=21.
                            if boundary=='dhcp-applied':
                                creation.check(case.m,20.)

                    case.m.checkpoint=expire
                    with case.linux_boundaries(),patch.object(preparation,'prepare_instance',prepare_guest), \
                            patch.object(ownership,'check_output',external), \
                            patch.object(broker.subprocess,'run',side_effect=fixture_hash) as hasher, \
                            patch.object(case.p,'prepare_dhcp',wraps=case.p.prepare_dhcp) as dhcp:
                        initial=case.m.call('create',case.spec,execute=True)
                    self.assertEqual(dhcp.call_count,int(boundary in ('dhcp-applied','start-confirmation')))
                    if boundary=='start-confirmation':
                        self.assertTrue(initial['ok'],initial)
                    else:
                        case.error(initial,'DEADLINE_EXCEEDED')
                    hasher.assert_called_once()
                    op,=case.m._operations()
                    identity=op['vm_id']
                    root=case.f.data/op['owned_workspace']
                    target=case.f.data/'instances'/op['assigned']['internal_name']
                    supply=provision.credential_file(case.f.data,identity,'agent')
                    before=supply.read_bytes()
                    generation=broker.load(case.f.data/'mqtt',0,self.provider.rules)
                    self.assertEqual(supply.stat().st_mode&0o777,0o600)
                    self.assertEqual(provision.status(case.f.data,identity)['guest_application'],'private-root-configured')
                    self.assertIn(identity,generation['profiles'])
                    self.assertEqual(op['creation']['deadline_monotonic_ms'],20000)
                    if boundary=='start-confirmation':
                        self.assertEqual(op['status'],'SUCCEEDED')
                        case.f.xen.records=case.p.records(validate_assets=False)
                        command=case.p.command
                        def expire_after_start(argv, **kwargs):
                            result=command(argv,**kwargs)
                            if argv[:2]==['xl','create']:ticks[0]+=91.
                            return result
                        case.p.command=expire_after_start
                        with patch.object(case.p,'check_xen'):
                            start=case.m.call('start',{'vm_id':identity},idempotency_key='fixture-start')
                        case.error(start,'RECONCILE_REQUIRED')
                        control=case.f.data/'control-requests'/(start['operation_id']+'.json')
                        start_record=json.loads(control.read_text())
                        self.assertEqual(start_record['phase'],'uncertain')
                        self.assertEqual(start_record['error']['details']['stage'],'start_confirmation')
                        self.assertEqual(len([a for a in case.f.xen.calls if a[:2]==['xl','create']]),1)
                    else:
                        self.assertEqual(op['error']['code'],'DEADLINE_EXCEEDED')
                    self.assertEqual(target.exists(),boundary!='before-publish')
                    if boundary=='before-publish':
                        self.assertTrue(creation.holds(case.m,op))
                        resources=json.loads((root/'.preparation-resources.json').read_text())
                        self.assertTrue(resources['clean'])
                        self.assertEqual((resources['mounts'],resources['loops']),([],[]))
                        # The mount double materializes guest-root files on the host.
                        # A real unmount hides these in root.raw before workspace cleanup.
                        shutil.rmtree(root/'root')
                    else:
                        inode=(target/'root.raw').stat().st_ino

                    case.m.checkpoint=lambda *args:None
                    with patch.object(preparation,'_copy_verified',side_effect=AssertionError('no repeat copy')), \
                            patch.object(provision,'ensure',side_effect=AssertionError('no credential reissue')), \
                            patch.object(case.p,'prepare_dhcp',side_effect=AssertionError('no late DHCP')):
                        if boundary=='before-publish':
                            with case.linux_boundaries():
                                settled=case.m.call('operation-reconcile',case.request(op),execute=True)
                            self.assertTrue(settled['ok'],settled)
                            self.assertEqual(settled['data']['effective_status'],'FAILED')
                            self.assertFalse(root.exists())
                        elif boundary=='published':
                            diagnostic=case.m.call('operation-reconcile',{
                                'operation_id':op['operation_id'],'vm_id':identity,
                                'expected_spec_sha256':op['spec_hash'],'purpose':'discard-private',
                            })
                            case.error(diagnostic,'TARGET_CHANGED')
                        replay=case.m.call('create',case.spec,execute=True)
                    current,=case.m._operations()
                    self.assertEqual(replay['operation_id'],op['operation_id'])
                    self.assertEqual(current['creation'],op['creation'])
                    self.assertEqual(current['assigned'],op['assigned'])
                    self.assertTrue(supply.read_bytes()==before)
                    latest=broker.load(case.f.data/'mqtt',0,self.provider.rules)
                    self.assertEqual(latest['revision'],generation['revision'])
                    self.assertTrue(latest['secrets'][identity]==generation['secrets'][identity])
                    if boundary=='before-publish':
                        case.error(replay,'DEADLINE_EXCEEDED')
                        self.assertFalse(creation.holds(case.m,current))
                        self.assertEqual(current['error'],op['error'])
                    elif boundary=='published':
                        case.error(replay,'DEADLINE_EXCEEDED')
                        self.assertTrue(creation.holds(case.m,current))
                        self.assertEqual(current['error'],op['error'])
                    elif boundary=='dhcp-applied':
                        self.assertTrue(replay['ok'],replay)
                        self.assertEqual(current['status'],'SUCCEEDED')
                        self.assertEqual(current['first_error'],op['error'])
                    else:
                        self.assertTrue(replay['ok'],replay)
                        self.assertEqual(current,op)
                        self.assertEqual(json.loads(control.read_text()),start_record)
                    if boundary!='before-publish':
                        self.assertEqual((target/'root.raw').stat().st_ino,inode)
                    self.assertEqual(len([a for a in case.f.xen.calls if a[:2]==['xl','create']]),
                                     int(boundary=='start-confirmation'))

    def test_real_factory_hook_supplies_only_after_uuid_plan_and_never_on_completed_boot(self):
        from first_install_fixture import Board
        board=Board(self.provider.data/'board');board.grow_mount();board.factory()
        manifest=board.data/'.xenova-install.json';factory=json.loads(manifest.read_text())
        for item in factory['instances']:
            item['record']['provisioning_profile']=self.provider.profile
            path=board.data/'seeds'/item['record']['id']/'instance.json';path.write_text(json.dumps(item['record']))
        manifest.write_text(json.dumps(factory));self.assertFalse((board.data/'mqtt').exists())
        supplied=[]
        def mount_boundary(platform,record,**kwargs):
            root=self.provider.data/record['id'];root.mkdir();self.guest(root,'debian' if 'debian' in record['id'] else 'ubuntu')
            result=provision.apply_linux(platform.data,record,root,**kwargs)
            supplied.append(record['uuid']);self.assertEqual(json.loads((root/'etc/xenova/platform-identity.json').read_text())['vm_id'],record['uuid'])
            return result
        with patch.object(provision,'apply_stopped_linux',mount_boundary):board.p.register()
        self.assertEqual(len(supplied),2);self.assertEqual(len(set(supplied)),2)
        with patch.object(provision,'ensure',side_effect=AssertionError('completed factory resupplied')),patch.object(provision,'apply_stopped_linux',side_effect=AssertionError('completed factory remounted')):board.p.register()

    def zephyr_template(self,f):
        from test_xenova_zephyr import bundle
        from test_xenova_network_zephyr import network_bundle
        import xenova_catalog as catalog
        import xenova_zephyr as zephyr
        directory=network_bundle(bundle(f.p.data/'provision-starter'),f.p.data/'rtos-templates/provision-drive')
        config=directory/'zephyr.config';config.chmod(0o644);config.write_text(config.read_text()+'\nCONFIG_XENOVA_TELEMETRY=y\nCONFIG_XENOVA_DRIVE_WARM=y\nCONFIG_XENOVA_DRIVE_LATTICE=y\n');config.chmod(0o444)
        path=directory/'firmware.json';meta=json.loads(path.read_text());meta.update(id='provision-drive',kind='zephyr-xen-firmware',version='1.0.0');meta['source']['app']='xenova-drive';meta['files']['zephyr.config']={'sha256':zephyr.sha(config),'bytes':config.stat().st_size}
        path.chmod(0o644);path.write_text(json.dumps(meta));path.chmod(0o444)
        descriptor={'schema_version':'1.0','template_id':'provision-drive','template_version':'1.0.0','guest_kind':'zephyr','asset_directory':'rtos-templates/provision-drive','source_template_id':'provision-drive','manifest_sha256':catalog.sha(path)}
        dest=f.p.data/'catalog/provision-drive';dest.mkdir();(dest/'1.0.0.json').write_text(json.dumps(descriptor))
        return {'vm_id':B,'name':'supplied-zephyr','template_id':'provision-drive','template_version':'1.0.0'}

    def test_catalog_projects_only_declared_app_heartbeat(self):
        import test_xenova_recovery_foundation as foundation
        import xenova_catalog as catalog
        import xenova_management as management

        f = foundation.FoundationTest('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.zephyr_template(f)
        manager = management.Manager(f.p)
        catalog_entry = f.p.data/'catalog/provision-drive/1.0.0.json'
        manifest = f.p.data/'rtos-templates/provision-drive/firmware.json'
        for app, expected in (('xenova-drive', True), ('xenova-starter', False), ('undeclared-fixture', None)):
            with self.subTest(app=app):
                value = json.loads(manifest.read_text())
                value['source']['app'] = app
                manifest.chmod(0o644)
                manifest.write_text(json.dumps(value))
                manifest.chmod(0o444)
                descriptor = json.loads(catalog_entry.read_text())
                descriptor['manifest_sha256'] = catalog.sha(manifest)
                catalog_entry.write_text(json.dumps(descriptor))
                response = manager.call('templates')
                self.assertTrue(response['ok'], response)
                row = next(t for t in response['data']['templates'] if t['template_id'] == 'provision-drive')
                self.assertIs(row['provisioning_profile']['signals']['app_heartbeat'], expected)
                self.assertIs(row['capabilities'].get('app_heartbeat'), expected)
                if expected is None:
                    self.assertNotIn('app_heartbeat', row['capabilities'])

    def test_zephyr_create_hash_barrier_allows_other_actual_stop_and_preserves_credentials(self):
        import test_xenova_recovery_foundation as foundation
        import xenova_management as management
        f=foundation.FoundationTest('runTest');f.setUp();self.addCleanup(f.doCleanups);f.start()
        request=self.zephyr_template(f);manager=management.Manager(f.p,defaults_path=f.m.defaults_path)
        entered,release=threading.Event(),threading.Event();results=[];real=broker.hash_new
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(8):raise AssertionError('hash barrier not released')
            return real(*args,**kwargs)
        with patch.object(broker,'hash_new',slow):
            thread=threading.Thread(target=lambda:results.append(manager.call('create',request,execute=True)));thread.start()
            try:
                self.assertTrue(entered.wait(4),results);self.assertTrue(manager.fork().call('intents')['ok'])
                operation=manager.fork().call('operation',{'idempotency_key':'create:'+B})
                self.assertTrue(operation['ok'],operation);self.assertEqual(operation['data']['effective_status'],'RUNNING')
                stopped=f.m.fork().call('stop',{'vm_id':f.vm_id},idempotency_key='during-account-hash')
                self.assertTrue(stopped['ok'],stopped);self.assertEqual(len(f.physical('shutdown')),1)
                self.assertTrue(thread.is_alive())
            finally:release.set();thread.join(12)
        self.assertFalse(thread.is_alive());self.assertTrue(results[0]['ok'],results)
        original=provision.credential_file(f.p.data,B,'drive').read_bytes()
        self.assertTrue(manager.call('create',request,execute=True)['ok'])
        self.assertTrue(provision.credential_file(f.p.data,B,'drive').read_bytes()==original)
        view=manager.call('provisioning',{'vm_id':B});self.assertTrue(view['ok'],view)
        self.assertEqual(view['data']['profile']['apps']['autonomous']['model'],'stateful')

    def test_zephyr_original_create_resumes_complete_private_firmware_after_hash_failure_without_recopy(self):
        import test_xenova_recovery_foundation as foundation
        import xenova_management as management
        import xenova_zephyr as zephyr
        f=foundation.FoundationTest('runTest');f.setUp();self.addCleanup(f.doCleanups)
        request=self.zephyr_template(f);manager=management.Manager(f.p,defaults_path=f.m.defaults_path)
        with patch.object(zephyr,'copy_revision',wraps=zephyr.copy_revision) as copies:
            with patch.object(broker.subprocess,'run',side_effect=OSError(errno.EIO,'fixture interrupted hasher')):
                failed=manager.call('create',request,execute=True)
            self.assertFalse(failed['ok']);self.assertEqual(copies.call_count,1)
            original=manager._operations()[0]
            if original['vm_id']!=B:original=next(o for o in manager._operations() if o['vm_id']==B)
            before=json.loads((next((f.p.data/'mqtt').glob('.pending-*'))/'credentials.json').read_text())['accounts'][B]
            completed=manager.fork().call('create',request,execute=True)
            self.assertTrue(completed['ok'],completed);self.assertEqual(copies.call_count,1)
            self.assertEqual(completed['operation_id'],original['operation_id'])
            self.assertTrue(json.loads(provision.credential_file(f.p.data,B,'drive').read_text())['password']==before)

    def test_zephyr_supply_timeout_retains_published_broker_account_after_firmware_cleanup(self):
        from test_xenova_broker_accounts import fixture_hash
        from test_xenova_creation_reconcile import CreationReconcileTest
        import xenova_creation as creation
        import xenova_zephyr as zephyr

        case=CreationReconcileTest('runTest');case.setUp();self.addCleanup(case.doCleanups)
        request=self.zephyr_template(case)
        case.m.preparer=case.m._prepare
        ticks=[10.0];case.p.clock=lambda:ticks[0]
        ensure=broker.ensure_accounts

        def expire_after_account(*args, **kwargs):
            result=ensure(*args,**kwargs)
            ticks[0]=11.
            return result

        with patch.object(broker.subprocess,'run',side_effect=fixture_hash) as hasher, \
                patch.object(broker,'ensure_accounts',expire_after_account):
            result=case.m.call('create',request,execute=True,budget_ms=500)
        hasher.assert_called_once()
        case.error(result,'RECOVERY_REQUIRED')
        original,=case.m._operations()
        self.assertEqual(original['phase'],'preparing')
        self.assertEqual(original['automatic_cleanup']['state'],'pending')
        self.assertNotIn('creation',original)  # The Linux execution-deadline policy is not bound here.
        target=case.p.data/'instances'/original['assigned']['internal_name']
        workspace=case.p.data/original['owned_workspace']
        self.assertFalse(target.exists())
        self.assertTrue(workspace.exists())
        self.assertTrue(creation.holds(case.m,original))
        supply=provision.home(case.p.data,B)
        self.assertEqual(list(supply.iterdir()),[])
        generation=broker.load(case.p.data/'mqtt',0,self.provider.rules)
        self.assertTrue({B,'telemetry-'+B}.issubset(generation['profiles']))
        with patch.object(zephyr,'copy_revision',side_effect=AssertionError('no repeat copy')), \
                patch.object(broker,'ensure_accounts',side_effect=AssertionError('no credential reissue')):
            settled=case.m.call('operation-reconcile',case.request(original),execute=True)
            self.assertTrue(settled['ok'],settled)
            replay=case.m.call('create',request,execute=True)
        case.error(replay,original['error']['code'])
        self.assertEqual(replay['operation_id'],original['operation_id'])
        self.assertFalse(workspace.exists())
        self.assertFalse(creation.holds(case.m,case.m._operations()[0]))
        self.assertEqual(list(supply.iterdir()),[])
        self.assertEqual(provision.status(case.f.data,B)['state'],'not-supplied')
        latest=broker.load(case.p.data/'mqtt',0,self.provider.rules)
        self.assertEqual(latest['revision'],generation['revision'])
        self.assertTrue(latest['secrets']==generation['secrets'])

    def drive(self):
        profile=from_manifest({'source':{'app':'xenova-drive'}},config='CONFIG_XENOVA_TELEMETRY=y\nCONFIG_XENOVA_DRIVE_WARM=y\nCONFIG_XENOVA_DRIVE_LATTICE=y')
        record={'uuid':A,'provisioning_profile':profile};provision.ensure(self.provider.data,record)
        return record

    def test_actual_bootstrap_receipts_bind_new_execution_without_rotating_vm_credentials(self):
        record=self.drive();data=self.provider.data;writes=[]
        context={'vm_id':A,'host_boot_id':str(uuid4()),'execution_id':str(uuid4()),'domid':42,'xen_unique_id':'1234','signal_profile':record['provisioning_profile']}
        p=SimpleNamespace(data=data,clock=time.monotonic)
        class Store:
            quiescent=True
            def __init__(self,channel):self.channel=channel
            def apply(self,current,payload,previous):
                writes.append((self.channel,current,payload))
                native=os.environ.get('XENOVA_BOOTSTRAP_DECODER')
                if native:
                    # 실제 제품 C decoder가 root 공급 bytes를 소비한다. Store만 외부 경계다.
                    source=data/'consumer-bootstrap.json';source.write_text(json.dumps(payload));source.chmod(0o600)
                    env={**os.environ,'XT_TEST_BOOTSTRAP_CHECK':'1','XT_TEST_BOOTSTRAP':str(source),
                         'XT_TEST_INSTANCE_ID':A,'XT_TEST_ACCOUNT_CHANNEL':'xenova-'+self.channel}
                    result=subprocess.run(['unshare','--net','--',native],env=env,capture_output=True,text=True,timeout=15)
                    if result.returncode or 'BOOTSTRAP_CHECK 0' not in result.stdout:
                        raise AssertionError('actual C bootstrap consumer rejected supplied bytes')
                    source.unlink()
            def close(self):pass
        before=provision.credential_file(data,A,'drive').read_bytes()
        args=dict(deadline=time.monotonic()+8,store_factory=Store,observe=lambda:{'status':'observed',**context})
        result=provision.bootstrap_execution(p,record,**args)
        self.assertEqual(result['guest_application'],'store-published')
        receipts=provision.receipt_directory(data,A,context['execution_id'])
        first=json.loads((receipts/'drive.json').read_text())
        provision.bootstrap_execution(p,record,**args)
        self.assertEqual(first,json.loads((receipts/'drive.json').read_text()))
        self.assertEqual(writes[0][2]['boot_id'],writes[1][2]['boot_id'])
        self.assertEqual(writes[1][2]['drive_profile'],'warm-drive-1')
        from dom0_manager.recovery.rejoin import ProductionBootstrap
        config={'receipt_dir':str(data/'drive-rejoin'),'readiness_profile':'warm-drive-1',
                'app_state_models':{'autonomous':'stateful','manual':'stateless'}}
        with patch.dict('sys.modules',{'provision_xenova_telemetry':provision.bootstrap_api()}):
            consumer=ProductionBootstrap(config,lambda identity:{'status':'observed',**context})
            binding=consumer.refresh(context)
        self.assertEqual(binding['boot_id'],first['boot_id'])
        self.assertEqual(binding['app_instances'],first['drive_apps'])
        self.assertEqual(len(writes),4)  # refresh는 Store 재주입 없이 원 신원을 소비한다.
        context.update(execution_id=str(uuid4()),domid=43,xen_unique_id='1235')
        provision.bootstrap_execution(p,record,**args)
        self.assertNotEqual(first['boot_id'],writes[-1][2]['boot_id'])
        self.assertTrue(provision.credential_file(data,A,'drive').read_bytes()==before)
        self.assertEqual(result['authentication'],'unobserved')

    def test_separate_world_import_uses_real_codec_and_resupply_preserves_both_realms(self):
        record=self.drive();data=self.provider.data;path=provision.credential_file(data,A,'drive')
        local=json.loads(path.read_text())
        world={'broker_ipv4':'127.0.0.2','port':18885,'username':A,'password':'world-only-fixture',
               'profile':'lattice-lab-1','trusted_internal_plaintext':True}
        source=data/'approved-world.json';source.write_text(json.dumps({'vm_id':A,'world':world}));source.chmod(0o600)
        supplied=provision.install_world(data,A,source)
        self.assertEqual(supplied['authentication'],'unobserved');provision.ensure(data,record)
        parsed,_=provision.bootstrap_api().credentials(path,A,'drive')
        self.assertTrue(parsed['password']==local['password']);self.assertTrue(parsed['world']==world)
        self.assertNotIn(world['password'],json.dumps(supplied))
        source.chmod(0o644)
        with self.assertRaises(ValueError):provision.install_world(data,A,source)

    def test_real_agent_and_two_isolated_brokers_enforce_account_boundaries(self):
        import paho.mqtt.client as mqtt
        from xenova_agent.__main__ import build_client,build_telemetry
        from xenova_agent.heartbeat import HeartbeatSender
        record=self.provider.record;summary=provision.ensure(self.provider.data,record)
        original=self.provider.value();servers=[];clients=[]
        rules=json.loads((ROOT/'platform/deploy/mosquitto/etc/policy.json').read_text())
        rules['services']={'telemetry-reference-subscriber':rules['services']['telemetry-reference-subscriber']}
        try:
            for realm in ('pi','world'):
                directory=self.provider.data/realm;directory.mkdir()
                # Independent broker writer roots and passwords; no network bridges.
                provision.ensure(directory,record,gid=0,rules=rules)
                value=broker.load(directory/'mqtt',0,rules)
                with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
                config=directory/'mosquitto.conf'
                config.write_text(f'user root\nlistener {port} 127.0.0.1\nallow_anonymous false\npassword_file {value["path"]}/passwd\nacl_file {value["path"]}/acl\n')
                server=subprocess.Popen(['mosquitto','-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                servers.append(server)
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                    except OSError:
                        self.assertIsNone(server.poll());time.sleep(.01)
                reader=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2);ready=threading.Event();messages=[]
                reader.username_pw_set(A,value['secrets'][A])
                reader.on_connect=lambda client,*args:client.subscribe('xenova/v1/vms/+/heartbeat',qos=1)
                reader.on_subscribe=lambda *args:ready.set()
                reader.on_message=lambda client,unused,msg:messages.append(msg.topic)
                reader.connect('127.0.0.1',port);reader.loop_start();clients.append(reader)
                self.assertTrue(ready.wait(3))
                telemetry=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2);v2_ready=threading.Event();v2=[]
                telemetry.username_pw_set('telemetry-reference-subscriber',value['secrets']['telemetry-reference-subscriber'])
                telemetry.on_connect=lambda client,*args:client.subscribe('xenova/v2/vms/+/telemetry/heartbeat',qos=1)
                telemetry.on_subscribe=lambda *args:v2_ready.set()
                telemetry.on_message=lambda client,unused,msg:v2.append((msg.topic,msg.payload))
                telemetry.connect('127.0.0.1',port);telemetry.loop_start();clients.append(telemetry)
                self.assertTrue(v2_ready.wait(3))
                supplied=dict(line.split('=',1) for line in provision.credential_file(directory,A,'agent').read_text().splitlines())
                # 배포 listener 주소/port만 격리 loopback으로 대체한다. 실제 공급된 자격은 그대로 소비한다.
                supplied.update(XENOVA_AGENT_BROKER_HOST='127.0.0.1',XENOVA_AGENT_BROKER_PORT=str(port))
                settings=AgentSettings.from_env(supplied)
                first,second=build_client(settings),build_client(settings);clients.extend((first,second))
                self.assertNotEqual(first._paho._client_id,second._paho._client_id)
                self.assertTrue(first.start(wait_s=3));self.assertTrue(second.start(wait_s=3))
                heartbeat=HeartbeatSender(first,A);self.assertTrue(heartbeat.send_once())
                publisher=build_telemetry(first,settings,reporter=None,reporter_thread=None,sender=heartbeat)
                publisher.tick()
                second.publish_raw(f'xenova/v1/vms/{B}/heartbeat',b'forbidden',qos=1,retain=False,wait_s=2)
                first.publish_raw(f'xenova/v2/vms/{B}/telemetry/heartbeat',b'forbidden',qos=1,retain=False,wait_s=2)
                end=time.monotonic()+2
                while (not messages or not v2) and time.monotonic()<end:time.sleep(.01)
                self.assertIn(f'xenova/v1/vms/{A}/heartbeat',messages)
                self.assertNotIn(f'xenova/v1/vms/{B}/heartbeat',messages)
                self.assertIn(f'xenova/v2/vms/{A}/telemetry/heartbeat',[topic for topic,_ in v2])
                self.assertNotIn(f'xenova/v2/vms/{B}/telemetry/heartbeat',[topic for topic,_ in v2])
                self.assertIs(json.loads(v2[0][1])['payload']['app_health_asserted'],False)
                self.assertTrue(first.connected and second.connected)
            self.assertTrue(original['secrets'][A]==self.provider.value()['secrets'][A])
            self.assertEqual(summary['authentication'],'unobserved')
        finally:
            for client in reversed(clients):
                if hasattr(client,'stop'):client.stop()
                else:client.disconnect();client.loop_stop()
            for server in servers:
                server.terminate();server.wait(5)

    def test_actual_arm_template_is_selected_copied_registered_and_supplied_to_distinct_factory_vms(self):
        source=os.environ.get('XENOVA_ACTUAL_DRIVE_TEMPLATE')
        if not source:self.skipTest('requires this candidate real ARM drive bundle; no synthetic fallback')
        import test_xenova_default_deployment as deployment
        import xenova_vm as vm
        from xenova_runtime import operation_lock
        from first_install_fixture import assemble
        f=deployment.DefaultDeploymentTest('runTest');f.setUp();self.addCleanup(f.doCleanups)
        board,factory=f.current_board()
        for item in factory['firmware_instances']:
            shutil.rmtree(board.data/'seeds'/item['record']['id'])  # owned tiny seed fixture only
        shutil.rmtree(board.data/'rtos-templates')  # owned old Starter fixture, before the fresh assembly copy
        entry=assemble.rtos.info(Path(source),assemble.sha(Path(source)/'firmware.json'))
        factory['rtos_templates']=assemble.copy_rtos_templates(board.data,[entry])
        factory['firmware_instances']=assemble.copy_rtos_seeds(board.data,[entry])
        (board.data/'.xenova-install.json').write_text(json.dumps(factory))
        with operation_lock(board.p.run) as common:board.p.register(common=common)
        records=vm.load_records(board.data);a,b=(records[n] for n in ('zephyr-drive-a','zephyr-drive-b'))
        self.assertEqual(a['active_firmware']['sha256'],entry['metadata']['files']['zephyr.bin']['sha256'])
        self.assertEqual(a['active_firmware'],b['active_firmware']);self.assertNotEqual(a['uuid'],b['uuid'])
        self.assertEqual(a['template_id'],'zephyr-drive-lattice-lab-arm64')
        aa=json.loads(provision.credential_file(board.data,a['uuid'],'drive').read_text())
        bb=json.loads(provision.credential_file(board.data,b['uuid'],'drive').read_text())
        self.assertTrue(aa['password']!=bb['password'])
        with patch.object(provision,'ensure',side_effect=AssertionError('completed factory repeated supply')):board.p.register()


if __name__=='__main__':unittest.main()
