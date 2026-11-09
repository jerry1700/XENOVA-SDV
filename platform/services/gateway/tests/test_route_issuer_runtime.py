"""실제 issuer/RouteOwner/P4/meter와 POSIX Authority journal의 RAM 등록 경계.

RouteRig의 geometry·정지 plant·app 적용은 선언된 leaf다. MCU/실제 C/M/D 성공을 뜻하지 않는다.
"""
import base64
from copy import deepcopy
import os
import time
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest

from xenova_drive_world import schema,encode,decode,validate
from xenova_gateway.authority import AuthorityStore
from xenova_gateway.authority_journal import AuthorityJournalWorker
from xenova_gateway.machine import Mode
from xenova_gateway.mission_authority import MissionAuthority
from xenova_gateway.operation_owner import OperationOwner,binding_record
from xenova_gateway.operator_service import OperatorService
from xenova_gateway.route_goal import RouteGoalOwner
from xenova_gateway.route_operator import RouteOperator
from xenova_gateway.route_owner import RouteOwner
from xenova_gateway.route_transition import RouteTransitionOwner
from route_runtime_fixture import RouteRig


pytestmark=pytest.mark.skipif(not hasattr(os,'O_DIRECTORY'),reason='actual POSIX Authority fsync')


@pytest.fixture
def issuer(tmp_path):
    rig=RouteRig();rig.begin();rig.register();assert rig.admit() is not None;rig.apply_leaf()
    r=rig.operator.runner;o=rig.operator;g=rig.grant
    store=AuthorityStore(tmp_path/'authority',rig.vehicle.host_boot,uid=os.getuid(),mountpoint=None)
    store.initialize(rig.peer[0]);r.authority=store;rig.authority=store
    r._clock=lambda:rig.now/1000000;r.operator=o;r.operation_support=True;r.route_profile=rig.route.policy
    r.drive_link.instance=rig.vehicle.gateway_instance
    rig.binding_rows={rig.peer[0]:dict(execution_id=rig.peer[1],app_instance_id=rig.peer[2])}
    r.bindings=SimpleNamespace(current=lambda vm,at:rig.binding_rows.get(vm))
    r.machine.generation=g.control_generation;r.machine.active_vm=rig.peer[0];r.machine.mode=Mode.AUTO
    r.machine.bindings=r.bindings
    operation=dict(version=1,service=rig.vehicle.gateway_instance,revision=g.operation_binding.operation_revision,
        permission_revision=g.operation_binding.permission_revision,parking_enabled=False,departure=rig.row['key'],
        desired_mode='AUTO',phase='ACTIVE',countdown_until=0,parking_attempt=None,follow=None,result='NONE',search_birth=None)
    record=dict(id=str(UUID(bytes=g.mission_id)),revision=g.mission_revision,grant_revision=g.grant_revision,
        kind=3,strategy=0,nonce=str(UUID(bytes=g.operator_request_nonce)),origin=rig.row['key'],issued=g.issued_pi_us,
        deadline=g.mission_deadline_pi_us,search_deadline=0,execution_deadline=0,corrections=0,
        execution_distance=0.,search_distance=0.,cancel_fence=g.cancel_fence_revision,phase='FOLLOW',result='NONE',admitted=True,
        scene_id=None if g.scene_admission_id==bytes(16) else str(UUID(bytes=g.scene_admission_id)),
        scene_revision=g.scene_revision,configuration=g.configuration,calibration=g.calibration,primitive=g.primitive.hex(),
        vehicle=g.vehicle,reservations=[],goal=None,owner=rig.peer[0],execution=rig.peer[1],app=rig.peer[2],
        generation=g.control_generation,operation_binding=binding_record(g.operation_binding),
        route_binding=base64.b64encode(encode(g.route_binding)).decode('ascii'))
    store.update(mode='AUTO',generation=g.control_generation,phase='APPLIED',rearm_required=False,
        current_mission=record,current_operation=operation)
    o.job=None;o.unknown=None;o.continuation=None;o.operations=OperationOwner(o)
    o.missions=MissionAuthority(r);o.worker=AuthorityJournalWorker(store,lambda:rig.now)
    facade=RouteOperator(o);o.routes=facade
    facade.attach(rig.route,RouteGoalOwner(rig.route),RouteTransitionOwner(rig.route),
        submit_management=lambda *args:False)  # D/M owner is intentionally unavailable in this test.
    def current_owner():
        owner=deepcopy(rig.owner_context);current=o.missions.current()
        if (current['generation'],current['owner'])!=(r.machine.generation,r.machine.active_vm):return None
        owner.vm,owner.execution,owner.app_instance=(UUID(current[k]).bytes for k in ('owner','execution','app'))
        owner.control_generation=current['generation'];owner.mission_id=UUID(current['id']).bytes
        owner.mission_revision=current['revision']
        owner.grant_revision=current['grant_revision'];owner.cancel_fence_revision=current['cancel_fence']
        return owner
    rig.route.current_owner=current_owner
    assert encode(o.missions.grant())==encode(g)
    try:yield rig,facade,store
    finally:o.worker.close()


