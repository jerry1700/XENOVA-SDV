"""최초 설치의 로컬 등록과 구형 통지 job 호환. HTTP 왕복은 process 시험에서 다룬다."""
import configparser
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from first_install_fixture import Board
import xenova_first_boot as boot
import xenova_registration_hook as hook
from xenova_runtime import operation_lock

ROOT=Path(__file__).resolve().parents[2]
VM='850e8400-e29b-41d4-a716-446655440001'


class RegistrationBootTest(unittest.TestCase):
    def test_legacy_defer_subprocess_preserves_async_job_and_secret_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'run').mkdir();(root/'data').mkdir();(root/'bin').mkdir()
            env=root/'local.env';env.write_text('XENOVA_HOST=127.0.0.1\nXENOVA_PORT=1\n');env.chmod(0o600)
            # systemd command boundary only. The product automatic/defer subprocess is real.
            command=root/'bin/systemd-run'
            command.write_text('#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n'
                               'Path(__file__).with_name("submitted.json").write_text(json.dumps({"args":sys.argv[1:],'
                               '"leaked":[k for k in os.environ if k in ("VM_REGISTRATION_SERVICE_TOKEN","XENOVA_REGISTRATION_URL")]}))\n')
            command.chmod(0o755)
            with patch.dict(os.environ,{'PATH':str(root/'bin')+':'+os.environ['PATH'],
                                        'VM_REGISTRATION_SERVICE_TOKEN':'fixture-must-not-inherit',
                                        'XENOVA_REGISTRATION_URL':'https://fixture.invalid'}):
                result=hook.automatic(root/'data',root/'run',{'uuid':VM},wait_ready=True,api_env=env,token_file=root/'token')
            self.assertEqual(result['state'],'attempt-started')
            end=time.monotonic()+3
            submitted=root/'bin/submitted.json'
            while not submitted.exists() and time.monotonic()<end:time.sleep(.02)
            value=json.loads(submitted.read_text());args=value['args']
            self.assertEqual(value['leaked'],[])
            self.assertIn('--no-block',args);self.assertIn('--collect',args)
            self.assertIn('--property=After=xenova-dom0-manager.service',args)
            self.assertIn('--property=Requires=xenova-dom0-manager.service',args)
            self.assertIn('--property=Restart=no',args)
            # 이 실행 인자에는 최초 등록 시점의 원격 호출 deadline이 없다.
            index=args.index('after-api')
            self.assertEqual(args[index+1:],[str(root/'data'),str(root/'run'),str(env),str(root/'token'),VM])
            self.assertFalse((root/'data/registration-notices').exists())

    def first_boot(self, board):
        with (patch.object(boot,'FirstBoot',return_value=board.p),
              patch.object(boot,'operation_lock',side_effect=lambda:operation_lock(board.p.run)),
              patch.object(boot.sys,'argv',['xenova-first-boot','register'])):
            boot.main()

    def test_first_boot_entrypoint_registers_locally_without_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            board=Board(Path(directory)/'board');board.grow_mount();board.factory()
            with patch.object(hook,'automatic',side_effect=AssertionError('factory must not notify')):
                self.first_boot(board)
                registry={p.parent.name:p.read_bytes() for p in board.data.glob('instances/*/instance.json')}
                self.assertEqual(len(registry),2)
                state_path=board.data/'.first-install-state.json'
                state=json.loads(state_path.read_text())
                self.assertEqual(state['phase'],'complete')
                self.assertNotIn('registration_notices',state)
                self.assertEqual({json.loads(value)['uuid'] for value in registry.values()},
                                 {value['uuid'] for value in state['identities'].values()})
                assets={str(p.relative_to(board.data)):hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in board.data.glob('instances/*/*') if p.is_file()}
                self.first_boot(board)
                self.assertEqual(registry,{p.parent.name:p.read_bytes() for p in board.data.glob('instances/*/instance.json')})
                self.assertEqual(assets,{str(p.relative_to(board.data)):hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in board.data.glob('instances/*/*') if p.is_file()})
                self.assertEqual(state,json.loads(state_path.read_text()))
            self.assertFalse((board.data/'registration-notices').exists())
            self.assertFalse((board.p.run/'registration-notices').exists())
            self.assertTrue(json.loads((board.p.run/'install-ready.json').read_text())['fresh_registration'] is False)

    def test_existing_factory_notice_history_is_preserved_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            board=Board(Path(directory)/'board');board.grow_mount();board.factory();board.p.register()
            path=board.data/'.first-install-state.json'
            state=json.loads(path.read_text())
            legacy=[next(iter(state['identities'].values()))['uuid']]
            state['registration_notices']=legacy;path.write_text(json.dumps(state))
            previous=board.data/'registration-notices'/(legacy[0]+'.json')
            previous.parent.mkdir()
            previous.write_text('{"automatic":true,"state":"delivery-unknown","confirmed":false}\n')
            saved=previous.read_bytes()
            with patch.object(hook,'automatic',side_effect=AssertionError('factory must not replay')):
                self.first_boot(board)
                self.first_boot(board)
            self.assertEqual(json.loads(path.read_text())['registration_notices'],legacy)
            self.assertEqual(previous.read_bytes(),saved)
            self.assertEqual(list(previous.parent.iterdir()),[previous])
            self.assertFalse((board.p.run/'registration-notices').exists())

    def test_ready_unit_is_after_autostart_and_boot_does_not_wait_for_api(self):
        files=ROOT/'platform/yocto/meta-xenova/recipes-core'
        def unit(relative):
            parser=configparser.ConfigParser(interpolation=None,strict=False);parser.read(files/relative);return parser
        api=unit('xenova-dom0-manager/files/xenova-dom0-manager.service')
        first=unit('xenova-platform/files/xenova-first-boot.service')
        admission=unit('xenova-platform/files/xenova-host-admission.service')
        self.assertEqual(api['Service']['Type'],'notify');self.assertEqual(api['Service']['NotifyAccess'],'main')
        self.assertIn('xenova-host-admission.service',api['Unit']['After'].split())
        self.assertIn('xenova-vms.service',admission['Unit']['After'].split())
        for key in ('After','Before','Wants','Requires','BindsTo','PartOf'):
            dependencies=first['Unit'].get(key,'').split()
            for external in (hook.API_UNIT,'network-online.target','tailscaled.service'):
                self.assertNotIn(external,dependencies)
        self.assertNotIn('ExecStartPost',api['Service'])


if __name__=='__main__':unittest.main()
