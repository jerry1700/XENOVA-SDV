"""새 RGB의 slot geometry 재구성만 줄이는 유한 compute-owner 정책이다.

UNKNOWN/occupancy·기한·정합을 승인하지 않는다. immutable anchor는 draft에 속하고,
별도 shared work counter는 취소된 draft의 실제 작업도 센다. I/O/clock 취득은 없다.
"""

from dataclasses import dataclass, replace, asdict
from hashlib import sha256
import math

from ..mapping.alignment import MapOdomAlignment
from ..mapping.local_map import GridSpec
from ..mapping.registration import PoseBox, wrap_yaw
from ..perception.parking import VALID
from ..storage.contracts import canonical
from ..storage.geometry_scope import CatalogGeometryScope


GEOMETRY_MAX_AGE_US = 500_000  # 기존 C parking admission의 geometry_age_ms<=500 경계.
WORK_KEYS = ('new_rgb', 'duplicate_rgb', 'extract_calls', 'extract_us', 'association_calls',
             'association_us', 'reused_new_rgb', 'reused_duplicate_rgb', 'occupancy_refresh_calls', 'occupancy_refresh_us')
REASONS = ('DISABLED', 'NO_ACCEPTED_GEOMETRY', 'CURRENT_PROOF_UNAVAILABLE', 'UNRESOLVED',
           'DOMAIN_OR_AISLE_CHANGED', 'COVERAGE_CHANGED', 'GEOMETRY_CHANGED', 'INTERVAL',
           'POSE_CHANGED', 'REGISTRATION_CHANGED', 'NEW_RGB_REUSE')


class ParkingGeometryWork:
    """단일 compute thread의 진단만 공유한다. 정책/채택 시각은 보유하지 않는다."""
    def __init__(self):
        self._values = {name: 0 for name in WORK_KEYS}
        self._reasons = {name: 0 for name in REASONS}
        self._last_rgb = None

    def observe_rgb(self, key):
        new = key != self._last_rgb
        self.add('new_rgb' if new else 'duplicate_rgb'); self._last_rgb = key
        return new

    def add(self, name, value=1):
        if name not in self._values or type(value) is not int or value < 0:
            raise ValueError('bounded parking work counter required')
        self._values[name] = min((1 << 63)-1, self._values[name]+value)

    def reason(self, name):
        if name not in self._reasons:raise ValueError('closed parking schedule reason required')
        self._reasons[name] = min((1 << 63)-1, self._reasons[name]+1)

    def snapshot(self):
        return dict(self._values, reasons=dict(self._reasons), neural_work_saved=0,
                    elapsed_clock='perf_counter_ns', fps_measured=False)


@dataclass(frozen=True)
class GeometryFrame:
    domain: bytes
    capture_key: tuple
    capture_lower_us: int
    pose: PoseBox
    registration: tuple | None
    registration_revision: int
    coverage: bytes
    geometry: tuple
    radius_m: float


@dataclass(frozen=True)
class ParkingGeometrySchedule:
    interval_us: int = 0
    anchor: GeometryFrame | None = None

    def __post_init__(self):
        if type(self.interval_us) is not int or not 0 <= self.interval_us <= GEOMETRY_MAX_AGE_US//2:
            raise ValueError('parking rebuild interval must be in0..250000us')
        if self.anchor is not None and type(self.anchor) is not GeometryFrame:
            raise ValueError('original immutable geometry anchor required')

    @staticmethod
    def live_interval(profile):
        # 미실측 정책: 기존 FREE/geometry 500ms 중 더 짧은 상한의 절반만 쓴다.
        # geometry/clearance 만료나 오차 허용을 늘리는 값이 아니다.
        return min(profile.free_age_us, GEOMETRY_MAX_AGE_US)//2

    def completed(self, frame):
        return replace(self, anchor=frame)

    def failed(self):
        return replace(self, anchor=None)

    def decide(self, frame, *, now_us, profile, resolved):
        if not self.interval_us:return False, 'DISABLED'
        if frame is None:return False, 'CURRENT_PROOF_UNAVAILABLE'
        if not resolved:return False, 'UNRESOLVED'
        old = self.anchor
        if old is None:return False, 'NO_ACCEPTED_GEOMETRY'
        if frame.domain != old.domain:return False, 'DOMAIN_OR_AISLE_CHANGED'
        if frame.coverage != old.coverage:return False, 'COVERAGE_CHANGED'
        if frame.geometry != old.geometry:return False, 'GEOMETRY_CHANGED'
        if not old.capture_lower_us <= frame.capture_lower_us <= now_us or now_us-old.capture_lower_us >= self.interval_us:
            return False, 'INTERVAL'
        radius = max(old.radius_m, frame.radius_m)
        a, b = old.pose.center, frame.pose.center
        movement = math.hypot(a[0]-b[0], a[1]-b[1])
        movement += math.hypot(*old.pose.widths[:2])/2 + math.hypot(*frame.pose.widths[:2])/2
        yaw = abs(wrap_yaw(a[2]-b[2])) + (old.pose.widths[2]+frame.pose.widths[2])/2
        movement += 2*radius*math.sin(min(math.pi, yaw)/2)
        if movement > profile.max_position_error_m or yaw > profile.max_heading_error_rad:
            return False, 'POSE_CHANGED'
        if frame.registration_revision < old.registration_revision:return False, 'REGISTRATION_CHANGED'
        if (old.registration is None) != (frame.registration is None):return False, 'REGISTRATION_CHANGED'
        if frame.registration is not None:
            a, b = old.registration, frame.registration
            drift = math.hypot(a[0]-b[0], a[1]-b[1]) + a[3]+b[3]
            turn = abs(wrap_yaw(a[2]-b[2])) + a[4]+b[4]
            drift += 2*radius*math.sin(min(math.pi, turn)/2)
            if drift > profile.max_position_error_m or turn > profile.max_heading_error_rad:
                return False, 'REGISTRATION_CHANGED'
        return True, 'NEW_RGB_REUSE'


