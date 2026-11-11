"""Real input -> core planner -> tracker -> full state application, no fake path."""
import ctypes as C
import math
import struct
from uuid import UUID
import pytest
from test_lattice import library,fixture,encode,obstacle
from world_codec import schema,decode
from test_world import modern_world as _modern_world

def modern_world():
    value=_modern_world();value.reference_vehicle_pose.y=1.2
    return value

def exported_state(lib,engine):
    out=C.create_string_buffer(32768);n=lib.engine_state(engine,out,len(out));assert n>0
    return decode(out.raw[:n],schema.DriveState)


def lane_grant(*,revision=3):
    """합성 Pi issuer receipt. 실제 Authority/장치 승인이나 운영 설정은 아니다."""
    link=schema.DriveLink(major=2)
    link.mission_grant.CopyFrom(schema.MissionGrant(major=1,vehicle='rc-test-1',
        pi_host_boot=UUID(fixture.BOOT).bytes,gateway_instance=bytes.fromhex('b1'*16),
        owner_vm=UUID('77777777-7777-4777-8777-777777777777').bytes,
        owner_execution=UUID('88888888-8888-4888-8888-888888888888').bytes,
        app_instance=UUID('99999999-9999-4999-8999-999999999999').bytes,control_generation=1,
        mission_id=bytes.fromhex('a1'*16),mission_revision=2,grant_revision=revision,kind=1,strategy=0,
        operator_request_nonce=bytes.fromhex('c1'*16),issued_pi_us=1000000,mission_deadline_pi_us=0,
        cancel_fence_revision=4,scene_admission_id=bytes(16),scene_revision=0,configuration='lattice-lab-1',
        calibration='encoder-lab-1',primitive=bytes.fromhex(fixture.PRIMITIVE),execution_deadline_pi_us=0,
        correction_attempts_used=0,execution_travel_m=0,max_execution_travel_m=0,admitted=True,
        search_deadline_pi_us=0,search_travel_m=0,max_search_travel_m=0))
    return link




def vehicle(seq=1,*,x=0.,at=1200000):
    return schema.VehicleSample(major=1,vehicle='rc-test-1',host_boot=fixture.BOOT,
        device_session=fixture.INSTANCE,odom_epoch=fixture.ODOM,sequence=seq,source_sample=seq,
        source_us=at-100000,pi_sample_us=at,clock_error_us=500,left_ticks=seq,right_ticks=seq,
        odom_pose=schema.Pose(x=x,y=0,yaw=0),velocity_mps=0.,estimated_steering_rad=0.,
        position_error_m=.005,heading_error_rad=.01,steering_error_rad=.001,quality=3,calibration='encoder-lab-1')


def send_vehicle(lib,engine,v):
    data=encode(v);assert lib.engine_vehicle(engine,data,len(data))==0


def load(lib,engine,world,at=1200000):
    data=encode(world);assert lib.engine_world(engine,data,len(data),at)==0


