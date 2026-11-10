"""운영 CLI의 가벼운 분기·원 요청 식별자·거절/timeout 표시를 검사한다.

transport 호출만 명시 fixture로 대체한다. 실제 관리 소켓/현재 S/M 설정 적용은 별도
시험이며 이 모듈은 doctor·카메라·상주 runtime을 기동하지 않는다.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from xenova_vehicle import cli


REQUEST = '70000000-0000-4000-8000-000000000001'


class OperationCliTests(unittest.TestCase):
    def test_operation_help_does_not_load_runtime_doctor_or_native_dependencies(self):
        code = '''import sys
from xenova_vehicle import cli
try:
    cli.main(['operation','--help'])
except SystemExit as error:
    assert error.code == 0
for name in ('xenova_vehicle.doctor','xenova_vehicle.runtime.resident_runtime',
             'xenova_vehicle.runtime.operation_management','depthai','numpy','cv2','sqlite3'):
    assert name not in sys.modules, name
'''
        result = subprocess.run([sys.executable, '-B', '-c', code],
            capture_output=True, text=True, timeout=10, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('standby', result.stdout)
        self.assertIn('--request-id', result.stdout)

    def test_request_arguments_and_applied_result_are_preserved_without_runtime_start(self):
        from xenova_vehicle.runtime import operation_management
        expected = dict(status='APPLIED', action='RUN', request_id=REQUEST,
            reason='OPERATION_SETTING_APPLIED_NOT_GEOMETRY_READY')
        output = io.StringIO()
        with patch.object(operation_management, 'operation_command', return_value=expected) as call, redirect_stdout(output):
            status = cli.main(['operation','run','--state-dir','/explicit-state',
                '--request-id',REQUEST,'--timeout','1.5','--json'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        call.assert_called_once_with(Path('/explicit-state'),'run',request_id=REQUEST,timeout_s=1.5)

    def test_invalid_operation_timeouts_are_rejected_before_loading_the_transport(self):
        code='''import sys
from xenova_vehicle import cli
for value in ('6','30','nan','inf','0','-1'):
    try:
        cli.main(['operation','run','--state-dir','/unused','--timeout',value])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError('invalid operation timeout accepted')
assert 'xenova_vehicle.runtime.operation_management' not in sys.modules
'''
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_refusal_and_unknown_query_are_not_reported_as_success(self):
        from xenova_vehicle.runtime import operation_management
        for result in (dict(status='REJECTED', reason='CURRENT_GATE_UNAVAILABLE'),
                       dict(status='UNKNOWN', reason='UNKNOWN_REQUEST', request_id=REQUEST),
                       dict(status='EXPIRED', reason='REQUEST_EXPIRED', request_id=REQUEST)):
            output = io.StringIO()
            with self.subTest(result=result), redirect_stdout(output), \
                    patch.object(operation_management, 'operation_command', return_value=result) as call:
                status = cli.main(['operation','query','--state-dir','/explicit-state','--request-id',REQUEST,'--json'])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue()), result)
            self.assertEqual(call.call_count, 1)

    def test_timeout_keeps_original_request_id_and_never_retries_with_another_key(self):
        from xenova_vehicle.runtime import operation_management
        output = io.StringIO()
        failure = TimeoutError('request_id=' + REQUEST + '; query this original request')
        with redirect_stderr(output), patch.object(operation_management, 'operation_command', side_effect=failure) as call:
            status = cli.main(['operation','standby','--state-dir','/explicit-state','--request-id',REQUEST])
        self.assertEqual(status, 2)
        self.assertIn(REQUEST, output.getvalue())
        self.assertEqual(call.call_count, 1)
