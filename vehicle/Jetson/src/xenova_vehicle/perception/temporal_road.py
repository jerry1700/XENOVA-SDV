"""지도에 배치된 선 관측에서 같은 방향 두 차로의 후보 구간을 연결한다.

관측 capture와 보간 구간을 남긴다. 차로 카탈로그 후보에는 현재 영상이 참여한
구간만 올린다. 통행 가능 공간과 현재 위치의 자격은 별도로 판정한다.
"""

from collections import deque
from dataclasses import dataclass
from itertools import combinations
import math
from statistics import median

from .inputs import FrameStamp
from .parking_entrance import observe_entrances
from .road import LaneHypothesis, RoadResult
from .road_normals import ReferenceObservations, heading_error, normal_samples
from .tape import TapeStrip
from ..mapping.odometry import PoseEstimate
from ..mapping.registration import segment_distance, transform_point, wrap_yaw


_FRAME_LIMIT = 12
_MAP_FRAME_LIMIT = 32
_STRIP_LIMIT = 16
_MAP_BAND_LIMIT = 32
_LINK_LIMIT = 512
_MARKING_LIMIT = 512
_STATION_STEP_M = .05
_BAND_SPREAD_M = .055
_DIRECTION_MATCH_RAD = .30
_ENTRANCE_RECHECK_FRAMES = 8


@dataclass(frozen=True)
class _Strip:
    points: tuple
    error_m: float
    source: tuple
    identity: int
    heading_errors: tuple
    original: TapeStrip
    pose_yaw_error_rad: float
    stamp: FrameStamp | None = None
    capture_pose: PoseEstimate | None = None
    historical: bool = False


@dataclass(frozen=True)
class StartupMarkings:
    """정합 이전의 RGB 식별자, 촬영 좌표 선 조각과 원 odom."""

    stamp: FrameStamp
    pose: PoseEstimate | None
    strips: tuple[TapeStrip, ...]
    usable_for_matching: bool


@dataclass(frozen=True)
class MarkingJoin:
    points: tuple
    error_m: float
    sources: tuple


@dataclass(frozen=True)
class _Band:
    y: float
    heading: float
    error_m: float
    sources: frozenset
    strips: frozenset
    role: int | None
    yaw_error_rad: float
    inferred: bool = False


@dataclass(frozen=True)
class RoadLink:
    """왼쪽 경계·중앙 점선·오른쪽 경계 순서의 지도 단면. 미관측 경계는 None이다."""

    start: tuple
    end: tuple
    error_m: float
    sources: frozenset
    observations: tuple
    interpolated: bool
    yaw_error_rad: float
    inferred_roles: tuple = (False,False,False)


@dataclass(frozen=True)
class RoadSection:
    """지도 좌표에서 이어진 두 차로. 각 구간의 보간 표시와 원 관측을 보존한다."""

    links: tuple[RoadLink, ...]

    @property
    def boundaries(self):
        stations = (self.links[0].start, *(link.end for link in self.links))
        return tuple(zip(*stations))

    @property
    def centers(self):
        left, divider, right = self.boundaries
        return tuple(tuple(((a[0]+b[0])/2, (a[1]+b[1])/2) for a,b in zip(divider,outer))
                     for outer in (left,right))


@dataclass(frozen=True)
class MappedRoad:
    """현재 RGB와 이전 원 관측에 연결된 지도 좌표의 차로 형상."""

    road: RoadResult
    sources: frozenset

    def within(self, profile):
        shape = self.road.hypotheses[0]
        return shape.error_m <= profile.max_position_error_m and shape.yaw_error_rad <= profile.max_yaw_error_rad


def _usable_graph(graph):
    return (graph.calibration_qualified and (graph.complete or set(graph.reasons) == {'NO_MEASURED_TAPE_WIDTH'}))


def _observed_strips(graph):
    return tuple(s for s in graph.strips if s.observed
                 and 'predicted' not in (s.start_kind,s.end_kind))


def _project_strips(strips, pose, source, *, stamp=None, capture_pose=None, historical=False):
    sample = pose.sample
    transform = sample.x,sample.y,sample.yaw
    result = []
    for strip in strips:
        radius = max(math.hypot(*point) for point in strip.points_xy)
        error = strip.error_m + sample.position_error_m + 2*radius*math.sin(sample.yaw_error_rad/2)
        errors = strip.point_error_xy or ((strip.error_m,strip.error_m),)*len(strip.points_xy)
        headings = tuple(heading_error(a,b,ea,eb)+sample.yaw_error_rad
                         for a,b,ea,eb in zip(strip.points_xy,strip.points_xy[1:],errors,errors[1:]))
        result.append(_Strip(tuple(transform_point(point,transform) for point in strip.points_xy),
                             error,source,strip.local_id,headings,strip,sample.yaw_error_rad,stamp,capture_pose,historical))
    return tuple(result)


def _matching_strips(strips):
    return tuple(sorted((s for s in strips if not s.original.closed),
                        key=lambda s: (-s.original.length_m,s.identity))[:_STRIP_LIMIT])


def _marking_ends(strip):
    points = strip.points
    ends = strip.original.start_kind,strip.original.end_kind
    raw = strip.original.points_xy
    if raw[-1][0] < raw[0][0]:
        points = tuple(reversed(points))
        ends = ends[::-1]
    result = []
    for start,ordered in ((True,points),(False,points[::-1])):
        a = ordered[0]
        b = next((p for p in ordered[1:] if math.dist(a,p)>=2*_STATION_STEP_M),ordered[-1])
        dx,dy = b[0]-a[0],b[1]-a[1]
        result.append((a,math.atan2(dy,dx) if start else math.atan2(-dy,-dx)))
    return result[0],result[1],ends


