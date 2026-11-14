"""재방문 위치 정합에 쓰는 지도 좌표의 실제 테이프 관측."""

from dataclasses import dataclass, replace
import math

from ..mapping.registration import (
    PointFeature, PoseBox, SegmentFeature, finite_feature_point, overlapping_points, segment_distance,
    transform_point,
)
from .registration_history import POINT_LIMIT, RegistrationObservation, in_current_frame, recent_observations
from .visual_registration import VisualRegistrationPolicy


_CELL_M = .16
_DASH_CELL_M = .055
_DIRECTION_BINS = 12
_MAP_LIMIT = 512
_LOCAL_LIMIT = 32
_HISTORY_LIMIT = 3
_MAX_OBSERVATION_X_M = .95
_MIN_NORMAL_LENGTH_M = .16


@dataclass(frozen=True)
class _Landmark:
    segment: SegmentFeature
    observed_us: int
    midpoint_error_xy: tuple[float, float] | None


def _midpoint(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _map_key(segment):
    x, y = _midpoint(segment.a, segment.b)
    cell = _DASH_CELL_M if segment.finite_endpoints else _CELL_M
    angle = math.atan2(segment.b[1] - segment.a[1], segment.b[0] - segment.a[0])
    return (math.floor(x / cell), math.floor(y / cell),
            round(angle * _DIRECTION_BINS / math.pi) % _DIRECTION_BINS,
            segment.finite_endpoints)


def _observed_parts(graph):
    """가림 끝은 횡방향 제약에만 사용한다. 짧은 실제 점선의 양끝은 함께 대응시킨다."""
    for strip in graph.strips:
        if not strip.observed or strip.closed or 'predicted' in (strip.start_kind, strip.end_kind):
            continue
        points = strip.points_xy
        a, b = points[0], points[-1]
        length = math.dist(a, b)
        if max(a[0], b[0]) <= _MAX_OBSERVATION_X_M and .018 <= length <= .25 and (
                strip.start_kind == strip.end_kind == 'observed_paint_end'):
            errors = strip.point_error_xy
            midpoint_error = tuple((errors[0][axis] + errors[-1][axis]) / 2 for axis in (0, 1)) if errors else None
            yield strip.local_id, 0, points, strip.error_m, midpoint_error, True
        start = 0
        part = 1
        while start < len(points) - 1:
            end = start + 1
            while end < len(points) - 1 and math.dist(points[start], points[end]) < .48:
                end += 1
            curve = points[start:end + 1]
            if max(p[0] for p in curve) <= 1.25 and math.dist(curve[0], curve[-1]) >= _MIN_NORMAL_LENGTH_M:
                # 원 strip의 원거리 전체 오차 대신 이 구간의 원 투영 오차를 사용한다.
                errors = strip.segment_error_xy[start:end]
                error_xy = tuple(max(value[axis] for value in errors) for axis in (0, 1)) if errors else None
                error = math.hypot(*error_xy) if error_xy else strip.error_m
                yield strip.local_id, part, curve, error, error_xy, False
            start = end
            part += 1


def _agrees_with_prior(point, segments, center, tolerance):
    x, y = transform_point(point.point, center)
    endpoints = dict(point.endpoint_matches)
    for identity in point.compatible:
        segment = segments[identity]
        error = point.error_m
        if point.normal_only:
            dx, dy = segment.b[0] - segment.a[0], segment.b[1] - segment.a[1]
            length = math.hypot(dx, dy)
            normal = dy / length, -dx / length
            residual = abs((x - segment.a[0]) * normal[0] + (y - segment.a[1]) * normal[1])
            if point.error_xy is not None:
                nx, ny = transform_point(normal, (0., 0., -center[2]))
                error = abs(nx) * point.error_xy[0] + abs(ny) * point.error_xy[1]
        else:
            residual = math.dist((x, y), finite_feature_point(segment, endpoints[identity]))
        if residual <= error + segment.error_m + tolerance:
            return True
    return False


def _rotated_axis_errors(error, yaw, yaw_radius, radius):
    cosine, sine = abs(math.cos(yaw)), abs(math.sin(yaw))
    dx, dy = error
    rotation_pad = 2 * (radius + math.hypot(dx, dy)) * math.sin(yaw_radius / 2)
    return cosine * dx + sine * dy + rotation_pad, sine * dx + cosine * dy + rotation_pad


def _midpoint_prior(prior, common, landmarks, tolerance):
    """각 점선의 후보 중점 합집합으로 prior를 좁히고 원 탐색 중심은 유지한다."""
    center = prior.center
    half_x, half_y, half_yaw = (width / 2 for width in prior.widths)
    for point in common:
        if not point.endpoint_matches or point.error_xy is None:
            continue
        mapped = transform_point(point.point, center)
        point_error = _rotated_axis_errors(point.error_xy, center[2], half_yaw, math.hypot(*point.point))
        bounds = []
        for key in point.compatible:
            landmark = landmarks[key]
            old = _midpoint(landmark.segment.a, landmark.segment.b)
            old_error = landmark.midpoint_error_xy or (landmark.segment.error_m,) * 2
            bounds.append(tuple((p - q - e - f - tolerance, p - q + e + f + tolerance)
                                for p, q, e, f in zip(old, mapped, point_error, old_error)))
        # 대응이 유일하지 않아도 후보 중 하나가 맞는 전체 구간을 남긴다.
        if bounds:
            x, y = zip(*bounds)
            x_low, x_high = min(v[0] for v in x), max(v[1] for v in x)
            y_low, y_high = min(v[0] for v in y), max(v[1] for v in y)
            if not x_low <= 0 <= x_high or not y_low <= 0 <= y_high:
                continue
            half_x = min(half_x, max(-x_low, x_high))
            half_y = min(half_y, max(-y_low, y_high))
    return PoseBox((center[0]-half_x, center[0]+half_x), (center[1]-half_y, center[1]+half_y), prior.yaw)


class MappedRegistration:
    """낮은 오차의 원 관측을 위치·방향별로 보관하고 차량 주변만 읽는다."""

    def __init__(self, policy=VisualRegistrationPolicy()):
        self._policy = policy
        self._landmarks = {}
        self._history = ()

    def clear(self):
        self._landmarks.clear()
        self._history = ()

    def __deepcopy__(self, memo):
        clone = type(self)(self._policy)
        clone._landmarks = self._landmarks.copy()
        clone._history = self._history
        memo[id(self)] = clone
        return clone

    def add(self, graph, capture, pose):
        p = pose.sample
        for strip, part, points, error, error_xy, finite in _observed_parts(graph):
            a, b = points[0], points[-1]
            curve_error = max(segment_distance(point, a, b) for point in points)
            radius = max(math.hypot(*point) for point in points)
            pose_error = p.position_error_m + 2 * radius * math.sin(p.yaw_error_rad / 2)
            transform = (p.x, p.y, p.yaw)
            support_error = None
            if error_xy is not None and not finite:
                support_error = error + curve_error + pose_error
                length = math.dist(a, b)
                error = (abs(b[1]-a[1])*error_xy[0] + abs(b[0]-a[0])*error_xy[1]) / length
            segment = SegmentFeature(transform_point(a, transform), transform_point(b, transform),
                error + curve_error + pose_error, f'{capture}:map:{strip}:{part}', capture,
                support_error, finite)
            key = _map_key(segment)
            current = self._landmarks.get(key)
            if current is not None and current.segment.error_m <= segment.error_m:
                continue
            if current is None and len(self._landmarks) >= _MAP_LIMIT:
                self._landmarks.pop(next(iter(self._landmarks)))
            midpoint_error = None
            if finite and error_xy is not None:
                midpoint = _midpoint(a,b)
                midpoint_error = tuple(value + p.position_error_m for value in
                    _rotated_axis_errors(error_xy,p.yaw,p.yaw_error_rad,math.hypot(*midpoint)))
            self._landmarks[key] = _Landmark(segment, pose.captured_us[1], midpoint_error)

    def observe(self, graph, capture, raw_pose, stream):
        """현재 RGB를 보존하고 직전 프레임은 원 휠 이동으로 현재 좌표에 옮긴다."""
        points, ends = [], {}
        for strip, part, curve, error, error_xy, finite in _observed_parts(graph):
            a, b = curve[0], curve[-1]
            name = f'map:{strip}:{part}'
            if finite:
                ends[name] = (a, b, error)
                samples = (a, b)
            else:
                curve_error = max(segment_distance(point, a, b) for point in curve)
                error += curve_error
                if error_xy is not None:
                    error_xy = tuple(value + curve_error for value in error_xy)
                samples = tuple((a[0] + (b[0] - a[0]) * fraction,
                                 a[1] + (b[1] - a[1]) * fraction) for fraction in (1/3, 2/3))
            points.extend(PointFeature(point, error, name, capture, error_xy=error_xy, normal_only=not finite)
                          for point in samples)
        current = RegistrationObservation(capture, stream, raw_pose, tuple(points), tuple(ends.items()))
        previous = recent_observations(self._history,current,self._policy.revisit_history_us)
        self._history = (*previous, current)[-_HISTORY_LIMIT:]
        return current, previous

    def match(self, observation, pose, reference, profile, *, lane_width_m, solve):
        """새 RGB가 예전 지도 구간과 맞아 기존 위치 범위를 줄일 때만 결과를 반환한다."""
        current, previous = observation
        points = current.points
        if not points or pose.sample.position_error_m >= lane_width_m / 3:
            return None
        center = pose.prior.center
        views = [(points, dict(current.ends))]
        views.extend(in_current_frame(earlier, current) for earlier in reversed(previous) if earlier.points)
        locations = tuple(transform_point(p.point, center) for features, _ in views for p in features)
        lateral_limit = lane_width_m / 2
        min_x = min(x for x, _ in locations) - lateral_limit
        max_x = max(x for x, _ in locations) + lateral_limit
        min_y = min(y for _, y in locations) - lateral_limit
        max_y = max(y for _, y in locations) + lateral_limit
        candidates = []
        cutoff = pose.captured_us[0] - self._policy.revisit_min_age_us
        for item in self._landmarks.values():
            segment = item.segment
            if item.observed_us > cutoff or segment.capture_id == current.capture:
                continue
            a, b = segment.a, segment.b
            if (min(a[0], b[0]) > max_x or max(a[0], b[0]) < min_x
                    or min(a[1], b[1]) > max_y or max(a[1], b[1]) < min_y):
                continue
            distance = min(segment_distance(point, segment.a, segment.b) for point in locations)
            if distance <= lateral_limit:
                candidates.append((distance + segment.error_m, item))
        candidates.sort(key=lambda item: item[0])
        selected = {item.segment.component: item for _, item in candidates[:_LOCAL_LIMIT]}
        reference = replace(reference, segments=tuple(item.segment for item in selected.values()),
                            feature_policy='OBSERVED_STRIP_SUPPORT_V1')
        segments = {s.component: s for s in reference.segments}
        tolerance = reference.shared_error_m + profile.residual_m + profile.numeric_guard_m
        common = []
        for features, ends in views:
            supported = overlapping_points(features, reference, pose.prior, profile, observed_ends=ends,
                position_radius_m=pose.sample.position_error_m, max_lateral_match_m=lateral_limit,
                include_midpoints=True)
            supported = tuple(replace(p, component=p.capture_id+':'+p.component) for p in supported
                              if p.normal_only or p.endpoint_matches)
            # 현재 추정과 충돌하는 오래된 조각은 동일한 테이프라는 근거가 약하다.
            # 조각 전체를 빼고 남은 관측만으로 기존 오차 구간을 제한한다.
            incompatible = {p.component for p in supported
                            if not _agrees_with_prior(p, segments, center, tolerance)}
            supported = tuple(p for p in supported if p.component not in incompatible)
            if not common and not supported:
                return None
            if len(common) + len(supported) <= POINT_LIMIT:
                common.extend(supported)
        # 단일 점선 오대응으로 기준점을 옮기지 않는다. 두 방향의 실선이나
        # 독립된 여러 조각이 현재 위치를 함께 제한할 때만 탐색한다.
        common = tuple(common)
        if len({p.component for p in common}) < 2 or not any(p.capture_id == current.capture for p in common):
            return None
        prior = _midpoint_prior(pose.prior, common, selected, tolerance)
        result = solve(common,reference,prior,position_radius_m=pose.sample.position_error_m,
                       max_cells=self._policy.revisit_max_cells,min_steps=(.025,.025,.018))
        if (result.status == 'BOUNDED' and pose.sample.position_error_m-result.position_error_m
                >= self._policy.revisit_min_improvement_m
                and result.permits(center)):
            return result, common, reference
        return None
