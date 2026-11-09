"""출차 이력/전환의 UNKNOWN 경계. retained copy는 명시 typed application leaf다."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest

from xenova_drive_world import schema,encode
from xenova_gateway.episode import none_snapshot
from xenova_gateway.route_episode import inactive_episode_history
from xenova_gateway.route_goal import RouteGoalOwner
from xenova_gateway.route_transition import RouteTransitionOwner
from xenova_gateway.route_policy import RouteError,RouteReason,ZERO
from route_runtime_fixture import RouteRig
from test_route_data_runtime import HASH
from test_world import parking_state


def parked_history(rig):
    """현재 서비스의 원 parking snapshot과 closed ledger를 typed 사전조건으로 공급한다."""
    state=parking_state();source=rig.case['witness'].source
    for name in ('map_epoch','frame_epoch','odom_epoch','configuration','calibration'):
        setattr(state,name,getattr(source,name))
    state.primitive=source.primitive.hex();state.episode.CopyFrom(none_snapshot(uuid4().bytes,1));state.state_features|=16
    raw=encode(state);wire=schema.DriveLink(major=2)
    wire.accepted.CopyFrom(schema.AcceptedState(host_boot=rig.vehicle.host_boot,
        gateway_instance=rig.vehicle.gateway_instance,state=state))
    original=encode(wire);rig.provider.copies[state.message_id]=(original,)
    rig.operator.episodes.registry=dict(snapshot=deepcopy(state.episode));rig.operator.episodes.failed=True
    record=dict(id=str(UUID(bytes=state.mission.mission_id)),revision=state.mission.mission_revision,kind=2,
        phase='HOLD',admitted=False,result='CANCELLED',goal=HASH('selected-parking-goal').hex())
    rig.authority.value['current_operation']=dict(parking_attempt=record)
    rig.operator.missions.current=lambda:None;rig.operator.missions.execution=(raw,rig.now/1000000)
    return state,record,original


def test_real_inactive_history_survives_hold_flag_but_does_not_rewrap_evicted_accepted_wire():
    rig=RouteRig();state,record,wire=parked_history(rig)
    result=inactive_episode_history(rig.operator,rig.case['witness'].source,state.mission.mission_id,bytes.fromhex(record['goal']))
    assert result.source_state_raw==encode(state) and result.accepted_wire==wire
    assert rig.operator.episodes.failed  # Mission HOLD alone does not erase actual closed/inactive provenance.
    rig.provider.copies.clear()
    retained=inactive_episode_history(rig.operator,rig.case['witness'].source,state.mission.mission_id,bytes.fromhex(record['goal']))
    assert retained.source_state_raw==result.source_state_raw and retained.accepted_wire is None
    assert retained.snapshot_raw==result.snapshot_raw


@pytest.mark.parametrize('fault',['absence','active','parking-active','goal','frame'])
def test_unknown_active_other_goal_or_incompatible_history_cannot_become_exit_none(fault):
    rig=RouteRig();state,record,_=parked_history(rig);source=deepcopy(rig.case['witness'].source)
    goal=bytes.fromhex(record['goal'])
    if fault=='absence':rig.operator.episodes.registry=None
    elif fault=='active':rig.operator.episodes.registry=dict(snapshot=SimpleNamespace(core=SimpleNamespace(state=2)))
    elif fault=='parking-active':record.update(phase='EXECUTE',admitted=True,result='NONE')
    elif fault=='goal':goal=HASH('different-goal')
    else:source.odom_epoch=str(uuid4())
    with pytest.raises(RouteError):inactive_episode_history(rig.operator,source,state.mission.mission_id,goal)
    assert len(rig.provider.copies)==1 and rig.route.registry is None


def test_transition_does_not_create_none_or_change_world_hwm_from_registry_absence():
    rig=RouteRig();rig.begin();rig.register();assert rig.admit() is not None
    case=rig.current_case();value=schema.WindowTransitionRequest(major=1,request_nonce=uuid4().bytes,action=1,
        owner=rig.owner_context,query_id=rig.case['query'].query_id,query_sha256=rig.route.query['sha'],
        route_id=rig.route.registry['snapshot'].core.route_id,expected_registry_epoch=rig.route.epoch,
        expected_registry_revision=rig.route.revision,transition_id=uuid4().bytes,
        from_window_sha256=rig.route.registry['snapshot'].current_window_sha256,to_window_sha256=HASH('next-window'),
        from_world_sha256=case['witness'].world_raw_sha256,candidate_local_path_sha256=ZERO,witness=case['witness'],
        expected_source_binding_revision=1,phase_revision=1,extent=case['extent'])
    transition=RouteTransitionOwner(rig.route);before=dict(rig.data.world_hwm)
    with pytest.raises(RouteError) as result:transition.request(encode(value),rig.peer)
    assert result.value.reason==RouteReason.EPISODE_UNKNOWN
    assert rig.operator.episodes.registry is None and rig.data.world_hwm==before and transition.current is None


def test_home_status_has_exact_nine_fields_and_no_implicit_goal_or_departure():
    rig=RouteRig();goal=RouteGoalOwner(rig.route);before=rig.route.original_query()
    view=goal.status()
    assert set(view)=={'supported','goal_id','goal_revision','goal_sha256','geometry_scope_sha256',
        'registered','current_compatible','blocked_reason','return_ready_revision'}
    assert not view['registered'] and view['goal_id'] is None and view['goal_revision']=='0'
    assert view['return_ready_revision'] is None
    assert rig.route.original_query()==before and rig.route.registry is None and not rig.stop_calls


@pytest.mark.parametrize('fault',[None,'expired','moving','parking-on','goal-absent','mapper-loss','profile-loss'])
def test_return_revision_is_exposed_only_after_current_return_ready_recheck(fault):
    rig=RouteRig();goal=RouteGoalOwner(rig.route)
    goal.goal=rig.data.get(1,rig.case['query'].goal_sha256)
    rig.authority.value['current_operation']=dict(phase='WAIT',parking_enabled=False)
    rig.operator.stationary=lambda:fault!='moving'  # 명시한 physical STOP leaf; 실장치 증거가 아니다.
    body=schema.RouteEligibility(major=1,eligibility_id=rig.case['query'].query_id,purpose=2,
        owner=rig.owner_context,witness=rig.case['witness'],task_space_kind=1,eligible=True,reason=0,
        goal_sha256=goal.goal.digest,exit_space_sha256=ZERO,
        topology_sha256=rig.case['candidate'].core.topology_sha256,
        window_sha256=rig.case['candidate'].current_window_sha256,global_plan_sha256=ZERO,extent=rig.case['extent'])
    ref=rig.route.observe_eligibility(encode(body),rig.peer)
    if fault=='expired':rig.operator.now=ref.valid_until_pi_us
    elif fault=='parking-on':rig.authority.value['current_operation']['parking_enabled']=True
    elif fault=='goal-absent':goal.goal=None
    elif fault=='mapper-loss':rig.route.mapper_support=lambda:0
    elif fault=='profile-loss':rig.route.installed=lambda:False
    before=rig.route.original_query();revision=rig.route.ready_revision[2]
    view=goal.status()
    assert view['return_ready_revision']==(str(ref.ready_revision) if fault is None else None)
    assert (view['blocked_reason'] is None)==(fault is None)
    assert rig.route.ready_revision[2]==revision and rig.route.original_query()==before
    assert rig.route.registry is None and not rig.stop_calls
