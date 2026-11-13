"""차로 지도와 실제 테이프 관측에서 주차 접근 후보를 찾는다.

관측한 분기점과 그 사이의 접근 구간만 다룬다. 주차칸 형상과 점유는 별도다.
"""

from dataclasses import asdict, dataclass, replace
from itertools import pairwise
import math

from .parking import ParkingAisle, ParkingProfile
from .tape import TapeJunction, TapeStrip
from ..mapping.registration import segment_distance


# 실측 영상으로 조정할 초기값. 관측된 분기점끼리만 지도에서 연관한다.
_CONTACT_M = .075
_INSIDE_M = .035
_BRANCH_M = .06
_DIRECTION_COS = math.cos(math.radians(30))
_ASSOCIATION_M = .12
_LIMIT = 32
_SLOT_WIDTH_M = ParkingProfile().width_m


@dataclass(frozen=True)
class EntranceBranch:
    xy: tuple
    outward: tuple
    edge_role: int
    error_m: float
    first_source: tuple
    last_source: tuple
    observations: int = 1


@dataclass(frozen=True)
class EntranceSpan:
    endpoints: tuple
    outward: tuple
    edge_role: int
    error_m: float


def _relative(point, origin, axis):
    return (point[0]-origin[0])*axis[0] + (point[1]-origin[1])*axis[1]


def _edges(links, *, followed):
    for link in links:
        # 한쪽 경계가 가려졌다면 같은 단면의 실제 중앙선·반대 경계 관측도 쓸 수 있다.
        observed_section = followed and any(link.observations)
        for role in (0, 2):
            a, b = link.start[role], link.end[role]
            if a is None or b is None or link.inferred_roles[role] and not observed_section:
                continue
            length = math.dist(a, b)
            if length < .01:
                continue
            tangent = ((b[0]-a[0])/length, (b[1]-a[1])/length)
            normal = (-tangent[1], tangent[0])
            if _relative(link.start[1], a, normal) > 0:
                normal = (-normal[0], -normal[1])
            yield a, length, tangent, normal, role, link.error_m


def _contact(p, q, edge):
    a, length, tangent, normal, role, error = edge
    near, far = _relative(p, a, normal), _relative(q, a, normal)
    if near > far:
        p, q, near, far = q, p, far, near
    if (not -_INSIDE_M <= near <= _CONTACT_M or far < _BRANCH_M
            or far-near < max(_BRANCH_M, _DIRECTION_COS*math.dist(p, q))):
        return None
    station = _relative(p, a, tangent)
    if not -_CONTACT_M <= station <= length+_CONTACT_M:
        return None
    # 접점이 짧게 가려졌다면 선을 경계까지 연장한다. 원 영상의 오차와 연장량을 남긴다.
    station -= near*_relative(q, p, tangent)/(far-near)
    overflow = max(-station, station-length, 0.)
    if overflow > _CONTACT_M:
        return None
    xy = (a[0]+station*tangent[0], a[1]+station*tangent[1])
    cost = abs(near) + overflow
    branch_length = math.dist(p, q)
    # Preserve the observed branch direction across views of the same curved road edge.
    outward = ((q[0]-p[0])/branch_length, (q[1]-p[1])/branch_length)
    return cost, xy, outward, role, error


def _same_branch(a, b):
    return (a.edge_role == b.edge_role and math.dist(a.xy, b.xy) <= _ASSOCIATION_M
            and _relative(a.outward, (0., 0.), b.outward) >= _DIRECTION_COS)


def _branch_chords(points):
    # Tape skeletons split diagonal paint into raster steps. Measure the direction
    # across an observed run, retaining isolated long segments and rejecting bends.
    for index, p in enumerate(points[:-1]):
        walked = 0.
        for j in range(index+1, len(points)):
            q = points[j]
            walked += math.dist(points[j-1], q)
            chord = math.dist(p, q)
            if chord >= _BRANCH_M:
                if all(segment_distance(point, p, q) <= .02 for point in points[index+1:j]):
                    yield p, q
                break
            if walked > 2*_BRANCH_M:
                break


