"""왜곡 계수가 있는 RGB 카메라의 편 영상 view 와, 그 view 를 쓰는 흰선 추출·NN 후보 투영을 검사한다.

영상은 왜곡 모델로 직접 그린 합성 바닥이다. 실제 카메라·모델·파일을 쓰지 않으며 NumPy/OpenCV 가 없으면
skip 한다. 왜곡 계수는 OAK-D-Lite RGB 한 대의 공장 값이고 실물 장면의 수락을 뜻하지 않는다.
"""

import math
import unittest

from xenova_vehicle.oak.yolop_frames import NNPacket, RuntimeTensor
from xenova_vehicle.perception.calibration import CameraModel, CalibrationProfile, Plane
from xenova_vehicle.perception.geometry import ground_to_pixels
from xenova_vehicle.perception.inputs import FrameStamp, ImageFrame, PerceptionInput
from xenova_vehicle.perception.neural import build_neural_layer
from xenova_vehicle.perception.rectify import POINT_RESIDUAL_PX, rgb_view
from xenova_vehicle.perception.tape import extract_tape
from xenova_vehicle.perception.yolop import decode_yolop

K = ((341.55971976740733, 0., 311.7750641565359), (0., 341.39208984375, 185.53857891376202), (0., 0., 1.))
DISTORTION = (-2.740800142288208, 4.606104850769043, 0.0004328511713538319, 0.00043487545917741954,
              3.0954771041870117, -2.8647782802581787, 5.032287120819092, 2.553771734237671,
              0., 0., 0., 0., 0.008, -0.0027)
ROTATION = ((0., 0., 1.), (-1., 0., 0.), (0., -1., 0.))  # 수평으로 앞을 보는 카메라
TRANSLATION = (.294, 0., .141)
LINES_Y = (.15, -.14)
LINE_WIDTH_M = .02


def camera(distortion=DISTORTION, **changes):
    values = dict(roi=(77., 0., 563., 360.), distortion=distortion, pixel_error=.5, rotation_error_rad=.002,
                  translation_error_m=.001, near_m=.15, far_m=3., qualified=True, remap_qualified=True)
    values.update(changes)
    return CameraModel(640, 360, K, ROTATION, TRANSLATION, **values)


def floor_image(np, cv2, source):
    """왜곡이 있는 source 카메라가 본 바닥. 어두운 바닥에 x 방향의 흰 선 두 줄이 있다."""
    u, v = np.meshgrid(np.arange(640, dtype=np.float64), np.arange(360, dtype=np.float64))
    pixels = np.stack((u.ravel(), v.ravel()), axis=-1).reshape(-1, 1, 2)
    normalized = cv2.undistortPoints(pixels, np.asarray(source.k), np.asarray(source.distortion)).reshape(-1, 2)
    rays = np.concatenate((normalized, np.ones((len(normalized), 1))), axis=1) @ np.asarray(source.rotation).T
    with np.errstate(divide='ignore', invalid='ignore'):
        scale = -source.translation[2] / rays[:, 2]
    x, y = source.translation[0] + scale * rays[:, 0], source.translation[1] + scale * rays[:, 1]
    floor = (rays[:, 2] < 0) & (scale > 0)
    white = floor & (x > .3) & (x < 1.6) & np.any([np.abs(y - line) <= LINE_WIDTH_M / 2 for line in LINES_Y], axis=0)
    image = np.full((360 * 640, 3), 20, dtype=np.uint8)
    image[floor] = 60
    image[white] = 255
    return image.reshape(360, 640, 3)


class RgbRectifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import cv2
            import numpy as np
        except ImportError as error:
            raise unittest.SkipTest('NumPy/OpenCV 가 있어야 편 영상 시험을 수행함') from error
        cls.np, cls.cv2 = np, cv2
        cls.source = camera()
        cls.view = rgb_view(cls.source)
        cls.plane = Plane(qualified=True)
        cls.image = floor_image(np, cv2, cls.source)

    def test_camera_without_distortion_needs_no_view(self):
        self.assertIsNone(rgb_view(camera(distortion=())))
        self.assertIsNone(rgb_view(camera(distortion=(0.,) * 14)))

    def test_view_camera_keeps_calibration_and_only_drops_distortion(self):
        rectified = self.view.camera
        self.assertFalse(any(rectified.distortion))
        for name in ('width', 'height', 'k', 'rotation', 'translation', 'rectification', 'pixel_error',
                     'ray_error_rad', 'rotation_error_rad', 'translation_error_m', 'qualified', 'near_m', 'far_m'):
            self.assertEqual(getattr(rectified, name), getattr(self.source, name), name)
        x0, y0, x1, y1 = rectified.roi
        self.assertTrue(0 <= x0 < x1 <= 640 and 0 <= y0 < y1 <= 360)
        self.assertGreater((x1 - x0) * (y1 - y0), .8 * 486 * 360)
        # ROI 안의 편 픽셀은 모두 원 ROI 안에서 온다
        u = self.view.map_u[int(y0):int(y1), int(x0):int(x1)]
        v = self.view.map_v[int(y0):int(y1), int(x0):int(x1)]
        sx0, sy0, sx1, sy1 = self.source.roi
        self.assertTrue(bool(((u >= sx0) & (u <= sx1 - 1) & (v >= sy0) & (v <= sy1 - 1)).all()))
        self.assertFalse(self.view.map_u.flags.writeable)

    def test_rectified_pixels_follow_the_undistorted_pinhole(self):
        np = self.np
        x, y = np.meshgrid(np.linspace(.45, 1.5, 12), np.linspace(-.3, .3, 9))
        du, dv, valid = ground_to_pixels(x, y, self.source, self.plane)
        ru, rv, rectified_valid = ground_to_pixels(x, y, self.view.camera, self.plane)
        both = valid & rectified_valid
        self.assertGreater(int(both.sum()), 40)
        moved_u, moved_v = self.view.points(du, dv)
        self.assertLess(float(np.nanmax(np.hypot(moved_u - ru, moved_v - rv)[both])), .05)
        # 편 영상에서 그 자리의 밝기는 원 영상의 왜곡된 자리에서 온다
        self.assertGreater(float(np.hypot(du - ru, dv - rv)[both].max()), 2.)
        rectified = self.view.image(self.image)
        on_line_u, on_line_v, on_line = ground_to_pixels(np.linspace(.5, 1.2, 15), np.full(15, LINES_Y[0]),
                                                         self.view.camera, self.plane)
        values = [int(rectified[int(round(b)), int(round(a))].max()) for a, b, ok in zip(on_line_u, on_line_v, on_line) if ok]
        self.assertGreater(len(values), 10)
        self.assertTrue(all(value >= 200 for value in values), values)

    def test_points_outside_the_model_are_not_returned(self):
        np = self.np
        u, v = self.view.points(np.array([320., np.nan, 320.]), np.array([250., 10., np.inf]))
        self.assertTrue(np.isfinite(u[0]) and np.isfinite(v[0]))
        self.assertTrue(np.isnan(u[1]) and np.isnan(u[2]))
        self.assertLessEqual(POINT_RESIDUAL_PX, .05)
        with self.assertRaises(ValueError):
            self.view.points(np.zeros(2), np.zeros(3))
        with self.assertRaises(ValueError):
            self.view.image(np.zeros((360, 640), dtype=np.uint8)[:100])

    def test_tape_is_rejected_on_the_distorted_camera_and_extracted_through_the_view(self):
        distorted = extract_tape(self.image, self.source, self.plane)
        self.assertEqual(len(distorted.strips), 0)
        self.assertIn('UNBOUNDED_PROJECTION', distorted.reasons)
        self.assertEqual(distorted.work['projection_unbounded'], distorted.work['projection_checks'])
        graph = extract_tape(self.view.image(self.image), self.view.camera, self.plane)
        self.assertEqual(graph.work.get('projection_unbounded', 0), 0)
        self.assertNotIn('UNBOUNDED_PROJECTION', graph.reasons)
        self.assertGreaterEqual(len(graph.strips), 2)
        seen = set()
        for strip in graph.strips:
            lateral = [point[1] for point in strip.points_xy]
            nearest = min(LINES_Y, key=lambda line: abs(line - lateral[0]))
            self.assertLess(max(abs(value - nearest) for value in lateral), .015, strip.points_xy)
            seen.add(nearest)
        self.assertEqual(seen, set(LINES_Y))

    def observation(self, calibration, mask):
        np = self.np
        values = np.zeros(894800 // 2, dtype='<f2')
        for label, begin in ((mask, 37800), (None, 242600)):
            head = values[begin:begin + 204800].reshape(1, 2, 320, 320)
            head[:, 0] = 1
            if label is not None:
                head[0, 0, 70:250][label] = 0
                head[0, 1, 70:250][label] = 1
        tensors = (
            RuntimeTensor('det_out', 0, 801, 3, (1, 6300, 6), (75600, 12, 2), 0),
            RuntimeTensor('drive_area_seg', 0, 17185, 4, (1, 2, 320, 320), (409600, 204800, 640, 2), 75600),
            RuntimeTensor('lane_line_seg', 0, 17185, 4, (1, 2, 320, 320), (409600, 204800, 640, 2), 485200))
        packet = NNPacket('nn_output', 7, 1000, 1001, 2000, values.tobytes(), tensors, 'synthetic-rectified-candidate')
        stamp = FrameStamp('synthetic', 'rgb', 7, 'rgb-clock', (100, 120), 1300)
        frame = ImageFrame(stamp, 640, 360, 'BGR8', bytes(640 * 360 * 3))
        return PerceptionInput('SYNTHETIC', calibration.sha256, frame, nn=decode_yolop(packet),
                               nn_key=('synthetic', 'nn_output', 'opaque-nn-clock', 7))

    def test_neural_candidates_project_through_the_view_and_keep_source_pixels(self):
        np = self.np
        calibration = CalibrationProfile(self.source, self.source, self.plane,
            ((-10., -10.), (10., -10.), (10., 10.), (-10., 10.)), kind='SYNTHETIC',
            first_surface_qualified=True, time_qualified=True, minimum_target_qualified=True)
        mask = np.zeros((180, 320), dtype=np.bool_)
        mask[140, 150] = True  # 원 영상 (300.5, 280.5): 차 앞 바닥
        observation = self.observation(calibration, mask)
        without = build_neural_layer(observation, calibration)
        self.assertEqual((len(without.road_points), len(without.unprojected)), (0, 1))
        layer = build_neural_layer(observation, calibration, rgb_view=self.view)
        self.assertEqual((len(layer.road_points), len(layer.unprojected)), (1, 0))
        point = layer.road_points[0]
        self.assertEqual(point.source_pixel_xy, (300.5, 280.5))
        self.assertEqual(layer.calibration_id, calibration.sha256)
        # 독립 계산: 원 픽셀의 광선(역왜곡)과 바닥의 교점
        ray = self.cv2.undistortPoints(np.array([[[300.5, 280.5]]]), np.asarray(K), np.asarray(DISTORTION)).reshape(2)
        distance = TRANSLATION[2] / ray[1]
        nominal = (TRANSLATION[0] + distance, -ray[0] * distance, 0.)
        self.assertTrue(all(a <= value <= b for a, value, b in zip(point.bounds.lower, nominal, point.bounds.upper)),
                        (point.bounds, nominal))
        self.assertLess(math.dist(point.bounds.lower[:2], point.bounds.upper[:2]), .2)
        other = rgb_view(camera(translation_error_m=.002))
        with self.assertRaises(ValueError):
            build_neural_layer(observation, calibration, rgb_view=other)


if __name__ == '__main__':
    unittest.main()
