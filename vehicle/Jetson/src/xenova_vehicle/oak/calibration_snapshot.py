"""이미 A가 읽은 CalibrationHandler의 factory 사실만 추출한다. Device를 열지 않는다.

resize K와 EEPROM R은 active rectification의 readback이 아니다. 실제 K_rect/ROI,
mount/plane/focus/오차 수락을 이 snapshot의 해시로 대신하지 않는다.
"""

import hashlib
import json


FULL_SENSOR_WH = (4208, 3120)
FULL_RGB_ROI = (77, 0, 563, 360)


def full_sensor_rgb_geometry(default_intrinsics):
    """IMX214 EEPROM grid를 전체 센서와 640×360 letterbox에 맞춘 후보 K로 옮긴다."""
    k, width, height = default_intrinsics
    if (width, height) == (1920, 1080):
        # 4K 중앙 crop의 2×2 binning으로 저장된 EEPROM도 처리한다.
        k = [[2*row[0], 2*row[1], 2*row[2]+.5] for row in k[:2]] + [k[2]]
        width, height = 3840, 2160
    if (width, height) not in ((3840, 2160), FULL_SENSOR_WH):
        raise ValueError('unknown IMX214 EEPROM grid for full-sensor projection')
    left, top, right, bottom = FULL_RGB_ROI
    sx, sy = (right-left)/FULL_SENSOR_WH[0], (bottom-top)/FULL_SENSOR_WH[1]
    cx = k[0][2] + (FULL_SENSOR_WH[0]-width)/2
    cy = k[1][2] + (FULL_SENSOR_WH[1]-height)/2
    active = [[sx*k[0][0], sx*k[0][1], left + sx*(cx+.5)-.5],
              [0., sy*k[1][1], top + sy*(cy+.5)-.5], [0., 0., 1.]]
    return dict(k=active, roi=FULL_RGB_ROI, sensor_wh=FULL_SENSOR_WH,
                status='EEPROM_CENTER_CROP_TRANSFORM_NOT_ACTIVE_READBACK')


def _copy(value, cap=262144):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                     separators=(',', ':')).encode('ascii')
    if len(raw) > cap:
        raise ValueError('factory calibration snapshot exceeds cap')
    return json.loads(raw)


def snapshot_factory(calibration, dai, *, eeprom_raw=None, device_id=None, graph_profile=None,
                     sensors=(), rgb_sensor_mode='1080p'):
    """고정 v2.29 caller가 제공한 handler에서 읽는다. 원 raw EEPROM은 caller가 읽어 넘긴다."""
    if getattr(dai, '__version__', None) != '2.29.0.0':
        raise ValueError('factory extractor requires fixed DepthAI2.29.0.0')
    if eeprom_raw is not None and (type(eeprom_raw) is not bytes or not 1 <= len(eeprom_raw) <= 65536):
        raise ValueError('invalid original EEPROM bytes')
    if device_id is not None and (type(device_id) is not str or not 1 <= len(device_id) <= 128):
        raise ValueError('invalid original device ID')
    errors = {}
    def read(name, getter):
        try:
            return _copy(getter())
        except Exception as error:
            errors[name] = type(error).__name__
            return None
    eeprom = read('eeprom', calibration.eepromToJson)
    cameras = {}
    for name, socket, size in (('rgb', dai.CameraBoardSocket.CAM_A, (640, 360)),
                                ('left', dai.CameraBoardSocket.CAM_B, (640, 480)),
                                ('right', dai.CameraBoardSocket.CAM_C, (640, 480))):
        default_k = read(name + '.default_k', lambda s=socket: calibration.getDefaultIntrinsics(s))
        camera = dict(socket=str(socket), default_intrinsics=default_k,
            distortion_model=read(name + '.distortion_model', lambda s=socket: str(calibration.getDistortionModel(s))),
            distortion_coefficients=read(name + '.distortion', lambda s=socket: calibration.getDistortionCoefficients(s)),
            resize_candidate_wh=list(size))
        if name == 'rgb' and rgb_sensor_mode == 'full':
            geometry = read('rgb.full_geometry', lambda: full_sensor_rgb_geometry(default_k))
            camera.update(full_sensor_geometry=geometry,
                resize_candidate=None if geometry is None else geometry['k'],
                resize_status='FULL_SENSOR_GEOMETRY_UNAVAILABLE' if geometry is None else geometry['status'])
        else:
            camera.update(resize_status='EEPROM_RESIZE_NOT_ACTIVE_READBACK', resize_candidate=read(
                name + '.resize_k', lambda s=socket, z=size: calibration.getCameraIntrinsics(s, z[0], z[1])))
        cameras[name] = camera
    result = dict(schema_version=1, scope='OAK_FACTORY_CALIBRATION_NOT_METRIC_ADMISSION',
        device_id=device_id, depthai_version=dai.__version__, eeprom=eeprom,
        eeprom_json_sha256=None if eeprom is None else hashlib.sha256(json.dumps(eeprom,
            sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest(),
        eeprom_raw_sha256=None if eeprom_raw is None else hashlib.sha256(eeprom_raw).hexdigest(),
        eeprom_raw_hex=None if eeprom_raw is None else eeprom_raw.hex(), cameras=cameras,
        stereo_left_socket=read('stereo_left_socket', lambda: str(calibration.getStereoLeftCameraId())),
        stereo_right_socket=read('stereo_right_socket', lambda: str(calibration.getStereoRightCameraId())),
        stored_left_rectification_rotation=read('left_r', lambda: calibration.getStereoLeftRectificationRotation()),
        stored_right_rectification_rotation=read('right_r', lambda: calibration.getStereoRightRectificationRotation()),
        calibrated_baseline_cm=read('baseline', lambda: calibration.getBaselineDistance(
            dai.CameraBoardSocket.CAM_C, dai.CameraBoardSocket.CAM_B, False)),
        calibrated_right_to_rgb_extrinsics_cm=read('right_to_rgb', lambda: calibration.getCameraExtrinsics(
            dai.CameraBoardSocket.CAM_C, dai.CameraBoardSocket.CAM_A, False)),
        factory_rgb_lens_position=read('lens_position', lambda: calibration.getLensPosition(dai.CameraBoardSocket.CAM_A)),
        active=dict(k_rect=None, r_rect=None, right_left_support_roi=None,
                    focus_status='NOT_VERIFIED', runtime_config_status='NOT_VERIFIED'),
        graph_request=_copy(graph_profile), sensor_inventory=_copy(sensors),
        getter_errors=errors, metric_qualified=False, live_authority=False)
    return _copy(result)


factory_snapshot = snapshot_factory