def observe_entrances(previous, strips, links, *, followed=False):
    """동일 지도에서 실제 분기점만 합친다. 과거 RGB를 다시 대조해도 한 번만 센다."""
    edges = tuple(_edges(links, followed=followed))
    if not edges:
        return previous
    branches = list(previous)
    for strip in strips:
        for p, q in _branch_chords(strip.points):
            contact = min((found for edge in edges if (found := _contact(p, q, edge)) is not None),
                          key=lambda item: (item[0], item[4]), default=None)
            if contact is None:
                continue
            cost, xy, outward, role, error = contact
            observation = EntranceBranch(xy, outward, role, strip.error_m+error+cost,
                                         strip.source, strip.source)
            index = min((i for i, old in enumerate(branches) if _same_branch(old, observation)),
                        key=lambda i: math.dist(branches[i].xy, xy), default=None)
            if index is None:
                branches.append(observation)
                continue
            old = branches[index]
            if old.last_source[:-1] == strip.source[:-1] and old.last_source[-1] >= strip.source[-1]:
                continue
            best = min((old, observation), key=lambda value: value.error_m)
            branches[index] = replace(best, first_source=old.first_source, last_source=strip.source,
                                      observations=old.observations+1)
    if len(branches) > _LIMIT:
        branches.sort(key=lambda branch: (-branch.observations, branch.error_m))
    return tuple(branches[:_LIMIT])


def entrance_spans(branches):
    """Return pairs of independently observed branches in the map frame."""
    return _pair_branches(tuple(branch for branch in branches if branch.observations >= 2))


def _pair_branches(branches):
    segments = {}
    for i, a in enumerate(branches):
        choices = []
        for j, b in enumerate(branches):
            if a.edge_role != b.edge_role or _relative(a.outward, (0., 0.), b.outward) < _DIRECTION_COS:
                continue
            nx, ny = (x+y for x,y in zip(a.outward,b.outward))
            norm = math.hypot(nx,ny)
            if not norm:
                continue
            outward = (nx/norm,ny/norm)
            tangent = (outward[1],-outward[0])
            distance = _relative(b.xy, a.xy, tangent)
            if distance > 0 and abs(_relative(b.xy, a.xy, outward)) <= _CONTACT_M:
                choices.append((distance,j,outward))
        if choices:
            distance,j,outward = min(choices)
            if _SLOT_WIDTH_M[0] <= distance <= _SLOT_WIDTH_M[1]:
                segments[tuple(sorted((i,j)))] = outward
    return tuple(EntranceSpan((branches[i].xy, branches[j].xy), segments[i,j],
                              branches[i].edge_role, max(branches[i].error_m, branches[j].error_m))
                 for i, j in sorted(segments))


def entrance_summary(branches, spans=None):
    spans = entrance_spans(branches) if spans is None else spans
    candidates = dict.fromkeys((*spans, *_pair_branches(branches)))
    return dict(branches=[asdict(branch) for branch in branches],
                candidates=[asdict(span) for span in candidates],
                approach_segments=[asdict(span) for span in spans])


def parking_approach_contacts(span, edge):
    """Find the road edge tangent at the entrance, including on a curved edge."""
    a, b = span.endpoints
    middle = ((a[0]+b[0])/2, (a[1]+b[1])/2)
    p, q = min(pairwise(edge), key=lambda segment: segment_distance(middle, *segment))
    if segment_distance(middle, p, q) > _CONTACT_M:
        return None
    direction = (q[0]-p[0], q[1]-p[1])
    side = 1 if span.edge_role == 0 else -1
    outward = (-side*direction[1], side*direction[0])
    if _relative(span.outward, (0., 0.), outward) < _DIRECTION_COS*math.hypot(*outward):
        return None
    return direction


def parking_mouths(spans, graph, capture_pose):
    """Measure mouth direction in the current RGB; the span only locates the search."""
    if (not spans or graph is None or not graph.calibration_qualified
            or not graph.complete and set(graph.reasons) != {'NO_MEASURED_TAPE_WIDTH'}):
        return ()
    x, y, yaw = capture_pose
    forward, left = (math.cos(yaw), math.sin(yaw)), (-math.sin(yaw), math.cos(yaw))

    def to_body(point):
        return _relative(point, (x, y), forward), _relative(point, (x, y), left)

    mouths = []
    for span in spans:
        a, b = span.endpoints
        side = 1 if span.edge_role == 0 else -1
        if side*((a[1]-b[1])*span.outward[0]+(b[0]-a[0])*span.outward[1]) < 0:
            a, b = b, a
        aisle = _measure_shared_line(graph, to_body(a), to_body(b), side, 0)
        if aisle is not None:
            mouths.append((span,aisle))
    return tuple(mouths)