def test_full_product_state_is_rechecked_and_calculated_before_adoption(library):
    a=library.engine_new();b=library.engine_new();world=fixture.world()
    obstacle(world,19,24,13,19)
    try:
        for e in (a,b):
            load(library,e,world);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(a,1220000,4096)==0
        command=(C.c_float*4)();assert library.engine_track(a,1220000,command)==0
        assert command[0]>0 # Real selected tracker completion, not a readiness timer.
        output=C.create_string_buffer(32768);length=library.engine_state(a,output,len(output))
        assert length>0
        state=decode(output.raw[:length],schema.DriveState)
        assert len(state.path)>50 and state.calculation_sequence==1
        assert library.engine_apply(b,output.raw,length,1220000)==0
        assert library.engine_track(b,1220000,command)==0 and command[0]>0
        state.path[len(state.path)//2].pose.y=99
        bad=encode(state)
        assert library.engine_apply(b,bad,len(bad),1220000)==9
        # Rejected incoming state does not overwrite the validated path.
        assert library.engine_track(b,1220000,command)==0 and command[0]>0
    finally:library.engine_free(a);library.engine_free(b)


def test_state_fixture_keeps_the_actual_calculation_sample_after_new_input(library):
    e=library.engine_new();world=fixture.world();command=(C.c_float*4)()
    try:
        load(library,e,world);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        assert exported_state(library,e).calculation_sequence==0
        assert library.engine_track(e,1220000,command)==0
        completed=exported_state(library,e);assert completed.vehicle_sequence==2 and completed.calculation_sequence==1
        send_vehicle(library,e,vehicle(3,at=1240000))
        unchanged=exported_state(library,e)
        assert unchanged.vehicle_sequence==2 and unchanged.calculation_sequence==1
        assert unchanged.observation_seq==completed.observation_seq
        assert library.engine_track(e,1240000,command)==0
        latest=exported_state(library,e)
        assert latest.vehicle_sequence==3 and latest.calculation_sequence==2
    finally:library.engine_free(e)


def test_missing_exact_anchor_expiry_then_real_fresh_input_recovery(library):
    e=library.engine_new();world=fixture.world();command=(C.c_float*4)()
    try:
        load(library,e,world);send_vehicle(library,e,vehicle(2))
        assert library.engine_plan(e,1200000,4096)==5
        # The missing exact historical sample cannot be replaced by latest.
        library.engine_free(e);e=library.engine_new()
        load(library,e,world);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        original=exported_state(library,e)
        assert original.calculation_sequence==0  # Prepared geometry has not run the actual tracker.
        assert library.engine_track(e,1700000,command)==7 and command[0]==0
        world.sequence=2;world.observation_seq=2;world.observed_us=1600000
        world.completed_us=1601000;world.published_us=1602000;world.reference_odom_seq=3
        send_vehicle(library,e,vehicle(3,at=1700000));load(library,e,world,at=1705000)
        assert library.engine_track(e,1705000,command)==6 and command[0]==0
        # Like the runtime planner owner, consume the new map request. Reusing
        # old geometry cannot refund its stale first correspondence.
        assert library.engine_plan(e,1705000,4096)==0
        prepared=exported_state(library,e)
        assert prepared.path_revision>original.path_revision and prepared.calculation_sequence==0
        assert library.engine_track(e,1705000,command)==0 and command[0]>0
        assert exported_state(library,e).vehicle_sequence==3
    finally:library.engine_free(e)


def test_refreshed_clock_upper_bound_preserves_original_observation_deadline(library):
    e=library.engine_new();w=fixture.world();out=(C.c_float*4)()
    try:
        send_vehicle(library,e,vehicle(at=1100000));send_vehicle(library,e,vehicle(2,at=1190000))
        load(library,e,w,at=1220000)
        assert library.engine_plan(e,1220000,4096)==0
        # Both valid clock intervals bound the same Pi clock. A tighter upper
        # bound may be lower than the previous receive estimate; the original
        # observation at Pi 1,100,000us still expires at 1,400,000us.
        assert library.engine_track(e,1215000,out)==0 and out[0]>0
        send_vehicle(library,e,vehicle(3,at=1400001))
        assert library.engine_track(e,1400001,out)==7 and out[0]==0
    finally:library.engine_free(e)


def test_map_before_anchor_keeps_valid_current_then_adopts_and_recovers(library):
    e=library.engine_new();w=fixture.world();out=(C.c_float*4)()
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        newer=fixture.world();newer.sequence=2;newer.observation_seq=2;newer.reference_odom_seq=3
        newer.occupancy=bytes([100])*len(newer.occupancy)
        load(library,e,newer,at=1230000)
        # Completed map arrived through another broker before its exact anchor.
        # It neither invents the anchor nor replaces the previous usable map.
        assert library.engine_track(e,1230000,out)==0 and out[0]>0
        send_vehicle(library,e,vehicle(3,at=1240000))
        assert library.engine_track(e,1240000,out)==6 and out[0]==0
        w.sequence=3;w.observation_seq=3;w.reference_odom_seq=4
        send_vehicle(library,e,vehicle(4,at=1260000));load(library,e,w,at=1260000)
        assert library.engine_plan(e,1260000,4096)==0
        assert library.engine_track(e,1260000,out)==0 and out[0]>0
    finally:library.engine_free(e)


def test_fresh_instance_near_road_end_rebuilds_with_swept_endpoint_reserve(library):
    e=library.engine_new();w=fixture.world()
    w.reference_vehicle_pose.CopyFrom(schema.Pose(x=4.5282,y=1.5311,yaw=.0051))
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        command=(C.c_float*4)();assert library.engine_track(e,1220000,command)==0 and command[0]>0
        out=C.create_string_buffer(32768);n=library.engine_state(e,out,len(out));assert n>0
        state=decode(out.raw[:n],schema.DriveState)
        # Independent fixture geometry: road ends x=6; corner radius includes
        # 0.010m half-step sweep. No planner output makes this expected value.
        import math
        assert abs(state.merge_goal.x-(6-math.hypot(.415,.215)))<.0001
    finally:library.engine_free(e)


@pytest.mark.parametrize('rotation',[0.,.4])
def test_curved_reference_goal_respects_rotated_grid_and_swept_end_reserve(library,rotation):
    e=library.engine_new();w=fixture.world(height=64);radius=2.
    cs,sn=math.cos(rotation),math.sin(rotation)
    w.origin.CopyFrom(schema.Pose(x=2,y=-1,yaw=rotation))
    def point(x,y):return {'x':2+cs*x-sn*y,'y':-1+sn*x+cs*y}
    for group in (w.reference,w.left_boundary,w.right_boundary):del group[:]
    for k in range(39):
        angle=-.2+k*.08;x=1.1+radius*math.sin(angle);y=3.2+radius*(1-math.cos(angle))
        nx,ny=-math.sin(angle),math.cos(angle)
        w.reference.add(**point(x,y));w.left_boundary.add(**point(x+nx*.7,y+ny*.7))
        w.right_boundary.add(**point(x-nx*.7,y-ny*.7))
    w.reference_vehicle_pose.CopyFrom(schema.Pose(**point(1.1,3.2),yaw=rotation))
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        out=(C.c_float*4)();assert library.engine_track(e,1220000,out)==0 and out[0]>0
        state=exported_state(library,e)
        reserve=math.hypot(.415,.215)
        local_goal_y=-(state.merge_goal.x-2)*sn+(state.merge_goal.y+1)*cs
        assert 5.8<local_goal_y<=6.4-reserve+1e-5
        for sample in state.path:
            c,s=math.cos(sample.pose.yaw),math.sin(sample.pose.yaw)
            for u in (-.125,.325):
                for v in (-.125,.125):
                    x=sample.pose.x+c*u-s*v-2;y=sample.pose.y+s*u+c*v+1
                    assert 0<=x*cs+y*sn<6.4 and 0<=-x*sn+y*cs<6.4
    finally:library.engine_free(e)


def exported_state(lib,engine):
    out=C.create_string_buffer(32768);n=lib.engine_state(engine,out,len(out));assert n>0
    return decode(out.raw[:n],schema.DriveState)


def test_blocked_automatic_terminal_advances_then_plans_past_obstacle(library):
    e=library.engine_new();w=fixture.world(height=64);out=(C.c_float*4)()
    # The original 4.5m target is x=4.8, inside this actual occupied box.
    obstacle(w,46,51,12,20)
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        first=exported_state(library,e)
        assert .6<first.path[-1].pose.x<4.4
        assert library.engine_track(e,1220000,out)==0 and out[0]>0
        # A later rolling observation extends beyond the same obstacle. The
        # automatic local target must not remain pinned before it forever.
        rolling_road(library,e,3,1300000,2.,1.6,0,blocks=((36,41,12,20),))
        assert library.engine_plan(e,1300000,4096)==0
        advanced=exported_state(library,e)
        assert advanced.path[-1].pose.x>5.8 and advanced.direction!=0
        assert library.engine_track(e,1300000,out)==0 and out[0]>0
    finally:library.engine_free(e)


def test_no_forward_terminal_stops_then_fresh_clear_map_recovers(library):
    e=library.engine_new();w=fixture.world(height=64);out=(C.c_float*4)()
    # The measured start remains free; every forward terminal's inflated front
    # intersects this wall. Retargeting cannot convert that into a usable path.
    obstacle(w,7,64,2,30)
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==6
        assert library.engine_track(e,1220000,out)!=0 and out[0]==0
        rolling_road(library,e,3,1300000,.3,1.6,0)
        assert library.engine_plan(e,1300000,4096)==0
        assert library.engine_track(e,1300000,out)==0 and out[0]>0
    finally:library.engine_free(e)


def rolling_road(lib,engine,sequence,at,x,y,yaw,steer=0,reference_revision=1,blocks=()):
    # Unit observations explicitly move the fixture. Circuit replay uses an
    # independently integrated vehicle instead of this state-placement helper.
    v=vehicle(sequence,at=at);v.odom_pose.CopyFrom(schema.Pose(x=x-.3,y=y-1.6,yaw=yaw))
    v.estimated_steering_rad=steer;v.velocity_mps=.4;send_vehicle(lib,engine,v)
    w=fixture.world(height=64);w.sequence=sequence;w.observation_seq=sequence;w.reference_odom_seq=sequence
    w.reference_revision=reference_revision
    w.observed_us=at-110000;w.completed_us=at-109000;w.published_us=at-108000
    w.origin.x=x-1.;w.reference_vehicle_pose.CopyFrom(schema.Pose(x=x,y=y,yaw=yaw))
    for group in (w.reference,w.left_boundary,w.right_boundary):del group[:]
    for k in range(7):
        px=x-1.+k
        w.reference.add(x=px,y=1.6);w.left_boundary.add(x=px,y=3.);w.right_boundary.add(x=px,y=.2)
    for bounds in blocks:obstacle(w,*bounds)
    load(lib,engine,w,at=at)


@pytest.mark.parametrize('bypass',[False,True])
def test_consumed_local_path_advances_without_losing_real_bypass_intent(library,bypass):
    e=library.engine_new();w=fixture.world(height=64);out=(C.c_float*4)()
    if bypass:obstacle(w,19,24,13,19)
    else:w.reference_vehicle_pose.y=1.45
    try:
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        assert library.engine_track(e,1220000,out)==0 and out[0]>0
        original=exported_state(library,e)
        assert (original.direction!=0)==bypass
        # Same observed failure: inside XY tolerance, but heading still differs.
        rolling_road(library,e,3,1300000,original.merge_goal.x,1.6,.278)
        assert library.engine_plan(e,1300000,4096)==0
        assert library.engine_track(e,1300000,out)==0 and out[0]>0
        advanced=exported_state(library,e)
        assert advanced.vehicle_sequence==3 and advanced.observation_seq==3
        assert advanced.path[-1].pose.x>original.merge_goal.x+2
        assert advanced.path_revision>original.path_revision
        assert library.engine_track(e,1300000,out)==0 and out[0]>0
        if bypass:
            assert advanced.direction==original.direction
            assert advanced.merge_goal==original.merge_goal
            assert advanced.maneuver_revision==original.maneuver_revision
            # A fresh Standby gets the persistent merge intent and the actual
            # forward execution path, rather than driving back to that intent.
            standby=library.engine_new()
            try:
                rolling_road(library,standby,3,1300000,original.merge_goal.x,1.6,.278)
                wire=encode(advanced)
                assert library.engine_apply(standby,wire,len(wire),1300000)==0
                assert library.engine_track(standby,1300000,out)==0 and out[0]>0
                assert exported_state(library,standby).merge_goal==original.merge_goal
            finally:library.engine_free(standby)
            # Heading alone inside the limit is insufficient when observation
            # uncertainty pushes its total beyond the original allowance.
            rolling_road(library,e,4,1400000,original.merge_goal.x+.2,1.6,.215)
            assert library.engine_plan(e,1400000,4096)==0
            assert exported_state(library,e).direction==original.direction
            rolling_road(library,e,5,1500000,original.merge_goal.x+.3,1.6,0)
            assert library.engine_plan(e,1500000,4096)==0
            settled=exported_state(library,e)
            assert settled.direction==0 and settled.maneuver_revision>original.maneuver_revision
        else:assert advanced.direction==0
    finally:library.engine_free(e)


def test_merge_passage_survives_handoff_and_clipped_reference(library):
    source=library.engine_new();target=library.engine_new();legacy=library.engine_new();out=(C.c_float*4)()
    world=fixture.world(height=64);obstacle(world,19,24,13,19)
    try:
        load(library,source,world);send_vehicle(library,source,vehicle());send_vehicle(library,source,vehicle(2,at=1220000))
        assert library.engine_plan(source,1220000,4096)==0
        assert library.engine_track(source,1220000,out)==0
        original=exported_state(library,source);assert original.direction
        x=original.merge_goal.x
        # Confirm actual passage while the milestone still belongs to this road.
        # The heading remains unaligned, so passage must not complete rejoining.
        rolling_road(library,source,3,1300000,x+.2,1.6,.278)
        assert library.engine_plan(source,1300000,4096)==0
        assert library.engine_track(source,1300000,out)==0 and out[0]>0
        state=exported_state(library,source)
        assert state.vehicle_sequence==3 and state.observation_seq==3
        assert state.merge_passed_reference_revision==1 and state.direction==original.direction
        assert state.merge_goal==original.merge_goal and state.maneuver_revision==original.maneuver_revision
        # Exercise the common binary envelope used by the real Gateway relay.
        link=schema.DriveLink(major=2);link.accepted.host_boot=fixture.BOOT
        link.accepted.gateway_instance=fixture.INSTANCE;link.accepted.state.CopyFrom(state)
        accepted=decode(encode(link),schema.DriveLink).accepted.state
        for engine,payload in ((target,accepted),(legacy,decode(encode(state),schema.DriveState))):
            if engine==legacy:payload.ClearField('merge_passed_reference_revision')
            rolling_road(library,engine,3,1300000,x+.2,1.6,.278)
            wire=encode(payload)
            assert library.engine_apply(engine,wire,len(wire),1300000)==0
            assert library.engine_track(engine,1300000,out)==0 and out[0]>0
        assert exported_state(library,target).merge_passed_reference_revision==1
        assert not exported_state(library,legacy).HasField('merge_passed_reference_revision')
        bad=schema.DriveState();bad.CopyFrom(accepted);bad.merge_passed_reference_revision=0
        raw=bad.SerializeToString()
        assert library.engine_apply(target,raw,len(raw),1300000)==106
        assert library.engine_track(target,1300000,out)==0 and out[0]>0
        assert exported_state(library,target).merge_passed_reference_revision==1
        duplicate=encode(accepted)+b'\xd8\x01\x01'
        with pytest.raises(ValueError,match='duplicate'):decode(duplicate,schema.DriveState)
        assert library.engine_apply(target,duplicate,len(duplicate),1300000)==103
        # Move only after adoption, keeping the incoming full path inside the
        # recipient's current map at adoption. Then the old milestone is clipped.
        for engine in (target,legacy):
            rolling_road(library,engine,4,1400000,x+1.8,1.6,.278)
            assert library.engine_plan(engine,1400000,4096)==0
            assert library.engine_track(engine,1400000,out)==0 and out[0]>0
        assert exported_state(library,target).merge_passed_reference_revision==1
        assert not exported_state(library,legacy).HasField('merge_passed_reference_revision')
        # Neither disappearance of the old point nor nearest projection onto a
        # far road endpoint may be used as new passage evidence.
        for engine in (target,legacy):
            rolling_road(library,engine,5,1500000,x+2.,1.6,0)
            assert library.engine_plan(engine,1500000,4096)==0
            assert library.engine_track(engine,1500000,out)==0 and out[0]>0
        settled=exported_state(library,target);unconfirmed=exported_state(library,legacy)
        assert settled.direction==0 and not settled.HasField('merge_passed_reference_revision')
        assert settled.maneuver_revision>state.maneuver_revision
        assert unconfirmed.direction==state.direction
        assert unconfirmed.path[-1].pose.x>x+4 # No return to the clipped milestone.
    finally:
        for engine in (source,target,legacy):library.engine_free(engine)


def test_passed_merge_survives_real_direction_change_and_binary_handoff(library):
    source=library.engine_new();target=library.engine_new();out=(C.c_float*4)()
    world=fixture.world(height=64);obstacle(world,19,24,13,19)
    try:
        load(library,source,world);send_vehicle(library,source,vehicle());send_vehicle(library,source,vehicle(2,at=1220000))
        assert library.engine_plan(source,1220000,4096)==0
        assert library.engine_track(source,1220000,out)==0
        original=exported_state(library,source);assert original.direction
        x=original.merge_goal.x
        rolling_road(library,source,3,1300000,x+.2,1.6,.278)
        assert library.engine_plan(source,1300000,4096)==0
        assert library.engine_track(source,1300000,out)==0 and out[0]>0
        passed=exported_state(library,source);assert passed.merge_passed_reference_revision==1
        # A real new occupied corridor forces the opposite bypass. The old
        # milestone is behind this rolling reference, not a newly chosen goal.
        blocked_side=(23,34,19,30) if original.direction>0 else (23,34,2,13)
        heading=-.278*original.direction
        rolling_road(library,source,4,1400000,x+1.8,1.6,heading,
                     blocks=((26,31,13,19),blocked_side))
        assert library.engine_plan(source,1400000,4096)==0
        assert library.engine_track(source,1400000,out)==0 and out[0]>0
        changed=exported_state(library,source)
        assert changed.vehicle_sequence==4 and changed.observation_seq==4
        assert changed.direction==-original.direction
        assert changed.merge_goal==passed.merge_goal
        assert changed.maneuver_revision>passed.maneuver_revision
        assert changed.merge_passed_reference_revision==1
        link=schema.DriveLink(major=2);link.accepted.host_boot=fixture.BOOT
        link.accepted.gateway_instance=fixture.INSTANCE;link.accepted.state.CopyFrom(changed)
        accepted=decode(encode(link),schema.DriveLink).accepted.state
        rolling_road(library,target,4,1400000,x+1.8,1.6,heading,
                     blocks=((26,31,13,19),blocked_side))
        wire=encode(accepted);assert library.engine_apply(target,wire,len(wire),1400000)==0
        assert library.engine_track(target,1400000,out)==0 and out[0]>0
        adopted=exported_state(library,target)
        assert adopted.merge_passed_reference_revision==1 and adopted.direction==changed.direction
        assert adopted.merge_goal==changed.merge_goal
        # Once actual current road/heading/steering guards hold, both the
        # original and the recipient close the same maneuver without returning
        # to the clipped milestone. Direction change is not itself completion.
        for engine in (source,target):
            rolling_road(library,engine,5,1500000,x+3.6,1.6,0)
            assert library.engine_plan(engine,1500000,4096)==0
            assert library.engine_track(engine,1500000,out)==0 and out[0]>0
            settled=exported_state(library,engine)
            assert settled.direction==0 and not settled.HasField('merge_passed_reference_revision')
            assert settled.maneuver_revision>changed.maneuver_revision
    finally:
        library.engine_free(source);library.engine_free(target)


def test_road_policy_binary_handoff_and_policy_mismatch_preserve_current_execution(library):
    from xenova_drive_world import mark_road
    source=library.engine_new();target=library.engine_new();out=(C.c_float*4)()
    w=fixture.world()
    mark_road(w,[schema.Point(x=p.x,y=1.96) for p in w.reference],
                [schema.Point(x=p.x,y=1.24) for p in w.reference])
    try:
        for engine in (source,target):
            load(library,engine,w);send_vehicle(library,engine,vehicle());send_vehicle(library,engine,vehicle(2,at=1220000))
        assert library.engine_plan(source,1220000,4096)==0
        assert library.engine_track(source,1220000,out)==0 and out[0]>0
        state=exported_state(library,source);assert state.world_features==1
        link=schema.DriveLink(major=2);link.accepted.host_boot=fixture.BOOT
        link.accepted.gateway_instance=fixture.INSTANCE;link.accepted.state.CopyFrom(state)
        accepted=decode(encode(link),schema.DriveLink).accepted.state
        assert accepted.world_features==1
        wire=encode(accepted);assert library.engine_apply(target,wire,len(wire),1220000)==0
        assert library.engine_track(target,1220000,out)==0 and out[0]>0
        legacy=schema.DriveState();legacy.CopyFrom(accepted);legacy.ClearField('world_features')
        wire=encode(legacy);assert library.engine_apply(target,wire,len(wire),1220000)==14
        assert library.engine_track(target,1220000,out)==0 and out[0]>0
        unsupported=schema.DriveState();unsupported.CopyFrom(state);unsupported.world_features=2
        with pytest.raises(ValueError,match='world features'):encode(unsupported)
        raw=unsupported.SerializeToString();assert library.engine_apply(target,raw,len(raw),1220000)==104
        assert exported_state(library,target).world_features==1
    finally:library.engine_free(source);library.engine_free(target)


def test_invalid_merge_passage_rejected_and_new_reference_does_not_inherit_it(library):
    e=library.engine_new();target=library.engine_new();out=(C.c_float*4)()
    world=fixture.world(height=64);obstacle(world,19,24,13,19)
    try:
        load(library,e,world);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_plan(e,1220000,4096)==0
        assert library.engine_track(e,1220000,out)==0
        state=exported_state(library,e);x=state.merge_goal.x
        rolling_road(library,e,3,1300000,x+.2,1.6,.278)
        assert library.engine_plan(e,1300000,4096)==0
        assert library.engine_track(e,1300000,out)==0
        state=exported_state(library,e);assert state.merge_passed_reference_revision==1
        assert state.vehicle_sequence==3 and state.observation_seq==3
        rolling_road(library,target,3,1300000,x+.2,1.6,.278,reference_revision=2)
        for bad in (0,2):
            changed=schema.DriveState();changed.CopyFrom(state);changed.merge_passed_reference_revision=bad
            with pytest.raises(ValueError,match='merge passage'):encode(changed)
            raw=changed.SerializeToString() # Also verify C's raw-input boundary.
            assert library.engine_apply(target,raw,len(raw),1300000)==106
        cleared=schema.DriveState();cleared.CopyFrom(state);cleared.direction=0
        with pytest.raises(ValueError,match='merge passage'):encode(cleared)
        raw=cleared.SerializeToString();assert library.engine_apply(target,raw,len(raw),1300000)==106
        wire=encode(state)
        assert library.engine_apply(target,wire,len(wire),1300000)==0
        assert library.engine_track(target,1300000,out)==0 and out[0]>0
        assert not exported_state(library,target).HasField('merge_passed_reference_revision')
        rolling_road(library,target,4,1400000,x+2.,1.6,0,reference_revision=2)
        assert library.engine_plan(target,1400000,4096)==0
        assert library.engine_track(target,1400000,out)==0 and out[0]>0
        assert exported_state(library,target).direction==state.direction
    finally:library.engine_free(e);library.engine_free(target)

def test_real_owner_memory_search_failure_then_normal_progress(library):
    library.engine_memory.argtypes=[C.c_void_p,C.c_uint,C.POINTER(C.c_uint64)]
    library.engine_force_node_limit.argtypes=[C.c_void_p,C.c_uint64]
    e=library.engine_new()
    def memory(region):
        data=(C.c_uint64*6)()
        assert library.engine_memory(e,region,data)==0
        reserved,capacity,used,peak,failures,busy=data
        assert 0<=used<=peak<=capacity<=reserved
        return list(data)
    try:
        assert all(memory(i)[2]==0 for i in range(4))
        w=fixture.world();obstacle(w,19,24,13,19)
        load(library,e,w);send_vehicle(library,e,vehicle());send_vehicle(library,e,vehicle(2,at=1220000))
        assert library.engine_force_node_limit(e,1220000)==6
        failed=memory(2);assert failed[4]==1 and failed[2]>0
        # A new real observation requests another bounded plan; no fake READY.
        w.sequence=2;w.observation_seq=2
        load(library,e,w)
        assert library.engine_plan(e,1220000,4096)==0
        out=(C.c_float*4)()
        assert library.engine_track(e,1220000,out)==0 and out[0]>0
        assert all(memory(i)[2]>0 for i in range(4))
        assert memory(2)[4]==failed[4] and memory(2)[3]>=failed[3]
    finally:
        library.engine_free(e)


def test_state2_metadata_survives_actual_apply_and_next_export_without_minting_authority(library):
    source=library.engine_new();target=library.engine_new();world=modern_world()
    grant=lane_grant();grant_wire=encode(grant)
    try:
        for engine in (source,target):
            load(library,engine,world);send_vehicle(library,engine,vehicle());send_vehicle(library,engine,vehicle(2,at=1220000))
        assert library.engine_grant(source,grant_wire,len(grant_wire),1220000)==0
        assert library.engine_plan(source,1220000,4096)==0
        command=(C.c_float*4)()
        assert library.engine_track_grant(source,grant_wire,len(grant_wire),1220000,command)==0
        state=exported_state(library,source)
        assert state.major==2 and state.state_features==15 and state.mission.kind==1
        assert state.HasField("task_reference") and state.task_reference.selected_lane_id==1
        assert state.mission.phase==10 and state.mission.grant_revision==3
        assert state.mission.mission_id==grant.mission_grant.mission_id
        assert len(state.segments)==1 and state.segments[0].signed_direction==1
        # 합성 gate 진술의 보존만 확인한다. 실제 stationary/servo 사건을 만들지 않는다.
        state.direction_gate.CopyFrom(schema.DirectionGateRef(gate_revision=5,stop_command_sequence=9,
            state=2,requested_direction=1))
        wire=encode(state)
        assert library.engine_apply(target,wire,len(wire),1220000)==0
        assert library.engine_track_grant(target,grant_wire,len(grant_wire),1220000,command)==0
        copied=exported_state(library,target)
        assert copied.mission==state.mission
        assert copied.segments==state.segments
        assert copied.direction_gate==state.direction_gate
        changed=lane_grant(revision=4);changed_wire=encode(changed)
        assert library.engine_track_grant(target,changed_wire,len(changed_wire),1220000,command)==9
        assert command[0]==0
        assert exported_state(library,target).mission==state.mission
    finally:library.engine_free(source);library.engine_free(target)


def test_new_world_whole_cell_permission_is_consumed_by_the_actual_collision_planner(library):
    for allowed in (True,False):
        engine=library.engine_new();world=modern_world()
        if not allowed:world.drivable_permission=bytes(len(world.drivable_permission))
        try:
            send_vehicle(library,engine,vehicle());send_vehicle(library,engine,vehicle(2,at=1220000))
            load(library,engine,world)
            result=library.engine_plan(engine,1220000,4096)
            assert (result==0)==allowed
        finally:library.engine_free(engine)


def test_localization_lease_expires_without_renewing_to_world_or_vehicle_deadline(library):
    engine=library.engine_new();world=modern_world();world.localization.valid_for_us=200000
    grant_wire=encode(lane_grant())
    try:
        send_vehicle(library,engine,vehicle());send_vehicle(library,engine,vehicle(2,at=1220000))
        load(library,engine,world)
        assert library.engine_grant(engine,grant_wire,len(grant_wire),1220000)==0
        assert library.engine_plan(engine,1220000,4096)==0
        command=(C.c_float*4)();assert library.engine_track_grant(engine,grant_wire,len(grant_wire),1220000,command)==0
        send_vehicle(library,engine,vehicle(3,at=1320000))
        assert library.engine_track_grant(engine,grant_wire,len(grant_wire),1320000,command)==7
        assert command[0]==0
    finally:library.engine_free(engine)


def test_legacy_auto_profiles_keep_none_candidate_while_lane_profile_requires_original_grant(library):
    from xenova_drive_world import mark_road
    for feature in (0,1,11):
        engine=library.engine_new();world=modern_world() if feature==11 else fixture.world()
        if feature==1:
            mark_road(world,[schema.Point(x=p.x,y=1.96) for p in world.reference],
                            [schema.Point(x=p.x,y=1.24) for p in world.reference])
        try:
            send_vehicle(library,engine,vehicle());send_vehicle(library,engine,vehicle(2,at=1220000))
            load(library,engine,world)
            assert library.engine_plan(engine,1220000,4096)==0
            command=(C.c_float*4)();result=library.engine_track(engine,1220000,command)
            if feature==11:
                assert result==9 and command[0]==0
            else:
                assert result==0 and command[0]>0
                state=exported_state(library,engine)
                assert state.major==2 and state.state_features==7 and state.mission.kind==0
                assert state.mission.mission_id==bytes(16) and state.world_features==feature
        finally:library.engine_free(engine)


# Explicit unit issuer/clock leaf. The separate actual app/Gateway integration
# exercises original P4, actor registry and first fullState; these tests do not.
def _bootstrap_abi(lib):
    lib.engine_behavior_enable_for_budget.argtypes=[C.c_void_p]
    lib.engine_fixture_auto_owner.argtypes=[C.c_void_p]
    lib.engine_fixture_phase_id.argtypes=[C.c_void_p,C.c_void_p]
    lib.engine_fixture_facts.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t,C.c_uint64]
    lib.engine_fixture_status.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t,C.c_uint64]
    lib.engine_fixture_registration.argtypes=[C.c_void_p,C.c_uint64,C.c_void_p,C.c_size_t]
    lib.engine_fixture_receipt.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t]
    lib.engine_fixture_continue.argtypes=[C.c_void_p,C.c_uint64,C.c_uint]
    lib.engine_fixture_begin.argtypes=[C.c_void_p,C.c_uint64]
    lib.engine_fixture_adoption_view.argtypes=[C.c_void_p,C.POINTER(C.c_uint64),C.c_void_p]


