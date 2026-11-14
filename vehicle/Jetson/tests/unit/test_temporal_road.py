"""원 관측을 지도에 놓고 두 차로의 짧은 공백만 연결하는 반례다."""

from dataclasses import replace
from copy import deepcopy
import math
from types import SimpleNamespace
import pytest

from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.wheel_motion import WheelCapture, WheelMotionProfile, WheelObservation, WheelProgress
from xenova_vehicle.perception.inputs import FrameStamp
from xenova_vehicle.perception.parking_approach import refit_approach
from xenova_vehicle.perception.parking_entrance import EntranceSpan
from xenova_vehicle.perception.road import RoadGeometryLimits, RoadProfile
from xenova_vehicle.perception.road_refit import _MarkingChord, _boundary_bounds, _heading_estimate, _position_estimate, fit_road_window
from xenova_vehicle.perception.tape import TapeGraph, TapeProfile, TapeStrip
from xenova_vehicle.perception.temporal_road import (RoadLink, RoadSection, TemporalRoadMap, _bounded_candidates,
    _divider_supported, _mapped_candidate, _project_strips, _reobserve_run, _section_length, _shared_forward_end,
    stored_road_window)


def observe(owner, sequence, x, dashed, *, lane_width=.285, extra_outer=False, error=.012):
    def strip(i, a, b, y, kind):
        return TapeStrip(i, ((a-x,y),(b-x,y)), (.02,.04), error, kind,kind)

    strips = [strip(0,.20,1.25,lane_width,'FOV_clip'),strip(1,.20,1.25,-lane_width,'FOV_clip')]
    strips.extend(strip(i,a,b,0.,'observed_paint_end') for i,(a,b) in enumerate(dashed,2))
    if extra_outer:
        strips.append(strip(8,.20,1.25,2*lane_width,'FOV_clip'))
    graph = TapeGraph(tuple(strips),source_key=('camera','rgb','clock',sequence),calibration_qualified=True)
    at = sequence*250_000
    sample = PoseSample('map','clock','calibration',at,x,0.,0.,.003,.001,origin='ODOMETRY')
    pose = PoseEstimate(sample,(at,at),(at,at))
    profile = replace(RoadProfile(),missing_outer_width_bounds_m=(lane_width-.02,lane_width+.02))
    return owner.observe(graph,pose,profile,TapeProfile())


def test_distinct_captures_join_same_divider_across_short_gap_in_map_coordinates():
    owner = TemporalRoadMap()
    dashes = ((.20,.40),(.60,.85))
    assert observe(owner,1,0.,dashes,lane_width=.32) is None
    assert owner.links
    revision = owner.revision
    observe(owner,1,0.,dashes,lane_width=.32)
    assert owner.revision == revision
    observe(owner,2,.05,dashes,lane_width=.32)
    gap, = (link for link in owner.links if link.interpolated
            and math.isclose(link.start[1][0],.4) and math.isclose(link.end[1][0],.6))
    assert math.dist(gap.start[1],gap.end[1]) <= RoadProfile().max_gap_m
    assert gap.start[0][1] == .32 and gap.end[2][1] == -.32
    assert gap.start[1][1] == gap.end[1][1] == 0.
    assert len(gap.sources) == 2


def test_long_gap_and_ambiguous_lane_roles_do_not_join():
    owner = TemporalRoadMap()
    dashes = ((.20,.40),(.95,1.25))
    observe(owner,1,0.,dashes)
    observe(owner,2,.05,dashes)
    assert owner.links
    assert all(link.end[1][0]-link.start[1][0] <= .051 for link in owner.links)
    owner.clear()
    observe(owner,3,0.,dashes,extra_outer=True)
    observe(owner,4,.05,dashes,extra_outer=True)
    assert owner.links == ()


def test_mapped_candidate_preserves_actual_sources_and_bounds():
    owner = TemporalRoadMap()
    divider = ((.20,.95),)
    assert observe(owner,1,0.,divider,error=.003) is None
    candidate = observe(owner,2,.05,divider,error=.003)
    assert candidate.sources == {('camera','rgb','clock',1),('camera','rgb','clock',2)}
    assert candidate.road.source_key in candidate.sources
    h = candidate.road.hypotheses[0]
    assert len(h.divider) == len(h.left_center) == len(h.right_center)
    assert h.error_m > .006 and h.yaw_error_rad > .001
    assert not any(h.inferred)


def test_dash_fit_only_supports_crossings_within_its_own_interval():
    profile = RoadProfile()
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    def station(x, offset=0.):
        width = profile.missing_outer_width_m
        return tuple((x, y + offset) for y in (width, 0., -width))

    link = RoadLink(station(0.), station(.5), .01, frozenset((key,)), evidence, False, .02)
    fitted = replace(link, boundary_normal_bounds_m=(.01,)*3, outer_chord_bounds_m=(.02,)*2)
    shape = _mapped_candidate((fitted,), key, profile).road.hypotheses[0]
    inferred = replace(link, start=station(.1), end=station(.4), interpolated=True, inferred_roles=(True,)*3)
    assert _divider_supported(inferred, shape, profile)
    assert not _divider_supported(replace(inferred, end=station(.6)), shape, profile)
    assert not _divider_supported(replace(inferred, start=station(.1,.285), end=station(.4,.285)), shape, profile)
    assert not _divider_supported(inferred, replace(shape, boundary_observations=None), profile)


