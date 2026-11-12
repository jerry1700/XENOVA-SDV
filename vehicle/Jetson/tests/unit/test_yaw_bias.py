"""Independent straight-drive check for the corrected map projection."""

import math
import unittest

from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.registration import RegistrationProfile
from xenova_vehicle.mapping.wheel_motion import WheelCapture, WheelMotionProfile, WheelObservation, WheelProgress
from xenova_vehicle.perception.yaw_bias import YawBias


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
        self.assertEqual(bias.project(alignment,end,RegistrationProfile()),raw)
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