def _paint_gap(first, second, profile):
    (a,h),(b,k) = first,second
    dx,dy = b[0]-a[0],b[1]-a[1]
    gap = math.hypot(dx,dy)
    delta = wrap_yaw(k-h)
    return (0. < gap <= profile.max_gap_m
            and abs(delta) <= min(.65,profile.merge_heading_rad+profile.max_curvature_1pm*gap)
            and abs(wrap_yaw(math.atan2(dy,dx)-h-delta/2)) <= profile.merge_heading_rad)


def _role_continuation(seed, seed_ends, old, old_ends, profile):
    tolerance = max(profile.merge_lateral_m,profile.missing_outer_width_error_m)
    points = old.points if old.original.points_xy[0][0]<=old.original.points_xy[-1][0] else old.points[::-1]
    for point,heading in seed_ends[:2]:
        for a,b in zip(points,points[1:]):
            if (segment_distance(point,a,b)<=tolerance
                    and abs(wrap_yaw(heading-math.atan2(b[1]-a[1],b[0]-a[0])))<=_DIRECTION_MATCH_RAD):
                return True
    return _paint_gap(old_ends[1],seed_ends[0],profile) or _paint_gap(seed_ends[1],old_ends[0],profile)


def _join_markings(strips, roles, profile):
    """서로 다른 RGB의 방향·간격이 맞는 짧은 단일 선 연결만 돌려준다."""
    gap_limit = min(profile.max_gap_m,2*_STATION_STEP_M)
    if gap_limit <= 0:
        return ()
    ends = tuple(_marking_ends(strip) for strip in strips)
    starts = {}
    for i,((p,_),_,kinds) in enumerate(ends):
        if kinds[0] != 'physical_junction':
            key = tuple(math.floor(v/gap_limit) for v in p)
            starts.setdefault(key,[]).append(i)
    proposals = {}
    for i,(_, (a,heading),kinds) in enumerate(ends):
        if kinds[1] == 'physical_junction':
            continue
        x,y = (math.floor(v/gap_limit) for v in a)
        choices = []
        for sx in range(x-1,x+2):
            for sy in range(y-1,y+2):
                for j in starts.get((sx,sy),()):
                    if strips[i].source == strips[j].source:
                        continue
                    first_roles = roles.get((strips[i].source,strips[i].identity),set())
                    second_roles = roles.get((strips[j].source,strips[j].identity),set())
                    if first_roles and second_roles and len(first_roles|second_roles) != 1:
                        continue
                    b,next_heading = ends[j][0]
                    dx,dy = b[0]-a[0],b[1]-a[1]
                    gap = math.hypot(dx,dy)
                    if (not profile.station_error_m < gap <= gap_limit
                            or abs(wrap_yaw(heading-next_heading)) > profile.merge_heading_rad
                            or abs(wrap_yaw(math.atan2(dy,dx)-heading)) > profile.merge_heading_rad
                            or abs(-dx*math.sin(heading)+dy*math.cos(heading)) > profile.merge_lateral_m):
                        continue
                    choices.append((gap,j,b))
        if choices:
            gap,j,b = min(choices)
            if all(math.dist(b,p)<=profile.merge_lateral_m for _,_,p in choices):
                proposals.setdefault(j,[]).append((gap,i,a,b))
    output = []
    for j,choices in proposals.items():
        gap,i,a,b = min(choices)
        if not all(math.dist(a,p)<=profile.merge_lateral_m for _,_,p,_ in choices):
            continue
        first,second = strips[i],strips[j]
        error = max(first.error_m,second.error_m)+profile.max_curvature_1pm*gap**2/8
        output.append(MarkingJoin((a,b),error,((first.source,first.identity),(second.source,second.identity))))
    return tuple(output)


def _bands(samples, known_roles, *, map_geometry=False):
    groups = []
    for sample in sorted(samples, key=lambda s: s[0]):
        if not groups or sample[0] - groups[-1][0][0] > _BAND_SPREAD_M:
            groups.append([])
        groups[-1].append(sample)
    if map_geometry:
        # 같은 위치에 있는 굽은 끝과 평행한 선을 한 방향으로 평균하지 않는다.
        directional_groups = []
        for group in groups:
            by_heading = []
            for sample in sorted(group,key=lambda s:s[1]):
                if not by_heading or sample[1]-by_heading[-1][0][1] > _DIRECTION_MATCH_RAD:
                    by_heading.append([])
                by_heading[-1].append(sample)
            directional_groups.extend(by_heading)
        groups = directional_groups
    result = []
    for group in groups:
        sources = frozenset(s[3] for s in group)
        if len(sources) < 2 and not map_geometry:
            continue
        heading = median(s[1] for s in group)
        if any(abs(s[1] - heading) > _DIRECTION_MATCH_RAD for s in group):
            continue
        y = median(s[0] for s in group)
        # 반복 관측은 역할 선택에 쓰고, 오차 상한은 원 관측을 모두 포함한다.
        error = max(s[2] + abs(s[0] - y) for s in group)
        strips = frozenset((s[3],s[4]) for s in group)
        roles = set().union(*(known_roles.get(key,()) for key in strips))
        if len(roles) > 1:
            continue
        yaw_error = max(s[5]+abs(s[1]-heading) for s in group)
        result.append(_Band(y, heading, error, sources, strips,
                            next(iter(roles),None),yaw_error))
    return sorted(result,key=lambda band:band.y) if map_geometry else result


