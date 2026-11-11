"""잘린 tape 끝을 물리 끝점으로 정합하는 허위 관측성 반례와 정책 회귀다.

literal graph는 의미 단위의 명시 관측이다. 실제 pixels 추출/실물 보정 수락이나
World·채택·LIVE 입력 시험이 아니다. SYNTHETIC 보정과 odom 표시는 그대로 둔다.
등록 profile의 residual/error/work cap은 기본값이며 실제 feature/reference/register를 쓴다.
"""

from dataclasses import asdict, replace
import itertools
import math
import unittest

from xenova_vehicle.mapping.odometry import PoseEstimate, PoseSample
from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.registration import PoseBox, RegistrationProfile, overlapping_points, register
from xenova_vehicle.perception.calibration import synthetic_calibration
from xenova_vehicle.perception.inputs import FrameStamp
from xenova_vehicle.perception.pipeline import PerceptionPipeline
from xenova_vehicle.perception.tape import ENDPOINT_KINDS, TapeGraph, TapeStrip


PHYSICAL = ('physical_junction', 'observed_paint_end')
CENSORED = ('FOV_clip', 'occluded', 'predicted')
TRUE_DX_M = .10


class RegistrationCensorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.calibration = synthetic_calibration()
        self.pipeline = PerceptionPipeline(self.calibration)
        self.profile = RegistrationProfile()
        self.prior = PoseBox((-.15, .15), (-.0001, .0001), (-.0001, .0001))
        self.a = FrameStamp('semantic-camera', 'rgb', 1, 'semantic-clock', (100_000, 100_000), 100_000)
        self.b = replace(self.a, sequence=2, captured_us=(200_000, 200_000), available_us=200_000)
        sample = PoseSample('semantic-odom', self.a.clock_id, self.calibration.sha256,
            self.a.captured_us[0], 0., 0., 0., 0., 0., origin='SYNTHETIC_ODOMETRY')
        self.pose_a = PoseEstimate(sample, self.a.captured_us, self.a.captured_us)
        self.pipeline._alignment=MapOdomAlignment.from_match(self.pose_a,self.pose_a)

    @staticmethod
    def strip(number, *, start='FOV_clip', end='FOV_clip', y=0., points=None, **kwargs):
        return TapeStrip(number, ((.3, y), (1.3, y)) if points is None else points,
            (.024, .026), .001, start, end, endpoint_error_m=(.001, .001), **kwargs)

    def graph(self, strips, stamp):
        return TapeGraph(tuple(strips), source_key=stamp.key, calibration_qualified=True)

    def reference(self, graph):
        return self.pipeline._reference_from(graph, 'capture-A', self.pose_a, 1)

    def features(self, graph):
        return self.pipeline._features(graph, 'capture-B')

    def result(self, reference_graph, current_graph):
        reference = self.reference(reference_graph)
        points = self.features(current_graph)
        if reference.feature_policy == 'OBSERVED_STRIP_SUPPORT_V1':
            points = overlapping_points(points,reference,self.prior,self.profile,lateral_m=.1425)
        # 기존 register는 empty points를 거절한다. 정보 부족을 새 성공/status로 포장하지 않는다.
        fitted = (register(points, reference, self.prior, self.profile,
            calibration_id=self.calibration.sha256) if points else None)
        return points, reference, fitted

    @staticmethod
    def contains(fitted, pose=(TRUE_DX_M, 0., 0.)):
        return fitted is not None and any(all(lo <= value <= hi for value, (lo, hi) in
            zip(pose, (box.x, box.y, box.yaw))) for box in fitted.feasible)

    @staticmethod
    def evidence(points, reference, fitted):
        return repr(dict(points=[asdict(point) for point in points],
            reference=asdict(reference), registration=None if fitted is None else asdict(fitted)))

    def assert_excluded(self, strip):
        old = self.graph((strip,), self.a)
        current = self.graph((strip,), self.b)
        points, reference, fitted = self.result(old, current)
        evidence = self.evidence(points, reference, fitted)
        self.assertEqual(points, (), evidence)
        self.assertEqual(reference.segments, (), evidence)

    def test_same_fov_window_cannot_falsely_bound_zero_and_exclude_true_ten_centimetres(self):
        """연속 무한 평행선은 전진 후에도 같은 local FOV 끝에서 잘린다."""
        strips = tuple(self.strip(number, y=y) for number, y in enumerate((-.3, 0., .3)))
        old, current = self.graph(strips, self.a), self.graph(strips, self.b)
        self.assertNotEqual(old.source_key, current.source_key)
        self.assertTrue(self.prior.x[0] <= 0. <= TRUE_DX_M <= self.prior.x[1])
        points, reference, fitted = self.result(old, current)
        evidence = 'true_dx=.10; default_profile=' + repr(asdict(self.profile)) + '; ' + self.evidence(points, reference, fitted)
        false_bound = fitted is not None and fitted.status == 'BOUNDED' and not self.contains(fitted)
        self.assertFalse(false_bound, evidence)
        self.assertTrue(points, evidence)
        self.assertEqual(reference.feature_policy,'OBSERVED_STRIP_SUPPORT_V1')
        self.assertTrue(self.contains(fitted,(-.15,0.,0.)),evidence)
        self.assertTrue(self.contains(fitted,(.15,0.,0.)),evidence)

    def test_observed_censored_ends_supply_normal_support_and_predicted_is_excluded(self):
        self.assertEqual(set(ENDPOINT_KINDS), set(PHYSICAL) | set(CENSORED))
        self.assert_excluded(self.strip(0,start='predicted',end='predicted'))
        for kind in ('FOV_clip','occluded'):
            with self.subTest(endpoint=kind):
                strip=self.strip(0,start=kind,end=kind)
                points,reference,fitted=self.result(self.graph((strip,),self.a),self.graph((strip,),self.b))
                self.assertEqual(reference.feature_policy,'OBSERVED_STRIP_SUPPORT_V1')
                self.assertTrue(points)
                self.assertTrue(self.contains(fitted))

    def test_any_predicted_end_is_excluded_in_both_metric_paths(self):
        for start, end in itertools.product(sorted(ENDPOINT_KINDS), repeat=2):
            if 'predicted' not in (start,end):
                continue
            with self.subTest(start=start, end=end):
                self.assert_excluded(self.strip(0, start=start, end=end))

    def test_closed_strip_has_no_metric_segment_endpoints_even_with_physical_labels(self):
        self.assert_excluded(self.strip(0, start='physical_junction', end='observed_paint_end',
            points=((.3, -.1), (1.3, -.1), (1.3, .1), (.3, -.1)), closed=True))

    def test_unobserved_strip_never_enters_current_features_or_frozen_reference(self):
        for closed in (False, True):
            with self.subTest(closed=closed):
                self.assert_excluded(self.strip(0, start='observed_paint_end', end='physical_junction',
                    observed=False, closed=closed))

    def test_real_finite_physical_ends_remain_and_bound_actual_translation(self):
        for start, end in itertools.product(PHYSICAL, repeat=2):
            with self.subTest(start=start, end=end):
                old_strips = tuple(self.strip(number, start=start, end=end, y=y)
                    for number, y in enumerate((-.3, 0., .3)))
                current_strips = tuple(replace(strip,
                    points_xy=tuple((x - TRUE_DX_M, y) for x, y in strip.points_xy)) for strip in old_strips)
                points, reference, fitted = self.result(self.graph(old_strips, self.a), self.graph(current_strips, self.b))
                evidence = self.evidence(points, reference, fitted)
                self.assertEqual(len(points), 6, evidence)
                self.assertEqual(len(reference.segments), 3, evidence)
                self.assertEqual(fitted.status, 'BOUNDED', evidence)
                self.assertTrue(fitted.complete, evidence)
                self.assertTrue(self.contains(fitted), evidence)

    def test_observed_corner_support_and_parallel_lines_preserve_true_translation(self):
        clipped = tuple(self.strip(number, y=y) for number, y in enumerate((-.3, 0., .3)))
        finite = (self.strip(3, start='physical_junction', end='observed_paint_end',
            points=((.2, .6), (.8, .6))),
            self.strip(4, start='observed_paint_end', end='physical_junction',
                points=((.2, .6), (.2, .9))))
        shifted = tuple(replace(strip, points_xy=tuple((x - TRUE_DX_M, y)
            for x, y in strip.points_xy)) for strip in finite)
        points, reference, fitted = self.result(self.graph(clipped + finite, self.a),
            self.graph(clipped + shifted, self.b))
        evidence = self.evidence(points, reference, fitted)
        self.assertTrue(points,evidence)
        self.assertEqual(len(reference.segments),5,evidence)
        self.assertIn(fitted.status, ('BOUNDED','AMBIGUOUS_OR_UNOBSERVABLE'), evidence)
        self.assertTrue(fitted.complete, evidence)
        self.assertTrue(self.contains(fitted), evidence)

    def test_current_capture_cannot_validate_its_own_support(self):
        graph=self.graph((self.strip(0),),self.a)
        reference=self.reference(graph)
        points=self.pipeline._features(graph,'capture-A')
        shared=overlapping_points(points,reference,self.prior,self.profile,lateral_m=.1425)
        with self.assertRaises(ValueError):
            register(shared,reference,self.prior,self.profile,calibration_id=self.calibration.sha256)

    def test_points_past_observed_support_cannot_gain_a_normal_constraint(self):
        reference=self.reference(self.graph((self.strip(0),),self.a))
        graph=self.graph((self.strip(0,points=((1.4,0.),(2.4,0.))),),self.b)
        shared=overlapping_points(self.features(graph),reference,self.prior,self.profile,lateral_m=.1425)
        self.assertEqual(shared,())

    def test_common_straight_support_reduces_yaw_without_narrowing_along_line(self):
        original=self.strip(0,points=((.2,0.),(2.6,0.)))
        angle=.02
        current=replace(original,points_xy=tuple(
            (x*math.cos(angle),-x*math.sin(angle)) for x,y in original.points_xy))
        reference=self.reference(self.graph((original,),self.a))
        prior=PoseBox((-.1,.1),(-.02,.02),(-.04,.04))
        profile=replace(self.profile,residual_m=.001,cell_steps=(.01,.005,.0025),
            max_position_error_m=.2,max_yaw_error_rad=.04)
        shared=overlapping_points(self.features(self.graph((current,),self.b)),reference,prior,profile,lateral_m=.1425)
        fitted=register(shared,reference,prior,profile,calibration_id=self.calibration.sha256)
        self.assertTrue(fitted.complete)
        self.assertLess(fitted.yaw_error_rad,.02)
        self.assertTrue(self.contains(fitted,(-.1,0.,angle)))
        self.assertTrue(self.contains(fitted,(.1,0.,angle)))

    def test_transverse_start_line_is_novel_but_an_overlapping_shifted_lane_is_compared(self):
        finite = dict(start='observed_paint_end', end='observed_paint_end')
        reference = self.reference(self.graph((self.strip(0, **finite),), self.a))
        start_line = self.strip(1, points=((.7, -.12), (.7, .12)), **finite)
        shifted_lane = self.strip(2, y=.08, **finite)
        current = self.features(self.graph((start_line, shifted_lane), self.b))
        prior = PoseBox((0., 0.), (0., 0.), (0., 0.))
        shared = overlapping_points(current, reference, prior, self.profile, lateral_m=.1425)
        self.assertEqual({point.component for point in shared}, {'strip:2'})
        fit = register(shared, reference, prior, self.profile, calibration_id=self.calibration.sha256)
        self.assertEqual(fit.status, 'INCONSISTENT')