def test_stored_window_rejects_an_axle_outside_a_clamped_section_end():
    profile = RoadProfile()
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    width = profile.missing_outer_width_m
    def station(x):
        return ((x, width), (x, 0.), (x, -width))

    link = RoadLink(station(.2), station(.8), .01, frozenset((key,)), evidence, False, .02)
    original = _mapped_candidate((link,), key, profile)
    model = SimpleNamespace(road=original.road, crossing=b'\1')
    section = RoadSection((link,))
    pose = PoseSample('map','clock','calibration',1,.15,-width/2,0.,.003,.001)
    request = dict(rear_m=.16, forward_m=.45)
    assert stored_road_window((section,), model, pose, profile, **request) is None
    inside = replace(pose, x=.4)
    road, _ = stored_road_window((section,), model, inside, profile, **request)
    divider = road.hypotheses[0].divider
    assert divider[0][0] <= inside.x <= divider[-1][0]
    demo, crossing = stored_road_window((section,), model, pose, profile, demo_course=True, **request)
    assert demo.hypotheses[0].divider[0][0] < pose.x
    assert crossing[0] == 0


def test_demo_window_uses_local_agreement_for_overlapping_reconstructions():
    profile = RoadProfile()
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    width = profile.missing_outer_width_m
    def station(x, y=0.):
        return ((x,y+width),(x,y),(x,y-width))
    def link(a, b, y=0.):
        return RoadLink(station(a,y),station(b,y),.01,frozenset((key,)),evidence,False,.02)

    original = _mapped_candidate((link(.4,.8),), key, profile)
    model = SimpleNamespace(road=original.road, crossing=b'\1')
    short = RoadSection((link(.3,.7),))
    long = RoadSection((link(-.2,.3),link(.3,.7),link(.7,1.2)))
    pose = PoseSample('map','clock','calibration',1,.6,-width/2,0.,.003,.001)
    request = dict(rear_m=.25, forward_m=.5)
    assert stored_road_window((short,long), model, pose, profile, **request) is None
    road, _ = stored_road_window((short,long), model, pose, profile, demo_course=True, **request)
    assert road.hypotheses[0].divider[-1] == (1.2,0.)

    adjacent = RoadSection((link(-.2,1.2,-width),))
    detached = SimpleNamespace(road=_mapped_candidate((link(2.,2.4),),key,profile).road,crossing=b'\1')
    assert stored_road_window((long,adjacent), detached, pose, profile, demo_course=True, **request) is None


def test_demo_window_recognizes_an_overlapping_fragment_ending_inside_the_bend():
    profile = RoadProfile()
    width = profile.missing_outer_width_m
    key = ('camera', 'rgb', 'clock', 1)
    evidence = (frozenset(),)*3

    def section(stop, lateral=0.):
        points = [tuple((r*math.sin(i*.05), .6-r*math.cos(i*.05)+lateral)
                        for r in (.6-width, .6, .6+width)) for i in range(stop)]
        return RoadSection(tuple(RoadLink(a,b,.01,frozenset((key,)),evidence,True,.02)
                                 for a,b in zip(points,points[1:])))

    short, long = section(14), section(28)
    at = _section_length(short)-.01
    first = (0., .14, short, at)
    assert _shared_forward_end(first, (0., .14, long, at+.05), profile)
    assert not _shared_forward_end(first, (0., .14, section(28,width), at+.05), profile)
    assert not _shared_forward_end(first, (0., .14, section(15), at+.02), profile)


def test_demo_window_keeps_stored_lane_when_a_short_observation_starts_ahead():
    profile = RoadProfile()
    width = profile.missing_outer_width_m
    key = ('camera', 'rgb', 'clock', 1)

    def section(angles, offset=0.):
        stations = [tuple((r*math.sin(a), .6-r*math.cos(a)+offset)
                          for r in (.6-width,.6,.6+width)) for a in angles]
        return RoadSection(tuple(RoadLink(a,b,.01,frozenset((key,)),(frozenset(),)*3,True,.02)
                                 for a,b in zip(stations,stations[1:])))

    long = section(tuple(i*.05 for i in range(38)))
    short_angles = tuple(.75+i*.05 for i in range(8))
    pose = PoseSample('map','clock','calibration',1,.26*math.sin(.56),.6-.26*math.cos(.56),.56,.003,.001)
    previous = section(tuple(i*.05 for i in range(10)), 2.)
    model = SimpleNamespace(road=_mapped_candidate(previous.links,key,profile).road,crossing=bytes(9))
    request = dict(rear_m=.25,forward_m=.6,demo_course=True)
    stored = stored_road_window((long,), model, pose, profile, **request)
    assert stored is not None
    assert stored_road_window((long,section(short_angles)), model, pose, profile, **request) == stored
    assert stored_road_window((long,section(short_angles,.026)), model, pose, profile, **request) is None


