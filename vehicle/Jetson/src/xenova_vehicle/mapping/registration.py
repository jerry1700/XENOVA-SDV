"""유한 SE(2) 탐색의 가능한 전체 집합을 보존한다. 최저 loss는 위치 증명이 아니다.

result17의 중심거리 하한에서 translation/yaw 이동·관측/지도 오차를 빼서만 cell을
배제한다. 작업이 끝나지 않은 cell은 남긴다. frozen reference와 현재 capture는
분리하며, 물리 prior/오차의 타당성은 생산자가 검증해야 한다.
"""

from collections import deque
from dataclasses import dataclass, replace
import math


def _number(value, name, limit=10000):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
        raise ValueError(name + ' must be a bounded finite number')
    return float(value)


def _point(value):
    if type(value) is not tuple or len(value) != 2:
        raise ValueError('point must be an exact XY pair')
    return tuple(_number(v, 'coordinate') for v in value)


def wrap_yaw(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


def transform_point(point, pose):
    x, y = point
    px, py, yaw = pose
    c, s = math.cos(yaw), math.sin(yaw)
    return px + c * x - s * y, py + s * x + c * y


def segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    if den == 0:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    t = max(0., min(1., ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / den))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


@dataclass(frozen=True)
class PointFeature:
    point: tuple[float, float]
    error_m: float
    component: str
    capture_id: str
    compatible: tuple[str, ...] = ()
    error_xy: tuple[float, float] | None = None
    normal_only: bool = False
    endpoint_matches: tuple[tuple[str, int], ...] = ()

    def __post_init__(self):
        _point(self.point)
        if _number(self.error_m, 'point error') < 0:
            raise ValueError('negative point error')
        if not self.component or not self.capture_id or type(self.compatible) is not tuple:
            raise ValueError('feature requires component/source identity')
        if self.error_xy is not None and any(v < 0 for v in _point(self.error_xy)):
            raise ValueError('negative axis-wise point error')
        if type(self.normal_only) is not bool:
            raise ValueError('explicit point constraint kind required')
        if (type(self.endpoint_matches) is not tuple or self.endpoint_matches and
                (self.normal_only or tuple(segment for segment,_ in self.endpoint_matches)!=self.compatible
                 or any(type(end) is not int or end not in (0,1,2) for _,end in self.endpoint_matches))):
            raise ValueError('finite constraint requires an original finite segment correspondence')


@dataclass(frozen=True)
class SegmentFeature:
    a: tuple[float, float]
    b: tuple[float, float]
    error_m: float
    component: str
    capture_id: str
    support_error_m: float | None = None
    finite_endpoints: bool = True

    def __post_init__(self):
        _point(self.a)
        _point(self.b)
        if _number(self.error_m, 'segment error') < 0 or not self.component or not self.capture_id:
            raise ValueError('invalid segment error/source identity')
        if self.support_error_m is not None and _number(self.support_error_m,'support error') < self.error_m:
            raise ValueError('support error must enclose normal error')
        if type(self.finite_endpoints) is not bool:
            raise ValueError('explicit segment endpoint meaning required')


@dataclass(frozen=True)
class FrozenReference:
    """유한 기하와 원 생산 정책이다. UNSPECIFIED는 endpoint 의미를 소급 수락하지 않는다.

    feature_policy는 pipeline의 strip 생산/복원 경계용이며 generic register의
    독립 feature 계약이나 LaneCatalog의 common-overlap 기하를 승인하지 않는다.
    """
    revision: int
    calibration_id: str
    segments: tuple[SegmentFeature, ...]
    shared_error_m: float = 0.
    feature_policy: str = 'UNSPECIFIED'

    def __post_init__(self):
        if type(self.revision) is not int or self.revision < 0 or not self.calibration_id:
            raise ValueError('reference requires revision and calibration identity')
        if (type(self.segments) is not tuple or len(self.segments) > 64
                or any(type(s) is not SegmentFeature for s in self.segments)):
            raise ValueError('reference segment cap exceeded')
        if _number(self.shared_error_m, 'shared error') < 0:
            raise ValueError('negative inherited error')
        if (type(self.feature_policy) is not str
                or self.feature_policy not in ('UNSPECIFIED', 'PHYSICAL_STRIP_ENDPOINTS_V1', 'OBSERVED_STRIP_SUPPORT_V1')):
            raise ValueError('unknown reference feature policy')
        if self.feature_policy == 'OBSERVED_STRIP_SUPPORT_V1':
            if any(s.a == s.b for s in self.segments):
                raise ValueError('observed support requires nonzero segments')
        elif any(not s.finite_endpoints or s.support_error_m is not None for s in self.segments):
            raise ValueError('censored support requires its original feature policy')


@dataclass(frozen=True)
class PoseBox:
    x: tuple[float, float]
    y: tuple[float, float]
    yaw: tuple[float, float]

    def __post_init__(self):
        for name in ('x', 'y', 'yaw'):
            bounds = _point(getattr(self, name))
            if bounds[0] > bounds[1]:
                raise ValueError('reversed prior interval')
        # yaw를 연속된 unwrapped 구간으로 보관한다. ±pi 경계를 버리지 않는다.
        if self.yaw[1] - self.yaw[0] > 2 * math.pi:
            raise ValueError('yaw interval exceeds one full rotation')

    @property
    def center(self):
        return tuple((v[0] + v[1]) / 2 for v in (self.x, self.y, self.yaw))

    @property
    def widths(self):
        return tuple(v[1] - v[0] for v in (self.x, self.y, self.yaw))

    def split(self, steps):
        widths = self.widths
        axis = max(range(3), key=lambda i: (widths[i] / steps[i], -i))
        bounds = [self.x, self.y, self.yaw]
        lo, hi = bounds[axis]
        middle = (lo + hi) / 2
        if middle in (lo, hi):
            return None
        first, second = bounds.copy(), bounds.copy()
        first[axis], second[axis] = (lo, middle), (middle, hi)
        return PoseBox(*first), PoseBox(*second)


@dataclass(frozen=True)
class RegistrationProfile:
    cell_steps: tuple[float, float, float] = (.02, .02, .01)
    residual_m: float = .01
    max_position_error_m: float = .05
    max_yaw_error_rad: float = .05
    shared_bias_m: float = 0.
    shared_bias_rad: float = 0.
    max_cells: int = 4096
    max_distance_tests: int = 2**22
    outlier_components: int = 0
    numeric_guard_m: float = 1e-9

    def __post_init__(self):
        if type(self.cell_steps) is not tuple or len(self.cell_steps) != 3:
            raise ValueError('registration needs three split resolutions')
        for value in self.cell_steps:
            if _number(value, 'cell step') <= 0:
                raise ValueError('nonpositive cell step')
        for name in ('residual_m', 'max_position_error_m', 'max_yaw_error_rad',
                     'shared_bias_m', 'shared_bias_rad', 'numeric_guard_m'):
            if _number(getattr(self, name), name) < 0:
                raise ValueError('negative registration bound')
        for name, maximum in (('max_cells', 4096), ('max_distance_tests', 2**22)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= maximum:
                raise ValueError('registration work cap exceeded')
        if type(self.outlier_components) is not int or not 0 <= self.outlier_components <= 4:
            raise ValueError('outlier budget must count components')


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    representative: tuple[float, float, float] | None
    position_error_m: float | None
    yaw_error_rad: float | None
    feasible: tuple[PoseBox, ...]
    reference_revision: int
    tested_cells: int
    distance_tests: int
    complete: bool

    def permits(self, pose):
        """완료된 탐색에서 기존 위치가 배제되지 않았는지 확인한다."""
        return self.complete and any(
            box.x[0] <= pose[0] <= box.x[1] and box.y[0] <= pose[1] <= box.y[1]
            and box.yaw[0] <= box.yaw[0] + (pose[2] - box.yaw[0]) % (2*math.pi) <= box.yaw[1]
            for box in self.feasible)


def overlapping_points(points, reference, prior, profile, *, observed_ends=None, position_radius_m=None,
                       max_lateral_match_m=None, include_endpoints=False, include_midpoints=False):
    """공통 구간을 고른다. observed_ends는 조각 ID별 (a, b, 중심선 오차)다.

    끝점 검사와 횡방향 대응 상한에는 capture로 조각 ID를 구분한 reference를 사용한다.
    횡방향 상한은 선의 대응을 고르는 조건이다. 잔차 계산의 원 관측 오차는 보존한다.
    include_endpoints는 관측한 중심선 끝과 방향이 확인된 유한 지도 조각에만 적용한다.
    include_midpoints는 양끝을 확인했으나 끝의 순서는 모호할 수 있는 짧은 조각에 쓴다.
    """
    if max_lateral_match_m is None:
        lateral_limit = math.inf
    else:
        lateral_limit = _number(max_lateral_match_m, 'lateral match limit')
        if lateral_limit <= 0 or reference.feature_policy != 'OBSERVED_STRIP_SUPPORT_V1':
            raise ValueError('lateral match limit requires a positive bound and capture-specific segments')
    if not reference.segments:
        return ()
    center = prior.center
    x_span, y_span, yaw_span = prior.widths
    translation = math.hypot(x_span, y_span) / 2
    rotation = 2 * math.sin(yaw_span / 4)
    first_points, directions = {}, {}
    for point in points:
        first = first_points.setdefault(point.component, point)
        dx, dy = point.point[0] - first.point[0], point.point[1] - first.point[1]
        length = math.hypot(dx, dy)
        if length:
            heading = math.atan2(dy, dx) + center[2]
            error = math.asin(min(1., (point.error_m + first.error_m) / length))
            limit = math.cos(min(math.pi / 2, .5 + error + yaw_span / 2))
            directions[point.component] = (math.cos(heading), math.sin(heading), limit)
    segments = []
    for segment in reference.segments:
        dx, dy = segment.b[0] - segment.a[0], segment.b[1] - segment.a[1]
        length = math.hypot(dx, dy)
        if length:
            segments.append((segment, dx / length, dy / length, length))
    end_matches = {}
    if observed_ends:
        if reference.feature_policy != 'OBSERVED_STRIP_SUPPORT_V1':
            raise ValueError('endpoint pairing requires capture-specific reference segments')
        end_translation = translation if position_radius_m is None else min(translation, position_radius_m)
        base_pad = end_translation + reference.shared_error_m + profile.residual_m + profile.numeric_guard_m
        for component, (a, b, error_m) in observed_ends.items():
            mapped_a, mapped_b = transform_point(a, center), transform_point(b, center)
            pad_a = base_pad + error_m + rotation * math.hypot(*a)
            pad_b = base_pad + error_m + rotation * math.hypot(*b)
            matches = {}
            for segment, _, _, _ in segments:
                if not segment.finite_endpoints:
                    continue
                limit_a, limit_b = pad_a + segment.error_m, pad_b + segment.error_m
                # 조각의 추적 방향은 뒤집힐 수 있다. 양끝을 같은 지도 조각에 대응시킨다.
                forward = math.dist(mapped_a, segment.a) <= limit_a and math.dist(mapped_b, segment.b) <= limit_b
                reverse = math.dist(mapped_a, segment.b) <= limit_a and math.dist(mapped_b, segment.a) <= limit_b
                if forward or reverse:
                    matches[segment.component] = 0 if forward and not reverse else 1 if reverse and not forward else None
            end_matches[component] = matches
    observed = []
    endpoint_support = {}
    for point in points:
        x, y = transform_point(point.point, center)
        direction = directions.get(point.component)
        point_error=point.error_m if point.error_xy is None else max(point.error_m,math.hypot(*point.error_xy))
        uncertainty = (translation + rotation * math.hypot(*point.point) + point_error
                       + reference.shared_error_m + profile.residual_m + profile.numeric_guard_m)
        compatible = []
        matching_ends = None if point.normal_only else end_matches.get(point.component)
        for segment, ux, uy, length in segments:
            if not point.normal_only and not segment.finite_endpoints:
                continue
            if matching_ends is not None and segment.component not in matching_ends:
                continue
            if point.compatible and segment.component not in point.compatible:
                continue
            # 횡단 표식과 차로 테이프의 가까운 투영을 같은 관측 구간으로 세지 않는다.
            if direction is not None and abs(ux*direction[0] + uy*direction[1]) < direction[2]:
                continue
            dx, dy = x - segment.a[0], y - segment.a[1]
            pad = uncertainty + (segment.error_m if segment.support_error_m is None else segment.support_error_m)
            station = dx * ux + dy * uy
            supported = (pad <= station <= length - pad if point.normal_only
                         else -pad <= station <= length + pad)
            # 큰 오차 범위가 다른 차로의 경계를 같은 선의 대응으로 확대하지 않게 한다.
            if supported and abs(dx * uy - dy * ux) <= min(pad, lateral_limit):
                compatible.append(segment.component)
        if compatible:
            observed.append(replace(point, compatible=tuple(compatible))
                if reference.feature_policy == 'OBSERVED_STRIP_SUPPORT_V1' else point)
            if (include_endpoints or include_midpoints) and matching_ends:
                # 끝점도 같은 횡방향 상한과 원 유한 구간 대응을 통과해야 한다.
                endpoint_support.setdefault(point.component,(point.capture_id,set(),point.error_xy))[1].update(compatible)
    for component,(capture,compatible,error_xy) in endpoint_support.items():
        a,b,error=observed_ends[component]
        if include_midpoints:
            matches = tuple(segment for segment in end_matches[component] if segment in compatible)
            observed.append(PointFeature(_midpoint(a,b),error,component,capture,matches,error_xy=error_xy,
                endpoint_matches=tuple((segment,2) for segment in matches)))
        if not include_endpoints:
            continue
        matches=tuple((segment,orientation) for segment,orientation in end_matches[component].items()
                      if segment in compatible and orientation is not None)
        if matches:
            for end,location in enumerate((a,b)):
                observed.append(PointFeature(location,error,component,capture,
                    tuple(segment for segment,_ in matches),endpoint_matches=tuple(
                        (segment,orientation^end) for segment,orientation in matches)))
    return tuple(observed)


def _midpoint(a, b):
    return (a[0]+b[0])/2,(a[1]+b[1])/2


def finite_feature_point(segment, index):
    """0/1은 실제 조각 양끝, 2는 끝점 순서에 무관한 중점이다."""
    return _midpoint(segment.a,segment.b) if index == 2 else (segment.a,segment.b)[index]


def register(points, reference, prior, profile=RegistrationProfile(), *, calibration_id, position_radius_m=None):
    """연속 prior의 conservative outer set과 유한 representative를 반환한다.

    max64 sample/segment, cap 도달 시 UNKNOWN_BUDGET다. BOUNDED는 입력 prior와
    feature-error 가정 아래의 수학적 결과이며 실제 odom/scene·이동 권한은 아니다.
    position_radius_m은 prior 중심의 원 위치 오차 반경이다. 생략하면 기존 사각 prior만
    사용한다. 탐색용 사각형으로 변환하며 생긴 모서리를 원래 위치 오차로 되돌리지 않는다.
    """
    if (type(reference) is not FrozenReference or type(prior) is not PoseBox
            or type(profile) is not RegistrationProfile or calibration_id != reference.calibration_id):
        raise ValueError('registration context mismatch')
    if (type(points) is not tuple or not 1 <= len(points) <= 64
            or any(type(p) is not PointFeature for p in points)):
        raise ValueError('registration requires 1..64 point features')
    if position_radius_m is not None and _number(position_radius_m, 'position radius') < 0:
        raise ValueError('negative position radius')
    if reference.feature_policy == 'OBSERVED_STRIP_SUPPORT_V1' and any(not p.compatible for p in points):
        raise ValueError('normal constraints require original common-support associations')
    if any(p.normal_only for p in points) and reference.feature_policy != 'OBSERVED_STRIP_SUPPORT_V1':
        raise ValueError('normal constraints require an observed-support reference')
    captures = {p.capture_id for p in points}
    if any(s.capture_id in captures for s in reference.segments):
        raise ValueError('current capture cannot validate itself against the reference')
    if not reference.segments:
        return RegistrationResult('NO_REFERENCE', None, None, None, (prior,), reference.revision, 0, 0, False)
    compatible = [tuple(s for s in reference.segments if (p.normal_only or s.finite_endpoints)
                       and (not p.compatible or s.component in p.compatible)) for p in points]
    missing_components = {p.component for p, value in zip(points, compatible) if not value}
    if len(missing_components) > profile.outlier_components:
        return RegistrationResult('INCONSISTENT', None, None, None, (), reference.revision, 0, 0, True)
    if len({p.component for p in points}) <= profile.outlier_components:
        return RegistrationResult('UNOBSERVABLE', None, None, None, (prior,), reference.revision, 0, 0, True)
    normals = {}
    if any(p.normal_only for p in points):
        for segment in reference.segments:
            length=math.dist(segment.a,segment.b)
            normals[segment]=((segment.b[1]-segment.a[1])/length,(segment.a[0]-segment.b[0])/length)
    # 탐색 중 바뀌는 것은 cell 자세와 폭이다. 원 관측과 segment 조합은 고정이다.
    point_terms = []
    for point,segments in zip(points,compatible):
        endpoint_indices = dict(point.endpoint_matches)
        ends = tuple(finite_feature_point(s,endpoint_indices[s.component]) for s in segments) if endpoint_indices else ()
        point_terms.append((point,segments,ends,math.hypot(*point.point),
            0. if point.error_xy is None else math.hypot(*point.error_xy)))
    needed = sum(map(len,compatible))
    origin = prior.center
    pending, leaves = deque((prior,)), []
    tested = distance_tests = 0
    candidates = []
    exhausted = False
    while pending:
        cell = pending.popleft()
        if position_radius_m is not None:
            dx=max(cell.x[0]-origin[0],0.,origin[0]-cell.x[1])
            dy=max(cell.y[0]-origin[1],0.,origin[1]-cell.y[1])
            if math.hypot(dx,dy)>position_radius_m+profile.numeric_guard_m:
                continue
        if tested >= profile.max_cells or distance_tests + needed > profile.max_distance_tests:
            pending.appendleft(cell)
            exhausted = True
            break
        tested += 1
        center = cell.center
        widths = cell.widths
        ex, ey, etheta = (w / 2 for w in widths)
        translation = math.hypot(ex, ey)
        rotation=2*math.sin(etheta/2)
        cosine,sine=math.cos(center[2]),math.sin(center[2])
        violations, nominal_violations, costs = set(), set(), {}
        for point, segments, ends, point_radius, axis_radius in point_terms:
            if not segments:
                violations.add(point.component)
                nominal_violations.add(point.component)
                costs.setdefault(point.component, []).append(.25)
                continue
            x,y = point.point
            location = center[0]+cosine*x-sine*y,center[1]+sine*x+cosine*y
            point_rotation=point_radius*rotation
            if point.normal_only:
                distances, errors, motions = [], [], []
                for s in segments:
                    nx,ny=normals[s]
                    distances.append(abs((location[0]-s.a[0])*nx+(location[1]-s.a[1])*ny))
                    bx,by=cosine*nx+sine*ny,-sine*nx+cosine*ny
                    axis_error=(point.error_m if point.error_xy is None else
                        abs(bx)*point.error_xy[0]+abs(by)*point.error_xy[1]
                        +axis_radius*rotation)
                    errors.append(axis_error)
                    motions.append(abs(nx)*ex+abs(ny)*ey+point_rotation)
            else:
                distances = ([math.dist(location,end) for end in ends] if ends
                             else [segment_distance(location, s.a, s.b) for s in segments])
                errors=[point.error_m]*len(segments)
                motions=[translation+point_rotation]*len(segments)
            distance_tests += len(segments)
            # 연속 yaw 구간의 이동 상한을 사용한다.
            lower = min(max(0., d - motion - error - s.error_m
                            - reference.shared_error_m - profile.numeric_guard_m)
                        for d, s, error, motion in zip(distances, segments, errors, motions))
            nominal = min(max(0., d - error - s.error_m - reference.shared_error_m)
                          for d, s, error in zip(distances, segments, errors))
            if lower > profile.residual_m:
                violations.add(point.component)
            if nominal > profile.residual_m + profile.numeric_guard_m:
                nominal_violations.add(point.component)
            costs.setdefault(point.component, []).append(min(min(distances)**2, .25))
        if len(violations) > profile.outlier_components:
            continue
        if len(nominal_violations) <= profile.outlier_components:
            # component별 가중치를 제한해 긴 선이 loss를 독점하지 않게 한다.
            rank = sum(sum(v) / len(v) for v in costs.values())
            candidates.append((rank, center))
            if len(candidates) > profile.max_cells:
                raise AssertionError('representative pool exceeded the tested-cell cap')
        if all(w <= step for w, step in zip(widths, profile.cell_steps)):
            leaves.append(cell)
        else:
            split = cell.split(profile.cell_steps)
            if split is None:
                leaves.append(cell)
            else:
                pending.extend(split)
    feasible = tuple(leaves) + tuple(pending)
    if not feasible:
        return RegistrationResult('INCONSISTENT', None, None, None, (), reference.revision,
                                  tested, distance_tests, True)
    # 최종 F에 포함된 후보만 쓴다. 배제된 조상 cell의 후보를 자동 채택하지 않는다.
    def inside(pose):
        return ((position_radius_m is None or math.dist(pose[:2],origin[:2])<=position_radius_m)
            and any(all(lo <= v <= hi for v, (lo, hi) in zip(pose, (b.x, b.y, b.yaw))) for b in feasible))
    # 관측 오차 안에서 이미 성립하는 자세를 잔차 최솟값을 위해 밀어내지 않는다.
    # 모든 후보는 위에서 잔차를 통과했다. 허용 집합 전체로 위치 오차를 계산한다.
    candidates.sort(key=lambda c: (sum((a-b)**2 for a,b in zip(c[1],origin)), c[0], c[1]))
    representative = next((c for c in candidates if inside(c[1])), None)
    if representative is None:
        return RegistrationResult('UNKNOWN_BUDGET' if exhausted else 'UNRESOLVED', None, None, None,
                                  feasible, reference.revision, tested, distance_tests, not exhausted)
    pose = representative[1]
    position_error = max(math.hypot(max(abs(b.x[0]-pose[0]), abs(b.x[1]-pose[0])),
                                   max(abs(b.y[0]-pose[1]), abs(b.y[1]-pose[1]))) for b in feasible)
    if position_radius_m is not None:
        position_error=min(position_error,position_radius_m+math.dist(pose[:2],origin[:2]))
    yaw_error = max(max(abs(b.yaw[0]-pose[2]), abs(b.yaw[1]-pose[2])) for b in feasible)
    position_error += reference.shared_error_m + profile.shared_bias_m + profile.numeric_guard_m
    yaw_error += profile.shared_bias_rad
    status = ('UNKNOWN_BUDGET' if exhausted else
              'BOUNDED' if position_error <= profile.max_position_error_m and yaw_error <= profile.max_yaw_error_rad
              else 'AMBIGUOUS_OR_UNOBSERVABLE')
    return RegistrationResult(status, pose, position_error, yaw_error, feasible,
                              reference.revision, tested, distance_tests, not exhausted)
