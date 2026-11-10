"""Gateway 부가 로그와 종료 시 한 번의 whitelist 관측만 전달한다.

권위/원 요청/시리얼 출력은 큐에 넣지 않으며 종료 관측도 완료·freshness 증거가 아니다.
"""
import builtins
from collections import deque
import json
import logging
import copy
import re
import sys
from logging.handlers import QueueHandler
import queue
import threading

_CONTROLS={i:'\\x'+format(i,'02x') for i in (*range(32),127)}
_IPC_COUNTERS=('accepted','decoded','encoded','expired','busy','peer_denied','high_water','malformed')
_DEVICE_COUNTERS=('parser_invalid','parser_oversize','stale_or_invalid','pending','command_sequence','clock_lease_revision')
_FAULTS=frozenset(('DEVICE_LINK_UNAVAILABLE','DEVICE_HANDSHAKE_TIMEOUT','DEVICE_APPLY_TIMEOUT',
    'DEVICE_CAPABILITY_UNSUPPORTED','DEVICE_RESTARTED','DEVICE_ODOMETRY_UNSUPPORTED','DEVICE_REJECTED',
    'DEVICE_FAULT','DEVICE_EVIDENCE_OVERFLOW','DEVICE_COUNTER_EXHAUSTED','DEVICE_CLOCK_DISCONTINUITY'))
_MODULES=frozenset(('ipc.py','operator_service.py','operator_client.py','output_owner.py','feedback.py',
    'feedback3.py','authority.py','authority_journal.py','provider.py','guest_clock.py','episode.py',
    'keyboard_server.py','keyboard_actor.py','scene_runtime.py','scene_authority.py','verified_runner.py',
    'runner.py','drive_link.py','queue.py','threading.py','selectors.py','socket.py','ssl.py'))
_EXCEPTIONS=frozenset(n for n,v in vars(builtins).items() if isinstance(v,type) and issubclass(v,BaseException)) | frozenset(
    ('SinkError','AuthorityError','OperatorError','FeedbackError','SceneAuthorityError','GatewayUnavailable','ProviderError'))
_FRAME=re.compile(r'^  File "([^"\r\n]{1,1024})", line ([0-9]{1,7}), in ([A-Za-z_][A-Za-z0-9_]{0,63}|<module>)$')


def _attrs(value):
    return vars(value) if value is not None and hasattr(value,'__dict__') else {}


def _count(value):
    return value if type(value) is int and 0<=value<2**63 else None


def _alive(owner):
    thread=_attrs(owner).get('thread')
    return thread.is_alive() if isinstance(thread,threading.Thread) else None


def _failure(value,budget):
    result={'present':value is not None,'exception_type':None,'frames':[]}
    if isinstance(value,BaseException):
        name=type(value).__name__
        result['exception_type']=name if name in _EXCEPTIONS else None
        return result
    if type(value) is not str:return result
    result['present']=bool(value)
    # 메시지·source code·locals는 해석/복사하지 않는다. 최초 예외 header 뒤는 읽지 않는다.
    for line in value[:4096].splitlines()[:64]:
        if line=='Traceback (most recent call last):':continue
        matched=_FRAME.fullmatch(line)
        if matched:
            path,number,function=matched.groups();module=path.replace('\\','/').rsplit('/',1)[-1]
            if module in _MODULES and budget[0] and 0<int(number)<=1000000:
                result['frames'].append(dict(module=module,function=function,line=int(number)));budget[0]-=1
        elif line and not line[0].isspace():
            token=line.split(':',1)[0].rsplit('.',1)[-1]
            result['exception_type']=token if token in _EXCEPTIONS else None
            break
    return result


def _queue_depth(value):
    if not isinstance(value,queue.Queue):return None
    if not value.mutex.acquire(False):return None
    try:
        return len(value.queue) if type(value.queue) in (deque,list) else None
    finally:value.mutex.release()