def _bootstrap_view(lib,e):
    data=(C.c_uint64*16)();hashes=C.create_string_buffer(64)
    assert lib.engine_fixture_adoption_view(e,data,hashes)==0
    return list(data),hashes.raw


def _bootstrap_phase_id(lib,e):
    out=C.create_string_buffer(37);assert lib.engine_fixture_phase_id(e,out)==0
    return out.value.decode('ascii')


def _bootstrap_status(snapshot,*,commit,now):
    from xenova_drive_world import schema
    return schema.EpisodeStatus(major=1,state=snapshot.core.state,
        registry_epoch=snapshot.acceptance.registry_epoch,registry_revision=snapshot.acceptance.registry_revision,
        accepted_core_sha256=snapshot.acceptance.accepted_core_sha256,episode_id=snapshot.core.episode_id,
        episode_revision=snapshot.core.revision,commit_state=commit,state_message_id=b's'*16 if commit==2 else bytes(16),
        state_sha256=b't'*32 if commit==2 else bytes(32),state_revision=1 if commit==2 else 0,
        plan_revision=1 if commit==2 else 0,phase_revision=1 if commit==2 else 0,
        calculation_hwm=1 if commit==2 else 0,vehicle_hwm=2 if commit==2 else 0,valid_until_pi_us=now+1000000)


