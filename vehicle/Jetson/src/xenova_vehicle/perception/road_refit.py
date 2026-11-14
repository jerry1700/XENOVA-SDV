"""같은 경계로 대응한 여러 RGB의 흰선을 국소 차로 구간으로 맞춘다."""

from dataclasses import dataclass
import math

from .road_normals import axis_error, heading_error
from ..mapping.registration import transform_point, wrap_yaw


_MIN_CHORD_M = .04
_MIN_FIT_SPAN_M = .32
_MAX_WINDOW_M = .52
_MAX_CHORD_TURN_RAD = .40
_MAX_AXIS_RESIDUAL_M = .055
_HEADING_NEIGHBORHOOD_M = .22
_MIN_HEADING_FRAMES = 4
_MIN_HEADING_SPREAD_M = .002
_POSITION_NEIGHBORHOOD_M = .12
_MIN_POSITION_FRAMES = 4


@dataclass(frozen=True)
class _MarkingChord:
    source: tuple
    identity: int
    u: tuple[float, float]
    v: tuple[float, float]
    heading: float
    coverage: tuple[float,float]
    observations: tuple
    dash_sample: tuple[float, float, float] | None

    def at(self, u):
        return self.v[0]+(self.v[1]-self.v[0])*(u-self.u[0])/(self.u[1]-self.u[0])

    def lateral_bound(self, lo, hi):
        errors = (bound for a,b,bound in self.observations if a <= hi and lo <= b)
        return max(errors,default=math.inf)

    def encloses(self, lo, hi):
        return self.coverage[0] <= lo and hi <= self.coverage[1]


@dataclass(frozen=True)
class RoadWindowFit:
    stations: tuple
    position_m: float
    heading_worst_case_rad: float
    placement_m: float
    placement_rad: float
    sources: frozenset
    observations: tuple
    divider_interpolated: bool
    heading_estimate_rad: float | None
    position_estimate_m: float | None
    normal_bounds_m: tuple[float, float, float]
    outer_chord_bounds_m: tuple[float, float]


def _project(point, origin, c, s):
    dx,dy = point[0]-origin[0],point[1]-origin[1]
    return c*dx+s*dy,-s*dx+c*dy