def _road_section(bands, width_bounds):
    lower, upper = width_bounds
    matches = []
    for right, divider, left in combinations(bands, 3):
        if any(b.role not in (None,i) for i,b in enumerate((left,divider,right))):
            continue
        if max(b.heading for b in (left, divider, right)) - min(b.heading for b in (left, divider, right)) > _DIRECTION_MATCH_RAD:
            continue
        scale = math.cos(divider.heading)
        widths = ((left.y-divider.y)*scale, (divider.y-right.y)*scale)
        if all(lower <= width <= upper for width in widths):
            matches.append((left, divider, right))
            if len(matches) > 1:
                return None, True
    if matches:
        return matches[0], False

    # 한쪽 역할이 이미 확인됐으면 한 차로 폭 떨어진 관측의 역할도 확인할 수 있다.
    for right, left in combinations(bands, 2):
        if abs(left.heading-right.heading) > _DIRECTION_MATCH_RAD:
            continue
        width = (left.y-right.y)*math.cos((left.heading+right.heading)/2)
        if not lower <= width <= upper:
            continue
        left_lane = right.role == 1 or left.role == 0
        right_lane = left.role == 1 or right.role == 2
        if left_lane and left.role in (None,0) and right.role in (None,1):
            matches.append((left,right,None))
        if right_lane and left.role in (None,1) and right.role in (None,2):
            matches.append((None,left,right))
        if len(matches) > 1:
            return None, True
    return (matches[0], False) if matches else (None, False)


def _section_from_outers(bands, profile):
    """두 차로 폭만큼 떨어진 바깥 경계에서 중앙선을 추정한다."""
    lower,upper = profile.missing_outer_width_bounds_m
    result = None
    for right,left in combinations(bands,2):
        if right.role not in (None,2) or left.role not in (None,0) or abs(left.heading-right.heading) > _DIRECTION_MATCH_RAD:
            continue
        heading = (left.heading+right.heading)/2
        scale = math.cos(heading)
        if not 2*lower <= (left.y-right.y)*scale <= 2*upper:
            continue
        if result is not None:
            return None,True
        if right.role is None and left.role is None and any(
                lower <= max(right.y-other.y,other.y-left.y)*scale <= upper for other in bands):
            # 바깥에 한 차로 폭만큼 떨어진 평행 관측이 있으면 어느 쌍인지 더 봐야 한다.
            return None,True
        # 두 차로의 폭 차이와 각 원 바깥 경계의 오차를 중앙선에 포함한다.
        divider = _Band((left.y+right.y)/2,heading,
            max(left.error_m,right.error_m)+profile.missing_outer_width_error_m/scale,
            left.sources|right.sources,frozenset(),1,
            max(left.yaw_error_rad,right.yaw_error_rad)+abs(left.heading-right.heading)/2,True)
        result = left,divider,right
    return result,False


def _fill_boundaries(section, profile):
    observed = [(role,band) for role,band in enumerate(section) if band is not None]
    result = list(section)
    for role,band in enumerate(section):
        if band is not None:
            continue
        origin,base = min(observed,key=lambda item: abs(item[0]-role))
        if base.yaw_error_rad > profile.max_heading_rad:
            return None
        width = (origin-role)*profile.missing_outer_width_m
        scale = math.cos(base.heading)
        error = (abs(origin-role)*profile.missing_outer_width_error_m
                 + 2*abs(width)*math.sin(min(math.pi,base.yaw_error_rad)/2))/scale
        result[role] = _Band(base.y+width/scale,base.heading,base.error_m+error,
                             base.sources,frozenset(),role,base.yaw_error_rad,True)
    return tuple(result)


def _map_cross_section(bands, profile):
    section,ambiguous = _road_section(bands,profile.missing_outer_width_bounds_m)
    if ambiguous:
        return None,True
    if section is not None and all(section):
        return section,False
    outer_pair,ambiguous = _section_from_outers(bands,profile)
    if ambiguous:
        return None,True
    if outer_pair is not None:
        return outer_pair,False
    if section is not None:
        return _fill_boundaries(section,profile),False
    established = [band for band in bands if band.role is not None]
    if len(established) == 1:
        anchor = established[0]
        return _fill_boundaries(tuple(anchor if i == anchor.role else None for i in range(3)),profile),False
    return None,len(established)>1


def _normal_station(point, heading, bands):
    x,y = point
    nx,ny = -math.sin(heading),math.cos(heading)
    return tuple((x+nx*b.y,y+ny*b.y) for b in bands),heading,bands


def _normal_link(before, after, profile):
    start,a_heading,left_bands = before
    end,b_heading,right_bands = after
    dx,dy = end[1][0]-start[1][0],end[1][1]-start[1][1]
    length = math.hypot(dx,dy)
    if length < profile.station_error_m or length <= 1e-9:
        return None
    heading = math.atan2(dy,dx)
    if max(abs(wrap_yaw(heading-h)) for h in (a_heading,b_heading)) > .55:
        return None
    gap = max(math.dist(a,b) for a,b in zip(start,end))
    if gap > profile.max_gap_m or any((b[0]-a[0])*dx+(b[1]-a[1])*dy <= 0 for a,b in zip(start,end)):
        return None
    inferred = tuple(a.inferred or b.inferred or not a.strips&b.strips or length>2*_STATION_STEP_M
                     for a,b in zip(left_bands,right_bands))
    bands = left_bands+right_bands
    error = max(b.error_m for b in bands)+profile.max_curvature_1pm*gap**2/8+profile.station_error_m
    yaw = max(b.yaw_error_rad for b in bands)+max(abs(wrap_yaw(heading-h)) for h in (a_heading,b_heading))
    if any(inferred):
        yaw += profile.max_curvature_1pm*gap
    return RoadLink(start,end,error,frozenset().union(*(b.sources for b in bands)),
        tuple(a.strips|b.strips for a,b in zip(left_bands,right_bands)),any(inferred),min(math.pi,yaw),inferred)