def _bootstrap_facts(lib,e,v,*,now,gap=0):
    from xenova_drive_world import schema
    facts=schema.DriveControlFacts(major=1,meter=schema.PiAbsoluteMeter(meter_epoch=b'm'*16,total_ticks=v.left_ticks,
        metres_per_tick=.001,gap_revision=gap,valid=True,vehicle_sequence=v.sequence),valid_until_pi_us=v.pi_sample_us+180000)
    wire=encode(facts);return lib.engine_fixture_facts(e,wire,len(wire),now),facts


def _bootstrap_vehicle(seq,at):
    v=vehicle(seq,at=at);v.left_ticks=v.right_ticks=100
    v.pi_session='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';v.gateway_instance='b1b1b1b1-b1b1-b1b1-b1b1-b1b1b1b1b1b1'
    return v


def _registered_bootstrap(lib,*,defer_search=False):
    from xenova_drive_world import (schema,initial_none_core,initial_none_snapshot,episode_core_sha256,
                                    episode_request_sha256)
    _bootstrap_abi(lib);e=lib.engine_new();world=modern_world();grant=lane_grant()
    try:
        obstacle(world,14,17,10,14)
        load(lib,e,world);send_vehicle(lib,e,_bootstrap_vehicle(1,1200000));v=_bootstrap_vehicle(2,1220000);send_vehicle(lib,e,v)
        raw=encode(grant);assert lib.engine_grant(e,raw,len(raw),1220000)==0
        assert lib.engine_plan(e,1220000,4096)==0
        out=(C.c_float*2)();assert lib.engine_track_grant(e,raw,len(raw),1220000,out)==0
        # The predecessor actual C calculation exists, but no physical dispatch
        # is claimed. The current unrun registered candidate is a new generation.
        lib.engine_behavior_enable_for_budget(e);lib.engine_fixture_auto_owner(e)
        code,facts=_bootstrap_facts(lib,e,v,now=1230000);assert code==0
        none=schema.EpisodeStatus(major=1,state=1,registry_epoch=b'r'*16,registry_revision=1,
            accepted_core_sha256=episode_core_sha256(initial_none_core()),episode_id=bytes(16),episode_revision=0,
            commit_state=2,state_message_id=b's'*16,state_sha256=b't'*32,state_revision=1,plan_revision=1,
            phase_revision=1,calculation_hwm=1,vehicle_hwm=2,valid_until_pi_us=2230000)
        initial=initial_none_snapshot(none);assert initial.core.state==1
        wire=encode(none);assert lib.engine_fixture_status(e,wire,len(wire),1230000)==0
        if defer_search:
            assert lib.engine_fixture_begin(e,1230000)==0
            return e,world,grant,None,initial,v,facts
        assert lib.engine_plan(e,1230000,4096)==1  # real search waits for registration
        output=C.create_string_buffer(32768);n=lib.engine_fixture_registration(e,1230000,output,len(output));assert n>0
        request_body=output.raw[:n]
        request=decode(request_body,schema.EpisodeRequest)
        request_sha=episode_request_sha256(request_body)  # Original C-sent body, not a typed re-encoding.
        assert request.action==1 and request.candidate.state==2 and request.candidate.phase==2
        assert _bootstrap_phase_id(lib,e)==''  # A request does not reserve a current fullState ID.
        snapshot=schema.EpisodeSnapshot(core=request.candidate,progress=request.progress,
            acceptance=schema.EpisodeAcceptanceRef(registry_epoch=b'r'*16,registry_revision=2,
                request_nonce=request.request_nonce,request_sha256=request_sha,
                accepted_core_sha256=episode_core_sha256(request.candidate),request_sequence=request.request_sequence,
                origin_vm=UUID(request.vm).bytes,origin_execution=UUID(request.execution).bytes,
                origin_app_instance=UUID(request.app_instance).bytes,origin_generation=request.control_generation,
                origin_grant_revision=request.expected_grant_revision))
        receipt=schema.EpisodeReceipt(major=1,request_nonce=request.request_nonce,request_sha256=request_sha,
            result=1,reason=0,registry_epoch=b'r'*16,registry_revision=2,snapshot=snapshot,request_sequence=request.request_sequence)
        rejected=schema.EpisodeReceipt();rejected.CopyFrom(receipt)
        rejected.request_sha256=b'?'*32;rejected.snapshot.acceptance.request_sha256=rejected.request_sha256
        wire=encode(rejected);assert lib.engine_fixture_receipt(e,wire,len(wire))==9
        assert _bootstrap_phase_id(lib,e)==''
        wire=encode(receipt);assert lib.engine_fixture_receipt(e,wire,len(wire))==0
        assert _bootstrap_phase_id(lib,e)==str(UUID(bytes=request.phase_state_message_id))
        active=_bootstrap_status(snapshot,commit=1,now=1230000);wire=encode(active)
        assert lib.engine_fixture_status(e,wire,len(wire),1230000)==0
        return e,world,grant,request,snapshot,v,facts
    except BaseException:
        lib.engine_free(e);raise


