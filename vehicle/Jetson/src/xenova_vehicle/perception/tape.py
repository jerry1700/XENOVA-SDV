"""Birdview에서 흰 테이프와 도로 바깥 경계의 프레임별 관측을 만든다.

색/폭/작업 cap은 조정할 실험 profile이다. NN mask는 보조 후보로만 보유하며,
calibration·통행권·FREE·stable ID를 만들지 않는다. NumPy/OpenCV는 호출 시 불러온다.
"""

from dataclasses import asdict, dataclass, field
from functools import lru_cache
import math
import struct
from threading import local
from typing import Any

from .inputs import FrameStamp
from .geometry import project_ground_bounds_many, timing_start, timing_finish


ENDPOINT_KINDS = frozenset(('physical_junction', 'observed_paint_end', 'FOV_clip', 'occluded', 'predicted'))
_EDGE_VALUE_MIN = 150
_SURFACE_STEP_VALUE = 8
_MAX_TAPE_GAP_M = .01 * math.sqrt(2)


@dataclass(frozen=True)
class TapeProfile:
    """미실측 초기 값. 좌표는 rear-axle base의 x전방/y왼쪽이며 길이는 m다."""

    x_min_m: float = 0.15
    x_max_m: float = 2.0
    y_min_m: float = -1.2
    y_max_m: float = 1.2
    cell_m: float = 0.005
    saturation_max: int = 85
    value_min: int = 185
    contrast_min: int = 25
    tape_width_min_m: float = 0.010
    tape_width_max_m: float = 0.045
    # 4~5cm 점선은 중심선을 가늘게 만들 때 끝이 짧아질 수 있다.
    min_strip_length_m: float = 0.02
    simplify_error_m: float = 0.004
    extraction_error_m: float = 0.005
    max_projection_error_m: float = 0.10
    max_thin_iterations: int = 64
    max_components: int = 512
    max_component_work: int = 1_048_576
    max_graph_pixels: int = 20_000
    edge_work_cap: int = 500_000
    max_strips: int = 32
    max_points_per_strip: int = 64

    def __post_init__(self):
        for name in ('x_min_m', 'x_max_m', 'y_min_m', 'y_max_m', 'cell_m', 'tape_width_min_m',
                     'tape_width_max_m', 'min_strip_length_m', 'simplify_error_m', 'extraction_error_m',
                     'max_projection_error_m'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('tape profile requires finite scalars')
        if (self.x_min_m >= self.x_max_m or self.y_min_m >= self.y_max_m
                or not .001 <= self.cell_m <= .05 or not 0 < self.tape_width_min_m <= self.tape_width_max_m <= .20
                or min(self.min_strip_length_m, self.max_projection_error_m) <= 0
                or min(self.simplify_error_m, self.extraction_error_m) < 0):
            raise ValueError('invalid tape metric profile')
        for name, cap in (('saturation_max', 255), ('value_min', 255), ('contrast_min', 255),
                          ('max_thin_iterations', 256), ('max_components', 2048),
                          ('max_component_work', 4_194_304), ('max_graph_pixels', 40_000),
                          ('edge_work_cap', 2_000_000),
                          ('max_strips', 32), ('max_points_per_strip', 64)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError('invalid tape work bound: ' + name)
        if self.rows * self.columns > 262_144 or max(self.rows, self.columns) > 1024:
            raise ValueError('birdview exceeds bounded raster')

    @property
    def rows(self):
        return int(math.floor((self.x_max_m - self.x_min_m) / self.cell_m)) + 1

    @property
    def columns(self):
        return int(math.floor((self.y_max_m - self.y_min_m) / self.cell_m)) + 1

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TapeStrip:
    """width_complete=False의 폭 구간은 읽힌 cross-section에만 적용한다. 가림 밖을 채우지 않는다."""

    local_id: int
    points_xy: tuple
    width_interval_m: tuple[float, float]
    error_m: float
    start_kind: str
    end_kind: str
    observed: bool = True
    closed: bool = False
    endpoint_error_m: tuple = (None, None)
    point_error_xy: tuple = ()
    segment_error_xy: tuple = ()
    width_complete: bool = True
    censored_cross_sections: int = 0
    outer_role: int | None = None

    def __post_init__(self):
        if (type(self.local_id) is not int or self.local_id < 0 or type(self.points_xy) is not tuple
                or not 2 <= len(self.points_xy) <= 64 or self.start_kind not in ENDPOINT_KINDS
                or self.end_kind not in ENDPOINT_KINDS):
            raise ValueError('invalid frame-local tape strip')
        if any(type(p) is not tuple or len(p) != 2 or any(type(v) not in (int, float) or not math.isfinite(v)
                                                       for v in p) for p in self.points_xy):
            raise ValueError('invalid tape point')
        if (type(self.width_interval_m) is not tuple or len(self.width_interval_m) != 2
                or not 0 <= self.width_interval_m[0] <= self.width_interval_m[1]
                or not math.isfinite(self.error_m) or self.error_m < 0
                or type(self.observed) is not bool or type(self.closed) is not bool
                or type(self.width_complete) is not bool or type(self.censored_cross_sections) is not int
                or self.censored_cross_sections < 0
                or self.outer_role is not None and (type(self.outer_role) is not int or self.outer_role not in (0, 2))):
            raise ValueError('invalid tape width/error/observation')
        if (type(self.endpoint_error_m) is not tuple or len(self.endpoint_error_m) != 2
                or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0)
                       for v in self.endpoint_error_m)):
            raise ValueError('invalid physical endpoint error')
        for values, size in ((self.point_error_xy, len(self.points_xy)),
                             (self.segment_error_xy, len(self.points_xy) - 1)):
            if (type(values) is not tuple or values and len(values) != size
                    or any(type(p) is not tuple or len(p) != 2 or any(type(v) not in (int, float)
                        or not math.isfinite(v) or v < 0 for v in p) for p in values)):
                raise ValueError('invalid axis-wise tape geometry error')

    @property
    def length_m(self):
        return sum(math.dist(a, b) for a, b in zip(self.points_xy, self.points_xy[1:]))


