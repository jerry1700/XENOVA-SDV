"""원 route 등록·시각/거리·fullState·apply 경계. native/physical 성능 시험은 아니다.

RouteRig의 A1 ledger/M/D/application/geometry typed leaf 범위를 함께 읽는다.
제품 clock owner와 encoder meter/codec/registry를 우회하지 않는다.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID,uuid4

import pytest

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import route_candidate_sha256,RouteReason
from xenova_gateway.route_policy import RouteError,NIL
from route_runtime_fixture import RouteRig


def test_cold_route_registry_stays_unknown_and_legacy_manual_stop_remain_independent():
    rig=RouteRig();owner=rig.route
    assert owner.registry is None and owner.status(rig.peer,rig.now+10000).route_state==0
    assert owner.status(rig.peer,rig.now+10000).registry_epoch==NIL
    assert owner.command_allowed(rig.peer,dict(producer_role='MANUAL'))
    assert owner.command_allowed(rig.peer,dict(command_kind='STOP',next_direction=0))
    assert owner.registration() is None and not rig.stop_calls


def test_begin_requires_original_operator_nonce_and_current_durable_countdown_binding():
    rig=RouteRig();rig.begin();original=rig.route.original_query()
    assert original==rig.query_raw and rig.route.registry is None
    other=RouteRig();bad=uuid4().bytes
    other.case['owner'].operation_binding.departure_nonce=bad
    other.case['query'].owner.operation_binding.departure_nonce=bad
    # The original acknowledged row cannot be relabelled as the new owner nonce.
    with pytest.raises(RouteError):other.begin()
    assert other.route.query is None


def test_original_candidate_has_no_receipt_until_actual_current_kind3_grant_ack():
    rig=RouteRig();rig.begin();value=rig.case['candidate'];raw=encode(value)
    assert rig.route.candidate(raw,rig.peer) is None and rig.route.registration() is not None
    assert rig.route.candidate(raw,rig.peer) is None and rig.route.registry is None
    fake=deepcopy(rig.case['candidate']);fake.request_nonce=uuid4().bytes
    with pytest.raises(RouteError):rig.route.candidate(encode(fake),rig.peer)
    assert rig.route.status(rig.peer,rig.now+10000).route_state==0


def test_receipt_first_full_state_application_and_new_calculation_are_separate_boundaries():
    rig=RouteRig();rig.begin();reply=rig.register();owner=rig.route
    original=encode(reply);assert reply.result==1 and owner.status(rig.peer,rig.now+10000).commit_state==1
    assert owner.candidate(encode(rig.case['candidate']),rig.peer)==original
    command=dict(producer_role='AUTO',command_kind='DRIVE',generation=rig.owner_context.control_generation,
        mission_id=str(UUID(bytes=rig.grant.mission_id)),mission_revision=rig.grant.mission_revision,grant_revision=rig.grant.grant_revision,
        plan_revision=rig.state.path_revision,phase_revision=rig.state.maneuver_revision,
        calculation_sequence=rig.state.calculation_sequence+1,clock_probe_sequence=rig.clock_ref,
        completed_guest_us=rig.now,valid_until_pi_us=rig.now+50000,vehicle_sequence=rig.vehicle.sequence,
        world_sequence=rig.case['world'].sequence)
    assert not owner.command_allowed(rig.peer,command)
    assert rig.admit() is not None and owner.status(rig.peer,rig.now+10000).commit_state==2
    assert not owner.command_allowed(rig.peer,command)
    rig.apply_leaf();assert owner.status(rig.peer,rig.now+10000).commit_state==3
    assert not owner.command_allowed(rig.peer,command)  # completed before actual apply ACK lower-bound
    rig.now+=4000;rig.operator.now=rig.now;command['completed_guest_us']=rig.now
    command['calculation_sequence']=rig.state.calculation_sequence
    assert not owner.command_allowed(rig.peer,command)
    command['calculation_sequence']+=1
    assert owner.command_allowed(rig.peer,command)
    command['world_sequence']+=1;assert not owner.command_allowed(rig.peer,command)
    before_state=owner.registry['state']['raw'];before_hwm=owner.registry['floor']
    rig.advance(20000)
    command.update(completed_guest_us=rig.now,valid_until_pi_us=rig.now+50000,
        vehicle_sequence=rig.vehicle.sequence,world_sequence=rig.case_now['world'].sequence)
    # A new actually-published frame can feed a fresh calculation while the
    # same admitted geometry remains current. No per-frame State/apply ACK.
    assert owner.command_allowed(rig.peer,command)
    assert owner.registry['state']['raw']==before_state and owner.registry['floor']==before_hwm
    other=deepcopy(rig.case_now);boot=str(uuid4())
    other['world'].mapper_boot=other['witness'].source.mapper_boot=boot
    other['world'].sequence+=1;other['witness'].source.world_sequence=other['world'].sequence
    other['witness'].world_raw_sha256=sha256(encode(other['world'])).digest()
    rig.publish_world(other);command['world_sequence']=other['world'].sequence
    assert not owner.command_allowed(rig.peer,command)
    command['world_sequence']=rig.case_now['world'].sequence
    rig.now=rig.case_now['witness'].valid_until_pi_us+1;rig.operator.now=rig.now
    command.update(completed_guest_us=rig.now,valid_until_pi_us=rig.now+50000)
    assert not owner.command_allowed(rig.peer,command)  # Original World E is not extended by lookup.


@pytest.mark.parametrize('change',['identity','path','basis','grant'])
def test_first_state_does_not_adopt_other_identity_geometry_or_permission(change):
    rig=RouteRig();rig.begin();rig.register();state=deepcopy(rig.state)
    if change=='identity':state.message_id=str(uuid4())
    elif change=='path':state.path[0].pose.x+=.01
    elif change=='basis':state.task_reference.basis_prefix[1].x+=.01
    else:state.mission.grant_revision+=1
    assert rig.admit(state) is None and rig.route.registry['state'] is None


def test_query_expiry_is_only_new_begin_admission_and_does_not_restart_registered_route():
    rig=RouteRig();rig.begin();reply=rig.register();assert rig.admit() is not None
    core=encode(reply.snapshot.core);query=rig.route.original_query()
    # Every sample is a declared plant/tracker leaf plus actual ingress meter.
    # Current original clock/World/Vehicle leases are renewed by their actual
    # owners; original route birth/core/Query bytes remain unchanged.
    for _ in range(101):
        rig.advance();rig.progress();assert rig.route.exhausted() is None
    assert rig.now>rig.case['query'].valid_until_pi_us and rig.route.original_query()==query
    assert encode(rig.route.registry['snapshot'].core)==core
    update=rig.update();assert rig.route.candidate(encode(update),rig.peer) is None
    registration=rig.route.registration();assert registration is not None
    current=decode(registration.snapshot_raw,schema.RouteSnapshot)
    assert current.core.started_pi_us==reply.snapshot.core.started_pi_us
    assert current.core.deadline_pi_us==reply.snapshot.core.deadline_pi_us
    assert current.core.phase_started_pi_us==reply.snapshot.core.phase_started_pi_us
    assert current.core.meter_baseline_total_ticks==reply.snapshot.core.meter_baseline_total_ticks
    assert current.progress.used_travel_ticks==2020 and current.progress.last_progress_pi_us>0
    # Retry still returns the original accepted bytes, never a new authority.
    assert rig.route.candidate(encode(rig.case['candidate']),rig.peer)==encode(reply)


def test_expired_original_query_cannot_admit_new_begin_even_with_fresh_inputs():
    rig=RouteRig();rig.begin()
    for _ in range(101):rig.advance()
    value=deepcopy(rig.case['candidate']);value.witness.CopyFrom(rig.case_now['witness'])
    value.extent.vehicle_sequence=value.progress.vehicle_sequence=rig.vehicle.sequence
    value.began_guest_us=value.phase_started_guest_us=rig.now-5000
    raw=rig.route.candidate(encode(value),rig.peer);reply=decode(raw,schema.RouteReceipt)
    assert (reply.result,reply.reason)==(3,RouteReason.CLOCK_OR_EXPIRY)
    assert not reply.HasField('snapshot') and not reply.HasField('initial_target_context')
    assert rig.route.pending is None and rig.route.registry is None


def test_request_arrival_does_not_refund_travel_after_original_birth_anchor():
    rig=RouteRig();rig.begin();origin=rig.meter.snapshot[0]
    rig.advance(20000,left=10,right=-10)
    request=deepcopy(rig.case['candidate']);request.witness.CopyFrom(rig.case_now['witness'])
    request.extent.vehicle_sequence=request.progress.vehicle_sequence=rig.vehicle.sequence
    assert rig.route.candidate(encode(request),rig.peer) is None
    snapshot=decode(rig.route.registration().snapshot_raw,schema.RouteSnapshot)
    assert snapshot.core.meter_baseline_total_ticks==origin.total_ticks
    assert snapshot.progress.used_travel_ticks==10
    assert snapshot.core.started_pi_us<rig.now and snapshot.core.started_pi_us+120000000==snapshot.core.deadline_pi_us


@pytest.mark.parametrize('loss',['moving','facts-missing'])
def test_prepare_stop_loss_cannot_become_a_new_begin_or_refund_preparation_anchor(loss):
    rig=RouteRig();rig.begin();anchor=rig.route.preparation['anchor'];original=rig.route.original_query()
    if loss=='moving':rig.physical_stopped=False
    else:rig.facts_available=False
    rig.route.tick()
    assert rig.route.failed and rig.route.reason==RouteReason.STOP_REQUIRED and rig.stop_calls
    assert rig.route.preparation['anchor']==anchor and rig.route.original_query()==original
    reply=decode(rig.route.candidate(encode(rig.case['candidate']),rig.peer),schema.RouteReceipt)
    assert reply.result!=1 and not reply.HasField('snapshot')
    assert rig.route.registry is None and rig.route.registration() is None


@pytest.mark.parametrize('fault',['feature','mapper','gap','distance','no-progress'])
def test_original_caps_and_capability_loss_fence_route_without_synthetic_finish_or_manual_revocation(fault):
    rig=RouteRig();rig.begin();reply=rig.register();assert rig.admit() is not None
    if fault=='feature':rig.operator.episodes.peers[rig.peer]=63
    elif fault=='mapper':rig.route.mapper_support=lambda:0
    elif fault=='gap':rig.advance(250000)
    elif fault=='distance':rig.advance(20000,left=50000,right=-50000)
    else:
        for _ in range(21):rig.advance(100000,left=0,right=0)
    rig.route.tick();assert rig.route.failed and rig.stop_calls
    assert rig.route.registry['snapshot'].state==2 and rig.route.registry['snapshot'].core.terminal_result==0
    assert rig.route.registry['snapshot'].core.started_pi_us==reply.snapshot.core.started_pi_us
    assert rig.route.command_allowed(rig.peer,dict(producer_role='MANUAL'))
    assert rig.route.command_allowed(rig.peer,dict(command_kind='STOP',next_direction=0))
    rig.operator.episodes.peers[rig.peer]=127;rig.route.mapper_support=lambda:2
    assert rig.route.failed and rig.route.status(rig.peer,rig.now+10000).route_state==0


def test_exact_retry_is_immutable_and_conflicting_body_is_not_a_second_registration():
    rig=RouteRig();rig.begin();reply=rig.register();revision=rig.route.revision
    raw=encode(rig.case['candidate']);assert reply.request_sha256==route_candidate_sha256(raw)
    rig.route.cancel();rig.now+=20000000;rig.operator.now=rig.now
    assert rig.route.candidate(raw,rig.peer)==encode(reply) and rig.route.revision==revision
    changed=deepcopy(rig.case['candidate']);changed.plan_revision+=1
    with pytest.raises(RouteError):rig.route.candidate(encode(changed),rig.peer)
    assert rig.route.registry['snapshot'].core.terminal_result==0 and rig.route.failed


def test_rebind_preserves_original_receipt_and_budget_but_requires_new_owner_state_before_output():
    rig=RouteRig();rig.begin();receipt=rig.register();assert rig.admit() is not None;rig.apply_leaf()
    owner=rig.route;original=encode(receipt);core=encode(owner.registry['snapshot'].core);query=owner.original_query()
    peer=tuple(str(uuid4()) for _ in range(3));target=deepcopy(rig.owner_context)
    target.vm,target.execution,target.app_instance=(UUID(value).bytes for value in peer)
    target.control_generation+=1;target.grant_revision+=1
    grant=deepcopy(rig.grant);grant.owner_vm=target.vm;grant.owner_execution=target.execution;grant.app_instance=target.app_instance
    grant.control_generation=target.control_generation;grant.grant_revision=target.grant_revision
    rig.owner_context=target;rig.grant=grant;rig.operator.episodes.peers[peer]=127
    owner.rebind_grant(encode(grant))
    assert owner.registry['state'] is None and not owner.registry['applied']
    assert encode(owner.registry['snapshot'].core)==core and owner.original_query()==query
    assert owner.candidate(encode(rig.case['candidate']),rig.peer)==original
    current=deepcopy(rig.state);current.source_vm,current.source_execution,current.source_app_instance=peer
    current.source_generation=target.control_generation;current.message_id=str(uuid4())
    current.calculation_sequence=current.route.progress.calculation_sequence=1
    current.mission.grant_revision=target.grant_revision
    prepared=owner.prepare_state(current,peer,encode(current))
    assert prepared is not None  # A's calculation43 and B's calculation1 are independent source counters.
    owner.commit_state(prepared)
    assert owner.status(peer,rig.now+10000).commit_state==2 and not owner.registry['applied']
    assert encode(owner.registry['snapshot'].core)==core and owner.registry['reply']==original
