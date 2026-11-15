"""Register curved tape observations to the surveyed road in the map frame."""

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SurveyArc:
    center: tuple[float, float]
    start: float
    divider_radius: float
    lane_width: float

    def __post_init__(self):
        if (type(self.center) is not tuple or len(self.center) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 100
                       for v in (*self.center, self.start, self.divider_radius, self.lane_width))
                or not -math.pi <= self.start <= math.pi
                or not 0.1 <= self.lane_width < self.divider_radius <= 5.):
            raise ValueError('invalid surveyed quarter arc in the map frame')

    def match(self, x, y, kind):
        dx, dy = x - self.center[0], y - self.center[1]
        radial = math.hypot(dx, dy)
        phase = (math.atan2(dy, dx) - self.start) % math.tau
        if radial == 0 or not 0.035 <= phase <= math.pi / 2 - 0.035:
            return None
        edge_radii = self.divider_radius - self.lane_width, self.divider_radius + self.lane_width
        radii = (self.divider_radius,) if kind == 'dash' else edge_radii
        gap = min((radial - radius for radius in radii), key=abs)
        return dx / radial, dy / radial, gap


@dataclass(frozen=True)
class CornerRegistration:
    translation: tuple[float, float]
    support: int
    residual_m: float


def register_corner(marks, pose, arcs):
    """Return a bounded translation when the observed tape constrains both axes.

    The encoder provides yaw. Arc evidence may refresh translation without
    resetting the yaw uncertainty accumulated since the last straight fit.
    """
    x, y, yaw = pose
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    rows = []
    for mark_id, mark in enumerate(marks):
        if mark.kind not in ('dash', 'solid'):
            continue
        samples = mark.points[:1] if mark.kind == 'dash' else mark.points[::3]
        for bx, by in samples:
            if not 0.6 <= bx <= 1.5 or abs(by) > 0.6:
                continue
            mx, my = x + cos_yaw * bx - sin_yaw * by, y + sin_yaw * bx + cos_yaw * by
            matches = [row for arc in arcs if (row := arc.match(mx, my, mark.kind)) is not None]
            if matches:
                nx, ny, gap = min(matches, key=lambda row: abs(row[2]))
                if abs(gap) <= 0.045:
                    rows.append((nx, ny, gap, mark_id))
    if len(rows) < 6 or len({row[3] for row in rows}) < 2:
        return None
    a = sum(nx * nx for nx, _, _, _ in rows)
    b = sum(nx * ny for nx, ny, _, _ in rows)
    d = sum(ny * ny for _, ny, _, _ in rows)
    if a * d - b * b < 0.012 * (a + d) ** 2:
        return None
    # The 9.5m tool moved the path toward the samples. Here the survey is
    # fixed, so move the encoder pose in the opposite direction.
    a, d = a + 2.0, d + 2.0
    k = a * d - b * b
    u = sum(nx * gap for nx, _, gap, _ in rows)
    v = sum(ny * gap for _, ny, gap, _ in rows)
    dx, dy = (b * v - d * u) / k, (b * u - a * v) / k
    scale = min(1.0, 0.005 / max(math.hypot(dx, dy), 1e-9))
    dx, dy = scale * dx, scale * dy
    residuals = sorted(abs(gap + nx * dx + ny * dy) for nx, ny, gap, _ in rows)
    residual = residuals[int(0.75 * len(residuals))]
    if residual > 0.020:
        return None
    return CornerRegistration((dx, dy), len(rows), residual)


def publish_survey_pose(world, pose, *, capture_us, position_bound_m, yaw_bound_rad):
    """Apply the pose sent with this WorldSnapshot and the original fit time."""
    world.reference_vehicle_pose.x, world.reference_vehicle_pose.y, world.reference_vehicle_pose.yaw = pose
    world.localization.observed_mapper_us = capture_us
    world.localization.anchor_position_bound_m = position_bound_m
    world.localization.anchor_heading_bound_rad = yaw_bound_rad


@dataclass(frozen=True)
class _Mark:
    kind: str
    points: tuple


def refresh_corner_alignment(graph, raw, mapped, previous, arcs, *, now_us):
    """Register one qualified tape graph against the configured map-frame arcs."""
    from .alignment import MapOdomAlignment

    if (graph is None or not graph.calibration_qualified or not arcs
            or previous is None or not previous.has_metadata or raw is None or mapped is None
            or not previous.observed_j_interval[1] < raw.captured_us[0]
            or not raw.captured_us[1] <= now_us < raw.captured_us[0] + previous.valid_for_us):
        return None
    roles = dict(graph.paint_roles)
    marks, errors = [], []
    for strip in graph.strips:
        if not strip.observed or strip.error_m > .025 or strip.closed:
            continue
        role = roles.get(strip.local_id, strip.outer_role)
        if role in (0, 2) or (role is None and strip.length_m >= .20):
            marks.append(_Mark('solid', strip.points_xy))
        elif role == 1 or (role is None and .025 <= strip.length_m <= .11
                           and None not in strip.endpoint_error_m):
            n = len(strip.points_xy)
            marks.append(_Mark('dash', (strip.points_xy[n//2],)))
        else:
            continue
        errors.append(strip.error_m)
    p = mapped.sample
    fit = register_corner(marks, (p.x, p.y, p.yaw), arcs)
    if fit is None:
        return None
    dx, dy = fit.translation
    # Direction is still propagated from the original odometry. A position
    # correction by itself cannot reduce the old yaw or absolute position bound.
    bound = max(p.position_error_m + math.hypot(dx, dy), fit.residual_m + max(errors))
    result = replace(mapped, sample=replace(p, x=p.x+dx, y=p.y+dy, position_error_m=bound))
    alignment = MapOdomAlignment.from_match(raw, result, observed_j_interval=raw.captured_us,
        registration_revision=previous.registration_revision+1, valid_for_us=previous.valid_for_us,
        position_drift_mps=previous.position_drift_mps, yaw_drift_rps=previous.yaw_drift_rps)
    return result, alignment
