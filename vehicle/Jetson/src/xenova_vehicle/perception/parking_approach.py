"""주차 입구 주변의 원 흰선으로 두 차로의 짧은 지도 구간을 다시 맞춘다."""

from dataclasses import dataclass, replace
import math
from statistics import median

from .road import LaneHypothesis, RoadResult
from .road_normals import heading_error
from ..mapping.registration import transform_point, wrap_yaw
from ..mapping.wheel_motion import relative_wheel_bounds


_WINDOW_M = .46
_MAX_ROLE_RESIDUAL_M = .075
_MAX_MARKING_ANGLE_RAD = .38


@dataclass(frozen=True)
class ApproachFit:
    road: RoadResult
    sources: frozenset
    longitudinal_bound_m: float
    lateral_bound_m: float
    entrance_relative_bounds_m: tuple[float | None, float | None]
    local_heading_bounds_rad: tuple[float, ...]
    map_position_bound_m: float | None


@dataclass(frozen=True)
class _Point:
    u: float
    v: float
    du: float
    dv: float
    raw: tuple
    camera_error: tuple
    path_error: tuple
    rotation: float
    position_error: float
    yaw_error: float
    source: tuple

    def axis_bound(self, direction, error):
        return _axis_bound(self.raw, error, self.rotation-direction, self.position_error, self.yaw_error)


def _axis_bound(raw, error, angle, position_error, yaw_error):
    c, s = math.cos(angle), math.sin(angle)
    x, y = raw
    parallel, transverse = c*x-s*y, s*x+c*y
    along_error = abs(c)*error[0]+abs(s)*error[1]
    across_error = abs(s)*error[0]+abs(c)*error[1]
    return (along_error + position_error + (abs(parallel)+along_error)*(1-math.cos(yaw_error))
            + (abs(transverse)+across_error)*math.sin(min(math.pi/2, yaw_error)))


def _point(raw, mapped, camera_error, path_error, pose, origin, heading, source, *, vertex_bounds):
    c, s = math.cos(heading), math.sin(heading)
    x, y = mapped[0] - origin[0], mapped[1] - origin[1]
    u, v = c*x + s*y, -s*x + c*y
    yaw = min(math.pi, pose.yaw_error_rad)
    rotation = pose.yaw-heading
    error = camera_error if vertex_bounds else tuple(map(max, camera_error, path_error))
    du = _axis_bound(raw, error, rotation, pose.position_error_m, yaw)
    dv = _axis_bound(raw, error, rotation-math.pi/2, pose.position_error_m, yaw)
    return _Point(u,v,du,dv,raw,camera_error,path_error,rotation,pose.position_error_m,yaw,source)


def _measurements(strips, origin, heading, width, outer, *, vertex_bounds=False):
    bands = [[], [], []]
    c, s = math.cos(heading), math.sin(heading)
    for strip in strips:
        if strip.mapped_pose is None or strip.original.closed:
            continue
        errors = strip.original.point_error_xy or ((strip.original.error_m,)*2,)*len(strip.points)
        parts = []
        pose = strip.mapped_pose.sample
        raw_points = strip.original.points_xy
        for i, (a, b) in enumerate(zip(strip.points,strip.points[1:])):
            x, y = a[0]-origin[0], a[1]-origin[1]
            u, v = c*x+s*y, -s*x+c*y
            x, y = b[0]-origin[0], b[1]-origin[1]
            du, dv = c*x+s*y-u, -s*x+c*y-v
            if abs(du) < 1e-9 or abs(math.atan(dv/du)) > _MAX_MARKING_ANGLE_RAD:
                continue
            first,last = sorted(((-_WINDOW_M-u)/du,(_WINDOW_M-u)/du))
            if last <= 0. or first >= 1.:
                continue
            path_error = (strip.original.segment_error_xy[i] if strip.original.segment_error_xy
                          else tuple(max(errors[i][k],errors[i+1][k]) for k in range(2)))
            segment = []
            for t in (max(0.,first),min(1.,last)):
                if abs(v+t*dv) > 2*width+_MAX_ROLE_RESIDUAL_M:
                    continue
                if t in (0.,1.):
                    j = i+int(t)
                    # A measured vertex does not inherit the largest error elsewhere on its strip.
                    segment.append(_point(raw_points[j],strip.points[j],errors[j],path_error,pose,origin,heading,
                                          strip.source,vertex_bounds=vertex_bounds))
                else:
                    p, q = raw_points[i:i+2]
                    raw = tuple(p[k]+t*(q[k]-p[k]) for k in range(2))
                    mapped = tuple(a[k]+t*(b[k]-a[k]) for k in range(2))
                    segment.append(_point(raw,mapped,path_error,path_error,pose,origin,heading,
                                          strip.source,vertex_bounds=vertex_bounds))
            if len(segment) != 2:
                continue
            if parts and math.hypot(parts[-1][-1].u-segment[0].u,parts[-1][-1].v-segment[0].v) < 1e-9:
                parts[-1].extend(segment)
            else:
                parts.append(segment)
        for points in parts:
            a = min(points, key=lambda p: p.u)
            b = max(points, key=lambda p: p.u)
            if b.u-a.u < .025 or abs(math.atan2(b.v-a.v,b.u-a.u)) > _MAX_MARKING_ANGLE_RAD:
                continue
            normal = median(p.v for p in points)
            role = outer-round(normal/width)
            if 0 <= role <= 2 and abs(normal-(outer-role)*width) <= _MAX_ROLE_RESIDUAL_M:
                bands[role].append((strip,tuple(points)))
    return bands


