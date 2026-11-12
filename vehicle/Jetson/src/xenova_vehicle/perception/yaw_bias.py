"""Estimate encoder yaw drift from repeated observations of the same mapped tape."""

from collections import deque
from dataclasses import replace
import math
from statistics import median

from .geometry import ground_to_pixels, project_ground, project_ground_bounds
from ..mapping.registration import transform_point, wrap_yaw


# The RGB angular allowances are provisional and need camera/mount validation.
_MIN_SPAN_M = .36
_ANGLE_ERROR_RAD = .0035
_PAIR_MIN_S = 1.5
_MAX_AGE_US = 15_000_000
_RATE_WALK = .000025
_MAX_LINES = 24


class YawBias:
    """Keep only recent odometry and a bounded set of frozen line observations."""

    def __init__(self):
        self.path = deque(maxlen=512)
        self.lines = {}
        self.paired_us = {}
        self.interval = None
        self.rate = 0.
        self.variance = 0.
        self.updates = 0
        self.updated_us = None
        self.applied = False
        self.applied_rate = None

    def add_pose(self, odom):
        if self.path and self.path[-1] == odom:
            return
        if self.path:
            prev = self.path[-1]
            a, b = prev.wheel_capture, odom.wheel_capture
            if (prev.sample.context != odom.sample.context
                    or not 0 < odom.sample.at_us - prev.sample.at_us <= 500_000
                    or a is None or b is None or a.progress.segment != b.progress.segment):
                self.__init__()
        self.path.append(odom)

    def observe(self, rgb, calibration, raw, fitted, common, reference, limit):
        """Observe only supported tape in an accepted match against an earlier capture."""
        if not limit or raw.wheel_capture is None:
            return
        at = raw.sample.at_us
        self.lines = {key: value for key, value in self.lines.items()
                      if at - value[0] <= _MAX_AGE_US}
        self.paired_us = {key: value for key, value in self.paired_us.items() if key in self.lines}
        per_strip = {}
        for point in common:
            per_strip.setdefault(point.component, []).append(set(point.compatible))
        supported = set().union(*(set.intersection(*matches) for matches in per_strip.values()
                                 if len(matches) >= 2))
        # Read only the oldest few eligible segments, preferring repeated observations.
        candidates = [s for s in reference.segments if s.component in supported
                      and math.dist(s.a, s.b) >= _MIN_SPAN_M + .08]
        candidates.sort(key=lambda s: s.component not in self.lines)
        measurements = []
        for segment in candidates[:4]:
            sampled = _tape_offsets(rgb, calibration, fitted.sample, segment)
            if sampled is None:
                continue
            offset, slots = sampled
            previous = self.lines.get(segment.component)
            if previous is None:
                if len(self.lines) == _MAX_LINES:
                    oldest = next(iter(self.lines))
                    del self.lines[oldest]
                    self.paired_us.pop(oldest,None)
                self.lines[segment.component] = (at, raw, offset, slots)
                continue
            first_us, first_raw, first_offset, first_slots = previous
            if at - first_us < _PAIR_MIN_S * 1e6 or at - self.paired_us.get(segment.component,0) < 800_000:
                continue
            motion = self._motion_between(first_raw, raw)
            if motion is None:
                self.lines[segment.component] = (at, raw, offset, slots)
                continue
            moving, clock_pad, wheel_angle = motion
            if moving < _PAIR_MIN_S:
                continue
            shared = slots.keys() & first_slots.keys()
            span = (max(shared)-min(shared))*.018 if shared else 0.
            if len(shared) < 21 or span < _MIN_SPAN_M:
                continue
            # Fit the change on identical parts of the same line. Its stored heading
            # and its fixed map rotation cancel out of the difference.
            keys = sorted(shared)
            before = _slope(keys, [first_slots[k][0] for k in keys])
            after = _slope(keys, [slots[k][0] for k in keys])
            if before is None or after is None or max(abs(before),abs(after)) > .12:
                continue
            raw_turn = raw.sample.yaw-first_raw.sample.yaw
            change = wrap_yaw(math.atan(after)-math.atan(before) + offset-first_offset + raw_turn)
            optical_error = _calibration_pad(calibration,first_slots,keys,before)
            optical_error += _calibration_pad(calibration,slots,keys,after)
            if optical_error >= math.pi:
                continue
            error = 2 * _ANGLE_ERROR_RAD * max(1.,.45/span) + optical_error + wheel_angle + limit*clock_pad
            measurements.append(((change-error)/moving,(change+error)/moving,change/moving,optical_error/moving))
            self.paired_us[segment.component] = at
        if not measurements:
            return
        if self.updated_us is not None and at-self.updated_us > _MAX_AGE_US:
            self.interval = self.updated_us = None
            self.updates = 0
        lo = max(-limit, *(value[0] for value in measurements))
        hi = min(limit, *(value[1] for value in measurements))
        if self.interval is not None:
            age_s = (at-self.updated_us)/1e6
            lo = max(lo, self.interval[0]-_RATE_WALK*age_s)
            hi = min(hi, self.interval[1]+_RATE_WALK*age_s)
        if lo > hi:
            # Contradictory evidence can be a wrong correspondence or a changed bias.
            self.interval = self.updated_us = None
            self.updates = 0
            self.lines.clear()
            self.paired_us.clear()
            return
        variance = (limit*limit if self.interval is None else
                    self.variance + (_RATE_WALK*(at-self.updated_us)/1e6)**2)
        measurement_var = max(((hi-lo)/2)**2,(limit/8)**2,median(v[3]**2 for v in measurements))
        gain = variance/(variance+measurement_var)
        old_rate = 0. if self.interval is None else self.rate
        self.rate = min(hi, max(lo, old_rate + gain*(median(v[2] for v in measurements)-old_rate)))
        self.interval = lo, hi
        self.variance = (1-gain)*variance
        self.updated_us = at
        self.updates += 1

    def _steps(self, start, end):
        if not self.path or self.path[-1] != end:
            return ()
        steps = []
        b = end
        for a in reversed(self.path):
            if a == b:
                continue
            first, last = a.wheel_capture, b.wheel_capture
            if first is None or last is None or first.progress.segment != last.progress.segment:
                return ()
            dt = (b.sample.at_us-a.sample.at_us)/1e6
            travel = last.progress.travel_m-first.progress.travel_m
            if dt <= 0 or travel < 0:
                return ()
            steps.append((b.sample.x-a.sample.x, b.sample.y-a.sample.y, dt, travel))
            if a == start:
                return tuple(reversed(steps))
            if a.sample.at_us <= start.sample.at_us:
                break
            b = a
        return ()

    def _motion_between(self, start, end):
        steps = self._steps(start, end)
        if not steps:
            return None
        first, last = start.wheel_capture, end.wheel_capture
        moving = sum(dt for _, _, dt, travel in steps if travel > 0.)
        timing = (start.captured_us[1]-start.captured_us[0] + end.captured_us[1]-end.captured_us[0])/1e6
        # Fixed encoder quantization and reported capture angle padding survive the pairing.
        model = first.progress.profile
        wheel_distance = abs(last.progress.wheel_travel_m-first.progress.wheel_travel_m)/2
        wheel_angle = (2*model.metres_per_tick_upper/model.track_width_m
                       + model.heading_error_per_m*wheel_distance
                       + first.yaw_pad_rad + last.yaw_pad_rad
                       + abs(last.progress.yaw_roundoff_rad-first.progress.yaw_roundoff_rad))
        return moving, timing, wheel_angle

    def project(self, alignment, raw, profile):
        """Use the residual interval only with a continuous raw wheel path and fresh evidence."""
        legacy = alignment.apply(raw)
        limit = alignment.yaw_drift_rps
        if not limit or self.interval is None or self.updates < 2 or alignment._matched_poses is None:
            return legacy
        age_us = raw.sample.at_us-self.updated_us
        if age_us < 0:
            return legacy
        start, _ = alignment._matched_poses
        steps = self._steps(start, raw)
        if not steps or not any(travel>0 for _,_,_,travel in steps):
            return legacy
        width = _RATE_WALK*age_us/1e6
        if age_us <= _MAX_AGE_US:
            lo, hi = max(-limit,self.interval[0]-width), min(limit,self.interval[1]+width)
            centered = (lo+hi)/2
            estimate = self.rate if abs(self.rate) >= math.sqrt(self.variance+width**2) else centered
        else:
            # An old rate estimate cannot move the pose. The wheel path still allows
            # propagation with the full configured interval around zero.
            lo,hi,centered,estimate = -limit,limit,0.,0.
        def bound_score(candidate):
            pose = candidate.sample
            return max(pose.position_error_m/max(profile.max_position_error_m,1e-12),
                       pose.yaw_error_rad/max(profile.max_yaw_error_rad,1e-12))
        candidates = [(legacy,None)]
        legacy_score = bound_score(legacy)
        # Try a damped correction before giving up the signed estimate entirely.
        for rate in dict.fromkeys((estimate,(estimate+centered)/2,centered)):
            projected = self._project_rate(alignment,raw,start,steps,lo,hi,rate)
            if (math.hypot(projected.sample.x-legacy.sample.x,projected.sample.y-legacy.sample.y)
                    <= legacy.sample.position_error_m+projected.sample.position_error_m
                    and abs(wrap_yaw(projected.sample.yaw-legacy.sample.yaw))
                    <= legacy.sample.yaw_error_rad+projected.sample.yaw_error_rad):
                candidates.append((projected,rate))
                if rate != centered and bound_score(projected) <= min(1.,legacy_score):
                    self.applied_rate,self.applied = rate,True
                    return projected
        # If the point estimate costs more than the existing budget, the interval
        # midpoint can still propagate a smaller, defensible error.
        selected,self.applied_rate = min(candidates,key=lambda item:bound_score(item[0]))
        self.applied = self.applied_rate is not None
        return selected

    @staticmethod
    def _project_rate(alignment,raw,start,steps,lo,hi,correction):
        limit = alignment.yaw_drift_rps
        residual = max(abs(lo-correction),abs(hi-correction))
        moving = stationary = sx = sy = derivative_x = derivative_y = remainder = timing_error = 0.
        max_rate = max(abs(lo),abs(hi),abs(correction))
        for dx, dy, dt, travel in steps:
            if travel == 0.:
                stationary += dt
                sx += dx
                sy += dy
                continue
            middle = moving + dt/2
            u, v = transform_point((dx,dy),(0.,0.,-correction*middle))
            sx += u
            sy += v
            derivative_x += middle*v
            derivative_y -= middle*u
            remainder += math.hypot(u,v)*middle**2
            timing_error += max_rate*dt*travel/2
            moving += dt
        sample = raw.sample
        dx, dy = sx-(sample.x-start.sample.x), sy-(sample.y-start.sample.y)
        dx, dy = transform_point((dx,dy),(0.,0.,alignment.pose[2]))
        capture_pad = max(0., (raw.captured_us[1]-start.captured_us[0])/1e6-moving-stationary)
        base = alignment.apply(raw, yaw_drift_rad=limit*(stationary+capture_pad))
        pose = base.sample
        bias_position = (residual*math.hypot(derivative_x,derivative_y)
                         + .5*residual**2*remainder + timing_error
                         + math.hypot(dx,dy)*pose.yaw_error_rad)
        return replace(base,sample=replace(pose,x=pose.x+dx,y=pose.y+dy,
            yaw=wrap_yaw(pose.yaw-correction*moving),
            position_error_m=math.nextafter(pose.position_error_m+bias_position,math.inf),
            yaw_error_rad=min(math.pi,math.nextafter(pose.yaw_error_rad+residual*moving,math.inf))))

    def summary(self):
        return dict(rate_rps=None if self.interval is None else self.rate,
                    interval_rps=self.interval,updates=self.updates,applied=self.applied,
                    applied_rate_rps=self.applied_rate if self.applied else None)


