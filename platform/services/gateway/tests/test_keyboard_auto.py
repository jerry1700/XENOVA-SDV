"""실제 K codec/source/actor의 AUTO 입력 경계 회귀.

중앙 operation_status/submit port만 명시 typed fixture다. C preflight·Authority journal·
실물 evdev/RF/STOP 또는 실제 모터 출력의 수락 결과로 세지 않는다.
"""
from copy import deepcopy
from hashlib import sha256
import queue
from types import SimpleNamespace
from uuid import uuid4

import pytest

from xenova_gateway.authority_journal import canonical
from xenova_gateway import keyboard_contract as wire
from xenova_gateway.keyboard_actor import KeyboardActor
from xenova_gateway.keyboard_auto import departure_call,trigger_sha256
from xenova_gateway.keyboard_source import KeyboardSession,KeyState,ENTER,W,REARM,Frame
from xenova_gateway.operator_contract import fingerprint,request
from test_keyboard_source import (Clock,fields,profile_from_fields,packet,event,response,state_reply,attached,
    HOST,GATEWAY,OWNER,SID,EPOCH)


class CentralPortFixture:
    """실제 central actor와 구분한 API 관찰 leaf. READY/STOP 검증을 위장하지 않는다."""
    def __init__(self,clock):
        self.clock=clock;self.instance=GATEWAY;self.supported=True;self.ready=True;self.revision=1
        self.phase='WAIT';self.active=None;self.submitted=[];self.stops=0;self.hold=False;self.desired=None
        self.runner=SimpleNamespace(authority=SimpleNamespace(host=HOST,value={}),
            machine=SimpleNamespace(generation=1,active_vm=OWNER,active_app='autonomous',mode=SimpleNamespace(value='AUTO_READY')))
        self.sources=SimpleNamespace(sid=SID,epoch=EPOCH,grant=1,sequence=1,state='ARMED_NEUTRAL',
            kind='NEUTRAL_ONLY',source=None,keys=[],armed=False,blocked=False)
    @property
    def now(self):return self.clock()
    def operation_status(self):
        return dict(contract='operation-status-1',supported=self.supported,host_boot_id=HOST,gateway_instance_id=GATEWAY,
            generation=1,pi_now_us=str(self.now),binding=dict(service_epoch=GATEWAY,operation_revision='1',
                departure_nonce='00000000-0000-0000-0000-000000000000',permission_revision='1',parking_enabled=False),
            vehicle_phase=self.phase,ready_revision=str(self.revision),auto_ready=self.ready,
            blocked_reason=None if self.ready else 'PREFLIGHT_UNAVAILABLE',valid_until_pi_us=str(self.now+100000),
            active_request_id=self.active,countdown_until_pi_us='0',desired_mode=self.desired or ('AUTO' if self.active else 'NONE'))
    def submit_auto_depart(self,call,**origin):
        request(call);future=queue.Queue(maxsize=1);self.submitted.append((deepcopy(call),origin,future));return future
    def _stop_driver(self):self.stops+=1
    def stationary(self):return True


class AutoRig:
    """원 input_event→K raw→실제 actor→중앙 typed port. OS socket/clock 시험은 별도다."""
    def __init__(self):
        self.clock=Clock();self.operator=CentralPortFixture(self.clock);self.actor=KeyboardActor(self.operator)
        self.profile=profile_from_fields(fields());self.actor.profile_loader=lambda alias:self.profile
        self.peer=object();self.source=KeyboardSession(self.profile,dict(host_boot_id=HOST,gateway_instance_id=GATEWAY,
            generation=1,owner=OWNER,mode='AUTO_READY'),self.clock,auto_enter=True)
        self.exchange()
        assert self.source.observer and self.source.binding is None
    def drain(self):
        while True:
            item=self.actor.take_outgoing()
            if item is None:break
            assert item[0] is self.peer
            self.source.receive(canonical(item[1]),self.clock())
    def exchange(self):
        original=self.source.outgoing(self.clock());assert original is not None
        self.source.sent(original,self.clock());future=self.actor.submit(wire.decode(original.raw),self.peer)
        self.actor.tick();answer=future.get_nowait();wire.validate(answer)
        self.source.receive(canonical(answer),self.clock());self.drain();return original,answer
    def frame(self,*events):
        if events:packet(self.source.state,*events,observed=self.clock())
        frame=self.source.state.snapshot(self.clock(),self.clock()+1);self.source.offer(frame)
        self.clock.value+=2
        sent,reply=self.exchange();assert sent.kind=='OBSERVE'
        self.clock.value+=10000
        return frame,reply
    def enter(self):
        self.frame();self.frame()
        self.frame(event(28,1,self.clock()))
        original,reply=self.exchange();assert original.kind=='AUTO_ENTER'
        return original,reply


