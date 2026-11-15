"""Measured two-lane rounded rectangle in the mapper's existing coordinates."""

from bisect import bisect_right
from dataclasses import dataclass, field
import math

from .survey_registration import SurveyArc


@dataclass(frozen=True)
class SurveyCourse:
    origin: tuple[float, float]
    long_m: float
    short_m: float
    radius_m: float
    lane_width_m: float
    heading_rad: float = 0.
    points: tuple = field(init=False, repr=False)
    stations: tuple = field(init=False, repr=False)
    lines: tuple = field(init=False, repr=False)
    arcs: tuple = field(init=False, repr=False)

    def __post_init__(self):
        bounds = (*self.origin, self.long_m, self.short_m, self.radius_m, self.lane_width_m, self.heading_rad)
        if (len(self.origin) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 100 for v in bounds)
                or not .2 <= self.long_m <= 4. or not .1 <= self.short_m <= self.long_m
                or not .1 <= self.lane_width_m < self.radius_m <= 2. or abs(self.heading_rad) > math.pi):
            raise ValueError('invalid measured rounded two-lane course')
        x, y, r = self.long_m, self.short_m, self.radius_m
        chunks = ((x, lambda f: (x*f, -r)),
                  (r*math.pi/2, lambda f: (x+r*math.sin(f*math.pi/2), -r*math.cos(f*math.pi/2))),
                  (y, lambda f: (x+r, y*f)),
                  (r*math.pi/2, lambda f: (x+r*math.cos(f*math.pi/2), y+r*math.sin(f*math.pi/2))),
                  (x, lambda f: (x*(1-f), y+r)),
                  (r*math.pi/2, lambda f: (-r*math.sin(f*math.pi/2), y+r*math.cos(f*math.pi/2))),
                  (y, lambda f: (-r, y*(1-f))),
                  (r*math.pi/2, lambda f: (-r*math.cos(f*math.pi/2), -r*math.sin(f*math.pi/2))))
        stations, points = [0.], []
        for length, point in chunks:
            count = math.ceil(length/.065)
            points.extend(self._world(*point(i/count)) for i in range(count))
        for a, b in zip(points, points[1:]+points[:1]):
            stations.append(stations[-1]+math.dist(a, b))
        lines = [[] for _ in range(5)]
        for i, p in enumerate(points):
            a, b = points[i-1], points[(i+1) % len(points)]
            yaw = math.atan2(p[1]-a[1], p[0]-a[0])
            turn = math.remainder(math.atan2(b[1]-p[1], b[0]-p[0])-yaw, math.tau)/2
            for branch in range(-2, 3):
                off = branch*self.lane_width_m/2/math.cos(turn)
                lines[branch+2].append((p[0]-off*math.sin(yaw+turn), p[1]+off*math.cos(yaw+turn)))
        arcs = tuple(SurveyArc(self._world(a, b), math.remainder(heading+self.heading_rad, math.tau), r, self.lane_width_m)
                     for a, b, heading in ((x, 0., -math.pi/2), (x, y, 0.), (0., y, math.pi/2), (0., 0., math.pi)))
        for name, value in (('points', tuple(points)), ('stations', tuple(stations)),
                            ('lines', tuple(tuple(line) for line in lines)), ('arcs', arcs)):
            object.__setattr__(self, name, value)

    def _world(self, x, y):
        c, s = math.cos(self.heading_rad), math.sin(self.heading_rad)
        return self.origin[0]+x*c-y*s, self.origin[1]+x*s+y*c

    @property
    def length(self):
        return self.stations[-1]

    def pose(self, station, branch):
        station %= self.length
        i = bisect_right(self.stations, station)-1
        line = self.lines[branch+2]
        a, b = line[i], line[(i+1) % len(line)]
        frac = (station-self.stations[i])/(self.stations[i+1]-self.stations[i])
        return a[0]+frac*(b[0]-a[0]), a[1]+frac*(b[1]-a[1]), math.atan2(b[1]-a[1], b[0]-a[0])

    def project(self, x, y):
        best = math.inf, 0.
        for i, (ax, ay) in enumerate(self.points):
            bx, by = self.points[(i+1) % len(self.points)]
            dx, dy = bx-ax, by-ay
            ds = self.stations[i+1]-self.stations[i]
            f = max(0., min(1., ((x-ax)*dx+(y-ay)*dy)/ds**2))
            best = min(best, ((x-ax-f*dx)**2+(y-ay-f*dy)**2, self.stations[i]+f*ds))
        return best[1]

    def window(self, station):
        left, right = station-.51, station+1.12
        lap = math.floor(left/self.length)
        positions = [p+k*self.length for k in (lap, lap+1) for p in self.stations[:-1]
                     if left+.008 < p+k*self.length < right-.008]
        if len(positions) > 30:
            positions = [positions[round(i*(len(positions)-1)/29)] for i in range(30)]
        return [left, *positions, right]
