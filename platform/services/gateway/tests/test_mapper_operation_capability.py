"""실제 MapperClockOwner/P4·local frame의 cap1 협상. M/child/STOP/guest report는 명시 typed leaf다.

MQTT 인증·실제 프로세스 reap·C geometry·모터 권위 성공으로 세지 않는다. 원 clock 코드와
Gateway cap/Gate/AUTO readiness 소비를 쓰며 지원 bool을 제품에 강제 주입하지 않는다.
"""
from hashlib import sha256
from types import SimpleNamespace
import socket
from uuid import UUID

import pytest
from xenova_drive_world import schema,encode,decode
from xenova_drive_world.mapper_clock import candidate_body,plan_json
from xenova_gateway.mapper_uplink import (GatewayMapperUplink,UplinkProfile,_Link,
    parse_operation_plan,build_operation_probe,operation_plan)
from xenova_gateway.operation_owner import OperationOwner
from xenova_gateway.verified_runner import VerifiedGatewayRunner
from xenova_gateway.machine import Mode
from test_mapper_uplink import policy,vehicle,reply_for,uid


class ClockRig:
    def __init__(self):
        self.now=1000000;self.sequence=1;self.last_plan=800000;self.last_reply=None
        self.u=GatewayMapperUplink(UplinkProfile(policy(),'127.0.0.1',1883,None,None),uid(1),uid(4),clock=lambda:self.now)
        self.u.uplink_instance=uid(20);self.u.channel_epoch=uid(21)
        left,right=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        identity=(uid(1),uid(4),uid(20),uid(21))
        self.u.link=_Link(left,identity);self.peer=_Link(right,identity)
        self.u.process=SimpleNamespace(exitcode=None)  # declared external child, not a real process/reap proof
        self.gates=0
        def gate():
            self.gates+=1
            return schema.PiOperationGate(gateway_instance=UUID(uid(4)).bytes,control_generation=7,
                operation_revision=2,mode=2,wait_confirmed=False,manual_local_admitted=True,
                vehicle_sequence=self.u.owner.vehicle.sequence,stop_command_sequence=0,valid_until_pi_us=self.now+100000)
        self.provider=SimpleNamespace(runner=SimpleNamespace(operation_support=True,
            operator=SimpleNamespace(operations=SimpleNamespace(mapper_gate=gate))),trust=None,world_failure=None)

    def close(self):
        self.u.link.socket.close();self.peer.socket.close()

    def plan(self,at=None):
        self.now=self.last_plan+200000 if at is None else at;self.last_plan=self.now
        self.sequence+=1;sample=vehicle(self.now,self.sequence)
        self.u.offer_vehicle(encode(sample));self.u.tick(self.provider)
        packets=[(header,body) for header,body in self.peer.receive(self.now) if header['kind']=='PROBE_PLAN']
        assert len(packets)==1
        header,body=packets[0]
        plan,gate=parse_operation_plan(body,request_id=header['request_id'],
            issued_us=int(header['issued_pi_us']),expires_us=int(header['expires_pi_us']))
        raw=build_operation_probe(plan,gate,self.now+100)
        return plan,raw

    def finish(self,plan,raw,*,support=1,change=None):
        reply=reply_for(plan,raw)
        if support is not None:reply.supported_mapper_features=support
        if change is not None:change(reply)
        original=encode(reply);p4=self.now+500
        assert self.peer.offer('TRUST_CANDIDATE',candidate_body(raw,original,p4),p4,plan.expires_us,plan.request_id)
        self.peer.flush(p4);self.now=p4;self.u.tick(self.provider);self.last_reply=original
        return self.u.operation_capability(self.now)

    def exchange(self,**kwargs):
        plan,raw=self.plan();return self.finish(plan,raw,**kwargs),raw

    def supported(self):
        first,raw=self.exchange();assert first is None and not decode(raw,schema.MapperClockProbe).HasField('operation_gate')
        cap,raw=self.exchange();assert cap is not None and not decode(raw,schema.MapperClockProbe).HasField('operation_gate')
        return cap


@pytest.fixture
def rig():
    value=ClockRig()
    try:yield value
    finally:value.close()


