"""수락한 map←odom 변환과 보수 오차를 다음 관측에 전파한다.

현재 관측을 검증하는 이전 frozen map은 움직이지 않는다. 정합이 허용한 실제
representative/bound로만 변환을 교체하고, 상관된 오차를 표본 수로 나누지 않는다.
"""

from dataclasses import dataclass, field, replace
import math

from .odometry import PoseEstimate
from .registration import transform_point, wrap_yaw
from .wheel_motion import relative_wheel_bounds, wheel_path_length_bound


@dataclass(frozen=True)
class MapOdomAlignment:
    pose: tuple[float, float, float]
    position_error_m: float
    yaw_error_rad: float
    context: tuple
    observed_j_interval: tuple | None = None
    registration_revision: int | None = None
    valid_for_us: int | None = None
    position_drift_mps: float | None = None
    yaw_drift_rps: float | None = None
    _matched_poses: tuple[PoseEstimate, PoseEstimate] | None = field(
        init=False, default=None, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.pose) is not tuple or len(self.pose)!=3 or type(self.context) is not tuple or len(self.context)!=4
                or any(type(v) is not str or not 1<=len(v)<=128 for v in self.context)
                or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>10000 for v in self.pose)
                or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=10000
                       for v in (self.position_error_m,self.yaw_error_rad))):
            raise ValueError('invalid original map alignment/context/bounds')
        metadata=(self.observed_j_interval,self.registration_revision,self.valid_for_us,self.position_drift_mps,self.yaw_drift_rps)
        if all(value is None for value in metadata):return
        if any(value is None for value in metadata):raise ValueError('alignment observation/revision/validity/drift must be supplied together')
        _interval(self.observed_j_interval)
        if (type(self.registration_revision) is not int or not 1<=self.registration_revision<2**63
                or type(self.valid_for_us) is not int or not 1<=self.valid_for_us<=1_000_000
                or self.observed_j_interval[0]+self.valid_for_us>=2**64
                or any(type(value) not in (int,float) or not math.isfinite(value) or value<0
                       for value in (self.position_drift_mps,self.yaw_drift_rps))):
            raise ValueError('invalid explicit alignment lifetime/drift')

    @property
    def has_metadata(self):return self.observed_j_interval is not None

    def fields(self):
        """legacy는 원4필드만 보존한다. null metadata를 새 운영 근거로 직렬화하지 않는다."""
        result=dict(pose=self.pose,position_error_m=self.position_error_m,yaw_error_rad=self.yaw_error_rad,context=self.context)
        if self.has_metadata:
            result.update(observed_j_interval=self.observed_j_interval,registration_revision=self.registration_revision,
                valid_for_us=self.valid_for_us,position_drift_mps=self.position_drift_mps,yaw_drift_rps=self.yaw_drift_rps)
        return result

    @classmethod
    def from_fields(cls,value):
        old={'pose','position_error_m','yaw_error_rad','context'}
        new={'observed_j_interval','registration_revision','valid_for_us','position_drift_mps','yaw_drift_rps'}
        if type(value) is not dict or set(value) not in (old,old|new):raise ValueError('unsupported exact alignment metadata shape')
        fields=dict(value)
        if not isinstance(fields['pose'],(tuple,list)) or not isinstance(fields['context'],(tuple,list)):
            raise ValueError('original alignment vectors required')
        fields['pose']=tuple(fields['pose']);fields['context']=tuple(fields['context'])
        if set(value)==old|new:
            if any(fields[name] is None for name in new):raise ValueError('full alignment metadata cannot contain unknown fields')
            if not isinstance(fields['observed_j_interval'],(tuple,list)):raise ValueError('original observed interval required')
            fields['observed_j_interval']=tuple(fields['observed_j_interval'])
        return cls(**fields)

    def valid_at(self,now_us):
        """현재 publication/정상 clock 갱신은 원 earliest+valid_for 기한을 늘리지 않는다."""
        return (self.has_metadata and type(now_us) is int
            and self.observed_j_interval[1]<=now_us<self.observed_j_interval[0]+self.valid_for_us)

    @classmethod
    def from_match(cls, odom, fitted, *, observed_j_interval=None,registration_revision=None,
                   valid_for_us=None,position_drift_mps=None,yaw_drift_rps=None):
        if type(odom) is not PoseEstimate or type(fitted) is not PoseEstimate or odom.sample.context!=fitted.sample.context:
            raise ValueError('alignment requires the same original odom context')
        raw,goal=odom.sample,fitted.sample
        if observed_j_interval is not None and (observed_j_interval!=odom.captured_us or fitted.captured_us!=odom.captured_us):
            raise ValueError('alignment must preserve the full original accepted capture interval')
        yaw=wrap_yaw(goal.yaw-raw.yaw)
        rotated=transform_point((raw.x,raw.y),(0.,0.,yaw))
        angle_error=min(math.pi,goal.yaw_error_rad+raw.yaw_error_rad)
        error=goal.position_error_m+raw.position_error_m+2*math.hypot(raw.x,raw.y)*math.sin(angle_error/2)
        alignment = cls((goal.x-rotated[0],goal.y-rotated[1],yaw),error,angle_error,raw.context,
            observed_j_interval,registration_revision,valid_for_us,position_drift_mps,yaw_drift_rps)
        object.__setattr__(alignment, '_matched_poses', (odom, fitted))
        return alignment

    def apply(self, odom, *, yaw_drift_rad=None):
        if type(odom) is not PoseEstimate or odom.sample.context!=self.context:
            raise ValueError('alignment cannot cross odom epoch/calibration/clock/origin')
        elapsed_us=0
        if self.has_metadata:
            _interval(odom.captured_us)
            if odom.captured_us[1]<self.observed_j_interval[1]:raise ValueError('alignment cannot apply to an older original capture')
            elapsed_us=odom.captured_us[1]-self.observed_j_interval[0]
        if yaw_drift_rad is not None:
            return self._project(odom,elapsed_us,yaw_drift_rad=yaw_drift_rad)
        return self._project(odom,elapsed_us)

    def project_history(self, odom):
        """첫 정합 전 선 관측을 원 시각과 오차로 지도에 옮긴다."""
        if (type(odom) is not PoseEstimate or odom.sample.context!=self.context
                or not self.has_metadata or self._matched_poses is None):
            raise ValueError('historical observation requires its original matched odom context')
        _interval(odom.captured_us)
        if odom.captured_us[1]>self.observed_j_interval[0]:
            raise ValueError('historical observation overlaps the alignment capture')
        return self._project(odom,self.observed_j_interval[1]-odom.captured_us[0])

    def project_neighbor(self, odom):
        """Carry a fitted capture pose to a nearby raw sample, including overlapping intervals."""
        if (type(odom) is not PoseEstimate or odom.sample.context!=self.context
                or not self.has_metadata or self._matched_poses is None):
            raise ValueError('neighbor projection requires an original matched odom context')
        _interval(odom.captured_us)
        lo,hi = self.observed_j_interval
        start,end = odom.captured_us
        return self._project(odom,max(abs(end-lo),abs(hi-start)))

    @classmethod
    def project_startup(cls, initial, odom, *, position_drift_mps, yaw_drift_rps):
        """Bound a new map from its first continuous encoder event."""
        if initial.sample.context != odom.sample.context or odom.captured_us[1] < initial.captured_us[1]:
            raise ValueError('startup projection requires the original odometry context and time order')
        wheel = initial.wheel_capture
        if wheel is not None and wheel.anchor_sample is not None:
            anchor = wheel.anchor_sample
            instant = (anchor.at_us, anchor.at_us)
            origin = PoseEstimate(anchor, instant, instant,
                                  replace(wheel, position_pad_m=0., yaw_pad_rad=0.))
            bounds = relative_wheel_bounds(origin, odom)
            if bounds is not None:
                # The encoder event defines this map's gauge. Visual-match drift
                # starts with a visual match; wheel calibration bounds this leg.
                position, yaw = bounds
                return replace(odom, sample=replace(odom.sample, position_error_m=position, yaw_error_rad=yaw))
        return cls.from_match(initial, initial)._project(odom, odom.captured_us[1] - initial.captured_us[0],
            drift_rates=(position_drift_mps, yaw_drift_rps))

    def _project(self, odom, elapsed_us, *, yaw_drift_rad=None, drift_rates=None):
        raw=odom.sample
        if self._matched_poses is not None:
            original, fitted = self._matched_poses
            if odom == original and fitted.captured_us == odom.captured_us:
                return replace(odom,sample=fitted.sample,wheel_capture=None)
        transform_angle=self.yaw_error_rad;position_drift=yaw_drift=0.
        # Before the first visual anchor, the upstream pose budget can omit
        # encoder quantization. Retain the wheel bound when it is wider.
        combine_wheel_bound = max if drift_rates is not None else min
        if self.has_metadata:
            drift_rates = self.position_drift_mps, self.yaw_drift_rps
        if drift_rates is not None:
            seconds=math.nextafter(elapsed_us/1_000_000,math.inf)
            position_rate, yaw_rate = drift_rates
            position_drift=math.nextafter(position_rate*seconds,math.inf) if position_rate else 0.
            yaw_drift=math.nextafter(yaw_rate*seconds,math.inf) if yaw_rate else 0.
            if yaw_drift_rad is not None:
                yaw_drift = yaw_drift_rad
            transform_angle=math.nextafter(transform_angle+yaw_drift,math.inf)
        path_length = (wheel_path_length_bound(original,odom)
            if self._matched_poses is not None and yaw_drift else None)
        # 고정 방향 오차는 변위에, 이동 중 누적된 방향 오차는 주행 길이에 적용한다.
        fixed_angle = transform_angle if path_length is None else self.yaw_error_rad
        x,y=transform_point((raw.x,raw.y),self.pose)
        rotation_scale=2*math.sin(min(math.pi,fixed_angle)/2)
        rotation=math.hypot(raw.x,raw.y)*rotation_scale
        error=self.position_error_m+raw.position_error_m+rotation+position_drift
        angle=transform_angle+raw.yaw_error_rad
        if self._matched_poses is not None:
            start, goal = original.sample, fitted.sample
            distance = math.hypot(raw.x-start.x, raw.y-start.y)
            # map 기준점 오차는 한 번 포함하고, yaw 오차는 기준점 이후 변위에 적용한다.
            relative_error = (goal.position_error_m + start.position_error_m + raw.position_error_m
                              + distance*rotation_scale + position_drift)
            error = min(error, relative_error)
            wheel_bounds = relative_wheel_bounds(original,odom)
            if wheel_bounds is not None:
                position, yaw = wheel_bounds
                anchor_angle = goal.yaw_error_rad + (yaw_drift if path_length is None else 0.)
                anchor_rotation = 2*math.sin(min(math.pi,anchor_angle)/2)
                error = combine_wheel_bound(error,goal.position_error_m+position+distance*anchor_rotation+position_drift)
                angle = combine_wheel_bound(angle,goal.yaw_error_rad+yaw+yaw_drift)
        if path_length is not None:
            error += 2*path_length*math.sin(min(math.pi,yaw_drift)/2)
        if angle>math.pi and drift_rates is None:
            raise ValueError('alignment no longer bounds a usable heading')
        if drift_rates is not None:
            error=math.nextafter(error,math.inf);angle=min(math.pi,math.nextafter(angle,math.inf))
        sample=replace(raw,x=x,y=y,yaw=wrap_yaw(raw.yaw+self.pose[2]),position_error_m=error,yaw_error_rad=angle)
        return PoseEstimate(sample,odom.captured_us,odom.bracket_us)


def _interval(value):
    if (type(value) is not tuple or len(value)!=2 or any(type(v) is not int or not 0<=v<2**64 for v in value)
            or value[0]>value[1]):raise ValueError('original alignment capture interval required')