def test_actual_observer_enter_reaches_typed_operator_call_with_original_hash_key_and_e():
    rig=AutoRig();original,reply=rig.enter();op=rig.operator
    assert reply['kind']=='AUTO_RESULT' and reply['payload']['phase']=='SUBMITTED' and not reply['payload']['terminal']
    assert rig.source.ordinary is None and rig.source.auto_trigger is original and rig.source.binding is None
    assert len(op.submitted)==1 and op.sources.kind=='NEUTRAL_ONLY' and op.stops==0
    call,origin,_=op.submitted[0];raw=wire.decode(original.raw);p=raw['payload']
    assert call==departure_call(raw,trigger_sha256(raw))
    assert call['request_id']==p['operator_request_id'] and call['deadline_pi_ms']==int(p['last_event_pi_us'])//1000+10000
    assert origin['trigger_sha256']==call['args']['trigger_sha256']
    assert origin['valid_until_pi_us']==original.expires==original.frame.t1+150000
    assert origin['current']() and call['args']['ready_revision']=='1'
    assert fingerprint(call)==sha256(canonical(dict(domain='operator.v2',request=call))).hexdigest()


def test_pending_enter_keeps_observing_without_extending_original_event_e_or_request():
    rig=AutoRig();original,_=rig.enter();call,origin,future=rig.operator.submitted[0]
    rig.operator.phase='COUNTDOWN';rig.operator.active=call['request_id']
    for _ in range(20):rig.frame()
    assert rig.clock()>original.expires and origin['current']()
    assert rig.source.auto_trigger is original and len(rig.operator.submitted)==1
    assert origin['valid_until_pi_us']==original.expires
    future.put_nowait(dict(version=2,ok=True,data=dict(terminal=False,phase='COUNTDOWN',result='NONE')))
    rig.actor.tick();rig.drain()
    assert rig.source.auto_result['operator_request_id']==call['request_id']
    assert rig.source.auto_result['phase']=='COUNTDOWN' and rig.source.auto_trigger is original


def test_early_enter_is_consumed_and_later_ready_or_repeat_never_reserves_it():
    rig=AutoRig();rig.operator.ready=False
    rig.frame();rig.frame();rig.frame(event(28,1,rig.clock()))
    assert rig.source.ordinary is None and rig.source.auto_trigger is None and not rig.operator.submitted
    rig.operator.ready=True;rig.operator.revision+=1
    rig.frame(event(28,2,rig.clock()))
    for _ in range(3):rig.frame()
    assert not rig.operator.submitted and rig.source.state.auto_edge is None
    rig.frame(event(28,0,rig.clock()));rig.frame()
    rig.frame(event(28,1,rig.clock()));original,_=rig.exchange()
    assert original.kind=='AUTO_ENTER' and len(rig.operator.submitted)==1


def test_source_readiness_is_frozen_at_down_and_expired_notice_does_not_arm_auto():
    rig=AutoRig();rig.frame();rig.frame()
    original=dict(rig.source.state.auto_context)
    packet(rig.source.state,event(28,1,rig.clock()),observed=rig.clock())
    assert rig.source.state.auto_edge[1]['ready_revision']==original['ready_revision']
    rig.source.state.auto_context={**original,'ready_revision':'999'}
    assert rig.source.state.auto_edge[1]['ready_revision']==original['ready_revision']
    state=KeyState();state.auto_enabled=state.observer=True;state.neutral_samples=state.auto_neutral_samples=2
    state.auto_context={**original,'valid_until_pi_us':str(rig.clock()-1)}
    packet(state,event(28,1,rig.clock()),observed=rig.clock())
    assert state.auto_edge==(1,None) and state.arm_event is None