def _covered(rows, max_gap):
    """Join short same-role paint gaps and trim the ends of the resulting run."""
    intervals = []
    for _, points in rows:
        first = min(points, key=lambda p: p.u)
        last = max(points, key=lambda p: p.u)
        intervals.append((first.u+first.du,last.u-last.du))
    runs = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if runs and start-runs[-1][1] <= max_gap:
            runs[-1] = runs[-1][0],max(end,runs[-1][1])
        else:
            runs.append((start,end))
    return runs


def _slope(rows):
    # Each strip's position error is shared by both endpoints. Keep the camera
    # errors in their two directions when measuring the chord's direction.
    chords = []
    for _, points in rows:
        a = min(points, key=lambda p: p.u)
        b = max(points, key=lambda p: p.u)
        chord = math.atan2(b.v-a.v,b.u-a.u)
        uncertainty = _chord_error(a,b)
        chords.append((a.u,b.u,chord,uncertainty))
    return chords


def _lateral_bound(rows, lo, hi, slope, intercept, profile):
    """Bound each subinterval with direct evidence or a short, bounded gap."""
    scale = math.hypot(1.,slope)
    intervals = []
    for _, points in rows:
        start, end = min(points,key=lambda p:p.u), max(points,key=lambda p:p.u)
        a, b = start.u+start.du, end.u-end.du
        if a >= b:
            continue
        bound = max((abs(p.v-slope*p.u-intercept)+p.dv+abs(slope)*p.du)/scale for p in points)
        intervals.append((a,b,bound))
    cuts = sorted({lo,hi,*(u for a,b,_ in intervals for u in (a,b) if lo < u < hi)})
    bounds = []
    for a,b in zip(cuts,cuts[1:]):
        direct = [error for start,end,error in intervals if start <= a and b <= end]
        if direct:
            bounds.append(min(direct))
            continue
        before = max(((end,-error) for _,end,error in intervals if end <= a),default=None)
        after = min(((start,error) for start,_,error in intervals if b <= start),default=None)
        if before is None or after is None:
            return None
        gap = (after[0]-before[0])*scale
        if gap > profile.max_gap_m:
            return None
        bounds.append(max(-before[1],after[1])+profile.max_curvature_1pm*gap**2/8)
    return max(bounds)


def _basis(span):
    a, b = span.endpoints
    origin = ((a[0]+b[0])/2,(a[1]+b[1])/2)
    heading = math.atan2(b[1]-a[1],b[0]-a[0])
    c, s = math.cos(heading), math.sin(heading)
    side = 1 if span.edge_role == 0 else -1
    if side*(-s*span.outward[0]+c*span.outward[1]) < 0:
        heading += math.pi
    return origin,heading