def operation_unit(rig):
    """기존 actor readiness의 입력 leaf만 만든다. durable/STOP/C preflight 성공 증거가 아니다."""
    r=VerifiedGatewayRunner.__new__(VerifiedGatewayRunner);r.operation_support=True;r.mapper_uplink=rig.u
    r._clock=lambda:rig.now/1e6;r.scene_runtime=None
    vm,execution,app=uid(30),uid(31),uid(32)
    r.bindings=SimpleNamespace(current=lambda active,now:dict(execution_id=execution,app_instances={'autonomous':app}))
    r.machine=SimpleNamespace(active_vm=vm,generation=7,mode=Mode.MANUAL,failure=None,transport_ok=True,
        authority=SimpleNamespace(protected=lambda active:False),output_permitted=True)
    r.drive_link=SimpleNamespace(trust=rig.u.owner.trust(rig.now))
    v=rig.u.owner.vehicle
    r._sink=SimpleNamespace(device_view=lambda:dict(vehicle_sample={key:getattr(v,key) for key in
        ('host_boot','pi_session','device_session','odom_epoch','calibration','sequence')}))
    o=SimpleNamespace(runner=r,instance=uid(4),now=rig.now,ready=True,unknown=None,
        stationary=lambda:True,sources=SimpleNamespace(keys=(),moving=False,valid=True,kind='NEUTRAL_ONLY',
        blocked=False,state='ARMED_NEUTRAL',all_up=2,expires=rig.now+1000000))
    owner=OperationOwner(o);o.operations=owner
    r.authority=SimpleNamespace(host=uid(1),value={'current_operation':owner.startup()})
    value=r.authority.value['current_operation'];value['phase']='WAIT'
    peer=(vm,execution,app);o.episodes=SimpleNamespace(peers={peer:63},supported=lambda selected:selected==peer)
    trust=r.drive_link.trust
    source=schema.EpisodeSourceContext(**{key:getattr(trust,key) for key in
        ('vehicle','mapper_boot','mapper_instance','clock_epoch','map_epoch','frame_epoch','odom_epoch','configuration','calibration')},
        primitive=bytes.fromhex(trust.primitive),reference_id='road:unit',reference_revision=1,world_sequence=1,observation_sequence=1)
    report=schema.AutoInputPreflight(major=1,source=source,vehicle_sequence=v.sequence,control_generation=7,
        checked_guest_us=1000000,valid_until_pi_us=rig.now+70000,clock_probe_sequence=1,clock_reply_sha256=b'x'*32)
    owner.reports[peer]=dict(body=report,raw=encode(report),generation=7)
    return r,o,owner,peer


def test_first_numeric_probes_have_no_gate_until_actual_two_reply_cap1(rig):
    cap=rig.supported()
    assert cap.reply_bytes==rig.last_reply and cap.reply_sha256==sha256(rig.last_reply).digest()
    original=decode(cap.reply_bytes,schema.MapperClockReply)
    assert cap.valid_until_pi_us==original.pi_send_us+policy().lease_us
    assert rig.gates==0
    _,raw=rig.plan();probe=decode(raw,schema.MapperClockProbe)
    assert probe.HasField('operation_gate') and rig.gates==1
    assert probe.operation_gate.valid_until_pi_us<=cap.valid_until_pi_us
    assert rig.u.owner.trust(rig.now) is not None


@pytest.mark.parametrize('support',[None,0])
def test_legacy_or_zero_support_renews_numeric_clock_without_gate(rig,support):
    for _ in range(2):cap,raw=rig.exchange(support=support);assert cap is None
    trust=rig.u.owner.trust(rig.now);assert trust is not None
    _,raw=rig.plan();probe=decode(raw,schema.MapperClockProbe)
    assert not probe.HasField('operation_gate') and probe.HasField('mapper_trust')
    assert probe.mapper_trust==trust and rig.gates==0


@pytest.mark.parametrize('support',[None,0])
def test_current_reply_omission_withdraws_cap_without_extending_old_proof(rig,support):
    old=rig.supported();old_bytes=old.reply_bytes
    cap,_=rig.exchange(support=support);assert cap is None
    assert old.reply_bytes==old_bytes and rig.u.owner.trust(rig.now).expires_pi_us>old.valid_until_pi_us
    _,raw=rig.plan();assert not decode(raw,schema.MapperClockProbe).HasField('operation_gate')


@pytest.mark.parametrize('fault',['mapper-source','session','channel','reply-invalid','process-exit','close'])
def test_source_session_restart_or_current_reply_error_withdraws_support(rig,fault):
    cap=rig.supported()
    if fault=='mapper-source':
        new,_=rig.exchange(change=lambda reply:setattr(reply,'map_epoch',uid(80)))
        assert new is None and rig.u.owner.trust(rig.now) is None  # new source still needs its own consistent pair
    elif fault=='session':
        sample=vehicle(rig.now,rig.sequence+1);sample.pi_session=uid(81)
        rig.u.offer_vehicle(encode(sample));assert rig.u.operation_capability(rig.now) is None
        assert rig.u.owner.trust(rig.now) is not None  # numeric semantics unchanged
    elif fault=='channel':
        rig.u.channel_epoch=uid(82);assert rig.u.operation_capability(rig.now) is None
    elif fault=='process-exit':
        rig.u.process.exitcode=1;rig.u.tick(rig.provider)
        assert rig.u.operation_capability(rig.now) is None and rig.u.failure is not None
    elif fault=='close':
        rig.u.process=None;rig.u.close()  # no real child/reap is claimed by this leaf
        assert rig.u.closed and rig.u.operation_capability(rig.now) is None
    else:
        new,_=rig.exchange(change=lambda reply:setattr(reply,'configuration','wrong-profile'))
        assert new is None and rig.u.counts['clock_rejected']==1 and rig.u.owner.trust(rig.now) is not None
    assert cap.reply_bytes==encode(decode(cap.reply_bytes,schema.MapperClockReply))


