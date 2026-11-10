"""기존 PC 환경의 서버 없는 AppTest. 원 fixture 관측이며 장비·LIVE 수락 시험이 아니다."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from snapshot import SCHEMA
from streamlit.testing.v1 import AppTest


class DashboardAppTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()
