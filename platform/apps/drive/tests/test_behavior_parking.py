"""Actual C episode/goal/task math. Pi meter and placed World are declared inputs.

These cases do not manufacture a registry ACK, physical STOP or runtime READY.
The app/actor bridge is a separate integration boundary in the same Nano cohort.
"""
import ctypes as C
import json
import math
import pytest
from test_lattice import library, Product, CFG, FOUND, STOP, obstacle
from test_engine import modern_world
from test_signed_lattice import ports as tracking_ports, sample
from world_codec import schema


def placed_world(count=1,*,shared_entry=True):
    world=modern_world(); world.required_features=15
    world.parking_set.CopyFrom(schema.ParkingSetMeta(revision=1,complete=True,
        track_count_in_roi=count,capacity_limited=False,evidence_sequence=1,state=1))
    lane=world.lane_model.lanes[0]
    # Match the actual wire float32 reconstruction 2*center-divider. Decimal .8
    # rounds below this edge and would declare a real slit in the strict union.
    top=2*lane.centerline[0].y-world.lane_model.divider[0].y if shared_entry else .8
    for index in range(count):
        x=1.+index*.6
        slot=world.parking_slots.add(slot_id=index+1,geometry_revision=1,geometry_state=1,
            occupancy_state=2,entry_edge=2,position_bound_m=.001,heading_bound_rad=.001,
            geometry_age_ms=0,clearance_age_ms=0,evidence_sequence=1,geometry_complete=True,
            occupancy_reason=0,source_kinds=1,occupancy_revision=1,usable_width_m=1.,
            usable_depth_m=top,approach_lane_id=1,goal_mask=1)
        for px,py in ((x,0.),(x+1.,0.),(x+1.,top),(x,top)): slot.corners.add(x=px,y=py)
        slot.reverse_goal.CopyFrom(schema.Pose(x=x+.5,y=.4,yaw=math.pi/2))
    return world


def ports(lib):
    lib.probe_parking_goal_set.argtypes=[C.c_void_p,C.c_uint32,C.c_uint,C.POINTER(C.c_float)]
    lib.probe_parking_search.argtypes=[C.c_void_p,C.c_uint32,C.c_float,C.c_float,C.c_float]
    lib.probe_parking_search_strategy.argtypes=[C.c_void_p,C.c_uint32,C.c_float,C.c_float,C.c_float,C.c_uint]
    lib.probe_parking_trace.argtypes=[C.c_void_p,C.POINTER(C.c_double)]
    lib.probe_parking_trace.restype=None
    lib.probe_parking_terminal.argtypes=[C.c_void_p,C.c_uint32,C.c_float,C.c_float,C.c_float,C.c_float]
    lib.probe_behavior_new.argtypes=[C.c_void_p]; lib.probe_behavior_new.restype=C.c_void_p
    lib.probe_behavior_step.argtypes=[C.c_void_p,C.c_void_p,C.c_uint64,C.c_uint64,C.c_uint64,C.c_float,C.c_float,C.c_int]
    lib.probe_behavior_deadline.argtypes=[C.c_void_p,C.c_int]; lib.probe_behavior_deadline.restype=C.c_uint64
    lib.probe_behavior_reenter.argtypes=[C.c_void_p,C.c_uint64]
    lib.probe_behavior_free.argtypes=[C.c_void_p]
    lib.probe_round.argtypes=[C.c_void_p,C.c_void_p,C.c_int,C.c_uint32]; lib.probe_round.restype=C.c_uint32


@pytest.mark.parametrize('kind',[0,1,2])
def test_unknown_occupied_and_clear_region_have_distinct_actual_eligibility(library,kind):
    ports(library); world=placed_world(); world.parking_slots[0].occupancy_state=kind
    product=Product(library,world)
    try:
        out=(C.c_float*81)(); count=library.probe_parking_goal_set(product.handle,1,1,out)
        assert (count>0)==(kind==2)
        assert count<=27
    finally: product.close()


@pytest.mark.parametrize('strategy,entry_gear', ((1,-1), (2,1)))
def test_parking_cusp_outside_slot_requires_gate_before_entering(library, strategy, entry_gear):
    tracking_ports(library)
    library.probe_parking_cusp_fixture.argtypes = [C.c_void_p, C.c_uint]
    library.probe_prepare_tracking.argtypes = [C.c_void_p]
    library.probe_prepare_tracking.restype = C.c_bool
    world = placed_world()
    slot = world.parking_slots[0]
    slot.goal_mask = 3
    slot.forward_goal.CopyFrom(schema.Pose(x=1.5,y=.4,yaw=-math.pi/2))
    traces = []
    for prepared in (False, True):
        product = Product(library,world)
        try:
            cusp = library.probe_parking_cusp_fixture(product.handle, strategy)
            assert cusp > 0 and library.probe_valid(product.handle) == FOUND
            if prepared:
                assert library.probe_prepare_tracking(product.handle)
            rc, stopped = sample(library, product, cusp)
            assert rc == STOP and stopped[:5] == [0., 0., 1., 0., entry_gear]
            assert library.probe_gate(product.handle, 2) == 0  # No acknowledged serial STOP
            assert library.probe_gate(product.handle, 0) == 1
            rc, entering = sample(library, product, cusp+1, 1020000)
            assert rc == FOUND and entering[0]*entry_gear > 0 and entering[2:4] == [0., 1.]
            traces.append(stopped + entering)
        finally:
            product.close()
    assert traces[1] == pytest.approx(traces[0], abs=1e-6)