def test_demo_window_preserves_the_turn_in_the_wire_model():
    from xenova_vehicle.perception.temporal_road import _fit_publication_curve
    from xenova_vehicle.transport.world_adapter import lane_model_from_road

    profile = RoadProfile()
    width = profile.missing_outer_width_m
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    def station(angle):
        return tuple((r*math.sin(angle), .6-r*math.cos(angle)) for r in (.6-width,.6,.6+width))
    stations = tuple(station(i*.08) for i in range(18))
    links = tuple(RoadLink(a,b,.14,frozenset((key,)),evidence,False,.1)
                  for a,b in zip(stations,stations[1:]))
    pose = PoseSample('map','clock','calibration',1,*stations[5][1],.4,.003,.001)
    original = _mapped_candidate(links[:4],key,profile)
    model = SimpleNamespace(road=original.road,crossing=bytes(4))
    section = _fit_publication_curve(RoadSection(links), profile)
    road,crossing = stored_road_window((section,), model, pose, profile,
        rear_m=.2,forward_m=.6,demo_course=True)
    wire = lane_model_from_road(road,lane_ids=(1,2),model_revision=1,lane_revisions=(1,1),crossing=crossing)
    divider = tuple((p.x,p.y) for p in wire.divider)
    assert len(divider) >= 5
    assert all(abs(math.hypot(x,y-.6)-.6) < .01 for x,y in divider)
    assert not any(wire.crossing)


def test_demo_window_refits_local_observed_curve_if_the_long_fit_is_missing():
    from xenova_vehicle.transport.world_adapter import lane_model_from_road

    profile = RoadProfile()
    width = profile.missing_outer_width_m
    radius = .6
    key = ('camera', 'rgb', 'clock', 1)
    other = (*key[:-1], 2)
    sources = frozenset((key, other))
    evidence = tuple(frozenset(((key, role), (other, role))) for role in range(3))
    stations = tuple(tuple((r*math.sin(a), radius-r*math.cos(a))
                           for r in (radius-width,radius,radius+width))
                     for a in (i*.08 for i in range(30)))
    links = tuple(RoadLink(a,b,.185,sources,evidence,False,.1)
                  for a,b in zip(stations,stations[1:]))
    original = _mapped_candidate(links[:4],key,profile)
    model = SimpleNamespace(road=original.road,crossing=bytes(4))
    pose = PoseSample('map','clock','calibration',1,*stations[14][1],1.12,.003,.001)
    # A section with no long fit and a fit whose shared chord bound cannot
    # describe this tight bend must both recover the same observed local cut.
    cached_links = tuple(replace(link, boundary_normal_bounds_m=(.185,)*3,
                                 outer_chord_bounds_m=(.5,)*2) for link in links)
    cached = RoadSection(cached_links)
    for section in (RoadSection(links), RoadSection(links, publication_curve=cached)):
        road,crossing = stored_road_window((section,), model, pose, profile,
                                          rear_m=.23,forward_m=.57,demo_course=True)
        wire = lane_model_from_road(road,lane_ids=(1,2),model_revision=1,
                                    lane_revisions=(1,1),crossing=crossing)
        assert len(wire.divider) >= 6
        assert max(math.hypot(a.x-b.x,a.y-b.y) for a,b in zip(wire.divider,wire.divider[1:])) < .16
        assert all(abs(math.hypot(p.x,p.y-radius)-radius) < .015 for p in wire.divider)
        assert not any(wire.crossing)


def test_demo_window_keeps_interior_knots_stable_as_the_rear_axle_advances():
    from xenova_vehicle.perception.temporal_road import _fit_publication_curve
    profile = RoadProfile()
    width = profile.missing_outer_width_m
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    radius = .6
    angles = tuple(i * .08 for i in range(30))
    stations = tuple(tuple((r * math.sin(a), radius - r * math.cos(a))
                           for r in (radius-width, radius, radius+width)) for a in angles)
    links = tuple(RoadLink(a,b,.14,frozenset((key,)),evidence,False,.1)
                  for a,b in zip(stations,stations[1:]))
    original = _mapped_candidate(links[8:14],key,profile)
    model = SimpleNamespace(road=original.road,crossing=bytes(6))
    section = _fit_publication_curve(RoadSection(links), profile)
    assert section.publication_curve is not None

    curves = []
    for index in (10, 11):
        pose = PoseSample('map','clock','calibration',1,*stations[index][1],angles[index],.003,.001)
        road,crossing = stored_road_window((section,),model,pose,profile,rear_m=.28,forward_m=.68,
                                           demo_course=True)
        lane = road.hypotheses[0]
        curves.append(dict(zip(lane.divider, zip(lane.left_outer, lane.right_outer))))
        assert not any(crossing)

    shared = curves[0].keys() & curves[1].keys()
    assert len(shared) >= 3 and curves[0].keys() != curves[1].keys()
    assert all(curves[0][point] == curves[1][point] for point in shared)
    assert all(abs(math.hypot(x,y-radius)-radius) < .01 for curve in curves for x,y in curve)


