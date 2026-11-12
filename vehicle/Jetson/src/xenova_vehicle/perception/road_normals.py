"""지도에 놓인 선을 따라 진행 방향의 법선 단면을 샘플링한다."""

from bisect import bisect_right
from dataclasses import dataclass
import math
from statistics import median

from ..mapping.registration import wrap_yaw


def heading_error(a, b, ea, eb):
    dx,dy = b[0]-a[0],b[1]-a[1]
    ex,ey = ea[0]+eb[0],ea[1]+eb[1]
    if abs(dx)<=ex and abs(dy)<=ey:
        return math.pi
    nominal = math.atan2(dy,dx)
    return max(abs(wrap_yaw(math.atan2(dy+y,dx+x)-nominal)) for x in (-ex,ex) for y in (-ey,ey))


@dataclass(frozen=True)
class _Trace:
    points: tuple
    arc: tuple
    headings: tuple
    errors: tuple

    def at(self, distance):
        i = min(len(self.points)-2,bisect_right(self.arc,distance)-1)
        a,b = self.points[i:i+2]
        length = self.arc[i+1]-self.arc[i]
        t = (distance-self.arc[i])/length if length else 0.
        return (a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])),i


def _trace(strip, curvature):
    forward = strip.original.points_xy[-1][0] >= strip.original.points_xy[0][0]
    points = strip.points if forward else strip.points[::-1]
    edge_errors = strip.heading_errors if forward else strip.heading_errors[::-1]
    raw = strip.original.points_xy if forward else strip.original.points_xy[::-1]
    errors_xy = strip.original.point_error_xy or ((strip.original.error_m,strip.original.error_m),)*len(raw)
    if not forward:
        errors_xy = errors_xy[::-1]
    arc = [0.]
    for a,b in zip(points,points[1:]):
        arc.append(arc[-1]+math.dist(a,b))
    path = _Trace(points,tuple(arc),(),edge_errors)
    headings = []
    errors = []
    for i,(first,last) in enumerate(zip(arc,arc[1:])):
        center = (first+last)/2
        # 짧은 픽셀 요철보다 같은 관측에서 앞뒤를 포함한 방향을 사용한다.
        a,_ = path.at(max(0.,center-.09))
        b,_ = path.at(min(arc[-1],center+.09))
        heading = math.atan2(b[1]-a[1],b[0]-a[0])
        headings.append(heading)
        # 실제 꼭짓점 둘을 사용한 각도 상한에 단면까지의 곡률과 원 자세 오차를 더한다.
        lo = bisect_right(arc,max(0.,center-.12))-1
        hi = min(len(arc)-1,bisect_right(arc,min(arc[-1],center+.12)))
        p,q = points[lo],points[hi]
        wide = math.atan2(q[1]-p[1],q[0]-p[0])
        uncertainty = (heading_error(raw[lo],raw[hi],errors_xy[lo],errors_xy[hi])+strip.pose_yaw_error_rad
                       +curvature*max(center-arc[lo],arc[hi]-center)+abs(wrap_yaw(wide-heading)))
        p,q = points[i],points[i+1]
        edge_heading = math.atan2(q[1]-p[1],q[0]-p[0])
        edge_bound = edge_errors[i]+abs(wrap_yaw(edge_heading-heading))+curvature*(last-first)/2
        errors.append(min(math.pi,uncertainty,edge_bound))
    return _Trace(points,tuple(arc),tuple(headings),tuple(errors))