def _link(first, second, pose, profile):
    x0, start = first
    x1, end = second
    dx = x1-x0
    pairs = tuple((a,b) for a,b in zip(start,end) if a is not None and b is not None)
    if len(pairs) < 2:
        return None
    gap = max(math.hypot(dx, b.y-a.y) for a,b in pairs)
    inferred_roles = tuple(a is not None and b is not None and (a.inferred or b.inferred
        or dx > 1.5*_STATION_STEP_M or not (a.strips & b.strips)) for a,b in zip(start,end))
    interpolated = any(inferred_roles)
    if gap > profile.max_gap_m:
        return None
    curve_pad = profile.max_curvature_1pm * gap**2 / 8
    lateral_limit = min(profile.missing_outer_width_m / 4, profile.merge_lateral_m + curve_pad)
    for a, b in pairs:
        expected_y = dx * math.tan((a.heading+b.heading)/2)
        if (abs(b.y-a.y-expected_y) > lateral_limit
                or abs(b.heading-a.heading) > min(.65, profile.merge_heading_rad + profile.max_curvature_1pm*gap)):
            return None
    mapped_start = tuple(transform_point((x0,a.y),pose) if a is not None and b is not None else None
                         for a,b in zip(start,end))
    mapped_end = tuple(transform_point((x1,b.y),pose) if a is not None and b is not None else None
                       for a,b in zip(start,end))
    used = tuple(b for pair in pairs for b in pair)
    observations = tuple(a.strips | b.strips if a is not None and b is not None else frozenset()
                         for a,b in zip(start,end))
    chord = math.atan2(end[1].y-start[1].y,dx)
    yaw = max(b.yaw_error_rad+abs(b.heading-chord) for b in (start[1],end[1]))
    if interpolated:
        yaw += profile.max_curvature_1pm*gap
    return RoadLink(mapped_start, mapped_end, max(b.error_m for b in used)+curve_pad,
                    frozenset().union(*(b.sources for b in used)), observations, interpolated,min(math.pi,yaw),inferred_roles)


def _mapped_candidate(run, key, profile):
    first_current = next((i for i,link in enumerate(run) if key in link.sources),None)
    if first_current is None:
        return None
    start = max(0,first_current-30)
    run = run[start:start+31]
    stations = (run[0].start, *(link.end for link in run))
    left, divider, right = map(tuple,zip(*stations))
    lengths = [0.]
    for a,b in zip(divider,divider[1:]):
        lengths.append(lengths[-1]+math.dist(a,b))
    if lengths[-1] < profile.min_usable_length_m:
        return None
    sources = frozenset().union(*(link.sources for link in run))
    roles = tuple(tuple(sorted({i for link in run for source,i in link.observations[role] if source == key}))
                  for role in range(3))
    if sum(map(len,roles)) > 32:
        return None
    error = max(link.error_m for link in run)+profile.station_error_m
    yaw = max(link.yaw_error_rad for link in run)
    centers = tuple(tuple(((a[0]+b[0])/2,(a[1]+b[1])/2) for a,b in zip(divider,outer))
                    for outer in (left,right))
    directions = tuple((b[0]-a[0],b[1]-a[1]) for a,b in zip(divider,divider[1:]))
    if any((b[0]-a[0])*dx+(b[1]-a[1])*dy <= 0
           for center in centers for (a,b),(dx,dy) in zip(zip(center,center[1:]),directions)):
        return None
    intervals = []
    for outer in (left,right):
        widths = []
        for i,(a,b) in enumerate(zip(divider,outer)):
            j = min(i,len(divider)-2)
            dx = divider[j+1][0]-divider[j][0]
            dy = divider[j+1][1]-divider[j][1]
            widths.append(abs(dx*(b[1]-a[1])-dy*(b[0]-a[0]))/math.hypot(dx,dy))
        direction_pad = 2*max(math.dist(a,b) for a,b in zip(divider,outer))*math.sin(yaw/2)
        pad = 2*error+direction_pad
        intervals.append((max(0.,min(widths)-pad),max(widths)+pad))
    inferred = (run[0].interpolated, *(link.interpolated for link in run))
    hypothesis = LaneHypothesis(0,roles,tuple(lengths),divider,left,right,*centers,inferred,tuple(intervals),error,yaw)
    return MappedRoad(RoadResult('PARTIAL' if any(inferred) else 'VALID',(hypothesis,),True,
                                 ('MULTI_FRAME_TAPE',),key,True),sources)


def _link_heading(link):
    a,b = link.start[1],link.end[1]
    return math.atan2(b[1]-a[1],b[0]-a[0])


def _matching_station(station, link, tolerance):
    fractions = []
    shifts = []
    for point,a,b in zip(station,link.start,link.end):
        dx,dy = b[0]-a[0],b[1]-a[1]
        length2 = dx*dx+dy*dy
        if length2 <= 1e-18:
            return None
        fraction = ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length2
        fractions.append(fraction)
        t = min(1.,max(0.,fraction))
        shifts.append(math.hypot(point[0]-a[0]-t*dx,point[1]-a[1]-t*dy))
    if not -1e-9 <= fractions[1] <= 1+1e-9:
        return None
    shift = max(shifts)
    if shift <= tolerance:
        return min(1.,max(0.,fractions[1])),shift


def _join_link(neighbor, incoming, prepend, profile, tolerance):
    if abs(wrap_yaw(_link_heading(incoming)-_link_heading(neighbor))) > _DIRECTION_MATCH_RAD:
        return None
    station = neighbor.start if prepend else neighbor.end
    match = _matching_station(station,incoming,tolerance)
    if match is None:
        return None
    t,shift = match
    if (t if prepend else 1-t)*math.dist(incoming.start[1],incoming.end[1]) <= max(1e-9,profile.station_error_m):
        return None
    start,end = (incoming.start,station) if prepend else (station,incoming.end)
    dx,dy = end[1][0]-start[1][0],end[1][1]-start[1][1]
    if any((b[0]-a[0])*dx+(b[1]-a[1])*dy <= 0 for a,b in zip(start,end)):
        return None
    yaw = incoming.yaw_error_rad+abs(wrap_yaw(math.atan2(dy,dx)-_link_heading(incoming)))
    inferred = tuple(a or b and shift>1e-9 for a,b in zip(incoming.inferred_roles,neighbor.inferred_roles))
    return RoadLink(start,end,incoming.error_m+shift,
        neighbor.sources|incoming.sources,tuple(a|b for a,b in zip(neighbor.observations,incoming.observations)),
        any(inferred),min(math.pi,yaw),inferred)