@dataclass(frozen=True)
class TapeJunction:
    local_id: int
    xy: tuple[float, float]
    degree: int
    kind: str


@dataclass(frozen=True)
class TapeGraph:
    strips: tuple[TapeStrip, ...]
    junctions: tuple[TapeJunction, ...] = ()
    status: str = 'VALID'
    complete: bool = True
    reasons: tuple[str, ...] = ()
    source_key: tuple | None = None
    calibration_qualified: bool = False
    work: dict = field(default_factory=dict)
    profile: TapeProfile = TapeProfile()
    mask: Any = field(default=None, repr=False, compare=False)
    skeleton: Any = field(default=None, repr=False, compare=False)
    valid_mask: Any = field(default=None, repr=False, compare=False)
    nn_candidates: Any = field(default=None, repr=False, compare=False)
    surface_edges: tuple[TapeStrip, ...] = ()
    paint_roles: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        if (type(self.strips) is not tuple or len(self.strips) > 32
                or any(type(s) is not TapeStrip for s in self.strips)
                or type(self.junctions) is not tuple or len(self.junctions) > 64
                or len({s.local_id for s in self.strips}) != len(self.strips)
                or self.status not in ('VALID', 'EMPTY', 'UNKNOWN') or type(self.complete) is not bool
                or type(self.calibration_qualified) is not bool):
            raise ValueError('invalid bounded tape graph')
        if (type(self.surface_edges) is not tuple or len(self.surface_edges) > 8
                or len(self.strips + self.surface_edges) > 32
                or any(type(s) is not TapeStrip or s.outer_role is None for s in self.surface_edges)
                or len({s.local_id for s in self.strips + self.surface_edges}) != len(self.strips + self.surface_edges)):
            raise ValueError('invalid bounded road surface edges')
        paint_ids = {strip.local_id for strip in self.strips}
        if (type(self.paint_roles) is not tuple or len(self.paint_roles) > len(paint_ids)
                or any(type(row) is not tuple or len(row) != 2 or type(row[0]) is not int
                       or row[0] not in paint_ids or type(row[1]) is not int or row[1] not in (0, 1, 2)
                       for row in self.paint_roles)
                or len({row[0] for row in self.paint_roles}) != len(self.paint_roles)):
            raise ValueError('paint roles must reference unique observed strips')

    def descriptor(self):
        return dict(status=self.status, complete=self.complete, reasons=list(self.reasons),
            source_key=self.source_key, calibration_qualified=self.calibration_qualified,
            strips=[asdict(s) for s in self.strips], junctions=[asdict(j) for j in self.junctions], work=dict(self.work))


def _thin_tables():
    tables=[]
    for step in (0,1):
        flags=[]
        for code in range(256):
            p=tuple(bool(code & (1<<i)) for i in range(8))
            transitions=sum(not p[i] and p[(i+1)%8] for i in range(8))
            corner=(not (p[0] and p[2] and p[4]) and not (p[2] and p[4] and p[6]) if step==0 else
                    not (p[0] and p[2] and p[6]) and not (p[0] and p[4] and p[6]))
            flags.append(2<=sum(p)<=6 and transitions==1 and corner)
        tables.append(bytes(flags))
    return tuple(tables)


# 원 N,NE,E,SE,S,SW,W,NW 순서. 캐시는 LUT512B+kernel36B이며 frame 배열은 보유하지 않는다.
_THIN_REMOVE=_thin_tables()
_THIN_KERNEL=struct.pack('<9f',128,1,2,64,0,4,32,16,8)


def _thin(np, mask, limit):
    """원 bool mask의 Zhang-Suen predicate/반복 한도를 정수 이웃 코드로 평가한다."""
    import cv2
    result = mask.copy()
    if limit <= 0:
        return result, False, limit
    x, y, width, height = cv2.boundingRect(result.view(np.uint8)) if result.size else (0, 0, 0, 0)
    if not width or not height:
        return result, True, 1
    # The input outside this rectangle is already zero, and thinning only deletes pixels.
    image = result[y:y+height, x:x+width]
    binary = image.view(np.uint8)
    kernel=np.frombuffer(_THIN_KERNEL,dtype='<f4').reshape(3,3)
    lookup=tuple(np.frombuffer(table,dtype=np.bool_) for table in _THIN_REMOVE)
    for iteration in range(limit):
        changed = False
        for step in (0, 1):
            # 0/1 입력의 이웃 코드가 0..255라 8비트 출력으로도 합을 그대로 보존한다.
            codes=cv2.filter2D(binary,cv2.CV_8U,kernel,borderType=cv2.BORDER_CONSTANT)
            remove = image & lookup[step][codes]
            if np.any(remove):
                image[remove] = False
                changed = True
        if not changed:
            return result, True, iteration + 1
    return result, False, limit


def _readonly(np, value):
    return np.frombuffer(value.tobytes(), dtype=value.dtype).reshape(value.shape)


@lru_cache(maxsize=2)
def _birdview_map(camera, plane, profile):
    """불변 보정/profile의 투영만 보유한다. 영상·가림·분류 결과는 캐시하지 않는다."""
    import numpy as np
    from .geometry import ground_to_pixels

    x = profile.x_min_m + np.arange(profile.rows, dtype=np.float64)[:, None] * profile.cell_m
    y = profile.y_min_m + np.arange(profile.columns, dtype=np.float64)[None, :] * profile.cell_m
    u, v, valid = ground_to_pixels(x, y, camera, plane)
    x0, y0, x1, y1 = camera.roi
    margin = camera.pixel_error + 1.0
    valid = valid & (u - margin >= x0) & (u + margin < x1) & (v - margin >= y0) & (v + margin < y1)
    return _readonly(np, u), _readonly(np, v), _readonly(np, valid)


