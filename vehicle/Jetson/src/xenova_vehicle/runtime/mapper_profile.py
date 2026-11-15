"""선택형 mapper-profile local schema1. 기존 wire/차량 configuration schema가 아니다.

모든 알고리즘/운동 값은 명시한다. capture_timing/localization은 생략/null을 허용하며 None bound나
false qualification을 수락한 값으로 채우지 않는다. 파일 읽기·센서 승인·MQTT·모드·이동
권위는 만들지 않는다. S-log는 bounded 원 파일을 읽어 from_bytes의 canonical 사본을
WorkerBoot로 전달한다. 로컬 파일 SHA와 차량 configuration ID는 별도 식별자다.
"""

from dataclasses import dataclass, fields
from hashlib import sha256
import json
import math

from ..mapping.alignment import REGISTRATION_MAX_AGE_US, SURVEY_REGISTRATION_MAX_AGE_US
from ..mapping.local_map import GridSpec
from ..mapping.map_lattice import GridWindow,RollingPolicy
from ..mapping.registration import RegistrationProfile
from ..mapping.survey_course import SurveyCourse
from ..mapping.wheel_motion import WheelMotionProfile
from ..perception.adapters import CaptureTiming
from ..perception.calibration import CalibrationProfile
from ..perception.depth import DepthConfig
from ..perception.free_space import TargetClass
from ..perception.parking import ParkingAisle
from ..perception.pipeline import PipelineProfile
from ..perception.road import RoadGeometryLimits, RoadProfile
from ..perception.tape import TapeProfile
from ..perception.tof import TofLeaf
from ..perception.visual_registration import VisualRegistrationPolicy
from .scene import SurveyProfile


MAX_BYTES = 65536
TOP = frozenset(('schema_version','vehicle_configuration','calibration_profile_sha256','capture_timing','demo_road_fill','demo_pose_bounds',
                 'localization','catalog_policy','survey','survey_course','rolling_roi','sensors','pipeline','targets','parking_aisles','stopping','motion_qualified'))


class MapperProfileError(ValueError):
    """명시 설정이 현재 bounded local profile/asset 관계와 맞지 않는다."""


def _uint(value, minimum=0, maximum=(1<<63)-1):
    if type(value) is not int or not minimum<=value<=maximum: raise MapperProfileError('bounded mapper integer required')
    return value


def _finite(value, minimum=0., maximum=100.):
    if type(value) not in (int,float) or not math.isfinite(value) or not minimum<=value<=maximum:
        raise MapperProfileError('explicit finite mapper bound required')
    return value


def _label(value):
    if type(value) is not str or not 1<=len(value)<=64 or any(not 33<=ord(c)<=126 for c in value):
        raise MapperProfileError('bounded printable mapper identity required')
    return value


def _sha(value):
    if type(value) is not str or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise MapperProfileError('lowercase mapper SHA256 required')
    return value


def _owned(value):
    """native 입력도 직렬화 전 node/depth/JSON 비용을 제한하고 hook/cycle을 거절한다."""
    nodes=cost=0; active=set()
    def copy(item,depth):
        nonlocal nodes,cost
        nodes+=1
        if nodes>16384 or depth>12: raise MapperProfileError('mapper tree work bound')
        kind=type(item)
        if item is None or kind is bool: result=item; cost+=5
        elif kind is int:
            if item.bit_length()>63: raise MapperProfileError('mapper integer range')
            result=item; cost+=len(str(item))
        elif kind is float:
            if not math.isfinite(item): raise MapperProfileError('nonfinite mapper number')
            result=0. if item==0 else item; cost+=len(json.dumps(result,allow_nan=False))
        elif kind is str:
            if not item.isascii() or len(item)>MAX_BYTES: raise MapperProfileError('bounded ASCII mapper text required')
            result=item; cost+=len(json.dumps(item,ensure_ascii=True))
        elif kind is dict or kind is list or kind is tuple:
            if id(item) in active or len(item)>(128 if kind is dict else 4096): raise MapperProfileError('mapper container bound/cycle')
            active.add(id(item)); cost+=2+max(0,len(item)-1)
            try:
                if kind is dict:
                    if any(type(key) is not str or len(key)>64 for key in item): raise MapperProfileError('mapper keys')
                    result={}
                    for key,child in item.items():
                        own_key=copy(key,depth+1); cost+=1; result[own_key]=copy(child,depth+1)
                else: result=[copy(child,depth+1) for child in item]
            finally: active.remove(id(item))
        else: raise MapperProfileError('exact builtin mapper values required')
        if cost>MAX_BYTES: raise MapperProfileError('canonical mapper JSON exceeds64KiB')
        return result
    return copy(value,0)


