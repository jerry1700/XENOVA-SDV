"""구간 적분과 원 프레임 재사용의 오차 반례다."""

from dataclasses import replace
import unittest

from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.odometry import PoseBuffer, PoseSample
from xenova_vehicle.mapping.wheel_motion import WheelMotionProfile, WheelObservation, relative_wheel_bounds
from xenova_vehicle.perception.inputs import FrameStamp


class RelativeWheelMotionTests(unittest.TestCase):
    def setUp(self):
        self.profile = WheelMotionProfile(32,.001,.0011,.18,.001,.001)
        self.buffer = PoseBuffer(max_speed_mps=1., wheel_profile=self.profile)
        self.source = ('gateway','pi','wheel')

    def sample(self, sequence, at, x, ticks, *, clock_pad=0, source=None):
        pose = PoseSample('odom','clock','cal',at,x,0.,0.,.04,.03)
        wheel = WheelObservation(source or self.source,sequence,at,(ticks,ticks),clock_pad)
        self.buffer.add(pose,wheel=wheel)

    def capture(self, lo, hi=None):
        hi = lo if hi is None else hi
        return self.buffer.at(FrameStamp('camera','rgb',lo,'clock',(lo,hi),hi),'cal')

    def test_same_capture_keeps_only_the_video_fit_error(self):
        self.sample(1,100_000,0.,0)
        self.sample(2,150_000,.01,10)
        original = self.capture(100_000,110_000)
        fitted = replace(original,sample=replace(original.sample,x=.03,
            position_error_m=.025,yaw_error_rad=.015),wheel_capture=None)
        alignment = MapOdomAlignment.from_match(original,fitted,observed_j_interval=original.captured_us,
            registration_revision=1,valid_for_us=100_000,position_drift_mps=.001,yaw_drift_rps=.001)
        self.assertEqual(alignment.apply(original),fitted)
        different_bracket = replace(original,bracket_us=(100_000,160_000))
        self.assertGreater(alignment.apply(different_bracket).sample.position_error_m,fitted.sample.position_error_m)

    def test_signed_wrap_and_reversing_keep_the_distance_travelled(self):
        maximum = 2**31-1
        self.sample(4,100_000,0.,maximum-2)
        self.sample(5,120_000,.01,-2**31+7)
        self.sample(6,140_000,0.,maximum-2)
        start, middle, end = (self.capture(at) for at in (100_000,120_000,140_000))
        forward = relative_wheel_bounds(start,middle)
        reverse = relative_wheel_bounds(start,end)
        self.assertEqual(start.sample.x,end.sample.x)
        self.assertGreater(reverse[0],forward[0])
        self.assertGreater(reverse[1],forward[1])
        self.assertEqual(relative_wheel_bounds(end,start),reverse)
        self.assertLess(reverse[0],.01)

    def test_alignment_uses_short_motion_and_keeps_capture_timing_uncertainty(self):
        self.sample(1,100_000,0.,0,clock_pad=500)
        self.sample(2,120_000,.01,10,clock_pad=500)
        self.sample(3,140_000,.02,20,clock_pad=500)
        start, now = self.capture(100_000),self.capture(120_000)
        uncertain = self.capture(118_000,125_000)
        self.assertGreater(relative_wheel_bounds(start,uncertain)[0],relative_wheel_bounds(start,now)[0])
        fit = replace(start,sample=replace(start.sample,position_error_m=.02,yaw_error_rad=.01),wheel_capture=None)
        alignment = MapOdomAlignment.from_match(start,fit)
        propagated = alignment.apply(now).sample
        fallback = alignment.apply(replace(now,wheel_capture=None)).sample
        self.assertGreater(propagated.position_error_m,fit.sample.position_error_m)
        self.assertLess(propagated.position_error_m,fallback.position_error_m)
        self.assertLess(propagated.yaw_error_rad,fallback.yaw_error_rad)

    def test_startup_pose_includes_wheel_quantization_missing_from_upstream_bound(self):
        self.sample(1,100_000,0.,0)
        self.sample(2,120_000,.01,10)
        start, now = (replace(capture,sample=replace(capture.sample,
            position_error_m=.0001,yaw_error_rad=.0001))
            for capture in (self.capture(100_000),self.capture(120_000)))
        wheel_position, wheel_yaw = relative_wheel_bounds(start,now)

        pose = MapOdomAlignment.project_startup(start,now,
            position_drift_mps=.001,yaw_drift_rps=.001).sample

        self.assertGreaterEqual(pose.position_error_m,start.sample.position_error_m+wheel_position+.00002)
        self.assertGreaterEqual(pose.yaw_error_rad,start.sample.yaw_error_rad+wheel_yaw+.00002)
        self.assertEqual((pose.x,pose.y,pose.yaw),(now.sample.x,now.sample.y,now.sample.yaw))

    def test_straight_motion_does_not_pay_full_tick_quantization_at_every_sample(self):
        for index in range(9):
            self.sample(index+1,100_000+index*20_000,index*.001,index)
        position,_ = relative_wheel_bounds(self.capture(100_000),self.capture(260_000))
        self.assertGreaterEqual(position,self.profile.metres_per_tick_upper)
        self.assertLess(position,2*self.profile.metres_per_tick_upper)

    def test_missing_sample_changed_source_or_inconsistent_pose_breaks_relative_proof(self):
        self.sample(1,100_000,0.,0)
        self.sample(3,120_000,.01,10)
        self.sample(4,140_000,.02,20)
        self.sample(5,160_000,.02,30)
        self.sample(6,180_000,.03,40,source=('new-gateway','pi','wheel'))
        first,after_gap,normal,wrong_pose,new_source = (self.capture(at) for at in range(100_000,180_001,20_000))
        self.assertIsNone(relative_wheel_bounds(first,after_gap))
        self.assertIsNone(self.capture(110_000).wheel_capture)
        self.assertIsNotNone(relative_wheel_bounds(after_gap,normal))
        self.assertIsNone(relative_wheel_bounds(normal,wrong_pose))
        self.assertIsNone(relative_wheel_bounds(wrong_pose,new_source))
