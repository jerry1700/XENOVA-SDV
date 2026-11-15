"""협상된 AUTO observer와 원 Enter를 중앙 operator actor에 연결한다.

입력 관측은 RAM의 한 slot이며 추진 SID/권한을 발급하지 않는다. 원 trigger E는
접수까지만 사용하고 countdown은 중앙의 원 request E와 현재 observer를 검사한다.
"""
from copy import deepcopy
from hashlib import sha256
import queue

from .authority_journal import canonical
from .keyboard_contract import AUTO_FRAME,JETSON_RETURN_PENDING
from .operator_contract import OperatorError


def trigger_sha256(packet):
    """원 v2 packet 전체의 canonical domain hash. operator call은 hash 범위 밖이다."""
    return sha256(canonical(dict(domain='keyboard.auto-enter.v2',packet=packet))).hexdigest()


def departure_call(packet, digest):
    p=packet['payload'];issued=int(p['last_event_pi_us'])//1000
    return dict(version=2,action='drive.depart',request_id=p['operator_request_id'],
        host_boot_id=packet['host_boot_id'],gateway_instance_id=packet['gateway_instance_id'],
        expected_generation=p['generation'],expected_owner=p['expected_owner'],deadline_pi_ms=issued+10000,
        args=dict(mode='AUTO',expected_operation_revision=p['operation_revision'],
            expected_permission_revision=p['permission_revision'],ready_revision=p['ready_revision'],trigger_sha256=digest))