class _TapeProjectionCache:
    """한 호출 스레드에서 최근 보정·격자 하나의 고정 투영만 재사용한다."""

    def __init__(self, key):
        self.key = key
        self.valid_source = self.valid_mask = None
        self.offsets = self.status = None

    def visible_grid(self, np, cv2, u, v, inside, valid_rgb):
        if valid_rgb is None:
            return inside.copy()
        if valid_rgb is self.valid_source:
            return self.valid_mask.copy()
        valid = inside & (cv2.remap(valid_rgb, u, v, cv2.INTER_NEAREST,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=0) != 0)
        # 임의 배열은 다음 프레임에서 제자리 수정될 수 있다. bytes 저장소만 키로 보유한다.
        owner = valid_rgb
        while isinstance(owner, np.ndarray):
            owner = owner.base
        if isinstance(owner, bytes):
            self.valid_source, self.valid_mask = valid_rgb, valid
            return valid.copy()
        return valid

    def point_offsets(self, np, u, v, rows, columns, diagnostics):
        camera, plane, profile = self.key
        if self.offsets is None:
            self.offsets = np.empty(u.shape + (2,), dtype=np.float64)
            self.status = np.zeros(u.shape, dtype=np.uint8)
        missing = self.status[rows, columns] == 0
        if np.any(missing):
            rr, cc = rows[missing], columns[missing]
            lower, upper, bounded = project_ground_bounds_many(u[rr, cc], v[rr, cc],
                camera, plane, pixel_error=camera.pixel_error + 1.0, diagnostics=diagnostics)
            nominal = np.column_stack((profile.x_min_m + rr * profile.cell_m,
                                       profile.y_min_m + cc * profile.cell_m))
            self.offsets[rr, cc] = np.maximum(np.abs(lower[:, :2] - nominal),
                                               np.abs(upper[:, :2] - nominal))
            self.status[rr, cc] = 1 + bounded.astype(np.uint8)
        return self.offsets[rows, columns], self.status[rows, columns] == 2


# 최대 262,144셀 × (float64 2개 + 상태 1B + 유효 mask 1B) = 4.5MiB / 호출 스레드.
_TAPE_PROJECTION = local()


def _tape_projection(camera, plane, profile):
    key = camera, plane, profile
    cache = getattr(_TAPE_PROJECTION, 'cache', None)
    if cache is None or cache.key != key:
        cache = _TAPE_PROJECTION.cache = _TapeProjectionCache(key)
    return cache