def test_demo_map_refresh_keeps_station_axis_while_extending_both_ends():
    from xenova_vehicle.perception.temporal_road import _fit_publication_curve

    profile = RoadProfile()
    old_key = ('camera', 'rgb', 'clock', 1)
    new_key = ('camera', 'rgb', 'clock', 2)
    width = profile.missing_outer_width_m

    def link(a, b, key):
        cross = lambda x: ((x,width),(x,0.),(x,-width))
        evidence = tuple(frozenset(((key, role),)) for role in range(3))
        return RoadLink(cross(a),cross(b),.01,frozenset((key,)),evidence,False,.01)

    positions = tuple(i/20 for i in range(21))
    links = tuple(link(a,b,old_key) for a,b in zip(positions,positions[1:]))
    original = _fit_publication_curve(RoadSection(links),profile).publication_curve
    extended = (link(-.17,0.,new_key), *links, link(1.,1.24,new_key))
    updated = _fit_publication_curve(RoadSection(extended),profile,(original,),frozenset((new_key,))).publication_curve

    assert updated.map_stations[0] < original.map_stations[0]
    assert updated.map_stations[-1] > original.map_stations[-1]
    assert all(station in updated.map_stations for station in original.map_stations[1:-1])
    original_by_station = dict(zip(original.map_stations,original.boundaries[1]))
    for at, point in zip(updated.map_stations,updated.boundaries[1]):
        if .2 < at < .8:
            assert point == original_by_station[at]
    assert all(a.end == b.start for a,b in zip(updated.links,updated.links[1:]))


def test_refresh_without_local_observations_preserves_curved_cross_sections():
    from xenova_vehicle.perception.temporal_road import _fit_publication_curve

    profile = RoadProfile()
    key = ('camera', 'rgb', 'clock', 1)
    evidence = tuple(frozenset(((key, role),)) for role in range(3))
    width = profile.missing_outer_width_m
    radius = .6
    points = tuple(tuple((r*math.sin(a), radius-r*math.cos(a))
                         for r in (radius-width,radius,radius+width))
                   for a in (i*.08 for i in range(23)))

    def section(points):
        return RoadSection(tuple(RoadLink(a,b,.01,frozenset((key,)),evidence,False,.04)
                                 for a,b in zip(points,points[1:])))

    original = _fit_publication_curve(section(points),profile).publication_curve
    shifted = tuple(tuple((x,y+.02) for x,y in point) for point in points)
    refreshed = _fit_publication_curve(section(shifted),profile,(original,)).publication_curve
    position = lambda curve: dict(zip(curve.map_stations,zip(*curve.boundaries)))
    before, after = position(original), position(refreshed)
    shared = before.keys() & after.keys()
    assert len(shared) >= 5
    assert all(before[s] == after[s] for s in shared)


def test_wire_shortcut_rejects_cutting_a_supported_turn():
    from xenova_vehicle.perception.temporal_road import _merge_wire_links, _wire_curve_links

    profile = RoadProfile()
    width = profile.missing_outer_width_m
    key = ('camera', 'rgb', 'clock', 1)
    observed = tuple(frozenset(((key, role),)) for role in range(3))

    def station(x,y):
        return tuple((x,y+offset) for offset in (width,0.,-width))

    points = (station(0.,0.),station(.1,.04),station(.2,0.))
    corner = tuple(RoadLink(a,b,.01,frozenset((key,)),observed,False,.02,
                      boundary_normal_bounds_m=(.01,)*3,outer_chord_bounds_m=(.01,)*2)
                   for a,b in zip(points,points[1:]))
    assert _merge_wire_links(corner,profile) is None
    assert tuple(link for link,_,_ in _wire_curve_links(corner,profile)) == corner


def test_reuses_bounded_map_run_with_two_current_markings_and_original_sources():
    profile = RoadProfile()
    limits = RoadGeometryLimits(.065,.05)
    first = ('camera','rgb','clock',1)
    second = (*first[:-1],2)
    current = (*first[:-1],50)
    width = profile.missing_outer_width_m
    stations = tuple(tuple((x,y) for y in (width,0.,-width)) for x in (.30,.35,.40,.45,.50,.55))
    observations = tuple(frozenset(((first,i),(second,i))) for i in range(3))
    links = tuple(RoadLink(a,b,.018,frozenset((first,second)),observations,False,.24,
                           heading_estimate_rad=.02)
                  for a,b in zip(stations,stations[1:]))
    over_limit = replace(links[-1], error_m=.12)
    initial, run = next(_bounded_candidates((*links[:-1],over_limit),second,profile,limits,{first,second}))
    assert initial.road.hypotheses[0].station_s[-1] == pytest.approx(.20)

    sample = PoseSample('map','clock','calibration',5_000_000,0.,0.,0.,.003,.001)
    pose = PoseEstimate(sample,(5_000_000,)*2,(5_000_000,)*2)
    outer_tape = tuple(TapeStrip(i, ((.25,y),(.60,y)), (.01,.02), .003,'FOV_clip','FOV_clip')
                       for i,y in enumerate((width,-width)))
    strips = _project_strips(outer_tape,pose,current)
    reused = _reobserve_run(run,strips,pose,profile,limits)
    assert reused.sources == {first,second,current}
    assert reused.road.source_key == current
    assert reused.road.hypotheses[0].divider == initial.road.hypotheses[0].divider
    assert reused.road.hypotheses[0].error_m == initial.road.hypotheses[0].error_m
    assert reused.road.hypotheses[0].yaw_error_rad == .24
    assert reused.heading_estimate_rad == .02
    assert not replace(reused,heading_estimate_rad=None).within(limits)
    assert _reobserve_run(run,strips[:1],pose,profile,limits) is None
    middle = replace(outer_tape[0],points_xy=((.25,width/2),(.60,width/2)),error_m=.14)
    ambiguous = _project_strips((middle,outer_tape[1]),pose,current)
    assert _reobserve_run(run,ambiguous,pose,profile,limits) is None