def _fresh_bootstrap_sample(world,*,now=1550000,mode='normal'):
    from copy import deepcopy
    value=deepcopy(world);value.sequence=value.observation_seq=2;value.reference_odom_seq=3
    # One new stationary capture/encoder sample. The declared mapper->Pi clock
    # is [100000,101000]us in lattice_bridge.c; even the original publication's
    # upper endpoint precedes arrival. No received World is restamped.
    v=_bootstrap_vehicle(3,now-4000)
    value.observed_us=v.pi_sample_us-100500
    value.completed_us=value.observed_us+1000;value.published_us=value.observed_us+2000
    value.localization.observed_mapper_us=value.observed_us
    if mode=='prefix':v.odom_pose.x=.6;v.left_ticks=v.right_ticks=700
    value.reference_vehicle_pose.x+=v.odom_pose.x
    return value,v


@pytest.mark.parametrize('future_publish',[False,True],ids=['coherent-capture','future-original-publication'])
def test_bootstrap_world_uses_original_mapper_capture_and_vehicle_anchor(library,future_publish):
    e=library.engine_new();now=1550000
    library.engine_fixture_world_result.argtypes=[C.c_void_p]
    try:
        value,v=_fresh_bootstrap_sample(modern_world(),now=now)
        assert value.reference_odom_seq==v.sequence
        assert value.observed_us+100500==v.pi_sample_us
        assert value.localization.observed_mapper_us==value.observed_us
        assert value.published_us+101000<now
        if future_publish:value.published_us=now-100000+2000
        send_vehicle(library,e,v)
        wire=encode(value);rc=library.engine_world(e,wire,len(wire),now)
        assert rc==(4 if future_publish else 0)
        assert library.engine_fixture_world_result(e)==(8 if future_publish else 0)  # DW_TIME, not geometry/source.
        assert encode(value)==wire
    finally:library.engine_free(e)