def _wheel_aligned_strips(strips, source_key):
    """Place raw tape in one nominal map frame with only relative pose error."""
    reference = next((s for s in strips if s.source == source_key and s.capture_pose is not None
                      and s.capture_pose.wheel_capture is not None and s.mapped_pose is not None),None)
    if reference is None:
        return (),None
    capture, anchor = reference.capture_pose,reference.mapped_pose.sample
    raw_anchor = capture.sample
    rotation = wrap_yaw(anchor.yaw-raw_anchor.yaw)
    poses = {source_key:replace(reference.mapped_pose,sample=replace(anchor,position_error_m=0.,yaw_error_rad=0.))}
    aligned = []
    for strip in strips:
        if strip.source[:-1] != source_key[:-1] or strip.capture_pose is None:
            continue
        if strip.source not in poses:
            bounds = relative_wheel_bounds(capture,strip.capture_pose)
            if bounds is None:
                poses[strip.source] = None
            else:
                raw = strip.capture_pose.sample
                x,y = transform_point((raw.x-raw_anchor.x,raw.y-raw_anchor.y),(anchor.x,anchor.y,rotation))
                sample = replace(raw,x=x,y=y,yaw=wrap_yaw(raw.yaw+rotation),
                                 position_error_m=bounds[0],yaw_error_rad=bounds[1])
                poses[strip.source] = replace(strip.capture_pose,sample=sample)
        pose = poses[strip.source]
        if pose is not None:
            nominal = pose.sample.x,pose.sample.y,pose.sample.yaw
            aligned.append(replace(strip,mapped_pose=pose,points=tuple(
                transform_point(point,nominal) for point in strip.original.points_xy)))
    return tuple(aligned),anchor


def _edge_samples(rows, stations=()):
    by_capture = {}
    for strip,points in rows:
        by_capture.setdefault(strip.source,[]).extend(points)
    # Limit the pair search while retaining the longitudinal baseline across images.
    selected = []
    for points in by_capture.values():
        selected.extend((min(points,key=lambda p:p.u+p.du),max(points,key=lambda p:p.u-p.du),
                         min(points,key=lambda p:p.dv)))
        selected.extend(min(points,key=lambda p:abs(p.u-station)+p.du) for station in stations)
    return tuple(dict.fromkeys(selected))


def _chord_error(a, b):
    if a.source == b.source:
        # The same capture translates both ends together and rotates the chord once.
        return heading_error(a.raw,b.raw,a.camera_error,b.camera_error)+a.yaw_error
    return heading_error((a.u,a.v),(b.u,b.v),(a.du,a.dv),(b.du,b.dv))


def _edge_chords(points, min_length_m=.12):
    ordered = sorted(points,key=lambda p:p.u)
    for i,a in enumerate(ordered):
        for b in ordered[i+1:]:
            if b.u-a.u < min_length_m or b.u-b.du <= a.u+a.du:
                continue
            angle = math.atan2(b.v-a.v,b.u-a.u)
            if abs(angle) > _MAX_MARKING_ANGLE_RAD:
                continue
            yield a,b,angle,_chord_error(a,b)


def _edge_chord(chords, lo, hi, curvature):
    """Estimate a nominal chord for the straight fit."""
    return min(((angle, bound+curvature*max(0.,a.u+a.du-lo,hi-b.u+b.du))
                for a,b,angle,bound in chords), key=lambda item:item[1], default=None)


def _local_tangent_bounds(chords, lo, hi, nominal, curvature):
    """Bound the local tangent on each of the three emitted segments."""
    result = []
    stations = tuple(lo+(hi-lo)*i/3 for i in range(4))
    for start,end in zip(stations,stations[1:]):
        lower,upper = -1.,1.
        for a,b,angle,bound in chords:
            # The true chord's direction occurs somewhere between its ends. For a
            # forward graph, d(sin(tangent))/du is curvature, including inside the chord.
            distance = max(abs(start-(b.u+b.du)),abs(end-(a.u-a.du)))
            variation = curvature*distance
            lower = max(lower,math.sin(max(-math.pi/2,angle-bound))-variation)
            upper = min(upper,math.sin(min(math.pi/2,angle+bound))+variation)
        if lower > upper:
            result.append(math.pi)
        else:
            result.append(max(abs(math.asin(lower)-nominal),abs(math.asin(upper)-nominal)))
    return tuple(result)


def _local_edge_heading(rows, lo, hi, nominal, curvature):
    # Keep observations close to the output stations as well as long baselines.
    stations = tuple(lo+(hi-lo)*i/3 for i in range(4))
    points = _edge_samples(rows,stations)
    chords = tuple(_edge_chords(points,min_length_m=.04))
    return _local_tangent_bounds(chords,lo,hi,nominal,curvature) if chords else (math.pi,)*3