def _end_heading(section, at_start):
    links = section.links if at_start else tuple(reversed(section.links))
    fixed = links[0].start[1] if at_start else links[0].end[1]
    for link in links:
        other = link.end[1] if at_start else link.start[1]
        if math.dist(fixed,other) >= 2*_STATION_STEP_M:
            break
    a,b = (fixed,other) if at_start else (other,fixed)
    return math.atan2(b[1]-a[1],b[0]-a[0])


def _gap_links(before, after, heading_a, heading_b, profile):
    start,end = before.end,after.start
    dx,dy = end[1][0]-start[1][0],end[1][1]-start[1][1]
    length = math.hypot(dx,dy)
    heading = math.atan2(dy,dx)
    delta = wrap_yaw(heading_b-heading_a)
    if (length <= 1e-9 or length > profile.max_gap_m
            or sum(bool(a and b) for a,b in zip(before.observations,after.observations)) < 2
            or abs(delta) > min(.85,profile.merge_heading_rad+profile.max_curvature_1pm*length)
            or abs(wrap_yaw(heading-heading_a-delta/2)) > _DIRECTION_MATCH_RAD):
        return ()
    directions = (math.cos(heading_a),math.sin(heading_a)),(math.cos(heading_b),math.sin(heading_b))
    distances = tuple(math.dist(a,b) for a,b in zip(start,end))
    count = max(1,math.ceil(length/_STATION_STEP_M))
    stations = [start]
    deviation = 0.
    for i in range(1,count):
        t = i/count
        u,v = t*(1-t)**2,t*t*(t-1)
        weight = t*t*(3-2*t)
        station = tuple(tuple(a[k]+weight*(b[k]-a[k])+d*(u*directions[0][k]+v*directions[1][k])
                              for k in (0,1)) for a,b,d in zip(start,end,distances))
        deviation = max(deviation,*(math.dist(p,tuple(a[k]+t*(b[k]-a[k]) for k in (0,1)))
                                    for p,a,b in zip(station,start,end)))
        stations.append(station)
    stations.append(end)
    low,high = profile.missing_outer_width_bounds_m
    pad = profile.merge_lateral_m
    for i,station in enumerate(stations):
        direction = heading_a+delta*i/count
        nx,ny = -math.sin(direction),math.cos(direction)
        middle = station[1]
        if any(not low-pad <= side*((point[0]-middle[0])*nx+(point[1]-middle[1])*ny) <= high+pad
               for point,side in ((station[0],1),(station[2],-1))):
            return ()
    for a,b in zip(stations,stations[1:]):
        cx,cy = b[1][0]-a[1][0],b[1][1]-a[1][1]
        if cx*dx+cy*dy <= 0 or any((q[0]-p[0])*cx+(q[1]-p[1])*cy <= 0 for p,q in zip(a,b)):
            return ()
    curve = profile.max_curvature_1pm*max(distances)**2/8
    yaw = max(before.yaw_error_rad,after.yaw_error_rad)+profile.max_curvature_1pm*length
    error = max(before.error_m,after.error_m)+curve+deviation
    observations = tuple(a|b for a,b in zip(before.observations,after.observations))
    return tuple(RoadLink(a,b,error,before.sources|after.sources,observations,True,min(math.pi,yaw),(True,True,True))
                 for a,b in zip(stations,stations[1:]))


def _connect_sections(sections, profile):
    # 관측 경계 둘 이상과 진행 방향이 맞는 짧은 이음만 연결한다.
    joins = {}
    predecessors = {}
    headings = [(_end_heading(section,True),_end_heading(section,False)) for section in sections]
    for i,before in enumerate(sections):
        for j,after in enumerate(sections):
            if i != j and (bridge := _gap_links(before.links[-1],after.links[0],headings[i][1],headings[j][0],profile)):
                distance = math.dist(bridge[0].start[1],bridge[-1].end[1])
                joins.setdefault(i,[]).append((distance,j,bridge))
                predecessors.setdefault(j,[]).append((distance,i))
    successors = {}
    for i,choices in joins.items():
        distance,j,bridge = min(choices)
        if min(predecessors[j])[1] != i:
            continue
        if any(other!=j and gap<=distance+_STATION_STEP_M
               and math.dist(bridge[-1].end[1],alternative[-1].end[1])>_STATION_STEP_M
               for gap,other,alternative in choices):
            continue
        successors[i] = j,bridge
    downstream = {j for j,_ in successors.values()}
    result = []
    consumed = set()
    for i,section in enumerate(sections):
        if i in downstream:
            continue
        links = list(section.links)
        consumed.add(i)
        while i in successors:
            i,bridge = successors[i]
            links.extend(bridge)
            links.extend(sections[i].links)
            consumed.add(i)
        result.append(RoadSection(tuple(links)))
    result.extend(section for i,section in enumerate(sections) if i not in consumed)
    return tuple(result)


