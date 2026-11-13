"""기존 PC 환경의 서버 없는 AppTest. 원 fixture 관측이며 장비·LIVE 수락 시험이 아니다."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from snapshot import SCHEMA
from streamlit.testing.v1 import AppTest
from live_fixtures import live_document


class DashboardAppTests(unittest.TestCase):
    def setUp(self):
        environment=patch.dict(os.environ,{'XENOVA_VIEWER_OBSERVATION_DIR':''});environment.start();self.addCleanup(environment.stop)

    def app(self):
        return AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=15).run()

    def test_unconnected_dashboard_has_only_disabled_load_button_and_copy_commands(self):
        app=self.app()
        self.assertEqual(list(app.exception),[])
        self.assertEqual(len(app.metric),6)
        self.assertTrue(all(x.value=='UNKNOWN' for x in app.metric))
        self.assertEqual(len(app.button),1)
        self.assertTrue(app.button[0].disabled)
        code=[x.value for x in app.code]
        self.assertIn('xenova-drive stop',code)
        self.assertIn('xenova-drive operation status',code)
        self.assertIn('새 실행 코스',[x.label for x in app.selectbox])

    def test_file_fixture_reports_never_light_input_or_create_live_ready_on_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            body=dict(schema=SCHEMA,source=dict(id='declared-ui-fixture',epoch='original-epoch',
                sequence='1',kind='DIAGNOSTIC',stamp=dict(domain='jetson-j',epoch='j-original',us='100')),
                operation=dict(mode='AUTO',active_vm='A',standby_vm='B',parking_enabled=False,
                    vehicle_phase='WAIT',auto_ready=True,blocked_reason='INPUT_EXPIRED'),
                map=dict(layers=[dict(kind='ESTIMATE',frame='odom:fixture',points=[[1,2]])]),media=[])
            path=Path(directory)/'snapshot.json';raw=json.dumps(body).encode();path.write_bytes(raw)
            app=self.app();app.text_input[0].set_value(directory).run()
            self.assertEqual(list(app.exception),[])
            self.assertTrue(all(x.value=='UNKNOWN' for x in app.metric))
            self.assertIn('원 보고: AUTO',[x.value for x in app.caption])
            table=app.table[0].value
            self.assertEqual(set(table['관측']),{'UNKNOWN'})
            original=app.session_state['reader'].current
            app.run()
            self.assertEqual(list(app.exception),[])
            self.assertEqual(app.session_state['reader'].current.first_received,original.first_received)
            self.assertEqual(path.read_bytes(),raw)
            self.assertTrue(app.button[0].disabled)

    def test_live_boot_status_and_zero_frames_are_visible_from_environment_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            body=live_document();body['live']['connection']['last_error']='declared Jetson not connected'
            path=Path(directory)/'snapshot.json';raw=json.dumps(body).encode();path.write_bytes(raw)
            with patch.dict(os.environ,{'XENOVA_VIEWER_OBSERVATION_DIR':directory}):app=self.app()
            self.assertEqual(list(app.exception),[])
            self.assertEqual(app.text_input[0].value,directory)
            self.assertTrue(any('WAITING_FOR_RUN' in x.value for x in app.info))
            self.assertTrue(any('Jetson not connected' in x.value for x in app.warning))
            self.assertEqual(app.table[0].value['수신 수'].tolist(),['0'])
            self.assertEqual(set(app.table[1].value['관측']),{'UNKNOWN'})
            self.assertEqual(path.read_bytes(),raw)
            self.assertTrue(all(x.disabled for x in app.button))

    def test_live_report_displays_mode_owner_and_does_not_infer_applied_or_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            body=live_document();body['operation']=dict(mode='MANUAL',active_vm='B',standby_vm='A',vehicle_phase='WAIT')
            body['live']['streams'].append(dict(name='operation',state='FRESH',received_count=1))
            body['live']['stages']['command']=dict(state='SENT',source_id='gateway',source_sequence=2,
                source_stamp=None,pc_received_stamp=None,detail='declared command transport')
            body['map']['layers']=[dict(kind='MODEL',frame='MAP:one',points=[[0,0],[1,1]]),
                dict(kind='ESTIMATE',frame='ODOM:two',points=[[.1,.2]])]
            (Path(directory)/'snapshot.json').write_text(json.dumps(body),encoding='utf-8')
            app=self.app();app.text_input[0].set_value(directory).run()
            self.assertEqual(list(app.exception),[])
            self.assertEqual([x.value for x in app.metric[:3]],['MANUAL','B','A'])
            self.assertEqual(app.table[1].value['관측'].tolist(),['UNKNOWN','SENT','UNKNOWN','UNKNOWN'])
            captions=[x.value for x in app.caption]
            self.assertIn('원 frame · MAP:one',captions);self.assertIn('원 frame · ODOM:two',captions)
            first=app.session_state['reader'].current.first_received;app.run()
            self.assertEqual(app.session_state['reader'].current.first_received,first)
            self.assertTrue(all(x.disabled for x in app.button))


if __name__=='__main__':unittest.main()