def _relative_entrance_bounds(span, shared_rows):
    """Remeasure a mouth endpoint from a physical junction on the shared tape."""
    endpoints = []
    for strip,_ in shared_rows:
        pose = strip.mapped_pose.sample
        kinds = (strip.original.start_kind,strip.original.end_kind)
        for index,kind,error in zip((0,-1),kinds,strip.original.endpoint_error_m):
            if kind == 'physical_junction' and error is not None:
                rotation = 2*math.sin(pose.yaw_error_rad/2)*math.hypot(*strip.original.points_xy[index])
                endpoints.append((strip.points[index],error+pose.position_error_m+rotation))
    return tuple(min((math.dist(target,point)+bound for point,bound in endpoints
                      if math.dist(target,point) <= _MAX_ROLE_RESIDUAL_M),default=None)
                 for target in span.endpoints)


def _observed_runs(rows, lo, hi, residual, axis):
    for strip, points in rows:
        if not strip.original.segment_error_xy:
            continue
        start = max(lo,min(point.u+point.du for point in points))
        end = min(hi,max(point.u-point.du for point in points))
        if start >= end:
            continue
        segments = []
        for a,b in zip(points,points[1:]):
            # b retains the error of the original segment ending at b, even on a reversed strip.
            error = max(a.axis_bound(axis,b.path_error),b.axis_bound(axis,b.path_error))
            a_pad,b_pad = a.axis_bound(0.,b.path_error),b.axis_bound(0.,b.path_error)
            segments.append((min(a.u-a_pad,b.u-b_pad),max(a.u+a_pad,b.u+b_pad),
                             max(residual(a),residual(b))+error))
        yield start,end,segments


def _boundary_bound(points, lo, hi, slope, intercept, yaw, profile, *, rows=()):
    """Bound short cells of the lane against nearby original observations."""
    scale = math.hypot(1.,slope)
    axis = math.atan(slope)+math.pi/2
    def residual(point):
        return abs(point.v-slope*point.u-intercept)/scale
    errors = [(p,residual(p)+p.axis_bound(axis,p.camera_error)) for p in points]
    observed = tuple(_observed_runs(rows,lo,hi,residual,axis))
    maximum = 0.
    steps = max(1,math.ceil((hi-lo)/.015))
    stations = sorted({lo,hi,*(lo+(hi-lo)*i/steps for i in range(1,steps)),
                       *(u for a,b,_ in observed for u in (a,b))})
    for start,end in zip(stations,stations[1:]):
        # A continuous strip has no gap at an internal vertex. All segments that
        # can reach the cell must contribute to the bound for that strip.
        candidates = [max(error for a,b,error in segments if a <= end and start <= b)
                      for run_start,run_end,segments in observed if run_start <= start and end <= run_end]
        for p,offset in errors:
            gap = (max(abs(start-p.u),abs(end-p.u))+p.du)*scale
            if gap <= profile.max_gap_m:
                candidates.append(offset+gap*math.sin(min(math.pi/2,yaw))+.5*profile.max_curvature_1pm*gap**2)
        if not candidates:
            return None
        maximum = max(maximum,min(candidates))
    return maximum


def _common_heading(chords):
    lower = max(angle-bound for angle,bound in chords)
    upper = min(angle+bound for angle,bound in chords)
    return (lower,upper) if lower <= upper else None