def test_heading_estimate_removes_shared_shift_and_uses_distinct_rgb_frames():
    keys = tuple(('camera','rgb','clock',seq) for seq in (1,2,3,4))
    chords = tuple(_MarkingChord(key,0,(0.,.4),(offset,offset+.4*math.tan(angle)),
        angle,(0.,.4),(),None)
        for key,offset,angle in zip(keys,(0.,.07,-.05,.03),(.01,.01,.03,.03)))
    error,sources = _heading_estimate(chords,0.,.4,.02,frozenset(keys),dashed=False)
    assert error == pytest.approx(.01,abs=1e-6)
    assert sources == frozenset(keys)
    assert _heading_estimate(chords,0.,.4,.02,frozenset(keys[:3]),dashed=False) is None


def test_position_estimate_counts_each_rgb_once_and_keeps_shared_shift():
    keys = tuple(('camera','rgb','clock',seq) for seq in range(4))
    chords = tuple(_MarkingChord(key,0,(0.,.4),(offset,offset),0.,(0.,.4),(),None)
                   for key,offset in zip(keys,(.01,.01,.03,.03)))
    error,sources = _position_estimate(chords+chords[:1]*10,0.,.4,(0.,0.),frozenset(keys),dashed=False)
    assert error == pytest.approx(math.sqrt((.01**2+.03**2)/2))
    assert sources == frozenset(keys)
    assert _position_estimate(chords,0.,.4,(0.,0.),frozenset(keys[:3]),dashed=False) is None


def test_boundary_chord_excludes_shared_translation_but_keeps_rotation():
    stations = tuple(tuple((x,y) for y in (.285,0.,-.285)) for x in (.25,.49))
    anchor = PoseSample('map','clock','calibration',1,0.,0.,0.,.02,0.)
    normal,chord = _boundary_bounds(stations,(.01,.02,.01),anchor,.005)
    assert normal == pytest.approx((.035,.045,.035))
    assert chord == pytest.approx((.03,.03))
    moved,relative = _boundary_bounds(stations,(.01,.02,.01),replace(anchor,position_error_m=.20),.005)
    assert moved == pytest.approx(tuple(bound+.18 for bound in normal))
    assert relative == chord
    turned,relative = _boundary_bounds(stations,(.01,.02,.01),replace(anchor,yaw_error_rad=.10),.005)
    assert turned[1] == pytest.approx(normal[1]+.49*math.sin(.10))
    assert relative == pytest.approx((.03+.48*math.sin(.05),)*2)


def test_refits_lane_direction_from_overlapping_markings_across_captures():
    profile = RoadProfile()
    width = profile.missing_outer_width_m
    keys = tuple(('camera','rgb','clock',sequence) for sequence in (1,2,3))
    sample = PoseSample('map','clock','calibration',5_000_000,0.,0.,0.,.001,.001)
    pose = PoseEstimate(sample,(5_000_000,)*2,(5_000_000,)*2)
    strips = []
    for key,(a,b) in zip(keys,((.22,.48),(.37,.65),(.41,.72))):
        pieces = tuple(TapeStrip(i,tuple((a+(b-a)*n/10,y) for n in range(11)),(.01,.02),.006,
            'FOV_clip','FOV_clip',point_error_xy=((.004,.002),)*11,segment_error_xy=((.004,.002),)*10)
            for i,y in enumerate((width,-width)))
        strips.extend(_project_strips(pieces,pose,key))
    start,end = (tuple((x,y) for y in (width,0.,-width)) for x in (.43,.47))
    fit = fit_road_window(strips,start,end,profile,sample,keys[-1],{},max_sources=11)
    assert fit is not None
    assert fit.sources == set(keys)
    assert not fit.observations[1]
    assert fit.stations[0][1][1] == pytest.approx(0.)
    assert fit.position_m < .04 and fit.heading_worst_case_rad < .04
    assert fit.heading_estimate_rad is None
    assert fit_road_window(strips[2:],start,end,profile,sample,keys[-1],{},max_sources=11) is None
    wrong_roles = {(strip.source,strip.identity):{1} for strip in strips if strip.identity == 1}
    assert fit_road_window(strips,start,end,profile,sample,keys[-1],wrong_roles,max_sources=11) is None