def parking_aisles(spans, graph, lane_model, capture_pose):
    """Attach a locally observed mouth to the stable lane it borders."""
    h = lane_model.road.hypotheses[0]
    boundaries = (h.left_outer,h.right_outer)
    aisles = []
    for span, aisle in parking_mouths(spans,graph,capture_pose):
        a,b = span.endpoints
        middle = ((a[0]+b[0])/2,(a[1]+b[1])/2)
        index = span.edge_role//2
        if any(segment_distance(middle, p, q) <= _CONTACT_M for p, q in pairwise(boundaries[1-index])):
            continue
        if parking_approach_contacts(span,boundaries[index]) is not None:
            aisles.append(replace(aisle,approach_lane_id=lane_model.lane_ids[index]))
            if len(aisles) == 4:
                break
    return tuple(aisles)


def _measure_shared_line(graph, a, b, side, lane_id):
    length = math.dist(a, b)
    if length < _SLOT_WIDTH_M[0] or length > _SLOT_WIDTH_M[1]:
        return None
    tangent = ((b[0]-a[0])/length, (b[1]-a[1])/length)
    normal = (-tangent[1], tangent[0])
    samples = {}
    for strip in graph.strips:
        if not strip.observed:
            continue
        for p, q in pairwise(strip.points_xy):
            s0, s1 = _relative(p, a, tangent), _relative(q, a, tangent)
            if abs(s1-s0) < _DIRECTION_COS*math.dist(p, q) or s0 == s1:
                continue
            lo, hi = max(min(s0,s1), -_CONTACT_M), min(max(s0,s1), length+_CONTACT_M)
            if lo >= hi:
                continue
            d0, d1 = _relative(p, a, normal), _relative(q, a, normal)
            for s in (lo, hi):
                d = d0 + (d1-d0)*(s-s0)/(s1-s0)
                if abs(d) <= _CONTACT_M:
                    samples[s, d] = max(samples.get((s, d), 0.), strip.error_m)
    if len(samples) < 2:
        return None
    lo, hi = min(s for s, _ in samples), max(s for s, _ in samples)
    if lo > _CONTACT_M or hi < length-_CONTACT_M:
        return None
    mean_s = sum(s for s, _ in samples)/len(samples)
    mean_d = sum(d for _, d in samples)/len(samples)
    slope = sum((s-mean_s)*(d-mean_d) for s, d in samples)/sum((s-mean_s)**2 for s, _ in samples)
    residual = max(abs(d-mean_d-slope*(s-mean_s)) for s, d in samples)
    if residual > ParkingProfile().max_line_residual_m:
        return None

    def point(s):
        d = mean_d+slope*(s-mean_s)
        return a[0]+s*tangent[0]+d*normal[0], a[1]+s*tangent[1]+d*normal[1]

    return ParkingAisle(point(-.025), point(length+.025), side,
                        max(samples.values())+residual, lane_id)


def _wall_runs(frames, origin, along, across, across_range, along_range):
    """Select contiguous sections of each original line near one bay edge."""
    for key, strips in frames:
        for strip in strips:
            if not strip.observed or strip.closed:
                continue
            run = []
            coords = tuple((_relative(p,origin,along),_relative(p,origin,across)) for p in strip.points_xy)
            for i, (u, v) in enumerate((*coords, (math.inf, math.inf))):
                if along_range[0] <= u <= along_range[1] and across_range[0] <= v <= across_range[1]:
                    run.append((u,v,i))
                    continue
                if len(run) >= 2:
                    first, last = run[0], run[-1]
                    if abs(last[0]-first[0]) >= .06 and abs(last[1]-first[1]) <= .35*abs(last[0]-first[0]):
                        if first[0] > last[0]:
                            run.reverse()
                        yield key,strip,tuple(run)
                run = []


