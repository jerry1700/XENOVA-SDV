"""원 관측을 지도에 놓고 두 차로의 짧은 공백만 연결하는 반례다."""

from dataclasses import replace
from copy import deepcopy
import math
import pytest

from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.wheel_motion import WheelCapture, WheelMotionProfile, WheelObservation, WheelProgress
from xenova_vehicle.perception.inputs import FrameStamp
from xenova_vehicle.perception.parking_approach import refit_approach
from xenova_vehicle.perception.parking_entrance import EntranceSpan
from xenova_vehicle.perception.road import RoadProfile
from xenova_vehicle.perception.tape import TapeGraph, TapeProfile, TapeStrip
from xenova_vehicle.perception.temporal_road import RoadLink, TemporalRoadMap, _project_strips


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