def _staggered_fit(span, strips, source_key, profile):
    aligned,anchor_pose = _wheel_aligned_strips(strips,source_key)
    if anchor_pose is None or len({s.source for s in aligned}) < 2:
        return None
    origin,heading = _basis(span)
    outer,width = span.edge_role,profile.missing_outer_width_m
    bands = _measurements(aligned,origin,heading,width,outer,vertex_bounds=True)
    samples = {role:_edge_samples(rows) for role,rows in enumerate(bands)
               if len({strip.source for strip,_ in rows}) >= 2}
    if outer not in samples or len(samples) < 2:
        return None
    spans = {role:(min(p.u+p.du for p in points),max(p.u-p.du for p in points))
             for role,points in samples.items()}
    start,end = spans[outer]
    length = profile.min_usable_length_m
    if end-start < length or start > .075 or end < -.075:
        return None
    lo = min(end-length,max(start,-length/2))
    hi = lo+length
    candidates = {role:tuple(_edge_chords(points)) for role,points in samples.items()}
    chords = {role:chord for role,observed in candidates.items() if (
        chord := _edge_chord(observed,lo,hi,profile.max_curvature_1pm)) is not None}
    if outer not in chords or len(chords) < 2:
        return None
    roles = tuple(chords)
    common = _common_heading(chords.values())
    if common is None:
        return None
    angle = sum(common)/2
    slope, scale = math.tan(angle),1/math.cos(angle)
    measured_offsets = {role:median(p.v-slope*p.u for _,points in bands[role] for p in points) for role in roles}
    shifted = [measured_offsets[role]-(outer-role)*width*scale for role in roles]
    if max(shifted)-min(shifted) > profile.missing_outer_width_error_m*2*scale:
        return None
    intercept = median(shifted)
    rows = [(role,strip,points) for role in roles for strip,points in bands[role]]
    if not any(strip.source == source_key for _,strip,_ in rows):
        return None
    # Use the absolute anchor only when projecting the completed shape back to map.
    dx,dy = anchor_pose.x-origin[0],anchor_pose.y-origin[1]
    c,s = math.cos(heading),math.sin(heading)
    anchor_u,anchor_v = c*dx+s*dy,-s*dx+c*dy
    sin_angle,cos_angle = math.sin(angle),math.cos(angle)
    offsets = tuple((outer-role)*width for role in range(3))
    # Extent was measured in the wheel-relative frame. The same rigid transform
    # moves both the extent and the observations, so only the output needs a map pad.
    radius = max(math.hypot(u-offset*sin_angle-anchor_u,intercept+slope*u+offset*cos_angle-anchor_v)
                 for u in (lo,hi) for offset in offsets)
    map_pad = anchor_pose.position_error_m+2*radius*math.sin(min(math.pi,anchor_pose.yaw_error_rad)/2)
    edge_ranges = {}
    directions = []
    local_heading = []
    for role in roles:
        shift = offsets[role]*sin_angle
        edge_lo,edge_hi = lo-shift,hi-shift
        chord = _edge_chord(candidates[role],edge_lo,edge_hi,profile.max_curvature_1pm)
        if chord is None:
            return None
        directions.append(chord)
        local_heading.append(_local_edge_heading(bands[role],edge_lo,edge_hi,angle,profile.max_curvature_1pm))
        edge_ranges[role] = edge_lo,edge_hi
    common = _common_heading(directions)
    if common is None:
        return None
    yaw = max(abs(angle-limit) for limit in common)
    lateral = 0.
    for role,(edge_lo,edge_hi) in edge_ranges.items():
        expected = intercept+offsets[role]*scale
        edge_error = _boundary_bound(samples[role],edge_lo,edge_hi,slope,expected,yaw,profile,rows=bands[role])
        if edge_error is None:
            return None
        lateral = max(lateral,edge_error)
    lateral += map_pad+profile.station_error_m
    if len(roles) < 3:
        lateral += profile.missing_outer_width_error_m
    longitudinal = max(p.du for _,_,points in rows for p in points)+map_pad
    local_heading = tuple(map(max,zip(*local_heading)))
    return _build_fit(origin,heading,lo,hi,angle,intercept,outer,width,rows,source_key,longitudinal,
                      lateral,max(local_heading)+anchor_pose.yaw_error_rad,'WHEEL_ALIGNED_APPROACH_TAPE',
                      entrance_relative=_relative_entrance_bounds(span,bands[outer]),
                      local_heading=local_heading,map_pad=map_pad)


def refit_approach(span, strips, source_key, profile):
    """Associate the entrance to original RGB tape, using wheel motion across captures."""
    overlap = _overlapping_fit(span,strips,source_key,profile)
    return overlap if overlap is not None else _staggered_fit(span,strips,source_key,profile)


