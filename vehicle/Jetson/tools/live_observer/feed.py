"""PC HIL의 선택 관측 피드. 운행 owner에는 latest 참조 교환만 허용한다.

daemon 하나가 작은 진단 사본과 원 frame을 변환해 pc.json을 원자 교체한다. 파일
오류·관측 누락은 운행 결과가 아니며 close는 flush/join/실제 정지를 보증하지 않는다.
"""

import base64
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4


SCHEMA='pc-live-source-1'
MAX_BYTES=2*1024*1024
INTERVAL_S=.5
_COUNTS=('attempted','rendered','expired','quantization_future','replaced','taken')


class PcLiveFeed:
    """active1/pending latest1 frame만 보유한다. 선택 폴더당 producer 하나를 사용한다."""

    def __init__(self,directory,*,run_id=None):
        self.run_id=str(uuid4()) if run_id is None else run_id
        if type(self.run_id) is not str or not 0<len(self.run_id)<=128 or any(ord(c)<33 for c in self.run_id):
            raise ValueError('bounded explicit observer run id required')
        self._run_origin='DIAGNOSTIC_UUID' if run_id is None else 'EXPLICIT'
        self._root=Path(directory).absolute()
        self._temporary=self._root/('.pc-'+str(uuid4())+'.tmp')
        self._lock=threading.Lock();self._stop=threading.Event()
        self._state=('CONNECTING_JETSON','CONNECTING',None,None)
        self._revision=0
        self._binding=self._producer=self._plant=self._peer=self._frame=None
        self._encoded_key=None;self._media=[];self._dropped=0;self.failure=None
        self._write_failures=0;self._io_error=None;self._last_write_ok=False
        self._thread=threading.Thread(target=self._run,name='pc-live-observer',daemon=True)
        self._thread.start()

    def _offer(self,**values):
        if self._stop.is_set() or self.failure is not None:return False
        if not self._lock.acquire(blocking=False):
            self._dropped=min(2**63-1,self._dropped+1);return False
        retired=None
        try:
            for key,value in values.items():
                if key=='_frame':retired=self._frame
                setattr(self,key,value)
            self._revision+=1
        finally:
            self._lock.release()
            retired=None
        return True

    def offer_state(self,stage,state,*,error=None,stop_reason=None):
        return self._offer(_state=(stage,state,error,stop_reason))

    def bind(self,binding):
        return self._offer(_binding=binding)

    def attach(self,*,producer,plant=None,peer=None):
        return self._offer(_producer=producer,_plant=plant,_peer=peer)

    def offer_frame(self,frame,*,binding=None):
        """원 immutable frame 참조만 바꾼다. next_frame/JSON/파일·시계 읽기는 하지 않는다."""
        return self._offer(_frame=frame,**({} if binding is None else dict(_binding=binding)))

    def close(self):
        """관측 worker만 종료 요청한다. 운행 owner는 worker를 기다리지 않는다."""
        self._stop.set()

    def finish_terminal(self,timeout_s=.3):
        """CLI가 운행 owner 종료를 확인한 뒤만 쓴다. 총 대기는 최대0.5초이며 실패는 포기한다."""
        self.close()
        if (type(timeout_s) not in (int,float) or not math.isfinite(timeout_s)
                or not 0<=timeout_s<=.5):return False
        try:self._thread.join(timeout_s)
        except RuntimeError:return False
        return not self._thread.is_alive() and self.failure is None and self._last_write_ok

    def _document(self,sequence):
        with self._lock:
            state,binding,producer,plant,peer,frame=(self._state,self._binding,self._producer,
                self._plant,self._peer,self._frame)
            revision=self._revision
        # 각 원 객체의 짧은 snapshot lock은 여기서 끝난다. 변환·I/O는 그 밖에서 한다.
        producer=None if producer is None else producer.snapshot()
        peer=None if peer is None else peer.snapshot()
        if plant is not None:
            original_plant=plant
            plant=asdict(original_plant.snapshot())
            feedback=original_plant.feedback
            if feedback is not None:
                sample=feedback.last_sample
                plant['wheel_feedback']=dict(ticks=list(feedback.ticks),
                    last_sample=None if sample is None else asdict(sample))
        fields=None if binding is None else binding.fields
        if fields is not None and frame is not None:
            scope=frame.scope.fields
            if (scope['source_id'],scope['pc_clock_epoch'],scope['session_id'])!=(
                    fields['source_id'],fields['pc_clock_epoch'],fields['jetson_session']):
                raise ValueError('PC observer frame/source mismatch')
        if fields is None and frame is not None:
            scope=frame.scope.fields
            source_id,epoch,session=scope['source_id'],scope['pc_clock_epoch'],scope['session_id']
        else:
            source_id=None if fields is None else fields['source_id']
            epoch=None if fields is None else fields['pc_clock_epoch']
            session=None if fields is None else fields['jetson_session']
        key=None if frame is None else (frame.scope.raw_bytes,frame.sequence,frame.fingerprint)
        if key!=self._encoded_key:
            self._media=[] if frame is None else self._encode_frame(frame,epoch)
            self._encoded_key=key
        stage,status,error,reason=state
        if producer is not None:
            terminal=producer.get('session_result')
            if reason is None and terminal is not None:reason=terminal.get('reason')
            if error is None and producer.get('failed'):
                error=producer.get('failure');stage=status='FAILED'
            elif producer.get('session_done') and stage not in ('FAILED','ENDED'):
                stage=status='ENDED'
        stamp=None if frame is None else dict(domain='pc-monotonic',epoch=epoch,us=frame.rgb_pc_interval[0])
        return dict(schema=SCHEMA,run_id=self.run_id,run_id_origin=self._run_origin,
            session_id=session,sequence=sequence,source_id=source_id,source_epoch=epoch,
            source_sequence=None if frame is None else frame.sequence,source_stamp=stamp,
            captured_interval_us=None if frame is None else list(frame.rgb_pc_interval),
            stage=stage,state=status,error=error,stop_reason=reason,
            counters=dict.fromkeys(_COUNTS,0) if producer is None else producer['counts'],
            producer=producer,peer=peer,plant=plant,media=self._media,binding=fields,
            collected_stamp=dict(domain='pc-monotonic',epoch=self.run_id,us=time.monotonic_ns()//1000),
            observer_revision=revision,observer_dropped=self._dropped,
            write_failures=self._write_failures,io_error=self._io_error,
            source_kind='HIL_SYNTHETIC',authority=False,physical_qualified=False)

    @staticmethod
    def _encode_frame(frame,epoch):
        result=[];original=frame.fields
        for panel,encoding,width,height,data,interval in (
                ('rgb','NV12',640,360,frame.rgb,frame.rgb_pc_interval),
                ('depth','RAW16_LE',640,480,frame.depth,frame.depth_pc_interval)):
            required=width*height*(3 if panel=='rgb' else 4)//2
            if type(data) is not bytes or len(data)!=required:raise ValueError('original bounded PC media required')
            result.append(dict(panel=panel,encoding=encoding,width=width,height=height,
                data_base64=base64.b64encode(data).decode('ascii'),sha256=sha256(data).hexdigest(),
                source_sequence=frame.sequence,source_stamp=dict(domain='pc-monotonic',epoch=epoch,us=interval[0]),
                original_stamp=dict(scope=original['scope'],**original[panel],sequence=frame.sequence,
                    render_completed_pc_us=frame.render_completed_pc_us,expires_pc_us=frame.expires_pc_us),
                captured_interval_us=list(interval),render_completed_pc_us=frame.render_completed_pc_us,
                expires_pc_us=frame.expires_pc_us,depth_unit='mm' if panel=='depth' else None))
        return result

    def _write(self,document):
        raw=(json.dumps(document,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
        if len(raw)>MAX_BYTES:raise ValueError('PC diagnostic exceeds2MiB')
        self._root.mkdir(parents=True,exist_ok=True)
        with self._temporary.open('wb') as target:target.write(raw)
        os.replace(self._temporary,self._root/'pc.json')

    def _run(self):
        sequence=0
        try:
            while True:
                sequence+=1
                document=self._document(sequence)
                try:
                    self._write(document)
                    self._last_write_ok=True
                except OSError as error:
                    # reader가 일시적으로 replace를 막아도 그 관측만 버린다.
                    self._last_write_ok=False
                    self._write_failures=min(2**63-1,self._write_failures+1)
                    self._io_error=dict(type=type(error).__name__,errno=error.errno,
                        winerror=getattr(error,'winerror',None))
                    if self._stop.is_set():break
                    self._stop.wait(INTERVAL_S)
                    continue
                if self._stop.is_set():
                    if document['observer_revision']==self._revision:break
                    continue
                self._stop.wait(INTERVAL_S)
        except Exception as error:
            self.failure=type(error).__name__
        finally:
            with self._lock:
                self._frame=self._encoded_key=None
                self._binding=self._producer=self._plant=self._peer=None
                self._media=[]


def start_feed(directory,*,run_id=None):
    """명시 opt-in만 시작한다. 관측 설정/기동 실패를 주행 예외로 전파하지 않는다."""
    if directory is None:return None
    try:return PcLiveFeed(directory,run_id=run_id)
    except Exception:return None
