"""실제 operator actor/journal/P4/CLI의 운영 경계. 물리·기하·앱 관측은 typed leaf다.

이 시험의 app 보고를 C planner 완료/장치 APPLIED로 계산하지 않는다.
"""
from copy import deepcopy
from collections import deque
import base64
import json
from hashlib import sha256
from pathlib import Path
import time
import threading
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest
from xenova_drive_world import schema,encode,decode
from xenova_gateway.drive_link import inbound
from xenova_gateway.guest_clock import SentClock
from xenova_gateway.operator_contract import request,fingerprint,OperatorError,capacity
from xenova_gateway.operation_owner import binding_record,validate_operation
from xenova_gateway.machine import Mode
from xenova_gateway.absolute_meter import AbsoluteWheelMeter
from xenova_gateway.scene_authority import COMMAND3,FEEDBACK3,MatchedStop
from xenova_gateway.frame import StopFrame
from test_operator_runtime import OperatorWorld
from test_verified import A,B,HOST
from b08_fixture import guest_now
from mapper_operation_fixture import install_mapper_fixture


def install_sequential_plant(w):
    """선언된 정지 plant의 새 표본만 meter에 넣는다. 겹친 window를 새 입력으로 재사용하지 않는다."""
    physical=w.sink;current=physical.vehicle
    config=SimpleNamespace(encoder_bits=64,metres_per_tick=.000001,metres_per_tick_upper=.000001,
        calibration=current.calibration,maximum_sample_age_us=200000)
    meter=AbsoluteWheelMeter(config,HOST);rows=deque(maxlen=128)
    previous=schema.VehicleSample();previous.CopyFrom(current)
    previous.sequence-=1;previous.source_sample-=1;previous.source_us-=20000;previous.pi_sample_us-=20000
    def record(value):
        rows.append(value);meter.record(pi_session=value.pi_session,device_session=value.device_session,
            odom_epoch=value.odom_epoch,sequence=value.sequence,source_sample=value.source_sample,
            source_us=value.source_us,left=value.left_ticks,right=value.right_ticks,
            pi_sample_us=value.pi_sample_us,clock_error_us=value.clock_error_us)
    record(previous);record(current);physical.odometry=SimpleNamespace(config=config,absolute=meter)
    original_write=physical.write
    def write(frame,at):
        result=original_write(frame,at)
        if isinstance(frame,StopFrame):physical.raw=None;physical.frame_at=int(at*1000000)
        return result
    physical.write=write
    def measure():
        at=int(w.w.t*1e6)
        last=physical.vehicle.pi_sample_us
        if at-last<1000:return
        # 독립 20ms 센서 producer를 모델링한다. 느린 host I/O가 센서 표본의 gap으로 바뀌지 않는다.
        stamps=list(range(last+20000,at+1,20000))
        if at-(stamps[-1] if stamps else last)>=1000:stamps.append(at)
        if len(stamps)>1024:meter.invalidate();physical.raw=None;return
        for stamp in stamps:
            value=schema.VehicleSample();value.CopyFrom(physical.vehicle)
            physical.sample+=1;value.sequence=value.source_sample=physical.sample
            value.source_us=value.pi_sample_us=stamp;physical.actuator_observation(value)
            physical.vehicle=value;record(value)
        at=physical.vehicle.pi_sample_us
        if not physical.sequence:return
        if at<getattr(physical,'frame_at',0):return
        frame=physical.frame
        command=COMMAND3.pack(UUID(physical.pi).bytes,UUID(physical.session).bytes,physical.sequence,bytes(16),0,0,0,0,
            frame.generation,frame.counter,2,0,0,0,1,0)
        feedback=FEEDBACK3.pack(UUID(physical.pi).bytes,UUID(physical.session).bytes,physical.sequence,
            frame.generation,frame.counter,2,2,value.source_sample,value.source_sample,at,4,0,0,1,bytes(16),0,0,0,0)
        # 측정 window만 줄인다. meter는 모든 accepted 표본을 따로 누적한다.
        window=[row for row in rows if at-row.pi_sample_us<=100000]
        physical.raw=MatchedStop(command,feedback,tuple(encode(row) for row in window))
    physical.measure=measure
    w.meter_view=lambda:dict(absolute_meter=meter.snapshot,
        clock_valid_until_pi_us=physical.vehicle.pi_sample_us+200000)


