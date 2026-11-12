"""PC 관측 상주 실행의 명시 설치 값이다. 비밀·연결·준비 권한은 발급하지 않는다.

기존 physical profile을 재해석하지 않는다. 원 motion/calibration/mapper/JIO 관계와
PC clock 가정만 묶으며 실제 NN asset/start, 등록된 worker와 D 사건은 runtime가 검산한다.
tools 타입은 import하지 않고 원 clock 필드를 보존해 실행 assembly가 대조한다.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from uuid import UUID

from ..simulation.motion_profile import DevelopmentMotionProfile
from ..transport.jio_profile import JetsonJioProfile
from ..yolop_profile import YolopBenchProfile
from .mapper_profile import MapperProfile
from .slot_cleanup import CleanupTimeLimits
from .slot_messages import SlotTimeLimits


MAX_INSTALLATION_BYTES = 16 * 1024


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError('canonical nonzero PC process/source UUID required')


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError('explicit bounded PC runtime integer required')


@dataclass(frozen=True, slots=True)
class PcResidentProfile:
    """한 연결 세션의 명시 값이다. caller는 실제 connected endpoint를 별도로 넘긴다.

    source/session/stream UUID를 새로 발급하지 않는다. S service/J clock만 실제 S birth에서
    정해 descriptor에 결박한다. JIO credentials는 fields/artifact에 포함하지 않는다.
    """

    oak: YolopBenchProfile
    motion: DevelopmentMotionProfile
    mapper: MapperProfile
    jio: JetsonJioProfile
    source_id: str
    pc_clock_epoch: str
    jetson_session: str
    rgb_stream: str
    depth_stream: str
    capture_clock_fields: tuple
    probe_interval_us: int
    observation_duration_s: float
    slot_limits: SlotTimeLimits
    cleanup_limits: CleanupTimeLimits

    def __post_init__(self):
        if (type(self.oak) is not YolopBenchProfile or type(self.motion) is not DevelopmentMotionProfile
                or type(self.mapper) is not MapperProfile or type(self.jio) is not JetsonJioProfile
                or type(self.slot_limits) is not SlotTimeLimits
                or type(self.cleanup_limits) is not CleanupTimeLimits):
            raise ValueError('exact original PC installation profiles required')
        for value in (self.source_id, self.pc_clock_epoch, self.jetson_session, self.rgb_stream, self.depth_stream):
            _uuid(value)
        if self.rgb_stream == self.depth_stream:
            raise ValueError('original RGB/RIGHT stream identities must differ')
        if (self.mapper.survey is not None or self.mapper.parking_aisles
                or self.mapper.rolling_roi is not None):
            raise ValueError('PC cold ordinary FOLLOW supports no Scene/parking/rolling restore')
        calibration = self.motion.bound_calibration
        self.jio.source_policy.check_profile(calibration)
        identity = self.motion.fields['identity']
        if ((self.jio.source_policy.vehicle, self.jio.source_policy.configuration,
                self.jio.source_policy.calibration) !=
                (identity['vehicle'], identity['configuration'], self.motion.sha256)
                or self.jio.clock_policy != self.motion.clock_policy(
                    device=self.jio.device, primitive=self.jio.source_policy.primitive)):
            raise ValueError('original PC motion/source/Pi clock policies differ')
        self.mapper.check(calibration, vehicle_configuration=identity['configuration'],
            vehicle_calibration=self.motion.sha256)
        if self.jio.tof_profiles:
            raise ValueError('PC ordinary FOLLOW does not install ToF or Scene sensors')
        fields = self.capture_clock_fields
        names = ('max_rtt_us', 'max_offset_width_us', 'drift_ppm', 'lease_us', 'lookback_us')
        if (type(fields) is not tuple or len(fields) != len(names)
                or any(type(row) is not tuple or len(row) != 2 for row in fields)
                or tuple(row[0] for row in fields) != names):
            raise ValueError('ordered exact original PC capture clock fields required')
        values = dict(fields)
        for name, low, high in (('max_rtt_us', 1, 200000), ('max_offset_width_us', 1, 2**63-1),
                ('drift_ppm', 0, 999999), ('lease_us', 1, 1000000), ('lookback_us', 0, 500000)):
            _integer(values[name], low, high)
        _integer(self.probe_interval_us, 1, values['lease_us']-1)
        if (type(self.observation_duration_s) not in (int, float)
                or not math.isfinite(self.observation_duration_s)
                or not 0.1 <= self.observation_duration_s <= 300):
            raise ValueError('explicit bounded PC observation session duration required')
        if len(self.canonical_bytes) > MAX_INSTALLATION_BYTES:
            raise ValueError('PC installation declaration exceeds16KiB')

    @property
    def fields(self):
        """비밀 없는 작은 독립 사본이다. 실제 broker/SDK/clock 승인은 포함하지 않는다."""
        return dict(profile='pc-resident-installation-1', input_kind='SYNTHETIC',
            source_id=self.source_id, pc_clock_epoch=self.pc_clock_epoch,
            jetson_session=self.jetson_session, stream_instances=dict(rgb_video=self.rgb_stream,
                depth_right=self.depth_stream), oak=self.oak.to_dict(),
            motion_profile_sha256=self.motion.sha256, mapper_profile_sha256=self.mapper.sha256,
            calibration_profile_sha256=self.motion.bound_calibration.sha256,
            source_policy=asdict(self.jio.source_policy), pi_clock_policy=asdict(self.jio.clock_policy),
            capture_clock_policy=dict(self.capture_clock_fields), probe_interval_us=self.probe_interval_us,
            observation_duration_s=self.observation_duration_s, slot_limits=asdict(self.slot_limits),
            cleanup_limits=asdict(self.cleanup_limits),
            physical_admission=False, complete_restore_supported=False, scene_supported=False)

    @property
    def canonical_bytes(self):
        raw = json.dumps(self.fields, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
            allow_nan=False).encode('ascii')
        if len(raw) > MAX_INSTALLATION_BYTES:
            raise ValueError('PC installation declaration exceeds16KiB')
        return raw

    @property
    def sha256(self):
        return sha256(self.canonical_bytes).hexdigest()
