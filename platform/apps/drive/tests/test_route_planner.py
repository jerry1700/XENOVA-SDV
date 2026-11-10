"""Actual C directed route/EXIT math on common declared observation records.

Typed records/source/Scene here are mathematical fixtures, not actual M/D/P4,
authenticated Query, app apply or movement permission. Python never computes
the positive global path; the returned C-produced RoutePlan is decoded as-is.
"""
import ctypes as C
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import math
import json
import subprocess

import pytest
from xenova_drive_world import schema,encode,decode,route_record_sha256,route_core_sha256,route_stop_facts_sha256,basis_prefix_sha256
from route_fixtures import route_case,accepted_route,uid,digest

HOME=Path(__file__).resolve().parents[1]
OK=0;INPUT=1;UNREACHABLE=2;WORK=3;DEADLINE=4;CANCELLED=5;CONTEXT=7


@pytest.fixture(scope='module')
def route_library(tmp_path_factory):
    world=HOME/'world';planner=HOME/'planner';output=tmp_path_factory.mktemp('actual-route')/'route.so'
    sources=[world/'world.c',world/'generated/world.pb.c',
             *(world/'nanopb'/name for name in ('pb_common.c','pb_decode.c','pb_encode.c')),
             *(planner/name for name in ('task_reference.c','parking.c','lattice.c','route_plan.c',
                                        'route_reference.c','route_exit.c','route_progress.c','generated/primitives.c')),
             HOME/'tests/route_planner_bridge.c']
    cmd=['cc','-std=c11','-D_POSIX_C_SOURCE=200809L','-O2','-g','-Wall','-Wextra','-Werror',
         '-fsanitize=undefined','-shared','-fPIC',*[f'-I{p}' for p in (world,world/'generated',world/'nanopb',planner)],
         *map(str,sources),'-lm','-o',str(output)]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stderr
    lib=C.CDLL(str(output));lib.route_probe_record.argtypes=[C.c_uint,C.c_uint,C.c_char_p,C.c_size_t]
    lib.route_probe_build.argtypes=[C.c_uint,C.c_uint,C.c_uint,C.c_float,C.c_float,C.c_int,C.c_uint,C.c_uint64]
    lib.route_probe_plan.argtypes=[C.c_void_p,C.c_size_t];lib.route_probe_plan.restype=C.c_size_t
    lib.route_probe_open.argtypes=[C.c_uint];lib.route_probe_window.argtypes=[C.c_uint]
    lib.route_probe_exit_prepare.argtypes=[C.c_uint64]
    lib.route_probe_exit_contains.argtypes=[C.c_float,C.c_float,C.c_float]
    lib.route_probe_exit_search.argtypes=[C.c_float]*6
    lib.route_probe_path_point.argtypes=[C.c_uint,C.POINTER(C.c_float)]
    lib.route_probe_exit_hash.argtypes=[C.c_void_p]
    lib.route_probe_meter.argtypes=[C.c_uint64,C.POINTER(C.c_uint64),C.POINTER(C.c_double)]
    lib.route_probe_progress_math.argtypes=[C.c_uint64]
    lib.route_probe_last_progress.restype=lib.route_probe_used_ticks.restype=C.c_uint64
    lib.route_probe_tracking_sequence.restype=C.c_uint64
    lib.route_probe_goal_plan.argtypes=[C.c_float]*3
    lib.route_probe_goal_track.argtypes=[C.c_float]*5+[C.c_uint64,C.POINTER(C.c_float)]
    lib.route_probe_goal_profile.argtypes=[C.POINTER(C.c_float)]
    lib.route_probe_goal_profile.restype=None
    lib.route_probe_terminal.argtypes=[C.c_uint64]
    lib.route_probe_vehicle.argtypes=[C.c_void_p,C.c_size_t];lib.route_probe_vehicle.restype=C.c_size_t
    return lib


def load(lib,case,*,raw_topology=False):
    lib.route_probe_reset();r=case['records']
    rows=((0,0,case['world']),(1,0,r['goal']),(2,0,r['topology']),(3,0,r['sections'][0]),
          (4,0,r['connections'][1]),(5,0,r['windows'][0]),(6,0,r['exit_space']),
          (9,0,case['query']),(10,0,case['witness']),(11,0,case['vehicle']))
    for kind,index,value in rows:
        raw=value.SerializeToString(deterministic=True) if kind==2 and raw_topology else encode(value)
        assert lib.route_probe_record(kind,index,raw,len(raw))==0


