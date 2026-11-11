"""원 accepted parking/episode 이력의 읽기 포트. UNKNOWN을 NONE로 발급하지 않는다.

같은 서비스 RAM의 실제 accepted State/parking execution과 current registry가 결박된
inactive 사실만 반환한다. 현재 P4/Vehicle/meter/Scene/coverage 승인은 호출자가 따로 한다.
"""
from dataclasses import dataclass
from hashlib import sha256

from xenova_drive_world import schema,encode,decode,episode_core_sha256
from .route_policy import RouteError,RouteReason,require


@dataclass(frozen=True)
class InactiveEpisodeHistory:
    snapshot_raw: bytes
    accepted_core_sha256: bytes
    source_state_raw: bytes
    source_state_sha256: bytes
    accepted_wire: bytes | None


def inactive_episode_history(operator,source,parking_attempt_id=None,parking_goal_sha256=None):
    """HOLD의 failed와 확정 이력을 나눈다. current ACTIVE/absence에 old terminal을 대입하지 않는다."""
    episode=operator.episodes;registry=episode.registry
    require(registry is not None,RouteReason.EPISODE_UNKNOWN)
    snapshot=registry['snapshot']
    require(snapshot.core.state==1,RouteReason.ACTIVE_EPISODE)
    require(episode_core_sha256(snapshot.core)==snapshot.acceptance.accepted_core_sha256,
        RouteReason.HASH_OR_BODY_MISMATCH)
    parking_record=None
    if parking_attempt_id is not None:
        from uuid import UUID
        operation=operator.runner.authority.value.get('current_operation')
        current=operator.missions.current()
        retained=None if operation is None else operation.get('parking_attempt')
        for item in (retained,current):
            if item is not None and item['kind']==2 and UUID(item['id']).bytes==parking_attempt_id:
                parking_record=item;break
        require(parking_record is not None and parking_record['phase']=='HOLD' and not parking_record['admitted']
            and parking_record['result'] in ('PARKED','CANCELLED','FAILED'),RouteReason.EPISODE_UNKNOWN)
        if parking_goal_sha256 is not None:
            require(parking_record['goal'] is not None and bytes.fromhex(parking_record['goal'])==parking_goal_sha256,
                RouteReason.HASH_OR_BODY_MISMATCH)
    candidates=[];provider=operator.runner.drive_link
    parking=operator.missions.execution
    if parking is not None:candidates.append((bytes(parking[0]),None))
    for saved in provider.copies.values():
        accepted=decode(saved[0],schema.DriveLink).accepted
        if accepted.host_boot!=operator.runner.authority.host or accepted.gateway_instance!=operator.instance:continue
        candidates.append((encode(accepted.state),bytes(saved[0])))
    for raw,wire in candidates:
        state=decode(raw,schema.DriveState)
        if (not state.HasField('episode') or state.episode.core.state!=1
                or state.episode.acceptance!=snapshot.acceptance
                or episode_core_sha256(state.episode.core)!=snapshot.acceptance.accepted_core_sha256):continue
        if parking_attempt_id is not None and (not state.HasField('mission') or state.mission.kind!=2
                or state.mission.mission_id!=parking_attempt_id or state.mission.mission_revision!=parking_record['revision']):continue
        if (any(getattr(state,k)!=getattr(source,k) for k in
                ('frame_epoch','map_epoch','odom_epoch','configuration','calibration'))
                or state.primitive!=source.primitive.hex()):continue
        digest=sha256(raw).digest()
        # Prefer the actually retained AcceptedState wire; do not reconstruct a
        # current wrapper around an old source if the bounded copy was evicted.
        original=wire
        for item in provider.copies.values():
            accepted=decode(item[0],schema.DriveLink).accepted
            if (accepted.host_boot==operator.runner.authority.host and accepted.gateway_instance==operator.instance
                    and sha256(encode(accepted.state)).digest()==digest):original=bytes(item[0]);break
        return InactiveEpisodeHistory(encode(state.episode),snapshot.acceptance.accepted_core_sha256,
            raw,digest,original)
    raise RouteError(RouteReason.EPISODE_UNKNOWN)
