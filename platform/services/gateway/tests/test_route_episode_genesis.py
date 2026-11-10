"""실제 EP genesis 선택과 kind3 미션 HWM. route phase/기존 accepted history는 선언된 leaf다.

EXIT search/전환/apply를 실행한 증거가 아니다. 없는 EP를 출력 권한으로 승격하지 않는 선택 경계다.
"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID,uuid4

from xenova_drive_world import schema,encode
from xenova_gateway.episode import EpisodeOwner,none_snapshot
from xenova_gateway.operation_owner import OperationOwner
from xenova_gateway.route_episode import inactive_episode_history
from xenova_gateway.route_goal import RouteGoalOwner
from xenova_gateway.route_operator import RouteOperator
from xenova_gateway.route_transition import RouteTransitionOwner
from route_runtime_fixture import RouteRig


def setup():
    rig=RouteRig();rig.begin();rig.register();assert rig.admit() is not None
    o=rig.operator;r=o.runner;current=dict(id=str(UUID(bytes=rig.grant.mission_id)),revision=rig.grant.mission_revision,
        kind=3,admitted=True,phase='FOLLOW');old=dict(current,id=str(uuid4()),kind=1)
    o.missions.current=lambda:old;o.missions.execution=None
    episode=EpisodeOwner(o);episode.mission_key=(old['id'],old['revision']);episode.failed=True
    prior=none_snapshot(uuid4().bytes,1)
    episode.registry=dict(snapshot=prior)
    o.episodes=episode;o.missions.current=lambda:current;o.ready=True
    r._durable_available=lambda:True;r.operation_support=True;r.route_profile=rig.route.policy
    o.operations=OperationOwner(o)
    facade=RouteOperator(o);o.routes=facade
    facade.attach(rig.route,RouteGoalOwner(rig.route),RouteTransitionOwner(rig.route),submit_management=lambda *args:False)
    # 원 서비스에서 수락한 이전 FOLLOW/EP NONE라는 typed source leaf다. current grant로 재포장하지 않는다.
    state=deepcopy(rig.state);state.ClearField('route');state.state_features&=~64
    state.episode.CopyFrom(prior);state.state_features|=16
    state.mission.ClearField('route_binding');state.mission.kind=1;state.mission.phase=10
    state.mission.mission_id=UUID(old['id']).bytes;state.mission.mission_deadline_pi_us=0
    original=schema.DriveLink(major=2)
    original.accepted.CopyFrom(schema.AcceptedState(host_boot=rig.vehicle.host_boot,gateway_instance=o.instance,state=state))
    rig.provider.copies[state.message_id]=(encode(original),)
    facade.predecessor=inactive_episode_history(o,rig.case['witness'].source)
    return rig,episode,facade,current


def test_exit_and_merge_preserve_inactive_origin_then_first_registered_road_initializes_once_without_future_state_gate():
    rig,episode,facade,current=setup();previous=episode.registry;key=episode.mission_key
    snapshot=rig.route.registry['snapshot'];original=deepcopy(snapshot)
    # These are registered-phase choice inputs only, not legal EXIT codec/fullState witnesses.
    snapshot.task_space_kind=2;snapshot.core.phase=1
    episode.sync_mission();assert episode.registry is previous and episode.mission_key==key
    snapshot.core.phase=2
    episode.sync_mission();assert episode.registry is previous and episode.failed
    snapshot.CopyFrom(original);rig.route.registry['state']=None;rig.route.registry['applied'].clear()
    episode.sync_mission()
    assert episode.mission_key==(current['id'],current['revision']) and not episode.failed
    assert episode.registry is not previous and episode.registry['snapshot'].core.state==1
    assert not episode.registry['state'] and not episode.registry['applied']
    first=episode.registry;raw=encode(first['snapshot'])
    episode.sync_mission();assert episode.registry is first and encode(first['snapshot'])==raw
    # A/B/grant rebind does not create another mission_key or refund an Episode.
    rig.owner_context.control_generation+=1;rig.owner_context.grant_revision+=1
    episode.sync_mission();assert episode.registry is first


def test_same_mission_unknown_or_active_is_not_replaced_and_restart_does_not_issue_none():
    rig,episode,facade,current=setup();episode.sync_mission()
    episode.registry=None;episode.failed=True
    episode.sync_mission();assert episode.registry is None and episode.failed
    active=dict(snapshot=SimpleNamespace(core=SimpleNamespace(state=2)))
    episode.registry=active
    episode.sync_mission();assert episode.registry is active  # Existing key is checked before genesis selection.
    restarted=EpisodeOwner(rig.operator)
    restarted.sync_mission()
    assert restarted.registry is None and restarted.startup_key==(current['id'],current['revision'])