def _trace(np, cv2, skeleton, valid, occluded, band, projection_error, projection_xy, profile, work):
    pixels = set(map(tuple,np.argwhere(skeleton).tolist()))
    adjacency = {p: tuple((p[0] + dy, p[1] + dx) for dy, dx in
        ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
        if (p[0] + dy, p[1] + dx) in pixels) for p in pixels}
    special = np.zeros(skeleton.shape, dtype=np.uint8)
    for p, neighbors in adjacency.items():
        if len(neighbors) != 2:
            special[p] = 1
    count, labels = cv2.connectedComponents(special, connectivity=8)
    groups = {}
    for p in pixels:
        if labels[p]:
            groups.setdefault(int(labels[p]), []).append(p)
    # 독립 cycle도 유한 graph로 남기되 도로의 열린 strip으로 바꾸지 않는다.
    _, components = cv2.connectedComponents(skeleton.astype(np.uint8), connectivity=8)
    first, represented = {}, set()
    for p in sorted(pixels):
        component = int(components[p])
        first.setdefault(component, p)
        if labels[p]:
            represented.add(component)
    cycle_nodes = set()
    for component, p in sorted(first.items()):
        if component not in represented:
            groups[count] = [p]
            labels[p] = count
            cycle_nodes.add(count)
            count += 1
    visited = set()
    paths = []
    trace_complete = True
    steps = 0
    for node, group in sorted(groups.items()):
        for first in sorted(group):
            for second in adjacency[first]:
                edge = tuple(sorted((first, second)))
                if labels[second] == node or edge in visited:
                    continue
                path = [first]
                previous, current = first, second
                for _ in range(profile.max_graph_pixels + 1):
                    steps += 1
                    if steps > profile.max_graph_pixels * 4 or len(paths) >= profile.max_strips * 8:
                        trace_complete = False
                        break
                    visited.add(tuple(sorted((previous, current))))
                    path.append(current)
                    if labels[current] != 0:
                        paths.append((path, node, int(labels[current])))
                        break
                    choices = [p for p in adjacency[current] if p != previous]
                    if len(choices) != 1:
                        break
                    previous, current = current, choices[0]
                    if current == first:
                        visited.add(tuple(sorted((previous, current))))
                        path.append(first)
                        paths.append((path, node, node))
                        break
                if not trace_complete:
                    break
            if not trace_complete:
                break
        if not trace_complete:
            break
    radius = int(math.ceil(profile.tape_width_max_m / profile.cell_m)) + 2
    kernel = np.ones((2 * radius + 1, 2 * radius + 1), np.uint8)
    near_occ = cv2.dilate(occluded.astype(np.uint8), kernel) != 0
    invalid = (~valid).astype(np.uint8)
    invalid[[0, -1], :] = 1
    invalid[:, [0, -1]] = 1
    near_invalid = cv2.dilate(invalid, kernel) != 0
    degrees = dict.fromkeys(groups, 0)
    for _, start, end in paths:
        degrees[start] += 1
        degrees[end] += 1

    def kind(node):
        group = groups[node]
        if node in cycle_nodes:
            return 'predicted'
        if any(near_occ[p] for p in group):
            return 'occluded'
        if any(near_invalid[p] or p[0] in (0, valid.shape[0] - 1) or p[1] in (0, valid.shape[1] - 1) for p in group):
            return 'FOV_clip'
        return 'physical_junction' if degrees[node] >= 3 else 'observed_paint_end'

    strips = []
    complete = trace_complete
    kinds = {node: kind(node) for node in groups}
    work.update(trace_steps=steps, strip_paths=len(paths), short_paths=0, edge_pair_missing=0,
                edge_pair_censored=0, edge_pair_internal=0, no_width_paths=0,
                width_rejected=0, point_cap_rejected=0, edge_budget_exhausted=0,
                trace_cap_exhausted=int(not trace_complete), strip_cap_exhausted=0, junction_cap_exhausted=0)
    for path, start, end in paths:
        points = [(profile.x_min_m + p[0] * profile.cell_m, profile.y_min_m + p[1] * profile.cell_m) for p in path]
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        if length + 1e-9 < profile.min_strip_length_m:
            work['short_paths'] += 1
            continue
        short_dash = length + 1e-9 < .08
        if short_dash and (start == end or kinds[start] != 'observed_paint_end'
                           or kinds[end] != 'observed_paint_end'):
            continue
        widths = []
        center_pad = 0.
        width_projection_pad = 0.
        edge_budget = False
        censored_sections = 0
        internal_sections = 0
        arc = [0.]
        for a, b in zip(points, points[1:]):
            arc.append(arc[-1] + math.dist(a, b))
        tangent_step = 1 if len(path) < 5 else 2
        for index in range(tangent_step, len(path) - tangent_step):
            if ((kinds[start] == 'physical_junction' and arc[index] < 2 * profile.tape_width_max_m)
                    or (kinds[end] == 'physical_junction' and length - arc[index] < 2 * profile.tape_width_max_m)):
                continue
            first, last = path[index - tangent_step], path[index + tangent_step]
            dy, dx = last[0] - first[0], last[1] - first[1]
            norm = math.hypot(dx, dy)
            if norm <= 1e-9:
                continue
            normal = -dx / norm, dy / norm
            edges = []
            causes = []
            limit = int(math.ceil(2 * profile.tape_width_max_m / profile.cell_m)) + 2
            for sign in (-1, 1):
                for offset in range(1, limit + 1):
                    if work['edge_tests'] >= profile.edge_work_cap:
                        edge_budget = True
                        break
                    work['edge_tests'] += 1
                    row = int(round(path[index][0] + sign * offset * normal[0]))
                    column = int(round(path[index][1] + sign * offset * normal[1]))
                    if not (0 <= row < band.shape[0] and 0 <= column < band.shape[1]) or not valid[row, column]:
                        causes.append('censored')
                        break
                    if occluded[row, column]:
                        causes.append('censored')
                        break
                    if not band[row, column]:
                        edges.append(offset - .5)
                        causes.append('measured')
                        break
                else:
                    causes.append('internal')
                if edge_budget:
                    break
            if edge_budget:
                break
            if len(edges) == 2:
                widths.append(sum(edges) * profile.cell_m)
                center_pad = max(center_pad, abs(edges[0] - edges[1]) * profile.cell_m / 2)
                ex, ey = projection_xy[path[index]]
                width_projection_pad = max(width_projection_pad, float(abs(normal[0]) * ex + abs(normal[1]) * ey))
            else:
                work['edge_pair_missing'] += 1
                # ROI/가림 바깥은 폭의 반증이 아니다. 읽지 못한 cross-section을
                # 측정값에 넣지 않고 표시한다. 내부에서 끝까지 edge를 못 찾은 경우만
                # 미해결 topology로 남겨 다른 정상 strip까지 VALID로 승격하지 않는다.
                if 'internal' in causes:
                    work['edge_pair_internal'] += 1
                    internal_sections += 1
                    if not short_dash:
                        complete = False
                else:
                    work['edge_pair_censored'] += 1
                    censored_sections += 1
        if edge_budget:
            work['edge_budget_exhausted'] = 1
            complete = False
            break
        if short_dash and (not widths or internal_sections or censored_sections):
            continue
        if not widths:
            work['no_width_paths'] += 1
            complete = False
            continue
        width = float(np.median(widths))
        pad = 2 * profile.cell_m
        if width + pad < profile.tape_width_min_m or width - pad > profile.tape_width_max_m:
            work['width_rejected'] += 1
            continue
        simplified = cv2.approxPolyDP(np.asarray(points, dtype=np.float32).reshape(-1, 1, 2),
                                     profile.simplify_error_m, start == end).reshape(-1, 2)
        if len(simplified) < 2:
            continue
        if len(simplified) > profile.max_points_per_strip:
            work['point_cap_rejected'] += 1
            complete = False
            continue
        if len(strips) == profile.max_strips:
            work['strip_cap_exhausted'] = 1
            complete = False
            break
        projection_pad = max(float(projection_error[p]) for p in path)
        local_pad = center_pad + profile.extraction_error_m + profile.cell_m / math.sqrt(2)
        point_error = (projection_pad + center_pad + profile.extraction_error_m + profile.simplify_error_m
                       + profile.cell_m / math.sqrt(2))
        physical_tip_error = point_error + width / 2 + pad
        endpoint_error = tuple(physical_tip_error if kinds[node] in ('physical_junction', 'observed_paint_end')
                               else None for node in (start, end))
        original = np.asarray(points, dtype=np.float64)
        vertex_indices = [int(np.argmin(np.sum((original - vertex) ** 2, axis=1))) for vertex in simplified]
        point_errors = tuple(tuple(float(projection_xy[path[index]][axis] + local_pad
            + abs(original[index, axis] - vertex[axis])) for axis in (0, 1))
            for index, vertex in zip(vertex_indices, simplified))
        segment_errors = []
        for i, (a, b) in enumerate(zip(simplified, simplified[1:])):
            lo, hi = sorted(vertex_indices[i:i + 2])
            source = original[lo:hi + 1]
            delta = b.astype(np.float64) - a
            norm2 = float(delta @ delta)
            fraction = np.clip(((source - a) @ delta) / max(norm2, 1e-20), 0., 1.)
            displacement = np.max(np.abs(source - (a + fraction[:, None] * delta)), axis=0)
            source_error = np.asarray([projection_xy[p] for p in path[lo:hi + 1]])
            segment_errors.append(tuple(float(v) for v in np.max(source_error, axis=0) + local_pad + displacement))
        strips.append(TapeStrip(len(strips), tuple(tuple(float(v) for v in p) for p in simplified),
            (max(0., width - pad - 2 * width_projection_pad), width + pad + 2 * width_projection_pad),
            point_error, kinds[start], kinds[end], True, start == end, endpoint_error, point_errors, tuple(segment_errors),
            not (censored_sections or internal_sections), censored_sections))
    all_junctions = [(node, group) for node, group in sorted(groups.items()) if degrees[node] >= 3]
    if len(all_junctions) > 64:
        work['junction_cap_exhausted'] = 1
        complete = False
    junctions = tuple(TapeJunction(node, (profile.x_min_m + sum(p[0] for p in group) / len(group) * profile.cell_m,
        profile.y_min_m + sum(p[1] for p in group) / len(group) * profile.cell_m), degrees[node], kinds[node])
        for node, group in all_junctions[:64])
    return strips, junctions, complete