def build(lib,*,lane=2,low=.3,high=.3,purpose=2,section=0,use_exit=False,fault=0,work=50000):
    rc=lib.route_probe_build(purpose,section,lane,low,high,use_exit,fault,work)
    if rc:return rc,None
    buf=C.create_string_buffer(4096);n=lib.route_probe_plan(buf,len(buf));assert n
    return rc,decode(buf.raw[:n],schema.RoutePlan)


def test_actual_c_routes_forward_and_uses_no_fake_terminal_edge(route_library):
    case=route_case();load(route_library,case)
    rc,plan=build(route_library)
    assert rc==OK
    assert [(o.section_index,o.lane_id) for o in plan.occurrences]==[(0,2),(1,2)]
    assert plan.occurrences[0].edge_index==0 and plan.occurrences[-1].edge_index==0xffffffff
    assert plan.occurrences[-1].exit_station_m==case['records']['goal'].station_m
    assert plan.goal_sha256==route_record_sha256(1,case['records']['goal'])
    assert route_library.route_probe_open(0)==OK


def test_actual_c_same_section_zero_edge_path_is_a_valid_open_route(route_library):
    case=route_case();goal=case['records']['goal'];goal.section_id=case['records']['sections'][0].section_id;goal.station_m=2.;goal.rear_axle_pose.x=2.
    case['records']['topology'].ClearField('edges')
    load(route_library,case);rc,plan=build(route_library)
    assert rc==OK and len(plan.occurrences)==1 and plan.occurrences[0].edge_index==0xffffffff


def test_actual_c_does_not_infer_lane_crossover_from_adjacent_lane_ids(route_library):
    case=route_case();load(route_library,case)
    assert build(route_library,lane=1)[0]==UNREACHABLE
    t=case['records']['topology']
    # Declared observed edge, not a Python-computed path or inferred adjacency.
    t.edges.add(from_section_index=0,to_section_index=0,from_lane_id=1,to_lane_id=2,kind=2,
                from_station_m=.5,to_station_m=1.,connection_sha256=digest(88),closes_circuit=False)
    load(route_library,case);rc,plan=build(route_library,lane=1)
    assert rc==OK and [(o.section_index,o.lane_id) for o in plan.occurrences]==[(0,1),(0,2),(1,2)]


def test_actual_c_goal_behind_requires_observed_forward_circuit(route_library):
    case=route_case();t=case['records']['topology'];goal=case['records']['goal']
    goal.section_id=t.sections[0].section_id;goal.station_m=.1;goal.rear_axle_pose.x=.1
    load(route_library,case);assert build(route_library)[0]==UNREACHABLE
    t.closed_observed=True
    t.edges.add(from_section_index=1,to_section_index=0,from_lane_id=2,to_lane_id=2,kind=1,
                from_station_m=4.,to_station_m=0.,connection_sha256=digest(89),closes_circuit=True)
    load(route_library,case);rc,plan=build(route_library)
    assert rc==OK and [o.section_index for o in plan.occurrences]==[0,1,0]
    assert all(o.entry_station_m<=o.exit_station_m for o in plan.occurrences)


@pytest.mark.parametrize('fault,work,expected',[(0,1,WORK),(1,50000,DEADLINE),(2,50000,CANCELLED)])
def test_actual_c_global_search_is_finite_and_preserves_one_work_deadline(route_library,fault,work,expected):
    load(route_library,route_case());assert build(route_library,fault=fault,work=work)[0]==expected


def window_points(window,xs):
    for name in ('reference','left_boundary','right_boundary','point_error_m'):window.ClearField(name)
    for x in xs:
        window.reference.add(x=x,y=1.6);window.left_boundary.add(x=x,y=2.4);window.right_boundary.add(x=x,y=.8);window.point_error_m.append(.001)
    window.road.ClearField('divider')
    for x in xs:window.road.divider.add(x=x,y=1.6)
    window.road.crossing=bytes([1])*(len(xs)-1);window.road.divider_state=bytes(len(xs))
    for lane in window.road.lanes:
        y=lane.centerline[0].y;lane.ClearField('centerline');lane.point_state=bytes(len(xs))
        for x in xs:lane.centerline.add(x=x,y=y)


def put_window(lib,index,window):
    raw=encode(window);assert lib.route_probe_record(5,index,raw,len(raw))==0


