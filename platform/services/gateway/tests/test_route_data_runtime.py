"""실제 route codec/cache의 원문·유한 보관 의미. M publication/D membership은 typed leaf다.

M/D·native C·physical/현재권한의 종단 성공으로 세지 않는다. 공통 양성 geometry
factory를 사용하며 제품 parser를 풀거나 잘못된 body를 READY로 승격하지 않는다.
"""
from hashlib import sha256
from uuid import UUID

import pytest

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import RouteReason,route_record_sha256
from xenova_gateway.route_data import RoadDataOwner,KINDS
from xenova_gateway.route_policy import RouteBudgetPolicy,RouteError
from route_fixtures import route_case


HASH=lambda label:sha256(('typed-route-unit:'+label).encode()).digest()
IDS=lambda number:UUID(int=number).bytes
FIELDS={'StartGoal':'start_goal','RoadTopology':'road_topology','RoadSection':'road_section',
    'RoadConnection':'road_connection','RoadWindow':'road_window','ExitSpace':'exit_space',
    'RoutePlan':'route_plan','RoadGeometryScope':'road_geometry_scope'}


def records(case):
    for value in case['records'].values():
        yield from value if isinstance(value,(list,tuple)) else (value,)
    yield case['plan']


def mapper(case):
    source=case['witness'].source;owner=case['owner'];vehicle=case['vehicle']
    return schema.MapperRouteWitness(mapper_boot=UUID(source.mapper_boot).bytes,
        mapper_instance=UUID(source.mapper_instance).bytes,clock_epoch=UUID(source.clock_epoch).bytes,
        pi_host_boot=UUID(vehicle.host_boot).bytes,pi_client_instance=UUID(vehicle.gateway_instance).bytes,
        clock_probe_sequence=1,clock_reply_sha256=HASH('mapper-clock'),checked_mapper_us=1,
        valid_until_pi_us=case['witness'].valid_until_pi_us,source_binding_revision=1,
        raw_binding_sha256=HASH('mapper-binding'),vehicle_sequence=vehicle.sequence,
        observation_sequence=source.observation_sequence,frame_evidence_sha256=HASH('frame'),
        scene_admission_id=owner.scene_admission_id,scene_revision=owner.scene_revision)


def delivery(case,record):
    field=FIELDS[record.DESCRIPTOR.name];kind=KINDS[field];digest=route_record_sha256(kind,record)
    body=schema.RoadData(major=1,publisher_kind=2 if kind==7 else 1,record_sha256=digest)
    getattr(body,field).CopyFrom(record)
    if kind==7:
        body.guest_owner.CopyFrom(case['owner']);body.guest_witness.CopyFrom(case['witness'])
    else:
        body.mapper.CopyFrom(mapper(case));scope=case['records']['scope']
        origin=record.origin.origin_context_sha256 if 'origin' in record.DESCRIPTOR.fields_by_name else scope.origin_storage_context_sha256
        body.durable.CopyFrom(schema.RoadDurableRef(database_uuid=scope.dataset_uuid,job_id=IDS(101),job_digest=HASH('job'),
            status=1,durable_revision=1,committed_mapper_us=1,origin_context_sha256=origin,
            record_sha256=digest,original_receipt_sha256=HASH('original-commit-receipt')))
    return encode(body)


def data_owner(*,mapper_allowed=True,durable_allowed=True):
    return RoadDataOwner(mapper_current=lambda value,peer,now:mapper_allowed and now<value.valid_until_pi_us,
        guest_current=lambda owner,witness,peer,now:now<witness.valid_until_pi_us,
        durable_membership=lambda ref,kind,raw:durable_allowed and ref.original_receipt_sha256==HASH('original-commit-receipt'),
        world_current=lambda value,peer,now:True)


def policy(case):
    return RouteBudgetPolicy(120000000,50.,30000000,10000000,90000000,10000000,2000000,200000,
        10000,96,1,HASH('extent-policy'),case['plan'].terminal_profile_sha256,
        case['candidate'].core.budget_profile_sha256)