def _extend_reference(section, observations, profile, *, backward, occupied):
    """관측으로 지지되는 만큼 중앙 기준선을 따라 다음 단면을 만든다."""
    width = profile.missing_outer_width_m
    sign = -1 if backward else 1
    seed = section.links[0] if backward else section.links[-1]
    station = seed.start if backward else seed.end
    heading = _end_heading(section, backward)
    error, yaw = seed.error_m, seed.yaw_error_rad
    anchor_sources = seed.sources
    support = seed.observations
    curvature = 0.
    result = []
    visited = []
    supported = 0
    gap = 0.
    step = _STATION_STEP_M
    max_turn = min(.20, profile.max_curvature_1pm*step, .9*step/width)
    for _ in range(_LINK_LIMIT):
        previous_error, previous_yaw = error, yaw
        previous_gap = gap
        turn = sign * curvature * step
        direction = heading + turn
        x, y = station[1]
        point = (x + sign*step*math.cos(heading + turn/2),
                 y + sign*step*math.sin(heading + turn/2))
        matched = observations.at(point, direction, width)
        if matched:
            residual = median(item[0] for item in matched.values())
            angle = median(item[1] for item in matched.values())
            direction = heading + max(-max_turn, min(max_turn, turn+.40*angle))
            curvature = .5*curvature + .5*wrap_yaw(direction-heading)/(sign*step)
            nx, ny = -math.sin(direction), math.cos(direction)
            center = point[0] + nx*residual, point[1] + ny*residual
            next_support = tuple(matched[role][4] if role in matched else frozenset() for role in range(3))
            error = max(item[2] + abs(item[0]-residual) for item in matched.values())
            if 1 not in matched:
                error += profile.missing_outer_width_error_m
            yaw = max(item[3] + abs(item[1]-angle) for item in matched.values())
            gap = 0.
        else:
            gap += step
            if gap > profile.max_gap_m:
                break
            center = point
            next_support = (frozenset(),)*3
        nx, ny = -math.sin(direction), math.cos(direction)
        next_station = tuple((center[0]+offset*nx,center[1]+offset*ny) for offset in (width,0.,-width))
        start, end = (next_station,station) if backward else (station,next_station)
        dx, dy = end[1][0]-start[1][0], end[1][1]-start[1][1]
        if any((b[0]-a[0])*dx+(b[1]-a[1])*dy <= 0 for a,b in zip(start,end)):
            break
        # 한 바퀴를 돌아 같은 방향의 기준선을 만나면 같은 도로를 다시 따라가지 않는다.
        if any(math.dist(center,p) < 1.5*step and abs(wrap_yaw(direction-h)) < _DIRECTION_MATCH_RAD
               for p,h in (*occupied,*visited[:-2])):
            break
        inferred = tuple(not bool(a & b) for a,b in zip(support,next_support))
        new_sources = frozenset(source for group in next_support for source,_ in group)
        sources = anchor_sources | new_sources
        if new_sources:
            anchor_sources = new_sources
        unsupported_distance = max(gap,previous_gap)
        shape_error = (max(error,previous_error)+profile.station_error_m
                       +profile.max_curvature_1pm*unsupported_distance**2/8)
        result.append(RoadLink(start,end,shape_error,sources,tuple(a|b for a,b in zip(support,next_support)),
                               any(inferred),min(math.pi,max(yaw,previous_yaw)+abs(wrap_yaw(direction-heading))+
                                               profile.max_curvature_1pm*unsupported_distance),inferred))
        visited.append((center,direction))
        if matched:
            supported = len(result)
        station, heading, support = next_station, direction, next_support
    links = result[:supported]
    return tuple(reversed(links)) if backward else tuple(links)


def _follow_reference(seed, observations, profile):
    after = _extend_reference(seed,observations,profile,backward=False,
        occupied=tuple((link.start[1],_link_heading(link)) for link in seed.links
                       if math.dist(link.start[1],seed.links[-1].end[1]) > _STATION_STEP_M))
    before = _extend_reference(seed,observations,profile,backward=True,
        occupied=tuple((link.end[1],_link_heading(link)) for link in (*seed.links,*after)
                       if math.dist(link.end[1],seed.links[0].start[1]) > _STATION_STEP_M))
    return RoadSection(before+seed.links+after) if before or after else seed


def _section_length(section):
    return sum(math.dist(link.start[1],link.end[1]) for link in section.links)


