"""Offline R45 yaw-bias injection through feedback-3 and surveyed RGB tape."""

import argparse
from collections import Counter
from copy import copy
from dataclasses import replace
import json
import math
from pathlib import Path

import cv2
import numpy as np

from replay import CAMERA_EVERY_STEPS, DRIVE_STEP_US, EncoderReplay, movement, run, wheel_speed_limit
from xenova_vehicle.mapping.alignment import MapOdomAlignment
from xenova_vehicle.mapping.registration import wrap_yaw
from xenova_vehicle.perception.geometry import ground_to_pixels
from xenova_vehicle.perception.pipeline import PerceptionPipeline


MIN_MOVING_STEP_M = .0004
TICK_AND_MOTION_ANGLE_RAD = .002


def course_strips(track):
    """Use only surveyed continuous road edges with a straight interior."""
    center = np.asarray(track['centerline'])
    half_width = track['lane_width_m'] / 2
    cross = np.arange(-.080, .081, .002)
    for fixed in (0, 1):
        along = 1 - fixed
        spans = {}
        for a, b in zip(center, np.roll(center, -1, axis=0)):
            if abs(a[fixed] - b[fixed]) < 1e-7:
                spans.setdefault(float(a[fixed]), []).extend((a[along], b[along]))
        for coordinate, stations in spans.items():
            longitudinal = np.arange(min(stations) + .055, max(stations) - .055, .012)
            if len(longitudinal) < 16:
                continue
            for sign in (-1, 1):
                locations = [None, None]
                locations[fixed] = coordinate + sign * half_width + cross[None, :]
                locations[along] = longitudinal[:, None]
                yield tuple(np.broadcast_arrays(*locations)), longitudinal, cross, fixed


