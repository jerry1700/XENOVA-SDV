"""가려진 코너에서 화면 앞에 들어온 옆 차로가 원 지도 차로를 덮지 않아야 한다."""

import math
import unittest

from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.perception.lane_tracking import MAX_LANE_SECTIONS, mapped_lane_ahead, track_lane
from xenova_vehicle.perception.road import RoadProfile, infer_road
from xenova_vehicle.perception.tape import TapeGraph, TapeStrip


def pose(sequence, x, y=0., yaw=0., *, epoch='odom-1'):
    at = sequence * 100_000
    sample = PoseSample(epoch, 'clock', 'calibration', at, x, y, yaw, .002, .002)
    return PoseEstimate(sample, (at, at), (at, at))


def visible_pair(sequence, estimate, x_limits, *, adjacent=False):
    """지도에서 점선 y=.1425의 한쪽 차로만 가상 카메라에 보인다."""
    sample = estimate.sample
    c, s = math.cos(sample.yaw), math.sin(sample.yaw)

    def local(x, y):
        dx, dy = x - sample.x, y - sample.y
        return dx*c + dy*s, -dx*s + dy*c

    divider = TapeStrip(0, tuple(local(x, .1425) for x in x_limits), (.02, .03), .003,
                        'observed_paint_end', 'FOV_clip')
    outer_y = .4275 if adjacent else -.1425
    outer = TapeStrip(1, tuple(local(x, outer_y) for x in x_limits), (.02, .03), .003,
                      'FOV_clip', 'FOV_clip')
    return TapeGraph((divider, outer), source_key=('camera', 'rgb', 'clock', sequence),
                     calibration_qualified=True)


class LaneTrackingTests(unittest.TestCase):
    def update(self, previous, graph, estimate, map_frame='map-1'):
        profile = RoadProfile()
        scope = (map_frame, estimate.sample.context)
        return track_lane(previous, infer_road(graph, profile), graph, estimate, scope, profile)

    def test_turned_pose_keeps_first_lane_then_reacquires_same_side(self):
        seed_pose = pose(1, 0.)
        seed_graph = visible_pair(1, seed_pose, (.35, .8))
        seed, status = self.update(None, seed_graph, seed_pose)
        self.assertEqual(status, 'TRACKED')

        next_pose = pose(2, .10, .01, .03)
        next_graph = visible_pair(2, next_pose, (.45, .9))
        tracked, status = self.update(seed, next_graph, next_pose)
        self.assertEqual(status, 'TRACKED')
        self.assertEqual(tracked.summary(status)['side'], 'RIGHT')
        self.assertLess(max(abs(y) for _, y in tracked.latest.points), .01)

        turn = pose(3, .15, .01, .39)
        adjacent = visible_pair(3, turn, (.65, 1.05), adjacent=True)
        self.assertTrue(infer_road(adjacent).hypotheses)
        held, status = self.update(tracked, adjacent, turn)
        self.assertEqual(status, 'HELD')
        self.assertIs(held, tracked)
        self.assertEqual(held.summary(status)['captured_us'], next_pose.captured_us)

        after = pose(4, 1.25)
        clear = visible_pair(4, after, (1.60, 2.05))
        resumed, status = self.update(held, clear, after)
        self.assertEqual(status, 'TRACKED')
        self.assertEqual(resumed.first_source_key, seed_graph.source_key)
        self.assertEqual(resumed.latest.source_key, clear.source_key)

        reset = pose(5, 1.25, epoch='odom-2')
        empty = TapeGraph((), source_key=('camera', 'rgb', 'clock', 5), calibration_qualified=True)
        self.assertEqual(self.update(resumed, empty, reset, 'map-2'), (None, 'UNAVAILABLE'))

    def test_earlier_far_section_is_reused_when_the_current_view_is_shorter(self):
        initial_pose = pose(1, 0.)
        far = visible_pair(1, initial_pose, (.40, .92))
        first, status = self.update(None, far, initial_pose)
        self.assertEqual(status, 'TRACKED')
        closer_pose = pose(2, .12)
        near = visible_pair(2, closer_pose, (.44, .66))
        current, status = self.update(first, near, closer_pose)
        self.assertEqual(status, 'TRACKED')
        self.assertEqual(current.latest.source_key, near.source_key)
        for sequence in range(3, MAX_LANE_SECTIONS+4):
            closer_pose = pose(sequence, .12)
            near = visible_pair(sequence, closer_pose, (.44, .66 + sequence*.001))
            current, status = self.update(current, near, closer_pose)
            self.assertEqual(status, 'TRACKED')
        self.assertEqual(len(current.sections), MAX_LANE_SECTIONS)

        blind_pose = pose(MAX_LANE_SECTIONS+4, .69)
        mapped = mapped_lane_ahead(current, blind_pose, RoadProfile())
        self.assertEqual(mapped.source_key, far.source_key)
        self.assertEqual(current.summary('MAPPED', mapped)['captured_us'], initial_pose.captured_us)
        self.assertIsNone(mapped_lane_ahead(current, pose(4, .69, .285), RoadProfile()))
        self.assertIsNone(mapped_lane_ahead(current, pose(4, .95), RoadProfile()))