def _overlapping_fit(span, strips, source_key, profile):
    origin,heading = _basis(span)
    width = profile.missing_outer_width_m
    outer = span.edge_role
    bands = _measurements(strips,origin,heading,width,outer)
    observed = tuple(role for role, rows in enumerate(bands)
                     if len({strip.source for strip,_ in rows}) >= 2)
    if outer not in observed or len(observed) < 2:
        return None
    # Prefer a continuously seen outer edge and demand a second role over the same
    # interval. A single inferred strip cannot choose the approach lane.
    cover = _covered(bands[outer],profile.max_gap_m)
    neighbors = [(role, run) for role in observed if role != outer for run in _covered(bands[role],profile.max_gap_m)]
    common = [(max(lo,a),min(hi,b),role) for lo,hi in cover for role,(a,b) in neighbors
              if min(hi,b)-max(lo,a) >= profile.min_usable_length_m]
    if not common:
        return None
    lo, hi, other = max(common,key=lambda run: run[1]-run[0])
    if lo > .075 or hi < -.075:
        return None
    observed = tuple(role for role in observed if role in (outer,other) or any(
        a <= lo and hi <= b for a,b in _covered(bands[role],profile.max_gap_m)))
    rows = [(role, strip, points) for role in observed for strip, points in bands[role]
            if min(p.u for p in points) <= hi and max(p.u for p in points) >= lo]
    sources = frozenset(strip.source for _,strip,_ in rows)
    if source_key not in sources or len(sources) < 2:
        return None

    chords = _slope([(strip,points) for _,strip,points in rows])
    measured = [chord for chord in chords if chord[1]-chord[0] >= .12 and chord[0] <= hi and chord[1] >= lo]
    if not measured:
        return None
    angle = median(chord[2] for chord in measured)
    slope = math.tan(angle)
    scale = math.hypot(1.,slope)
    offsets = {role: median(p.v-slope*p.u for r,_,points in rows if r == role for p in points)
               for role in observed}
    shifted = [offsets[role]-(outer-role)*width*scale for role in observed]
    if max(shifted)-min(shifted) > profile.missing_outer_width_error_m*2*scale:
        return None
    anchor = median(shifted)
    station_offsets = {role:(outer-role)*width*math.sin(angle) for role in observed}
    # Perpendicular offsets also move the outer vertices along the seed axis.
    # Keep every measured edge inside its supported interval after that shift.
    lo += max(station_offsets.values())
    hi += min(station_offsets.values())
    if (hi-lo)*scale < profile.min_usable_length_m:
        return None
    bounds = [_lateral_bound(bands[role],lo-station_offsets[role],hi-station_offsets[role],
                             slope,anchor+(outer-role)*width*scale,profile) for role in observed]
    if any(bound is None for bound in bounds):
        return None
    lateral = max(bounds)+profile.station_error_m
    if len(observed) < 3:
        lateral += profile.missing_outer_width_error_m
    longitudinal = max(p.du for _,_,points in rows for p in points)
    local_heading = tuple(map(max,zip(*(
        _local_edge_heading(bands[role],lo-station_offsets[role],hi-station_offsets[role],
                            angle,profile.max_curvature_1pm)
        for role in observed))))
    return _build_fit(origin,heading,lo,hi,angle,anchor,outer,width,rows,source_key,longitudinal,
                      lateral,max(local_heading),'MULTI_FRAME_APPROACH_TAPE',local_heading=local_heading)


def _build_fit(origin, heading, lo, hi, angle, intercept, outer, width, rows, source_key,
               longitudinal, lateral, yaw, reason, *, entrance_relative=(None,None), local_heading, map_pad=None):
    slope,scale = math.tan(angle),1/math.cos(angle)
    station_s = tuple((hi-lo)*scale*i/3 for i in range(4))
    c, s = math.cos(heading), math.sin(heading)
    left = (-math.sin(heading+angle),math.cos(heading+angle))
    edges = []
    for role in range(3):
        points = []
        for t in station_s:
            u = lo+t/scale
            v = intercept+slope*u
            d = (outer-role)*width
            points.append((origin[0]+c*u-s*v+d*left[0],origin[1]+s*u+c*v+d*left[1]))
        edges.append(tuple(points))
    left_outer,divider,right_outer = edges
    centers = tuple(tuple(((a[0]+b[0])/2,(a[1]+b[1])/2) for a,b in zip(divider,edge))
                    for edge in (left_outer,right_outer))
    ids = tuple(tuple(sorted({strip.identity for r,strip,_ in rows if r == role and strip.source == source_key}))
                for role in range(3))
    if sum(map(len,ids)) > 32:
        return None
    widths = ((max(0.,width-2*lateral),width+2*lateral),)*2
    # Endpoints are clipped in u above. The error tube around the emitted
    # segment encloses the transverse uncertainty and curvature between samples.
    hypothesis = LaneHypothesis(0,ids,station_s,divider,left_outer,right_outer,*centers,
                                (True,)*4,widths,lateral,min(math.pi,yaw))
    road = RoadResult('PARTIAL',(hypothesis,),True,(reason,),source_key,True)
    sources = frozenset(strip.source for _,strip,_ in rows)
    return ApproachFit(road,sources,longitudinal,lateral,entrance_relative,local_heading,map_pad)
