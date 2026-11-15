"""Exercise the surveyed mode with the original mapper and World schemas."""

from dataclasses import replace
import json
import math
from types import SimpleNamespace

import pytest
import test_mapper_world
import test_mapper_runtime_profile
from xenova_vehicle.mapping.survey_course import SurveyCourse
from xenova_vehicle.mapping.survey_registration import SurveyArc, refresh_corner_alignment
from xenova_vehicle.perception.tape import TapeGraph, TapeStrip
from xenova_vehicle.runtime.mapper_profile import MapperProfile, MapperProfileError, StoppingLimits
from xenova_vehicle.runtime.mapper_mapping import MappingContextPort
from xenova_vehicle.runtime.mapper_publication import MapperPublicationPort
from xenova_vehicle.runtime.resident_workers import relay_publications
from xenova_drive_world import decode


def test_mapper_profile_passes_pinned_measured_course():
    original = test_mapper_runtime_profile.MapperRuntimeProfileTests(methodName='runTest')
    original.setUp()
    fields = original.config.to_dict()
    fields['catalog_policy'] = dict(crossing_allowed=True, aisle_lane_indices=[])
    fields['parking_aisles'] = []
    fields['localization'] = dict(registration_valid_for_us=25_000_000,position_drift_mps=.002,yaw_drift_rps=.002)
    fields['survey_course'] = dict(origin=[0., .7], long_m=1.51, short_m=.46,
                                   radius_m=.47, lane_width_m=.285, heading_rad=0.)
    original.config = MapperProfile(fields)
    proc = original.processor()
    assert len(proc._config.survey_course.arcs) == 4
    assert proc._config.survey_course.pose(0., 0)[:2] == (0., .7-.47)
    item = original.item()
    summary = json.loads(proc.geometry(item,None,lambda *_: None))['latest']
    assert summary['localization_configured']
    assert proc._config.localization.registration_valid_for_us == 25_000_000
    proc.cancelled(item,'TEST_COMPLETE')
    with pytest.raises(MapperProfileError):
        MapperProfile({k: v for k, v in fields.items() if k != 'survey_course'})
    fields['localization']['registration_valid_for_us'] += 1
    with pytest.raises(MapperProfileError):
        MapperProfile(fields)


def test_corner_tape_refreshes_only_qualified_capture_and_keeps_yaw():
    original = test_mapper_world.MapperWorldTests(methodName='runTest')
    original.setUp()
    sample = original.inputs.pose.sample
    raw = replace(original.inputs.pose, captured_us=(1_001_000,1_001_100), bracket_us=(1_001_000,1_001_100),
                  sample=replace(sample, x=0., y=0., yaw=0.),wheel_capture=None)
    mapped = replace(raw,sample=replace(raw.sample,x=.013,y=-.009,yaw_error_rad=.013))
    survey_arc = SurveyArc((.85,.5), -math.pi/2, .47, .285)
    strips=[]
    for i, a in enumerate((-1.5,-1.3,-1.1,-.9,-.7,-.5,-.3,-.15)):
        points=tuple((.85+.47*math.cos(a+da), .5+.47*math.sin(a+da)) for da in (-.04,0.,.04))
        strips.append(TapeStrip(i,points,(.010,.012),.001,'observed_paint_end','observed_paint_end',
                                endpoint_error_m=(.001,.001)))
    observed = TapeGraph(tuple(strips), calibration_qualified=True, paint_roles=tuple((i,1) for i in range(len(strips))))
    previous = replace(original.alignment, valid_for_us=25_000_000)
    fit, alignment = refresh_corner_alignment(observed,raw,mapped,previous,(survey_arc,),now_us=1_001_100)
    assert alignment.observed_j_interval == raw.captured_us
    assert alignment.valid_for_us == 25_000_000
    assert alignment.valid_at(raw.captured_us[0]+24_999_999)
    assert not alignment.valid_at(raw.captured_us[0]+25_000_000)
    assert fit.sample.yaw == mapped.sample.yaw and fit.sample.yaw_error_rad == mapped.sample.yaw_error_rad
    assert math.hypot(fit.sample.x,fit.sample.y) < math.hypot(mapped.sample.x,mapped.sample.y)
    assert refresh_corner_alignment(replace(observed, calibration_qualified=False),raw,mapped,
        original.alignment,(survey_arc,),now_us=1_001_100) is None


def test_resident_world_builder_uses_surveyed_geometry_and_keeps_grid_proof():
    original = test_mapper_world.MapperWorldTests(methodName='runTest')
    original.setUp()
    control = original.build().message()
    course = SurveyCourse((0., .7),1.51,.46,.47,.285)
    built = original.build(survey_course=course,survey_crossing=True)
    assert built is not None
    value = built.message()
    assert value.reference_id.startswith('survey:')
    assert len(value.reference) == 48
    for field in ('occupancy','age_le32','drivable_permission','reference_odom_seq','reference_vehicle_pose','localization'):
        assert getattr(value,field) == getattr(control,field)
    assert set(value.lane_model.crossing) == {1}


def test_actual_resident_report_reaches_link_with_unchanged_bytes():
    runtime = test_mapper_runtime_profile.MapperRuntimeProfileTests(methodName='runTest')
    runtime.setUp()
    original = test_mapper_world.MapperWorldTests(methodName='runTest')
    original.setUp()
    processor = runtime.processor()
    processor.clock = lambda: original.now+20
    processor._config = SimpleNamespace(stopping=StoppingLimits(.5,1.,10_000,50_000,30_000,.005),
        demo_road_fill=False,demo_pose_bounds=None,
        survey_course=SurveyCourse((0., .7),1.51,.46,.47,.285),catalog_policy=SimpleNamespace(crossing_allowed=True))
    context_port = MappingContextPort()
    context_port.publish(original.context)
    processor._publication_port = MapperPublicationPort(context_port)
    processor._mapping = SimpleNamespace(_atlas=None)
    processor._tof_obstacles = SimpleNamespace(apply=lambda snapshot, **opts: (snapshot, opts['static_retained']))
    draft = original.draft
    processor._mapping_draft = replace(draft, result=replace(draft.result,
        summary=dict(draft.result.summary,rgb_updated=True)))
    report, reason = processor._world_report(SimpleNamespace(sensors=original.sensor_owner))
    assert reason == 'READY'
    assert processor._publication_port.offer(report)
    calls=[]
    link=SimpleNamespace(offer_world=lambda raw, **facts: calls.append(raw) is None)
    assert relay_publications(processor._publication_port,link,clock=processor.clock) == 1
    assert calls == [report.raw]
    assert decode(calls[0]).reference_id.startswith('survey:')