def _fresh_bootstrap_input(lib,e,world,*,now=1550000,mode='normal'):
    value,v=_fresh_bootstrap_sample(world,now=now,mode=mode)
    send_vehicle(lib,e,v)
    if mode=='geometry':obstacle(value,1,7,8,16)
    if mode=='source':value.frame_epoch='99999999-9999-4999-8999-999999999999'
    wire=encode(value);code=lib.engine_world(e,wire,len(wire),now)
    if mode=='source':assert code!=0
    else:assert code==0
    rc,facts=_bootstrap_facts(lib,e,v,now=now,gap=1 if mode=='gap' else 0);assert rc==0
    return v,facts


@pytest.mark.parametrize('mode',['normal','prefix','gap'])
def test_unpublished_phase_search_establishes_current_first_correspondence_before_request(library,mode):
    from xenova_drive_world import episode_request_sha256
    e,world,_,_,_,_,_=_registered_bootstrap(library,defer_search=True)
    try:
        # The actual bounded search spans more than the original Vehicle age.
        # Current input is newly produced; no receipt/grant/READY is supplied.
        _fresh_bootstrap_input(library,e,world,mode=mode)
        rc=library.engine_fixture_continue(e,1550000,4096)
        output=C.create_string_buffer(32768)
        if mode=='normal':
            assert rc==1  # actual path exists, original registration still required
            n=library.engine_fixture_registration(e,1550000,output,len(output));assert n>0
            raw=output.raw[:n];request=decode(raw,schema.EpisodeRequest)
            assert request.action==1 and request.candidate.phase==2
            assert request.candidate.origin_vehicle_sequence==2 and request.vehicle_sequence==3
            assert request.candidate.left_baseline_ticks==request.candidate.right_baseline_ticks==100
            assert request.plan_sha256!=bytes(32) and episode_request_sha256(raw)!=bytes(32)
            assert _bootstrap_phase_id(library,e)==''  # Preparation is not acceptance.
        else:
            assert rc==6  # Actual candidate/path rejected, not merely still waiting.
            assert library.engine_fixture_registration(e,1550000,output,len(output))<0
            assert _bootstrap_phase_id(library,e)==''
    finally:library.engine_free(e)