def test_actual_c_exact_append_and_explicit_suffix_prefix_seam_are_distinct(route_library):
    case=route_case();load(route_library,case)
    old=deepcopy(case['records']['windows'][0]);window_points(old,(0.,1.,2.,3.));put_window(route_library,0,old)
    new=deepcopy(old);window_points(new,(0.,1.,2.,3.,4.));new.revision+=1;put_window(route_library,1,new)
    assert route_library.route_probe_window(0)==1 and route_library.route_probe_window(1)==0
    new.reference[0].y+=.001;put_window(route_library,1,new)
    assert route_library.route_probe_window(0)==0
    new=deepcopy(old);window_points(new,(2.,3.,4.,5.,6.));new.window_id=uid(91)
    new.from_window_id=old.window_id;new.from_window_sha256=route_record_sha256(5,old)
    new.transition_id=uid(92);new.overlap_point_count=2;new.overlap_prefix_sha256=basis_prefix_sha256(new.reference[:2])
    put_window(route_library,1,new)
    assert route_library.route_probe_window(1)==1 and route_library.route_probe_window(0)==0
    new.point_error_m[0]=.002;put_window(route_library,1,new)
    assert route_library.route_probe_window(1)==0


def exit_case():
    case=route_case();w=case['world'];space=case['records']['exit_space']
    w.required_features|=4
    slot=w.parking_slots.add(slot_id=space.slot_id,geometry_revision=space.slot_revision,geometry_state=1,
        occupancy_state=0,entry_edge=0,position_bound_m=.01,heading_bound_rad=.01,geometry_age_ms=0,
        clearance_age_ms=0,evidence_sequence=1,geometry_complete=True,occupancy_reason=0,source_kinds=1,
        occupancy_revision=1,usable_width_m=.8,usable_depth_m=.8,approach_lane_id=space.aisle_lane_id,goal_mask=1)
    slot.corners.extend(space.selected_slot_polygon);slot.reverse_goal.CopyFrom(schema.Pose(x=1.,y=2.8,yaw=-math.pi/2))
    w.parking_set.CopyFrom(schema.ParkingSetMeta(revision=1,complete=True,track_count_in_roi=1,capacity_limited=False,evidence_sequence=1,state=1))
    w.scene.CopyFrom(schema.SceneAdmissionRef(admission_id=uid(93),admission_revision=1,survey_id=uid(94),survey_revision=1,
        policy=1,coverage_profile_sha256=digest(93),allow_restore_after_mapper_restart=False))
    case['owner'].scene_admission_id=w.scene.admission_id;case['owner'].scene_revision=1
    case['query'].owner.CopyFrom(case['owner'])
    case['witness'].world_raw_sha256=sha256(encode(w)).digest()
    # These protobuf constructor inputs are copies. Registration must consume
    # the same declared current Scene/source, not the old NIL candidate owner.
    case['candidate'].owner.CopyFrom(case['owner'])
    case['candidate'].witness.CopyFrom(case['witness'])
    return case


def test_actual_c_exit_requires_original_slot_connector_goal_and_current_query(route_library):
    case=exit_case();case['query'].valid_until_pi_us=1030000;load(route_library,case)
    assert route_library.route_probe_exit_space()==OK
    assert route_library.route_probe_exit_prepare(1020000)==OK
    assert route_library.route_probe_exit_prepare(case['query'].valid_until_pi_us)==CONTEXT
    # Source seq is an original current sample, never a new timestamped copy.
    case['witness'].source.world_sequence+=1;load(route_library,case)
    assert route_library.route_probe_exit_prepare(1020000)==CONTEXT


def test_actual_c_observed_connector_covers_gap_without_filling_unknown_or_other_slot(route_library):
    case=exit_case();space=case['records']['exit_space'];slot=case['world'].parking_slots[0]
    # The original slot touched the road at y=2.4, so their union still
    # contained this body after moving an unused connector. Declare a real
    # 10cm road/slot gap, with the observed connector as its only coverage.
    for point in space.selected_slot_polygon:
        if point.y<2.5:point.y=2.5
    slot.ClearField('corners');slot.corners.extend(space.selected_slot_polygon);slot.usable_depth_m=.7
    case['witness'].world_raw_sha256=sha256(encode(case['world'])).digest()
    load(route_library,case)
    assert route_library.route_probe_exit_contains(1.,2.45,-math.pi/2)==1
    assert route_library.route_probe_exit_contains(3.,2.8,-math.pi/2)==0
    # A real declared geometry gap remains a rejection, with no epsilon/snap.
    connection=case['records']['connections'][1]
    for p in connection.connector_polygon:p.y+=.7
    case['records']['exit_space'].connection_sha256[0]=route_record_sha256(4,connection)
    load(route_library,case)
    assert route_library.route_probe_exit_contains(1.,2.45,-math.pi/2)==0


