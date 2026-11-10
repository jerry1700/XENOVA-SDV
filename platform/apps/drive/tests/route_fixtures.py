"""공통 codec의 명시 typed geometry fixture. 실제 M/D/P4/Authority/차량 성공이 아니다.

실제 actor 시험은 owner/witness/World/Vehicle 원문을 넣어 재구성한다. 이 모듈의 plan은
비교/부정 자료이며 실제 C global planner를 대신 실행하지 않는다.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID
from xenova_drive_world import schema,encode,route_record_sha256,route_source_key_sha256,route_witness_sha256
from xenova_drive_world import route_query_sha256,route_candidate_sha256,route_core_sha256,route_owner_sha256,route_local_path_sha256
from xenova_drive_world.route import NIL,ZERO,NO_OUTGOING_EDGE


def uid(number):return UUID(int=number).bytes

def digest(number):return bytes([number])*32


def route_case(*,owner=None,witness=None,world=None,vehicle=None,records=None):
    from test_world import modern_world,parking_state
    w=deepcopy(world) if world is not None else modern_world()
    if witness is not None and world is None:
        source=witness.source
        for name in ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch','clock_epoch',
                     'configuration','calibration','reference_id','reference_revision'):
            setattr(w,name,getattr(source,name))
        w.primitive=source.primitive.hex();w.sequence=source.world_sequence;w.observation_seq=source.observation_sequence
    elif world is None:
        w.sequence=10;w.reference_id=str(UUID(bytes=uid(20)));w.observation_seq=10;w.reference_odom_seq=10
    # Four shared canonical stations; global hard domain and lane geometry remain distinct.
    xs=(0.,2.,4.,6.)
    if world is None:
        for key in ('reference','left_boundary','right_boundary'):w.ClearField(key)
        for x in xs:
            w.reference.add(x=x,y=1.6);w.left_boundary.add(x=x,y=3.2);w.right_boundary.add(x=x,y=0.)
    v=deepcopy(vehicle) if vehicle is not None else schema.VehicleSample(major=1,vehicle=w.vehicle,
        host_boot=str(UUID(bytes=uid(101))),device_session=str(UUID(bytes=uid(102))),odom_epoch=w.odom_epoch,
        sequence=10,source_sample=10,source_us=1000000,pi_sample_us=1000000,clock_error_us=10,
        left_ticks=100,right_ticks=100,odom_pose=schema.Pose(x=.3,y=2.,yaw=0.),velocity_mps=.05,
        estimated_steering_rad=0.,steering_error_rad=.01,position_error_m=.01,heading_error_rad=.01,
        quality=3,calibration=w.calibration,gateway_instance=str(UUID(bytes=uid(103))),pi_session=str(UUID(bytes=uid(104))))
    if world is None:
        w.reference_odom_seq=v.sequence;w.reference_vehicle_pose.CopyFrom(v.odom_pose)
    old_world_raw=encode(w)
    origin=schema.RoadObservationOrigin(major=1,mapper_boot=UUID(w.mapper_boot).bytes,
        mapper_instance=UUID(w.mapper_instance).bytes,clock_epoch=UUID(w.clock_epoch).bytes,
        observation_sequence=w.observation_seq,frame_source_sequence=10,capture_mapper_lower_us=w.observed_us-10,
        capture_mapper_upper_us=w.observed_us+10,completed_mapper_us=w.completed_us,
        raw_binding_sha256=digest(1),frame_evidence_sha256=digest(2),origin_context_sha256=digest(3),
        alignment_raw_sha256=digest(4),mapper_clock_reply_sha256=digest(5),vehicle_sequence=v.sequence,
        independent_world_sha256=sha256(old_world_raw).digest())
    if records is None:
        scope=schema.RoadGeometryScope(major=1,dataset_uuid=uid(110),map_epoch=UUID(w.map_epoch).bytes,
            frame_epoch=UUID(w.frame_epoch).bytes,vehicle=w.vehicle,configuration=w.configuration,calibration=w.calibration,
            primitive=bytes.fromhex(w.primitive),origin_pi_host_boot=UUID(v.host_boot).bytes,
            origin_gateway_instance=UUID(v.gateway_instance).bytes,origin_pi_session=UUID(v.pi_session).bytes,
            origin_device_session=UUID(v.device_session).bytes,origin_odom_epoch=UUID(w.odom_epoch).bytes,
            alignment_frame_sha256=digest(6),geometry_policy_sha256=digest(7),coordinate_policy=1,
            origin_storage_context_sha256=origin.origin_context_sha256)
        scope_sha=route_record_sha256(8,scope)
        def section(identifier,stations):
            result=schema.RoadSection(major=1,section_id=uid(identifier),revision=1,geometry_scope_sha256=scope_sha,
                origin=origin,heading_error_rad=.001,alignment_raw_sha256=origin.alignment_raw_sha256,
                observation_evidence_sha256=origin.frame_evidence_sha256)
            result.road.revision=1;result.road.divider_position_bound_m=.001;result.road.divider_heading_bound_rad=.001
            result.road.crossing=bytes([1])*(len(stations)-1);result.road.divider_state=bytes(len(stations))
            for x in stations:
                result.reference.add(x=x,y=1.6);result.left_boundary.add(x=x,y=2.4);result.right_boundary.add(x=x,y=.8)
                result.point_error_m.append(.001);result.road.divider.add(x=x,y=1.6)
            for identifier,y in ((1,1.2),(2,2.)):
                lane=result.road.lanes.add(lane_id=identifier,travel_direction=1,position_bound_m=.001,
                    heading_bound_rad=.001,revision=1,adjacent_lane_id=3-identifier,point_state=bytes(len(stations)))
                for x in stations:lane.centerline.add(x=x,y=y)
            return result
        sections=(section(21,(0.,1.,2.,3.)),section(22,(2.,3.,4.,5.,6.)))
        section_sha=tuple(route_record_sha256(3,x) for x in sections)
        connection=schema.RoadConnection(major=1,connection_id=uid(31),revision=1,geometry_scope_sha256=scope_sha,
            origin=origin,from_section_sha256=section_sha[0],to_section_sha256=section_sha[1],
            from_lane_id=2,to_lane_id=2,from_point_index=2,to_point_index=0,
            heading_error_rad=.001,kind=1,evidence_roots_sha256=[origin.frame_evidence_sha256])
        for x in (2.,3.):connection.overlap_reference.add(x=x,y=1.6);connection.overlap_error_m.append(.001)
        for x,y in ((2.,.8),(3.,.8),(3.,2.4),(2.,2.4)):connection.connector_polygon.add(x=x,y=y)
        parking_goal_sha=digest(20)
        slot_connector=schema.RoadConnection(major=1,connection_id=uid(32),revision=1,geometry_scope_sha256=scope_sha,
            origin=origin,from_section_sha256=section_sha[0],to_section_sha256=section_sha[0],
            from_lane_id=2,to_lane_id=2,from_point_index=1,to_point_index=1,heading_error_rad=.001,kind=3,
            evidence_roots_sha256=[origin.frame_evidence_sha256,parking_goal_sha])
        for x,y in ((.6,2.),(1.4,2.),(1.4,2.6),(.6,2.6)):slot_connector.connector_polygon.add(x=x,y=y)
        connections=(connection,slot_connector);conn_sha=tuple(route_record_sha256(4,x) for x in connections)
        topology=schema.RoadTopology(major=1,topology_id=uid(40),revision=1,geometry_scope_sha256=scope_sha,
            closed_observed=False,origin_roots_sha256=origin.frame_evidence_sha256)
        for i,item in enumerate(sections):
            topology.sections.add(section_id=item.section_id,section_sha256=section_sha[i],left_lane_id=1,right_lane_id=2,
                length_m=3. if i==0 else 4.,order=i)
        topology.edges.add(from_section_index=0,to_section_index=1,from_lane_id=2,to_lane_id=2,kind=1,
            from_station_m=3.,to_station_m=1.,connection_sha256=conn_sha[0],closes_circuit=False)
        topology_sha=route_record_sha256(2,topology)
        window=schema.RoadWindow(major=1,window_id=uid(20),revision=1,geometry_scope_sha256=scope_sha,
            topology_sha256=topology_sha,section_sha256=section_sha,origin=origin,heading_error_rad=.001,
            from_window_id=NIL,from_window_sha256=ZERO,overlap_prefix_sha256=ZERO,transition_id=NIL,overlap_point_count=0)
        window.road.CopyFrom(w.lane_model)
        for x in xs:
            window.reference.add(x=x,y=1.6);window.left_boundary.add(x=x,y=2.4);window.right_boundary.add(x=x,y=.8);window.point_error_m.append(.001)
        window_sha=route_record_sha256(5,window)
        exit_space=schema.ExitSpace(major=1,space_id=uid(45),revision=1,geometry_scope_sha256=scope_sha,
            origin=origin,parking_attempt_id=uid(46),parking_goal_sha256=parking_goal_sha,slot_id=1,slot_revision=1,
            aisle_lane_id=2,road_section_sha256=section_sha[0],connection_sha256=[conn_sha[1]],position_error_m=.01,heading_error_rad=.01)
        for x,y in ((.6,2.4),(1.4,2.4),(1.4,3.2),(.6,3.2)):exit_space.selected_slot_polygon.add(x=x,y=y)
        goal=schema.StartGoal(major=1,goal_id=uid(50),revision=1,geometry_scope_sha256=scope_sha,
            rear_axle_pose=schema.Pose(x=4.,y=2.,yaw=0.),position_error_m=.01,heading_error_rad=.01,
            section_id=sections[1].section_id,lane_id=2,station_m=2.,origin=origin,
            vehicle_raw_sha256=sha256(encode(v)).digest(),alignment_raw_sha256=origin.alignment_raw_sha256,
            world_raw_sha256=sha256(old_world_raw).digest(),terminal_profile_sha256=digest(21),
            origin_vehicle_sequence=v.sequence,registered_pi_us=v.pi_sample_us,stop_command_sequence=1,stop_report_sequence=1,
            stop_facts_sha256=digest(22))
        records=dict(scope=scope,sections=sections,connections=connections,windows=(window,),exit_space=exit_space,
            goal=goal,topology=topology)
    else:records=deepcopy(records)
    scope_sha=route_record_sha256(8,records['scope']);window=records['windows'][0]
    window_sha=route_record_sha256(5,window);topology_sha=route_record_sha256(2,records['topology']);goal_sha=route_record_sha256(1,records['goal'])
    w.required_features=27
    w.route_view.CopyFrom(schema.WorldRouteView(major=1,geometry_scope_sha256=scope_sha,topology_sha256=topology_sha,
        window_id=window.window_id,window_revision=window.revision,window_sha256=window_sha,from_window_id=NIL,
        from_window_sha256=ZERO,transition_id=NIL,first_section_id=records['sections'][0].section_id,
        last_section_id=records['sections'][-1].section_id,overlap_prefix_sha256=ZERO,geometry_revision=1))
    source=schema.EpisodeSourceContext(**{name:getattr(w,name) for name in ('vehicle','mapper_boot','mapper_instance',
        'map_epoch','frame_epoch','odom_epoch','clock_epoch','configuration','calibration','reference_id','reference_revision')},
        primitive=bytes.fromhex(w.primitive),world_sequence=w.sequence,observation_sequence=w.observation_seq)
    wit=deepcopy(witness) if witness is not None else schema.RouteCurrentWitness(source=source,vehicle_sequence=v.sequence,
        checked_guest_us=1010000,valid_until_pi_us=1090000,clock_probe_sequence=1,clock_reply_sha256=digest(30),
        world_raw_sha256=sha256(encode(w)).digest(),vehicle_raw_sha256=sha256(encode(v)).digest())
    if witness is not None and world is not None:
        if witness.source!=source or witness.world_raw_sha256!=sha256(encode(w)).digest():
            raise ValueError('fixture must preserve supplied original World/source bytes')
        if witness.vehicle_sequence!=v.sequence or witness.vehicle_raw_sha256!=sha256(encode(v)).digest():
            raise ValueError('fixture must preserve supplied original Vehicle bytes')
    ctx=deepcopy(owner) if owner is not None else schema.RouteOwnerContext(vm=uid(61),execution=uid(62),app_instance=uid(63),
        control_generation=1,operation_binding=schema.OperationBinding(service_epoch=UUID(v.gateway_instance).bytes,
            operation_revision=2,departure_nonce=uid(64),permission_revision=1,parking_enabled=False),
        mission_id=NIL,mission_revision=0,grant_revision=0,cancel_fence_revision=0,scene_admission_id=NIL,scene_revision=0,
        source_key_sha256=route_source_key_sha256(wit.source),geometry_scope_sha256=scope_sha)
    if ctx.geometry_scope_sha256!=scope_sha:raise ValueError('fixture owner needs original compatible geometry records')
    state=parking_state();state.source_vm=str(UUID(bytes=ctx.vm));state.source_execution=str(UUID(bytes=ctx.execution))
    state.source_app_instance=str(UUID(bytes=ctx.app_instance));state.source_generation=ctx.control_generation
    for name in ('frame_epoch','odom_epoch','configuration','primitive','reference_id','reference_revision','calibration','map_epoch'):
        setattr(state,name,getattr(w,name))
    state.observation_seq=w.observation_seq;state.world_features=27;state.state_features=111;state.vehicle_sequence=wit.vehicle_sequence
    state.calculation_sequence=42;state.path_revision=1;state.maneuver_revision=1;state.direction=state.maneuver_phase=0
    for field in ('path','segments','direction_gate','mission'):state.ClearField(field)
    for x,speed in ((.3,.1),(.8,0.)):
        item=state.path.add(steering_rad=0.,velocity_mps=speed);item.pose.CopyFrom(schema.Pose(x=x,y=2.,yaw=0.))
    state.segments.add(first_index=0,last_index=1,signed_direction=1);state.merge_goal.CopyFrom(state.path[-1].pose)
    state.task_reference.CopyFrom(schema.TaskReferenceState(home_lane_id=2,from_lane_id=2,selected_lane_id=2,changing=False,
        basis_revision=1,basis_prefix=window.reference,vehicle=w.vehicle,mapper_boot=w.mapper_boot,mapper_instance=w.mapper_instance,
        clock_epoch=w.clock_epoch))
    plan=schema.RoutePlan(major=1,route_id=uid(70),revision=1,purpose=2,goal_sha256=goal_sha,topology_sha256=topology_sha,
        geometry_scope_sha256=scope_sha,max_laps=1,exit_space_sha256=ZERO,origin_guest_witness_sha256=route_witness_sha256(wit),
        merge_section_id=NIL,merge_lane_id=0,merge_station_lower_m=0.,merge_station_upper_m=0.,
        merge_seed_pose=schema.Pose(x=0.,y=0.,yaw=0.),terminal_profile_sha256=records['goal'].terminal_profile_sha256)
    plan.occurrences.add(section_index=0,lane_id=2,entry_station_m=.3,exit_station_m=3.,edge_index=0)
    plan.occurrences.add(section_index=1,lane_id=2,entry_station_m=1.,exit_station_m=2.,edge_index=NO_OUTGOING_EDGE)
    plan_sha=route_record_sha256(7,plan)
    local_sha=route_local_path_sha256(state,global_plan_sha256=plan_sha,geometry_scope_sha256=scope_sha,
        basis_window_sha256=window_sha,task_space_kind=1,exit_space_sha256=ZERO,basis_revision=1,basis_prefix=window.reference)
    core=schema.RouteCore(route_id=plan.route_id,revision=1,purpose=2,goal_sha256=goal_sha,topology_sha256=topology_sha,
        global_plan_sha256=plan_sha,geometry_scope_sha256=scope_sha,started_pi_us=0,deadline_pi_us=0,meter_epoch=NIL,
        meter_baseline_total_ticks=0,max_travel_m=6.,phase=3,phase_revision=1,phase_started_pi_us=0,phase_expires_pi_us=0,
        parking_attempt_id=NIL,parking_goal_sha256=ZERO,predecessor_episode_sha256=ZERO,budget_profile_sha256=digest(31),
        target_lane_id=2,max_laps=1,stop_wait_command_sequence=0,local_path_sha256=local_sha,meter_gap_revision=0,
        metres_per_tick_upper=0.,progress_mode=1,phase_goal=schema.Pose(x=4.,y=2.,yaw=0.),stop_wait_facts_sha256=ZERO,
        stop_wait_started_pi_us=0,terminal_result=0,terminal_reason=0)
    progress=schema.RouteProgress(occurrence_index=0,station_lower_m=.29,station_upper_m=.31,used_travel_ticks=0,
        last_progress_pi_us=0,vehicle_sequence=wit.vehicle_sequence,calculation_sequence=42,revision=1,completed_laps=0,
        confirmed_stop_wait_us=0,remaining_upper_m=3.71,stop_wait_hwm_pi_us=0)
    extent=schema.RouteExtentProof(mode=1,window_sha256=window_sha,exit_space_sha256=ZERO,available_forward_m=5.,
        available_backward_m=.3,required_forward_m=1.,required_backward_m=.2,braking_distance_m=.2,body_radius_m=.4,
        position_bound_m=.01,heading_bound_rad=.01,cusp_sweep_sha256=ZERO,s19_sweep_sha256=ZERO,vehicle_sequence=wit.vehicle_sequence)
    query=schema.RouteQuery(major=1,query_id=uid(71),purpose=2,goal_sha256=goal_sha,owner=ctx,
        issued_pi_us=1000000,valid_until_pi_us=11000000,operator_request_nonce=uid(72))
    candidate=schema.RouteCandidate(major=1,request_nonce=uid(73),request_sequence=1,action=1,query_id=query.query_id,
        query_sha256=route_query_sha256(query),owner=ctx,witness=wit,expected_registry_epoch=NIL,expected_registry_revision=0,
        expected_route_revision=0,core=core,progress=progress,task_space_kind=1,current_window_sha256=window_sha,
        basis_window_sha256=window_sha,basis_window_revision=1,exit_space_sha256=ZERO,phase_state_message_id=uid(74),
        plan_revision=1,calculation_sequence=42,began_guest_us=max(1,wit.checked_guest_us-5000),
        phase_started_guest_us=max(1,wit.checked_guest_us-5000),control_facts_sha256=ZERO,extent=extent)
    return dict(query=query,candidate=candidate,owner=ctx,witness=wit,world=w,vehicle=v,records=records,plan=plan,state=state,extent=extent)


def accepted_route(case,*,registry_epoch,registry_revision=1,acceptance_nonce,started_pi_us,meter_epoch,
                   meter_baseline_total_ticks,metres_per_tick_upper,initial_target_context=None,
                   duration_us=40000000,phase_us=10000000):
    """typed accepted geometry only. Actual actor/P4/meter/Authority calls are supplied by integration tests."""
    from test_world import lane_grant
    result=deepcopy(case);request=result['candidate'];core=deepcopy(request.core)
    core.started_pi_us=started_pi_us;core.deadline_pi_us=started_pi_us+duration_us
    core.phase_started_pi_us=started_pi_us;core.phase_expires_pi_us=min(core.deadline_pi_us,started_pi_us+phase_us)
    core.meter_epoch=meter_epoch;core.meter_baseline_total_ticks=meter_baseline_total_ticks;core.metres_per_tick_upper=metres_per_tick_upper
    ref=schema.RouteAcceptanceRef(registry_epoch=registry_epoch,registry_revision=registry_revision,acceptance_nonce=acceptance_nonce,
        request_nonce=request.request_nonce,request_sha256=route_candidate_sha256(request),accepted_core_sha256=route_core_sha256(core),
        request_sequence=request.request_sequence,origin_owner_sha256=route_owner_sha256(request.owner))
    progress=deepcopy(request.progress);progress.calculation_sequence=request.calculation_sequence+1
    snapshot=schema.RouteSnapshot(core=core,progress=progress,acceptance=ref,current_window_sha256=request.current_window_sha256,
        state=2,task_space_kind=request.task_space_kind,exit_space_sha256=request.exit_space_sha256,
        basis_window_sha256=request.basis_window_sha256,basis_window_revision=request.basis_window_revision,
        current_world_sha256=request.witness.world_raw_sha256)
    target=deepcopy(initial_target_context or request.owner)
    if initial_target_context is None:
        target.control_generation+=1;target.mission_id=uid(75);target.mission_revision=target.grant_revision=1
    binding=schema.RouteBinding(route_id=core.route_id,revision=core.revision,purpose=core.purpose,core_sha256=ref.accepted_core_sha256,
        global_plan_sha256=core.global_plan_sha256,goal_sha256=core.goal_sha256)
    grant=lane_grant();grant.vehicle=result['world'].vehicle;grant.pi_host_boot=UUID(result['vehicle'].host_boot).bytes
    grant.gateway_instance=target.operation_binding.service_epoch;grant.owner_vm=target.vm;grant.owner_execution=target.execution
    grant.app_instance=target.app_instance;grant.control_generation=target.control_generation;grant.mission_id=target.mission_id
    grant.mission_revision=target.mission_revision;grant.grant_revision=target.grant_revision;grant.kind=3
    grant.operator_request_nonce=result['query'].operator_request_nonce;grant.issued_pi_us=started_pi_us+1
    grant.cancel_fence_revision=target.cancel_fence_revision;grant.scene_admission_id=target.scene_admission_id;grant.scene_revision=target.scene_revision
    grant.configuration=result['world'].configuration;grant.calibration=result['world'].calibration;grant.primitive=bytes.fromhex(result['world'].primitive)
    grant.operation_binding.CopyFrom(target.operation_binding);grant.route_binding.CopyFrom(binding)
    receipt=schema.RouteReceipt(major=1,request_nonce=request.request_nonce,request_sha256=ref.request_sha256,result=1,reason=0,
        snapshot=snapshot,request_sequence=request.request_sequence,query_id=request.query_id,query_sha256=request.query_sha256,
        initial_target_context=target)
    state=result['state'];state.source_vm=str(UUID(bytes=target.vm));state.source_execution=str(UUID(bytes=target.execution))
    state.source_app_instance=str(UUID(bytes=target.app_instance));state.source_generation=target.control_generation
    state.message_id=str(UUID(bytes=request.phase_state_message_id));state.calculation_sequence=progress.calculation_sequence
    state.mission.CopyFrom(schema.MissionExecution(mission_id=grant.mission_id,mission_revision=grant.mission_revision,kind=3,
        phase=core.phase,result=0,operator_request_nonce=grant.operator_request_nonce,mission_deadline_pi_us=0,
        cancel_fence_revision=grant.cancel_fence_revision,active_segment=0,scene_admission_id=grant.scene_admission_id,
        scene_admission_revision=grant.scene_revision,strategy=0,execution_deadline_pi_us=0,correction_attempts_used=0,
        execution_travel_m=0.,grant_revision=grant.grant_revision,search_travel_m=0.,search_deadline_pi_us=0,
        operation_binding=grant.operation_binding,route_binding=binding))
    state.route.CopyFrom(snapshot);result.update(snapshot=snapshot,receipt=receipt,state=state,grant=grant,binding=binding,target=target)
    return result
