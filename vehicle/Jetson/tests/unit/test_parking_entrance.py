"""공유 테이프에 붙은 주차 입구를 관측 순서대로 확인한다."""

import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.registration import RegistrationProfile
from xenova_vehicle.perception.parking_approach import (_boundary_bound, _chord_error, _edge_chords,
    _local_tangent_bounds, _point, _relative_entrance_bounds)
from xenova_vehicle.perception.parking_entrance import (EntranceBranch, EntranceSpan, entrance_spans,
    entrance_summary, observe_entrances, parking_mouths, parking_slot_tape)
from xenova_vehicle.perception.parking import VALID, extract_slots
from xenova_vehicle.perception.calibration import BodyModel
from xenova_vehicle.perception.road import RoadProfile
from xenova_vehicle.perception.tape import TapeGraph, TapeProfile, TapeStrip
from xenova_vehicle.perception.temporal_road import RoadLink, RoadSection, TemporalRoadMap, _mapped_parking_approach
from test_parking import source, strip


def observe(owner, sequence, x, walls=()):
    lines = [
        ((.2,.285),(1.25,.285)), ((.2,0.),(1.25,0.)), ((.2,-.285),(1.25,-.285)),
        # 출발선처럼 도로를 가로지르는 선.
        ((.35,-.285),(.35,.285)),
    ]
    # 옆선과 뒤쪽 선이 한 조각으로 연결되어 있어도 옆선 방향을 독립적으로 검사한다.
    lines.extend(((wall,-.285),(wall,-.43),(wall+.12,-.43)) for wall in walls)
    strips = tuple(TapeStrip(i, tuple((a-x,b) for a,b in line), (.02,.03), .01,
                            'physical_junction','FOV_clip') for i,line in enumerate(lines))
    graph = TapeGraph(strips,source_key=('camera','rgb','clock',sequence),calibration_qualified=True)
    at = sequence*100_000
    sample = PoseSample('map','clock','calibration',at,x,0.,0.,.003,.001,origin='ODOMETRY')
    pose = PoseEstimate(sample,(at,at),(at,at))
    owner.observe(graph,pose,RoadProfile(),TapeProfile())