def test_refit_uses_separate_short_dash_observations_and_preserves_map_placement_error():
    profile = RoadProfile()
    width = profile.missing_outer_width_m
    keys = tuple(('camera','rgb','clock',sequence) for sequence in (1,2,3))
    sample = PoseSample('map','clock','calibration',1_000_000,0.,0.,0.,.035,.024)
    local_pose = PoseEstimate(replace(sample,position_error_m=0.,yaw_error_rad=0.),
                              (1_000_000,)*2,(1_000_000,)*2)
    strips = []
    for key,centers in zip(keys,((.27,.39),(.50,.61),(.72,))):
        edges = tuple(TapeStrip(i,((.23,y),(.77,y)),(.01,.02),.003,'FOV_clip','FOV_clip',
            point_error_xy=((.003,.002),)*2,segment_error_xy=((.003,.002),))
            for i,y in enumerate((width,-width)))
        dashes = tuple(TapeStrip(i,((x-.022,.011),(x+.022,.011)),(.01,.02),.003,
            'observed_paint_end','observed_paint_end',point_error_xy=((.003,.002),)*2,
            segment_error_xy=((.003,.002),)) for i,x in enumerate(centers,2))
        strips.extend(_project_strips(edges+dashes,local_pose,key))
    start,end = (tuple((x,y) for y in (width,0.,-width)) for x in (.48,.52))
    fit = fit_road_window(strips,start,end,profile,sample,keys[-1],{},max_sources=11)
    assert fit is not None and fit.divider_interpolated
    assert {key for key,_ in fit.observations[1]} == set(keys)
    assert all(station[1][1] == pytest.approx(.011) for station in fit.stations)
    assert fit.position_m < .04
    assert fit.placement_m > sample.position_error_m and fit.placement_rad == sample.yaw_error_rad
    if_disabled = fit_road_window(strips,start,end,replace(profile,allow_dashed=False),sample,keys[-1],{},max_sources=11)
    assert if_disabled is not None and not if_disabled.observations[1]


def test_parking_approach_uses_real_boundaries_and_keeps_range_error_along_the_road():
    entrance = EntranceSpan(((.35,-.285),(.65,-.285)),(0.,-1.),2,.2)
    profile = replace(RoadProfile(),max_curvature_1pm=.2)
    history = ()
    for sequence,x in ((1,0.),(2,.08)):
        key = ('camera','rgb','clock',sequence)
        pose = PoseEstimate(PoseSample('map','clock','calibration',sequence,x,0.,0.,.003,.001),
                            (sequence,sequence),(sequence,sequence))
        strips = tuple(TapeStrip(i,((.05-x,y),(.95-x,y)),(.012,.02),.06,'FOV_clip','FOV_clip',
            point_error_xy=((.06,.004),)*2,segment_error_xy=((.06,.004),))
            for i,y in enumerate((-.285,0.,.285)))
        history += _project_strips(strips,pose,key)
    fit = refit_approach(entrance,history,key,profile)
    assert fit is not None
    shape, = fit.road.hypotheses
    assert fit.sources == {key,('camera','rgb','clock',1)}
    assert fit.longitudinal_bound_m > .06
    assert .007 < fit.lateral_bound_m < .02
    assert shape.error_m == fit.lateral_bound_m
    assert all(math.isclose(math.dist(a,b),.285) for a,b in zip(shape.divider,shape.right_outer))
    assert refit_approach(entrance,tuple(s for s in history if s.identity == 0),key,profile) is None
    assert refit_approach(entrance,history,('camera','rgb','clock',3),profile) is None