@pytest.mark.parametrize('change',('new_key','short_tap','source_loss','input_expiry'))
def test_countdown_guard_observes_new_key_short_tap_source_loss_and_expiry(change):
    rig=AutoRig();_,_=rig.enter();_,origin,_=rig.operator.submitted[0]
    rig.frame(event(28,0,rig.clock()))
    assert origin['current']()
    if change=='new_key':rig.frame(event(17,1,rig.clock()))
    elif change=='short_tap':
        stamp=rig.clock();rig.frame(event(17,1,stamp),event(17,0,stamp))
    elif change=='source_loss':rig.actor.lost(rig.peer);rig.actor.tick()
    else:rig.clock.value+=150001
    assert not origin['current']() and len(rig.operator.submitted)==1


def test_actor_early_moving_enter_does_not_mutate_existing_manual_sid_or_arm():
    rig=AutoRig();s=rig.actor.scope;r=rig.operator.sources
    s['attached']=True;r.kind='HELD';r.source=s['source'];r.armed=True;r.blocked=False;r.keys=['up']
    rig.operator.runner.machine.mode.value='MANUAL'
    before=(r.sid,r.epoch,r.grant,r.armed,r.blocked,list(r.keys))
    now=rig.clock();p=dict(device_instance=rig.source.device,link_epoch=rig.source.link,source_sample_seq='1',
        key_event_seq='2',t1_pi_us=str(now),t2_pi_us=str(now),key_mask=W|ENTER,last_event_pi_us=str(now))
    # This direct typed frame observation has no all-up/Enter candidate; it is
    # deliberately below the existing MANUAL FRAME authority path.
    rig.actor.auto.observed(p,manual=True)
    packet=rig.source._packet('AUTO_ENTER',dict(**p,enter_event_seq='2',operation_revision='1',permission_revision='1',
        ready_revision='1',generation=1,expected_owner=OWNER,operator_request_id=f'g1:{HOST}:{now//1000}:{uuid4()}'),
        issued=now,expires=now+150000)
    answer=rig.actor.auto.enter(wire.decode(packet.raw),rig.peer)
    assert not answer['payload']['accepted'] and answer['payload']['result']=='NEUTRAL_REQUIRED'
    assert before==(r.sid,r.epoch,r.grant,r.armed,r.blocked,r.keys) and not rig.operator.submitted


def test_manual_arm_and_auto_depart_are_disjoint_and_v1_packet_remains_v1():
    client,clock=attached();original_binding=client.binding
    client.auto_enabled=client.state.auto_enabled=True
    client.state.auto_context=dict(generation=1,owner=OWNER,operation_revision='1',permission_revision='1',
        ready_revision='1',auto_ready=True,valid_until_pi_us=str(clock()+100000),vehicle_phase='ACTIVE',
        mode='MANUAL',desired_mode='MANUAL',manual_attach_available=True)
    client.state.neutral_samples=2
    packet(client.state,event(28,1,clock()),observed=clock())
    assert client.state.arm_event is not None and client.state.auto_edge is None
    client.state.arm_accepted();packet(client.state,event(28,0,clock()),observed=clock())
    client.state.frame_accepted(client.state.snapshot(clock(),clock()))
    client.state.frame_accepted(client.state.snapshot(clock(),clock()))
    client.state.neutral_samples=2
    packet(client.state,event(28,1,clock()),observed=clock())
    assert client.state.arm_event is None and client.state.auto_edge is not None
    assert client.binding==original_binding
    client.state.auto_enabled=False;client.state.auto_edge=None
    client.offer(Frame(1,client.state.key_event,clock(),clock(),ENTER,clock()))
    old=client.outgoing(clock());assert old.kind=='FRAME' and wire.decode(old.raw)['version']==1