class TapeHeading:
    """Extract an angle residual from RGB tape edges in the surveyed map frame."""

    def __init__(self, track):
        self.strips = tuple(course_strips(track))

    def observe(self, rgb, calibration, pose):
        x, y, yaw = pose
        c, s = math.cos(yaw), math.sin(yaw)
        measured = []
        for (mx, my), stations, cross, fixed in self.strips:
            dx, dy = mx - x, my - y
            body_x, body_y = c * dx + s * dy, -s * dx + c * dy
            if not np.any((body_x > .25) & (body_x < 1.3) & (abs(body_y) < .80)):
                continue
            u, v, valid = ground_to_pixels(body_x, body_y, calibration.rgb, calibration.plane)
            usable = valid.all(axis=1) & ((body_x > .24) & (body_x < 1.3)).all(axis=1)
            if np.count_nonzero(usable) < 16:
                continue
            pixels = cv2.remap(rgb, u[usable], v[usable], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
            white = (pixels.min(axis=2) >= 175) & (np.ptp(pixels.astype(np.int16), axis=2) < 65)
            rows = []
            for station, row in zip(stations[usable], white):
                indices = np.flatnonzero(row)
                if not 7 <= len(indices) <= 28 or indices[0] == 0 or indices[-1] == len(cross)-1:
                    continue
                if indices[-1] - indices[0] + 1 != len(indices):
                    continue
                rows.append((station, (cross[indices[0]] + cross[indices[-1]]) / 2))
            if len(rows) < 16:
                continue
            longitudinal, lateral = np.asarray(rows).T
            slope, offset = np.polyfit(longitudinal, lateral, 1)
            residual = lateral - (slope * longitudinal + offset)
            inliers = abs(residual - np.median(residual)) <= .0035
            if np.count_nonzero(inliers) < max(16, .80 * len(rows)):
                continue
            span = float(np.ptp(longitudinal[inliers]))
            if span < .18:
                continue
            slope, offset = np.polyfit(longitudinal[inliers], lateral[inliers], 1)
            # World X-constant strips slope from +Y to +X; positive yaw tilts to -X.
            angle = math.atan(float(slope)) * (-1 if fixed == 0 else 1)
            if abs(angle) < .12:
                measured.append((span, angle))
        if not measured:
            return None
        longest, angle = max(measured)
        agreeing = [(span, item) for span, item in measured if abs(item - angle) <= .010]
        if len(agreeing) < (len(measured) + 1) // 2:
            return None
        residual = sum(span ** 2 * item for span, item in agreeing) / sum(span ** 2 for span, _ in agreeing)
        return residual, longest, len(agreeing)


class ScalarBiasFilter:
    """Estimate one constant yaw rate from surveyed angle residuals."""

    def __init__(self):
        self.rate = 0.
        self.variance = .004 ** 2
        self.window = []
        self.last_update = 0.
        self.updates = []

    def observe(self, at, motion_s, measured):
        if at < 2.:
            return
        if measured is not None:
            self.window.append((at, motion_s, measured[0]))
        if len(self.window) < 4 or at - self.last_update < 1.:
            return
        window = [point for point in self.window if at - point[0] <= 1.0]
        self.window = []
        if len(window) < 4:
            return
        stamp = float(np.median([point[1] for point in window]))
        angle = float(np.median([point[2] for point in window]))
        variance = self.variance + .00006 ** 2 * (at - self.last_update)
        noise = .004 ** 2
        gain = variance * stamp / (stamp * stamp * variance + noise)
        self.rate += gain * (angle - stamp * self.rate)
        self.variance = (1. - gain * stamp) * variance
        self.last_update = at
        self.updates.append((at, angle, self.rate, math.sqrt(self.variance)))

    def correction_rate(self, at, minimum_sigma):
        elapsed = max(0., at - self.last_update)
        sigma = math.sqrt(self.variance + .00006 ** 2 * elapsed)
        return self.rate if abs(self.rate) > minimum_sigma * sigma else 0.


class ResidualBounds:
    """Bound the residual of a constant-rate correction over an encoder path."""

    def __init__(self, rate_limit):
        self.interval = (-rate_limit, rate_limit)
        self.contradiction_at = None
        self.correction = 0.
        self.samples = {}
        self.steps = []
        self.update_count = 0

    def observe(self, at, moving_time, measured):
        if self.interval is None or measured is None or moving_time < 2.:
            return
        angle, span, _ = measured
        # Development hypotheses for surveyed R45, checked against independent replay scores.
        image_error = .0035 if span >= .45 else .011
        error = image_error + TICK_AND_MOTION_ANGLE_RAD
        lo, hi = self.interval
        lower = max(lo, math.nextafter((angle - error) / moving_time, -math.inf))
        upper = min(hi, math.nextafter((angle + error) / moving_time, math.inf))
        if lower > upper:
            self.interval = None
            self.contradiction_at = at
            return
        if (lower, upper) != self.interval:
            self.update_count += 1
        self.interval = lower, upper

    def append(self, at, motion_s, motion_step_s, applied_rate, distance, dx, dy):
        correction_step = applied_rate * motion_step_s
        self.correction += correction_step
        self.steps.append((motion_s - motion_step_s / 2, self.correction - correction_step / 2,
                           motion_step_s, correction_step, abs(distance), dx, dy))
        self.samples[at] = len(self.steps), motion_s, self.correction

    def between(self, start_us, end_us):
        if self.interval is None or start_us not in self.samples or end_us not in self.samples:
            return None
        start, moving, correction = self.samples[start_us]
        end, end_moving, end_correction = self.samples[end_us]
        if start == end:
            return 0., 0.
        batch = np.asarray(self.steps[min(start,end):max(start,end)])
        midpoint, applied, step, applied_step, distance, dx, dy = batch.T
        yaw_errors = []
        translation_linear = []
        midpoint_errors = []
        step_errors = []
        for rate in self.interval:
            yaw_errors.append(abs(rate * (end_moving - moving) - (end_correction - correction)))
            angle = rate * (midpoint - moving) - (applied - correction)
            # First-order rotation of each corrected travel vector, summed before taking the norm.
            translation_linear.append(math.hypot(float(angle @ dx), float(angle @ dy)))
            midpoint_errors.append(abs(angle))
            step_errors.append(abs(rate * step - applied_step))
        worst_midpoint = np.maximum(*midpoint_errors)
        worst_step = np.maximum(*step_errors)
        # Rotation remainder is <= d*a^2/2. sinc(turn/2) is 1/4-Lipschitz in turn.
        translation = max(translation_linear) + float(distance @ (.5*worst_midpoint**2 + .25*worst_step))
        return math.nextafter(translation + 1e-8, math.inf), math.nextafter(max(yaw_errors) + 1e-9, math.inf)

    def project(self, original_project, alignment, odom, elapsed_us):
        if alignment._matched_poses is None or not alignment.has_metadata:
            return original_project(alignment, odom, elapsed_us)
        original, _ = alignment._matched_poses
        if (original.captured_us[0] != original.captured_us[1]
                or odom.captured_us[0] != odom.captured_us[1]):
            return original_project(alignment, odom, elapsed_us)
        bounds = self.between(original.captured_us[0], odom.captured_us[0])
        if bounds is None:
            return original_project(alignment, odom, elapsed_us)
        # Only the test's injected yaw-rate term is replaced; retain the rest of the original budget.
        base_alignment = copy(alignment)
        object.__setattr__(base_alignment, 'yaw_drift_rps', 0.)
        base = original_project(base_alignment, odom, elapsed_us)
        position, yaw = bounds
        sample = replace(base.sample, position_error_m=base.sample.position_error_m + position,
            yaw_error_rad=min(math.pi,base.sample.yaw_error_rad + yaw))
        return replace(base, sample=sample)


class InjectedEncoderReplay(EncoderReplay):
    """Inject a repeatable rate into the simulator wheel stream before the Gateway."""

    def __init__(self, calibration, poses, samples, indices, motion_profile, *, rate, start_at_zero=False):
        super().__init__(calibration, poses, motion_profile,
            wheel_speed_limit_mps=wheel_speed_limit(samples,motion_profile['encoder']),start_at_zero=start_at_zero)
        self.samples, self.frames = samples, indices
        self.rate = rate
        self.start_us = None
        self.camera_rows = []

    def advance(self, index, at):
        if index == 0:
            self.start_us = at
            return self.warm_up(at)
        start, end = self.frames[index-1:index+1]
        base = at - end * DRIVE_STEP_US
        for step in range(start + 1, end + 1):
            distance, turn = movement(self.samples[step-1], self.samples[step])
            steer = math.atan(self.odom.config.wheelbase_m * turn / distance) if distance else 0.
            bias = self.rate * DRIVE_STEP_US / 1e6 if abs(distance) > MIN_MOVING_STEP_M else 0.
            self._sample(base + step * DRIVE_STEP_US, distance, turn + bias, steer)

    def observe(self, observation):
        observation, pose = super().observe(observation)
        raw = pose.sample.x, pose.sample.y, pose.sample.yaw
        at = (observation.rgb.stamp.captured_us[0] - self.start_us) / 1e6
        self.camera_rows.append(dict(at=at,raw=raw,corrected=raw,visual=None))
        return observation, pose


class BiasedEncoderReplay(InjectedEncoderReplay):
    def __init__(self, calibration, poses, samples, indices, motion_profile, *, rate, estimator,
                 apply_filter, minimum_sigma, residual_bounds):
        super().__init__(calibration,poses,samples,indices,motion_profile,rate=rate)
        self.estimator = estimator
        self.apply_filter = apply_filter
        self.minimum_sigma = minimum_sigma
        self.residual_bounds = residual_bounds
        self.filter = ScalarBiasFilter()
        self.corrected = None
        self.previous_raw = None
        self.motion_s = 0.

    def _receive_vehicle(self, row):
        super()._receive_vehicle(row)
        raw = tuple(row['odom_pose'][key] for key in ('x','y','yaw'))
        at = row['source_us']
        if self.previous_raw is None:
            self.corrected = raw
            if self.residual_bounds is not None:
                self.residual_bounds.samples[at] = 0, 0., 0.
        else:
            before, last_at = self.previous_raw
            distance, turn = movement({'pose':before}, {'pose':raw})
            applied_rate, motion_step_s = 0., 0.
            if abs(distance) > MIN_MOVING_STEP_M:
                motion_step_s = max(0, at - max(last_at, self.start_us)) / 1e6
                self.motion_s += motion_step_s
                applied_rate = self.filter.correction_rate((at - self.start_us) / 1e6, self.minimum_sigma)
                turn -= applied_rate * motion_step_s
            x, y, yaw = self.corrected
            half = turn / 2
            arc = distance * (math.sin(half) / half if half else 1.)
            dx, dy = arc*math.cos(yaw+half), arc*math.sin(yaw+half)
            self.corrected = x + dx, y + dy, wrap_yaw(yaw+turn)
            if self.residual_bounds is not None:
                self.residual_bounds.append(at, self.motion_s, motion_step_s, applied_rate, distance, dx, dy)
        self.previous_raw = raw, at

    def observe(self, observation):
        observation, pose = EncoderReplay.observe(self, observation)
        raw = pose.sample.x, pose.sample.y, pose.sample.yaw
        at = (observation.rgb.stamp.captured_us[0] - self.start_us) / 1e6
        measured = self.estimator.observe(observation.rgb.array(), self.calibration, raw)
        self.filter.observe(at, self.motion_s, measured)
        if self.residual_bounds is not None:
            self.residual_bounds.observe(at, self.motion_s, measured)
        self.camera_rows.append(dict(at=at, raw=raw, corrected=self.corrected, visual=measured,
            rate=self.filter.rate, sigma=math.sqrt(self.filter.variance),
            correction_rate=self.filter.correction_rate(at, self.minimum_sigma),
            rate_interval=None if self.residual_bounds is None else self.residual_bounds.interval,
            motion_s=self.motion_s))
        # The corrected trajectory no longer agrees exactly with raw wheel ticks.
        # Both arms use the same fallback bounds and original capture timestamps.
        sample = pose.sample
        if self.apply_filter:
            sample = replace(sample, x=self.corrected[0], y=self.corrected[1], yaw=self.corrected[2])
        if self.residual_bounds is not None:
            # The absolute fallback estimate still needs the encoder's resolution at seeding.
            tick = self.odom.config.metres_per_tick_upper
            yaw_tick = 2 * tick / self.odom.config.track_width_m
            sample = replace(sample, position_error_m=sample.position_error_m + tick,
                yaw_error_rad=sample.yaw_error_rad + yaw_tick)
        return observation, replace(pose, sample=sample, wheel_capture=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rate', type=float, required=True)
    parser.add_argument('--filter', action='store_true')
    parser.add_argument('--min-sigma', type=float, default=0.)
    parser.add_argument('--residual-bounds', action='store_true')
    parser.add_argument('--online-map', action='store_true', help='run the normal pipeline from an empty map')
    parser.add_argument('--no-online-filter', action='store_true', help='disable the online filter for the paired control')
    parser.add_argument('--rate-limit', type=float, default=.001)
    parser.add_argument('--position-drift-mps', type=float, help='override the replay-only position drift budget')
    parser.add_argument('--yaw-drift-rps', type=float, help='override the replay-only yaw drift budget')
    parser.add_argument('--ground-drift-mps', type=float, nargs=2, metavar=('X','Y'), default=(0.,0.),
                        help='inject a scene-coordinate position drift without changing the encoder path')
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--scene', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--rgb-eeprom', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    if options.residual_bounds and not options.filter:
        parser.error('--residual-bounds requires --filter')
    if options.online_map and (options.filter or options.residual_bounds):
        parser.error('--online-map uses the runtime filter instead of the surveyed prototype')
    if options.no_online_filter and not options.online_map:
        parser.error('--no-online-filter requires --online-map')
    if (not all(math.isfinite(value) for value in (options.rate,options.rate_limit,options.min_sigma))
            or options.rate_limit <= 0. or options.min_sigma < 0.):
        parser.error('rates and threshold must be finite, with a positive rate limit and nonnegative threshold')
    overrides = {key: getattr(options,key) for key in ('position_drift_mps','yaw_drift_rps')
                 if getattr(options,key) is not None}
    if any(not math.isfinite(value) or value < 0 for value in overrides.values()):
        parser.error('localization drift budgets must be finite and nonnegative')
    if not all(math.isfinite(value) for value in options.ground_drift_mps):
        parser.error('injected ground drift must be finite')
    if (overrides or any(options.ground_drift_mps)) and not options.online_map:
        parser.error('ground drift and localization budget overrides require --online-map')
    drive = json.loads(options.trace.read_bytes())
    samples = tuple(drive['trace']) + (dict(pose=drive['final'], step=len(drive['trace'])),)
    if len(samples) < 2 or any(sample['step'] != step for step,sample in enumerate(samples)):
        parser.error('trace must provide consecutive 20 ms steps and a final pose')
    indices = tuple(range(0, len(samples)-1, CAMERA_EVERY_STEPS)) + (len(samples)-1,)
    route = tuple((i*DRIVE_STEP_US, tuple(samples[i]['pose'])) for i in indices)
    estimator = None if options.online_map else TapeHeading(json.loads(options.scene.read_bytes())['track'])
    motion_profile = json.loads((options.profiles/'development-motion.json').read_bytes())
    residual_bounds = ResidualBounds(options.rate_limit) if options.residual_bounds else None
    project = MapOdomAlignment._project
    propagate = PerceptionPipeline._propagate_odometry
    propagation = {}

    def project_residual(alignment, odom, elapsed_us):
        return residual_bounds.project(project, alignment, odom, elapsed_us)

    def observe_propagation(core, odom):
        prior, selected = propagate(core, odom)
        details = []
        for alignment in (core._propagation_anchor or core._alignment, core._alignment):
            sample = alignment.apply(odom).sample
            details.append((alignment.observed_j_interval[0], sample.x, sample.y, sample.yaw,
                            sample.position_error_m, sample.yaw_error_rad))
        propagation[odom.captured_us[0]] = dict(candidates=details,
            chosen=(selected.sample.position_error_m, selected.sample.yaw_error_rad))
        return prior, selected

    def factory(calibration, poses):
        if options.online_map:
            return InjectedEncoderReplay(calibration,poses,samples,indices,motion_profile,rate=options.rate,
                                         start_at_zero=True)
        return BiasedEncoderReplay(calibration, poses, samples, indices, motion_profile,
            rate=options.rate, estimator=estimator, apply_filter=options.filter, minimum_sigma=options.min_sigma,
            residual_bounds=residual_bounds)

    try:
        if residual_bounds is not None:
            MapOdomAlignment._project = project_residual
        if not options.online_map:
            PerceptionPipeline._propagate_odometry = observe_propagation
        online_filter = options.online_map and not options.no_online_filter
        frames,rig,cones = run(route,options.scene,options.profiles,options.rgb_eeprom,factory,
                               estimate_yaw_bias=online_filter,map_local_origin=options.online_map,
                               localization_overrides=overrides,ground_drift_mps=options.ground_drift_mps)
    finally:
        MapOdomAlignment._project = project
        PerceptionPipeline._propagate_odometry = propagate
    assert rig.accepted >= len(samples) and rig.sink.rejected == 0, (rig.stats(), len(samples))
    assert all(frame['elapsed_s'] == row['at'] for frame, row in zip(frames, rig.camera_rows, strict=True))
    scored = []
    for frame,row in zip(frames,rig.camera_rows,strict=True):
        # Oracle pose is read only here, after the mapper and filter have completed.
        truth = frame['truth']
        errors = {}
        for key in ('raw', 'corrected'):
            x, y, yaw = row[key]
            errors[f'{key}_error'] = (math.hypot(x-truth[0], y-truth[1]), wrap_yaw(yaw-truth[2]))
        if row['visual'] is not None:
            errors['visual_angle_residual'] = wrap_yaw(row['visual'][0] - errors['raw_error'][1])
        if options.online_map and frame['mapped'] is not None:
            mapped = frame['mapped']
            errors['map_error'] = (math.dist(mapped[:2],truth[:2]),wrap_yaw(mapped[2]-truth[2]))
        scored.append(dict(**row, **errors, status=frame['status'], mapped=frame['mapped'],
            bound=frame['position_error_m'], yaw_bound=frame['yaw_error_rad'], qualified=frame['qualified'], truth=truth,
            yaw_bias=frame['yaw_bias'],propagation=propagation.get(rig.start_us + round(row['at']*1e6))))
    metrics = dict(rate=options.rate, filter_applied=options.filter or online_filter, minimum_sigma=options.min_sigma,
        residual_bounds=options.residual_bounds or online_filter, rate_limit=options.rate_limit,
        interval_contradiction_at=None if residual_bounds is None else residual_bounds.contradiction_at,
        interval_update_count=0 if residual_bounds is None else residual_bounds.update_count,
        samples=rig.stats(),
        updates=[] if options.online_map else rig.filter.updates,
        online_map=options.online_map,statuses=dict(Counter(frame['status'] for frame in frames)),
        pose_frames=sum(frame['mapped'] is not None for frame in frames), frames=len(frames),
        cones=cones, rows=scored)
    if overrides or any(options.ground_drift_mps):
        metrics.update(localization_overrides=overrides, ground_drift_mps=options.ground_drift_mps)
    options.output.write_text(json.dumps(metrics,indent=2,allow_nan=False),encoding='utf-8')
    print({key:value for key,value in metrics.items() if key!='rows'}, flush=True)


if __name__ == '__main__':
    main()