@pytest.fixture
def operating(tmp_path):
    w=OperatorWorld(tmp_path/'authority',operation_support=True)
    try:
        install_sequential_plant(w)
        w.r.drive_link.trust.odom_epoch=w.sink.vehicle.odom_epoch
        original_bind=w.w.bind
        def bind():
            original_bind();installed=deepcopy(w.w.cache._value)
            for row in installed['vms'].values():row['app_state_models']['autonomous']='stateful'
            w.w.cache.install(installed)
        w.w.bind=bind;w.w.bind()
        w.peer=(A,w.w.executions[A],w.w.ids[A]['autonomous'])
        w.o._stop_driver()
        # Authority bytes can already say WAIT while the original worker result
        # is still awaiting actor adoption. New fixture work uses the same guard
        # as real durable control transitions instead of racing that original ACK.
        w.pump(lambda:w.o.operations.current()['phase']=='WAIT' and w.o.job is None
            and not w.o.worker.busy and w.o.worker._results.empty())
        w.r.drive_link.tick()
        install_mapper_fixture(w)
        yield w
    finally:
        if hasattr(w,'mapper_fixture'):w.mapper_fixture.close()
        w.close()


def operation_call(w,action,**values):
    view=w.o.operation_status();binding=view['binding']
    if action=='drive.depart':
        args=dict(mode='AUTO',expected_operation_revision=binding['operation_revision'],
            expected_permission_revision=binding['permission_revision'],ready_revision=view['ready_revision'],trigger_sha256=None)
    else:args=dict(enabled=True,expected_permission_revision=binding['permission_revision'])
    args.update(values);return w.call(action,args)