def _tuple(value):
    return tuple(_tuple(item) for item in value) if type(value) is list else value


def _typed(cls,value):
    names={f.name for f in fields(cls) if f.init}
    if type(value) is not dict: raise MapperProfileError('all '+cls.__name__+' fields must be explicit')
    keys=set(value)
    if keys!=names and not (cls is RoadProfile and keys==names-{'missing_outer_width_bounds_m'}):
        raise MapperProfileError('all '+cls.__name__+' fields must be explicit')
    for field in fields(cls):
        if not field.init or field.name not in value: continue
        item=value[field.name]
        if field.type in (int,'int') and type(item) is not int: raise MapperProfileError('integer field: '+field.name)
        if field.type in (float,'float') and type(item) not in (int,float): raise MapperProfileError('numeric field: '+field.name)
        if field.type in (bool,'bool') and type(item) is not bool: raise MapperProfileError('boolean field: '+field.name)
    try: return cls(**{key:_tuple(item) for key,item in value.items()})
    except (ValueError,TypeError,OverflowError) as error: raise MapperProfileError('invalid '+cls.__name__+': '+str(error)) from error


@dataclass(frozen=True)
class SensorLimits:
    vehicle_max_age_us: int
    tof_max_age_us: int
    max_speed_mps: float
    max_yaw_rate_rps: float
    max_gap_us: int
    capacity: int
    tof_sources: tuple
    wheel_odometry: WheelMotionProfile | None = None

    def __post_init__(self):
        for name in ('vehicle_max_age_us','tof_max_age_us','max_gap_us'): _uint(getattr(self,name),1,1_000_000)
        _uint(self.capacity,2,256)
        for name in ('max_speed_mps','max_yaw_rate_rps'):
            if not _finite(getattr(self,name)): raise MapperProfileError('positive sensor motion bound required')
        if type(self.tof_sources) is not tuple or len(self.tof_sources)>2: raise MapperProfileError('at most2 explicit ToF sources')
        if self.wheel_odometry is not None and type(self.wheel_odometry) is not WheelMotionProfile:
            raise MapperProfileError('explicit wheel odometry calibration required')
        sensors=set(); names=set()
        for entry in self.tof_sources:
            if type(entry) is not tuple or len(entry)!=2 or type(entry[1]) is not TofLeaf: raise MapperProfileError('typed external ToF leaf required')
            name,leaf=entry; _label(name)
            if leaf.sensor_id in sensors or name in names: raise MapperProfileError('duplicate ToF source inventory')
            sensors.add(leaf.sensor_id); names.add(name)

    @property
    def tof_leaves(self):
        """SensorSamples용 독립 dict. 반환한 dict를 바꿔도 profile은 변하지 않는다."""
        return {leaf.sensor_id:(name,leaf) for name,leaf in self.tof_sources}

    @classmethod
    def from_dict(cls,value):
        names = {f.name for f in fields(cls)}
        if (type(value) is not dict or not names-{'wheel_odometry'} <= set(value) <= names
                or type(value['tof_sources']) is not list):
            raise MapperProfileError('exact SensorLimits fields required')
        if len(value['tof_sources'])>2: raise MapperProfileError('ToF inventory exceeds2')
        leaves=[]
        for row in value['tof_sources']:
            if type(row) is not dict or set(row)!={'external_leaf_name','leaf'}: raise MapperProfileError('exact external leaf row')
            try: leaf=TofLeaf.from_dict(row['leaf'])
            except (ValueError,TypeError,OverflowError) as error: raise MapperProfileError('invalid original ToF leaf') from error
            leaves.append((row['external_leaf_name'],leaf))
        wheel = value.get('wheel_odometry')
        return cls(**{**value,'tof_sources':tuple(leaves),
                      'wheel_odometry':None if wheel is None else _typed(WheelMotionProfile,wheel)})