def test_actual_signed_exit_search_ends_in_road_and_unknown_current_grid_blocks_it(route_library,tmp_path):
    case=exit_case();case['query'].purpose=1;case['query'].goal_sha256=bytes(32);load(route_library,case)
    assert build(route_library,purpose=1,use_exit=True,low=2.,high=2.)[0]==OK
    rc=route_library.route_probe_exit_search(1.,2.8,-math.pi/2,2.,2.,0.)
    points=[]
    if rc==1:
        for i in range(route_library.route_probe_path_count()):
            out=(C.c_float*5)();assert route_library.route_probe_path_point(i,out)==0;points.append(list(out))
    (tmp_path/'actual-exit-search.json').write_text(json.dumps(dict(result=rc,points=points),indent=2))
    assert rc==1
    assert route_library.route_probe_exit_valid()==1 and points[-1][1]<2.4 and points[-1][4]==0
    raw_hash=C.create_string_buffer(32)
    assert route_library.route_probe_exit_hash(raw_hash)==1 and raw_hash.raw!=bytes(32)
    assert route_library.route_probe_exit_legacy_export()==0  # No Task33/NONE publication shortcut.
    assert any(p[1]>2.4 for p in points)
    case['world'].occupancy=bytes([255])*len(case['world'].occupancy)
    case['witness'].world_raw_sha256=sha256(encode(case['world'])).digest();load(route_library,case)
    assert route_library.route_probe_exit_search(1.,2.8,-math.pi/2,2.,2.,0.)==10


def accepted_math_case():
    case=accepted_route(route_case(),registry_epoch=uid(120),acceptance_nonce=uid(121),started_pi_us=1000000,
        meter_epoch=uid(122),meter_baseline_total_ticks=1000,metres_per_tick_upper=.01)
    case['facts']=schema.DriveControlFacts(major=1,meter=schema.PiAbsoluteMeter(meter_epoch=uid(122),total_ticks=1000,
        metres_per_tick=.01,gap_revision=0,valid=True,vehicle_sequence=case['vehicle'].sequence),valid_until_pi_us=10000000)
    return case


def put(lib,kind,value):
    raw=encode(value);assert lib.route_probe_record(kind,0,raw,len(raw))==0


def load_math(lib,case):
    load(lib,case)
    for kind,key in ((7,'plan'),(12,'snapshot'),(13,'grant'),(14,'facts'),(15,'state')):put(lib,kind,case[key])


def test_actual_c_original_route_budget_is_not_refunded_by_owner_or_replan(route_library):
    case=accepted_math_case();load_math(route_library,case);assert route_library.route_probe_sync()==OK
    changed=deepcopy(case['snapshot']);changed.core.deadline_pi_us+=1000
    changed.acceptance.accepted_core_sha256=route_core_sha256(changed.core);put(route_library,12,changed)
    assert route_library.route_probe_sync()==CONTEXT
    changed=deepcopy(case['snapshot']);changed.core.local_path_sha256=digest(111);changed.core.revision+=1
    changed.acceptance.accepted_core_sha256=route_core_sha256(changed.core);put(route_library,12,changed)
    assert route_library.route_probe_sync()==OK  # Original birth/phase clocks survive a geometry replan.


@pytest.mark.parametrize('fault,expected',[('none',OK),('gap',CONTEXT),('scale',CONTEXT),('epoch',CONTEXT),('distance',9),('phase-expiry',9)])
def test_actual_c_consumes_absolute_pi_meter_and_original_phase_e(route_library,fault,expected):
    case=accepted_math_case();facts=case['facts'];facts.meter.total_ticks=1100;now=1100000
    if fault=='gap':facts.meter.gap_revision+=1
    elif fault=='scale':facts.meter.metres_per_tick*=2
    elif fault=='epoch':facts.meter.meter_epoch=uid(123)
    elif fault=='distance':facts.meter.total_ticks=1601
    elif fault=='phase-expiry':
        now=case['snapshot'].core.phase_expires_pi_us;case['vehicle'].sequence+=1;case['vehicle'].pi_sample_us=now
        facts.meter.vehicle_sequence=case['vehicle'].sequence;facts.valid_until_pi_us=now+1000000
    load_math(route_library,case);ticks=C.c_uint64();metres=C.c_double()
    assert route_library.route_probe_meter(now,C.byref(ticks),C.byref(metres))==expected
    if not expected:assert ticks.value==100 and 1.<metres.value<1.0000000001