def test_static_original_delivery_and_record_body_survive_new_current_witness_without_rehashing_geometry():
    case=route_case();data=data_owner();record=case['records']['goal'];wire=delivery(case,record)
    saved=data.receive(wire,('typed-M',),1);digest=saved.digest;original=saved.original_body
    refreshed=decode(wire,schema.RoadData);refreshed.mapper.checked_mapper_us+=1
    newer=encode(refreshed);same=data.receive(newer,('typed-M',),1)
    assert same.digest==digest and same.original_body==original and same.original_envelope==wire
    assert data.original_delivery(1,digest)==newer and record.origin==same.message().origin


@pytest.mark.parametrize('mapper_allowed,durable_allowed,reason',[
    (False,True,RouteReason.SOURCE_UNAVAILABLE),(True,False,RouteReason.DURABLE_NOT_READY)])
def test_mapper_or_original_d_membership_failure_never_adopts_record(mapper_allowed,durable_allowed,reason):
    case=route_case();data=data_owner(mapper_allowed=mapper_allowed,durable_allowed=durable_allowed)
    with pytest.raises(RouteError) as result:data.receive(delivery(case,case['records']['goal']),('typed-M',),1)
    assert result.value.reason==reason and not data.records


def test_pinned_current_goal_is_not_evicted_by_another_legal_registration():
    case=route_case();data=data_owner();first=data.receive(delivery(case,case['records']['goal']),('typed-M',),1)
    data.pin((first.digest,));other=type(case['records']['goal'])();other.CopyFrom(case['records']['goal'])
    other.revision+=1;wire=delivery(case,other)
    with pytest.raises(RouteError) as result:data.receive(wire,('typed-M',),1)
    assert result.value.reason==RouteReason.POOL_BUSY
    assert data.get(1,first.digest).original_body==first.original_body


def test_record_digest_mismatch_is_rejected_at_actual_codec_boundary():
    case=route_case();body=decode(delivery(case,case['records']['goal']),schema.RoadData)
    body.start_goal.revision+=1
    # Serialize only to express a corrupt inbound wire; the normal encoder must
    # reject it before cache admission as well. No relaxed product codec.
    raw=body.SerializeToString(deterministic=True)
    with pytest.raises(ValueError):data_owner().receive(raw,('typed-M',),1)


def test_route_install_policy_has_no_implicit_default_and_does_not_accept_more_laps():
    case=route_case();installed=policy(case)
    assert installed.phase_cap(1)==30000000 and installed.phase_cap(3)==90000000
    values={key:getattr(installed,key) for key in installed.__dataclass_fields__}
    values['max_laps']=2
    with pytest.raises(RouteError):RouteBudgetPolicy(**values)
    with pytest.raises(RouteError):RouteBudgetPolicy.from_registered({},HASH('not-installation-proof'))


@pytest.mark.parametrize('wrong',['ack-only','different-original'])
def test_world_management_ack_or_different_published_bytes_do_not_move_actual_world_hwm(wrong):
    case=route_case();data=data_owner();value=schema.MapperWorldWitness(major=1,mapper=mapper(case),
        source=case['witness'].source,world_raw_sha256=case['witness'].world_raw_sha256)
    other=type(case['world'])();other.CopyFrom(case['world']);other.sequence+=1
    data.world_current=lambda value,peer,now:True if wrong=='ack-only' else encode(other)
    with pytest.raises(RouteError):data.receive_world(encode(value),('typed-M',),1)
    assert not data.worlds and not data.world_hwm
    data.world_current=lambda value,peer,now:encode(case['world'])
    data.receive_world(encode(value),('typed-M',),1)
    assert encode(data.world_snapshot(value.world_raw_sha256,1))==encode(case['world'])
    changed=type(case['world'])();changed.CopyFrom(case['world']);changed.completed_us+=1
    conflict=type(value)();conflict.CopyFrom(value);conflict.world_raw_sha256=sha256(encode(changed)).digest()
    data.world_current=lambda value,peer,now:encode(changed)
    # Same source+sequence is not a second publication with different original bytes.
    with pytest.raises(RouteError):data.receive_world(encode(conflict),('typed-M',),1)
    assert len(data.worlds)==1