def parking_trace(library,product,tmp_path,validation=None):
    out=(C.c_double*24)();library.probe_parking_trace(product.handle,out)
    names=('root_union','start_result','search_result','goal_count','selected_goal','path_count',
        'expanded','primitives','footprints','cells','road_checks','aisle_outer_y','slot_entry_y',
        'space_uncertainty','root_x','root_y','root_yaw','first_goal_x','first_goal_y','first_goal_yaw',
        'last_path_x','last_path_y','last_path_yaw','actual_goal_contains')
    row=dict(zip(names,out));row['validation_result']=validation
    (tmp_path/'actual-parking-search-trace.json').write_text(json.dumps(row,indent=2)+'\n')
    return row


def test_actual_signed_goal_set_enters_only_the_selected_known_slot(library,tmp_path):
    ports(library); product=Product(library,placed_world())
    try:
        # Aligned maneuver root in the selected slot/aisle union. At lane-center
        # y1.2 the north-facing padded nose crosses the forbidden divider1.6.
        result=library.probe_parking_search(product.handle,1,1.5,1.0,math.pi/2)
        valid=library.probe_valid(product.handle) if result==FOUND else None
        trace=parking_trace(library,product,tmp_path,valid)
        assert result==FOUND,trace
        assert valid==FOUND,trace
        assert trace['root_union']==trace['actual_goal_contains']==1
        assert trace['aisle_outer_y']==trace['slot_entry_y']
        assert all(point[4]<=0 for point in product.path())
        assert product.path()[-1][1]<.8
    finally: product.close()


def test_forward_parking_tracks_at_low_speed_and_stops_for_new_occupancy(library):
    ports(library)
    world=placed_world()
    slot=world.parking_slots[0]
    slot.goal_mask=3
    slot.forward_goal.CopyFrom(schema.Pose(x=1.5,y=.4,yaw=-math.pi/2))
    product=Product(library,world)
    try:
        goals=(C.c_float*81)()
        assert library.probe_parking_goal_set(product.handle,1,2,goals)>0
        # A straight approach two test primitives from the first parking seed.
        approach=2*CFG['primitive_speed']*CFG['primitive_duration']
        pose=(goals[0],goals[1]+approach,goals[2])
        assert library.probe_parking_search_strategy(product.handle,1,*pose,2)==FOUND
        rc,command=product.track(pose,.16,0.)
        assert rc==FOUND and command[0]==pytest.approx(.16)
        ix=int(pose[0]/world.resolution_m)
        iy=int(pose[1]/world.resolution_m)
        obstacle(world,ix,ix+1,iy,iy+1)
        product.update(world)
        rc,command=product.track(pose,.16,0.)
        assert rc==10 and command[0]==0.
    finally:
        product.close()


def test_signed_parking_rejects_padded_root_across_the_forbidden_divider(library,tmp_path):
    ports(library); world=placed_world()
    world.lane_model.crossing=bytes(len(world.lane_model.crossing))
    product=Product(library,world)
    try:
        # Preserve cohort01's original root as a negative. The root footprint
        # fails before graph expansion or occupancy-cell work, not a timeout.
        result=library.probe_parking_search(product.handle,1,1.5,1.2,math.pi/2)
        trace=parking_trace(library,product,tmp_path)
        assert result==10 and trace['root_union']==0,trace
        measured=product.metrics()
        assert measured[0]==0 and measured[2]==1 and measured[3]==0
    finally: product.close()


def test_signed_parking_can_use_the_permitted_adjacent_lane(library,tmp_path):
    ports(library); product=Product(library,placed_world())
    try:
        result=library.probe_parking_search(product.handle,1,1.5,1.2,math.pi/2)
        valid=library.probe_valid(product.handle) if result==FOUND else None
        trace=parking_trace(library,product,tmp_path,valid)
        assert result==valid==FOUND,trace
        assert trace['root_union']==trace['actual_goal_contains']==1
    finally: product.close()