class ParkingEntranceTests(unittest.TestCase):
    def test_mouth_uses_current_paint_and_walls_can_come_from_different_views(self):
        current = source(2)
        previous = source(1)
        mouth = EntranceSpan(((0.,-.02),(.40,.02)),(0.,1.),0,.12)
        left = strip(0,((0.,0.),(0.,.55)))
        right = strip(1,((.40,0.),(.40,.55)))
        back = strip(2,((0.,.55),(.40,.55)))
        shared = strip(3,((-.05,0.),(.45,0.)))
        graph = TapeGraph((right,back,shared),source_key=current.stamp.key,calibration_qualified=True)
        (_,aisle), = parking_mouths((mouth,),graph,(0.,0.,0.))
        self.assertAlmostEqual(aisle.tangent[1],0.)
        self.assertEqual(extract_slots(graph,aisle,source=current,body=BodyModel()).candidates,())
        combined, originals = parking_slot_tape(graph,aisle,((previous.stamp.key,(left,)),))
        self.assertEqual(originals,frozenset((previous.stamp.key,current.stamp.key)))
        result = extract_slots(combined,aisle,source=current,body=BodyModel(),shared_entry=True)
        candidate, = result.candidates
        self.assertEqual(candidate.geometry_state,VALID)
        self.assertAlmostEqual(candidate.corners[3][1],.55)
        self.assertIsNone(parking_slot_tape(replace(graph,strips=(right,shared)),aisle,((previous.stamp.key,(left,)),)))

    def test_approach_chord_counts_shared_capture_yaw_once(self):
        pose = PoseSample('map','clock','calibration',0,0.,0.,0.,.08,.02,origin='ODOMETRY')
        first = _point((.4,0.),(.4,0.),(0.,0.),(0.,0.),pose,(0.,0.),0.,('rgb',1),vertex_bounds=True)
        last = _point((.8,0.),(.8,0.),(0.,0.),(0.,0.),pose,(0.,0.),0.,('rgb',1),vertex_bounds=True)
        self.assertAlmostEqual(_chord_error(first,last), .02)
        self.assertGreater(_chord_error(first,replace(last,source=('rgb',2))), .02)

    def test_observed_approach_line_does_not_create_gap_at_uncertain_shared_vertex(self):
        pose = PoseSample('map','clock','calibration',0,0.,0.,0.,0.,0.,origin='ODOMETRY')
        error = (.04,.002)
        points = tuple(_point((x,0.),(x,0.),error,error,pose,(0.,0.),0.,('rgb',1),vertex_bounds=True)
                       for x in (-.1,0.,.1))
        strip = SimpleNamespace(original=SimpleNamespace(segment_error_xy=(error,error)))
        profile = RoadProfile()
        point_only = _boundary_bound(points,-.015,.015,0.,0.,.2,profile)
        observed = _boundary_bound(points,-.015,.015,0.,0.,.2,profile,rows=((strip,points),))
        self.assertGreater(point_only, .01)
        self.assertAlmostEqual(observed, .002)

    def test_local_heading_accounts_for_a_turn_inside_the_observed_chord(self):
        pose = PoseSample('map','clock','calibration',0,0.,0.,0.,0.,0.,origin='ODOMETRY')
        points = tuple(_point((x,0.),(x,0.),(0.,0.),(0.,0.),pose,(0.,0.),0.,('rgb',1),vertex_bounds=True)
                       for x in (0.,.30))
        chords = tuple(_edge_chords(points))
        straight = _local_tangent_bounds(chords,0.,.30,0.,0.)
        self.assertEqual(straight, (0.,0.,0.))
        curved = _local_tangent_bounds(chords,0.,.30,0.,1.)
        self.assertGreater(curved[1], 0.)
        self.assertGreater(curved[0], curved[1])
        self.assertAlmostEqual(curved[0], curved[2])

    def test_relative_entrance_requires_an_observed_junction(self):
        pose = PoseSample('map','clock','calibration',0,0.,0.,0.,.01,0.,origin='ODOMETRY')
        shared = TapeStrip(1,((.2,-.28),(.52,-.28)),(.01,.03),.02,
                           'physical_junction','FOV_clip',endpoint_error_m=(.02,None))
        strip = SimpleNamespace(original=shared,points=shared.points_xy,mapped_pose=SimpleNamespace(sample=pose))
        entrance = EntranceSpan(((.2,-.28),(.52,-.28)),(0.,-1.),2,.4)
        self.assertEqual(_relative_entrance_bounds(entrance,((strip,()),)),(.03,None))

    def test_curved_mouth_uses_both_confirmed_branch_directions(self):
        source = ('camera','rgb','clock',1)
        first = EntranceBranch((-.36,-.63),(.926,-.377),2,.04,source,source,2)
        second = EntranceBranch((-.21,-.35),(.737,-.676),2,.04,source,source,2)
        span, = entrance_spans((first,second))
        self.assertEqual(span.endpoints,(first.xy,second.xy))
        self.assertEqual(entrance_spans((first,replace(second,observations=1))), ())

    def test_control_road_map_skips_entrance_diagnostics_across_copy_and_clear(self):
        control = deepcopy(TemporalRoadMap(parking_entrance_diagnostics=False))
        control.clear()
        diagnostic = TemporalRoadMap()
        for index in range(1, 5):
            observe(diagnostic, index, .05*index, walls=(.65,.95))
        self.assertTrue(diagnostic.parking_branches)
        with patch('xenova_vehicle.perception.temporal_road.observe_entrances') as entrances:
            for index in range(1, 5):
                observe(control, index, .05*index, walls=(.65,.95))
        entrances.assert_not_called()
        self.assertEqual(control.links, diagnostic.links)
        self.assertEqual(control.parking_branches, ())

    def test_partial_sightings_merge_in_map_without_promoting_a_single_branch(self):
        owner = TemporalRoadMap()
        observe(owner,1,0.)
        self.assertEqual(owner.parking_branches, ())
        observe(owner,2,.05,walls=(.65,))
        observe(owner,2,.05,walls=(.65,))
        branch, = owner.parking_branches
        self.assertEqual(branch.observations, 1)
        self.assertFalse(entrance_summary(owner.parking_branches)['approach_segments'])

        observe(owner,3,.10,walls=(.65,.95))
        self.assertFalse(entrance_summary(owner.parking_branches)['approach_segments'])
        self.assertEqual(len(entrance_summary(owner.parking_branches)['candidates']), 1)
        observe(owner,4,.15,walls=(.95,))
        segment, = entrance_summary(owner.parking_branches)['approach_segments']
        endpoints = sorted(segment['endpoints'])
        self.assertAlmostEqual(endpoints[0][0], .65, places=2)
        self.assertAlmostEqual(endpoints[1][0], .95, places=2)
        self.assertTrue(all(abs(point[1]+.285) < .01 for point in endpoints))
        self.assertLess(segment['outward'][1], -.9)

        observe(owner,5,.20)
        self.assertEqual(entrance_summary(owner.parking_branches)['approach_segments'], [segment])
        owner.clear()
        self.assertFalse(owner.parking_branches)

    def test_later_road_extension_can_place_earlier_branches_with_observed_edge(self):
        link = RoadLink(((.2,.285),(.2,0.),(.2,-.285)),
            ((1.25,.285),(1.25,0.),(1.25,-.285)), .02, frozenset(), (),
            True, .1, (False,False,True))
        branches = tuple(SimpleNamespace(points=((x,-.29),(x,-.43)), error_m=.02,
            source=('camera','rgb','clock',sequence)) for sequence in (1,2) for x in (.65,.95))
        self.assertFalse(observe_entrances((), branches, (link,), followed=True))
        observed_center = frozenset({(('camera','rgb','clock',3),0)})
        linked = replace(link, observations=(frozenset(),observed_center,frozenset()))
        self.assertFalse(observe_entrances((), branches, (linked,)))
        retained = observe_entrances((), branches, (linked,), followed=True)
        self.assertEqual([item.observations for item in retained], [2,2])
        self.assertEqual(len(entrance_summary(retained)['approach_segments']), 1)
        self.assertEqual(observe_entrances(retained, branches, (linked,), followed=True), retained)

    def test_curved_approach_requires_shared_and_neighboring_tape_within_error_limits(self):
        current = ('camera','rgb','clock',3)
        earlier = ('camera','rgb','clock',2)
        stations = tuple(tuple((x, y + x*x/2) for y in (.285, 0., -.285))
                         for x in (.0, .06, .12, .18, .24, .30))
        seen = (frozenset(), frozenset({(earlier, 1)}), frozenset({(current, 2)}))
        links = tuple(RoadLink(a, b, .02, frozenset((earlier, current)), seen,
                               True, .02, (True, False, False)) for a, b in zip(stations, stations[1:]))
        mouth = EntranceSpan((stations[0][2], stations[-1][2]), (0., -1.), 2, .03)

        def fit(run):
            return _mapped_parking_approach((RoadSection(run),), mouth, current,
                                            RoadProfile(), RegistrationProfile())

        candidate = fit(links)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.road.hypotheses[0].right_outer, tuple(row[2] for row in stations))
        self.assertEqual(candidate.sources, frozenset((earlier, current)))
        self.assertIsNone(fit(tuple(replace(link, inferred_roles=(True, False, True)) for link in links)))
        self.assertIsNone(fit(tuple(replace(link, yaw_error_rad=.2) for link in links)))
        old_shared = (seen[0], frozenset({(current, 1)}), frozenset({(earlier, 2)}))
        self.assertIsNone(fit(tuple(replace(link, observations=old_shared) for link in links)))
