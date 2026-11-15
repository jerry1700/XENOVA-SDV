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

    def test_new_map_uses_the_first_encoder_event_and_keeps_relative_quantization(self):
        self.sample(1,100_000,0.,0)
        self.sample(2,120_000,.01,10)
        start, now = self.capture(100_000), self.capture(120_000)
        wheel_position, wheel_yaw = relative_wheel_bounds(start,now)

        pose = MapOdomAlignment.project_startup(start,now,
            position_drift_mps=.001,yaw_drift_rps=.001).sample

        self.assertAlmostEqual(pose.position_error_m,wheel_position)
        self.assertAlmostEqual(pose.yaw_error_rad,wheel_yaw)
        self.assertGreaterEqual(pose.position_error_m,self.profile.metres_per_tick_upper)
        self.assertLess(pose.position_error_m,start.sample.position_error_m)
        self.assertEqual(MapOdomAlignment.project_startup(start,start,
            position_drift_mps=.001,yaw_drift_rps=.001).sample.x,start.sample.x)
        self.assertEqual((pose.x,pose.y,pose.yaw),(now.sample.x,now.sample.y,now.sample.yaw))

    def test_exposure_uses_interior_samples_and_checks_the_actual_adjacent_gaps(self):
        self.buffer = PoseBuffer(max_gap_us=25_000,max_speed_mps=1.,wheel_profile=self.profile)
        for index in range(5):
            self.sample(index+1,100_000+index*20_000,0.,0,clock_pad=500)
        capture = self.capture(100_000,180_000)
        self.assertEqual(capture.sample.at_us,140_000)
        self.assertEqual(capture.support_us,tuple(range(100_000,180_001,20_000)))
        self.assertAlmostEqual(capture.sample.position_error_m,.05)
        self.assertAlmostEqual(capture.sample.yaw_error_rad,.05)
        self.assertEqual(capture.wheel_capture.anchor_sample.at_us,140_000)
        self.assertLess(capture.wheel_capture.position_pad_m,.014)
        self.sample(6,220_000,0.,0)
        self.assertIsNone(self.capture(180_000,220_000))

    def test_straight_motion_does_not_pay_full_tick_quantization_at_every_sample(self):
        for index in range(9):
            self.sample(index+1,100_000+index*20_000,index*.001,index)
        position,_ = relative_wheel_bounds(self.capture(100_000),self.capture(260_000))
        self.assertGreaterEqual(position,self.profile.metres_per_tick_upper)
        self.assertLess(position,2*self.profile.metres_per_tick_upper)

    def test_short_missing_sample_has_a_larger_position_bound(self):
        self.sample(1,100_000,0.,0)
        self.sample(3,140_000,.01,10)
        start, later = self.capture(100_000),self.capture(140_000)
        bound = relative_wheel_bounds(start,later)
        self.assertIsNotNone(bound)
        self.assertGreater(bound[0], .005)
        self.assertLess(bound[0], .05)
        self.assertGreater(later.wheel_capture.progress.position_gap_error_m,0.)

    def test_long_gap_changed_source_or_inconsistent_pose_breaks_relative_proof(self):
        self.sample(1,100_000,0.,0)
        self.sample(8,240_000,.01,10)
        self.sample(9,260_000,.02,20)
        self.sample(10,280_000,.02,30)
        self.sample(11,300_000,.03,40,source=('new-gateway','pi','wheel'))
        first,after_gap,normal,wrong_pose,new_source = (self.capture(at) for at in
                                                       (100_000,240_000,260_000,280_000,300_000))
        self.assertIsNone(relative_wheel_bounds(first,after_gap))
        self.assertIsNone(self.capture(110_000))
        self.assertIsNotNone(relative_wheel_bounds(after_gap,normal))
        self.assertIsNone(relative_wheel_bounds(normal,wrong_pose))
        self.assertIsNone(relative_wheel_bounds(wrong_pose,new_source))
