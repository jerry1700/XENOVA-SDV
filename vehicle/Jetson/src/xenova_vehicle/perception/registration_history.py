"""최근 RGB 제약을 원 오도메트리의 상대 이동으로 현재 차체 좌표에 옮긴다."""

from dataclasses import dataclass, replace
import math

from ..mapping.odometry import PoseEstimate
from ..mapping.registration import PointFeature, overlapping_points, transform_point, wrap_yaw
from ..mapping.wheel_motion import relative_wheel_bounds


HISTORY_FRAMES = 2
POINT_LIMIT = 64


@dataclass(frozen=True)
class RegistrationObservation:
    capture: str
    stream: tuple
    odometry: PoseEstimate
    points: tuple[PointFeature, ...]
    ends: tuple


def in_current_frame(earlier, current):
    previous = earlier.odometry.sample
    now = current.odometry.sample
    dx, dy = previous.x-now.x, previous.y-now.y
    shift = transform_point((dx,dy),(0.,0.,-now.yaw))
    rotation = wrap_yaw(previous.yaw-now.yaw)
    transform = (*shift,rotation)
    # 현재 차체에서 본 이전 자세다. 연속 휠 구간이면 두 캡처 사이의 오차만 사용한다.
    bounds = relative_wheel_bounds(current.odometry,earlier.odometry)
    if bounds is None:
        bounds = (previous.position_error_m+now.position_error_m
            + 2*math.hypot(dx,dy)*math.sin(min(math.pi,now.yaw_error_rad)/2),
            previous.yaw_error_rad+now.yaw_error_rad)
    displacement_error,yaw_error = bounds
    rotation_scale = 2*math.sin(min(math.pi,yaw_error)/2)
    c,s = abs(math.cos(rotation)),abs(math.sin(rotation))
    points = []
    for feature in earlier.points:
        pad = displacement_error+math.hypot(*feature.point)*rotation_scale
        error_xy = None
        if feature.error_xy is not None:
            x,y = feature.error_xy
            error_xy = c*x+s*y+pad,s*x+c*y+pad
        points.append(replace(feature,point=transform_point(feature.point,transform),
                              error_m=feature.error_m+pad,error_xy=error_xy))
    ends = {key:(transform_point(a,transform),transform_point(b,transform),
                 error+displacement_error+max(math.hypot(*a),math.hypot(*b))*rotation_scale)
            for key,(a,b,error) in earlier.ends}
    return tuple(points),ends


def recent_observations(history, current, max_age_us):
    return tuple(frame for frame in history if frame.stream == current.stream
        and frame.odometry.sample.context == current.odometry.sample.context
        and frame.odometry.captured_us[1] < current.odometry.captured_us[0]
        and current.odometry.captured_us[1]-frame.odometry.captured_us[0] <= max_age_us)


def combined_constraints(history, current, reference, prior, profile, *, position_radius_m,
                         max_lateral_match_m, max_age_us):
    """자기 관측을 제외한 frozen map에 현재·이전 관측을 각각 대응시킨다."""
    previous = recent_observations(history,current,max_age_us)
    if not previous:
        return (),reference
    captures = {current.capture,*(frame.capture for frame in previous)}
    reference = replace(reference,segments=tuple(segment for segment in reference.segments
                                                 if segment.capture_id not in captures))
    selected = []
    for frame in (current,*reversed(previous)):
        points,ends = (frame.points,dict(frame.ends)) if frame is current else in_current_frame(frame,current)
        common = overlapping_points(points,reference,prior,profile,observed_ends=ends,
            position_radius_m=position_radius_m,max_lateral_match_m=max_lateral_match_m,include_endpoints=True)
        if frame is current and not common:
            return (),reference
        if len(selected)+len(common) <= POINT_LIMIT:
            # local strip ID는 다른 RGB에 재사용되므로 solver의 component도 캡처별로 분리한다.
            selected.extend(replace(point,component=point.capture_id+':'+point.component) for point in common)
    if len({point.capture_id for point in selected}) < 2:
        return (),reference
    return tuple(selected),reference