def _final_snapshot(ipc,operator_ipc,keyboard_ipc,runner,sink):
    """종료 중의 내부 관측만 구성한다. 추가 owner 호출·잠금 대기·상태 검산은 없다."""
    servers={};budget=[4]
    for name,owner in (('provider',ipc),('operator',operator_ipc),('keyboard',keyboard_ipc)):
        fields=_attrs(owner);stats=fields.get('stats');stats=stats if type(stats) is dict else {}
        servers[name]=dict(present=owner is not None,thread_alive=_alive(owner),
            counters={key:_count(stats.get(key)) for key in _IPC_COUNTERS},failure=_failure(fields.get('failure'),budget))
    runner_fields=_attrs(runner);actor=_attrs(runner_fields.get('operator'));lanes=actor.get('queues')
    depths=[_queue_depth(q) for q in lanes[:3]] if type(lanes) in (list,tuple) else []
    pending,waiting=actor.get('pending'),actor.get('_waiting');worker=_attrs(actor.get('worker'));busy=worker.get('_busy')
    operator=dict(present=bool(actor),queue_depths=depths,
        pending=len(pending) if type(pending) is dict else None,
        waiting=len(waiting) if type(waiting) in (list,tuple,deque) else None,
        job_present=actor.get('job') is not None,
        worker_busy=busy.is_set() if isinstance(busy,threading.Event) else None,
        worker_jobs_depth=_queue_depth(worker.get('_jobs')),worker_results_depth=_queue_depth(worker.get('_results')))
    cached=_attrs(sink).get('_device_view');cached=cached if type(cached) is dict else None
    device={'cached_view_available':cached is not None}
    if cached is not None:
        fault=cached.get('fault')
        device.update(cached_fault_present=fault is not None,cached_fault=fault if type(fault) is str and fault in _FAULTS else None,
            **{key:cached.get(key) if type(cached.get(key)) is bool else None for key in ('ready','clock_ready','report_is_latest')},
            counters={key:_count(cached.get(key)) for key in _DEVICE_COUNTERS})
    native=_attrs(_attrs(sink).get('device'))
    for key in ('fault','_sensor_blocked_reason'):
        value=native.get(key)
        device['native_'+key.lstrip('_')]=value if type(value) is str and value in _FAULTS else None
    ingress=dict(queue_depth=_queue_depth(runner_fields.get('_queue')),
                 dropped_queue_full=_count(runner_fields.get('dropped_queue_full')),
                 rejected_commands=_count(runner_fields.get('rejected_commands')))
    return dict(contract='gateway-final-1',stage='before_diagnostics_close',completion_proven=False,
                ipc=servers,operator=operator,ingress=ingress,device=device)