def update(rig):
    rig.advance(20000);rig.progress();candidate=rig.update()
    assert rig.route.candidate(encode(candidate),rig.peer) is None
    assert rig.route.registration() is not None
    return candidate,rig.route.registration()


def admit_update(rig,candidate,reply):
    """새 권한을 소비한 C completion/fullState/application은 명시 typed leaf다."""
    grant=rig.operator.missions.grant();state=rig.state;receipt=decode(reply,schema.RouteReceipt)
    state.message_id=str(UUID(bytes=candidate.phase_state_message_id));state.state_revision+=1
    state.calculation_sequence=candidate.calculation_sequence+1
    state.route.CopyFrom(receipt.snapshot);state.route.progress.calculation_sequence=state.calculation_sequence
    state.mission.grant_revision=grant.grant_revision;state.mission.route_binding.CopyFrom(grant.route_binding)
    assert rig.admit() is not None;rig.apply_leaf()


def test_same_authority_update_has_no_disk_write_and_next_actual_journal_keeps_latest_registration(issuer):
    rig,facade,store=issuer;o=rig.operator;before=store.path.read_bytes();core=deepcopy(rig.route.registry['snapshot'].core)
    candidate,registration=update(rig)
    raw=o.missions.commit_route_registration(registration)
    reply=rig.route.commit_grant(candidate.request_nonce,raw)
    grant=decode(raw,schema.MissionGrant)
    assert store.path.read_bytes()==before and o.job is None and not o.worker.busy
    assert grant.grant_revision==rig.grant.grant_revision+1 and grant.control_generation==rig.grant.control_generation
    accepted=decode(reply,schema.RouteReceipt)
    assert accepted.snapshot.core.started_pi_us==core.started_pi_us
    assert accepted.snapshot.core.deadline_pi_us==core.deadline_pi_us
    assert accepted.snapshot.core.meter_baseline_total_ticks==core.meter_baseline_total_ticks
    assert accepted.snapshot.core.phase_expires_pi_us==core.phase_expires_pi_us
    assert rig.route.registry['state'] is None and not rig.route.registry['applied']
    assert rig.route.candidate(encode(candidate),rig.peer)==reply
    done=[]
    assert OperatorService._proposal(o,'route-checkpoint',{'reason':'same-authority-checkpoint'},'test_checkpoint',
        rig.now+1000000,lambda ok,unknown:done.append((ok,unknown)))
    end=time.monotonic()+2
    while o.worker.busy or o.worker._results.empty():
        assert time.monotonic()<end,'actual journal ACK not returned'
        time.sleep(.001)
    # The exact same grant is safe while its identical logical record is being persisted.
    assert o.missions.grant()==grant
    outcome=o.worker.take('operator');assert outcome.proposal is o.job and outcome.ack is not None
    o.job=None;o.missions.journal_completed(True,False);o.continuation(True,False)
    assert done==[(True,False)] and o.missions.grant()==grant
    assert store.value['current_mission']==o.missions.current() and store.path.read_bytes()!=before


@pytest.mark.parametrize('change',['generation','permission','unknown'])
def test_invalid_ram_registration_does_not_fall_back_to_old_durable_seed(issuer,change):
    rig,facade,store=issuer;o=rig.operator
    _,registration=update(rig);o.missions.commit_route_registration(registration)
    seed=deepcopy(store.value['current_mission'])
    if change=='generation':store.update(generation=store.value['generation']+1)
    elif change=='permission':
        value=deepcopy(store.value['current_operation']);value['permission_revision']+=1
        store.update(current_operation=value)
    else:o.missions.journal_completed(False,True)
    assert o.missions.grant() is None
    current=o.missions.current()
    assert not current['admitted'] and current['phase']=='HOLD'
    assert current['grant_revision']>seed['grant_revision'] and current['deadline']==seed['deadline']
    assert o.missions.grant() is None and not rig.route.command_allowed(rig.peer,dict(producer_role='AUTO',command_kind='DRIVE'))


def test_receipt_expiry_after_issuer_adoption_revokes_instead_of_rolling_back(issuer,monkeypatch):
    rig,facade,store=issuer;o=rig.operator;before=store.path.read_bytes()
    _,registration=update(rig);original=rig.route.commit_grant;old=o.missions.current()['grant_revision']
    def expires(nonce,raw):
        rig.now=decode(registration.candidate_raw,schema.RouteCandidate).witness.valid_until_pi_us
        o.now=rig.now
        return original(nonce,raw)
    monkeypatch.setattr(rig.route,'commit_grant',expires)
    stops=[];o._stop=lambda reason:(stops.append(reason),rig.route.cancel(stop=False))
    facade._commit_phase_registration()
    assert stops and rig.route.failed and o.missions.grant() is None
    assert o.missions.route_overlay['grant_revision']==old+1 and store.path.read_bytes()==before
    assert rig.route.registry['snapshot'].core.revision==1  # No successful current receipt was made.