@dataclass(frozen=True)
class LocalizationLimits:
    """명시 로컬 bound이며 wire/실측 승인이 아니다. drift0 검증 책임은 caller에 있다."""

    registration_valid_for_us: int
    position_drift_mps: float
    yaw_drift_rps: float

    def __post_init__(self):
        _uint(self.registration_valid_for_us,1,SURVEY_REGISTRATION_MAX_AGE_US)
        for name in ('position_drift_mps','yaw_drift_rps'):
            try: _finite(getattr(self,name),maximum=math.inf)
            except OverflowError as error:
                raise MapperProfileError('finite localization drift required') from error


@dataclass(frozen=True)
class StoppingLimits:
    max_speed_mps: float
    min_braking_mps2: float
    reaction_latency_us: int
    cycle_bound_us: int
    completion_bound_us: int
    body_error_m: float

    def __post_init__(self):
        for name in ('max_speed_mps','min_braking_mps2'):
            if not _finite(getattr(self,name)): raise MapperProfileError('positive stopping speed/braking required')
        _finite(self.body_error_m,maximum=10.)
        _uint(self.reaction_latency_us,0,10_000_000); _uint(self.cycle_bound_us,1,10_000_000)
        _uint(self.completion_bound_us,0,10_000_000)


@dataclass(frozen=True)
class CatalogPolicy:
    """설치 도로의 횡단 규칙과 통로 역할이다. lane ID나 관측/이동 승인은 아니다."""

    crossing_allowed: bool
    aisle_lane_indices: tuple

    def __post_init__(self):
        if (type(self.crossing_allowed) is not bool or type(self.aisle_lane_indices) is not tuple
                or len(self.aisle_lane_indices)>4
                or any(type(index) is not int or index not in (0,1) for index in self.aisle_lane_indices)):
            raise MapperProfileError('explicit crossing bool and at most four left0/right1 lane roles required')

    def crossing(self,road,stamp):
        """원 frame의 유한 divider segment에만 설치 규칙을 적용한다. 기하는 별도로 검사한다."""
        from ..perception.road import RoadResult
        from ..perception.inputs import FrameStamp
        if type(road) is not RoadResult or type(stamp) is not FrameStamp or road.source_key!=stamp.key:
            raise MapperProfileError('original road observation and frame required')
        count=0 if not road.hypotheses else len(road.hypotheses[0].divider)-1
        if not 0<=count<=31:raise MapperProfileError('original divider segment count exceeds wire cap')
        return bytes([int(self.crossing_allowed)])*count