def test_signed_parking_still_checks_the_cells_across_the_divider(library,tmp_path):
    ports(library); world=placed_world()
    obstacle(world,14,16,16,17)
    product=Product(library,world)
    try:
        result=library.probe_parking_search(product.handle,1,1.5,1.2,math.pi/2)
        trace=parking_trace(library,product,tmp_path)
        assert trace['root_union']==1 and result==10,trace
        assert trace['cells']>0 and trace['expanded']==0,trace
    finally: product.close()


def test_signed_parking_rejects_the_original_wire_gap_between_slot_and_aisle(library,tmp_path):
    ports(library);product=Product(library,placed_world(shared_entry=False))
    try:
        result=library.probe_parking_search(product.handle,1,1.5,1.0,math.pi/2)
        trace=parking_trace(library,product,tmp_path)
        assert trace['slot_entry_y']<trace['aisle_outer_y']
        assert result==10 and trace['root_union']==0,trace
        assert trace['expanded']==0 and trace['footprints']==1 and trace['cells']==0
    finally: product.close()


@pytest.mark.parametrize('x,y,yaw,error,accepted',[
    (1.65,.4,math.pi/2,.003,True),  # center residual exceeds the search hint
    (1.5,.4,math.pi/2+.12,.003,True),
    (1.5,.4,-math.pi/2,.003,False),
    (1.95,.4,math.pi/2,.003,False),
    (1.5,.4,math.pi/2,.5,False),
])
def test_terminal_region_does_not_spend_correction_on_seed_residual(library,x,y,yaw,error,accepted):
    ports(library); product=Product(library,placed_world())
    try: assert bool(library.probe_parking_terminal(product.handle,1,x,y,yaw,error))==accepted
    finally: product.close()


@pytest.mark.parametrize('case',['phase_replan','absolute_travel','no_progress','normal_stop_wait'])
def test_original_episode_budgets_do_not_follow_path_or_wait_counters(library,case):
    ports(library); product=Product(library,modern_world()); behavior=library.probe_behavior_new(product.handle)
    assert behavior
    try:
        step=lambda t,ticks,seq,wait=0: library.probe_behavior_step(product.handle,behavior,t,ticks,seq,.6,1.2,wait)
        assert step(1000000,1000,1)==0
        if case=='phase_replan':
            assert library.probe_behavior_reenter(behavior,10900000)
            assert step(11000000,1000,2)==3
        elif case=='absolute_travel': assert step(1100000,1600,2)==4
        elif case=='no_progress': assert step(3000000,1000,2)==5
        else:
            assert step(1900000,1000,2,1)==0
            assert step(4000000,1000,3,1)==0
            assert step(4100000,1000,4)==0
            assert step(5200000,1000,5)==5
        assert library.probe_behavior_deadline(behavior,1)==41000000
        assert library.probe_behavior_deadline(behavior,0)==11000000
    finally: library.probe_behavior_free(behavior); product.close()


def test_episode_corners_keep_the_local_station_interval(library):
    library.probe_episode_station.argtypes = [C.c_int, C.POINTER(C.c_float)]
    bounds = (C.c_float * 2)()
    assert library.probe_episode_station(0, bounds) == 1
    assert bounds[0] <= 0 and bounds[1] >= .39
    assert library.probe_episode_station(1, bounds) == 0


def test_occupied_cell_far_past_lane_end_is_not_a_blocker(library):
    library.probe_observed_blocker.argtypes = [C.c_void_p]
    for column, expected in ((63, 0), (59, 1)):
        world = modern_world()
        obstacle(world, column, column + 1, 12, 13)
        product = Product(library, world)
        try:
            assert library.probe_observed_blocker(product.handle) == expected
        finally:
            product.close()


def test_stable_id_pending_survives_same_snapshot_metric_reordering(library):
    ports(library); world=placed_world(6); product=Product(library,world)
    behavior=library.probe_behavior_new(product.handle); assert behavior
    try:
        assert library.probe_round(product.handle,behavior,0,0)==1
        for identifier in (1,2,3): library.probe_round(product.handle,behavior,1,identifier)
        assert library.probe_round(product.handle,behavior,2,0)==4
        world.sequence=world.observation_seq=3
        reversed_slots=[schema.ParkingSlot() for _ in world.parking_slots]
        for copy,slot in zip(reversed_slots,reversed(world.parking_slots)): copy.CopyFrom(slot)
        del world.parking_slots[:]
        for slot in reversed_slots: world.parking_slots.add().CopyFrom(slot)
        product.update(world)
        assert library.probe_round(product.handle,behavior,0,0)==4
        for identifier in (4,5,6): library.probe_round(product.handle,behavior,1,identifier)
        assert library.probe_round(product.handle,behavior,2,0)==0
        assert library.probe_round(product.handle,behavior,0,0)==0
    finally: library.probe_behavior_free(behavior); product.close()