def current_geometry_frame(bridge, result, stamp, prior, aisles, registration_revision, metric_evidence, now_us):
    """실제 현재 snapshot/정합/원 metric 자료로만 reuse 후보를 만든다. None은 full 경로다."""
    from .parking_bridge import MetricEvidence, _in_roi
    import numpy as np
    try:
        snapshot, summary = result.map_snapshot, result.summary
        if (type(prior) is not PoseBox or stamp.captured_us is None or stamp.age_us(now_us, bridge.clock_id) is None
                or stamp.age_us(now_us, bridge.clock_id) >= min(bridge.profile.free_age_us, GEOMETRY_MAX_AGE_US)
                or summary.get('quarantined') is not False or summary.get('geometric_model_qualified') is not True
                or type(metric_evidence) is not MetricEvidence or stamp.key not in metric_evidence.frame_keys
                or metric_evidence.pi_host_boot != bridge.context.pi_host_boot
                or result.tape is None or result.tape.source_key != stamp.key or not result.tape.complete
                or result.tape.status != 'VALID' or not result.tape.calibration_qualified):
            return None
        spec = snapshot['spec']; arrays = snapshot['arrays']; shape = (spec.height, spec.width)
        if (type(spec) is not GridSpec or spec.width*spec.height > 4096
                or snapshot['frame_id'] != bridge.catalog.frame_id or snapshot['kind'] != bridge._kind
                or snapshot['clock_id'] != bridge.clock_id or snapshot['calibration_id'] != bridge.local_calibration_id):
            return None
        masks = (arrays['support'], arrays['permission'])
        if any(type(mask) is not np.ndarray or mask.dtype != np.bool_ or mask.shape != shape for mask in masks):return None
        domain = CatalogGeometryScope.from_context(bridge.context, 'SLOT').bytes if bridge._kind == 'LIVE' else bridge.context.bytes
        domain += canonical((stamp.key[:-1],
            tuple((a.a, a.b, a.side, a.error_m, a.approach_lane_id) for a in aisles), asdict(result.tape.profile)))
        registration = None
        if bridge._kind == 'LIVE':
            alignment = MapOdomAlignment.from_fields(summary['alignment_metadata'])
            mapped = summary['mapped_pose']
            if (not summary.get('localization_qualified') or not alignment.valid_at(now_us)
                    or alignment.registration_revision != registration_revision
                    or alignment.context[:3] != (bridge.context.odom_epoch, bridge.clock_id, bridge.local_calibration_id)
                    or type(mapped) not in (list, tuple) or len(mapped) != 3
                    or not all(lo <= value <= hi for (lo, hi), value in zip((prior.x, prior.y, prior.yaw), mapped))):return None
            registration = (*alignment.pose, alignment.position_error_m, alignment.yaw_error_rad)
        lo, hi = spec.bounds(0, 0), spec.bounds(spec.width-1, spec.height-1)
        bounds = (lo[0], lo[1], hi[2], hi[3])
        tracks = tuple(t for t in bridge.catalog.tracks if _in_roi(t.candidate, bounds))
        if not 1 <= len(tracks) <= 8:return None
        for track in tracks:
            c = track.candidate; pad = c.position_bound_m
            if track.geometry_age_ms(now_us, bridge.clock_id) >= GEOMETRY_MAX_AGE_US//1000:return None
            if c.source.input_kind != bridge._kind or c.geometry_state != VALID or c.source.domain != (bridge.local_calibration_id, bridge.context.map_epoch,
                    bridge.catalog.frame_id, bridge.context.odom_epoch, bridge.clock_id):return None
            xs, ys = zip(*c.corners)
            x0, y0, x1, y1 = min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad
            if not bounds[0] <= x0 < x1 <= bounds[2] or not bounds[1] <= y0 < y1 <= bounds[3]:return None
            col0 = max(0, math.floor((x0-spec.origin_xy[0])/spec.resolution_m))
            row0 = max(0, math.floor((y0-spec.origin_xy[1])/spec.resolution_m))
            col1 = min(spec.width, math.floor((x1-spec.origin_xy[0])/spec.resolution_m)+1)
            row1 = min(spec.height, math.floor((y1-spec.origin_xy[1])/spec.resolution_m)+1)
            if not bool(np.all(masks[0][row0:row1, col0:col1])):return None
        coverage = sha256(canonical((spec.width, spec.height, spec.resolution_m, spec.origin_xy))+
                          masks[0].tobytes()+masks[1].tobytes()).digest()
        geometry = tuple((t.slot_id, t.geometry_revision, t.candidate.geometry_sha256) for t in tracks)
        radius = max(math.dist(p, prior.center[:2]) for t in tracks for p in t.candidate.corners)
        return GeometryFrame(domain, stamp.key, stamp.captured_us[0], prior, registration,
                             registration_revision, coverage, geometry, radius)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        return None