def _white_band(np, cv2, hsv, visible, profile):
    """Recover dim tape edges next to bright paint when the local band is narrow."""
    values = hsv[..., 2]
    eligible = visible & (hsv[..., 1] <= profile.saturation_max)
    strong = eligible & (values >= profile.value_min)
    weak = eligible & (values >= _EDGE_VALUE_MIN) & ~strong
    if profile.value_min <= _EDGE_VALUE_MIN or not strong.any() or not weak.any():
        return strong
    # Only an immediately adjacent pixel can extend the bright core.
    connected = cv2.dilate(strong.astype(np.uint8), np.ones((3, 3), np.uint8)) != 0
    weak &= connected
    if not weak.any():
        return strong

    # Opening across the strip estimates the brightness around a narrow band.
    # Unobserved pixels must never supply a dark background at a clipped edge.
    luminance = np.where(visible, values, 255).astype(np.uint8)
    background = luminance.copy()
    half_width_cells = profile.tape_width_max_m / (2 * profile.cell_m)
    horizontal = np.ones((1, 2 * int(half_width_cells) + 3), dtype=np.uint8)
    diagonal = np.eye(2 * int(half_width_cells / math.sqrt(2)) + 3, dtype=np.uint8)
    for kernel in (horizontal, horizontal.T, diagonal, np.fliplr(diagonal).copy()):
        opened = cv2.morphologyEx(luminance, cv2.MORPH_OPEN, kernel)
        np.minimum(background, opened, out=background)
    return strong | (weak & (cv2.subtract(values, background) >= profile.contrast_min))


def _close_tape_gaps(white, eligible, cell_m):
    """Fill bracketed runs up to one 1cm-grid diagonal, inside visible ground."""
    axial = int((_MAX_TAPE_GAP_M + 1e-12) / cell_m)
    if not axial:
        return white
    diagonal = int((_MAX_TAPE_GAP_M + 1e-12) / (math.sqrt(2) * cell_m))
    restored = white.copy()
    directions = ((white, eligible, restored, 0, axial), (white.T, eligible.T, restored.T, 0, axial),
                  (white, eligible, restored, 1, diagonal),
                  (white[:, ::-1], eligible[:, ::-1], restored[:, ::-1], 1, diagonal))
    for seed, visible, output, column_step, max_gap in directions:
        for gap in range(1, max_gap+1):
            span = gap+1
            height = seed.shape[0]-span
            width = seed.shape[1]-span*column_step
            if height <= 0 or width <= 0:
                break
            candidates = seed[:height, :width] & seed[span:, span*column_step:]
            interior = [(slice(step, step+height), slice(step*column_step, step*column_step+width))
                        for step in range(1, span)]
            for cells in interior:
                candidates &= visible[cells] & ~seed[cells]
            for cells in interior:
                output[cells] |= candidates
    return restored