class TemporalRoadMap:
    """최근 관측의 공통 단면을 찾고 지도 좌표의 구간을 유한 개수로 보관한다."""

    def __init__(self):
        self.clear()

    def clear(self, *, keep_startup=False):
        self._frames = deque(maxlen=_MAP_FRAME_LIMIT)
        self._recent_sources = deque(maxlen=_FRAME_LIMIT)
        if not keep_startup:
            self._startup = deque(maxlen=_MAP_FRAME_LIMIT)
        self._markings = deque(maxlen=_MARKING_LIMIT)
        self._startup_seeds = deque()
        self._links = {}
        self._roles = {}
        self._map_roles = {}
        self._last_source = None
        self.parking_branches = ()
        self._entrance_recheck = 0
        self.revision = 0

    def __deepcopy__(self, memo):
        # frame/strip/link는 불변이다. 후보 코어는 owner의 가변 컨테이너만 복제한다.
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        clone._frames = self._frames.copy()
        clone._recent_sources = self._recent_sources.copy()
        clone._startup = self._startup.copy()
        clone._markings = self._markings.copy()
        clone._startup_seeds = self._startup_seeds.copy()
        clone._links = self._links.copy()
        clone._roles = {key:roles.copy() for key,roles in self._roles.items()}
        clone._map_roles = {key:roles.copy() for key,roles in self._map_roles.items()}
        clone._last_source = self._last_source
        clone.parking_branches = self.parking_branches
        clone._entrance_recheck = self._entrance_recheck
        clone.revision = self.revision
        return clone

    @property
    def links(self):
        return tuple(self._links.values())

    @property
    def startup_markings(self):
        return tuple(self._startup)

    @property
    def markings(self):
        return tuple(self._markings)

    def marking_joins(self, profile):
        return _join_markings(tuple(s for s in self._markings if not s.original.closed),self._roles,profile)

    def collect_startup(self, graph, stamp, pose):
        if graph is None or graph.source_key != stamp.key or not graph.calibration_qualified:
            return
        if self._startup and self._startup[-1].stamp.key[:-1] != stamp.key[:-1]:
            self._startup.clear()
        strips = _observed_strips(graph)
        if strips:
            self._startup.append(StartupMarkings(stamp,pose,strips,_usable_graph(graph)))

    def place_startup(self, alignment, *, stamp):
        """원 RGB 자료를 첫 정합으로 배치하고 누적 지도에만 사용한다."""
        if not alignment.has_metadata:
            return
        while self._startup:
            original = self._startup.popleft()
            if (original.stamp.key[:-1] != stamp.key[:-1] or original.pose is None
                    or original.pose.sample.context != alignment.context):
                continue
            if original.pose.captured_us[1] > alignment.observed_j_interval[0]:
                continue
            try:
                mapped = alignment.project_history(original.pose)
            except ValueError:
                continue
            strips = _project_strips(original.strips,mapped,original.stamp.key,
                stamp=original.stamp,capture_pose=original.pose,historical=True)
            self._markings.extend(strips)
            if original.usable_for_matching:
                matching = _matching_strips(strips)
                self._frames.append(matching)
                self._startup_seeds.extend(matching[:2])

    def sections(self, profile):
        """확인된 단면을 시작점으로 원 선 관측을 따라 지도 기준선을 연장한다."""
        pending = sorted((link for link in self.links if all(p is not None for p in link.start)),
                         key=lambda link: (sum(link.inferred_roles),link.error_m))
        tolerance = max(profile.merge_lateral_m,profile.missing_outer_width_error_m)
        sections = []
        while pending:
            section = deque([pending.pop(0)])
            for prepend in (False,True):
                while pending:
                    neighbor = section[0] if prepend else section[-1]
                    choices = [(i,joined) for i,incoming in enumerate(pending)
                               if (joined := _join_link(neighbor,incoming,prepend,profile,tolerance)) is not None]
                    if not choices:
                        break
                    i,link = min(choices,key=lambda item: (sum(item[1].inferred_roles),
                        -math.dist(item[1].start[1],item[1].end[1]),item[1].error_m))
                    pending.pop(i)
                    (section.appendleft if prepend else section.append)(link)
            sections.append(RoadSection(tuple(section)))
            # 중첩 관측은 원 링크에 남긴다. 양끝이 이미 포함된 조각은 중복 차로가 아니다.
            pending = [incoming for incoming in pending if not all(any(
                abs(wrap_yaw(_link_heading(link)-_link_heading(incoming))) <= _DIRECTION_MATCH_RAD
                and _matching_station(station,link,tolerance) is not None for link in section)
                for station in (incoming.start,incoming.end))]
        sections = tuple(section for section in _connect_sections(sections,profile)
                         if _section_length(section) >= profile.min_usable_length_m)
        if not sections or not self._markings:
            return sections
        roles = {key: set(self._roles.get(key,())) | set(self._map_roles.get(key,()))
                 for key in self._roles.keys() | self._map_roles.keys()}
        observations = ReferenceObservations(self._markings,roles,profile)
        # 새로 생긴 긴 단면 하나가 기존 코너의 지도를 밀어내지 않도록 몇 개의 시작점을 비교한다.
        seeds = sorted(sections,key=_section_length,reverse=True)[:3]
        seed,reference = max(((seed,_follow_reference(seed,observations,profile)) for seed in seeds),
                             key=lambda pair: _section_length(pair[1]))
        if reference is seed:
            return sections
        return (reference,*(section for section in sections if section is not seed))

    @property
    def source_keys(self):
        retained = {frame[0].source for frame in self._frames if frame}
        return frozenset(key for key in self._recent_sources if key in retained)

    def observe(self, graph, pose, road_profile, tape_profile, *, stamp=None, capture_pose=None, qualified=True):
        if (graph is None or graph.source_key is None or graph.source_key == self._last_source
                or not graph.calibration_qualified):
            return
        sample = pose.sample
        transform = (sample.x, sample.y, sample.yaw)
        projected = _project_strips(_observed_strips(graph),pose,graph.source_key,
            stamp=stamp,capture_pose=capture_pose)
        self._last_source = graph.source_key
        self._markings.extend(projected)
        if not _usable_graph(graph):
            return
        strips = _matching_strips(projected)
        self._frames.append(strips)
        if qualified:
            self._recent_sources.append(graph.source_key if strips else None)
        active = {(s.source,s.identity) for frame in self._frames for s in frame}
        self._roles = {key:roles for key,roles in self._roles.items() if key in active}
        self._map_roles = {key:roles for key,roles in self._map_roles.items() if key in active}
        if not strips:
            return
        self._collect_map_geometry(strips,road_profile)
        if not qualified:
            return

        first = max(-5, math.ceil((tape_profile.x_min_m-.40)/_STATION_STEP_M))
        last = min(40, math.floor(tape_profile.x_max_m/_STATION_STEP_M))
        lateral_range = min(max(abs(tape_profile.y_min_m),abs(tape_profile.y_max_m)),
                            2.5*road_profile.missing_outer_width_m)
        stations = {i: [] for i in range(first,last+1)}
        recent_sources = self.source_keys
        c,s = math.cos(sample.yaw),math.sin(sample.yaw)
        for frame in self._frames:
            if not frame or frame[0].source not in recent_sources:
                continue
            for strip in frame:
                offsets = ((x-sample.x,y-sample.y) for x,y in strip.points)
                local = tuple((c*x+s*y,-s*x+c*y) for x,y in offsets)
                for a, b, yaw_error in zip(local,local[1:],strip.heading_errors):
                    if a[0] > b[0]:
                        a,b = b,a
                    dx = b[0]-a[0]
                    if dx <= 1e-6:
                        continue
                    heading = math.atan2(b[1]-a[1],dx)
                    if abs(heading) > road_profile.max_heading_rad:
                        continue
                    begin = max(first, math.ceil(a[0]/_STATION_STEP_M))
                    end = min(last, math.floor(b[0]/_STATION_STEP_M))
                    for i in range(begin,end+1):
                        y = a[1]+(b[1]-a[1])*(i*_STATION_STEP_M-a[0])/dx
                        if abs(y) <= lateral_range:
                            stations[i].append((y,heading,strip.error_m,strip.source,strip.identity,yaw_error))
        previous = None
        run = []
        candidates, interpolated = [], []
        for index,samples in stations.items():
            current, ambiguous = _road_section(_bands(samples,self._roles), road_profile.missing_outer_width_bounds_m)
            if ambiguous:
                self._retain(run,road_profile,graph.source_key,candidates,interpolated)
                previous,run = None,[]
            if current is None:
                continue
            candidate = (index*_STATION_STEP_M,current)
            link = None if previous is None else _link(previous,candidate,transform,road_profile)
            if link is None:
                self._retain(run,road_profile,graph.source_key,candidates,interpolated)
                run = []
            else:
                run.append(link)
            previous = candidate
        self._retain(run,road_profile,graph.source_key,candidates,interpolated)
        self.parking_branches = observe_entrances(self.parking_branches, projected, self.links)
        if not self._entrance_recheck:
            # 코너의 진입선이 먼저 보이고 차로 기준선이 뒤늦게 이어질 수 있다.
            history = tuple(strip for frame in self._frames for strip in frame
                            if strip.source in recent_sources)
            self.parking_branches = observe_entrances(self.parking_branches, history,
                (link for section in self.sections(road_profile) for link in section.links), followed=True)
        self._entrance_recheck = (self._entrance_recheck+1) % _ENTRANCE_RECHECK_FRAMES
        candidates = candidates or interpolated
        if len(candidates) == 1:
            return candidates[0]

    def _retain(self, run, profile, key, candidates, interpolated):
        qualified_run = sum(math.dist(link.start[1],link.end[1]) for link in run) >= profile.min_usable_length_m
        complete = []
        observed = []
        for link in (*run,None) if qualified_run else ():
            full = link is not None and all(p is not None for p in link.start)
            if full:
                complete.append(link)
                if not link.interpolated:
                    observed.append(link)
                    continue
            candidate = _mapped_candidate(observed,key,profile)
            if candidate is not None:
                candidates.append(candidate)
            observed = []
            if full:
                continue
            if any(link.interpolated for link in complete):
                candidate = _mapped_candidate(complete,key,profile)
                if candidate is not None:
                    interpolated.append(candidate)
            complete = []
        retained = []
        for link in run:
            if qualified_run:
                for role, observations in enumerate(link.observations):
                    for identity in observations:
                        self._roles.setdefault(identity,set()).add(role)
            elif link.interpolated or any(point is None for point in link.start):
                continue
            retained.append(link)
        self._store(retained)

    def _collect_map_geometry(self, strips, profile):
        # 선의 국소 방향으로 단면을 만들어 차량 yaw에서 멀어지는 코너도 관측한다.
        seeds = tuple(self._startup_seeds.popleft() for _ in range(min(2,len(self._startup_seeds))))+strips[:6]
        history = tuple(strip for frame in self._frames for strip in frame)
        known = [(strip,_marking_ends(strip),role) for strip in history
                 if len(role:=self._map_roles.get((strip.source,strip.identity),()))==1]
        for seed in seeds:
            key = seed.source,seed.identity
            if key in self._map_roles:
                continue
            ends = _marking_ends(seed)
            roles = set().union(*(role for old,old_ends,role in known
                                 if _role_continuation(seed,ends,old,old_ends,profile)))
            if len(roles)==1:
                self._map_roles[key] = roles
        sections = normal_samples(history,seeds,width_m=profile.missing_outer_width_m,
            radius_m=3*profile.missing_outer_width_bounds_m[1]+_BAND_SPREAD_M,
            step_m=_STATION_STEP_M,heading_limit=.55,curvature=profile.max_curvature_1pm)
        for seed,stations in sections:
            previous = None
            run = []
            for point,heading,samples in stations:
                bands = _bands(samples,self._map_roles,map_geometry=True)
                section,ambiguous = ((None,True) if len(bands)>_MAP_BAND_LIMIT else _map_cross_section(bands,profile))
                if ambiguous:
                    self._retain_map_run(run,profile)
                    previous,run = None,[]
                    continue
                if section is None or not any(seed in band.strips for band in section):
                    continue
                candidate = _normal_station(point,heading,section)
                link = None if previous is None else _normal_link(previous,candidate,profile)
                if link is None:
                    self._retain_map_run(run,profile)
                    run = []
                else:
                    run.append(link)
                previous = candidate
            self._retain_map_run(run,profile)

    def _retain_map_run(self, run, profile):
        if sum(math.dist(link.start[1],link.end[1]) for link in run) >= profile.min_usable_length_m:
            for link in run:
                for role,observations in enumerate(link.observations):
                    for identity in observations:
                        self._map_roles.setdefault(identity,set()).add(role)
            self._store(run)

    def _store(self, links):
        changed = False
        for link in links:
            key = tuple(round(value/_STATION_STEP_M) for point in (link.start[1],link.end[1]) for value in point)
            old = self._links.get(key)
            quality = (sum(p is None for p in link.start),sum(link.inferred_roles),link.error_m)
            if old is not None and (sum(p is None for p in old.start),sum(old.inferred_roles),old.error_m) <= quality:
                continue
            if old is None and len(self._links) >= _LINK_LIMIT:
                self._links.pop(next(iter(self._links)))
            self._links[key] = link
            changed = True
        if changed:
            self.revision += 1