def preflight(w,*,world_sequence=1,preserve_report=False,**changes):
    """원 P4는 실제 owner 왕복, 기하 합격 report 생산만 명시 typed C leaf다."""
    w.mapper_fixture.step()
    if not w.mapper_fixture.ready():w.pump(w.mapper_fixture.ready)
    w.r.drive_link.tick();confirm_operation_clock(w,preserve_report=preserve_report);w.r.drive_link.tick()
    trust=w.r.drive_link.trust;now=w.o.now;sequence=w.clock_sequence
    source=schema.EpisodeSourceContext(**{k:getattr(trust,k) for k in
        ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch','clock_epoch','configuration','calibration')},
        primitive=bytes.fromhex(trust.primitive),reference_id='road:lab',reference_revision=1,
        world_sequence=world_sequence,observation_sequence=world_sequence)
    interval=w.o.clocks.interval(w.peer,sequence,guest_now(w),now,w.w.m.generation)
    assert interval is not None
    report=schema.AutoInputPreflight(major=1,source=source,vehicle_sequence=w.sink.vehicle.sequence,
        control_generation=w.w.m.generation,checked_guest_us=guest_now(w),
        valid_until_pi_us=min(now+70000,interval[0]+100000),clock_probe_sequence=sequence,
        clock_reply_sha256=sha256(w.clock_raw).digest())
    for key,value in changes.items():setattr(report,key,value)
    probe=schema.ClockProbe(vm=w.peer[0],execution=w.peer[1],app_instance=w.peer[2],sequence=sequence+2,
        guest_send_us=guest_now(w),supported_state_features=63,auto_input_preflight=report)
    assert w.r.drive_link.receive(A,schema.DriveLink(major=2,probe=probe),w.w.t)
    w.clock_sequence=sequence+2
    with w.io.lock:prepared=w.io.pending.pop((inbound(A),'clock'))
    return report,prepared


def confirm_operation_clock(w,*,preserve_report=False):
    """같은 설치 profile63을 실제 P4 왕복 내내 선언한다. 의도하지 않은 downgrade를 끼우지 않는다."""
    now=w.o.now;peer=w.peer;sequence=getattr(w,'clock_sequence',5)+2
    guest=1000000+now-getattr(w,'guest_origin_pi',now)
    first=schema.ClockProbe(vm=peer[0],execution=peer[1],app_instance=peer[2],sequence=sequence,
        guest_send_us=guest-5000,supported_state_features=63)
    original=w.o.operations.reports.get(peer)
    carried=None if not preserve_report or original is None else original['body']
    if carried is not None:
        # A healthy clock rollover carries the still-current original statement,
        # including its old confirmed P4, sample and E. Clock-only absence is a
        # real loss event and is exercised separately, not hidden as renewal.
        assert carried.checked_guest_us<=first.guest_send_us and now<carried.valid_until_pi_us
        first.auto_input_preflight.CopyFrom(carried)
    assert w.r.drive_link.receive(A,schema.DriveLink(major=2,probe=first),w.w.t-.004)
    with w.io.lock:prepared=w.io.pending.pop((inbound(A),'clock'))
    raw=prepared.stamp(now);w.io.clock_completions.put_nowait(SentClock(peer,w.w.m.generation,raw));w.r.drive_link.tick()
    second=schema.ClockProbe(vm=peer[0],execution=peer[1],app_instance=peer[2],sequence=sequence+1,
        guest_send_us=guest,supported_state_features=63,receipt=schema.GuestClockReceipt(probe_sequence=sequence,
            reply_sha256=sha256(raw).digest(),guest_receive_us=guest-1000))
    if carried is not None:second.auto_input_preflight.CopyFrom(carried)
    assert w.r.drive_link.receive(A,schema.DriveLink(major=2,probe=second),w.w.t)
    w.clock_raw=raw;w.clock_sequence=sequence
    if not hasattr(w,'guest_origin_pi'):w.guest_origin_pi=now


def capability(w,features):
    w.clock_sequence+=1
    body=schema.ClockProbe(vm=w.peer[0],execution=w.peer[1],app_instance=w.peer[2],sequence=w.clock_sequence,
        guest_send_us=guest_now(w),supported_state_features=features)
    assert w.r.drive_link.receive(A,schema.DriveLink(major=2,probe=body),w.w.t)


def settle(w,call):
    reply=w.send(call);w.pump(lambda:w.o._known(call['request_id'])['terminal'])
    return reply,w.o._known(call['request_id'])


def test_service_boot_off_actual_wait_and_setting_never_depart(operating):
    w=operating;view=w.o.operation_status();generation=w.w.m.generation
    assert view['contract']=='operation-status-1' and view['vehicle_phase']=='WAIT'
    assert not view['binding']['parking_enabled'] and w.o.hold
    _,row=settle(w,operation_call(w,'parking.permission.set'))
    after=w.o.operation_status();revision=after['binding']['permission_revision']
    assert row['result']=='APPLIED' and after['binding']['parking_enabled']
    _,same=settle(w,operation_call(w,'parking.permission.set'))
    assert same['result']=='NO_CHANGE' and w.o.operation_status()['binding']['permission_revision']==revision
    assert w.w.m.generation==generation and w.o.missions.current() is None and w.o.hold
    assert len(w.o.status())==len(w.send(dict(version=2,action='status'))['data'])


def test_early_departure_is_terminal_and_not_reserved_for_later_ready(operating):
    w=operating;call=operation_call(w,'drive.depart');generation=w.w.m.generation
    first=w.send(call)
    assert not first['ok'] and first['error']['code']=='PREFLIGHT_UNAVAILABLE'
    assert w.o._known(call['request_id'])['terminal'] and w.o.operations.current()['phase']=='WAIT'
    preflight(w);assert w.o.operation_status()['auto_ready']
    assert w.send(call)==first and w.w.m.generation==generation
    assert w.o.missions.current() is None and not w.o._effects.get(call['request_id'])


def test_safe_stop_keeps_selected_owner_private_until_explicit_manual_setup_then_new_auto_request(operating,tmp_path):
    w=operating;selected=w.w.m.active_vm
    # 실제 Authority/control 전이, source MANUAL lifecycle만 기존 typed fixture다.
    w.r._emit(w.w.m._change(Mode.SAFE_STOP,selected,'cold-operation-test',w.w.t))
    w.tick();preflight(w);generation=w.w.m.generation
    assert w.o.status()['owner'] is None and w.w.m.active_vm==selected
    assert not w.o.operation_status()['auto_ready']
    rejected=w.send(operation_call(w,'drive.depart'))
    assert not rejected['ok'] and w.w.m.generation==generation
    manual=w.call('mode.manual');manual['expected_owner']=None
    # 원 SAFE_STOP CAS가 qualification 3s를 기다리는 동안, 실제 warm
    # machine의 manual_fresh가 먼저 disarmed MANUAL을 선택한다. 이는
    # 출력 허가가 아니며 원 요청을 새 generation으로 재포장하지 않는다.
    stale=w.apply(manual)
    assert stale['result']=='FAILED' and stale['terminal']
    assert w.w.m.mode is Mode.MANUAL and w.w.m.active_vm==selected
    assert w.w.m.generation>generation and not w.w.m.output_permitted and w.o.hold
    assert w.o._effects.get(manual['request_id'],{}).get('committed') is None
    w.tick();w.apps()
    assert w.w.m.delivered and w.w.m.phase=='APPLIED' and not w.w.m.output_permitted
    current=w.call('mode.manual')
    assert current['expected_owner']==selected and current['expected_generation']==w.w.m.generation
    assert w.apply(current)['result']=='APPLIED'
    assert w.w.m.active_vm==selected and w.o.operations.current()['desired_mode']=='MANUAL'
    before_neutral=w.o.operation_status()
    # Current MANUAL apply is not a new-generation STOP APPLIED observation.
    # Use the real terminal neutral action and fresh declared plant samples;
    # never relabel the previous generation's stationary proof.
    neutral=w.call('manual.neutral',dict(driver_session=w.o.sources.sid))
    neutral_response=w.send(neutral)
    (tmp_path/'safe-stop-manual-neutral.json').write_text(json.dumps(dict(request=neutral,response=neutral_response,
        before=before_neutral,after=w.o.operation_status(),source=w.o.sources.status(),hold=w.o.hold,
        generation=w.w.m.generation,mode=w.w.m.mode.value),sort_keys=True,indent=2))
    assert neutral_response['ok'],neutral_response
    w.pump(lambda:w.o.stationary())
    preflight(w);ready=w.o.operation_status()
    assert ready['auto_ready'],(before_neutral,ready)
    assert w.o.missions.current() is None  # READY/명시 수동 조작은 AUTO mission을 만들지 않는다.


@pytest.mark.parametrize('change',['sha','generation','vehicle','expiry'])
def test_current_preflight_is_original_source_clock_and_expiry_bound(operating,change):
    w=operating
    kw={'sha':dict(clock_reply_sha256=b'z'*32),'generation':dict(control_generation=w.w.m.generation+1),
        'vehicle':dict(vehicle_sequence=2**32),'expiry':dict(valid_until_pi_us=w.o.now+900000)}[change]
    _,prepared=preflight(w,**kw)
    reply=decode(prepared.stamp(w.o.now),schema.DriveLink).clock
    assert reply.guest_clock_valid_until_pi_us>w.o.now
    assert reply.HasField('operation_status') and not reply.operation_status.auto_ready
    assert not w.o.operation_status()['auto_ready']


def test_ready_revision_ignores_normal_sequence_but_changes_on_loss_and_permission(operating):
    w=operating;report,_=preflight(w);first=w.o.operation_status();assert first['auto_ready']
    previous_clock=report.clock_probe_sequence;original_report=encode(report)
    # The original report is still within its E and predates both new Probe
    # sends. Preserve it during actual P4 rollover, then produce a new report.
    w.w.t+=.008;w.tick();updated,_=preflight(w,world_sequence=2,preserve_report=True);second=w.o.operation_status()
    assert updated.clock_probe_sequence!=previous_clock and encode(report)==original_report
    assert second['ready_revision']==first['ready_revision']
    clock=w.o.clocks.lease(w.peer,updated.clock_probe_sequence,w.o.now,w.w.m.generation)
    assert clock is not None
    capability(w,63)  # Same supported peer, actual next Probe omits its input report.
    lost=w.o.operation_status()
    assert not lost['auto_ready'] and int(lost['ready_revision'])>int(second['ready_revision'])
    assert w.o.clocks.lease(w.peer,updated.clock_probe_sequence,w.o.now,w.w.m.generation)==clock
    _,row=settle(w,operation_call(w,'parking.permission.set'))
    assert row['result']=='APPLIED' and int(w.o.operation_status()['ready_revision'])>int(lost['ready_revision'])


@pytest.mark.parametrize('delay',[0,200000])
def test_io_expiry_withdraws_only_ready_and_preserves_numeric_clock_and_original_probe(operating,delay):
    w=operating;report,prepared=preflight(w);original=decode(bytes(prepared),schema.DriveLink).clock
    reply=decode(prepared.stamp(w.o.now+delay),schema.DriveLink).clock
    assert reply.probe==original.probe and reply.guest_clock_valid_until_pi_us==original.guest_clock_valid_until_pi_us
    assert reply.HasField('operation_status')==(delay==0)
    if not delay:assert reply.operation_status.auto_ready and reply.operation_status.valid_until_pi_us<=report.valid_until_pi_us


def test_countdown_uses_fresh_inputs_and_cancels_without_new_control_or_mission(operating):
    w=operating;preflight(w);call=operation_call(w,'drive.depart');generation=w.w.m.generation
    assert w.send(call)['ok']
    assert w.o.operations.current()['phase']=='COUNTDOWN' and w.o.hold
    assert not w.w.m.output_permitted and w.o.missions.current() is None
    w.o.operations.reports.clear()
    w.pump(lambda:w.o._known(call['request_id'])['terminal'])
    assert w.o._known(call['request_id'])['phase']=='FAILED' and w.w.m.generation==generation
    w.pump(lambda:w.o.operations.current()['phase']=='WAIT')
    preflight(w);assert w.o.operations.current()['departure'] is None and w.o.hold


def begin_auto_state(w):
    from b08_fixture import state,accept
    preflight(w);call=operation_call(w,'drive.depart');generation=w.w.m.generation
    assert w.send(call)['ok'] and w.o.missions.current() is None
    end=time.monotonic()+5;ordinal=0
    while w.w.m.generation==generation:
        assert time.monotonic()<end,'countdown/control journal did not finish'
        w.w.t+=.02;w.w.bind();w.sink.measure();w.r.drive_link.tick()
        if ordinal%3==0:preflight(w)
        ordinal+=1;w.o.tick();time.sleep(.001)
    assert w.o.operations.current()['phase']=='APPLYING' and w.o.missions.grant().admitted
    assert w.o.operations.last_state is None and not w.o._known(call['request_id'])['terminal']
    w.tick();w.r.drive_link.tick();body=state(w)
    body.mission.operation_binding.CopyFrom(w.o.missions.grant().operation_binding);body.state_features|=32
    assert accept(w,body)
    return call,body


def test_auto_departure_does_not_require_future_grant_but_does_require_full_state_and_new_calculation(operating):
    w=operating;call,body=begin_auto_state(w)
    command=dict(plan_revision=body.path_revision,phase_revision=body.maneuver_revision,calculation_sequence=body.calculation_sequence)
    assert not w.o.operations.command_allowed(command)
    command['calculation_sequence']+=1;assert w.o.operations.command_allowed(command)
    # 후보 검산은 actual command ingress/계산 완료가 아니다. 원 요청은 아직 완료되지 않는다.
    assert not w.o._known(call['request_id'])['terminal'] and w.o.operations.fresh_command is None


@pytest.mark.parametrize('features',[31,0])
def test_current_operation_capability_loss_fences_existing_state_and_command_until_new_operation(operating,features):
    w=operating;_,body=begin_auto_state(w);owner=w.o.operations
    command=dict(plan_revision=body.path_revision,phase_revision=body.maneuver_revision,calculation_sequence=body.calculation_sequence+1)
    proof=deepcopy(owner.last_state[0]);grant=encode(w.o.missions.grant());original=encode(body)
    assert owner.state_allowed(body) and owner.proof_current(proof) and owner.command_allowed(command)
    capability(w,features)
    assert not w.o.operation_status()['auto_ready'] and w.o.hold
    assert not owner.state_allowed(body) and not owner.proof_current(proof) and not owner.command_allowed(command)
    assert owner.last_state is None and owner.fresh_command is None
    capability(w,63)
    assert owner.supported(w.peer)
    assert not owner.state_allowed(body) and not owner.proof_current(proof) and not owner.command_allowed(command)
    assert encode(w.o.missions.grant())==grant and encode(body)==original
    # 복구 Probe는 새 grant/fullState/calculation이나 원 E 재발급이 아니다.


def test_auto_capability_loss_does_not_fence_existing_manual_registry(operating):
    w=operating;preflight(w);w.apply(operation_call(w,'drive.depart',mode='MANUAL'))
    source=w.o.sources
    before=(source.sid,source.epoch,source.grant,source.action,source.sequence,source.expires,
        source.valid,source.blocked,source.armed,tuple(source.keys),w.o.fence_revision,len(w.sink.frames))
    capability(w,31)
    assert not w.o.hold and w.w.m.mode is Mode.MANUAL
    assert before==(source.sid,source.epoch,source.grant,source.action,source.sequence,source.expires,
        source.valid,source.blocked,source.armed,tuple(source.keys),w.o.fence_revision,len(w.sink.frames))


def test_external_digest_cannot_impersonate_keyboard_enter(operating):
    w=operating;preflight(w);call=operation_call(w,'drive.depart',trigger_sha256='ab'*32)
    result=w.send(call)
    assert result['error']['code']=='UNAUTHENTICATED_TRIGGER' and w.o.operations.current()['phase']=='WAIT'


def test_actual_keyboard_packet_reaches_central_journal_and_source_loss_cancels_original_intent(operating):
    from xenova_gateway import keyboard_contract as wire
    from xenova_gateway.authority_journal import canonical
    from xenova_gateway.keyboard_source import KeyboardSession
    from xenova_gateway.keyboard_auto import departure_call,trigger_sha256
    from test_keyboard_source import fields,profile_from_fields,packet,event
    w=operating;preflight(w);profile=profile_from_fields(fields());peer=object()
    w.o.keyboard.profile_loader=lambda alias:profile
    source=KeyboardSession(profile,w.o.status(),lambda:w.o.now,auto_enter=True)
    def exchange():
        pending=source.outgoing(w.o.now);assert pending is not None
        source.sent(pending,w.o.now);raw=wire.decode(pending.raw)
        result=w.o.keyboard.submit(raw,peer);w.o.keyboard.tick()
        source.receive(canonical(result.get_nowait()),w.o.now)
        while (outgoing:=w.o.keyboard.take_outgoing()) is not None:source.receive(canonical(outgoing[1]),w.o.now)
        return pending,raw
    def frame(enter=False):
        w.w.t+=.01;w.w.bind();w.sink.measure();w.r.drive_link.tick()
        if enter:packet(source.state,event(28,1,w.o.now),observed=w.o.now)
        source.offer(source.state.snapshot(w.o.now,w.o.now+1));w.w.t+=.000002
        pending,_=exchange();assert pending.kind=='OBSERVE'
    exchange();assert source.observer and source.binding is None
    frame();frame();frame(True);pending,raw=exchange();assert pending.kind=='AUTO_ENTER'
    call=departure_call(raw,trigger_sha256(raw));key=call['request_id']
    assert w.o._known(key)['request']==call and w.o.hold and not w.w.m.output_permitted
    w.pump(lambda:w.o.operations.current()['phase']=='COUNTDOWN')
    assert w.o._known(key)['fingerprint']==fingerprint(call)
    w.o.keyboard.lost(peer)
    w.pump(lambda:w.o._known(key)['terminal'])
    assert w.o._known(key)['phase']=='FAILED'
    w.pump(lambda:w.o.operations.current()['phase']=='WAIT')
    assert w.o.hold and w.o.operations.current()['departure'] is None


def test_manual_departure_uses_actual_control_apply_and_preserves_permission(operating):
    w=operating;settle(w,operation_call(w,'parking.permission.set'));permission=w.o.operations.current()['permission_revision']
    call=operation_call(w,'drive.depart',mode='MANUAL');generation=w.w.m.generation
    row=w.apply(call)
    assert row['result']=='APPLIED' and w.w.m.generation==generation+1
    assert w.o.operations.current()['phase']=='ACTIVE' and w.o.operations.current()['permission_revision']==permission
    assert w.o.sources.kind=='PULSE' and not w.o.sources.keys
    assert w.o.operations.current()['parking_attempt'] is None


@pytest.mark.parametrize('expired',[False,True])
def test_mapper_child_preserves_original_gate_and_keeps_numeric_probe_after_gate_expiry(operating,expired):
    from xenova_gateway.mapper_uplink import operation_plan,parse_operation_plan,build_operation_probe
    from xenova_drive_world.mapper_clock import ClockPolicy,MapperClockOwner
    w=operating;w.apply(operation_call(w,'drive.depart',mode='MANUAL'))
    gate=w.o.operations.mapper_gate();assert gate is not None and gate.manual_local_admitted
    trust=w.r.drive_link.trust
    policy=ClockPolicy(trust.vehicle,'oak-1',trust.configuration,trust.calibration,trust.primitive,2,10,5000,1000000,200000,1000000)
    owner=MapperClockOwner(policy,HOST)
    vehicle=schema.VehicleSample(major=1,**{**w.view(),'gateway_instance':w.o.instance})
    owner.set_vehicle(encode(vehicle),w.o.now);plan=owner.plan(w.o.now,str(uuid4()));assert plan is not None
    raw=operation_plan(plan,gate)
    restored,original=parse_operation_plan(raw,request_id=plan.request_id,issued_us=plan.issued_us,expires_us=plan.expires_us)
    p1=gate.valid_until_pi_us+1 if expired else w.o.now+1;assert p1<plan.expires_us
    wire=build_operation_probe(restored,original,p1);probe=decode(wire,schema.MapperClockProbe)
    assert probe.pi_send_us==p1 and probe.HasField('operation_gate') is not expired
    if not expired:assert encode(probe.operation_gate)==encode(gate)


def test_operation_cap_and_old_canonical_fingerprint_are_not_widened(operating):
    w=operating;old=w.call('mode.manual')
    from xenova_gateway.authority_journal import canonical
    assert fingerprint(old)==sha256(canonical({'domain':'operator.v2','request':old})).hexdigest()
    request(old);value=deepcopy(w.r.authority.value);assert capacity(value)
    value['current_operation']['result']='x'*6145
    assert not capacity(value)
    with pytest.raises(ValueError):validate_operation(value['current_operation'])


def test_durable_closed_attempt_keeps_three_exact_replies_through_setting_toggles_and_follow_projection(operating,monkeypatch):
    """기존 종결 attempt는 typed 저장 사전조건이다. 신규 live 주차/예약 성공의 증거가 아니다."""
    from xenova_gateway.mission_link import BudgetEnvelope
    from xenova_gateway.authority_journal import canonical
    from b08_fixture import journal
    w=operating;scene=schema.SceneAdmissionRef(admission_id=uuid4().bytes,admission_revision=1)
    monkeypatch.setattr(w.r,'scene_runtime',SimpleNamespace(owner=None,
        motion_grant=lambda now:SimpleNamespace(scene=scene,valid_until_pi_us=now+100000)))
    record=w.o.missions.start(w.call('parking.start',dict(strategy='REVERSE')),2,admitted=True)
    record['operation_binding']=binding_record(w.o.operations.binding())
    record['operation_binding'].update(departure_nonce=uuid4().hex,operation_revision=3,parking_enabled=True)
    record.update(execution_deadline=record['issued']+30000000,goal='12'*32)
    requests=[];original=[]
    for correction in range(3):
        body=schema.MissionBudgetRequest(major=1,request_nonce=uuid4().bytes,mission_id=UUID(record['id']).bytes,
            mission_revision=record['revision'],control_generation=record['generation'],expected_grant_revision=correction+1,
            action=1 if correction==0 else 2,slot_id=1,slot_revision=1,goal_revision=1,plan_revision=correction+1,
            plan_sha256=b'p'*32,selected_goal_sha256=bytes.fromhex(record['goal']),scene_admission_id=scene.admission_id,
            scene_revision=1,completed_guest_us=1000000,valid_until_pi_us=w.o.now+100000)
        raw=encode(body);digest=sha256(raw).digest();record['corrections']=correction;record['grant_revision']=correction+2
        reply=encode(schema.MissionBudgetReply(major=1,request_nonce=body.request_nonce,request_sha256=digest,
            result=1,reason=0,grant=w.o.missions.grant(record)))
        record['reservations'].append(dict(nonce=body.request_nonce.hex(),sha=digest.hex(),reply=base64.b64encode(reply).decode('ascii')))
        requests.append(BudgetEnvelope(*w.peer,w.w.m.generation,raw,body.request_nonce,digest,w.o.now,body.valid_until_pi_us))
        original.append(reply)
    record.update(admitted=False,phase='HOLD',result='CANCELLED')
    follow=deepcopy(record);follow.update(kind=1,strategy=0,deadline=0,search_deadline=0,execution_deadline=0,
        corrections=0,execution_distance=0.,search_distance=0.,reservations=[],goal=None)
    follow.pop('search_meter',None);follow.pop('execution_meter',None)
    value=deepcopy(w.o.operations.current());value['parking_attempt']=deepcopy(record)
    journal(w,dict(current_operation=value,current_mission=follow))
    assert len(canonical(record))<=4096 and len(canonical(w.o.operations.current()))<=6144
    for enabled in (True,False,True):
        settle(w,operation_call(w,'parking.permission.set',enabled=enabled))
        retained=w.o.operations.current()['parking_attempt']
        assert retained==record and w.o.missions.current()['kind']==1
    for envelope,reply in zip(requests,original):
        value,changes=w.o.missions.budget(envelope,w.o.now+200000000,False)
        assert changes is None and encode(value)==reply


def test_performing_off_submits_stop_before_blocked_journal_and_keeps_cancelled_budget(operating,monkeypatch):
    """진행중 임무는 typed 사전조건, 새 control/원장/즉시 STOP 경로는 실제 제품이다."""
    from b08_fixture import journal
    w=operating;scene=schema.SceneAdmissionRef(admission_id=uuid4().bytes,admission_revision=1)
    monkeypatch.setattr(w.r,'scene_runtime',SimpleNamespace(owner=None,
        motion_grant=lambda now:SimpleNamespace(scene=scene,valid_until_pi_us=now+100000)))
    w.r._emit(w.w.m._change(Mode.AUTO,A,'explicit-stopped-test-context',w.w.t,arm=False))
    key=operation_call(w,'drive.depart')['request_id']
    value=deepcopy(w.o.operations.current());value.update(phase='APPLYING',desired_mode='AUTO',departure=key,parking_enabled=True)
    record=w.o.missions.start(w.call('parking.start',dict(strategy='REVERSE')),2,admitted=True)
    record.update(phase='EXECUTE',execution_deadline=record['issued']+30000000,goal='23'*32)
    record['execution_meter']=w.o.missions._meter_anchor();assert record['execution_meter'] is not None
    journal(w,dict(current_operation=value,current_mission=record))
    assert w.o.missions.exhausted(w.o.now) is None
    original=deepcopy(w.o.operations.current()['parking_attempt']);entered=threading.Event();release=threading.Event();save=w.w.auth.save
    def blocked(value):entered.set();assert release.wait(2);return save(value)
    monkeypatch.setattr(w.w.auth,'save',blocked)
    assert w.o._proposal('test-blocked-operation-journal',{},'test',w.o.now+10000000,lambda ok,unknown:None)
    assert entered.wait(1)
    call=operation_call(w,'parking.permission.set',enabled=False);before=len(w.sink.frames);fence=w.o.fence_revision
    response=w.o.submit(call)
    try:
        started=time.monotonic();w.tick()
        assert time.monotonic()-started<.2 and w.o.fence_revision>fence and len(w.sink.frames)>before
        assert response.empty() and w.o.operations.current()['parking_enabled']
    finally:release.set()
    w.pump(lambda:not response.empty());assert response.get_nowait()['ok']
    w.pump(lambda:w.o._known(call['request_id'])['terminal'])
    w.pump(lambda:w.o.operations.current()['phase']=='WAIT')
    current=w.o.operations.current();assert not current['parking_enabled'] and current['result']=='CANCELLED'
    assert current['parking_attempt']['deadline']==original['deadline']
    assert current['parking_attempt']['execution_deadline']==original['execution_deadline']
    assert current['parking_attempt']['result']=='CANCELLED' and not current['parking_attempt']['admitted']


@pytest.mark.parametrize('action,args',[
    ('parking.permission.set',dict(enabled=1,expected_permission_revision='1')),
    ('parking.permission.set',dict(enabled=True,expected_permission_revision=str(2**63))),
    ('drive.depart',dict(mode='AUTO',expected_operation_revision='01',expected_permission_revision='1',ready_revision='1',trigger_sha256=None)),
    ('drive.depart',dict(mode='AUTO',expected_operation_revision='1',expected_permission_revision='1',ready_revision='1',trigger_sha256='AB'*32)),
])
def test_new_actions_reject_wrong_types_ranges_and_noncanonical_digest(operating,action,args):
    with pytest.raises((OperatorError,ValueError)):request(operating.call(action,args))


def test_real_cli_builds_explicit_cas_actions_and_separate_status(monkeypatch,operating):
    from xenova_gateway import operator_client as cli
    from xenova_gateway import keyboard_source
    w=operating;calls=[];original_read=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:HOST if str(path)=='/proc/sys/kernel/random/boot_id' else original_read(path,*a,**kw))
    def call(self,body):
        request(body);calls.append(body)
        if body['action']=='status':return dict(version=2,ok=True,data=w.o.status())
        if body['action']=='operation.status':return dict(version=2,ok=True,data=w.o.operation_status())
        return dict(version=2,ok=False,error=dict(code='NOT_READY',request_id=body['request_id'],outcome='NOT_ACCEPTED',retryable=True))
    monkeypatch.setattr(cli.OperatorClient,'call',call);monkeypatch.setattr(cli.Attempts,'save',lambda self,body:None)
    monkeypatch.setattr(cli.Attempts,'observe',lambda *args:None)
    assert cli.main(['--wait-ms','0','operation','status'])==0
    assert calls[-1]==dict(version=2,action='operation.status')
    assert cli.main(['--wait-ms','0','parking','permission','on'])==2
    assert calls[-1]['action']=='parking.permission.set' and calls[-1]['args']['enabled'] is True
    assert cli.main(['--wait-ms','0','drive','depart','auto'])==2
    assert calls[-1]['action']=='drive.depart' and calls[-1]['args']['trigger_sha256'] is None
    forwarded=[]
    monkeypatch.setattr(keyboard_source,'main',lambda args:forwarded.append(args) or 0)
    assert cli.main(['manual','keyboard','--auto-enter'])==0 and '--auto-enter' in forwarded[-1]
    assert cli.main(['manual','keyboard'])==0 and '--auto-enter' not in forwarded[-1]
