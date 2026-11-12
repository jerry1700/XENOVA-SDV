"""차로 지도와 실제 테이프 관측에서 주차 접근 후보를 찾는다.

관측한 분기점과 그 사이의 접근 구간만 다룬다. 주차칸 형상과 점유는 별도다.
"""

from dataclasses import asdict, dataclass, replace
from itertools import pairwise
import math

from .parking import ParkingProfile


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
    return cost, xy, normal, role, error


def _same_branch(a, b):
    return (a.edge_role == b.edge_role and math.dist(a.xy, b.xy) <= _ASSOCIATION_M
            and _relative(a.outward, (0., 0.), b.outward) >= _DIRECTION_COS)


def observe_entrances(previous, strips, links, *, followed=False):
    """동일 지도에서 실제 분기점만 합친다. 과거 RGB를 다시 대조해도 한 번만 센다."""
    edges = tuple(_edges(links, followed=followed))
    if not edges:
        return previous
    branches = list(previous)
    for strip in strips:
        for p, q in pairwise(strip.points):
            if math.dist(p, q) < _BRANCH_M:
                continue
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


def entrance_summary(branches):
    confirmed = [branch for branch in branches if branch.observations >= 2]
    segments = set()
    for i, a in enumerate(confirmed):
        tangent = (a.outward[1], -a.outward[0])
        choices = []
        for j, b in enumerate(confirmed):
            if a.edge_role != b.edge_role or _relative(a.outward, (0., 0.), b.outward) < _DIRECTION_COS:
                continue
            distance = _relative(b.xy, a.xy, tangent)
            if distance > 0 and abs(_relative(b.xy, a.xy, a.outward)) <= _CONTACT_M:
                choices.append((distance, j))
        if choices:
            distance, j = min(choices)
            if _SLOT_WIDTH_M[0] <= distance <= _SLOT_WIDTH_M[1]:
                segments.add(tuple(sorted((i, j))))
    return dict(branches=[asdict(branch) for branch in branches], approach_segments=[
        dict(endpoints=(confirmed[i].xy, confirmed[j].xy), outward=confirmed[i].outward,
             edge_role=confirmed[i].edge_role, error_m=max(confirmed[i].error_m, confirmed[j].error_m))
        for i, j in sorted(segments)])
