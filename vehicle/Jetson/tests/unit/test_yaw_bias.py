"""Independent straight-drive check for the corrected map projection."""

import math
import unittest

from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.registration import RegistrationProfile
from xenova_vehicle.mapping.wheel_motion import WheelCapture, WheelMotionProfile, WheelObservation, WheelProgress
from xenova_vehicle.perception.yaw_bias import YawBias, _RATE_WALK


class YawBiasProjectionTests(unittest.TestCase):
    def test_continuous_wheel_arc_recovers_straight_motion_within_bounds(self):
        actual_rate = .003
        speed = .2
        model = WheelMotionProfile(32,.000157,.00016,.18,.001,.001)
        bias = YawBias()
        poses = []
        for step in range(21):
            seconds = step/4
            yaw = actual_rate*seconds
            stamp = 1_000_000+step*250_000
            # The raw wheel solution is a circular arc; the vehicle drove straight.
            raw = PoseSample('odom','clock','calibration',stamp,
                speed*math.sin(yaw)/actual_rate,speed*(1-math.cos(yaw))/actual_rate,yaw,.001,.001)
            wheel = WheelObservation(('gateway',),step,stamp,(step,step),0)
            progress = WheelProgress(('continuous',),wheel,model,speed*seconds,2*speed*seconds,yaw)
            pose = PoseEstimate(raw,(stamp,stamp),(stamp,stamp),WheelCapture(progress,0.,0.))
            poses.append(pose)
            bias.add_pose(pose)
        start,end = poses[0],poses[-1]
        alignment = MapOdomAlignment.from_match(start,start,observed_j_interval=start.captured_us,
            registration_revision=1,valid_for_us=500_000,position_drift_mps=.001,yaw_drift_rps=.004)
        raw = alignment.apply(end)
        nominal = bias.project(alignment,end,RegistrationProfile()).sample
        self.assertFalse(bias.applied)
        self.assertAlmostEqual(nominal.yaw, raw.sample.yaw)
        self.assertLessEqual(math.dist((nominal.x,nominal.y),(1.,0.)),nominal.position_error_m)
        bias.interval = (.0028,.0032)
        bias.rate = .0031
        bias.variance = .0001**2
        bias.updates = 2
        bias.updated_us = end.sample.at_us
        corrected = bias.project(alignment,end,RegistrationProfile()).sample
        self.assertTrue(bias.applied)
        self.assertLess(abs(corrected.yaw),.002)
        self.assertLessEqual(abs(corrected.yaw),corrected.yaw_error_rad)
        self.assertLessEqual(math.dist((corrected.x,corrected.y),(1.,0.)),corrected.position_error_m)
        self.assertLess(corrected.position_error_m,raw.sample.position_error_m)

    def test_recent_rate_interval_bounds_a_changing_rate_over_an_old_wheel_path(self):
        model = WheelMotionProfile(32,1e-5,1e-5,.15,.0001,.0001)
        bias = YawBias()
        for step in range(301):
            stamp = 1_000_000+step*100_000
            raw = PoseSample('odom','clock','calibration',stamp,step*.02,0.,0.,0.,0.)
            wheel = WheelObservation(('wheel',),step+1,stamp,(step*2000,step*2000),0)
            progress = WheelProgress(('continuous',),wheel,model,step*.02,step*.04)
            bias.add_pose(PoseEstimate(raw,(stamp,stamp),(stamp,stamp),WheelCapture(progress,0.,0.)))
        start,end = bias.path[0],bias.path[-1]
        alignment = MapOdomAlignment.from_match(start,start,observed_j_interval=start.captured_us,
            registration_revision=1,valid_for_us=500_000,position_drift_mps=.001,yaw_drift_rps=.001)
        bias.interval = (.00095,.001)
        bias.rate = .001
        bias.variance = .00002**2
        bias.updates = 2
        bias.updated_us = end.sample.at_us

        # The rate climbs at the allowed walk and reaches .001 rad/s at the last
        # observation. Integrate the physical heading independently of the filter.
        def true_yaw(seconds):
            return -((.001-30*_RATE_WALK)*seconds + .5*_RATE_WALK*seconds**2)

        actual_x = .002*math.fsum(math.cos(true_yaw((i+.5)*.01)) for i in range(3000))
        actual_y = .002*math.fsum(math.sin(true_yaw((i+.5)*.01)) for i in range(3000))
        corrected = bias.project(alignment,end,RegistrationProfile()).sample
        self.assertTrue(bias.applied)
        self.assertLessEqual(abs(corrected.yaw-true_yaw(30)),corrected.yaw_error_rad)
        self.assertLessEqual(math.dist((corrected.x,corrected.y),(actual_x,actual_y)),
                             corrected.position_error_m)

    def test_return_to_origin_keeps_position_error_from_heading_drift_without_filter(self):
        seconds, speed, rate = 30., .26, .001
        angular_speed = math.tau/seconds
        radius = speed/angular_speed
        model = WheelMotionProfile(32,1e-5,1e-5,.18,.0001,.0001)
        bias = YawBias()
        for step in range(301):
            t = step/10
            at = 1_000_000+step*100_000
            yaw = angular_speed*t
            raw = PoseSample('odom','clock','calibration',at,radius*math.sin(yaw),
                radius*(1-math.cos(yaw)),math.remainder(yaw,math.tau),.0001,.0001)
            wheel = WheelObservation(('gateway',),step,at,(step,step),0)
            progress = WheelProgress(('continuous',),wheel,model,speed*t,2*speed*t,yaw)
            bias.add_pose(PoseEstimate(raw,(at,at),(at,at),WheelCapture(progress,0.,0.)))
        start,end = bias.path[0],bias.path[-1]
        alignment = MapOdomAlignment.from_match(start,start,observed_j_interval=start.captured_us,
            registration_revision=1,valid_for_us=500_000,position_drift_mps=0.,yaw_drift_rps=rate)
        nominal = bias.project(alignment,end,RegistrationProfile()).sample
        self.assertFalse(bias.applied)
        actual_error = 2*speed/(angular_speed+rate)*math.sin(rate*seconds/2)
        self.assertLessEqual(actual_error,nominal.position_error_m)
        # Evict the original pose history while retaining the continuous encoder prefix.
        bias.path.popleft()
        prefix_only = bias.project(alignment,end,RegistrationProfile()).sample
        self.assertLessEqual(actual_error,prefix_only.position_error_m)
        self.assertLess(nominal.position_error_m,prefix_only.position_error_m)