class GatewayDiagnostics(QueueHandler):
    """표준 QueueHandler의 사본을 기존 handler로 전달한다. 종료는 유한 대기다.

    Gateway 실행 진입점이 소유한다. 막힌 handler를 강제 종료하거나 worker를 재시작하지 않는다.
    snapshot의 손실/실패/잔존을 성공 drain과 구별한다. 일반 메시지는 4096문자에서
    자른 뒤 제어문자를 escape한다(최대 16384문자). 희귀 ERROR/stack은 자르지 않는다.
    """
    capacity=128
    def __init__(self,logger):
        super().__init__(queue.Queue(maxsize=self.capacity))
        self.logger=logger;self.previous=list(logger.handlers);self.propagate=logger.propagate
        targets=self.previous or list(logging.getLogger().handlers)
        self.targets=[]
        self.submitted=0;self.dropped=0;self.truncated=0;self.failed=0;self.first_failure=None
        self.closing=False;self._sentinel=object();self._admission=threading.Lock()
        self.first_sink_error=None;self.last_sink_error=None
        self.last_critical=None
        for target in targets:
            if type(target) is logging.StreamHandler:
                # 기본 StreamHandler.handleError의 stderr 재출력/stack 증폭을 피한다.
                # 원 handler의 설정/수명은 바꾸지 않고 같은 stream의 진단 사본만 쓴다.
                target=copy.copy(target);target.createLock();target.handleError=self._stream_failure
            self.targets.append(target)
        self.worker=threading.Thread(target=self._run,name='gateway-diagnostics',daemon=True)
        logger.handlers=[self];logger.propagate=False
        try:self.worker.start()
        except BaseException:
            self.closing=True;logger.handlers=self.previous;logger.propagate=self.propagate
            super().close();raise

    def _stream_failure(self,record):
        self._sink_failure(sys.exc_info()[1])

    def _sink_failure(self,error):
        self.failed+=1
        detail={'cause_type':type(error).__name__,'errno':getattr(error,'errno',None)}
        if self.first_failure is None:self.first_failure=detail['cause_type']
        if self.first_sink_error is None:self.first_sink_error=detail
        self.last_sink_error=detail

    def emit(self,record):
        if record.levelno>=logging.ERROR or record.exc_info:
            # 희귀 결함의 원 cause/stack은 요약·포화 폐기 대상으로 바꾸지 않는다.
            # 현 Gateway의 fatal 출력은 제어/소유 자원을 종료한 뒤 호출한다.
            self.last_critical=logging.Formatter().format(record)
            for target in self.targets:
                if record.levelno>=target.level:
                    try:target.handle(record)
                    except Exception as error:self._sink_failure(error)
            return
        # 포화 때 prepare/예외 문자열·stack 형식화부터 다시 만들지 않는다.
        with self._admission:
            if self.closing or self.queue.full():self.dropped+=1;return
        try:
            prepared=self.prepare(record)  # mutable args/예외 객체를 나중에 참조하지 않는 표준 사본
            text=prepared.msg
            if len(text)>4096:self.truncated+=1;text=text[:4096]
            prepared.msg=text.translate(_CONTROLS)
            with self._admission:
                if self.closing:self.dropped+=1;return
                try:self.queue.put_nowait(prepared);self.submitted+=1
                except queue.Full:self.dropped+=1
        except queue.Full:self.dropped+=1
        except Exception as error:
            self.failed+=1
            if self.first_failure is None:self.first_failure=type(error).__name__

    def _run(self):
        while True:
            record=self.queue.get()
            try:
                if record is self._sentinel:return
                for target in self.targets:
                    if record.levelno<target.level:continue
                    try:target.handle(record)
                    except Exception as error:self._sink_failure(error)
            finally:self.queue.task_done()

    def snapshot(self):
        return {'submitted':self.submitted,'dropped':self.dropped,'truncated':self.truncated,
                'failed':self.failed,'first_failure':self.first_failure,'pending':self.queue.qsize(),
                'first_sink_error':self.first_sink_error,'last_sink_error':self.last_sink_error,
                'worker_alive':self.worker.is_alive(),'closing':self.closing}

    def emit_final(self,*,ipc=None,operator_ipc=None,keyboard_ipc=None,runner=None,sink=None):
        """종료 INFO 한 건을 비차단 best-effort로 기존 큐에 넣는다. 전달 성공을 보증하지 않는다."""
        try:
            raw=json.dumps(_final_snapshot(ipc,operator_ipc,keyboard_ipc,runner,sink),
                           ensure_ascii=True,separators=(',',':'),allow_nan=False)
            if len(raw)>3584:
                raw='{"contract":"gateway-final-1","stage":"before_diagnostics_close","unavailable":true,"completion_proven":false}'
            record=logging.LogRecord(self.logger.name,logging.INFO,'',0,'gateway.final '+raw,(),None)
            # 일반 logger/handler lock이나 Queue.put_nowait의 mutex에서 기다리지 않는다.
            if not self._admission.acquire(False):return False
            try:
                if self.closing or not self.queue.mutex.acquire(False):return False
                try:
                    if len(self.queue.queue)>=self.capacity:return False
                    self.queue._put(record);self.queue.unfinished_tasks+=1
                    self.queue.not_empty.notify();self.submitted+=1
                finally:self.queue.mutex.release()
            finally:self._admission.release()
            return True
        except BaseException:
            return False  # 진단 오류는 원 cleanup/exit/최초 오류를 바꾸지 않는다.

    def close(self):
        if not getattr(self,'closing',True):
            with self._admission:
                if self.closing:return
                self.closing=True;self.logger.handlers=self.previous;self.logger.propagate=self.propagate
                try:self.queue.put_nowait(self._sentinel)
                except queue.Full:
                    # 종료 표식의 자리는 부가 기록 하나로 한정한다. 필수 저장은 이 큐에 없다.
                    try:self.queue.get_nowait();self.queue.task_done();self.dropped+=1
                    except queue.Empty:pass
                    self.queue.put_nowait(self._sentinel)
            self.worker.join(timeout=.25)
        super().close()