class AutoKeyboardBoundary:
    """기존 KeyboardActor 회차 안에서만 갱신한다. pending1/result1; 별도 writer/thread 없음."""
    def __init__(self,actor):
        self.actor=actor;self.pending=None;self.guard=None;self.last_result=None;self.notice=None

    @property
    def operator(self):return self.actor.operator

    def _jetson_return(self):
        operations=getattr(self.operator,'operations',None)
        return operations if getattr(operations,'jetson_return',None) is not None else None

    def status(self):
        getter=getattr(self.operator,'operation_status',None)
        if getter is None:raise OperatorError('UNSUPPORTED_PROFILE')
        value=getter()
        if (value.get('contract')!='operation-status-1' or not value.get('supported')
                or value.get('host_boot_id')!=self.operator.runner.authority.host
                or value.get('gateway_instance_id')!=self.operator.instance):raise OperatorError('UNSUPPORTED_PROFILE')
        if value.get('binding') is None:raise OperatorError('NOT_READY')
        return value

    def hello(self,call,peer,profile):
        self.status()
        if not profile.continuous_allowed:raise OperatorError('UNVERIFIED_RADIO')
        p=call['payload'];m=self.operator.runner.machine
        self.actor.scope=dict(peer=peer,source=call['source_instance'],channel=call['channel_epoch'],profile=profile,
            attached=False,generation=m.generation,last=call,auto=True,device=p['device_instance'],link=p['link_epoch'],
            frame=None,all_up=0,candidate=None,enter_hwm=0)
        self.actor.rx=int(call['sequence'])
        return self.ready(call,reply=True)

    def ready(self,call,*,reply=False):
        v=self.status();s=self.actor.scope;b=v['binding'];m=self.operator.runner.machine
        identity=(b['operation_revision'],b['permission_revision'],v['ready_revision'],m.generation,m.active_vm)
        recovering=self._jetson_return() is not None
        auto_ready=v['auto_ready'] and not recovering
        if auto_ready and self.operator.now<int(v['valid_until_pi_us']):
            if s.get('ready_identity')!=identity:
                s['ready_identity']=identity;s['ready_since']=self.operator.now
        else:s['ready_identity']=None;s['ready_since']=None
        p=dict(device_instance=s['device'],link_epoch=s['link'],generation=m.generation,
            owner=None if m.mode.value=='SAFE_STOP' else m.active_vm,mode=m.mode.value,
            operation_revision=b['operation_revision'],permission_revision=b['permission_revision'],
            parking_enabled=b['parking_enabled'],ready_revision=v['ready_revision'],auto_ready=auto_ready,
            vehicle_phase=v['vehicle_phase'],blocked_reason=JETSON_RETURN_PENDING if recovering else v['blocked_reason'],
            valid_until_pi_us=v['valid_until_pi_us'],
            active_request_id=v['active_request_id'],desired_mode=v['desired_mode'],supported=True,
            manual_attach_available=bool(m.mode.value=='MANUAL' and not self.operator.hold),
            reply_to_kind=call['kind'] if reply else None,reply_to_sequence=call['sequence'] if reply else None)
        return self.actor._response(call,'AUTO_READY',p)

    def observe(self,call):
        s=self.actor.scope
        if not s.get('auto'):raise OperatorError('UNSUPPORTED_PROFILE')
        if s['attached'] and not self.replacing_manual():raise OperatorError('WRONG_MODE')
        self.observed(call['payload'],manual=False)
        s['attached']=False
        return self.ready(call,reply=True)

    def observed(self,p,*,manual):
        """원 frame 한 개를 보존한다. keydown 뒤의 READY가 옛 candidate를 바꾸지 않는다."""
        s=self.actor.scope
        if not s or not s.get('auto'):return
        now=self.operator.now;frame={name:p[name] for name in AUTO_FRAME};old=s['frame']
        if (p['device_instance'],p['link_epoch'])!=(s['device'],s['link']):raise OperatorError('SOURCE_MISMATCH')
        sample,event,t1,t2=map(lambda n:int(p[n]),('source_sample_seq','key_event_seq','t1_pi_us','t2_pi_us'))
        if old is not None and (sample<=int(old['source_sample_seq']) or event<int(old['key_event_seq'])):
            raise OperatorError('STALE_SAMPLE')
        if not 0<=t1<=t2<=now or t2-t1>20000 or now>=t1+150000:raise OperatorError('INPUT_EXPIRED')
        last=p['last_event_pi_us']
        if last is not None and (not 0<=int(last)<=t2 or (old is None or event>int(old['key_event_seq'])) and now-int(last)>100000):
            raise OperatorError('INPUT_BACKLOG')
        before=0 if old is None else old['key_mask'];mask=p['key_mask'];neutral=s['all_up']
        s['all_up']=min(2,neutral+1) if mask==0 else 0
        s['frame']=frame
        enter_edge=mask==64 and before==0 and old is not None and event==int(old['key_event_seq'])+1 and neutral>=2
        recovery=self._jetson_return()
        if recovery is not None:
            s['candidate']=None
            if enter_edge and last is not None:
                recovery.retain_manual_after_jetson(int(last))
            return
        if enter_edge:
            r=self.actor.registry
            ready=self.status()
            identity=(ready['binding']['operation_revision'],ready['binding']['permission_revision'],
                ready['ready_revision'],self.operator.runner.machine.generation,self.operator.runner.machine.active_vm)
            phase_ok=ready['vehicle_phase']=='WAIT' or manual and self.operator.runner.machine.mode.value=='MANUAL' and not self.operator.hold
            if ((not manual or r.armed and not r.blocked) and phase_ok and ready['auto_ready'] and s.get('ready_identity')==identity
                    and s.get('ready_since') is not None and last is not None
                    and s['ready_since']<=int(last)<int(ready['valid_until_pi_us'])):
                # readiness is copied at this original edge, never at a later retry.
                m=self.operator.runner.machine
                s['candidate']=dict(frame=deepcopy(frame),status=deepcopy(ready),
                    owner=None if m.mode.value=='SAFE_STOP' else m.active_vm)

    def _answer(self,call,digest,*,accepted=False,terminal=True,phase='REJECTED',result='NOT_READY'):
        return self.actor._response(call,'AUTO_RESULT',dict(operator_request_id=call['payload']['operator_request_id'],
            trigger_sha256=digest,accepted=accepted,terminal=terminal,phase=phase,result=result,
            reply_to_sequence=call['sequence']))

    def replacing_manual(self):
        s=self.actor.scope;g=self.guard
        if not s or not s.get('auto') or not g or g['scope'] is not s:return False
        try:v=self.status()
        except OperatorError:return False
        return bool(v['active_request_id']==g['call']['payload']['operator_request_id']
            and v['desired_mode']=='AUTO' and v['vehicle_phase'] in ('COUNTDOWN','APPLYING','ACTIVE'))

    def _current(self,g):
        s=self.actor.scope
        if s is not g['scope'] or 'closing' in s or g.get('cancelled'):return False
        frame=s.get('frame');now=self.operator.now;p=g['call']['payload']
        if frame is None or now>=int(frame['t1_pi_us'])+150000:return False
        if (s['device'],s['link'])!=(p['device_instance'],p['link_epoch']):return False
        # Only the original Enter release may advance the event HWM. A quick
        # down+up of another key still changes the count and cancels countdown.
        event=int(frame['key_event_seq']);origin=int(p['enter_event_seq'])
        return bool(event==origin and frame['key_mask']==64 or event==origin+1 and frame['key_mask']==0)

    def invalidate(self):
        """원 release/fault fence는 AUTO 의도도 줄인다. 새 SID나 입력 기한은 만들지 않는다."""
        s=self.actor.scope
        if not s or not s.get('auto'):return
        s['candidate']=None
        if self.guard is not None and self.guard['scope'] is s:self.guard['cancelled']=True

    def enter(self,call,peer):
        s=self.actor.scope;p=call['payload'];digest=trigger_sha256(call);raw=canonical(call)
        if not s.get('auto'):raise OperatorError('UNSUPPORTED_PROFILE')
        if self.last_result is not None and self.last_result[0]==raw:return self.last_result[1]
        event=int(p['enter_event_seq'])
        if event<=s['enter_hwm']:return self._answer(call,digest,result='STALE_SAMPLE')
        s['enter_hwm']=event  # Early input is consumed even when it is rejected.
        candidate=s.get('candidate');s['candidate']=None
        result='NOT_READY'
        if self.pending is not None:result='BUSY_NOT_ACCEPTED'
        elif candidate is None:result='NEUTRAL_REQUIRED'
        elif any(p[name]!=candidate['frame'][name] for name in AUTO_FRAME):result='SOURCE_MISMATCH'
        elif (int(call['issued_pi_us'])!=int(p['t1_pi_us'])
                or int(call['expires_pi_us'])!=int(p['t1_pi_us'])+150000):result='INPUT_EXPIRED'
        else:
            v=candidate['status'];b=v['binding']
            if (p['generation']!=v['generation'] or p['expected_owner']!=candidate['owner']
                    or p['operation_revision']!=b['operation_revision'] or p['permission_revision']!=b['permission_revision']
                    or p['ready_revision']!=v['ready_revision']):result='CONTEXT_CHANGED'
            else:
                g=dict(scope=s,call=deepcopy(call),peer=peer,digest=digest)
                if self._current(g):
                    try:
                        future=self.operator.submit_auto_depart(departure_call(call,digest),trigger_sha256=digest,
                            valid_until_pi_us=int(call['expires_pi_us']),current=lambda:self._current(g))
                    except (OperatorError,queue.Full) as error:result=getattr(error,'code','BUSY_NOT_ACCEPTED')
                    else:
                        self.guard=g;self.pending=dict(guard=g,future=future)
                        answer=self._answer(call,digest,accepted=True,terminal=False,phase='SUBMITTED',result='PENDING')
                        self.last_result=(raw,answer);return answer
                else:result='INPUT_EXPIRED'
        answer=self._answer(call,digest,result=result);self.last_result=(raw,answer);return answer

    def tick(self):
        if self.pending is not None:
            try:value=self.pending['future'].get_nowait()
            except queue.Empty:pass
            else:
                g=self.pending['guard'];self.pending=None
                if value.get('ok'):
                    row=value['data'];answer=self._answer(g['call'],g['digest'],accepted=True,
                        terminal=row['terminal'],phase=row['phase'],result=row['result'])
                else:answer=self._answer(g['call'],g['digest'],result=value.get('error',{}).get('code','UNCONFIRMED'))
                self.last_result=(canonical(g['call']),answer)
                self.actor._send_async(g['peer'],answer)
        s=self.actor.scope
        if not s or not s.get('auto') or self.operator.now-s.get('notice_at',0)<50000:return
        if self.actor.outgoing.full():return  # Informational refresh never fences a normal MANUAL SID.
        try:answer=self.ready(s['last'])
        except OperatorError:return
        s['notice_at']=self.operator.now
        try:self.actor.outgoing.put_nowait((s['peer'],answer))
        except queue.Full:pass