def _slope(keys, values):
    import numpy as np
    x = np.asarray(keys)*.018
    y = np.asarray(values)
    x -= x.mean()
    slope = float(x @ (y-y.mean()) / (x @ x))
    residual = y - y.mean() - slope*x
    if float(np.max(abs(residual))) > .004:
        return None
    return slope


def _calibration_pad(calibration, slots, keys, slope):
    camera,plane = calibration.rgb,calibration.plane
    if not any((camera.ray_error_rad,camera.rotation_error_rad,camera.translation_error_m,
                plane.normal_error_rad,plane.offset_error_m)):
        return 0.
    middle = sum(keys)/len(keys)
    deltas = [(key-middle)*.018 for key in keys]
    square_sum = sum(delta*delta for delta in deltas)
    pad = 0.
    for key,delta in zip(keys,deltas):
        _,u,v = slots[key]
        point = project_ground(u,v,camera,plane)
        bounds = project_ground_bounds(u,v,camera,plane,pixel_error=0.)
        if point is None or bounds is None:
            return math.pi
        radius = math.hypot(*(max(abs(value-lo),abs(value-hi))
                              for value,lo,hi in zip(point[:2],bounds.lower,bounds.upper)))
        pad += abs(delta)*radius
    # atan is 1-Lipschitz. Along-line displacement also contributes to cross-line slope.
    return (1+abs(slope))*pad/square_sum