def test_actor_unsupported_profile_rejects_negotiation_without_manual_authority():
    clock=Clock();op=CentralPortFixture(clock);op.supported=False;actor=KeyboardActor(op)
    actor.profile_loader=lambda alias:profile_from_fields(fields())
    source=KeyboardSession(profile_from_fields(fields()),dict(host_boot_id=HOST,gateway_instance_id=GATEWAY,
        generation=1,owner=OWNER,mode='AUTO_READY'),clock,auto_enter=True)
    pending=source.outgoing(clock());future=actor.submit(wire.decode(pending.raw),object());actor.tick()
    answer=future.get_nowait();source.receive(canonical(answer),clock())
    assert answer['payload']['reason']=='UNSUPPORTED_PROFILE' and source.done
    assert op.sources.kind=='NEUTRAL_ONLY' and not op.submitted and op.stops==0


def test_explicit_manual_return_requests_original_attach_and_requires_new_arm():
    rig=AutoRig();original,_=rig.enter();call,_,_=rig.operator.submitted[0]
    rig.operator.phase='COUNTDOWN';rig.operator.active=call['request_id'];rig.frame()
    assert rig.source.observer and rig.source.auto_progress
    rig.operator.runner.machine.mode.value='MANUAL';rig.operator.phase='ACTIVE'
    rig.operator.desired='MANUAL';rig.operator.ready=False
    rig.operator.active=f'g1:{HOST}:{rig.clock()//1000}:{uuid4()}'
    notice=rig.actor.auto.ready(rig.actor.scope['last']);rig.source.receive(canonical(notice),rig.clock())
    attach=rig.source.outgoing(rig.clock())
    assert attach.kind=='ATTACH' and wire.decode(attach.raw)['version']==1
    assert not rig.source.observer and not rig.source.state.armed and rig.source.state.require_rearm
    assert rig.source.binding is None and attach is rig.source.ordinary


@pytest.mark.parametrize('field',('channel_epoch','source_instance','request_id','issued_pi_us','expires_pi_us'))
def test_trigger_hash_covers_original_envelope(field):
    rig=AutoRig();original,_=rig.enter();value=wire.decode(original.raw);changed=deepcopy(value)
    changed[field]=str(uuid4()) if field.endswith('epoch') or field in ('source_instance','request_id') else str(int(changed[field])+1)
    assert trigger_sha256(value)!=trigger_sha256(changed)


def test_contract_exact_v2_u63_and_max_packet_stays_under2048_depth6():
    rig=AutoRig();original,_=rig.enter();value=wire.decode(original.raw)
    maximum=deepcopy(value);p=maximum['payload'];large=2**63-150001
    maximum.update(sequence=str(2**63-1),issued_pi_us=str(large),expires_pi_us=str(large+150000))
    p.update(source_sample_seq=str(2**63-1),key_event_seq=str(2**63-1),enter_event_seq=str(2**63-1),
        t1_pi_us=str(large),t2_pi_us=str(large),last_event_pi_us=str(large),operation_revision=str(2**63-1),
        permission_revision=str(2**63-1),ready_revision=str(2**63-1),generation=2**32-1,
        operator_request_id=f'g1:{HOST}:{large//1000}:{uuid4()}')
    raw=canonical(maximum);assert len(raw)<=2048 and wire.decode(raw)==maximum
    for mutate in (lambda v:v.update(version=1),lambda v:v['payload'].update(extra=1),
            lambda v:v['payload'].update(ready_revision=str(2**63)),lambda v:v['payload'].update(key_mask=True),
            lambda v:v['payload'].update(operator_request_id=str(uuid4()))):
        bad=deepcopy(value);mutate(bad)
        with pytest.raises(ValueError):wire.decode(canonical(bad))