def _outer_paint(np, cv2, hsv, visible, profile):
    """Find paint that is brighter than the board beside an observed floor shoulder."""
    near = max(1, round(.010 / profile.cell_m))
    far = max(2 * near + 1, round(.035 / profile.cell_m))
    span = hsv.shape[1] - 2 * far
    if span <= 0:
        return np.zeros((2, *visible.shape), dtype=np.bool_)
    saturation, value = (hsv[..., i] for i in (1, 2))
    window = np.ones((1, 2 * far + 1), dtype=np.uint8)
    seen = cv2.erode(visible.view(np.uint8), window)[:, far:far + span] != 0

    def sample(channel, offset):
        start = far + offset
        return channel[:, start:start + span]

    paint_v, paint_s = sample(value, 0), sample(saturation, 0)
    masks = np.zeros((2, *visible.shape), dtype=np.bool_)
    for index, side in enumerate((1, -1)):
        inside_v = cv2.addWeighted(sample(value, -side * near), .5,
                                  sample(value, -side * 2 * near), .5, 0)
        board_s, floor_s = sample(saturation, -side * far), sample(saturation, side * far)
        board_v, floor_v = sample(value, -side * far), sample(value, side * far)
        # 방향은 밝은 바닥 쪽이다. 재질의 채도가 다르면 약한 밝기 차이도 쓸 수 있다.
        floor_lift = cv2.subtract(floor_v, board_v)
        floor_side = ((floor_lift >= profile.contrast_min)
                      | ((floor_lift >= _SURFACE_STEP_VALUE)
                         & (cv2.absdiff(floor_s, board_s) >= profile.contrast_min)))
        # 도색과 바닥 사이에 관측된 경계가 있어야 밝은 바닥 자체를 테이프로 읽지 않는다.
        floor_edge = ((cv2.absdiff(floor_s, paint_s) >= _SURFACE_STEP_VALUE)
                      | (cv2.absdiff(paint_v, floor_v) >= _SURFACE_STEP_VALUE))
        paint_color = ((paint_s <= profile.saturation_max)
                       | (cv2.subtract(paint_s, board_s) <= profile.contrast_min))
        masks[index, :, far:far + span] = (seen & floor_side & floor_edge & paint_color
            & (paint_v >= profile.value_min) & (cv2.subtract(paint_v, inside_v) >= profile.contrast_min))
        masks[index] = _close_tape_gaps(masks[index], visible, profile.cell_m)
    return masks


def _paint_roles(np, strips, values, visible, profile, outer_paint):
    """Inspect the ground on both sides of observed paint, away from the tape itself."""
    origin = np.array((profile.x_min_m, profile.y_min_m))
    offsets = max(.026, profile.tape_width_max_m / 2 + profile.cell_m) + np.array((0., .018))
    roles = []
    for strip in strips:
        if strip.closed or not strip.observed or not strip.width_complete:
            continue
        points = np.asarray(strip.points_xy)
        if points[0, 0] > points[-1, 0]:
            points = points[::-1]
        edges = np.diff(points, axis=0)
        lengths = np.hypot(*edges.T)
        stations = np.cumsum(lengths)
        length = stations[-1]
        count = min(16, int(length / .015))
        if count < 2:
            continue
        along = (np.arange(count) + .5) * (length / count)
        segments = np.searchsorted(stations, along, side='right')
        tangent = edges[segments] / lengths[segments, None]
        middle = points[segments + 1] + tangent * (along - stations[segments])[:, None]
        normal = np.column_stack((-tangent[:, 1], tangent[:, 0]))
        shoulders = middle[:, None, None, :] + (normal[:, None, None, :]
                    * np.stack((offsets, -offsets))[None, :, :, None])
        pixels = np.rint((shoulders - origin) / profile.cell_m).astype(int)
        inside = ((pixels >= 0) & (pixels < visible.shape)).all(axis=(1, 2, 3)) & (tangent[:, 0] > .3)
        rows, columns = pixels[inside].transpose(3, 0, 1, 2)
        seen = visible[rows, columns].all(axis=(1, 2))
        shoulders_v = values[rows[seen], columns[seen]].astype(np.int16)
        if len(shoulders_v) < 2:
            continue
        dark = (shoulders_v < _EDGE_VALUE_MIN).all(axis=2)
        difference = shoulders_v.min(axis=2) - shoulders_v[:, ::-1, :].max(axis=2)
        left = (difference[:, 0] >= profile.contrast_min) & dark[:, 1] & ~dark[:, 0]
        right = (difference[:, 1] >= profile.contrast_min) & dark[:, 0] & ~dark[:, 1]
        center = dark.all(axis=1) & (np.abs(difference).max(axis=1) < profile.contrast_min)
        row, column = np.rint((middle[inside][seen] - origin) / profile.cell_m).astype(int).T
        confirmed_left, confirmed_right = outer_paint[:, row, column]
        outer_votes = np.count_nonzero((confirmed_left & ~confirmed_right,
                                        confirmed_right & ~confirmed_left), axis=1)
        outer = np.flatnonzero(outer_votes * 4 >= len(shoulders_v) * 3)
        if len(outer) == 1:
            roles.append((strip.local_id, 2 * int(outer[0])))
            continue
        votes = np.count_nonzero((left, center, right), axis=1)
        accepted = np.flatnonzero(votes * 4 >= len(shoulders_v) * 3)
        if len(accepted) == 1 and np.count_nonzero(votes) == 1:
            roles.append((strip.local_id, int(accepted[0])))
    return tuple(roles)