def test_registered_unrun_path_recovers_after_first_facts_expiry_without_budget_refund(library):
    from xenova_drive_world import episode_core_sha256
    e,world,grant,request,snapshot,v,facts=_registered_bootstrap(library)
    try:
        predecessor,_=_bootstrap_view(library,e)
        old=encode(facts);assert library.engine_fixture_facts(e,old,len(old),1460000)!=0
        assert library.engine_fixture_continue(e,1460000,1)==1  # retain private candidate, no fake firstState
        fresh,_=_fresh_bootstrap_input(library,e,world)
        assert library.engine_fixture_continue(e,1550000,8)==0
        view,hashes=_bootstrap_view(library,e)
        assert view[1]==fresh.pi_sample_us and view[0]!=predecessor[0]
        # The initialized global tracker still belongs to the actual preceding
        # path. Publishing an unrun successor must not reset that progress.
        assert view[3:6]==predecessor[3:6] and view[3]!=view[0]
        assert hashes==request.plan_sha256+episode_core_sha256(snapshot.core)
        assert view[8:12]==[snapshot.core.started_pi_us,snapshot.core.deadline_pi_us,
                            snapshot.core.phase_deadline_pi_us,snapshot.core.meter_baseline_total_ticks]
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)==0 and out[0]>0
        tracked,_=_bootstrap_view(library,e)
        assert tracked[3]==tracked[0] and tracked[4] and tracked[5]==fresh.pi_sample_us
    finally:library.engine_free(e)


