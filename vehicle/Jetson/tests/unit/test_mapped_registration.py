"""Revisit matching uses original map observations and preserves ambiguity."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.registration import FrozenReference, RegistrationProfile, RegistrationResult, SegmentFeature
from xenova_vehicle.perception.calibration import CalibrationProfile, synthetic_calibration
from xenova_vehicle.perception.inputs import FrameStamp, ImageFrame, PerceptionInput
from xenova_vehicle.perception.mapped_registration import MappedRegistration
from xenova_vehicle.perception.pipeline import PerceptionPipeline, PipelineProfile
from xenova_vehicle.perception.tape import TapeGraph, TapeStrip
from xenova_vehicle.perception.registration_history import RegistrationObservation
from xenova_vehicle.perception.visual_registration import (
    RegistrationDecision, VisualRegistrationPolicy, _SearchBudget, select_registration,
)


class MappedRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.archive = MappedRegistration()
        self.reference = FrozenReference(5, 'calibration', ())
        self.profile = RegistrationProfile(
            cell_steps=(.01, .01, .005), residual_m=.002,
            max_position_error_m=.05, max_yaw_error_rad=.04)

    def graph(self, lateral=0., *, predicted=False):
        end = 'predicted' if predicted else 'observed_paint_end'
        strips = tuple(TapeStrip(i, (a, b), (.01, .02), .001, end, end) for i, (a, b) in enumerate((
            ((.4, .05 + lateral), (.45, .05 + lateral)),
            ((.65, -.07 + lateral), (.65, -.12 + lateral)),
        )))
        return TapeGraph(strips, calibration_qualified=True)

    def pose(self, at, position_error=.001):
        sample = PoseSample('odom', 'clock', 'calibration', at, 0., 0., 0., position_error, .01,
                            origin='SYNTHETIC_ODOMETRY')
        return PoseEstimate(sample, (at, at), (at, at))

    def fit(self, graph, at, *, identity='current'):
        pose = self.pose(at, .06)
        observation = self.archive.observe(graph, identity, pose, ('source',))
        return self.archive.match(observation, pose, self.reference, self.profile,
                                  lane_width_m=.285,solve=_SearchBudget(self.profile).solve)

    def test_returns_to_old_short_paint_without_replacing_recent_reference(self):
        self.archive.add(self.graph(), 'old', self.pose(100_000))
        result, _, local_reference = self.fit(self.graph(), 5_000_000)
        self.assertLess(result.position_error_m, self.profile.max_position_error_m)
        self.assertTrue(result.permits((0., 0., 0.)))
        self.assertEqual({segment.capture_id for segment in local_reference.segments}, {'old'})
        self.assertEqual(self.reference.segments, ())

    def test_revisit_search_spends_only_remaining_frame_budget(self):
        pose = self.pose(5_000_000, .06)
        current = RegistrationObservation('current', ('source',), pose, (), ())
        profile = replace(self.profile,max_cells=12,max_distance_tests=30)
        usage = []

        def solve(points, reference, prior, limits, **_):
            usage.append((limits.max_cells, limits.max_distance_tests))
            if len(usage) == 1:
                return RegistrationResult('AMBIGUOUS_OR_UNOBSERVABLE',None,None,None,(prior,),
                                          reference.revision,7,19,True)
            return RegistrationResult('UNKNOWN_BUDGET',None,None,None,(prior,),reference.revision,5,11,False)

        def revisit(solve, reference):
            solve(('test',),reference,pose.prior,position_radius_m=.06,max_cells=1024)
            return None

        points = self.archive.observe(self.graph(),'current',pose,('source',))[0].points
        with (patch('xenova_vehicle.perception.visual_registration.overlapping_points',return_value=points),
              patch('xenova_vehicle.perception.visual_registration.register',side_effect=solve)):
            decision = select_registration(current,self.reference,pose,pose,profile,VisualRegistrationPolicy(),
                                           history=(),history_age_us=0,lane_width_m=.285,revisit=revisit)
        self.assertEqual(usage,[(12,30),(5,11)])
        self.assertEqual((decision.result.tested_cells,decision.result.distance_tests),(12,30))

    def test_single_frame_correction_uses_admission_limits(self):
        pose = self.pose(5_000_000)
        fit = RegistrationResult('BOUNDED',(0.,0.,.02),.001,.005,(pose.prior,),self.reference.revision,1,1,True)
        decision = RegistrationDecision(fit,'NORMAL',(),self.reference)
        coarser = replace(self.profile,cell_steps=(.03,.03,.03),residual_m=.04)
        self.assertTrue(decision.conflicts_with(pose,coarser,VisualRegistrationPolicy()))
        self.assertFalse(decision.conflicts_with(pose,coarser,VisualRegistrationPolicy(single_frame_turn_rad=.025)))

    def test_rejects_adjacent_lane_and_recent_or_predicted_landmarks(self):
        graph = self.graph()
        self.archive.add(graph, 'old', self.pose(100_000))
        self.assertIsNone(self.fit(graph, 1_000_000))
        self.assertIsNone(self.fit(self.graph(.285), 5_000_000))
        self.assertIsNone(self.fit(graph, 5_800_000, identity='old'))
        self.archive.clear()
        self.archive.add(self.graph(predicted=True), 'old', self.pose(100_000))
        self.assertIsNone(self.fit(graph, 5_000_000))

    def test_pipeline_renews_odom_anchor_after_reobserving_old_map(self):
        fields = synthetic_calibration().to_dict()
        fields.update(kind='LIVE', vehicle_bundle_sha256='b' * 64)
        calibration = CalibrationProfile.from_dict(fields)
        pipeline = PerceptionPipeline(calibration, PipelineProfile(registration=self.profile))
        long = TapeStrip(2, ((.2, .2), (.7, .2)), (.01, .02), .001,
                         'observed_paint_end', 'observed_paint_end')
        initial = replace(self.graph(), strips=(*self.graph().strips, long))

        def process(at, graph, error):
            stamp = FrameStamp('camera', 'rgb', at, 'clock', (at, at), at)
            frame = ImageFrame(stamp, 640, 360, 'BGR8', bytes(640 * 360 * 3))
            raw = self.pose(at, error)
            pose = replace(raw, sample=replace(raw.sample, calibration_id=calibration.sha256))
            with patch('xenova_vehicle.perception.pipeline.extract_tape', return_value=graph):
                return pipeline.process(PerceptionInput('LIVE', calibration.sha256, frame), pose=pose,
                    localization_valid_for_us=250_000, localization_position_drift_mps=0.,
                    localization_yaw_drift_rps=0.)

        process(100_000, initial, .001)
        seeded = process(200_000, initial, .001)
        self.assertTrue(seeded.summary['localization_qualified'])
        # 모델의 최근 조각을 다른 구간에서 본 원 관측으로 대체한다.
        recent = SegmentFeature((3., 3.), (3.4, 3.), .01, 'recent:strip:0', 'recent')
        pipeline.reference = replace(pipeline.reference, segments=(recent,))
        result = process(5_000_000, self.graph(), .06)
        self.assertEqual(result.summary['registration_method'], 'MAPPED_REVISIT')
        self.assertTrue(result.summary['localization_qualified'])
        self.assertLess(result.summary['mapped_position_error_m'], self.profile.max_position_error_m)
        self.assertEqual(pipeline._propagation_anchor.observed_j_interval, (5_000_000, 5_000_000))