class ReferenceObservations:
    """기준선의 다음 법선과 교차하는 실제 흰선과 관측 ID를 조회한다."""

    def __init__(self, strips, roles, profile):
        self._cell_m = profile.missing_outer_width_m
        self._edges = []
        self._cells = {}
        for strip in strips:
            key = strip.source, strip.identity
            role = roles.get(key, ())
            if strip.original.closed or len(role) > 1:
                continue
            path = _trace(strip, profile.max_curvature_1pm)
            for a,b,heading,yaw in zip(path.points,path.points[1:],path.headings,path.errors):
                index = len(self._edges)
                self._edges.append((a,b,heading,strip.error_m,yaw,key,next(iter(role),None)))
                for cell in self._box(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])):
                    self._cells.setdefault(cell,[]).append(index)

    def _box(self, left, top, right, bottom):
        for i in range(math.floor(left/self._cell_m),math.floor(right/self._cell_m)+1):
            for j in range(math.floor(top/self._cell_m),math.floor(bottom/self._cell_m)+1):
                yield i,j

    def at(self, center, heading, width):
        px,py = center
        c,s = math.cos(heading),math.sin(heading)
        tolerance = width/3
        dx,dy = abs(s)*(width+tolerance),abs(c)*(width+tolerance)
        indices = {index for cell in self._box(px-dx,py-dy,px+dx,py+dy)
                   for index in self._cells.get(cell,())}
        samples = [{},{},{}]
        for i in sorted(indices):
            a,b,direction,error,yaw,key,known_role = self._edges[i]
            turn = wrap_yaw(direction-heading)
            if abs(turn) > .48:
                continue
            ax,ay,bx,by = a[0]-px,a[1]-py,b[0]-px,b[1]-py
            u,v = ax*c+ay*s,bx*c+by*s
            if u*v > 0. or abs(v-u) < 1e-9:
                continue
            t = -u/(v-u)
            lateral = -(ax+t*(bx-ax))*s+(ay+t*(by-ay))*c
            role = 1-round(lateral/width)
            if not 0 <= role <= 2 or known_role not in (None,role):
                continue
            residual = lateral-(1-role)*width
            if abs(residual) > tolerance:
                continue
            old = samples[role].get(key)
            if old is None or abs(residual) < abs(old[0]):
                samples[role][key] = residual,turn,error,yaw
        result = {}
        for role,items in enumerate(samples):
            if not items:
                continue
            center_offset = median(value[0] for value in items.values())
            cluster = {key:value for key,value in items.items() if abs(value[0]-center_offset) <= tolerance/2}
            if not cluster or len(cluster)*2 < len(items):
                continue
            if len(cluster) == 1 and len(items) > 1:
                continue
            relative = median(value[1] for value in cluster.values())
            error = max(value[2]+abs(value[0]-center_offset) for value in cluster.values())
            yaw = max(value[3]+abs(value[1]-relative) for value in cluster.values())
            result[role] = center_offset,relative,error,yaw,frozenset(cluster)
        if len(result) > 1 and max(x[0] for x in result.values())-min(x[0] for x in result.values()) > tolerance:
            return {}
        return result


def normal_samples(strips, seeds, *, width_m, radius_m, step_m, heading_limit, curvature):
    """선 끝의 모양과 무관하게 주변 선이 법선과 만나는 위치와 원 오차를 돌려준다."""
    traces = {(s.source,s.identity):_trace(s,curvature) for s in strips}
    cells = {}
    edges = []
    for strip in strips:
        path = traces[strip.source,strip.identity]
        for i,(a,b) in enumerate(zip(path.points,path.points[1:])):
            if path.arc[i+1] <= path.arc[i]:
                continue
            index = len(edges)
            edges.append((a,b,path.headings[i],path.errors[i],strip))
            for x in range(math.floor(min(a[0],b[0])/width_m),math.floor(max(a[0],b[0])/width_m)+1):
                for y in range(math.floor(min(a[1],b[1])/width_m),math.floor(max(a[1],b[1])/width_m)+1):
                    cells.setdefault((x,y),[]).append(index)
    for seed in seeds:
        path = traces.get((seed.source,seed.identity))
        if path is None or path.arc[-1] < step_m:
            continue
        count = min(32,math.ceil(path.arc[-1]/step_m))
        stations = []
        for i in range(count+1):
            at = path.arc[-1]*i/count
            point,index = path.at(at)
            heading = path.headings[index]
            c,s = math.cos(heading),math.sin(heading)
            px,py = point
            # 법선의 bbox 안에 있는 실제 선분만 조회한다.
            dx,dy = abs(s)*radius_m,abs(c)*radius_m
            nearby = set()
            for x in range(math.floor((px-dx)/width_m),math.floor((px+dx)/width_m)+1):
                for y in range(math.floor((py-dy)/width_m),math.floor((py+dy)/width_m)+1):
                    nearby.update(cells.get((x,y),()))
            samples = [(0.,0.,seed.error_m,seed.source,seed.identity,path.errors[index])]
            for edge in sorted(nearby):
                a,b,direction,error,strip = edges[edge]
                if (strip.source,strip.identity) == (seed.source,seed.identity):
                    continue
                relative = wrap_yaw(direction-heading)
                if abs(relative)>heading_limit:
                    continue
                ax,ay,bx,by = a[0]-px,a[1]-py,b[0]-px,b[1]-py
                u,v = ax*c+ay*s,bx*c+by*s
                if min(u,v)>1e-9 or max(u,v)<-1e-9 or abs(v-u)<=1e-9:
                    continue
                t = min(1.,max(0.,-u/(v-u)))
                lateral = -(ax+t*(bx-ax))*s+(ay+t*(by-ay))*c
                if abs(lateral)<=radius_m:
                    samples.append((lateral,relative,strip.error_m,strip.source,strip.identity,error))
            stations.append((point,heading,samples))
        yield (seed.source,seed.identity),stations