def _tape_offsets(rgb, calibration, pose, segment):
    """Sample a fixed map chord and report observed tape centers along the chord."""
    import cv2
    import numpy as np
    a,b = segment.a,segment.b
    length = math.dist(a,b)
    ux,uy = (b[0]-a[0])/length,(b[1]-a[1])/length
    stations = np.arange(.04,length-.04,.018)
    cross = np.arange(-.065,.066,.002)
    x = a[0]+ux*stations[:,None]-uy*cross[None,:]-pose.x
    y = a[1]+uy*stations[:,None]+ux*cross[None,:]-pose.y
    c,s = math.cos(pose.yaw),math.sin(pose.yaw)
    forward,lateral = c*x+s*y,-s*x+c*y
    u,v,valid = ground_to_pixels(forward,lateral,calibration.rgb,calibration.plane)
    valid = valid & (forward>.24) & (forward<1.5)
    indices = np.flatnonzero(valid.all(axis=1))
    if len(indices) < 21:
        return None
    pixels = cv2.remap(rgb,u[indices],v[indices],cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
    white = (pixels.min(axis=2)>=175)&(np.ptp(pixels,axis=2)<65)
    offsets = {}
    for k,row in zip(indices,white):
        hits = np.flatnonzero(row)
        if (7 <= len(hits) <= 28 and hits[0]>0 and hits[-1]<len(cross)-1
                and hits[-1]-hits[0]+1==len(hits)):
            a,b = hits[0],hits[-1]
            offsets[int(k)] = (float((cross[a]+cross[b])/2),
                               float((u[k,a]+u[k,b])/2),float((v[k,a]+v[k,b])/2))
    if len(offsets) < 21 or (max(offsets)-min(offsets))*.018 < _MIN_SPAN_M:
        return None
    if _slope(tuple(offsets),[value[0] for value in offsets.values()]) is None:
        return None
    return -pose.yaw, offsets
