"""종료 관측의 whitelist·비차단 경계·원 cleanup/exit 보존. 실보드 진단 수락은 아니다."""
import io
import json
import logging
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from xenova_gateway.diagnostics import GatewayDiagnostics, _final_snapshot


def sample():
    secret='PASSWORD_DO_NOT_PRINT /private/authority.json request-body-token'
    failure=('Traceback (most recent call last):\n'
             '  File "/private/SECRET_LOCATION/ipc.py", line 135, in _loop\n'
             '    raise ValueError("'+secret+'")\n'
             'ValueError: '+secret+'\n'
             '  File "/private/ipc.py", line 1, in SECRET_AFTER_HEADER\n')
    server=SimpleNamespace(thread=threading.Thread(),stats=dict(accepted=4,decoded=3,encoded=1,expired=2,
                           busy=0,peer_denied=0,high_water=2,malformed=0,secret=secret),failure=failure)
    worker=SimpleNamespace(_busy=threading.Event(),_jobs=queue.Queue(1),_results=queue.Queue(1))
    worker._results.put_nowait(secret)
    operator=SimpleNamespace(queues=[queue.Queue(2),queue.Queue(8),queue.Queue(4)],pending={secret:object()},
                             _waiting=[secret],job=object(),worker=worker,unknown=secret)
    operator.queues[0].put_nowait(secret)
    ingress=queue.Queue();ingress.put_nowait(secret)
    runner=SimpleNamespace(operator=operator,_queue=ingress,dropped_queue_full=7,rejected_commands=8,
                           authority=secret,device_view=Mock(side_effect=AssertionError('must not refresh')))
    sink=SimpleNamespace(_device_view=dict(fault='DEVICE_LINK_UNAVAILABLE',ready=False,clock_ready=False,
                         report_is_latest=False,parser_invalid=3,parser_oversize=0,stale_or_invalid=2,pending=1,
                         secret=secret),device=SimpleNamespace(fault='DEVICE_APPLY_TIMEOUT',
                         _sensor_blocked_reason='DEVICE_HANDSHAKE_TIMEOUT',credential=secret))
    return server,runner,sink,secret


def test_whitelist_json_never_formats_original_failure_input_or_authority():
    server,runner,sink,secret=sample()
    result=_final_snapshot(server,server,None,runner,sink)
    raw=json.dumps(result,separators=(',',':'))
    assert len(raw)<3584 and secret not in raw and '/private' not in raw
    assert 'SECRET_AFTER_HEADER' not in raw and 'SECRET_LOCATION' not in raw
    assert result['ipc']['operator']['failure']==dict(present=True,exception_type='ValueError',
        frames=[dict(module='ipc.py',function='_loop',line=135)])
    assert result['operator']['queue_depths']==[1,0,0]
    assert result['operator']['pending']==1 and result['operator']['waiting']==1
    assert result['operator']['worker_jobs_depth']==0 and result['operator']['worker_results_depth']==1
    assert result['ingress']==dict(queue_depth=1,dropped_queue_full=7,rejected_commands=8)
    assert result['device']['cached_fault']=='DEVICE_LINK_UNAVAILABLE'
    assert result['device']['native_fault']=='DEVICE_APPLY_TIMEOUT'
    assert result['device']['native_sensor_blocked_reason']=='DEVICE_HANDSHAKE_TIMEOUT'
    runner.device_view.assert_not_called()
    assert result['completion_proven'] is False


def test_contended_actor_and_ingress_queues_do_not_wait_or_call_qsize():
    _,runner,sink,_=sample();lane=runner.operator.queues[0];ingress=runner._queue
    finished=threading.Event();result=[]
    jobs,results=runner.operator.worker._jobs,runner.operator.worker._results
    lane.mutex.acquire();ingress.mutex.acquire();jobs.mutex.acquire();results.mutex.acquire()
    def observe():
        result.append(_final_snapshot(None,None,None,runner,sink));finished.set()
    thread=threading.Thread(target=observe)
    try:
        with patch.object(lane,'qsize',side_effect=AssertionError('blocking qsize')):
            thread.start();completed=finished.wait(.5)
    finally:
        results.mutex.release();jobs.mutex.release();ingress.mutex.release();lane.mutex.release();thread.join(1)
    assert completed and not thread.is_alive()
    assert result[0]['operator']['queue_depths'][0] is None
    assert result[0]['ingress']['queue_depth'] is None
    assert result[0]['operator']['worker_jobs_depth'] is None and result[0]['operator']['worker_results_depth'] is None