def test_actual_c_small_measured_steps_accumulate_progress_and_teleport_does_not(route_library):
    case=accepted_math_case();load_math(route_library,case);assert route_library.route_probe_sync()==OK
    for i in range(1,7):
        # Newly declared sample/meter leaves for arithmetic. No original raw
        # receipt/World or application authority is re-labelled by this case.
        case['vehicle'].sequence=10+i;case['vehicle'].pi_sample_us=1000000+i*20000
        case['vehicle'].odom_pose.x=.3+i*.01
        case['facts'].meter.vehicle_sequence=case['vehicle'].sequence;case['facts'].meter.total_ticks=1000+i
        put(route_library,11,case['vehicle']);put(route_library,14,case['facts'])
        assert route_library.route_probe_progress_math(case['vehicle'].pi_sample_us)==OK
    assert route_library.route_probe_last_progress()>1000000 and route_library.route_probe_used_ticks()==6
    case['vehicle'].sequence+=1;case['vehicle'].odom_pose.x=2.;case['vehicle'].pi_sample_us+=20000
    case['facts'].meter.vehicle_sequence=case['vehicle'].sequence
    put(route_library,11,case['vehicle']);put(route_library,14,case['facts'])
    assert route_library.route_probe_progress_math(case['vehicle'].pi_sample_us)==8


def test_actual_c_registered_route_uses_original_budget_after_query_e_without_refunding_it(route_library):
    case=accepted_math_case();load_math(route_library,case);assert route_library.route_probe_sync()==OK
    new=deepcopy(case['snapshot']);new.core.revision+=1;new.core.phase_revision+=1;new.core.phase=4
    new.core.phase_started_pi_us=10500000;new.core.phase_expires_pi_us=20500000
    new.acceptance.accepted_core_sha256=route_core_sha256(new.core);put(route_library,12,new)
    assert route_library.route_probe_sync()==OK
    now=case['query'].valid_until_pi_us+1;assert now<new.core.phase_expires_pi_us
    case['vehicle'].pi_sample_us=now;case['facts'].valid_until_pi_us=now+1000000
    put(route_library,11,case['vehicle']);put(route_library,14,case['facts'])
    ticks=C.c_uint64();metres=C.c_double()
    assert route_library.route_probe_meter(now,C.byref(ticks),C.byref(metres))==OK
    new.core.deadline_pi_us+=1;new.acceptance.accepted_core_sha256=route_core_sha256(new.core);put(route_library,12,new)
    assert route_library.route_probe_sync()==CONTEXT