def _surface_edges(np, cv2, hsv, visible, white, u, v, cache, profile, first_id, diagnostics):
    """Trace the outward dark-to-light transition on both sides of the board."""
    step = max(1, round(.025 / profile.cell_m))
    shoulder = max(2, round(.035 / profile.cell_m))
    if first_id == 32 or hsv.shape[1] <= 2 * shoulder:
        return ()
    columns = np.arange(shoulder,hsv.shape[1]-shoulder)
    width = len(columns)
    snap = max(1,round(.025/profile.cell_m))
    max_step = max(2,round(.045/profile.cell_m))
    candidates = []
    rows = np.arange(0, len(hsv), step)
    before, after = np.maximum(rows - 1, 0), np.minimum(rows + 1, len(hsv) - 1)
    channel = hsv[..., 2]
    first, middle, last = channel[before], channel[rows], channel[after]
    values = np.maximum(np.minimum(first, middle), np.minimum(np.maximum(first, middle), last)).astype(np.float64)
    border = (before == rows) | (after == rows)
    values[border] = (first[border].astype(np.float64) + last[border]) * .5
    observed = cv2.erode(visible.astype(np.uint8), np.ones((3, 2 * shoulder + 1), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=1)
    valid_rows = observed[rows, shoulder:shoulder + width] != 0
    for role, side in ((0, 1), (2, -1)):
        inside, outside = columns - side * shoulder, columns + side * shoulder
        run = []
        for row,value,valid in zip(rows, values, valid_rows):
            # Both shoulders must be visible. The far sample ignores narrow tape
            # and the near sample places the boundary at the material or tape edge.
            change = value[columns+side]-value[columns-side]
            options = columns[valid & (value[inside] < _EDGE_VALUE_MIN)
                & (value[outside]-value[inside] >= profile.contrast_min) & (change >= _SURFACE_STEP_VALUE)]
            if run:
                previous = run[-1][1]
                options = options[abs(options-previous) <= max_step]
            if len(options):
                change_at = change[options-shoulder]
                # Prefer the first contrast when entering from the black surface.
                nearby = options[change_at >= .7 * change_at.max()]
                column = (min if side > 0 else max)(nearby)
                lo,hi = max(0,column-snap),min(len(value),column+snap+1)
                tape = np.flatnonzero(white[row,lo:hi])+lo
                if len(tape):
                    column = round((int(tape[0])+int(tape[-1]))/2)
                run.append((row,int(column),bool(len(tape))))
                continue
            if len(run) >= 5:
                candidates.append((role,run))
            run = []
        if len(run) >= 5:
            candidates.append((role,run))
    result = []
    for role, pixels in sorted(candidates,key=lambda item: -len(item[1]))[:8]:
        rows,columns,paint = np.asarray(pixels).T
        if paint.sum()*10 >= 9*len(paint):
            continue
        points = np.column_stack((profile.x_min_m+rows*profile.cell_m,profile.y_min_m+columns*profile.cell_m))
        contour = cv2.approxPolyDP(points.astype(np.float32), .006, False).reshape(-1, 2)
        keep = [int(np.argmin(np.sum((points-point)**2,axis=1))) for point in contour]
        pad, bounded = cache.point_offsets(np, u, v, rows, columns, diagnostics)
        errors = np.hypot(pad[:,0],pad[:,1])
        if (len(keep) < 2 or len(keep) > profile.max_points_per_strip or not bounded.all()
                or np.max(errors) > profile.max_projection_error_m):
            continue
        segment = tuple(tuple(float(a)+.006 for a in np.max(pad[i:j+1],axis=0)) for i,j in zip(keep,keep[1:]))
        result.append(TapeStrip(first_id+len(result),tuple(tuple(map(float,points[i])) for i in keep),
            (0.,0.),float(np.max(errors))+.006,'FOV_clip','FOV_clip',
            point_error_xy=tuple(tuple(map(float,pad[i])) for i in keep),segment_error_xy=segment,
            width_complete=False,outer_role=role))
        if first_id+len(result) == 32:
            break
    return tuple(result)


def extract_tape(bgr, camera, plane, profile=TapeProfile(), *, stamp=None, occlusion_mask=None,
                 valid_rgb=None, nn_candidates=None, diagnostics=None):
    """RGB에서 흰 테이프와 어두운 도로 바깥 경계를 별도 관측으로 뽑는다."""
    timing=timing_start(diagnostics,'tape.birdview_remap')
    if type(profile) is not TapeProfile or stamp is not None and type(stamp) is not FrameStamp:
        raise ValueError('expected tape profile and optional original FrameStamp')
    import numpy as np
    import cv2
    if (not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8
            or bgr.shape != (camera.height, camera.width, 3)):
        raise ValueError('tape input differs from camera BGR shape')
    if occlusion_mask is not None and (not isinstance(occlusion_mask, np.ndarray)
            or occlusion_mask.shape != bgr.shape[:2] or occlusion_mask.dtype not in (np.bool_, np.uint8)):
        raise ValueError('occlusion mask must be bool/uint8 in original RGB coordinates; nonzero is blocked')
    u, v, inside = _birdview_map(camera, plane, profile)
    warped = cv2.remap(bgr, u, v, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    timing_finish(diagnostics,timing)
    timing=timing_start(diagnostics,'tape.validity_remap')
    if valid_rgb is not None:
        if valid_rgb.shape != bgr.shape[:2] or valid_rgb.dtype != np.uint8:
            raise ValueError('valid_rgb must describe the rectified RGB grid')
    cache = _tape_projection(camera, plane, profile)
    valid = cache.visible_grid(np, cv2, u, v, inside, valid_rgb)
    timing_finish(diagnostics,timing)
    timing=timing_start(diagnostics,'tape.occlusion_remap')
    occ = np.zeros(valid.shape, dtype=np.bool_)
    if occlusion_mask is not None:
        footprint = cv2.dilate((occlusion_mask != 0).astype(np.uint8), np.ones((3, 3), np.uint8))
        occ = cv2.remap(footprint, u, v,
                        cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0) != 0
    timing_finish(diagnostics,timing)
    timing=timing_start(diagnostics,'tape.mask')
    hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
    visible = valid & ~occ
    white = _white_band(np, cv2, hsv, visible, profile)
    white = _close_tape_gaps(white, visible & (hsv[..., 1] <= profile.saturation_max), profile.cell_m)
    outer_paint = _outer_paint(np, cv2, hsv, visible, profile)
    outer_white = outer_paint.any(axis=0)
    white |= outer_white
    work = dict(grid_cells=int(white.size), components=0, component_cells=0, thin_iterations=0,
                graph_pixels=0, projection_checks=0, projection_batches=0,
                projection_unbounded=0, projection_limited=0, edge_tests=0)
    reasons = []
    complete = True
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white.astype(np.uint8), connectivity=8)
    if count - 1 > profile.max_components:
        complete = False
        reasons.append('COMPONENT_CAP')
        white[:] = False
    else:
        for index in range(1, count):
            bx, by, width, height, area = (int(v) for v in stats[index])
            work['components'] += 1
            if area < 4:
                bounds = (slice(by, by+height), slice(bx, bx+width))
                white[bounds][labels[bounds] == index] = False
                continue
            x0, x1 = max(0, bx - 4), min(white.shape[1], bx + width + 4)
            y0, y1 = max(0, by - 4), min(white.shape[0], by + height + 4)
            work['component_cells'] += (x1 - x0) * (y1 - y0)
            if work['component_cells'] > profile.max_component_work:
                complete = False
                reasons.append('COMPONENT_WORK_CAP')
                break
            part = labels[y0:y1, x0:x1] == index
            ring = (cv2.dilate(part.astype(np.uint8), np.ones((7, 7), np.uint8)) != 0) & ~part
            ring &= valid[y0:y1, x0:x1] & ~white[y0:y1, x0:x1]
            values = hsv[y0:y1, x0:x1, 2]
            if not np.any(ring) or float(np.median(values[part])) - float(np.median(values[ring])) < profile.contrast_min:
                white[y0:y1, x0:x1][part & ~outer_white[y0:y1, x0:x1]] = False
    distance = cv2.distanceTransform(white.astype(np.uint8), cv2.DIST_L2, 5)
    # T/X 교차부의 내접 원은 단일 band 반폭보다 크다. 그 영역을 넓은 물체로
    # 지우면 실제 분기가 네 개의 가림 tip으로 바뀌므로 더 깊은 white core만 격리한다.
    # 그보다 얇지만 폭 profile을 벗어난 물체는 아래 edge-pair 검사에서 제외한다.
    wide = distance * profile.cell_m > profile.tape_width_max_m
    if np.any(wide):
        radius = max(1, int(math.ceil(profile.tape_width_max_m / profile.cell_m)))
        censored = cv2.dilate(wide.astype(np.uint8), np.ones((2 * radius + 1, 2 * radius + 1), np.uint8)) != 0
        white[censored] = False
        occ |= censored
        reasons.append('WIDE_WHITE_SURFACE_CENSORED')
    timing_finish(diagnostics,timing)
    timing=timing_start(diagnostics,'tape.skeleton')
    skeleton, converged, work['thin_iterations'] = _thin(np, white, profile.max_thin_iterations)
    timing_finish(diagnostics,timing)
    if not converged:
        complete = False
        reasons.append('THINNING_CAP')
    work['graph_pixels'] = int(np.count_nonzero(skeleton))
    if work['graph_pixels'] > profile.max_graph_pixels:
        complete = False
        reasons.append('GRAPH_PIXEL_CAP')
        strips, junctions = [], ()
    else:
        timing=timing_start(diagnostics,'tape.projection')
        projection_error = np.zeros(valid.shape, dtype=np.float64)
        projection_xy = np.zeros(valid.shape + (2,), dtype=np.float64)
        near_invalid = cv2.dilate((~valid).astype(np.uint8), np.ones((3, 3), np.uint8)) != 0
        rows, columns = np.nonzero(skeleton)
        work['projection_checks'] = len(rows)
        if len(rows):
            # Work counts observations. TimingDiagnostics separately counts actual bound calculations.
            work['projection_batches'] = 1
            pad, bounded = cache.point_offsets(np, u, v, rows, columns, diagnostics)
            errors = np.nextafter(np.hypot(pad[:, 0], pad[:, 1]), np.inf)
            projection_error[rows, columns] = errors
            projection_xy[rows, columns] = np.nextafter(pad, np.inf)
            limited = bounded & (errors > profile.max_projection_error_m)
            work['projection_unbounded'] = int(np.count_nonzero(~bounded))
            work['projection_limited'] = int(np.count_nonzero(limited))
            if np.any(~bounded & ~near_invalid[rows, columns]):
                complete = False
                reasons.append('UNBOUNDED_PROJECTION')
            if np.any(limited):
                reasons.append('PROJECTION_LIMIT')
            rejected_rows, rejected_columns = rows[~bounded | limited], columns[~bounded | limited]
            skeleton[rejected_rows, rejected_columns] = False
            valid[rejected_rows, rejected_columns] = False
        timing_finish(diagnostics,timing)
        timing=timing_start(diagnostics,'tape.graph')
        strips, junctions, trace_complete = _trace(np, cv2, skeleton, valid, occ, white, projection_error,
                                                  projection_xy, profile, work)
        timing_finish(diagnostics,timing)
        if not trace_complete:
            complete = False
            for key, reason in (('trace_cap_exhausted', 'TRACE_WORK_CAP'),
                    ('edge_budget_exhausted', 'EDGE_WORK_CAP'), ('edge_pair_internal', 'UNRESOLVED_INTERIOR_EDGE_PAIR'),
                    ('no_width_paths', 'NO_MEASURED_TAPE_WIDTH'), ('point_cap_rejected', 'STRIP_POINT_CAP'),
                    ('strip_cap_exhausted', 'STRIP_COUNT_CAP'), ('junction_cap_exhausted', 'JUNCTION_COUNT_CAP')):
                if work[key]:
                    reasons.append(reason)
    timing=timing_start(diagnostics,'tape.surface_edges')
    surface_edges = _surface_edges(np,cv2,hsv,visible,white,u,v,cache,profile,len(strips),diagnostics)
    timing_finish(diagnostics,timing)
    paint_roles = _paint_roles(np, strips, hsv[..., 2], visible, profile, outer_paint)
    work['surface_edges'] = len(surface_edges)
    return TapeGraph(tuple(strips), junctions, 'UNKNOWN' if not complete else 'VALID' if strips else 'EMPTY',
        complete, tuple(reasons), None if stamp is None else stamp.key,
        bool(camera.qualified and plane.qualified), work, profile,
        _readonly(np, white), _readonly(np, skeleton), _readonly(np, valid), nn_candidates, surface_edges, paint_roles)