def _join_wall(runs, origin, along, across, role):
    """Join overlapping runs of the same edge, keeping their original view keys."""
    if not runs:
        return None
    def lo(run): return run[2][0][0]
    def hi(run): return run[2][-1][0]
    def offset(run): return sum(p[1] for p in run[2])/len(run[2])
    seed = max(runs, key=lambda run: hi(run)-lo(run))
    center = offset(seed)
    matching = sorted((run for run in runs if abs(offset(run)-center) <= .035),key=lo)
    groups = []
    end = -math.inf
    for run in matching:
        if lo(run) > end+.035:
            groups.append([])
        groups[-1].append(run)
        end = max(end,hi(run))
    group = next(group for group in groups if seed in group)
    # One dense skeleton should not count as more independent views.
    points = tuple(p for run in group for p in (run[2][0],run[2][-1]))
    mu = sum(p[0] for p in points)/len(points)
    mv = sum(p[1] for p in points)/len(points)
    slope = sum((u-mu)*(v-mv) for u,v,_ in points)/sum((u-mu)**2 for u,_,_ in points)
    def lateral(u): return mv+slope*(u-mu)
    def point(u): return tuple(x+u*a+lateral(u)*b for x,a,b in zip(origin,along,across))
    start,end = min(map(lo,group)),max(map(hi,group))
    errors = tuple((lo(run),hi(run),run[1].error_m+max(abs(v-lateral(u)) for u,v,_ in run[2])) for run in group)
    cuts = sorted({u for a,b,_ in errors for u in (a,b)})
    coverage = []
    for a,b in pairwise(cuts):
        direct = min((e for lo_,hi_,e in errors if lo_ <= a and b <= hi_),default=None)
        if direct is None:
            direct = max(min(e for _,hi_,e in errors if hi_ == a),min(e for lo_,_,e in errors if lo_ == b))
        coverage.append(direct)
    error = max(coverage)
    width = (min(strip.width_interval_m[0] for _,strip,_ in group),
             max(strip.width_interval_m[1] for _,strip,_ in group))
    def kind(station):
        for _,strip,pts in group:
            for u,_,index in (pts[0],pts[-1]):
                if abs(u-station) <= .025 and index in (0,len(strip.points_xy)-1):
                    endpoint = strip.start_kind if index == 0 else strip.end_kind
                    if endpoint == 'physical_junction':
                        return endpoint
        return 'FOV_clip'
    junctions = tuple(dict.fromkeys(strip.points_xy[index] for _,strip,pts in group
        for _,_,index in (pts[0],pts[-1]) if (index == 0 and strip.start_kind == 'physical_junction'
                    or index == len(strip.points_xy)-1 and strip.end_kind == 'physical_junction')))
    return (TapeStrip(role,(point(start),point(end)),width,error,kind(start),kind(end)),
            frozenset(key for key,_,_ in group),junctions)


def parking_slot_tape(graph, aisle, history):
    """Remeasure the two bay sides and back in the frame of the observed mouth."""
    if (not history or not graph.calibration_qualified
            or not graph.complete and set(graph.reasons) != {'NO_MEASURED_TAPE_WIDTH'}):
        return None
    frames = (*history, (graph.source_key, graph.strips))
    profile = ParkingProfile()
    t,n = aisle.tangent,aisle.inward
    width = math.dist(aisle.a,aisle.b)
    # Shared-line fit includes a small margin beyond each detected mouth end.
    bands = ((0, n, t, (-.03,.09),(-.04,profile.depth_m[1])),
             (1, n, t, (width-.09,width+.03),(-.04,profile.depth_m[1])),
             (2, t, n, profile.depth_m,(-.05,width+.05)))
    walls = tuple(_join_wall(tuple(_wall_runs(frames,aisle.a,along,across,side,extent)),
                             aisle.a,along,across,role) for role,along,across,side,extent in bands)
    if any(wall is None for wall in walls):
        return None
    sources = frozenset().union(*(wall[1] for wall in walls), (graph.source_key,))
    if len(sources) < 2:
        return None
    junctions = tuple(TapeJunction(i,p,3,'physical_junction') for i,p in enumerate(dict.fromkeys(
        p for wall in walls for p in wall[2])))[:64]
    return replace(graph,strips=tuple(wall[0] for wall in walls), junctions=junctions,
                   complete=True, reasons=()), sources