def test_actual_c_global_goal_requires_goal_region_and_declared_matching_physical_stop(route_library,tmp_path):
    # Actual local search/RPP on a typed static scene + acceleration/braking
    # bounded bicycle plant. STOP facts remain declared leaves, not device ACKs.
    base=exit_case();base['candidate'].core.phase=4;base['candidate'].progress.occurrence_index=1
    base['candidate'].progress.station_lower_m=1.19;base['candidate'].progress.station_upper_m=1.21
    case=accepted_route(base,registry_epoch=uid(120),acceptance_nonce=uid(121),started_pi_us=1000000,
        meter_epoch=uid(122),meter_baseline_total_ticks=1000,metres_per_tick_upper=.01)
    case['vehicle'].odom_pose.CopyFrom(schema.Pose(x=3.2,y=2.,yaw=0.));case['vehicle'].velocity_mps=0
    case['facts']=schema.DriveControlFacts(major=1,meter=schema.PiAbsoluteMeter(meter_epoch=uid(122),total_ticks=1000,
        metres_per_tick=.01,gap_revision=0,valid=True,vehicle_sequence=case['vehicle'].sequence),valid_until_pi_us=10000000)
    load_math(route_library,case)
    assert route_library.route_probe_goal_plan(3.2,2.,0.)==1
    profile=(C.c_float*5)();route_library.route_probe_goal_profile(profile)
    wheelbase,max_accel,max_brake,goal_distance,goal_heading=profile
    assert min(wheelbase,max_accel,max_brake,goal_distance,goal_heading)>0
    x=3.2;y=2.;yaw=0.;velocity=steer=0.;stamp=1000000;rows=[];stop=False;first_stop=None
    first_stop_sample=None;first_stop_calculation=0;stationary=[];terminal_inputs={};terminal_results={}
    artifact=tmp_path/'actual-route-goal-tracker.json'
    def current_vehicle():
        buf=C.create_string_buffer(2048);n=route_library.route_probe_vehicle(buf,len(buf));assert n
        return decode(buf.raw[:n],schema.VehicleSample)
    def sample_view(v):
        return dict(sequence=v.sequence,source_sample=v.source_sample,pi_sample_us=v.pi_sample_us,
                    clock_error_us=v.clock_error_us,velocity_mps=v.velocity_mps,
                    vehicle_wire_hex=encode(v).hex())
    def save_trace(stage):
        artifact.write_text(json.dumps(dict(stage=stage,samples=rows,final_pose=[x,y,yaw],first_stop=first_stop,
            compiled_profile=dict(wheelbase=wheelbase,max_accel=max_accel,max_brake=max_brake,
                                  goal_distance=goal_distance,goal_heading=goal_heading),
            declared_goal=dict(x=case['records']['goal'].rear_axle_pose.x,y=case['records']['goal'].rear_axle_pose.y,
                               position_error_m=case['records']['goal'].position_error_m,
                               heading_error_rad=case['records']['goal'].heading_error_rad),
            declared_vehicle_errors=dict(position_error_m=case['vehicle'].position_error_m,
                                         heading_error_rad=case['vehicle'].heading_error_rad),
            first_stop_sample=None if first_stop_sample is None else sample_view(first_stop_sample),
            first_stop_calculation=first_stop_calculation,stationary_samples=[sample_view(v) for v in stationary],
            terminal_inputs=terminal_inputs,terminal_results=terminal_results,
            scope='actual C search/RPP; typed scene, bounded bicycle plant and declared STOP facts; no app/device authority'),indent=2),encoding='utf8')
    stage='approach-or-braking'
    try:
        for _ in range(500):
            stamp+=20000;out=(C.c_float*2)()
            rc=route_library.route_probe_goal_track(x,y,yaw,velocity,steer,stamp,out)
            rows.append((stamp,x,y,yaw,velocity,rc,list(out)))
            assert rc in (1,2),rows[-1]
            if first_stop is not None:
                assert rc==2,('STOP must survive the declared braking trajectory',rows[-1])
            if rc==2:
                assert out[0]==0.,('STOP must command zero without fabricating zero measured speed',rows[-1])
                if first_stop is None:
                    first_stop=rows[-1];first_stop_sample=current_vehicle()
                    first_stop_calculation=route_library.route_probe_tracking_sequence()
                if velocity==0.:
                    stationary.append(current_vehicle());stop=True;break
            # A STOP commands zero; it does not report that the moving plant has
            # already stopped. Integrate actual bounded braking before fresh samples.
            previous=velocity;target,steer=out
            velocity=previous+max(-max_brake*.02,min(max_accel*.02,target-previous))
            speed=(previous+velocity)/2
            x+=speed*math.cos(yaw)*.02;y+=speed*math.sin(yaw)*.02
            yaw+=speed*math.tan(steer)/wheelbase*.02
        stage='stationary-after-bounded-braking' if stop else 'bounded-plant-did-not-stop'
        assert stop
        # Two real calls with fresh declared zero-motion samples, then modelled
        # encoder/STOP facts. No speed0/LP_STOP alone is treated as confirmation.
        for _ in range(2):
            stamp+=20000;out=(C.c_float*2)()
            rc=route_library.route_probe_goal_track(x,y,yaw,0.,steer,stamp,out)
            rows.append((stamp,x,y,yaw,0.,rc,list(out)));stage='fresh-stationary-sample'
            assert rc==2,rows[-1]
            stationary.append(current_vehicle())
        first,vehicle=stationary[0],stationary[-1]
        assert len(stationary)==3 and first.sequence<vehicle.sequence and first.pi_sample_us<vehicle.pi_sample_us
        core=case['snapshot'].core;core.stop_wait_command_sequence=7
        core.stop_wait_started_pi_us=first.pi_sample_us+first.clock_error_us
        facts=case['facts'];facts.meter.vehicle_sequence=vehicle.sequence
        facts.meter.total_ticks=1000+math.ceil(abs(x-3.2)/.01);facts.valid_until_pi_us=stamp+100000
        facts.stop.CopyFrom(schema.StopFacts(state=3,pi_session=vehicle.pi_session,device_session=vehicle.device_session,
            command_sequence=7,control_generation=case['grant'].control_generation,producer_role=1,
            source_vm=case['grant'].owner_vm,source_execution=case['grant'].owner_execution,source_app_instance=case['grant'].app_instance,
            calculation_sequence=first_stop_calculation,counter=100,applied_report_sequence=1,applied_source_sample=first_stop_sample.source_sample,
            applied_pi_lower_us=first_stop_sample.pi_sample_us-first_stop_sample.clock_error_us,
            applied_pi_upper_us=first_stop_sample.pi_sample_us+first_stop_sample.clock_error_us,
            stationary_first_sequence=first.sequence,stationary_last_sequence=vehicle.sequence,
            stationary_started_pi_us=first.pi_sample_us+first.clock_error_us,
            stationary_observed_pi_us=vehicle.pi_sample_us-vehicle.clock_error_us,
            valid_until_pi_us=stamp+100000,left_speed_bound_mps=.001,right_speed_bound_mps=.001,yaw_rate_bound_rps=.001,
            plan_revision=1,phase_revision=core.phase_revision,mission_id=case['grant'].mission_id,
            mission_revision=case['grant'].mission_revision,grant_revision=case['grant'].grant_revision,
            driver_session=bytes(16),driver_action_seq=0,driver_state_seq=0))
        core.stop_wait_facts_sha256=route_stop_facts_sha256(facts)
        case['snapshot'].acceptance.accepted_core_sha256=route_core_sha256(core)
        case['grant'].route_binding.core_sha256=case['snapshot'].acceptance.accepted_core_sha256
        assert case['grant'].scene_admission_id==case['world'].scene.admission_id
        assert case['grant'].scene_revision==case['world'].scene.admission_revision
        terminal_inputs.update({key+'_wire_hex':encode(case[key]).hex() for key in ('world','snapshot','grant','facts')})
        terminal_inputs['vehicle_wire_hex']=encode(vehicle).hex()
        for kind,key in ((12,'snapshot'),(13,'grant'),(14,'facts')):put(route_library,kind,case[key])
        stage='matching-declared-stop'
        terminal_results['matching']=route_library.route_probe_terminal(stamp+100)
        assert terminal_results['matching']==OK,terminal_results
        # The original stale candidate produced this NIL/revision0 grant. A
        # fresh physical STOP must not authorize a different declared Scene.
        stage='mismatched-scene-negative'
        old_scene=deepcopy(case['grant']);old_scene.scene_admission_id=bytes(16);old_scene.scene_revision=0
        terminal_inputs['mismatched_scene_grant_wire_hex']=encode(old_scene).hex()
        put(route_library,13,old_scene)
        terminal_results['mismatched_scene']=route_library.route_probe_terminal(stamp+100)
        assert terminal_results['mismatched_scene']==10
        put(route_library,13,case['grant'])
        # Original source03's instant-stop pose was outside the global goal's
        # Vehicle+StartGoal uncertainty region. Even matching fresh STOP is insufficient.
        premature=deepcopy(vehicle);premature.odom_pose.x=3.8912033052742387
        assert abs(premature.odom_pose.x-case['records']['goal'].rear_axle_pose.x)+vehicle.position_error_m+case['records']['goal'].position_error_m>goal_distance
        put(route_library,11,premature)
        assert route_library.route_probe_terminal(stamp+100)==10
        wrong_pose=deepcopy(vehicle);wrong_pose.odom_pose.x-=.31;put(route_library,11,wrong_pose)
        assert route_library.route_probe_terminal(stamp+100)==10
        put(route_library,11,vehicle)
        facts.stop.state=2;put(route_library,14,facts)
        assert route_library.route_probe_terminal(stamp+100)==10
        facts.stop.state=3;facts.stop.command_sequence+=1;put(route_library,14,facts)
        assert route_library.route_probe_terminal(stamp+100)==10
        stage='goal-region-and-stop-negatives-complete'
    finally:
        save_trace(stage)