def test_actual_tracking_progress_is_bound_to_completed_vehicle_and_original_meter(library):
    from xenova_drive_world import episode_core_sha256
    library.engine_fixture_track_progress.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,
        C.POINTER(C.c_uint64),C.c_void_p]
    e,world,grant,_,snapshot,_,_=_registered_bootstrap(library)
    def observed():
        raw=C.create_string_buffer(256);view=(C.c_uint64*4)();core=C.create_string_buffer(32)
        n=library.engine_fixture_track_progress(e,raw,len(raw),view,core)
        return n,list(view),core.raw,decode(raw.raw[:n],schema.EpisodeProgress) if n>0 else None
    try:
        assert library.engine_fixture_continue(e,1230000,8)==0
        fresh,facts=_fresh_bootstrap_input(library,e,world)
        assert library.engine_plan(e,1550000,4096)==0
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)==0
        n,view,core,progress=observed()
        assert n>0 and view[0] and view[2]==fresh.sequence==progress.vehicle_sequence
        assert core==episode_core_sha256(snapshot.core)
        assert progress.used_travel_ticks==facts.meter.total_ticks-snapshot.core.meter_baseline_total_ticks
        assert progress.revision>snapshot.progress.revision
        assert progress.last_progress_pi_us==snapshot.progress.last_progress_pi_us==0  # stationary is no progress
        registered=encode(snapshot)
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)==0
        assert observed()[3].last_progress_pi_us==0  # same sample/retry is no movement
        # A newer actual Vehicle without that Vehicle's original meter sample
        # may still be conservatively tracked, but cannot mint an export proof.
        newer=_bootstrap_vehicle(fresh.sequence+1,1556000);send_vehicle(library,e,newer)
        assert library.engine_track_grant(e,wire,len(wire),1560000,out)==0
        assert observed()[0]==0 and not observed()[1][0]
        # The actual input owner can receive a new facts sample between the
        # planner turn and the calculation. Consume it read-only; do not make
        # the tracker a second writer of the planner's facts/episode cache.
        library.engine_track_grant_facts.argtypes=[C.c_void_p,C.c_char_p,C.c_size_t,
            C.c_char_p,C.c_size_t,C.c_uint64,C.POINTER(C.c_float)]
        library.engine_fixture_planner_facts_sequence.argtypes=[C.c_void_p]
        library.engine_fixture_planner_facts_sequence.restype=C.c_uint64
        latest=schema.DriveControlFacts();latest.CopyFrom(facts)
        latest.meter.vehicle_sequence=newer.sequence
        latest.valid_until_pi_us=newer.pi_sample_us+200000
        source=encode(latest)
        assert library.engine_fixture_planner_facts_sequence(e)==fresh.sequence
        assert library.engine_track_grant_facts(e,wire,len(wire),source,len(source),1560000,out)==0
        assert observed()[3].vehicle_sequence==newer.sequence and observed()[3].last_progress_pi_us==0
        assert library.engine_fixture_planner_facts_sequence(e)==fresh.sequence
        expired=schema.DriveControlFacts();expired.CopyFrom(latest);expired.valid_until_pi_us=1560000
        stale=encode(expired)
        assert library.engine_track_grant_facts(e,wire,len(wire),stale,len(stale),1560000,out)!=0
        assert observed()[0]==0 and library.engine_fixture_planner_facts_sequence(e)==fresh.sequence
        assert encode(snapshot)==registered  # Original Core/ref/birth/meter/budget remains unchanged.
    finally:library.engine_free(e)


def test_registered_unrun_geometry_reuse_adopts_new_local_slot_and_same_original_plan(library):
    e,world,grant,request,snapshot,_,_=_registered_bootstrap(library)
    try:
        assert library.engine_fixture_continue(e,1230000,8)==0
        before,original=_bootstrap_view(library,e)
        fresh,_=_fresh_bootstrap_input(library,e,world)
        assert library.engine_plan(e,1550000,4096)==0
        after,adopted=_bootstrap_view(library,e)
        assert after[0]!=before[0] and after[1]==fresh.pi_sample_us and before[1]<1350000
        assert adopted==original and after[6:14]==before[6:14]
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)==0 and out[0]>0
    finally:library.engine_free(e)


def test_initialized_progress_is_never_reset_as_unrun_first_correspondence(library):
    e,world,grant,_,_,_,_=_registered_bootstrap(library)
    try:
        assert library.engine_fixture_continue(e,1230000,8)==0
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1230000,out)==0
        before,original=_bootstrap_view(library,e);assert before[4]
        _fresh_bootstrap_input(library,e,world)
        assert library.engine_plan(e,1550000,4096)==1  # Fresh search waits for its original registration handshake.
        after,current=_bootstrap_view(library,e)
        assert after[0]==before[0] and after[1]==before[1] and after[5]==before[5] and current==original
        accepted_id=_bootstrap_phase_id(library,e)
        output=C.create_string_buffer(32768);n=library.engine_fixture_registration(e,1550000,output,len(output));assert n>0
        pending=decode(output.raw[:n],schema.EpisodeRequest)
        assert str(UUID(bytes=pending.phase_state_message_id))!=accepted_id
        assert _bootstrap_phase_id(library,e)==accepted_id  # No next-request ID on the old current path.
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)!=0
    finally:library.engine_free(e)


@pytest.mark.parametrize('mode',['source','geometry','prefix','gap'])
def test_first_correspondence_adoption_rejects_lost_source_coverage_prefix_or_meter(library,mode):
    e,world,grant,_,_,_,_=_registered_bootstrap(library)
    try:
        assert library.engine_fixture_continue(e,1230000,8)==0
        before,original=_bootstrap_view(library,e)
        _fresh_bootstrap_input(library,e,world,mode=mode)
        assert library.engine_plan(e,1550000,4096)!=0
        after,current=_bootstrap_view(library,e)
        assert after[0]==before[0] and after[1]==before[1] and current==original
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1550000,out)!=0
    finally:library.engine_free(e)



def test_committed_active_profile_keeps_normal_current_tracking_and_reuse(library):
    e,world,grant,_,snapshot,_,_=_registered_bootstrap(library)
    try:
        assert library.engine_fixture_continue(e,1230000,8)==0
        wire=encode(grant);out=(C.c_float*2)()
        assert library.engine_track_grant(e,wire,len(wire),1230000,out)==0
        # Unit registry leaf changes its current status, not a future grant or
        # forged product completion. Actual Gateway fullState acceptance is a
        # separate original app/actor integration test.
        status=_bootstrap_status(snapshot,commit=2,now=1230000);body=encode(status)
        assert library.engine_fixture_status(e,body,len(body),1230000)==0
        before,original=_bootstrap_view(library,e)
        _fresh_bootstrap_input(library,e,world,now=1320000)
        assert library.engine_plan(e,1320000,4096)==0
        after,current=_bootstrap_view(library,e)
        assert after[0]==before[0] and current==original
        assert library.engine_track_grant(e,wire,len(wire),1320000,out)==0
    finally:library.engine_free(e)
