"""PC EARLY capture와 현재 M-main sensor heads를 별도로 보존한다.

과거 pose/bracket/ClockLease는 EARLY snapshot 안에 그대로 둔다. 이 factory는
새 Vehicle이나 clock을 수락하지 않고 실제 main owner가 이미 수락한 현재 원본만
고정한다. compute는 최신 registry의 원 DTO와 token/fence를 함께 검산해야 한다.
불변 DTO 자체는 이후 EOF/source fence를 감지하거나 현재 사용권을 발급하지 않는다.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256

from xenova_drive_world import decode, schema
from xenova_drive_world.mapper_clock import ClockLease

from .mapper_sensor_state import (MapperSensorState, SensorFrameSnapshot, VehicleAdmission,
    _context_semantics, _row)
from ..simulation.pc_input import PcSourceBinding
from ..storage.contracts import StorageContext, canonical, json_bytes
from ..transport.sensor_samples import FrozenSensorSamples, SensorSamples, VehicleEvidence


MAX_PC_CURRENT_SENSOR_BYTES=16*1024
_FACTORY=object()


class PcCurrentSensorsNotReady(ValueError):
    """현재 원 sensor heads가 없거나 원 frame/source/fence와 일치하지 않는다."""


def _uint(value):
    if type(value) is not int or not 0<=value<2**63:
        raise PcCurrentSensorsNotReady('exact bounded current sensor integer required')
    return value


def _lease_window(lease,source,pi_host_boot,now):
    if (type(lease) is not ClockLease or lease.source!=source or lease.pi_host_boot!=pi_host_boot):
        raise PcCurrentSensorsNotReady('current R1 differs from original source/origin')
    # 원 trust bounds는 승인 window 전체 drift를 포함한다. 현재 offset을 추정하지 않는다.
    lease.pi_interval_to_j((lease.probe_pi_send_us,lease.probe_pi_send_us),now)
    trust=decode(lease.trust_bytes,schema.WorldTrust)
    return max(0,lease.probe_pi_send_us-trust.upper_offset_us),lease.expires_pi_us-trust.upper_offset_us


@dataclass(frozen=True,slots=True,init=False)
class PcCurrentSensors:
    """원 EARLY identity와 현재 Vehicle/R1을 연결한 추가 최대16KiB proof다.

    byte_length는 EARLY proof에 이미 예약된 calibration/snapshot을 제외한 current-head
    closure의 unique bytes와 scalar metadata 크기다. RGB/RIGHT나 mutable owner는 보유하지
    않는다. source_context 전체64KiB는 caller의 PcCurrentFacts가 별도로 예약한다.
    """
    early_snapshot: SensorFrameSnapshot
    sensors: FrozenSensorSamples
    vehicle: VehicleEvidence
    vehicle_admission: VehicleAdmission
    clock_lease: ClockLease
    storage_context: StorageContext
    source_bytes: bytes
    source_semantics: bytes
    source_semantics_sha256: str
    context_sha256: str
    job_id: str
    source_generation: str
    source_key: tuple
    expires_us: int
    fence_revision: int
    checked_j_us: int
    byte_length: int
    _owner_token: object
    _factory_token: object
    _clock_window: tuple

    def __init__(self,*args,**kwargs):
        raise TypeError('PcCurrentSensors is produced only from the original M-main sensor owner')

    @classmethod
    def from_owner(cls,owner,early_snapshot,*,now_us,source_context_bytes,fence_revision):
        """원 admission만 query한다. ingest나 과거 full-capture pose query는 하지 않는다.

        실제 main이 최신 source context를 ingest한 뒤 호출해야 한다. 정상 probe/Vehicle
        delivery 진전은 허용하되 publication/source/origin/storage 의미 교체는 거절한다.
        """
        if type(owner) is not MapperSensorState or type(early_snapshot) is not SensorFrameSnapshot:
            raise PcCurrentSensorsNotReady('exact M-main owner and original EARLY snapshot required')
        owner._owned();_uint(now_us);_uint(fence_revision)
        early=early_snapshot
        if (type(owner._pc_binding) is not PcSourceBinding or owner._pc_owner_token is None
                or type(owner.sensors) is not SensorSamples or type(early.sensors) is not FrozenSensorSamples
                or early.sensors.context is not owner.sensors.context or early.calibration is not owner.calibration
                or owner.sensors.context.domain!='DEVELOPMENT_SYNTHETIC'
                or early.pc_binding_bytes!=owner._pc_binding.canonical_bytes
                or early.pc_frame_proof is None or early.source_generation!=owner._pc_binding.sha256
                or type(early.source_key) is not tuple or len(early.source_key)!=4):
            raise PcCurrentSensorsNotReady('original PC sensor context/calibration/descriptor required')
        if (type(source_context_bytes) is not bytes or not source_context_bytes
                or source_context_bytes!=owner._source_context_bytes
                or owner._last_ingest_us is None or now_us<owner._last_ingest_us):
            raise PcCurrentSensorsNotReady('actual last ingested current source context required')
        early.validate_frame(job_id=early.job_id,source_generation=early.source_generation,
            source_key=early.source_key,expires_us=early.expires_us,now_us=now_us,
            context_bytes=source_context_bytes,fence_revision=fence_revision)
        # capture를 넘기면 과거 bracket을 다시 query하므로 현재 heads만 읽는다.
        inputs=owner._query(None,now_us)
        if (owner.fence_revision!=fence_revision or inputs.pose is not None
                or type(inputs.storage_context) is not StorageContext
                or inputs.storage_context!=early.metric_inputs.storage_context
                or type(inputs.vehicle) is not VehicleEvidence or type(inputs.clock_lease) is not ClockLease):
            raise PcCurrentSensorsNotReady('current original Vehicle/R1 unavailable or sensor fence changed')
        frozen=owner.sensors.freeze();vehicle=inputs.vehicle
        record=owner.original_vehicle_admission(vehicle)
        if (type(record) is not VehicleAdmission or owner.validate_vehicle_admission(record) is not vehicle
                or not 0<=record.received_j_us<=record.admitted_j_us<=now_us
                or frozen.latest_vehicle is not vehicle or not frozen.current_vehicle(vehicle,now_us)):
            raise PcCurrentSensorsNotReady('actual current Vehicle admission identity/age required')
        source=canonical(inputs.source)
        if source!=frozen.context._source or source!=owner._source or source!=canonical(early.metric_inputs.source):
            raise PcCurrentSensorsNotReady('current Vehicle/R1 source differs from original EARLY source')
        window=_lease_window(inputs.clock_lease,inputs.source,frozen.context.pi_host_boot,now_us)
        _lease_window(vehicle.clock_lease,inputs.source,frozen.context.pi_host_boot,now_us)
        value=json_bytes(source_context_bytes,65536)
        key=sha256(inputs.clock_lease.raw_probe_bytes).hexdigest()
        if value['active_probe_sha256']!=key or key not in value['probes']:
            raise PcCurrentSensorsNotReady('current R1 is not the original active source row')
        raw,received,probe_key=_row(value['probes'][key])
        if raw!=inputs.clock_lease.raw_probe_bytes or received>now_us or probe_key not in (None,key):
            raise PcCurrentSensorsNotReady('current R1 original source row differs')
        semantics=_context_semantics(source_context_bytes);context=inputs.storage_context
        meta=canonical(dict(job_id=early.job_id,source_generation=early.source_generation,
            source_key=early.source_key,expires_us=early.expires_us,fence_revision=fence_revision,
            checked_j_us=now_us,context_sha256=context.sha256,clock_window=window,
            admitted_j_us=record.admitted_j_us,received_j_us=record.received_j_us,
            vehicle=dict(wire_sha256=vehicle.wire_sha256,pi_interval=vehicle.pi_interval,
                j_interval=vehicle.j_interval,valid_until_j_us=vehicle.valid_until_j_us,
                pose=asdict(vehicle.pose_sample)),vehicle_age=frozen.vehicle_age,tof_age=frozen.tof_age,
            speed=frozen.speed,yaw_rate=frozen.yaw_rate,sensor_fence_revision=frozen.fence_revision))
        parts={source,semantics,frozen.context._source,meta,vehicle.wire}
        leases=[inputs.clock_lease,vehicle.clock_lease]
        for _,evidence in frozen._tofs:
            if evidence is not None:
                parts.add(evidence.wire);leases.append(evidence.clock_lease)
                parts.add(canonical(dict(pi_interval=evidence.pi_interval,j_interval=evidence.j_interval,
                    valid_until_j_us=evidence.valid_until_j_us,stamp=asdict(evidence.stamp))))
        parts.update(canonical(leaf.to_dict()) for _,leaf in frozen._leaves)
        parts.update(raw for lease in leases for raw in (lease.raw_probe_bytes,lease.trust_bytes))
        parts.update(canonical({name:getattr(lease,name) for name in lease.__dataclass_fields__
            if name not in ('raw_probe_bytes','trust_bytes')}) for lease in leases)
        size=sum(map(len,parts))
        if size>MAX_PC_CURRENT_SENSOR_BYTES:
            raise PcCurrentSensorsNotReady('whole current sensor proof exceeds16KiB; no proof omitted')
        result=object.__new__(cls)
        fields=dict(early_snapshot=early,sensors=frozen,vehicle=vehicle,vehicle_admission=record,
            clock_lease=inputs.clock_lease,storage_context=context,source_bytes=source,
            source_semantics=semantics,source_semantics_sha256=sha256(semantics).hexdigest(),
            context_sha256=context.sha256,job_id=early.job_id,source_generation=early.source_generation,
            source_key=early.source_key,expires_us=early.expires_us,fence_revision=fence_revision,
            checked_j_us=now_us,byte_length=size,_owner_token=owner._pc_owner_token,
            _factory_token=_FACTORY,_clock_window=window)
        for name,value in fields.items():object.__setattr__(result,name,value)
        result.validate_current(early,owner_token=owner._pc_owner_token,fence_revision=fence_revision,now_us=now_us)
        return result

    @property
    def source(self):return json_bytes(self.source_bytes,65536)

    def validate_current(self,early_snapshot,*,owner_token,fence_revision,now_us):
        """최신 main registry 원 ref와 함께 사용한다. JSON/hash/과거 capture 재환산은 없다."""
        _uint(now_us);_uint(fence_revision)
        if (type(self) is not PcCurrentSensors or self._factory_token is not _FACTORY
                or self._owner_token is not owner_token or owner_token is None
                or early_snapshot is not self.early_snapshot
                or (early_snapshot.job_id,early_snapshot.source_generation,early_snapshot.source_key,early_snapshot.expires_us)
                    !=(self.job_id,self.source_generation,self.source_key,self.expires_us)
                or fence_revision!=self.fence_revision or early_snapshot.fence_revision!=fence_revision
                or not self.checked_j_us<=now_us or not now_us+1<self.expires_us
                or not self._clock_window[0]<=now_us<self._clock_window[1]
                or not self.sensors.current_vehicle(self.vehicle,now_us)):
            raise PcCurrentSensorsNotReady('current sensor proof foreign, fenced, stale or expired')
        return self

    def __copy__(self):return self

    def __deepcopy__(self,memo):return self
