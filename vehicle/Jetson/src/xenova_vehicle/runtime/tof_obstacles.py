"""단일 존 ToF의 근거리 HIT를 발행용 World에만 덧씌운다.

저장 지도·FREE 증거·주차칸·출력 권한을 만들지 않는다. 센서 원 시각의 odom을
사용하며 같은 관측을 다시 발행해 수명을 늘리지 않는다. 만료된 HIT 영역은 새
카메라 관측이 확인될 때까지 UNKNOWN이다. owner는 M-compute 하나다.
"""

from dataclasses import replace
import math

from ..mapping.odometry import PoseBuffer
from ..mapping.registration import transform_point
from ..perception.tof import tof_evidence


class TofObstacleOverlay:
    """현재 발행 창(최대64×64)의 HIT 시각과 원 만료만 보관한다. I/O·thread는 없다."""

    def __init__(self):
        self._context = self._spec = None
        self._low = self._high = self._expires = None
        self._sensor = None
        self._seen = {}; self._resets = {}

    def _window(self, snapshot):
        import numpy as np
        spec = snapshot['spec']
        context = tuple(snapshot[key] for key in ('frame_id', 'clock_id', 'calibration_id'))
        if context == self._context and spec == self._spec:
            return
        shape = spec.height, spec.width
        values = [np.zeros(shape, dtype=np.uint64) for _ in range(4)]
        if self._context == context and self._spec.resolution_m == spec.resolution_m:
            old = self._spec
            dx = (spec.origin_xy[0]-old.origin_xy[0])/spec.resolution_m
            dy = (spec.origin_xy[1]-old.origin_xy[1])/spec.resolution_m
            # 같은 lattice의 창 이동만 보존한다. 다른 grid를 근사 대응시키지 않는다.
            if abs(dx-round(dx)) < 1e-7 and abs(dy-round(dy)) < 1e-7:
                dx, dy = round(dx), round(dy)
                x0, x1 = max(0, -dx), min(spec.width, old.width-dx)
                y0, y1 = max(0, -dy), min(spec.height, old.height-dy)
                if x0 < x1 and y0 < y1:
                    for new, previous in zip(values, (self._low, self._high, self._expires, self._sensor)):
                        new[y0:y1, x0:x1] = previous[y0+dy:y1+dy, x0+dx:x1+dx]
        else:
            self._resets.clear()
        self._seen.clear()
        self._context, self._spec = context, spec
        self._low, self._high, self._expires, self._sensor = values

    @staticmethod
    def _map_pose(alignment, pose):
        """같은 map←odom으로 과거 ToF도 변환한다. 시각 차이만큼 drift 오차를 넓힌다."""
        fixed = replace(alignment, observed_j_interval=None, registration_revision=None,
                        valid_for_us=None, position_drift_mps=None, yaw_drift_rps=None)
        mapped = fixed.apply(pose)
        if not alignment.has_metadata:
            return mapped
        lo, hi = pose.captured_us
        a, b = alignment.observed_j_interval
        seconds = max(abs(lo-b), abs(hi-a))/1_000_000
        sample = replace(mapped.sample,
            position_error_m=math.nextafter(mapped.sample.position_error_m+seconds*alignment.position_drift_mps, math.inf),
            yaw_error_rad=min(math.pi, math.nextafter(mapped.sample.yaw_error_rad+seconds*alignment.yaw_drift_rps, math.inf)))
        return replace(mapped, sample=sample)

    def _record(self, result, pose, evidence):
        spec = self._spec
        p = pose.sample
        for box in result.hit_boxes:
            if not box.qualified:
                continue
            points = [transform_point((x, y), (p.x, p.y, p.yaw))
                      for x in (box.lower[0], box.upper[0]) for y in (box.lower[1], box.upper[1])]
            lower = [math.nextafter(min(v[k] for v in points), -math.inf) for k in (0, 1)]
            upper = [math.nextafter(max(v[k] for v in points), math.inf) for k in (0, 1)]
            c0 = max(0, math.floor((lower[0]-spec.origin_xy[0])/spec.resolution_m))
            c1 = min(spec.width, math.floor((upper[0]-spec.origin_xy[0])/spec.resolution_m)+1)
            r0 = max(0, math.floor((lower[1]-spec.origin_xy[1])/spec.resolution_m))
            r1 = min(spec.height, math.floor((upper[1]-spec.origin_xy[1])/spec.resolution_m)+1)
            if c0 >= c1 or r0 >= r1:
                continue
            part = (slice(r0, r1), slice(c0, c1))
            newer = evidence.j_interval[0] >= self._low[part]
            for target, value in ((self._low, evidence.j_interval[0]),
                                  (self._high, evidence.j_interval[1]),
                                  (self._expires, evidence.valid_until_j_us),
                                  (self._sensor, evidence.sensor_id+1)):
                target[part][newer] = value

    def apply(self, snapshot, *, sensor_snapshot, alignment, snapshot_us, now_us, static_retained=None):
        """불변 입력에서 발행용 사본을 만든다. 미관측·무효 ToF는 World 준비를 막지 않는다."""
        import numpy as np
        from .mapper_sensor_state import SensorFrameSnapshot
        if sensor_snapshot is None:
            return snapshot, static_retained
        if type(sensor_snapshot) is not SensorFrameSnapshot:
            raise ValueError('original frozen sensor snapshot required for ToF overlay')
        sensors = sensor_snapshot.sensors
        if sensors is None or not sensors.leaves:
            return snapshot, static_retained
        self._window(snapshot)
        for reset in sensor_snapshot.metric_inputs.sensor_resets:
            identity = reset.sensor_instance, reset.sensor_generation
            if self._resets.get(reset.sensor_id) != identity:
                selected = self._sensor == reset.sensor_id+1
                self._expires[selected] = np.minimum(self._expires[selected], now_us)
                self._seen.pop(reset.sensor_id, None)
                self._resets[reset.sensor_id] = identity
        poses = None
        for sensor, evidence in sensors._tofs:
            if evidence is None or self._seen.get(sensor) == evidence.wire_sha256:
                continue
            if not sensors.current_tof(evidence, now_us):
                continue
            try:
                if poses is None:
                    poses = PoseBuffer(**sensor_snapshot.pose_args)
                    for original in sorted(sensor_snapshot.vehicle_admissions, key=lambda row: row.evidence.pose_sample.at_us):
                        poses.add(original.evidence.pose_sample)
                raw_pose = poses.at(evidence.stamp, sensor_snapshot.calibration.sha256)
                if raw_pose is None:
                    continue
                pose = self._map_pose(alignment, raw_pose)
                leaf = sensors.leaves[sensor]
                result = tof_evidence(evidence, leaf, sensor_snapshot.calibration, admission=sensors,
                    now_us=now_us, pose=pose, motion_qualified=True)
            except ValueError:
                # 센서 하나의 불완전 pose/보정은 그 관측만 보류한다.
                continue
            self._record(result, pose, evidence)
            self._seen[sensor] = evidence.wire_sha256
        known = self._expires != 0
        if not bool(known.any()):
            return snapshot, static_retained
        arrays = snapshot['arrays']
        age = arrays['age_ms'].astype(np.int64)
        # age_ms는 내림 양자화이므로 카메라 관측 시각의 하한으로만 비교한다.
        camera_lower = snapshot_us-(age+1)*1000
        newer_camera = known & (arrays['state'] != 0) & (age != np.iinfo(np.uint32).max) & (camera_lower >= self._high)
        self._low[newer_camera] = self._high[newer_camera] = self._expires[newer_camera] = 0
        known &= ~newer_camera
        if not bool(known.any()):
            return snapshot, static_retained
        active = known & (self._expires > now_us)
        state, ages, permission = arrays['state'].copy(), arrays['age_ms'].copy(), arrays['permission'].copy()
        state[known] = 0
        state[active] = 2
        ages[known] = np.minimum((np.uint64(now_us)-self._low[known])//1000, np.iinfo(np.uint32).max).astype(np.uint32)
        permission[known] = False
        for value in (state, ages, permission):
            value.setflags(write=False)
        result = dict(snapshot, arrays=dict(arrays, state=state, age_ms=ages, permission=permission),
            counts=dict(unknown=int(np.count_nonzero(state == 0)), free=int(np.count_nonzero(state == 1)),
                        occupied=int(np.count_nonzero(state == 2))))
        retained = static_retained
        if retained is not None:
            retained = retained.copy()
            retained[known] = False
        return result, retained