def test_unmatched_old_request_packet_does_not_revoke_or_extend_current_cap(rig):
    original=rig.supported();plan,raw=rig.plan();p4=rig.now+500
    # Authenticated local channel, unrelated/outdated request. The actor must
    # ignore it before inspecting its mismatched old reply body.
    body=candidate_body(raw,original.reply_bytes,p4)
    assert rig.peer.offer('TRUST_CANDIDATE',body,p4,plan.expires_us,uid(999))
    rig.peer.flush(p4);rig.now=p4;rig.u.tick(rig.provider)
    assert rig.u.operation_capability(rig.now) is original
    assert original.reply_bytes==rig.last_reply and rig.u.counts['old_candidate']==1
    assert rig.u.owner.pending.request_id==plan.request_id
    assert original.valid_until_pi_us==decode(original.reply_bytes,schema.MapperClockReply).pi_send_us+policy().lease_us


def test_original_cap_e_expires_and_gate_clips_to_it(rig):
    cap=rig.supported();_,raw=rig.plan(cap.valid_until_pi_us-10000)
    gate=decode(raw,schema.MapperClockProbe).operation_gate
    assert gate.valid_until_pi_us==cap.valid_until_pi_us
    rig.now=cap.valid_until_pi_us
    assert rig.u.operation_capability(rig.now) is None
    assert cap.valid_until_pi_us==decode(cap.reply_bytes,schema.MapperClockReply).pi_send_us+policy().lease_us


def test_withdrawal_removes_unsent_gate_but_preserves_numeric_original_plan(rig):
    rig.supported();rig.now=rig.last_plan+200000
    sample=vehicle(rig.now,rig.sequence+1);rig.u.offer_vehicle(encode(sample))
    plan=rig.u.owner.plan(rig.now,rig.u.uplink_instance);assert plan is not None
    gate=rig.provider.runner.operator.operations.mapper_gate()
    rig.u.pending['probe']=('PROBE_PLAN',operation_plan(plan,gate),plan.issued_us,plan.expires_us,plan.request_id)
    assert rig.u.link.offer('PROBE_PLAN',operation_plan(plan,gate),plan.issued_us,plan.expires_us,plan.request_id)
    rig.u._revoke_operation_capability()
    assert rig.u.pending['probe'][1]==plan_json(plan)
    from xenova_gateway.mapper_uplink import unframe
    header,body=unframe(rig.u.link.pending[0]);assert body==plan_json(plan) and header['request_id']==plan.request_id
    numeric=decode(build_operation_probe(plan,None,rig.now+100),schema.MapperClockProbe)
    assert numeric.nonce==plan.nonce and numeric.probe_seq==plan.probe_seq and not numeric.HasField('operation_gate')


def test_auto_ready_requires_current_matching_mapper_support_manual_is_independent(rig):
    rig.supported();r,o,owner,peer=operation_unit(rig)
    assert owner.inputs()[0] and owner.inputs('MANUAL')[0]
    first=owner.view();assert first['auto_ready']
    cap=rig.u.operation_capability(rig.now)
    rig.u._revoke_operation_capability()
    assert owner.inputs()[1]=='SOURCE_UNAVAILABLE' and owner.inputs('MANUAL')[0]
    second=owner.view();assert not second['auto_ready'] and int(second['ready_revision'])>int(first['ready_revision'])
    assert r.machine.mode is Mode.MANUAL and r.machine.generation==7
    assert rig.u.owner.trust(rig.now) is not None and cap.reply_bytes==rig.last_reply


def test_support_is_not_installation_or_another_source_or_drive_authority(rig):
    rig.supported();r,o,owner,peer=operation_unit(rig)
    r.operation_support=False
    assert r.mapper_operation_capability(rig.now) is None and owner.inputs()[1]=='UNSUPPORTED_PROFILE'
    assert rig.u.owner.trust(rig.now) is not None
    r.operation_support=True
    body=owner.reports[peer]['body'];body.source.map_epoch=uid(99)
    assert owner.inputs()[1]=='SOURCE_UNAVAILABLE'
    assert owner.current()['phase']=='WAIT' and owner.current()['departure'] is None
    assert r.machine.mode is Mode.MANUAL and r.machine.generation==7


def test_existing_provider_without_operation_runner_keeps_numeric_clock_and_no_gate(rig):
    # The actual motion diagnostic provider predates operation support. Absence
    # of its runner is not a new installed feature or a numeric clock failure.
    del rig.provider.runner
    rig.supported()
    _,raw=rig.plan()
    probe=decode(raw,schema.MapperClockProbe)
    assert probe.HasField('mapper_trust') and not probe.HasField('operation_gate')
    assert rig.u.failure is None and rig.gates==0