def test_prepared_b_reuses_original_state_only_after_real_durable_rebind_and_a_rejoin_keeps_b(issuer):
    rig,facade,store=issuer;o=rig.operator;r=o.runner
    candidate,registration=update(rig);raw=o.missions.commit_route_registration(registration)
    receipt=rig.route.commit_grant(candidate.request_nonce,raw);admit_update(rig,candidate,receipt)
    original_core=encode(rig.route.registry['snapshot'].core);original_reply=rig.route.registry['reply']
    old_generation=r.machine.generation
    target=tuple(str(uuid4()) for _ in range(3));rig.binding_rows[target[0]]=dict(execution_id=target[1],app_instance_id=target[2])
    o.episodes.peers[target]=127
    # Existing control Authority, not the RAM API, performs the declared A/B control transfer.
    store.issue(Mode.AUTO,target[0],'typed-existing-warm-handover',r._clock(),1.,arm=True)
    r.machine.generation=store.value['generation'];r.machine.active_vm=target[0]
    source=o.missions.route_rebind_source();assert source is not None and source['generation']==old_generation
    assert o.missions.grant() is None  # Old generation's motion authority has already been withdrawn.
    o._proposal=lambda *args:OperatorService._proposal(o,*args)
    o._stop=lambda reason:rig.stop_calls.append(reason)
    o.mission_fence=None;r.drive_link.publish_mission=lambda grant:True  # Explicit transport leaf, not device APPLIED.
    OperatorService._mission_tick(o)
    assert o.job is not None and o.job.operation=='mission_binding'
    end=time.monotonic()+2
    while o.worker.busy or o.worker._results.empty():
        assert time.monotonic()<end
        time.sleep(.001)
    assert o.missions.grant() is None  # New grant bytes are withheld until the actual ACK is consumed.
    facade.tick()
    assert not rig.route.failed and not rig.stop_calls and rig.route.registry['state'] is not None
    status=rig.route.status(target,rig.now+10000);validate(status)
    assert status.route_state==2 and status.commit_state==1 and status.current_permission_sha256==bytes(32)
    outcome=o.worker.take('operator');assert outcome.ack is not None
    o.job=None;o.missions.journal_completed(True,False);o.continuation(True,False)
    facade.tick()
    assert o.missions.grant().owner_vm==UUID(target[0]).bytes and not rig.route.failed
    assert rig.route.registry['state'] is None and not rig.route.registry['applied']
    assert encode(rig.route.registry['snapshot'].core)==original_core and rig.route.registry['reply']==original_reply
    assert not rig.route.command_allowed(target,dict(producer_role='AUTO',command_kind='DRIVE'))
    # A's recovery/capability alone is not another control transfer or an automatic owner reset.
    o.episodes.peers[rig.peer]=127;facade.tick()
    assert r.machine.active_vm==target[0] and o.missions.grant().owner_vm==UUID(target[0]).bytes


@pytest.mark.parametrize('loss',['restart','manual','feature','seed'])
def test_recovery_candidate_is_not_current_authority_or_a_restart_none_shortcut(issuer,loss):
    rig,facade,store=issuer;o=rig.operator;r=o.runner
    target=tuple(str(uuid4()) for _ in range(3));rig.binding_rows[target[0]]=dict(execution_id=target[1],app_instance_id=target[2])
    o.episodes.peers[target]=127
    store.issue(Mode.AUTO,target[0],'typed-existing-warm-handover',r._clock(),1.,arm=True)
    r.machine.generation=store.value['generation'];r.machine.active_vm=target[0]
    assert o.missions.route_rebind_source() is not None
    original=encode(rig.route.registry['snapshot'].core)
    if loss=='restart':
        fresh=RouteOwner(o,rig.route.policy,rig.data,current_owner=rig.route.current_owner,
            installed=rig.route.installed,mapper_support=rig.route.mapper_support,stop_auto=rig.stop_calls.append,
            terminal_check=rig.route.terminal_check,progress_check=rig.route.progress_check)
        facade.route=fresh
        assert fresh.registry is None  # Geometry retained in D/RAM does not restore the source acceptance.
    elif loss=='manual':
        store.issue(Mode.MANUAL,target[0],'typed-explicit-manual',r._clock(),1.,arm=False)
        r.machine.generation=store.value['generation'];r.machine.mode=Mode.MANUAL
    elif loss=='feature':o.episodes.peers[target]=63
    else:
        changed=deepcopy(store.value['current_mission']);changed['grant_revision']+=1
        store.update(current_mission=changed)
    assert o.missions.route_rebind_source() is None
    assert encode(rig.route.registry['snapshot'].core)==original
    assert rig.route.command_allowed(target,dict(producer_role='MANUAL'))
