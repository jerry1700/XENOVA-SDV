"""Jetson 개발 M의 SYNTHETIC same-core factory다. C/Gateway 대체 실행 경로가 아니다.

원 encoder/returned-clock snapshot만 pose로 쓰고 GT는 받지 않는다. caller 소유 SQLite
store의 실제 예약/COMMIT은 쓰되 Device/actor/thread/DB connection을 만들지 않는다.
store/connection도 이 compute owner의 caller가 소유한다. 실제 RPi Xen/Gateway 소비자는
여기서 설치하거나 연결하지 않으며 그 연결/운영 수락은 별도다.
새 development-frame-evidence-1은 J endpoint raw key를 명시한 local proof이며 physical
RAW/Scene/복원 권한이 아니다. World 후보도 현재 Gateway 수락/송신 완료를 증명하지 않는다.
PC variant는 pc-frame-evidence-1에 원 descriptor·PC/J clock proof를 보존한다. caller의
실제 최신 current view가 필요하며 source/S actor/READY 전체 연결을 대신하지 않는다.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import math
import struct
import threading
from uuid import UUID, uuid4

from ..mapping.lane_catalog import LaneCatalog, LaneSource
from ..mapping.odometry import PoseEstimate
from ..mapping.road_basis import RoadBasis
from ..perception.inputs import PerceptionInput
from ..perception.pipeline import PerceptionPipeline
from ..runtime.mapper_profile import MapperProfile
from ..runtime.mapper_sensor_state import SensorFrameSnapshot
from ..runtime.raw_binding import RawBinding
from ..storage.contracts import StorageContext, StorageJob, canonical, json_bytes
from ..storage.records import ContextRecord, EvidenceTable, CheckpointPayload, ProvenanceRecord, PROVENANCE
from ..storage.repository import PersistenceStore
from ..transport.world_adapter import WorldSource, WorldBuildError, build_world, lane_model_from_road, _f32, _f32_up
from .motion_profile import DevelopmentMotionProfile
from .wall_sensor import MAX_CAPTURE_LIFETIME_US


CLOCK_BASIS = 'HOST_MONOTONIC_J_INTERVAL_ENDPOINTS_1'
PC_CLOCK_BASIS = 'PC_MONOTONIC_TO_J_FIXED_LEASE_INTERVALS_1'


class DevelopmentMappingNotReady(ValueError):
    """원 관측/clock/profile/자료/기한이 없어 개발 World를 만들지 못했다."""


def _development_stopping(grid, pose, snapshot, limits, now, *, current_sensors=None):
    """기존 stopping_region의 같은 보수 원식을 명시 개발 domain에만 적용한다."""
    import numpy as np
    from ..mapping.free_schedule import StoppingRegion
    inputs = snapshot.metric_inputs; vehicle = inputs.vehicle; admission = snapshot.sensors
    if current_sensors is not None:
        from ..runtime.pc_current_sensors import PcCurrentSensors
        if type(current_sensors) is not PcCurrentSensors or current_sensors.early_snapshot is not snapshot:
            raise DevelopmentMappingNotReady('ORIGINAL_CURRENT_SENSOR_FACTORY_PROOF_REQUIRED')
        vehicle,admission=current_sensors.vehicle,current_sensors.sensors
    if pose is None or not admission.current_vehicle(vehicle, now): return None
    p = pose.sample; source = admission.context.source
    if (admission.context.domain != 'DEVELOPMENT_SYNTHETIC' or p.origin != 'SYNTHETIC_ODOMETRY'
            or p.clock_id != admission.context.pose_clock_id or p.epoch != source['odom_epoch']
            or limits.max_speed_mps < admission.speed):
        raise DevelopmentMappingNotReady('DEVELOPMENT_STOPPING_SOURCE_UNAVAILABLE')
    age = now - min(vehicle.j_interval[0], pose.captured_us[0])
    seconds = math.nextafter((age + limits.reaction_latency_us + limits.cycle_bound_us + limits.completion_bound_us) / 1e6, math.inf)
    body = snapshot.calibration.body
    radius = math.nextafter(math.hypot(max(body.front, body.rear), body.width / 2), math.inf)
    travel = math.nextafter(limits.max_speed_mps * seconds, math.inf)
    braking = math.nextafter(math.nextafter(limits.max_speed_mps**2, math.inf) /
        math.nextafter(2 * limits.min_braking_mps2, -math.inf), math.inf)
    for pad in (limits.body_error_m, p.position_error_m, travel, braking): radius = math.nextafter(radius + pad, math.inf)
    if not math.isfinite(radius): raise DevelopmentMappingNotReady('UNBOUNDED_STOPPING_REGION')
    bounds = (math.nextafter(p.x-radius, -math.inf), math.nextafter(p.y-radius, -math.inf),
        math.nextafter(p.x+radius, math.inf), math.nextafter(p.y+radius, math.inf))
    spec = grid.spec; ox, oy = spec.origin_xy; r = spec.resolution_m
    rows, cols = np.indices((spec.height, spec.width), dtype=np.float64)
    x0, y0 = ox + cols*r, oy + rows*r
    needed = ((np.nextafter(x0, -np.inf) <= bounds[2]) & (np.nextafter(x0+r, np.inf) >= bounds[0])
        & (np.nextafter(y0, -np.inf) <= bounds[3]) & (np.nextafter(y0+r, np.inf) >= bounds[1]))
    complete = bool(ox <= bounds[0] and oy <= bounds[1] and bounds[2] <= ox+spec.width*r and bounds[3] <= oy+spec.height*r)
    return StoppingRegion(spec, grid.frame_id, snapshot.calibration.sha256, p.clock_id, p.epoch,
        (source['map_epoch'], source['frame_epoch'], source['odom_epoch']), vehicle.source_key, vehicle.wire_sha256,
        now, min(vehicle.valid_until_j_us, now+limits.cycle_bound_us+limits.completion_bound_us),
        limits.cycle_bound_us, limits.completion_bound_us, bounds, radius, complete,
        needed.tobytes(), vehicle, admission, pose)


@dataclass(frozen=True)
class DevelopmentMappingCandidate:
    result: object
    world: object
    reasons: tuple
    frame_provenance: object
    proof_receipt: object
    lane_receipt: object
    snapshot: SensorFrameSnapshot
    _token: object
    _state: tuple


def build_development_world(pipeline,model,basis,snapshot,inputs,*,context,calibration,
                            source_bytes,sequence,now_us,current_sensors=None,pc_owner_token=None):
    """원 SYNTHETIC 계산/센서·카탈로그로 ordinary World 후보만 만든다.

    Store/actor/clock/ID를 소유하지 않는다. source는 원 admitted snapshot과 같아야 하며
    최종 current PC view/source/fence/E 및 sequence 채택은 caller의 기존 lock 책임이다.
    Scene 승인·실제 RPi 수신 또는 주행 사용권을 발급하지 않는다.
    """
    from ..mapping.lane_catalog import LaneModelCandidates
    from ..mapping.road_basis import RoadBasisSnapshot
    from ..perception.calibration import CalibrationProfile
    from ..runtime.mapper_sensor_state import MetricInputs
    from ..storage.contracts import uint
    uint(now_us);uint(sequence,positive=True)
    if (type(pipeline) is not PerceptionPipeline or type(model) is not LaneModelCandidates
            or type(basis) is not RoadBasisSnapshot or basis.status!='READY'
            or type(snapshot) is not SensorFrameSnapshot or type(inputs) is not MetricInputs
            or type(context) is not StorageContext or type(calibration) is not CalibrationProfile
            or calibration.kind!='SYNTHETIC' or snapshot.calibration!=calibration
            or pipeline.calibration!=calibration or type(source_bytes) is not bytes):
        raise ValueError('exact original SYNTHETIC pipeline/catalog/sensor/source values required')
    original=snapshot.metric_inputs;source=json_bytes(source_bytes,2048)
    if (source_bytes!=canonical(source) or source!=original.source or source!=inputs.source
            or original.storage_context!=context or inputs.storage_context!=context
            or inputs.pose is not original.pose or inputs.vehicle is not original.vehicle
            or inputs.clock_lease is not original.clock_lease
            or any(source[k]!=getattr(context,a) for k,a in (
                ('mapper_instance','origin_mapper_instance'),('map_epoch','map_epoch'),
                ('frame_epoch','frame_epoch'),('odom_epoch','odom_epoch'),('configuration','configuration'),
                ('calibration','calibration'),('primitive','primitive_sha256')))):
        raise ValueError('development World source differs from original admitted snapshot/context')
    snapshot.validate_frame(job_id=snapshot.job_id,source_generation=snapshot.source_generation,
        source_key=snapshot.source_key,expires_us=snapshot.expires_us,now_us=now_us,
        context_bytes=snapshot.source_context_bytes)
    if (inputs.pose is None or inputs.vehicle is None or inputs.clock_lease is None
            or snapshot.sensors is None or snapshot.sensors.context.domain!='DEVELOPMENT_SYNTHETIC'
            or inputs.pose.sample.origin!='SYNTHETIC_ODOMETRY'):
        raise DevelopmentMappingNotReady('CURRENT_VEHICLE_OR_CLOCK_UNAVAILABLE')
    snapshot.original_vehicle_admission(inputs.vehicle)
    vehicle,lease,sensors=inputs.vehicle,inputs.clock_lease,snapshot.sensors
    if current_sensors is not None:
        from ..runtime.pc_current_sensors import PcCurrentSensors
        if type(current_sensors) is not PcCurrentSensors:raise ValueError('actual current sensor factory proof required')
        current_sensors.validate_current(snapshot,owner_token=pc_owner_token,fence_revision=snapshot.fence_revision,now_us=now_us)
        if current_sensors.storage_context!=context or current_sensors.source_bytes!=source_bytes:
            raise DevelopmentMappingNotReady('CURRENT_SENSOR_SOURCE_CONTEXT_CHANGED')
        vehicle,lease,sensors=current_sensors.vehicle,current_sensors.clock_lease,current_sensors.sensors
    from xenova_drive_world import schema
    from ..runtime.mapper_world import _domain, _heading_bound, _preferred, _NotReady
    now = now_us; alignment = pipeline._alignment
    if alignment is None or not alignment.has_metadata or not alignment.valid_at(now): raise DevelopmentMappingNotReady('LOCALIZATION_EXPIRED')
    lane_source=model.source
    if (lane_source.input_kind!='SYNTHETIC' or not lane_source.qualified
            or lane_source.stamp.key!=snapshot.source_key or lane_source.calibration_id!=calibration.sha256
            or lane_source.map_epoch!=context.map_epoch or lane_source.odom_epoch!=context.odom_epoch
            or lane_source.frame_id!=pipeline.grid.frame_id or lane_source.map_revision!=pipeline.grid.revision
            or lane_source.registration_revision!=alignment.registration_revision
            or basis.lane_ids!=model.lane_ids or basis.latest_model_revision!=model.model_revision
            or basis.latest_lane_source!=lane_source):
        raise DevelopmentMappingNotReady('CURRENT_DEVELOPMENT_CATALOG_DOMAIN_REQUIRED')
    if not sensors.current_vehicle(vehicle, now): raise DevelopmentMappingNotReady('CURRENT_VEHICLE_EXPIRED')
    lease.j_interval_to_pi((now, now), now_j_us=now)
    view = pipeline.grid.snapshot(now, clock_id=snapshot.capture_stamp.clock_id)
    spec = view['spec']
    try:
        upper, lower, grid_error = _domain(spec, len(basis.points))
        preferred = _preferred(spec, model.road)
    except _NotReady as error:
        raise DevelopmentMappingNotReady('WORLD_DOMAIN_OR_ROAD_NOT_REPRESENTABLE') from error
    mapped = alignment.apply(PoseEstimate(vehicle.pose_sample, vehicle.j_interval, vehicle.j_interval)).sample
    capture = snapshot.capture_stamp.captured_us
    value = schema.WorldSnapshot(major=1, minor=2, required_features=11, sequence=sequence,
        **{k: source[k] for k in ('vehicle', 'mapper_boot', 'mapper_instance', 'clock_epoch', 'map_epoch',
            'frame_epoch', 'odom_epoch', 'configuration', 'calibration', 'primitive')},
        reference_id=basis.reference_id, reference_revision=basis.reference_revision,
        reference_odom_seq=vehicle.anchor.sequence, observation_seq=snapshot.capture_stamp.sequence,
        observed_us=sum(capture)//2, completed_us=now, published_us=now, complete=True, truncated=False, inflation_policy=0)
    value.origin.CopyFrom(schema.Pose(x=_f32(spec.origin_xy[0]), y=_f32(spec.origin_xy[1]), yaw=0.))
    anchor = value.reference_vehicle_pose; anchor.x, anchor.y, anchor.yaw = map(_f32, (mapped.x, mapped.y, mapped.yaw))
    for point in basis.points: value.reference.add(x=point[0], y=point[1])
    for point in upper: value.left_boundary.add(x=point[0], y=point[1])
    for point in lower: value.right_boundary.add(x=point[0], y=point[1])
    value.lane_model.CopyFrom(lane_model_from_road(model.road, lane_ids=model.lane_ids,
        model_revision=model.model_revision, lane_revisions=model.lane_revisions, crossing=model.crossing))
    interval = alignment.observed_j_interval
    try: heading = _heading_bound(basis, model.road)
    except _NotReady as error: raise DevelopmentMappingNotReady('UNBOUNDED_REFERENCE_HEADING') from error
    value.localization.CopyFrom(schema.MapLocalization(registration_revision=alignment.registration_revision,
        anchor_position_bound_m=_f32_up(mapped.position_error_m + math.hypot(anchor.x-mapped.x, anchor.y-mapped.y)),
        anchor_heading_bound_rad=_f32_up(mapped.yaw_error_rad + abs(anchor.yaw-mapped.yaw)),
        geometry_position_bound_m=_f32_up(basis.geometry_error_m + grid_error),
        geometry_heading_bound_rad=_f32_up(heading),
        position_drift_bound_mps=_f32_up(alignment.position_drift_mps), heading_drift_bound_rps=_f32_up(alignment.yaw_drift_rps),
        observed_mapper_us=sum(interval)//2, valid_for_us=alignment.valid_for_us, quality=1,
        timestamp_error_us=max((capture[1]-capture[0]+1)//2, (interval[1]-interval[0]+1)//2)))
    origin = WorldSource.from_calibration('SYNTHETIC', view['frame_id'], context.frame_epoch,
        view['clock_id'], calibration, capture, now, wire_clock_epoch=source['clock_epoch'])
    return build_world(view, value, source=origin, preferred_road=preferred)


class DevelopmentMapping:
    """생성/prepare/accepted/cancelled를 같은 compute owner에서만 호출한다.

    mapper_profile의 qualification/오차는 caller의 명시 개발 정책이다. 누락값을 채우지
    않는다. current source/fence의 마지막 재검산과 실제 publication은 caller 책임이다.
    """
    def __init__(self, motion_profile, mapper_profile, raw_binding, context, store, *, source, clock, crossing=None):
        self._initialize(motion_profile,mapper_profile,raw_binding,context,store,
            source=source,clock=clock,crossing=crossing)

    @classmethod
    def for_pc_source(cls,motion_profile,mapper_profile,pc_binding,context,store,*,source,clock,
                      pc_owner_token,crossing=None):
        """PC source는 별도 descriptor/owner를 명시한다. S 등록이나 READY를 발급하지 않는다."""
        if pc_owner_token is None:raise ValueError('original PC owner token required')
        result=object.__new__(cls)
        result._initialize(motion_profile,mapper_profile,pc_binding,context,store,
            source=source,clock=clock,crossing=crossing,pc_owner_token=pc_owner_token)
        return result

    def _initialize(self,motion_profile,mapper_profile,raw_binding,context,store,*,source,clock,
                    crossing=None,pc_owner_token=None):
        pc=pc_owner_token is not None
        if pc:
            from .pc_input import PcSourceBinding
            if type(raw_binding) is not PcSourceBinding:raise ValueError('exact PC descriptor required')
            raw_binding.check_profile(motion_profile)
        if (type(motion_profile) is not DevelopmentMotionProfile or type(mapper_profile) is not MapperProfile
                or (not pc and type(raw_binding) is not RawBinding) or type(context) is not StorageContext
                or type(store) is not PersistenceStore or not callable(clock)):
            raise ValueError('exact development/profile/raw/context/store and actual J clock required')
        calibration = motion_profile.bound_calibration
        if crossing is not None and type(crossing) is not bytes and not callable(crossing):
            raise ValueError('explicit crossing bytes/policy or unconfigured None required')
        mapper_profile.check(calibration, vehicle_configuration=source['configuration'], vehicle_calibration=source['calibration'])
        binding = json_bytes(context.source_binding, 2048)
        if (calibration.kind != 'SYNTHETIC' or source['calibration'] != motion_profile.sha256
                or raw_binding.fields['calibration_profile_sha256' if pc else 'calibration_bundle'] != calibration.sha256
                or source['clock_epoch'] != raw_binding.fields['destination_clock_epoch' if pc else 'clock_epoch']
                or binding['raw_binding_sha256'] != raw_binding.sha256
                or binding['mapper_worker'] != context.mapper_worker
                or binding['mapper_instance'] != context.origin_mapper_instance
                or context.database_uuid != store.database_uuid
                or any(source[k] != getattr(context, a) for k, a in (
                    ('mapper_instance', 'origin_mapper_instance'), ('map_epoch', 'map_epoch'),
                    ('frame_epoch', 'frame_epoch'), ('odom_epoch', 'odom_epoch'), ('configuration', 'configuration'),
                    ('calibration', 'calibration'), ('primitive', 'primitive_sha256')))):
            raise ValueError('development calibration/source/original DB context differs')
        self.motion_profile, self.profile, self.raw, self.context, self.store = motion_profile, mapper_profile, raw_binding, context, store
        self._pc_binding=raw_binding if pc else None;self._pc_owner_token=pc_owner_token
        self._pc_active=self._staged_pc=None
        self.calibration, self.clock = calibration, clock
        self._crossing = crossing
        self._source = canonical(source); self._owner = threading.get_ident()
        self._state = (PerceptionPipeline(calibration, mapper_profile.pipeline, targets=mapper_profile.targets), None, RoadBasis())
        self._grant = None; self._next = 0; self._world_sequence = 0
        self._range_original = None
        self._proof_head = None
        self._staged = self._retired = self._fault = None

    def _owned(self):
        if threading.get_ident() != self._owner: raise RuntimeError('development mapping belongs to original compute owner')

    @property
    def pipeline(self):
        self._owned(); return self._state[0]

    def _job(self, snapshot):
        now = self.clock()
        if not now < snapshot.expires_us: raise DevelopmentMappingNotReady('ORIGINAL_FRAME_EXPIRED')
        return StorageJob(self.context, str(uuid4()), now, snapshot.expires_us)

    def _validate(self, observation, snapshot, now, *, context_bytes=None, fence_revision=None):
        if (type(observation) is not PerceptionInput or type(snapshot) is not SensorFrameSnapshot
                or observation.kind != 'SYNTHETIC' or observation.calibration_id != self.calibration.sha256
                or snapshot.calibration != self.calibration or observation.rgb.stamp.key != snapshot.source_key
                or observation.capture_interval != snapshot.capture_stamp.captured_us):
            raise DevelopmentMappingNotReady('ORIGINAL_DEVELOPMENT_FRAME_REQUIRED')
        # generic SensorFrameSnapshot은 DevelopmentMotion의 capture 계약을 보장하지 않는다.
        # 같은 최초 producer 규칙을 여기서도 닫아 instantaneous synthetic 우회를 막는다.
        full = observation.capture_interval
        pc=self._pc_binding is not None
        source_id=self.raw.source_id if pc else 'sim-wall:'+self.raw.fields['capture_epoch']
        clock_id=self.raw.clock_id if pc else 'jetson-monotonic:'+self.raw.fields['clock_epoch']
        if (observation.rgb.stamp.source_id != source_id or observation.rgb.stamp.clock_id != clock_id
                or full is None or full[0] >= full[1]
                or type(now) is not int or type(snapshot.expires_us) is not int
                or not 0 <= now < snapshot.expires_us < 2**63
                or not full[1] < snapshot.expires_us <= full[0] + MAX_CAPTURE_LIFETIME_US):
            raise DevelopmentMappingNotReady('ORIGINAL_DEVELOPMENT_CAPTURE_LIFETIME_OR_SOURCE_REQUIRED')
        if pc:
            if (self._pc_active is None or snapshot.pc_frame_proof!=self._pc_active[0].proof_bytes
                    or snapshot.pc_binding_bytes!=self.raw.canonical_bytes
                    or snapshot.expires_us!=self._pc_active[0].expires_j_us
                    or self._pc_active[0].observation is not observation):
                raise DevelopmentMappingNotReady('ORIGINAL_PC_FRAME_PROOF_REQUIRED')
            self._pc_active[0].validate_current(self._pc_active[1],now,
                binding=self._pc_binding,owner_token=self._pc_owner_token)
            if (observation.nn is None or observation.nn.packet.context_key!=self.raw.fields['nn_manifest_sha256']):
                raise DevelopmentMappingNotReady('ORIGINAL_PC_NN_MANIFEST_REQUIRED')
        elif snapshot.pc_frame_proof is not None or snapshot.pc_binding_bytes is not None:
            raise DevelopmentMappingNotReady('PC_PROOF_CANNOT_ENTER_SAME_HOST_MAPPING')
        snapshot.validate_frame(job_id=snapshot.job_id, source_generation=self.raw.sha256,
            source_key=observation.rgb.stamp.key, expires_us=snapshot.expires_us, now_us=now,
            context_bytes=snapshot.source_context_bytes if context_bytes is None else context_bytes,
            fence_revision=snapshot.fence_revision if fence_revision is None else fence_revision)
        inputs = snapshot.metric_inputs
        if (inputs.storage_context != self.context or canonical(inputs.source) != self._source
                or inputs.pose is None or inputs.pose.sample.origin != 'SYNTHETIC_ODOMETRY'
                or inputs.clock_lease is None or inputs.vehicle is None or snapshot.sensors is None
                or snapshot.sensors.context.domain != 'DEVELOPMENT_SYNTHETIC'
                or not snapshot.sensors.current_vehicle(inputs.vehicle, now)):
            raise DevelopmentMappingNotReady('ORIGINAL_SENSOR_POSE_CLOCK_UNAVAILABLE')
        key = observation.nn_key
        if (observation.nn is None or type(key) is not tuple or len(key) != 8
                or key[:4] != observation.rgb.stamp.key or key[-1] != sha256(observation.rgb.payload).hexdigest()
                or key[-2] != observation.nn.packet.context_key
                or key[4:6] != (observation.nn.packet.device_us, observation.nn.packet.sdk_us)
                or observation.nn.packet.sequence != observation.rgb.stamp.sequence
                or not observation.nn.packet.received_j_us <= now):
            raise DevelopmentMappingNotReady('ORIGINAL_HOST_SIM_NN_JOIN_REQUIRED')
        snapshot.original_pose_bracket(inputs.pose)
        snapshot.original_vehicle_admission(inputs.vehicle)
        inputs.clock_lease.pi_interval_to_j((inputs.clock_lease.probe_pi_send_us, inputs.clock_lease.probe_pi_send_us), now)
        return inputs

    def _commit(self, job, payload):
        try:
            receipt = self.store.commit(job, payload, current_context_sha256=self.context.sha256)
            if receipt.status != 'COMMITTED' or not receipt.current(job, payload, self.context.sha256, self.clock()):
                self._fault = (job, receipt)
                raise DevelopmentMappingNotReady('ORIGINAL_STORAGE_NOT_COMMITTED')
            return receipt
        except BaseException as error:
            if self._fault is None: self._fault = (job, type(error).__name__)
            raise

    def _frame_record(self, observation, snapshot, inputs):
        if observation.depth is None or observation.rgb.stamp.captured_us is None or observation.depth.stamp.captured_us is None:
            raise DevelopmentMappingNotReady('FULL_RGB_RIGHT_CAPTURE_REQUIRED')
        fingerprint = sha256(canonical(dict(geometry_key=observation.geometry_key,
            rgb_sha256=sha256(observation.rgb.payload).hexdigest(), right_sha256=sha256(observation.depth.payload).hexdigest(),
            nn_key=observation.nn_key, source_context_sha256=sha256(snapshot.source_context_bytes).hexdigest()))).hexdigest()
        if self._proof_head is not None and self._proof_head[0] == snapshot.job_id:
            if self._proof_head[1] != fingerprint: raise ValueError('same original development job changed its evidence')
            return self._proof_head[2:]
        if self._grant is None:
            job = self._job(snapshot)
            nonce = str(uuid4()); self._range_original = (job, nonce)
            try:
                grant = self.store.reserve_provenance(job, nonce, block_count=256,
                    current_context_sha256=self.context.sha256)
                if grant.result != 'GRANTED' or not grant.current(job, self.context.sha256, self.clock()):
                    raise DevelopmentMappingNotReady('ORIGINAL_PROVENANCE_RANGE_REQUIRED')
                self._grant = grant
            except BaseException:
                self._fault = self._range_original
                raise
            self._next = self._grant.start_inclusive
        if self._next >= self._grant.end_exclusive: raise DevelopmentMappingNotReady('PROVENANCE_EXHAUSTED')
        identity = self._next; self._next += 1  # 취소/만료도 원 durable ID를 환불하지 않는다.
        keys = tuple((self.raw.fields['stream_instances'][name], image.stamp.sequence, endpoint)
            for name, image in (('rgb_video', observation.rgb), ('depth_right', observation.depth))
            for endpoint in image.stamp.captured_us)
        body=dict(profile='development-frame-evidence-1',
            input_kind='SYNTHETIC', clock_basis=CLOCK_BASIS, raw_keys=keys,
            raw_binding_sha256=self.raw.sha256, storage_context_sha256=self.context.sha256,
            source_context_sha256=sha256(snapshot.source_context_bytes).hexdigest(), source=inputs.source,
            frame_job_id=snapshot.job_id, geometry_expires_j_us=str(snapshot.expires_us),
            rgb_sha256=sha256(observation.rgb.payload).hexdigest(), right_sha256=sha256(observation.depth.payload).hexdigest(),
            nn_source_key=observation.nn_key,
            calibration_profile_sha256=self.calibration.sha256,
            original_probe_sha256=sha256(inputs.clock_lease.raw_probe_bytes).hexdigest(),
            original_trust_sha256=sha256(inputs.clock_lease.trust_bytes).hexdigest())
        if self._pc_binding is not None:
            # 새 PC grammar만 중복을 뺀다. raw4keys는 PROVENANCE 원문, local 보정/stream은
            # full descriptor, PC endpoints/payload SHA는 full header에 그대로 남는다.
            body={key:body[key] for key in ('input_kind','clock_basis','storage_context_sha256',
                'source_context_sha256','frame_job_id','geometry_expires_j_us','nn_source_key',
                'original_probe_sha256','original_trust_sha256')}
            body.update(profile='pc-frame-evidence-1',clock_basis=PC_CLOCK_BASIS,
                pc_source_binding=json_bytes(snapshot.pc_binding_bytes,2048),
                pc_frame_proof=json_bytes(snapshot.pc_frame_proof,16384))
        try:context = ContextRecord(canonical(dict(kind='RAW',body=body)))
        except ValueError as error:
            if self._pc_binding is None:raise
            raise DevelopmentMappingNotReady('PC_FRAME_CONTEXT_EXCEEDS_ORIGINAL_REPRESENTATION') from error
        lo, hi = inputs.clock_lease.j_interval_to_pi(observation.capture_interval, now_j_us=self.clock())
        raw_keys = b''.join(struct.pack('<16sQQ', UUID(stream).bytes, seq, at) for stream, seq, at in keys)
        record = ProvenanceRecord(identity, PROVENANCE.pack(bytes.fromhex(context.sha256), 3, 0, 4, 0,
            UUID(self.context.pi_host_boot).bytes, lo, hi, observation.rgb.stamp.sequence,
            0, 0, 0, 0, raw_keys, 0, bytes(16)))
        job = self._job(snapshot); heads = self.store.catalog_heads(job, current_context_sha256=self.context.sha256)
        hwm = tuple(next(value for kind, value, _ in heads.heads if kind == k) for k in ('LANE', 'SLOT'))
        payload = CheckpointPayload.build(job, evidence=EvidenceTable((record,), (context,), catalog_highwaters=hwm),
            durable_cutoff_pi_us=record.pi_upper)
        receipt = self._commit(job, payload)
        self._proof_head = (snapshot.job_id, fingerprint, record, receipt)
        return record, receipt

    def prepare(self, observation, snapshot):
        """원 NN-joined 관측과 snapshot만 계산한다. World 없음도 원 core 결과와 이유를 남긴다."""
        self._owned()
        if self._pc_binding is not None:raise ValueError('PC mapping requires prepare_pc')
        return self._prepare(observation,snapshot)

    def prepare_pc(self,pc_frame,snapshot,*,current_pc_view):
        """실제 mapped PC 원본과 NN join만 소비한다. 원 PC/J proof는 snapshot과 같아야 한다."""
        self._owned()
        from .pc_input import PcFrameInput
        if self._pc_binding is None or type(pc_frame) is not PcFrameInput:
            raise ValueError('explicit PC mapping and original PcFrameInput required')
        pc_frame.validate_original()
        pc_frame.validate_current(current_pc_view,self.clock(),binding=self._pc_binding,owner_token=self._pc_owner_token)
        self._pc_active=(pc_frame,current_pc_view)
        try:result=self._prepare(pc_frame.observation,snapshot)
        finally:self._pc_active=None
        self._staged_pc=(result,pc_frame)
        return result

    def _prepare(self,observation,snapshot):
        self._owned()
        if self._staged is not None: raise RuntimeError('original development candidate has no terminal decision')
        if self._fault is not None: raise DevelopmentMappingNotReady('ORIGINAL_STORAGE_FAILURE')
        self._retired = None
        inputs = self._validate(observation, snapshot, self.clock())
        core, lane, basis = deepcopy(self._state)
        config = self.profile; localization = config.localization
        args = {}
        if localization is not None:
            args = dict(localization_valid_for_us=localization.registration_valid_for_us,
                localization_position_drift_mps=localization.position_drift_mps, localization_yaw_drift_rps=localization.yaw_drift_rps)
        def needed(grid, pose, at_us):
            return _development_stopping(grid, pose, snapshot, config.stopping, at_us)
        result = core.process(observation, pose=inputs.pose, now_us=self.clock(), completed_clock=self.clock,
            motion_qualified=config.motion_qualified, needed_region_factory=needed,
            cycle_bound_us=config.stopping.cycle_bound_us, completion_bound_us=config.stopping.completion_bound_us, **args)
        record = proof_receipt = lane_receipt = world = None; reasons = []
        try:
            self._validate(observation, snapshot, self.clock())
            if result.summary.get('rgb_updated') is not True: raise DevelopmentMappingNotReady('DUPLICATE_RGB')
            record, proof_receipt = self._frame_record(observation, snapshot, inputs)
            grid = core.grid
            if lane is None:
                lane = LaneCatalog(self.context, clock_id=observation.rgb.stamp.clock_id,
                    calibration_id=self.calibration.sha256, frame_id=grid.frame_id)
            alignment = core._alignment
            # 기존 summary의 LIVE-only 자격 bit는 False 그대로 남긴다.
            qualified = (config.motion_qualified and localization is not None and alignment is not None
                and alignment.has_metadata and alignment.valid_at(self.clock())
                and result.summary.get('quarantined') is False and result.summary.get('input_odom_origin') == 'SYNTHETIC_ODOMETRY')
            mapped = inputs.pose
            if qualified:
                if result.registration is not None and result.registration.status == 'BOUNDED':
                    fit = result.registration
                    x,y,yaw = fit.representative
                    mapped = replace(inputs.pose,sample=replace(inputs.pose.sample,x=x,y=y,yaw=yaw,
                        position_error_m=fit.position_error_m,yaw_error_rad=fit.yaw_error_rad))
                else:
                    _, mapped = core._propagate_odometry(inputs.pose)
            source = LaneSource(observation.rgb.stamp, self.calibration.sha256, self.context.map_epoch,
                grid.frame_id, self.context.odom_epoch, grid.revision,
                0 if alignment is None else alignment.registration_revision,
                result.summary.get('geometric_model_qualified') is True and qualified, 'SYNTHETIC')
            road = result.road
            if road is None: raise DevelopmentMappingNotReady('ROAD_UNAVAILABLE')
            if self._crossing is None: raise DevelopmentMappingNotReady('CROSSING_POLICY_UNCONFIGURED')
            crossing = self._crossing(road, observation.rgb.stamp) if callable(self._crossing) else self._crossing
            association = lane.observe(road, source, mapped.prior, prior_qualified=qualified,
                crossing=crossing, profile=config.pipeline.registration,
                position_radius_m=mapped.sample.position_error_m)
            if association.status == 'NEW' and lane.available_ids < 2:
                job = self._job(snapshot); head = self.store.catalog_heads(job, current_context_sha256=self.context.sha256)
                need = lane.allocation_need(association, head, job, reservation_nonce=str(uuid4()), now_us=self.clock())
                hwm = tuple(need.reservation.end_exclusive if k == 'LANE' else
                    next(v for kind, v, _ in head.heads if kind == k) for k in ('LANE', 'SLOT'))
                payload = CheckpointPayload.build(job, evidence=EvidenceTable(reservations=(need.reservation,),
                    catalog_highwaters=hwm), intent='CATALOG_RESERVATION')
                lane_receipt = self._commit(job, payload)
                lane.apply_committed_range(need, job, payload, lane_receipt,
                    current_context_sha256=self.context.sha256, now_us=self.clock())
            model = lane.update(association, provenance_ids=(record.provenance_id, record.provenance_id))
            if model is None: raise DevelopmentMappingNotReady('LANE_' + association.status + ':' + ','.join(association.reasons))
            reference = basis.observe(lane, model, current_stamp=observation.rgb.stamp, resolution_m=grid.spec.resolution_m,
                now_us=self.clock(), max_age_us=snapshot.expires_us - observation.capture_interval[0])
            if reference.status != 'READY': raise DevelopmentMappingNotReady('BASIS_' + reference.status + ':' + str(reference.reason))
            world = self._world(core, result, model, reference, snapshot, inputs)
        except (DevelopmentMappingNotReady, WorldBuildError) as error:
            reasons.append(str(error))
        self._validate(observation, snapshot, self.clock())
        token = object()
        candidate = DevelopmentMappingCandidate(result, world, tuple(reasons), record, proof_receipt,
            lane_receipt, snapshot, token, (core, lane, basis))
        self._staged = candidate
        return candidate

    def _world(self, core, result, model, basis, snapshot, inputs):
        self._world_sequence += 1
        return build_development_world(core,model,basis,snapshot,inputs,context=self.context,
            calibration=self.calibration,source_bytes=self._source,sequence=self._world_sequence,now_us=self.clock())

    def accepted(self, candidate, *, current_source_context_bytes, fence_revision):
        """원 frame E/currentness를 마지막 확인한다. World의 실제 발행/현재 권한은 별도다."""
        self._owned()
        if self._pc_binding is not None:raise ValueError('PC mapping requires accepted_pc and actual current view')
        return self._accepted(candidate,current_source_context_bytes=current_source_context_bytes,
            fence_revision=fence_revision)

    def accepted_pc(self,candidate,*,pc_frame,current_pc_view,current_source_context_bytes,fence_revision):
        """caller가 읽은 실제 current PC owner view로 마지막 source/session/fence/E를 검산한다."""
        self._owned()
        if (self._pc_binding is None or self._staged_pc is None
                or self._staged_pc[0] is not candidate or self._staged_pc[1] is not pc_frame):
            raise ValueError('exact original staged PC frame/candidate required')
        return self._accepted(candidate,current_source_context_bytes=current_source_context_bytes,
            fence_revision=fence_revision,pc=(pc_frame,current_pc_view))

    def _accepted(self,candidate,*,current_source_context_bytes,fence_revision,pc=None):
        self._owned()
        if candidate is not self._staged: raise ValueError('exact original staged development candidate required')
        snapshot = candidate.snapshot
        snapshot.validate_frame(job_id=snapshot.job_id, source_generation=self.raw.sha256,
            source_key=snapshot.source_key, expires_us=snapshot.expires_us, now_us=self.clock(),
            context_bytes=current_source_context_bytes, fence_revision=fence_revision)
        inputs = snapshot.metric_inputs; checked = self.clock()
        snapshot.validate_frame(job_id=snapshot.job_id, source_generation=self.raw.sha256,
            source_key=snapshot.source_key, expires_us=snapshot.expires_us, now_us=checked, fence_revision=fence_revision)
        if not snapshot.sensors.current_vehicle(inputs.vehicle, checked): raise DevelopmentMappingNotReady('CURRENT_VEHICLE_EXPIRED')
        inputs.clock_lease.j_interval_to_pi((checked, checked), now_j_us=checked)
        if pc is not None:
            pc[0].validate_current(pc[1],checked,binding=self._pc_binding,owner_token=self._pc_owner_token)
        self._retired = self._state; self._state = candidate._state; self._staged = None
        self._staged_pc=None

    def cancelled(self, candidate):
        self._owned()
        if candidate is not self._staged: raise ValueError('exact original staged development candidate required')
        self._staged = None
        self._staged_pc=None
