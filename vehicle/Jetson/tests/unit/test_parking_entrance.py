"""공유 테이프에 붙은 주차 입구를 관측 순서대로 확인한다."""

import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.perception.parking_entrance import entrance_summary, observe_entrances
from xenova_vehicle.perception.road import RoadProfile
from xenova_vehicle.perception.tape import TapeGraph, TapeProfile, TapeStrip
from xenova_vehicle.perception.temporal_road import RoadLink, TemporalRoadMap


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