def test_huge_failure_is_prefix_limited_and_frames_are_globally_bounded():
    server,_,_,_=sample()
    server.failure=('Traceback (most recent call last):\n'+
        '  File "/never-print/ipc.py", line 1, in _loop\n'*100+'RuntimeError: DO_NOT_PRINT\n'+'Z'*1000000)
    result=_final_snapshot(server,server,server,None,None)
    assert sum(len(s['failure']['frames']) for s in result['ipc'].values())<=4
    assert len(json.dumps(result))<4096
    assert 'DO_NOT_PRINT' not in json.dumps(result)
    server.failure='X'*4096+'\n  File "/private/ipc.py", line 7, in SHOULD_NOT_PARSE\n'
    assert not _final_snapshot(server,None,None,None,None)['ipc']['provider']['failure']['frames']


def test_missing_and_untrusted_values_are_unavailable_without_string_formatting():
    class Poison:
        def __str__(self):raise AssertionError('must not stringify')
    server=SimpleNamespace(stats=dict(accepted=Poison(),busy=True),failure=Poison())
    sink=SimpleNamespace(_device_view={'fault':'PASSWORD_SECRET','ready':Poison()},
                         device=SimpleNamespace(fault=Poison(),_sensor_blocked_reason='UNKNOWN_SECRET'))
    result=_final_snapshot(server,None,None,None,sink)
    assert result['ipc']['provider']['counters']['accepted'] is None
    assert result['ipc']['provider']['counters']['busy'] is None
    assert result['device']['cached_fault'] is None and result['device']['native_fault'] is None
    assert result['device']['ready'] is None
    assert result['ipc']['operator']['present'] is False
    assert _final_snapshot(None,None,None,None,None)['device']['cached_view_available'] is False


def channel():
    logger=logging.Logger('declared-final-diagnostic',logging.WARNING)
    stream=io.StringIO();handler=logging.StreamHandler(stream);logger.addHandler(handler)
    return GatewayDiagnostics(logger),stream


def test_one_info_record_is_complete_json_below_existing_log_cap():
    server,runner,sink,secret=sample();owner,stream=channel()
    try:
        assert owner.emit_final(ipc=server,operator_ipc=server,runner=runner,sink=sink)
        owner.close()
        lines=stream.getvalue().splitlines()
        assert len(lines)==1 and len(lines[0])<4096 and secret not in lines[0]
        result=json.loads(lines[0].removeprefix('gateway.final '))
        assert result['stage']=='before_diagnostics_close' and result['completion_proven'] is False
        assert owner.submitted==1
    finally:owner.close()


def test_final_emit_contention_and_failure_are_best_effort_without_waiting():
    owner,_=channel()
    try:
        owner._admission.acquire()
        try:assert owner.emit_final() is False
        finally:owner._admission.release()
        owner.queue.mutex.acquire()
        try:assert owner.emit_final() is False
        finally:owner.queue.mutex.release()
        with patch('xenova_gateway.diagnostics._final_snapshot',side_effect=RuntimeError('SECRET')):
            assert owner.emit_final() is False
    finally:owner.close()


@pytest.mark.parametrize('original_failure',[False,True])
def test_entrypoint_final_diagnostic_error_never_changes_original_exit_or_cleanup(original_failure):
    import xenova_gateway.__main__ as entry
    order=[]
    settings=SimpleNamespace(log_level='WARNING',profile='legacy',sink='mock',broker_host='declared',broker_port=1,
        username=None,password=None,start_mode='SAFE_STOP',retained_wait_s=0)
    lease=SimpleNamespace(close=lambda:order.append('lease'))
    client=SimpleNamespace(stop=lambda:order.append('client'))
    sink=SimpleNamespace(close=lambda:order.append('sink'))
    diagnostic=SimpleNamespace(emit_final=Mock(side_effect=RuntimeError('do not log diagnostic secret')),
                               close=lambda:order.append('diagnostics'))
    runner=SimpleNamespace(run=lambda stop:None,scene_runtime=None)
    original=OSError(5,'original output cause')
    with patch.object(entry,'parse',return_value=settings),patch.object(entry,'_windows_timer_resolution'), \
            patch.object(entry.signal,'signal'),patch.object(entry.logging,'basicConfig'), \
            patch.object(entry,'GatewayDiagnostics',return_value=diagnostic), \
            patch.object(entry,'GatewayLease',return_value=Mock(acquire=Mock(return_value=lease))), \
            patch('xenova_mqtt.XenovaClient',return_value=client), \
            patch.object(entry,'GatewayRunner',return_value=runner), \
            patch.object(entry,'open_sink',side_effect=original if original_failure else None,return_value=sink), \
            patch.object(entry.log,'error') as error:
        assert entry.main([])==(2 if original_failure else 0)
    diagnostic.emit_final.assert_called_once()
    assert order[-2:]==['lease','diagnostics']
    if original_failure:
        assert error.call_args.kwargs['exc_info'][1] is original
    else:assert not error.called