def test_parking_approach_aligns_disjoint_boundaries_with_continuous_wheel_motion():
    entrance = EntranceSpan(((.27,-.285),(.57,-.285)),(0.,-1.),2,.2)
    profile = replace(RoadProfile(),max_curvature_1pm=.02)
    wheels = WheelMotionProfile(32,.00001,.00001,.15,.001,.001)
    history = ()
    for sequence in range(1,5):
        x = .02*sequence
        ticks = round(x/wheels.metres_per_tick)
        at = sequence*100_000
        key = ('camera','rgb','clock',sequence)
        observation = WheelObservation(('esp32',),sequence,at,(ticks,ticks),0)
        progress = WheelProgress(('continuous',),observation,wheels,x,2*x)
        raw = PoseEstimate(PoseSample('map','clock','calibration',at,x,0.,0.,.035,.002),
                           (at,at),(at,at),WheelCapture(progress,0.,0.))
        # A previous map alignment can shift the older boundary. Only the raw
        # capture and the last frame's map anchor belong in the relative fit.
        if sequence <= 2:
            start,end,y,map_shift = .28,.58,-.285,.10
        else:
            start,end,y,map_shift = .59,.85,0.,0.
        mapped = replace(raw,sample=replace(raw.sample,y=map_shift,position_error_m=.005))
        tape = TapeStrip(0,((start-x,y),(end-x,y)),(.012,.02),.004,'FOV_clip','FOV_clip',
                         point_error_xy=((.003,.002),)*2,segment_error_xy=((.003,.002),))
        history += _project_strips((tape,),mapped,key,capture_pose=raw)
    fit = refit_approach(entrance,history,key,profile)
    assert fit is not None
    assert fit.road.reasons == ('WHEEL_ALIGNED_APPROACH_TAPE',)
    assert fit.sources == {strip.source for strip in history}
    shape, = fit.road.hypotheses
    assert shape.error_m < .065 and shape.yaw_error_rad < .05
    assert all(abs(y+.285) < .002 for _,y in shape.right_outer)
    corner = refit_approach(entrance,history,key,replace(profile,max_curvature_1pm=.4))
    assert corner is not None
    assert corner.lateral_bound_m > fit.lateral_bound_m

    divergent = history[:2]
    for strip in history[2:]:
        a,b = strip.original.points_xy
        tape = replace(strip.original,points_xy=(a,(b[0],b[1]+.06)))
        divergent += _project_strips((tape,),strip.mapped_pose,strip.source,capture_pose=strip.capture_pose)
    assert refit_approach(entrance,divergent,key,profile) is None

    def disconnect(strip):
        capture = strip.capture_pose.wheel_capture
        different = replace(capture,progress=replace(capture.progress,segment=('new-segment',)))
        return replace(strip,capture_pose=replace(strip.capture_pose,wheel_capture=different))

    assert refit_approach(entrance,tuple(disconnect(s) if s.source[-1] <= 2 else s for s in history),key,profile) is None

    owner = TemporalRoadMap()
    owner._frames.extend((strip,) for strip in history)
    owner._parking_sources.extend(strip.source for strip in history)
    prior, *_ = owner.parking_tape_history(key)
    assert prior[0] == history[0].source
    assert math.isclose(prior[1][0].points_xy[0][0],.20)
    assert math.isclose(prior[1][0].points_xy[0][1],-.285)
    assert prior[1][0].error_m > history[0].original.error_m
    owner._frames[0] = (disconnect(history[0]),)
    assert history[0].source not in dict(owner.parking_tape_history(key))


def test_two_lane_map_retains_large_errors_and_merges_overlapping_captures():
    owner = TemporalRoadMap()
    observe(owner,1,0.,((.20,.60),),error=.06)
    observe(owner,2,.05,((.20,.60),),error=.06)
    original = owner.sections(RoadProfile())[0]
    assert all(link.error_m > .05 for link in original.links)
    # RGB provenance의 최근 범위 밖에서도 누적 형상과 원 오차를 조회할 수 있다.
    for sequence in range(3,17):
        graph = TapeGraph((),source_key=('camera','rgb','clock',sequence),calibration_qualified=True)
        at = sequence*250_000
        pose = PoseEstimate(PoseSample('map','clock','calibration',at,0.,0.,0.,.003,.001),
                            (at,at),(at,at))
        owner.observe(graph,pose,RoadProfile(),TapeProfile())
    assert ('camera','rgb','clock',1) not in owner.source_keys
    observe(owner,17,.35,((.50,.95),),error=.07)
    observe(owner,18,.40,((.50,.95),),error=.07)
    section, = owner.sections(RoadProfile())
    assert section.boundaries[1][0][0] < .30 and section.boundaries[1][-1][0] > .85
    assert all(a.end == b.start for a,b in zip(section.links,section.links[1:]))
    assert len(section.centers) == 2
    captures = {source[-1] for link in section.links for source in link.sources}
    assert 1 in captures and captures&{17,18}
    assert all(link.error_m > .05 for link in section.links)


def test_confirmed_outer_boundaries_keep_center_gap_inferred_in_local_map():
    owner = TemporalRoadMap()
    dashes = ((.20,.40),(.60,.85))
    observe(owner,1,0.,dashes,error=.06)
    observe(owner,2,.05,dashes,error=.06)
    sections = owner.sections(RoadProfile())
    local_links = [link for link in owner.links if link.inferred_roles == (False,True,False)]
    assert sections and local_links
    assert any(not link.observations[1] and link.observations[0] and link.observations[2]
               and link.error_m > .05 for link in local_links)


def test_short_map_seam_uses_direction_and_spacing_across_captures():
    def link(a,b,source,shift=0.):
        station = lambda x: ((x,.285+shift),(x,shift),(x,-.285+shift))
        observations = tuple(frozenset(((source,role),)) for role in range(3))
        return RoadLink(station(a),station(b),.09,frozenset((source,)),observations,False,.01)

    first = link(0.,.20,('camera','rgb','clock',1))
    owner = TemporalRoadMap()
    owner._store((first,link(.265,.45,('camera','rgb','clock',2),shift=.12)))
    assert len(owner.sections(RoadProfile())) == 2
    owner.clear()
    second = link(.265,.45,('camera','rgb','clock',2))
    owner._store((first,second))
    section, = owner.sections(RoadProfile())
    bridge = section.links[1:-1]
    assert bridge[0].start == first.end and bridge[-1].end == second.start
    assert all(link.inferred_roles == (True,True,True) and link.error_m > first.error_m for link in bridge)


