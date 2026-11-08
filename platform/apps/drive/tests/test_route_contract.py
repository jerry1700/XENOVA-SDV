"""신규 route 계약의 typed 원문/C codec/hash 검증. 실제 권한·M/D·HIL 수락이 아니다."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import struct

import pytest
from xenova_drive_world import schema,encode,decode
import xenova_drive_world as contract
from xenova_drive_world.route import NIL,ZERO,NO_OUTGOING_EDGE,ROUTE_CAPS
from route_fixtures import route_case,accepted_route,uid,digest

HOME=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def route_probe(tmp_path_factory):
    build=tmp_path_factory.mktemp('route-native');binary=build/'route-probe';world=HOME/'world'
    sources=[world/'world.c',world/'generated/world.pb.c',*(world/'nanopb'/name for name in
        ('pb_common.c','pb_decode.c','pb_encode.c')),HOME/'tests/route_probe.c']
    argv=['cc','-std=c11','-g','-Wall','-Wextra','-Werror','-I'+str(world),'-I'+str(world/'nanopb'),
          '-I'+str(world/'generated'),*[str(x) for x in sources],'-lm','-o',str(binary)]
    subprocess.run(argv,check=True,capture_output=True,text=True,timeout=60)
    return binary,build


def native(probe,kind,raw):
    binary,build=probe;path=build/'input.bin';path.write_bytes(raw)
    result=subprocess.run([str(binary),kind,str(path)],check=True,capture_output=True,text=True,timeout=5)
    return json.loads(result.stdout)


def accepted(case=None):
    return accepted_route(case or route_case(),registry_epoch=uid(80),acceptance_nonce=uid(81),
        started_pi_us=1005000,meter_epoch=uid(82),meter_baseline_total_ticks=100,metres_per_tick_upper=.001)


def local_hash(case):
    c=case['candidate']
    return contract.route_local_path_sha256(case['state'],global_plan_sha256=c.core.global_plan_sha256,
        geometry_scope_sha256=c.core.geometry_scope_sha256,basis_window_sha256=c.basis_window_sha256,
        task_space_kind=c.task_space_kind,exit_space_sha256=c.exit_space_sha256,
        basis_revision=c.basis_window_revision,basis_prefix=case['records']['windows'][0].reference)


def mapper_witness(case):
    w,v=case['world'],case['vehicle'];origin=case['records']['sections'][0].origin
    from uuid import UUID
    return schema.MapperRouteWitness(mapper_boot=UUID(w.mapper_boot).bytes,mapper_instance=UUID(w.mapper_instance).bytes,
        clock_epoch=UUID(w.clock_epoch).bytes,pi_host_boot=UUID(v.host_boot).bytes,pi_client_instance=uid(83),
        clock_probe_sequence=1,clock_reply_sha256=origin.mapper_clock_reply_sha256,checked_mapper_us=w.completed_us,
        valid_until_pi_us=1100000,source_binding_revision=1,raw_binding_sha256=origin.raw_binding_sha256,
        vehicle_sequence=v.sequence,observation_sequence=w.observation_seq,frame_evidence_sha256=origin.frame_evidence_sha256,
        scene_admission_id=NIL,scene_revision=0)


def deliveries(case):
    r=case['records'];records=((1,r['goal']),(2,r['topology']),(3,r['sections'][0]),(4,r['connections'][0]),
        (5,r['windows'][0]),(6,r['exit_space']),(7,case['plan']),(8,r['scope']))
    for kind,record in records:
        data=schema.RoadData(major=1,publisher_kind=2 if kind==7 else 1,
            record_sha256=contract.route_record_sha256(kind,record))
        names={1:'start_goal',2:'road_topology',3:'road_section',4:'road_connection',5:'road_window',
               6:'exit_space',7:'route_plan',8:'road_geometry_scope'}
        getattr(data,names[kind]).CopyFrom(record)
        if kind==7:data.guest_owner.CopyFrom(case['owner']);data.guest_witness.CopyFrom(case['witness'])
        else:
            data.mapper.CopyFrom(mapper_witness(case))
            data.durable.CopyFrom(schema.RoadDurableRef(database_uuid=uid(84),job_id=uid(85),job_digest=digest(40),
                status=1,durable_revision=1,committed_mapper_us=case['world'].completed_us,
                origin_context_sha256=r['sections'][0].origin.origin_context_sha256,
                record_sha256=data.record_sha256,original_receipt_sha256=digest(41)))
        yield kind,record,data


def assert_roundtrip(probe,kind,value,expected_hash=None):
    raw=encode(value);assert decode(raw,type(value))==value
    result=native(probe,kind,raw)
    assert result['result']==0 and result['bytes']==len(raw) and result['sizeof']>0
    assert len(raw)<=result['generated_max']
    if expected_hash is not None:assert result['hashed'] and result['sha256']==expected_hash.hex()
    return result


def test_typed_records_original_body_hashes_and_native_codec(route_probe):
    case=route_case()
    for kind,record,data in deliveries(case):
        raw=encode(record);domain=('xenova.drive.route.record.v1/'+str(kind)).encode()+b'\0'
        expected=sha256(domain+len(raw).to_bytes(4,'little')+raw).digest()
        assert contract.route_record_sha256(kind,record)==contract.route_record_sha256(kind,raw)==expected
        assert_roundtrip(route_probe,'record-'+str(kind),record,expected)
        link=schema.DriveLink(major=2,road_data=data);wire=encode(link)
        assert contract.road_data_body(wire)==encode(data) and contract.road_record_body(encode(data))==raw
        assert native(route_probe,'link',wire)['result']==0
    # The observed slot connector is separate from the directed graph edge.
    connector=case['records']['connections'][1]
    assert connector.kind==3 and case['records']['topology'].edges[0].kind==1
    assert_roundtrip(route_probe,'record-4',connector,contract.route_record_sha256(4,connector))



@pytest.mark.parametrize('kind,field,name',[(3,10,'point_error_m'),(4,13,'overlap_error_m'),(5,12,'point_error_m')])
def test_numeric_arrays_have_one_cross_language_packed_canonical_body(route_probe,kind,field,name):
    records=route_case()['records']
    value={3:records['sections'][0],4:records['connections'][0],5:records['windows'][0]}[kind]
    assert value.DESCRIPTOR.fields_by_name[name].GetOptions().packed
    raw=encode(value);items=getattr(value,name)
    payload=struct.pack('<'+'f'*len(items),*items)
    assert 0<len(payload)<128 and field<16
    packed=bytes([field*8+2,len(payload)])+payload
    assert raw.count(packed)==1
    assert_roundtrip(route_probe,'record-'+str(kind),value,contract.route_record_sha256(kind,value))
    # Protobuf can parse both forms, but source identity has exactly one body.
    unpacked=b''.join(bytes([field*8+5])+payload[i:i+4] for i in range(0,len(payload),4))
    noncanonical=raw.replace(packed,unpacked,1)
    parsed=type(value)();parsed.ParseFromString(noncanonical)
    assert parsed==value
    with pytest.raises(ValueError):decode(noncanonical,type(value))
    assert native(route_probe,'record-'+str(kind),noncanonical)['result']!=0
    # Parser mergeability does not allow split canonical arrays, truncated
    # float elements, or one packed field concealing an excessive count.
    split=bytes([field*8+2,4])+payload[:4]+bytes([field*8+2,len(payload)-4])+payload[4:]
    partial=bytes([field*8+2,len(payload)-1])+payload[:-1]
    excessive_payload=payload+struct.pack('<f',0.)*33
    def uint(value):
        body=bytearray()
        while value>127:body.append((value&127)|128);value>>=7
        return bytes(body)+bytes([value])
    excessive=bytes([field*8+2])+uint(len(excessive_payload))+excessive_payload
    for malformed in (split,partial,excessive):
        modified=raw.replace(packed,malformed,1)
        with pytest.raises(ValueError):decode(modified,type(value))
        assert native(route_probe,'record-'+str(kind),modified)['result']!=0


def test_query_candidate_accepted_core_receipt_and_source_hash_pairing(route_probe):
    case=accepted()
    for key,helper in (('query',contract.route_query_sha256),('candidate',contract.route_candidate_sha256),
        ('owner',contract.route_owner_sha256),('witness',contract.route_witness_sha256),
        ('receipt',contract.route_receipt_sha256)):
        assert_roundtrip(route_probe,key,case[key],helper(case[key]))
    assert_roundtrip(route_probe,'core',case['snapshot'].core,contract.route_core_sha256(case['snapshot'].core))
    assert_roundtrip(route_probe,'source',case['witness'].source,contract.route_source_key_sha256(case['witness'].source))
    assert_roundtrip(route_probe,'permission',case['grant'],contract.route_permission_sha256(case['grant']))
    assert_roundtrip(route_probe,'snapshot',case['snapshot'])
    assert_roundtrip(route_probe,'state',case['state'])
    exchange=schema.RouteExchange(major=1,route_candidate=case['candidate'])
    link=schema.DriveLink(major=2,route_exchange=exchange);raw=encode(link)
    assert contract.route_exchange_body(raw)==encode(exchange)
    assert contract.route_candidate_body(raw)==encode(case['candidate'])
    assert native(route_probe,'link',raw)['result']==0


def test_local_path_commitment_avoids_acceptance_cycle_and_current_reference_revision(route_probe):
    base=route_case();before=local_hash(base)
    assert before==base['candidate'].core.local_path_sha256  # No future grant/ref is required.
    case=accepted(base)
    assert native(route_probe,'local',encode(case['state']))['sha256']==before.hex()
    case['state'].reference_revision+=1
    assert local_hash(case)==before and native(route_probe,'local',encode(case['state']))['sha256']==before.hex()
    for change in ('point','frame','basis','plan'):
        candidate=deepcopy(case)
        if change=='point':candidate['state'].path[1].pose.x+=.01
        elif change=='frame':candidate['state'].frame_epoch='00000000-0000-0000-0000-000000000099'
        elif change=='basis':candidate['candidate'].basis_window_revision+=1
        else:candidate['candidate'].core.global_plan_sha256=digest(42)
        assert local_hash(candidate)!=before


def test_rejected_receipt_needs_no_fabricated_snapshot_target_or_none(route_probe):
    case=route_case();request=case['candidate']
    receipt=schema.RouteReceipt(major=1,request_nonce=request.request_nonce,
        request_sha256=contract.route_candidate_sha256(request),result=2,reason=16,
        request_sequence=1,query_id=request.query_id,query_sha256=request.query_sha256)
    assert_roundtrip(route_probe,'receipt',receipt,contract.route_receipt_sha256(receipt))
    for change in ('accepted-without-both','target-only','unknown-snapshot'):
        bad=deepcopy(receipt)
        if change=='accepted-without-both':bad.result=1
        elif change=='target-only':bad.initial_target_context.CopyFrom(case['owner'])
        else:
            bad.snapshot.CopyFrom(accepted(case)['snapshot']);bad.snapshot.state=0
        with pytest.raises(ValueError):encode(bad)
        assert native(route_probe,'receipt',bad.SerializeToString())['result']!=0


def test_unknown_duplicate_and_noncanonical_new_bodies_are_rejected_but_legacy_is_separate(route_probe):
    case=route_case();raw=encode(case['query'])
    for bad in (raw+b'\xf8\x07\x01',raw+b'\x08\x01',b'\x08\x81\x00'+raw[2:]):
        with pytest.raises(ValueError):decode(bad,schema.RouteQuery)
        assert native(route_probe,'query',bad)['result']!=0
    # Original optional-unknown behavior is retained outside new route subtrees.
    from test_world import modern_world
    old=modern_world();assert decode(encode(old)+b'\xf8\x07\x01')==old


def test_world_and_state_features_require_exact_presence_and_kind3_binding(route_probe):
    case=accepted()
    assert_roundtrip(route_probe,'view',case['world'].route_view)
    for target,field,new in (('world','required_features',11),('state','state_features',47),('grant','kind',1)):
        value=deepcopy(case[target]);setattr(value,field,new)
        with pytest.raises(ValueError):encode(value)
    value=deepcopy(case['state']);value.ClearField('route')
    with pytest.raises(ValueError):encode(value)
    assert native(route_probe,'state',value.SerializeToString())['result']!=0
    # No synthetic NONE is emitted to upgrade legacy support.
    assert not case['state'].HasField('episode')


def test_final_outgoing_sentinel_supports_single_section_no_edge_and_rejects_middle(route_probe):
    value=route_case()['plan'];del value.occurrences[1:];value.occurrences[0].edge_index=NO_OUTGOING_EDGE
    assert_roundtrip(route_probe,'record-7',value,contract.route_record_sha256(7,value))
    for final_real in (False,True):
        bad=route_case()['plan'];bad.occurrences[0 if not final_real else -1].edge_index=NO_OUTGOING_EDGE if not final_real else 0
        with pytest.raises(ValueError):encode(bad)
        assert native(route_probe,'record-7',bad.SerializeToString())['result']!=0
    bad=route_case()['records']['topology'];bad.edges[0].kind=3
    with pytest.raises(ValueError):encode(bad)
    assert native(route_probe,'record-2',bad.SerializeToString())['result']!=0


def test_slot_connector_has_real_same_scope_road_anchor_and_cannot_be_graph_edge(route_probe):
    original=route_case()['records']['connections'][1]
    for field,value in (('to_section_sha256',digest(44)),('to_lane_id',1),('to_point_index',2)):
        bad=deepcopy(original);setattr(bad,field,value)
        with pytest.raises(ValueError):encode(bad)
        assert native(route_probe,'record-4',bad.SerializeToString())['result']!=0


def test_finish_keeps_original_birth_budget_meter_and_phase_revision_in_closed_proof(route_probe):
    case=accepted();snapshot=case['snapshot'];old=deepcopy(snapshot.core)
    finish=deepcopy(case['candidate']);finish.action=3;finish.request_nonce=uid(88);finish.request_sequence=2
    finish.expected_registry_epoch=snapshot.acceptance.registry_epoch;finish.expected_registry_revision=1
    finish.expected_route_revision=old.revision;finish.core.CopyFrom(old);finish.core.revision+=1
    finish.core.phase=5;finish.core.terminal_result=1;finish.began_guest_us=finish.phase_started_guest_us=0
    finish.owner.CopyFrom(case['target'])
    snapshot.state=3;snapshot.core.CopyFrom(finish.core)
    snapshot.acceptance.request_nonce=finish.request_nonce;snapshot.acceptance.request_sequence=finish.request_sequence
    snapshot.acceptance.request_sha256=contract.route_candidate_sha256(finish)
    snapshot.acceptance.accepted_core_sha256=contract.route_core_sha256(snapshot.core)
    snapshot.acceptance.origin_owner_sha256=contract.route_owner_sha256(finish.owner)
    assert_roundtrip(route_probe,'candidate',finish,contract.route_candidate_sha256(finish))
    assert_roundtrip(route_probe,'snapshot',snapshot)
    assert snapshot.core.phase_revision==old.phase_revision and snapshot.core.started_pi_us==old.started_pi_us
    assert snapshot.core.deadline_pi_us==old.deadline_pi_us and snapshot.core.meter_epoch==old.meter_epoch
    for field in ('accepted_core_sha256','request_sha256'):
        bad=deepcopy(snapshot);setattr(bad.acceptance,field,ZERO)
        with pytest.raises(ValueError):encode(bad)
        assert native(route_probe,'snapshot',bad.SerializeToString())['result']!=0


def test_legal_maximum_arrays_stay_bounded_with_real_generated_limits(route_probe):
    case=route_case();state=case['state'];state.ClearField('path');state.ClearField('segments')
    for i in range(512):state.path.add(pose=schema.Pose(x=.3+i*.001,y=2.,yaw=0.),steering_rad=0.,velocity_mps=.1 if i<511 else 0.)
    state.segments.add(first_index=0,last_index=511,signed_direction=1);state.merge_goal.CopyFrom(state.path[-1].pose)
    case['candidate'].core.local_path_sha256=local_hash(case)
    result=accepted(case);assert_roundtrip(route_probe,'state',result['state'])
    assert native(route_probe,'local',encode(result['state']))['sha256']==case['candidate'].core.local_path_sha256.hex()
    topology=deepcopy(case['records']['topology']);template=deepcopy(topology.sections[0]);topology.ClearField('sections')
    for i in range(32):
        section=topology.sections.add();section.CopyFrom(template);section.section_id=uid(200+i);section.order=i
    topology.ClearField('edges')
    for i in range(96):topology.edges.add(from_section_index=i%31,to_section_index=i%31+1,from_lane_id=2,
        to_lane_id=2,kind=1,from_station_m=2.,to_station_m=0.,connection_sha256=digest(45),closes_circuit=False)
    assert_roundtrip(route_probe,'record-2',topology,contract.route_record_sha256(2,topology))
    plan=deepcopy(case['plan']);plan.ClearField('occurrences')
    for i in range(96):plan.occurrences.add(section_index=i%32,lane_id=2,entry_station_m=0.,exit_station_m=1.,
        edge_index=i if i<95 else NO_OUTGOING_EDGE)
    assert_roundtrip(route_probe,'record-7',plan,contract.route_record_sha256(7,plan))
    # These are legal structural vectors, not a claim that all maxima form one traversable route.
    bounds=native(route_probe,'snapshot',encode(result['snapshot']))
    assert bounds['generated_max']==1012 and bounds['bytes']<=ROUTE_CAPS['RouteSnapshot']==1024
    topology.sections.add().CopyFrom(template)
    with pytest.raises(ValueError):encode(topology)


def test_current_extent_is_typed_evidence_and_does_not_make_permission(route_probe):
    case=route_case();request=case['candidate']
    eligibility=schema.RouteEligibility(major=1,eligibility_id=uid(90),purpose=2,owner=case['owner'],
        witness=case['witness'],task_space_kind=1,goal_sha256=request.core.goal_sha256,exit_space_sha256=ZERO,
        topology_sha256=request.core.topology_sha256,window_sha256=request.current_window_sha256,
        global_plan_sha256=ZERO,eligible=True,reason=0,extent=request.extent)
    assert_roundtrip(route_probe,'eligibility',eligibility,contract.route_eligibility_sha256(eligibility))
    assert not eligibility.global_plan_sha256.strip(b'\0')  # future path/grant is not a prerequisite
    bad=deepcopy(eligibility);bad.witness.source.map_epoch='00000000-0000-0000-0000-000000000097'
    with pytest.raises(ValueError):encode(bad)
    assert native(route_probe,'eligibility',bad.SerializeToString())['result']!=0


def test_transition_current_witness_and_management_hash_domains_pair_native(route_probe):
    case=route_case();c=case['candidate'];mapper=mapper_witness(case)
    request=schema.WindowTransitionRequest(major=1,request_nonce=uid(91),action=1,owner=case['owner'],
        query_id=case['query'].query_id,query_sha256=contract.route_query_sha256(case['query']),route_id=c.core.route_id,
        expected_registry_epoch=uid(80),expected_registry_revision=1,transition_id=uid(92),
        from_window_sha256=c.current_window_sha256,to_window_sha256=digest(47),from_world_sha256=c.witness.world_raw_sha256,
        candidate_local_path_sha256=ZERO,witness=case['witness'],expected_source_binding_revision=1,
        phase_revision=1,extent=c.extent)
    receipt=schema.WindowTransitionReceipt(major=1,request_nonce=request.request_nonce,
        request_sha256=contract.route_transition_request_sha256(request),transition_id=request.transition_id,
        state=1,from_window_sha256=request.from_window_sha256,to_window_sha256=request.to_window_sha256,
        active_window_sha256=request.from_window_sha256,source_binding_revision=1,activated_world_sha256=ZERO,
        activated_world_sequence=0,mapper=mapper,reason=0,transition_generation=1,valid_until_pi_us=1090000)
    lookup=schema.RoadDataRequest(major=1,request_nonce=uid(93),record_kind=5,
        record_sha256=c.current_window_sha256,owner=case['owner'],valid_until_pi_us=1090000)
    world=schema.MapperWorldWitness(major=1,mapper=mapper,source=case['witness'].source,
        world_raw_sha256=case['witness'].world_raw_sha256)
    for key,value,helper in (('transition_request',request,contract.route_transition_request_sha256),
        ('transition_receipt',receipt,contract.route_transition_receipt_sha256),
        ('data_request',lookup,contract.route_data_request_sha256),('world_witness',world,contract.route_world_witness_sha256)):
        assert_roundtrip(route_probe,key,value,helper(value))
    # PREPARE/ABORT never fabricate a fresh World commit.
    receipt.state=4
    assert_roundtrip(route_probe,'transition_receipt',receipt,contract.route_transition_receipt_sha256(receipt))
    receipt.activated_world_sequence=1
    with pytest.raises(ValueError):encode(receipt)
    assert native(route_probe,'transition_receipt',receipt.SerializeToString())['result']!=0


def test_unknown_status_has_no_fabricated_none_or_acceptance_and_requires_capability(route_probe):
    from test_input_clock import reply
    status=schema.RouteStatus(major=1,registry_epoch=NIL,registry_revision=0,route_id=NIL,
        accepted_core_sha256=ZERO,global_plan_sha256=ZERO,local_path_sha256=ZERO,route_state=0,commit_state=0,
        reason=2,valid_until_pi_us=6010000,state_message_id=NIL,state_sha256=ZERO,state_revision=0,
        plan_revision=0,phase_revision=0,calculation_hwm=0,vehicle_hwm=0,current_permission_sha256=ZERO,
        current_window_sha256=ZERO,terminal_result=0)
    assert_roundtrip(route_probe,'status',status)
    # Current capability is a consumer check too; an absent advertisement is not support.
    link=reply();link.clock.route_status.CopyFrom(status)
    link.clock.guest_clock_valid_until_pi_us=6020000
    link.clock.probe.supported_state_features=127
    raw=encode(link);assert native(route_probe,'link',raw)['result']==0
    link.clock.probe.ClearField('supported_state_features')
    with pytest.raises(ValueError):encode(link)
    assert native(route_probe,'link',link.SerializeToString())['result']!=0
    bad=deepcopy(status);bad.route_state=1
    with pytest.raises(ValueError):encode(bad)
    assert native(route_probe,'status',bad.SerializeToString())['result']!=0


def test_registration_original_stationary_facts_and_current_vehicle_are_bound(route_probe):
    case=route_case();v=deepcopy(case['vehicle'])
    v.sequence+=1;v.source_sample+=1;v.source_us+=20000;v.pi_sample_us+=20000;v.velocity_mps=0.
    stop=schema.StopFacts(state=3,pi_session=v.pi_session,device_session=v.device_session,command_sequence=1,
        control_generation=0,producer_role=0,source_vm=NIL,source_execution=NIL,source_app_instance=NIL,
        calculation_sequence=0,counter=1,applied_report_sequence=1,applied_source_sample=5,
        applied_pi_lower_us=900000,applied_pi_upper_us=900010,stationary_first_sequence=8,stationary_last_sequence=v.sequence,
        stationary_started_pi_us=920000,stationary_observed_pi_us=v.pi_sample_us-10000,valid_until_pi_us=1100000,
        left_speed_bound_mps=.001,right_speed_bound_mps=.001,yaw_rate_bound_rps=.001,
        plan_revision=0,phase_revision=0,mission_id=NIL,mission_revision=0,grant_revision=0,
        driver_session=NIL,driver_action_seq=0,driver_state_seq=0)
    facts=schema.DriveControlFacts(major=1,meter=schema.PiAbsoluteMeter(meter_epoch=uid(82),total_ticks=100,
        metres_per_tick=.001,gap_revision=0,valid=True,vehicle_sequence=v.sequence),stop=stop,valid_until_pi_us=1090000)
    request=schema.RoadRegistrationRequest(major=1,request_nonce=uid(94),owner=case['owner'],expected_goal_revision=0,
        expected_goal_sha256=ZERO,world_raw_sha256=case['witness'].world_raw_sha256,vehicle=v,facts=facts,
        issued_pi_us=v.pi_sample_us+1,valid_until_pi_us=1080000)
    assert_roundtrip(route_probe,'facts',facts,contract.route_stop_facts_sha256(facts))
    assert_roundtrip(route_probe,'registration_request',request,contract.route_registration_request_sha256(request))
    for field in ('sequence','pi_session'):
        bad=deepcopy(request)
        if field=='sequence':bad.vehicle.sequence+=1
        else:bad.vehicle.pi_session='00000000-0000-0000-0000-000000000096'
        with pytest.raises(ValueError):encode(bad)
        assert native(route_probe,'registration_request',bad.SerializeToString())['result']!=0


def test_nested_new_snapshot_bytes_are_checked_before_legacy_parent_can_discard_them(route_probe):
    case=accepted()
    def uint(value):
        raw=bytearray()
        while value>127:raw.append((value&127)|128);value>>=7
        return bytes(raw)+bytes([value])
    def patch(raw,path):
        at=0;parts=[]
        def read():
            nonlocal at
            result=shift=0
            while True:
                byte=raw[at];at+=1;result|=(byte&127)<<shift
                if byte<128:return result
                shift+=7
        while at<len(raw):
            start=at;tag=read();kind=tag&7
            if kind==0:read();parts.append(raw[start:at]);continue
            if kind==2:size=read()
            else:size=8 if kind==1 else 4
            body=raw[at:at+size];at+=size
            if tag>>3==path[0]:
                assert kind==2
                body=patch(body,path[1:]) if len(path)>1 else body+b'\xf8\x07\x01'
                parts.append(uint(tag)+uint(len(body))+body)
            else:parts.append(raw[start:at])
        return b''.join(parts)
    state=encode(case['state']);bad=patch(state,(35,))
    with pytest.raises(ValueError):decode(bad,schema.DriveState)
    assert native(route_probe,'state',bad)['result']!=0
    for payload,path in ((schema.DriveLink(major=2,snapshot=case['state']),(4,35)),
        (schema.DriveLink(major=2,accepted=schema.AcceptedState(host_boot=case['vehicle'].host_boot,
            gateway_instance=case['vehicle'].gateway_instance,state=case['state'])),(5,3,35))):
        assert native(route_probe,'link',encode(payload))['result']==0
        bad=patch(encode(payload),path)
        with pytest.raises(ValueError):decode(bad,schema.DriveLink)
        assert native(route_probe,'link',bad)['result']!=0
    bad=patch(encode(case['world']),(42,))
    with pytest.raises(ValueError):decode(bad)
