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
from xenova_vehicle.mapping.registration import (FrozenReference, PointFeature, PoseBox, RegistrationProfile,
    SegmentFeature, overlapping_points, register)
from xenova_vehicle.perception.calibration import synthetic_calibration
from xenova_vehicle.perception.inputs import FrameStamp
from xenova_vehicle.perception.pipeline import PerceptionPipeline
from xenova_vehicle.perception.registration_history import RegistrationObservation, combined_constraints
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
            points = overlapping_points(points,reference,self.prior,self.profile)
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
        shared=overlapping_points(points,reference,self.prior,self.profile)
        with self.assertRaises(ValueError):
            register(shared,reference,self.prior,self.profile,calibration_id=self.calibration.sha256)

    def test_points_past_observed_support_cannot_gain_a_normal_constraint(self):
        reference=self.reference(self.graph((self.strip(0),),self.a))
        graph=self.graph((self.strip(0,points=((1.4,0.),(2.4,0.))),),self.b)
        shared=overlapping_points(self.features(graph),reference,self.prior,self.profile)
        self.assertEqual(shared,())

    def test_unmatched_adjacent_lane_does_not_invalidate_supported_normal_points(self):
        old=self.graph((self.strip(0,y=0.),),self.a)
        reference=self.reference(old)
        reference=replace(reference,segments=tuple(replace(s,error_m=.09) for s in reference.segments))
        current=self.graph((replace(self.strip(0,y=0.),error_m=.035),
                            replace(self.strip(1,y=.285),error_m=.035)),self.b)
        prior=PoseBox((-.035,.035),(-.035,.035),(-.001,.001))
        shared=overlapping_points(self.features(current),reference,prior,self.profile)
        self.assertEqual({point.component for point in shared},{'strip:0'})
        fitted=register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256)
        self.assertEqual(fitted.status,'BOUNDED')
        self.assertTrue(fitted.permits((0.,0.,0.)))

    def test_large_uncertainty_does_not_match_parallel_lines_across_a_lane(self):
        reference = FrozenReference(1,self.calibration.sha256,(
            SegmentFeature((.2,0.),(2.5,0.),.17,'capture-A:edge','capture-A',.23,False),
        ),feature_policy='OBSERVED_STRIP_SUPPORT_V1')
        points = tuple(PointFeature((x,y),.035,component,'capture-B',normal_only=True)
                       for component,y in (('same',.04),('adjacent',.285)) for x in (1.2,1.5))
        prior = PoseBox((-.045,.045),(-.045,.045),(-.001,.001))
        self.assertEqual({p.component for p in overlapping_points(points,reference,prior,self.profile)},
                         {'same','adjacent'})
        shared = overlapping_points(points,reference,prior,self.profile,max_lateral_match_m=.285/2)
        self.assertEqual([p.component for p in shared],['same','same'])
        self.assertEqual({p.compatible for p in shared},{('capture-A:edge',)})
        fitted = register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256,
                          position_radius_m=.045)
        self.assertEqual(fitted.status,'BOUNDED')
        self.assertTrue(fitted.permits((0.,0.,0.)))

    def test_history_cannot_match_an_adjacent_lane_or_its_own_capture(self):
        current_pose = replace(self.pose_a, sample=replace(self.pose_a.sample,at_us=200_000,x=.10),
                               captured_us=(200_000,200_000),bracket_us=(200_000,200_000))
        earlier_pose = replace(self.pose_a,sample=replace(self.pose_a.sample,position_error_m=.30))
        current = RegistrationObservation('now',self.b.key[:-1],current_pose,
            tuple(PointFeature((x,0.),.001,'current','now',normal_only=True) for x in (.4,.7)),())
        earlier = RegistrationObservation('recent',self.a.key[:-1],earlier_pose,
            tuple(PointFeature((x,.285),.001,'previous','recent',normal_only=True) for x in (.5,.8)),())
        reference = FrozenReference(1,self.calibration.sha256,(
            SegmentFeature((-1.,0.),(2.,0.),.001,'older:center','older',finite_endpoints=False),
            SegmentFeature((-1.,.285),(2.,.285),.001,'recent:outer','recent',finite_endpoints=False)),
            feature_policy='OBSERVED_STRIP_SUPPORT_V1')
        prior = PoseBox((-.20,.40),(-.30,.30),(-.01,.01))
        def fit(lateral, age=500_000):
            return combined_constraints((earlier,),current,reference,prior,self.profile,
                position_radius_m=.30,max_lateral_match_m=lateral,max_age_us=age)

        accepted, older = fit(.1425)
        self.assertEqual(accepted,())
        self.assertEqual(tuple(s.capture_id for s in older.segments),('older',))
        loose,_ = fit(.4)
        self.assertEqual({point.capture_id for point in loose},{'now','recent'})
        self.assertEqual(fit(.4,99_999)[0],())

    def test_finite_strip_requires_both_ends_to_match_the_same_reference(self):
        reference=FrozenReference(1,self.calibration.sha256,(
            SegmentFeature((.60,0.),(.86,0.),.09,'capture-A:strip:0','capture-A'),),
            feature_policy='OBSERVED_STRIP_SUPPORT_V1')
        prior=PoseBox((-.035,.035),(-.035,.035),(0.,0.))
        ends={'strip:0':((.86,0.),(.61,0.),.04),
              'strip:1':((.995,.015),(1.13,.02),.04)}
        points=tuple(PointFeature(point,error,component,'capture-B')
            for component,(a,b,error) in ends.items() for point in (a,b))
        old=overlapping_points(points,reference,prior,self.profile)
        self.assertIn('strip:1',{point.component for point in old})
        shared=overlapping_points(points,reference,prior,self.profile,
            observed_ends=ends,position_radius_m=.035)
        self.assertEqual([point.component for point in shared],['strip:0','strip:0'])
        fitted=register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256,
            position_radius_m=.035)
        self.assertEqual(fitted.status,'BOUNDED')
        self.assertTrue(fitted.permits((0.,0.,0.)))
        with_ends=overlapping_points(points,reference,prior,self.profile,observed_ends=ends,
            position_radius_m=.035,include_endpoints=True)
        self.assertEqual(tuple((point.component,point.endpoint_matches) for point in with_ends
            if point.endpoint_matches),(('strip:0',(('capture-A:strip:0',1),)),
                                        ('strip:0',(('capture-A:strip:0',0),))))

    def test_common_straight_support_reduces_yaw_without_narrowing_along_line(self):
        original=self.strip(0,points=((.2,0.),(2.6,0.)))
        angle=.02
        current=replace(original,points_xy=tuple(
            (x*math.cos(angle),-x*math.sin(angle)) for x,y in original.points_xy))
        reference=self.reference(self.graph((original,),self.a))
        prior=PoseBox((-.1,.1),(-.02,.02),(-.04,.04))
        profile=replace(self.profile,residual_m=.001,cell_steps=(.01,.005,.0025),
            max_position_error_m=.2,max_yaw_error_rad=.04)
        shared=overlapping_points(self.features(self.graph((current,),self.b)),reference,prior,profile)
        fitted=register(shared,reference,prior,profile,calibration_id=self.calibration.sha256)
        self.assertTrue(fitted.complete)
        self.assertLess(fitted.yaw_error_rad,.02)
        self.assertTrue(self.contains(fitted,(-.1,0.,angle)))
        self.assertTrue(self.contains(fitted,(.1,0.,angle)))

    def test_circular_position_prior_keeps_its_radius_when_lines_add_no_constraint(self):
        reference=self.reference(self.graph((self.strip(0),),self.a))
        prior=PoseBox((-.04,.04),(-.04,.04),(-.001,.001))
        # 넓은 측정 오차는 원 prior의 모든 위치를 허용한다. 원을 사각형으로 만들며
        # 추가한 모서리 때문에 4cm가 sqrt(2)*4cm로 커져서는 안 된다.
        points=tuple(replace(p,error_m=.1,error_xy=None) for p in self.features(
            self.graph((self.strip(0),),self.b)))
        shared=overlapping_points(points,reference,prior,self.profile)
        box=register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256)
        disk=register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256,
            position_radius_m=.04)
        self.assertEqual(box.status,'AMBIGUOUS_OR_UNOBSERVABLE')
        self.assertEqual(disk.status,'BOUNDED')
        self.assertAlmostEqual(disk.position_error_m,.04,places=8)
        self.assertTrue(disk.permits((.04,0.,0.)))
        self.assertTrue(disk.permits((0.,-.04,0.)))
        self.assertEqual(disk.yaw_error_rad,.001)
        for radius in (-.01,float('nan'),True):
            with self.subTest(radius=radius),self.assertRaises(ValueError):
                register(shared,reference,prior,self.profile,calibration_id=self.calibration.sha256,
                    position_radius_m=radius)

    def test_square_corner_outside_original_disk_cannot_become_a_valid_match(self):
        reference=FrozenReference(1,'calibration',tuple(SegmentFeature((x+.035,.035),(x+.035,.035),
            .0001,str(i),'old') for i,x in enumerate((0.,1.))))
        points=tuple(PointFeature((x,0.),.0001,str(i),'new',(str(i),)) for i,x in enumerate((0.,1.)))
        prior=PoseBox((-.04,.04),(-.04,.04),(0.,0.))
        profile=replace(self.profile,cell_steps=(.001,.001,.01),residual_m=.0001)
        square=register(points,reference,prior,profile,calibration_id='calibration')
        disk=register(points,reference,prior,profile,calibration_id='calibration',position_radius_m=.04)
        self.assertEqual(square.status,'BOUNDED')
        self.assertEqual(disk.status,'INCONSISTENT')

    def test_finite_feature_noise_does_not_move_an_already_compatible_odometry_pose(self):
        reference=FrozenReference(1,'calibration',(
            SegmentFeature((.008,.008),(.008,.008),.001,'landmark','old'),))
        point=PointFeature((0.,0.),.001,'landmark','new')
        prior=PoseBox((-.02,.02),(-.02,.02),(0.,0.))
        result=register((point,),reference,prior,replace(self.profile,cell_steps=(.005,.005,.01)),
            calibration_id='calibration',position_radius_m=.02)
        self.assertEqual(result.status,'BOUNDED')
        self.assertEqual(result.representative,(0.,0.,0.))
        self.assertTrue(result.permits((.008,.008,0.)))

    def test_transverse_start_line_is_novel_but_incompatible_shared_points_are_rejected(self):
        finite = dict(start='observed_paint_end', end='observed_paint_end')
        reference = self.reference(self.graph((self.strip(0, **finite),), self.a))
        start_line = self.strip(1, points=((.7, -.12), (.7, .12)), **finite)
        shifted_lane = self.strip(2, points=((.3,.04),(1.3,-.04)), **finite)
        current = self.features(self.graph((start_line, shifted_lane), self.b))
        prior = PoseBox((0., 0.), (-.05, .05), (0., 0.))
        shared = overlapping_points(current, reference, prior, self.profile)
        self.assertEqual({point.component for point in shared}, {'strip:2'})
        fit = register(shared, reference, prior, self.profile, calibration_id=self.calibration.sha256)
        self.assertEqual(fit.status, 'INCONSISTENT')