def marking_frame(sequence, a, b, *, epoch='map', source='camera'):
    at = sequence*250_000
    stamp = FrameStamp(source,'rgb',sequence,'clock',(at,at),at)
    strip = TapeStrip(0,(a,b),(.02,.04),.062,'FOV_clip','FOV_clip')
    graph = TapeGraph((strip,),source_key=stamp.key,calibration_qualified=True)
    sample = PoseSample(epoch,'clock','calibration',at,0.,0.,0.,.003,.001)
    pose = PoseEstimate(sample,(at,at),(at,at))
    return graph,stamp,pose


def test_startup_projection_preserves_capture_and_stays_out_of_recent_sources():
    original = TemporalRoadMap()
    graph,stamp,pose = marking_frame(1,(.20,.1),(.40,.1))
    original.collect_startup(graph,stamp,pose)
    clone = deepcopy(original)
    _,next_stamp,anchor = marking_frame(2,(.20,.1),(.40,.1))
    fitted = replace(anchor,sample=replace(anchor.sample,x=.4))
    alignment = MapOdomAlignment.from_match(anchor,fitted,observed_j_interval=anchor.captured_us,
        registration_revision=1,valid_for_us=100_000,position_drift_mps=.01,yaw_drift_rps=.02)
    with pytest.raises(ValueError,match='older original capture'):
        alignment.apply(pose)
    clone.place_startup(alignment,stamp=next_stamp)
    mark, = clone.markings
    assert original.startup_markings[0].strips[0] is graph.strips[0]
    assert original.markings == () and clone.startup_markings == ()
    assert mark.historical and mark.stamp is stamp and mark.original is graph.strips[0]
    assert mark.capture_pose is pose and mark.points[0] == (.20+.4,.1)
    assert mark.error_m > mark.original.error_m and clone.source_keys == frozenset()
    projected = alignment.project_history(pose)
    assert projected.captured_us == pose.captured_us
    assert projected.sample.position_error_m >= pose.sample.position_error_m+.0025
    assert projected.sample.yaw_error_rad >= pose.sample.yaw_error_rad+.005


def test_publication_refreshes_when_a_new_marking_extends_the_reference():
    owner = TemporalRoadMap()
    profile = RoadProfile()
    observe(owner, 1, 0., ((.20,.60),), error=.06)
    observe(owner, 2, .05, ((.20,.60),), error=.06)
    graph,stamp,pose = marking_frame(3,(.35,.1),(.60,.1))
    previous = owner.publication_sections(profile, stamp.captured_us[0])
    assert previous
    revision = owner.revision
    owner.observe(replace(graph, complete=False, reasons=('UNUSABLE_GRAPH',)),
                  pose, profile, TapeProfile(), stamp=stamp)
    assert owner.revision == revision
    assert owner.publication_sections(profile,stamp.captured_us[0]+100_000) is previous
    assert owner.publication_sections(profile,stamp.captured_us[0]+500_000) is not previous


def test_unclassified_single_boundary_joins_only_nearby_aligned_paint():
    owner = TemporalRoadMap()
    for sequence,a,b in ((1,(.20,.1),(.40,.1)),(2,(.46,.1),(.70,.1)),
                         (3,(.75,.1),(.75,.30)),(4,(.80,.38),(1.,.38))):
        graph,stamp,pose = marking_frame(sequence,a,b)
        owner.observe(graph,pose,RoadProfile(),TapeProfile(),stamp=stamp,capture_pose=pose)
    join, = owner.marking_joins(RoadProfile())
    assert join.points == ((.4,.1),(.46,.1))
    assert join.error_m > .062 and len(owner.markings) == 4
    assert owner.sections(RoadProfile()) == ()


def test_first_capture_uses_lane_width_and_tangent_on_a_curve_without_paint_ends():
    owner = TemporalRoadMap()
    radius,width = .70,.285
    stamp = FrameStamp('camera','rgb',1,'clock',(250_000,250_000),250_000)
    strips = []
    for role,r in enumerate((radius-width,radius,radius+width)):
        points = tuple((r*math.sin(.20+i*.04),radius-r*math.cos(.20+i*.04)) for i in range(15))
        strips.append(TapeStrip(role,points,(.02,.04),.012,'FOV_clip','FOV_clip'))
    graph = TapeGraph(tuple(strips),source_key=stamp.key,calibration_qualified=True)
    sample = PoseSample('map','clock','calibration',250_000,0.,0.,0.,.003,.001)
    pose = PoseEstimate(sample,stamp.captured_us,stamp.captured_us)
    assert owner.observe(graph,pose,RoadProfile(),TapeProfile()) is None
    section = max(owner.sections(RoadProfile()),key=lambda s: len(s.links))
    assert any(link.inferred_roles == (False,False,False) for link in section.links)
    assert all(abs(math.dist(a,b)-width)<.035 for a,b in zip(*section.boundaries[:2]))
    assert all(abs(math.hypot(x,y-radius)-radius)<.02 for x,y in section.boundaries[1])