@dataclass(frozen=True,init=False)
class MapperProfile:
    canonical_bytes: bytes
    sha256: str
    vehicle_configuration: str
    calibration_profile_sha256: str
    capture_timing: CaptureTiming | None
    localization: LocalizationLimits | None
    catalog_policy: CatalogPolicy | None
    survey: SurveyProfile | None
    survey_course: SurveyCourse | None
    rolling_roi: RollingPolicy | None
    sensors: SensorLimits
    pipeline: PipelineProfile
    targets: tuple
    parking_aisles: tuple
    stopping: StoppingLimits
    declared_motion_qualified: bool
    demo_road_fill: bool
    demo_pose_bounds: tuple[float, float] | None

    def __init__(self,value):
        own=_owned(value)
        if type(own) is not dict or set(own)-TOP or TOP-{'capture_timing','localization','catalog_policy','survey','survey_course','rolling_roi','demo_road_fill','demo_pose_bounds'}-set(own) or type(own.get('schema_version')) is not int or own['schema_version']!=1:
            raise MapperProfileError('exact mapper-profile local schema1 required')
        own.setdefault('capture_timing',None)
        _label(own['vehicle_configuration']); _sha(own['calibration_profile_sha256'])
        if type(own['motion_qualified']) is not bool: raise MapperProfileError('explicit motion qualification required')
        timing=None if own['capture_timing'] is None else _typed(CaptureTiming,own['capture_timing'])
        localization=None if own.get('localization') is None else _typed(LocalizationLimits,own['localization'])
        sensors=SensorLimits.from_dict(own['sensors']); stopping=_typed(StoppingLimits,own['stopping'])
        if stopping.max_speed_mps<sensors.max_speed_mps: raise MapperProfileError('stopping bound cannot understate admitted sensor motion')
        data=own['pipeline']
        optional={'visual_registration':VisualRegistrationPolicy,'road_geometry_limits':RoadGeometryLimits}
        pipeline_names={f.name for f in fields(PipelineProfile) if f.init}
        if type(data) is not dict or set(data)-pipeline_names or pipeline_names-set(optional)-set(data):
            raise MapperProfileError('all PipelineProfile fields must be explicit')
        nested={name:_typed(cls,data[name]) for name,cls in (('grid',GridSpec),('tape',TapeProfile),('road',RoadProfile),
                ('depth',DepthConfig),('registration',RegistrationProfile))}
        nested.update((name,_typed(cls,data[name])) for name,cls in optional.items() if data.get(name) is not None)
        _uint(data['free_ttl_us'],1,500000); _uint(data['max_observation_age_us'],1,500000)
        if (not 2<=nested['grid'].width<=64 or not 2<=nested['grid'].height<=64 or nested['depth'].stride!=1):
            raise MapperProfileError('deployment grid/stride1/original500ms age bound exceeded')
        _label(data['permission_source'])
        try: pipeline=PipelineProfile(**{key:nested[key] if key in nested else _tuple(item) for key,item in data.items()})
        except (ValueError,TypeError,OverflowError) as error: raise MapperProfileError('invalid PipelineProfile') from error
        rolling=None if own.get('rolling_roi') is None else _typed(RollingPolicy,own['rolling_roi'])
        if rolling is not None:
            try:rolling.check_window(GridWindow.from_spec(pipeline.grid))
            except ValueError as error:raise MapperProfileError('rolling policy exceeds original grid') from error
        targets=own['targets']; aisles=own['parking_aisles']
        if type(targets) is not list or len(targets)>4 or type(aisles) is not list or len(aisles)>4:
            raise MapperProfileError('bounded targets4/parking aisles4 required')
        target_values=tuple(_typed(TargetClass,item) for item in targets)
        demo_road_fill = own.get('demo_road_fill', False)
        if type(demo_road_fill) is not bool or (demo_road_fill and not target_values):
            raise MapperProfileError('demo road fill requires an explicit boolean and obstacle targets')
        demo_pose_bounds = own.get('demo_pose_bounds')
        if demo_pose_bounds is not None:
            keys = ('position_m', 'heading_rad')
            if (not demo_road_fill or type(demo_pose_bounds) is not dict or set(demo_pose_bounds) != set(keys)
                    or any(type(v) not in (float, int) or not 0 < v <= 1 for v in demo_pose_bounds.values())):
                raise MapperProfileError('demo pose bounds require demo road fill and explicit positive metre/radian values')
            demo_pose_bounds = tuple(float(demo_pose_bounds[key]) for key in keys)
        aisle_values=tuple(_typed(ParkingAisle,item) for item in aisles)
        catalog_policy=None if own.get('catalog_policy') is None else _typed(CatalogPolicy,own['catalog_policy'])
        if catalog_policy is not None and (len(catalog_policy.aisle_lane_indices)!=len(aisle_values)
                or any(aisle.approach_lane_id!=0 for aisle in aisle_values)):
            raise MapperProfileError('each aisle needs a declared lane role and no preassigned durable lane ID')
        survey=None if own.get('survey') is None else _typed(SurveyProfile,own['survey'])
        if survey is not None:
            # 단일 존 ToF는 근거리 HIT 보조이며 정적 지도 조사의 필수 근거가 아니다.
            if (survey.input_kind!='LIVE' or survey.required_sensor_mask&~3
                    or survey.observation_max_age_us>pipeline.max_observation_age_us
                    or survey.max_pose_gap_us>sensors.max_gap_us
                    or survey.station_window_us>1_000_000 or survey.atlas_cell_capacity>128*4096):
                raise MapperProfileError('survey exceeds installed sensors or original observation/window/atlas bounds')
        course = own.get('survey_course')
        if course is not None and (localization is None or catalog_policy is None or aisles):
            raise MapperProfileError('survey course requires localization and an explicit road-only crossing policy')
        survey_course = None if course is None else _typed(SurveyCourse, course)
        if localization is not None and survey_course is None:
            _uint(localization.registration_valid_for_us,1,REGISTRATION_MAX_AGE_US)
        raw=json.dumps(own,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        if len(raw)>MAX_BYTES: raise MapperProfileError('canonical mapper profile exceeds64KiB')
        for name,item in dict(canonical_bytes=raw,sha256=sha256(raw).hexdigest(),vehicle_configuration=own['vehicle_configuration'],
                calibration_profile_sha256=own['calibration_profile_sha256'],capture_timing=timing,localization=localization,catalog_policy=catalog_policy,survey=survey,survey_course=survey_course,rolling_roi=rolling,sensors=sensors,pipeline=pipeline,
                targets=target_values,parking_aisles=aisle_values,stopping=stopping,declared_motion_qualified=own['motion_qualified'],
                demo_road_fill=demo_road_fill,demo_pose_bounds=demo_pose_bounds).items():
            object.__setattr__(self,name,item)

    @classmethod
    def from_dict(cls,value): return cls(value)

    @classmethod
    def from_bytes(cls,raw):
        if type(raw) is not bytes or not 0<len(raw)<=MAX_BYTES: raise MapperProfileError('bounded original mapper bytes required')
        def pairs(items):
            value={}
            for key,item in items:
                if key in value: raise MapperProfileError('duplicate mapper JSON key')
                value[key]=item
            return value
        def integer(value):
            if len(value)>20: raise MapperProfileError('mapper integer lexical bound')
            number=int(value); _uint(abs(number)); return number
        try:
            value=json.loads(raw.decode('ascii'),object_pairs_hook=pairs,parse_int=integer,
                parse_constant=lambda value:(_ for _ in ()).throw(MapperProfileError('nonfinite mapper JSON')))
        except (UnicodeError,json.JSONDecodeError,RecursionError) as error: raise MapperProfileError('invalid mapper JSON') from error
        return cls(value)

    def to_dict(self): return json.loads(self.canonical_bytes)

    @property
    def timing_configured(self):
        return self.capture_timing is not None and all(getattr(self.capture_timing,f.name) is not None for f in fields(CaptureTiming))

    @property
    def localization_configured(self):
        """3개 로컬 bound의 명시 여부다. 실제 localization/world/scene 자격은 별도다."""
        return self.localization is not None

    @property
    def motion_qualified(self):
        """원 bool은 canonical에 남고, 미정 취득 시각은 effective 자격을 열지 않는다."""
        return self.declared_motion_qualified and self.timing_configured

    def check(self,calibration,*,vehicle_configuration,vehicle_calibration):
        """로컬 profile SHA와 차량 bundle/config ID를 각각 대조한다. 센서 승인은 아니다."""
        if type(calibration) is not CalibrationProfile: raise MapperProfileError('actual calibration profile required')
        _label(vehicle_configuration); _sha(vehicle_calibration)
        if (vehicle_configuration!=self.vehicle_configuration or calibration.sha256!=self.calibration_profile_sha256
                or calibration.vehicle_bundle_sha256!=vehicle_calibration):
            raise MapperProfileError('local calibration SHA or original vehicle configuration/bundle differs')
        external=calibration.leaf_hashes['external']
        for name,leaf in self.sensors.tof_sources:
            if external.get(name)!=leaf.sha256: raise MapperProfileError('ToF leaf differs from approved vehicle bundle inventory')