def _chords(strip, origin, axis, offsets):
    """한 프레임의 같은 경계에서 이어진 선 조각과 국소 투영 상한을 뽑는다."""
    c,s = math.cos(axis),math.sin(axis)
    local = tuple(_project(p,origin,c,s) for p in strip.points)
    rotation = strip.mapped_pose.sample.yaw-axis
    camera_errors = strip.original.point_error_xy or ((strip.original.error_m,)*2,)*len(local)
    sample = strip.mapped_pose.sample
    raw = strip.original.points_xy
    path = strip.original.segment_error_xy or ((strip.original.error_m,)*2,)*(len(local)-1)
    along = tuple(axis_error(p,error,rotation,sample.position_error_m,sample.yaw_error_rad)
                  for p,error in zip(raw,camera_errors))
    chunks = []
    for i,(u,v) in enumerate(local):
        role = min(range(3),key=lambda r:abs(offsets[r]-v))
        if abs(u) > _MAX_WINDOW_M or abs(v-offsets[role]) > _MAX_AXIS_RESIDUAL_M:
            continue
        if not chunks or chunks[-1][0] != role or chunks[-1][1][-1] != i-1:
            chunks.append((role,[]))
        chunks[-1][1].append(i)
    is_dash = (strip.original.start_kind == strip.original.end_kind == 'observed_paint_end'
               and not strip.original.closed)
    for role,indices in chunks:
        if len(indices) < 2:
            continue
        size = len(indices)
        for first,last in dict.fromkeys(((0,size),(0,2*size//3),(size//3,size))):
            section = indices[first:last]
            if len(section) < 2:
                continue
            a,b = min(section,key=lambda i:local[i][0]),max(section,key=lambda i:local[i][0])
            u,v = local[a],local[b]
            dash = role == 1 and is_dash and first == 0 and last == size and size == len(local)
            if v[0]-u[0] < (_MIN_CHORD_M/2 if dash else _MIN_CHORD_M):
                continue
            angle = math.atan2(v[1]-u[1],v[0]-u[0])
            if abs(angle) > _MAX_CHORD_TURN_RAD:
                continue
            # 꼬인 끝과 주차선 분기는 직선 관측으로 묶지 않는다.
            ca,sa = math.cos(angle),math.sin(angle)
            residual = max(abs((local[i][1]-u[1])*ca-(local[i][0]-u[0])*sa) for i in section)
            if residual > _MAX_AXIS_RESIDUAL_M/2:
                continue
            coverage = u[0]+along[a],v[0]-along[b]
            if coverage[0] >= coverage[1] and not dash:
                continue
            observations = []
            for i in section[:-1]:
                j = i+1
                start = min(local[i][0]-along[i],local[j][0]-along[j])
                end = max(local[i][0]+along[i],local[j][0]+along[j])
                deviation = max(abs((local[k][1]-u[1])*ca-(local[k][0]-u[0])*sa) for k in (i,j))
                lateral = max(axis_error(raw[k],path[i],rotation-angle-math.pi/2,
                    sample.position_error_m,sample.yaw_error_rad) for k in (i,j))
                observations.append((start,end,(deviation+lateral)/ca))
            dash_sample = None
            if dash:
                # 단점의 종방향 오차가 점선 길이만큼 커도, 횡방향 측정은 남는다.
                i = section[len(section)//2]
                x,y = local[i]
                lateral = axis_error(raw[i],camera_errors[i],rotation-math.pi/2,
                                     sample.position_error_m,sample.yaw_error_rad)
                dash_sample = x,y,lateral+math.tan(_MAX_CHORD_TURN_RAD)*along[i]
            yield role,_MarkingChord(strip.source,strip.identity,(u[0],v[0]),(u[1],v[1]),angle,
                                   coverage,tuple(observations),dash_sample)


def _road_chords(strips, start, end, profile, known_roles):
    """기존 두 차로의 역할 배치를 기준으로 관측한 경계별 긴 현을 선택한다."""
    a,b = start[1],end[1]
    origin = ((a[0]+b[0])/2,(a[1]+b[1])/2)
    heading = math.atan2(b[1]-a[1],b[0]-a[0])
    c,s = math.cos(heading),math.sin(heading)
    offsets = tuple((_project(p,origin,c,s)[1]+_project(q,origin,c,s)[1])/2
                    for p,q in zip(start,end))
    if not all(profile.lane_width_min_m <= x-y <= profile.lane_width_max_m
               for x,y in zip(offsets,offsets[1:])):
        return None
    roles = [[],[],[]]
    for strip in strips:
        known = known_roles.get((strip.source,strip.identity),())
        for role,chord in _chords(strip,origin,heading,offsets):
            if not known or known == {role}:
                roles[role].append(chord)
    return origin,heading,tuple(tuple(chords) for chords in roles)


def _fit_interval(chords, lo, hi):
    """짧은 선 조각이 다른 프레임으로 이어진 구간도 한 경계로 평가한다."""
    cuts = sorted({lo,hi,*(u for chord in chords for u in chord.coverage if lo < u < hi),
                   *(u for chord in chords for a,b,_ in chord.observations for u in (a,b) if lo < u < hi)})
    def interval_at(u):
        rows = tuple(chord for chord in chords if chord.encloses(u,u))
        if rows:
            low = max(chord.at(u)-chord.lateral_bound(u,u) for chord in rows)
            high = min(chord.at(u)+chord.lateral_bound(u,u) for chord in rows)
            if low <= high:
                return low,high
        return None

    first_bound,last_bound = interval_at(lo),interval_at(hi)
    if first_bound is None or last_bound is None:
        return None
    first,last = (sum(values)/2 for values in (first_bound,last_bound))
    slope = (last-first)/(hi-lo)
    lateral = 0.
    for a,b in zip(cuts,cuts[1:]):
        active = tuple(chord for chord in chords if chord.encloses(a,b))
        if not active:
            return None
        for u in (a,b):
            low = max(chord.at(u)-chord.lateral_bound(a,b) for chord in active)
            high = min(chord.at(u)+chord.lateral_bound(a,b) for chord in active)
            if low > high:
                return None
        error = min(chord.lateral_bound(a,b)+max(abs(chord.at(u)-first-slope*(u-lo)) for u in (a,b))
                    for chord in active)
        lateral = max(lateral,error)
    # 채택 대상은 이 구간의 현이다. 실제 좌우 관측이 각 station에서
    # 주는 횡방향 구간으로 현을 구한다. 종방향 오차는 위에서 coverage를
    # 줄이는 데 이미 사용했으므로 각도에서 같은 오차를 다시 넣지 않는다.
    direction = heading_error((lo,first),(hi,last),
        (0.,(first_bound[1]-first_bound[0])/2),(0.,(last_bound[1]-last_bound[0])/2))
    return (first,last),lateral,direction


def _select_chords(chords, lo, hi, key):
    overlapping = tuple(ch for ch in chords if ch.coverage[0] < hi and ch.coverage[1] > lo)
    selected = {}
    # 구간 양끝과 중앙을 실제로 덮는 관측을 남긴다. 한쪽에 몰린 RGB가
    # 다른 위치에서 본 같은 경계를 밀어내지 않도록 한다.
    for u in (lo,(lo+hi)/2,hi):
        unique = {}
        for chord in sorted((ch for ch in overlapping if ch.encloses(u,u)),
                            key=lambda ch:(ch.lateral_bound(u,u),abs(ch.heading))):
            unique.setdefault(chord.source,chord)
        for chord in tuple(unique.values())[:2]:
            selected.setdefault(chord,None)
    current = min((ch for ch in overlapping if ch.source == key),
                  key=lambda ch:(ch.lateral_bound(lo,hi),abs(ch.heading)),default=None)
    if current is not None:
        selected.setdefault(current,None)
    return tuple(selected)


def _fit_divider(chords, lo, hi, profile):
    """서로 다른 위치의 실제 점선을 모으고 양끝 사이의 짧은 빈 구간만 보간한다."""
    samples = sorted((ch for ch in chords if ch.dash_sample is not None),
                     key=lambda ch: ch.dash_sample[0])
    if len({ch.source for ch in samples}) < 2:
        return None
    # 최근 영상에 같은 점선이 여러 번 보일 수 있다. 서로 다른 점선 위치를
    # 남기되 각 위치에서 오차가 작은 관측을 쓰고, 겹친 관측의 모순은 거절한다.
    groups = []
    for chord in samples:
        u = chord.dash_sample[0]
        if not groups or u-groups[-1][0].dash_sample[0] > _MIN_CHORD_M/2:
            groups.append([])
        groups[-1].append(chord)
    points = []
    slope_limit = math.tan(_MAX_CHORD_TURN_RAD)
    for group in groups:
        best = min(group,key=lambda ch:ch.dash_sample[2])
        u = best.dash_sample[0]
        bounds = [(y-error-slope_limit*abs(x-u),y+error+slope_limit*abs(x-u),ch)
                  for ch in group for x,y,error in (ch.dash_sample,)]
        low,_,lower_chord = max(bounds,key=lambda row:row[0])
        _,high,upper_chord = min(bounds,key=lambda row:row[1])
        if low > high:
            return None
        support = (best,lower_chord,upper_chord)
        extra = next((ch for ch in group if ch.source != best.source),best)
        points.append((u,(low+high)/2,(high-low)/2,support,extra))
    before = [i for i,p in enumerate(points) if p[0] <= lo]
    after = [i for i,p in enumerate(points) if p[0] >= hi]
    if not before or not after:
        return None
    points = points[before[-1]:after[0]+1]
    used = tuple(dict.fromkeys(ch for p in points for ch in p[3]))
    if len({ch.source for ch in used}) < 2:
        # 같은 프레임에서 본 점선도 쓰되, 두 프레임 이상이 실제로 보강해야 한다.
        extra = next((p[4] for p in points if p[4].source != used[0].source),None)
        if extra is None:
            return None
        used += (extra,)
    gap_limit = min(profile.max_gap_m,2*profile.missing_outer_width_m/3)
    curvature = profile.max_curvature_1pm/math.cos(_MAX_CHORD_TURN_RAD)**3
    segments = []
    for (a,y,ea,*_),(b,z,eb,*_) in zip(points,points[1:]):
        if b-a > gap_limit or abs(z-y) > slope_limit*(b-a)+ea+eb:
            return None
        # 선의 접선 변화 상한으로 관측 사이의 현 보간 오차를 계산한다.
        segments.append((a,b,y,z,ea,eb,curvature*(b-a)**2/8))
    def section_at(row,u):
        a,b,y,z,ea,eb,pad = row
        t = (u-a)/(b-a)
        return y+t*(z-y),(1-t)*ea+t*eb+pad
    first,ea = section_at(segments[0],lo)
    last,eb = section_at(segments[-1],hi)
    slope = (last-first)/(hi-lo)
    lateral = max(abs(y-first-slope*(u-lo))+bound
                  for row in segments for u in (max(lo,row[0]),min(hi,row[1]))
                  for y,bound in (section_at(row,u),))
    direction = heading_error((lo,first),(hi,last),(0.,ea),(0.,eb))
    return ((first,last),lateral,direction),used


def _heading_estimate(chords, lo, hi, nominal, allowed_sources, *, dashed):
    """공통 횡이동을 뺀 프레임별 기울기로 기존 방향의 불확실성을 추정한다."""
    by_source = {}
    for chord in chords:
        if chord.source not in allowed_sources:
            continue
        if dashed:
            if chord.dash_sample is None:
                continue
            points = (chord.dash_sample[:2],)
        else:
            if chord.coverage[0] > hi or chord.coverage[1] < lo:
                continue
            points = zip(chord.u,chord.v)
        for u,v in points:
            if lo-_HEADING_NEIGHBORHOOD_M <= u <= hi+_HEADING_NEIGHBORHOOD_M:
                by_source.setdefault(chord.source,set()).add((u,v))
    moments = []
    for source,points in sorted(by_source.items()):
        if len(points) < 2:
            continue
        x = math.fsum(u for u,_ in points)/len(points)
        y = math.fsum(v for _,v in points)/len(points)
        xx = math.fsum((u-x)**2 for u,_ in points)/len(points)
        xy = math.fsum((u-x)*(v-y) for u,v in points)/len(points)
        moments.append((source,xx,xy))
    if len(moments) < _MIN_HEADING_FRAMES:
        return None

    def heading(rows):
        xx = sum(row[1] for row in rows)
        if xx < _MIN_HEADING_SPREAD_M**2:
            return None
        return math.atan2(sum(row[2] for row in rows),xx)

    split = (len(moments)+1)//2
    combined,early,late = (heading(rows) for rows in (moments,moments[:split],moments[split:]))
    if combined is None or early is None or late is None:
        return None
    estimate = math.hypot(wrap_yaw(nominal-combined),wrap_yaw(early-late)/2)
    return estimate, frozenset(row[0] for row in moments)


def _position_estimate(chords, lo, hi, nominal, allowed_sources, *, dashed):
    """같은 경계의 횡방향 잔차를 RGB마다 한 표본으로 집계한다."""
    by_source = {}
    for chord in chords:
        if chord.source not in allowed_sources:
            continue
        if dashed:
            if chord.dash_sample is None:
                continue
            points = (chord.dash_sample[:2],)
        else:
            if chord.u[1] < lo or hi < chord.u[0]:
                continue
            points = zip(chord.u, chord.v)
        for u, v in points:
            if lo - _POSITION_NEIGHBORHOOD_M <= u <= hi + _POSITION_NEIGHBORHOOD_M:
                by_source.setdefault(chord.source, set()).add((u, v))
    if len(by_source) < _MIN_POSITION_FRAMES:
        return None
    slope = (nominal[1] - nominal[0]) / (hi - lo)
    offsets = [math.fsum(v - nominal[0] - slope * (u - lo) for u, v in sorted(points)) / len(points)
               for points in by_source.values()]
    return math.sqrt(math.fsum(offset**2 for offset in offsets) / len(offsets)), frozenset(by_source)


def _boundary_bounds(stations, errors, anchor, station_error):
    """절대 횡오차와 공통 평행이동이 상쇄되는 현 벡터 오차를 구한다."""
    yaw = min(math.pi, anchor.yaw_error_rad)
    normal, chord = [], []
    for role, (a, b) in enumerate(zip(*stations)):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        relative = [(p[0] - anchor.x, p[1] - anchor.y) for p in (a, b)]
        shape = errors[role] + station_error
        rotation = max(abs(x * ux + y * uy) * math.sin(min(yaw, math.pi / 2))
                       + abs(y * ux - x * uy) * (1 - math.cos(yaw)) for x, y in relative)
        normal.append(shape + anchor.position_error_m + rotation)
        chord.append(2 * shape + 2 * length * math.sin(yaw / 2))
    return tuple(normal), (chord[0], chord[2])


def _fit_window(origin, heading, roles, longitudinal, profile, anchor, key, max_sources):
    lo,hi = longitudinal
    used = {}
    nominal = {}
    errors = {}
    headings = {}
    divider_interpolated = False
    # 양쪽 실선의 공통 범위를 먼저 확인한다. 범위가 없으면 점선을
    # 보간해도 두 차로를 만들 수 없다.
    for role in (0,2,1):
        chords = roles[role]
        rows = _select_chords(chords,lo,hi,key)
        fitted = _fit_interval(rows,lo,hi) if len({ch.source for ch in rows}) >= 2 else None
        if role == 1 and fitted is None and profile.allow_dashed:
            divider = _fit_divider(chords,lo,hi,profile)
            if divider is not None:
                fitted,rows = divider
                divider_interpolated = True
        if fitted is not None:
            nominal[role],errors[role],headings[role] = fitted
            used[role] = rows
        elif role != 1:
            return None
    if not any(ch.source == key for rows in used.values() for ch in rows):
        return None
    if 1 not in used:
        nominal[1] = tuple((a+b)/2 for a,b in zip(nominal[0],nominal[2]))
        errors[1] = (errors[0]+errors[2])/2+profile.missing_outer_width_error_m
    stations = tuple(tuple(transform_point((u,nominal[role][i]),(*origin,heading)) for role in range(3))
                     for i,u in enumerate(longitudinal))
    if any(not profile.lane_width_min_m <= nominal[role][i]-nominal[role+1][i] <= profile.lane_width_max_m
           for i in (0,1) for role in (0,1)):
        return None
    radius = max(math.hypot(p[0]-anchor.x,p[1]-anchor.y) for station in stations for p in station)
    shape_error = max(errors.values())
    placement = anchor.position_error_m + 2*(radius+shape_error)*math.sin(min(math.pi,anchor.yaw_error_rad)/2)
    sources = frozenset(ch.source for rows in used.values() for ch in rows)
    if len(sources) > max_sources:
        return None
    extra = sorted({ch.source for role in used for ch in roles[role]}-sources,reverse=True)
    allowed = sources | frozenset(extra[:max_sources-len(sources)])
    estimates, positions = [], []
    for role in used:
        angle = math.atan2(nominal[role][1]-nominal[role][0],hi-lo)
        dashed = role == 1 and divider_interpolated
        estimate = _heading_estimate(roles[role],lo,hi,angle,allowed,dashed=dashed)
        if estimate is not None:
            estimates.append(estimate)
        position = _position_estimate(roles[role],lo,hi,nominal[role],allowed,dashed=dashed)
        if position is not None:
            positions.append(position)
    # 근거가 부족한 경계가 있으면 기존 판정을 사용한다.
    heading_estimate = None
    if len(estimates) == len(used):
        heading_estimate = max(value for value,_ in estimates)
        sources = sources.union(*(keys for _,keys in estimates))
    position_estimate = None
    if len(positions) == 3:
        position_estimate = max(value for value,_ in positions)
        sources = sources.union(*(keys for _,keys in positions))
    normal, chord = _boundary_bounds(stations,errors,anchor,profile.station_error_m)
    evidence_ids = {role:frozenset((ch.source,ch.identity) for ch in rows) for role,rows in used.items()}
    return RoadWindowFit(stations,shape_error,max(headings.values()),placement,anchor.yaw_error_rad,
                         sources,tuple(evidence_ids.get(role,frozenset()) for role in range(3)),
                         divider_interpolated,heading_estimate,position_estimate,normal,chord)


def fit_road_window(strips, start, end, profile, anchor, key, known_roles, *, max_sources):
    """주변 경계의 공통 범위에서만 짧은 직선 구간을 다시 만든다."""
    fit = _road_chords(strips,start,end,profile,known_roles)
    if fit is None:
        return None
    origin,heading,roles = fit
    half = max(profile.min_usable_length_m,_MIN_FIT_SPAN_M)/2
    # 실제로 본 점선의 끝을 기준으로 창을 잡는다. 카메라에서 먼 쪽에
    # 점선이 없으면 고정된 중심의 창만 검사할 때 가까운 관측을 놓친다.
    if profile.allow_dashed and roles[0] and roles[2]:
        # 전체 점선 범위에는 한쪽 실선이 아직 보이지 않는 부분도 있다.
        # 세 경계가 모두 닿는 범위에서 실제 점선 두 개를 골라 창을 만든다.
        lower = max(min(ch.coverage[0] for ch in roles[role]) for role in (0,2))
        upper = min(max(ch.coverage[1] for ch in roles[role]) for role in (0,2))
        dashes = sorted({ch.dash_sample[0] for ch in roles[1]
                         if ch.dash_sample is not None and lower <= ch.dash_sample[0] <= upper})
        # 가장 긴 창부터 검사하고, 선택된 바깥점 하나씩 제외한 후보도 본다.
        windows = sorted({(lo,hi) for lo in dashes[:3] for hi in dashes[-3:]
                          if hi-lo >= 1.9*half and lo <= half and hi >= -half},
                         key=lambda span:(-(span[1]-span[0]),span[0]))
        for window in windows[:3]:
            fitted = _fit_window(origin,heading,roles,window,profile,anchor,key,max_sources)
            if (fitted is not None and fitted.observations[1]
                    and fitted.heading_estimate_rad is not None):
                return fitted
    fallback = _fit_window(origin,heading,roles,(-half,half),profile,anchor,key,max_sources)
    if fallback is not None and fallback.observations[1]:
        return fallback
    # 점선 한두 조각이 고정 구간의 끝에 못 닿으면, 앞뒤 관측이 실제로 겹친
    # 위치로 구간을 옮긴다. 관측 양끝 바깥으로 중앙선을 늘리지 않는다.
    if not any(ch.dash_sample is not None for ch in roles[1]):
        return fallback
    for offset in (-half/4,half/4):
        fitted = _fit_window(origin,heading,roles,(offset-half,offset+half),profile,anchor,key,max_sources)
        if fitted is not None and fitted.observations[1]:
            return fitted
    short = max(profile.min_usable_length_m,half*1.5)/2
    if short < half:
        for offset in (0.,-half/4,half/4):
            fitted = _fit_window(origin,heading,roles,(offset-short,offset+short),profile,anchor,key,max_sources)
            if fitted is not None and fitted.observations[1]:
                return fitted
    return fallback