def test_readiness_and_result_max_packets_keep_bounded_original_codec():
    rig=AutoRig();original,_=rig.enter()
    ready=rig.actor.auto.ready(rig.actor.scope['last']);p=ready['payload']
    ready.update(sequence=str(2**63-1),issued_pi_us=str(2**63-2),expires_pi_us=str(2**63-1))
    for name in ('operation_revision','permission_revision','ready_revision','valid_until_pi_us'):
        p[name]=str(2**63-1)
    p.update(generation=2**32-1,blocked_reason='R'*64,reply_to_kind='HELLO_AUTO',reply_to_sequence=str(2**63-1),
        active_request_id=f'g1:{HOST}:{(2**63-1)//1000}:{uuid4()}')
    assert len(canonical(ready))<=2048 and wire.decode(canonical(ready))==ready
    value=rig.actor.auto._answer(wire.decode(original.raw),trigger_sha256(wire.decode(original.raw)),
        phase='P'*64,result='R'*64)
    assert len(canonical(value))<=2048 and wire.decode(canonical(value))==value


def test_readiness_rejects_unpaired_reply_and_unknown_domains():
    rig=AutoRig();ready=rig.actor.auto.ready(rig.actor.scope['last'])
    ready['payload'].update(reply_to_kind='HELLO_AUTO',reply_to_sequence='1')
    assert wire.decode(canonical(ready))==ready
    for name,value in (('reply_to_kind',None),('reply_to_sequence',None),
            ('reply_to_kind','AUTO_ENTER'),('vehicle_phase','READY'),
            ('mode','READY'),('desired_mode','READY')):
        bad=deepcopy(ready);bad['payload'][name]=value
        with pytest.raises(ValueError,match='keyboard AUTO readiness domain'):
            wire.decode(canonical(bad))


def test_informational_ready_backpressure_never_fences_existing_manual_sid():
    rig=AutoRig();r=rig.operator.sources;r.kind='HELD';r.armed=True;r.keys=['up']
    before=(r.sid,r.epoch,r.grant,r.armed,list(r.keys))
    for _ in range(4):rig.actor.outgoing.put_nowait((rig.peer,{}))
    rig.clock.value+=50001;rig.actor.auto.tick()
    assert rig.actor.outgoing.qsize()==4 and rig.operator.stops==0
    assert before==(r.sid,r.epoch,r.grant,r.armed,r.keys)


def test_actor_replay_cannot_submit_same_enter_after_new_readiness():
    rig=AutoRig();original,_=rig.enter();rig.frame(event(28,0,rig.clock()))
    rig.operator.revision+=1
    future=rig.actor.submit(wire.decode(original.raw),rig.peer);rig.actor.tick()
    response=future.get_nowait()
    assert response['kind']=='STATE' and not response['payload']['accepted']
    assert len(rig.operator.submitted)==1


def test_actual_evdev_early_enter_during_manual_motion_has_no_arm_or_auto_or_fence():
    state=KeyState();state.auto_enabled=True;state.armed=True;state.blocked=state.require_rearm=False
    packet(state,event(17,1,1000000),observed=1000000)
    packet(state,event(28,1,1000010),observed=1000010)
    assert state.mask==W|ENTER and state.armed and not state.blocked
    assert state.arm_event is None and state.auto_edge is None and state.fence_mask==0


def test_observer_input_fault_closes_before_staged_enter_can_be_sent():
    rig=AutoRig();rig.frame();rig.frame();rig.frame(event(28,1,rig.clock()))
    assert rig.source.ordinary.kind=='AUTO_ENTER' and not rig.operator.submitted
    rig.source.state.fence(REARM,'SYN_DROPPED')
    assert rig.source.outgoing(rig.clock()) is None and rig.source.done
    rig.actor.lost(rig.peer);rig.actor.tick()
    assert rig.actor.scope is None and not rig.operator.submitted


def test_source_fence_invalidates_auto_intent_without_new_expiry_or_request():
    rig=AutoRig();original,_=rig.enter();call,origin,_=rig.operator.submitted[0]
    assert origin['current']()
    rig.actor.auto.invalidate()
    assert not origin['current']() and origin['valid_until_pi_us']==original.expires
    assert rig.operator.submitted[0][0]==call and len(rig.operator.submitted)==1
