"""PC 개발 M compute의 실제 pipeline·비동기 D catalog·ordinary World 후보다.

원 EARLY SensorFrameSnapshot과 같은 RGB/RIGHT의 실제 NN join만 받는다. Device/SQL/
actor/thread/요청 UUID를 만들지 않으며 LIVE/Scene 경계를 넓히지 않는다. World가 없어도
유효한 관측 core는 계속 채택하지만 원 E/source/fence를 잃은 후보는 채택하지 않는다.
등록·parser READY·송신·RPi 주행 수락은 이 모듈의 결과가 아니다.
"""

from copy import deepcopy
from dataclasses import dataclass
from threading import get_ident

from .mapper_catalogs import CatalogNotReady
from .mapper_profile import MapperProfile
from .mapper_sensor_state import SensorFrameSnapshot
from .pc_mapper_storage import PcMapperStorage, PcStorageNotReady
from ..perception.pipeline import PerceptionPipeline
from ..simulation.development_mapping import (DevelopmentMappingNotReady, build_development_world,
    _development_stopping)
from ..simulation.motion_profile import DevelopmentMotionProfile
from ..simulation.pc_input import PcSourceBinding, PcFrameInput
from ..simulation.wall_sensor import MAX_CAPTURE_LIFETIME_US
from ..storage.contracts import StorageContext, canonical, json_bytes, uint
from ..transport.world_adapter import WorldBuildError


class PcCoreNotReady(DevelopmentMappingNotReady):
    """원 current 입력/기한 또는 현재 지원하는 PC compute 범위가 없다."""


@dataclass(frozen=True)
class PcCoreCandidate:
    result: object
    world: object
    reasons: tuple
    frame: PcFrameInput
    snapshot: SensorFrameSnapshot
    storage_draft: object
    _pipeline: PerceptionPipeline
    _token: object
    _world_clock_lease: object = None


class PcMapperCore:
    """생성/prepare/maintenance/terminal은 같은 M compute owner가 직렬 호출한다.

    accepted의 current view/source context/fence는 실제 main owner에서 방금 공급한
    값이어야 한다. Root compute의 독립 source/admission/operation serial 마지막 검사도
    필요하다. immutable view를 새 current 관측의 대용으로 무기한 재사용하지 않는다.
    """

    def __init__(self,motion_profile,mapper_profile,binding,context,storage,*,source_bytes,clock,pc_owner_token):
        if (type(motion_profile) is not DevelopmentMotionProfile or type(mapper_profile) is not MapperProfile
                or type(binding) is not PcSourceBinding or type(context) is not StorageContext
                or type(storage) is not PcMapperStorage or not callable(clock) or pc_owner_token is None):
            raise ValueError('exact original PC profiles/context/storage owner and actual J clock required')
        binding.check_profile(motion_profile)
        mapper_profile.check(motion_profile.bound_calibration,vehicle_configuration=context.configuration,
            vehicle_calibration=context.calibration)
        source=json_bytes(source_bytes,2048);publication=json_bytes(context.source_binding,2048)
        if (canonical(source)!=source_bytes or storage.context!=context
                or storage.binding.canonical_bytes!=binding.canonical_bytes
                or storage.motion_profile.sha256!=motion_profile.sha256 or storage.profile.sha256!=mapper_profile.sha256
                or publication['raw_binding_sha256']!=binding.sha256
                or source['clock_epoch']!=binding.fields['destination_clock_epoch']
                or source['calibration']!=motion_profile.sha256
                or any(source[k]!=getattr(context,a) for k,a in (
                    ('mapper_instance','origin_mapper_instance'),('map_epoch','map_epoch'),('frame_epoch','frame_epoch'),
                    ('odom_epoch','odom_epoch'),('configuration','configuration'),('calibration','calibration'),('primitive','primitive_sha256')))):
            raise ValueError('PC core differs from actual storage/source/descriptor/profile')
        if mapper_profile.rolling_roi is not None:raise PcCoreNotReady('PC_ROLLING_ATLAS_NOT_READY')
        if storage.status in ('WAIT_SOURCE_COMPLETE','PC_COMPLETE_RESTORE_NOT_READY'):
            raise PcCoreNotReady('PC_COMPLETE_RESTORE_NOT_READY')
        self.motion_profile,self.profile,self.binding,self.context,self.storage=motion_profile,mapper_profile,binding,context,storage
        self.calibration,self.clock,self._source_bytes=motion_profile.bound_calibration,clock,source_bytes
        self._pc_owner_token=pc_owner_token;self._thread=get_ident()
        self._pipeline=PerceptionPipeline(self.calibration,mapper_profile.pipeline,targets=mapper_profile.targets)
        self._staged=self._retired=None;self._terminal_token=None;self._world_sequence=0

    def _owner(self):
        if get_ident()!=self._thread:raise RuntimeError('PC core belongs to original M compute owner')

    @property
    def pipeline(self):self._owner();return self._pipeline

    def _inputs(self,frame,snapshot,current_view,now,*,context_bytes=None,fence_revision=None,original=False,current_sensors=None):
        uint(now)
        if type(frame) is not PcFrameInput or type(snapshot) is not SensorFrameSnapshot:
            raise PcCoreNotReady('ORIGINAL_PC_FRAME_AND_EARLY_SNAPSHOT_REQUIRED')
        if original:frame.validate_original()
        frame.validate_current(current_view,now,binding=self.binding,owner_token=self._pc_owner_token)
        obs=frame.observation;full=obs.capture_interval
        if (obs.kind!='SYNTHETIC' or obs.calibration_id!=self.calibration.sha256 or snapshot.calibration!=self.calibration
                or obs.rgb.stamp.source_id!=self.binding.source_id or obs.rgb.stamp.clock_id!=self.binding.clock_id
                or obs.depth is None or full is None or full[0]>=full[1]
                or not full[1]<frame.expires_j_us<=full[0]+MAX_CAPTURE_LIFETIME_US
                or full!=snapshot.capture_stamp.captured_us or snapshot.pc_frame_proof!=frame.proof_bytes
                or snapshot.pc_binding_bytes!=self.binding.canonical_bytes or snapshot.expires_us!=frame.expires_j_us):
            raise PcCoreNotReady('ORIGINAL_PC_CAPTURE_LIFETIME_SOURCE_OR_PROOF_REQUIRED')
        snapshot.validate_frame(job_id=snapshot.job_id,source_generation=self.binding.sha256,
            source_key=obs.rgb.stamp.key,expires_us=frame.expires_j_us,now_us=now,
            context_bytes=snapshot.source_context_bytes if context_bytes is None else context_bytes,
            fence_revision=snapshot.fence_revision if fence_revision is None else fence_revision)
        inputs=snapshot.metric_inputs
        if (inputs.storage_context!=self.context or canonical(inputs.source)!=self._source_bytes
                or inputs.pose is None or inputs.pose.sample.origin!='SYNTHETIC_ODOMETRY'
                or inputs.clock_lease is None or inputs.vehicle is None or snapshot.sensors is None
                or snapshot.sensors.context.domain!='DEVELOPMENT_SYNTHETIC'):
            raise PcCoreNotReady('ORIGINAL_CURRENT_SENSOR_POSE_CLOCK_REQUIRED')
        from .pc_current_sensors import PcCurrentSensors
        if type(current_sensors) is not PcCurrentSensors:raise PcCoreNotReady('CURRENT_SENSOR_FACTORY_PROOF_REQUIRED')
        current_sensors.validate_current(snapshot,owner_token=self._pc_owner_token,
            fence_revision=snapshot.fence_revision if fence_revision is None else fence_revision,now_us=now)
        if current_sensors.storage_context!=self.context or current_sensors.source_bytes!=self._source_bytes:
            raise PcCoreNotReady('CURRENT_SENSOR_SOURCE_CONTEXT_CHANGED')
        if original:
            key=obs.nn_key;packet=None if obs.nn is None else obs.nn.packet
            if (packet is None or packet.context_key!=self.binding.fields['nn_manifest_sha256']
                    or type(key) is not tuple or len(key)!=8 or key[:4]!=obs.rgb.stamp.key
                    or key[4:6]!=(packet.device_us,packet.sdk_us) or key[-2]!=packet.context_key
                    or key[-1]!=frame.proof_fields['packet_header']['rgb']['sha256']
                    or packet.sequence!=obs.rgb.stamp.sequence or packet.received_j_us>now):
                raise PcCoreNotReady('ORIGINAL_HOST_SIM_NN_MANIFEST_AND_FULL_KEY_REQUIRED')
            snapshot.original_pose_bracket(inputs.pose);snapshot.original_vehicle_admission(inputs.vehicle)
        # 캡처의 원 clock 증거는 수락 당시 원 J로만 재검산한다. 현재 lease로 재환산하지 않는다.
        inputs.clock_lease.j_interval_to_pi(obs.capture_interval,now_j_us=snapshot.snapshot_j_us)
        return inputs

    def prepare(self,frame,snapshot,*,current_pc_view,current_sensors=None,checkpoint=None,current_provider=None):
        """실제 core 계산만 수행하며 optional provider는 main의 원 registry 사실만 읽는다.

        provider가 없으면 전달된 current DTO를 엄격히 유지한다. provider는 같은 실제
        input token에 결박돼야 하며 EARLY pose/bracket/capture clock을 바꾸지 않는다.
        """
        self._owner()
        if self._staged is not None:raise RuntimeError('original PC compute candidate is not terminal')
        if checkpoint is not None and not callable(checkpoint):raise ValueError('optional actual compute checkpoint required')
        if current_provider is not None and not callable(current_provider):raise ValueError('optional readonly current provider required')
        provider_token=None
        def refresh(*,original=False):
            nonlocal current_pc_view,current_sensors,provider_token
            args={}
            if current_provider is not None:
                from .pc_compute import PcCurrentFacts
                facts=current_provider()
                if (type(facts) is not PcCurrentFacts or facts.job_id!=snapshot.job_id
                        or facts.input_token is None or provider_token is not None and facts.input_token is not provider_token):
                    raise PcCoreNotReady('CURRENT_PROVIDER_CHANGED_ORIGINAL_INPUT')
                provider_token=facts.input_token
                current_pc_view,current_sensors=facts.current_pc_view,facts.current_sensors
                args=dict(context_bytes=facts.source_context_bytes,fence_revision=facts.fence_revision)
            return self._inputs(frame,snapshot,current_pc_view,self.clock(),original=original,
                current_sensors=current_sensors,**args)
        self._retired=None
        inputs=refresh(original=True)
        core=deepcopy(self._pipeline);config=self.profile;localization=config.localization
        args={} if localization is None else dict(localization_valid_for_us=localization.registration_valid_for_us,
            localization_position_drift_mps=localization.position_drift_mps,localization_yaw_drift_rps=localization.yaw_drift_rps)
        def needed(grid,pose,at_us):
            if current_provider is not None:refresh()
            return _development_stopping(grid,pose,snapshot,config.stopping,at_us,current_sensors=current_sensors)
        draft=None;world=None;world_lease=None;reasons=[]
        try:
            result=core.process(frame.observation,pose=inputs.pose,now_us=self.clock(),completed_clock=self.clock,
                checkpoint=checkpoint,motion_qualified=config.motion_qualified,needed_region_factory=needed,
                cycle_bound_us=config.stopping.cycle_bound_us,completion_bound_us=config.stopping.completion_bound_us,**args)
            refresh();now=self.clock()
            if checkpoint is not None:checkpoint('PC_CATALOG_PREPARE')
            if result.summary.get('rgb_updated') is not True:reasons.append('DUPLICATE_RGB')
            else:
                alignment=core._alignment
                qualified=(config.motion_qualified and localization is not None and alignment is not None
                    and alignment.has_metadata and alignment.valid_at(now)
                    and result.summary.get('quarantined') is False
                    and result.summary.get('input_odom_origin')=='SYNTHETIC_ODOMETRY')
                mapped=alignment.apply(inputs.pose) if qualified else inputs.pose
                spec=core.grid.spec;low=spec.bounds(0,0);high=spec.bounds(spec.width-1,spec.height-1)
                try:
                    if current_provider is not None:refresh()
                    draft=self.storage.prepare_frame(frame,snapshot,result,current_pc_view=current_pc_view,
                        capture_prior=mapped,registration_revision=0 if alignment is None else alignment.registration_revision,
                        prior_qualified=qualified,now_us=self.clock(),roi_bounds=(low[0],low[1],high[2],high[3]),current_sensors=current_sensors)
                    view=draft.catalog.view;model=view.lane;basis=view.road_basis
                    reasons.extend(view.reasons)
                    if model is None:
                        association=draft.catalog.state.lane.current_association
                        reasons.append('LANE_'+view.lane_status)
                        if association is not None:reasons.extend(association.reasons)
                    elif basis is None or basis.status!='READY':
                        reasons.append('ROAD_BASIS_NOT_READY' if basis is None else 'BASIS_'+basis.status+':'+str(basis.reason))
                    else:
                        if current_provider is not None:refresh()
                        self._world_sequence+=1
                        world=build_development_world(core,model,basis,snapshot,inputs,context=self.context,
                            calibration=self.calibration,source_bytes=self._source_bytes,
                            sequence=self._world_sequence,now_us=self.clock(),current_sensors=current_sensors,pc_owner_token=self._pc_owner_token)
                        world_lease=current_sensors.clock_lease
                except (PcStorageNotReady,CatalogNotReady,DevelopmentMappingNotReady,WorldBuildError) as error:
                    reasons.append(str(error))
            refresh()
            candidate=PcCoreCandidate(result,world,tuple(dict.fromkeys(reasons)),frame,snapshot,draft,core,object(),world_lease)
            self._staged=candidate
            return candidate
        except BaseException:
            if draft is not None:self.storage.cancelled(draft)
            raise

    def _original(self,candidate):
        if type(candidate) is not PcCoreCandidate or candidate is not self._staged:
            raise ValueError('exact original staged PC compute candidate required')

    def accepted(self,candidate,*,pc_frame,current_pc_view,current_source_context_bytes,fence_revision,current_sensors=None):
        """최신 caller 사실로 마지막 검산한다. D encode/I/O나 새 ID를 발급하지 않는다."""
        self._owner();self._original(candidate)
        if pc_frame is not candidate.frame:raise ValueError('candidate changed its original PC frame')
        try:
            inputs=self._inputs(pc_frame,candidate.snapshot,current_pc_view,self.clock(),
                context_bytes=current_source_context_bytes,fence_revision=fence_revision,current_sensors=current_sensors)
            # source JSON 검산 뒤 마지막 실제 J를 읽는다. 원 E를 새 값으로 바꾸지 않는다.
            checked=self.clock();snapshot=candidate.snapshot
            snapshot.validate_frame(job_id=snapshot.job_id,source_generation=self.binding.sha256,
                source_key=pc_frame.observation.rgb.stamp.key,expires_us=pc_frame.expires_j_us,
                now_us=checked,fence_revision=fence_revision)
            pc_frame.validate_current(current_pc_view,checked,binding=self.binding,owner_token=self._pc_owner_token)
            current_sensors.validate_current(snapshot,owner_token=self._pc_owner_token,fence_revision=fence_revision,now_us=checked)
        except BaseException:
            self.cancelled(candidate)
            raise
        self._accept_refs(candidate)

    def accepted_at(self,candidate,*,compute,item,checked_j_us):
        """실제 scheduler registry가 검산한 main 사실과 마지막 원 J로 cheap 채택한다."""
        from .pc_compute import PcMapperCompute, PcMapperInput
        self._owner();self._original(candidate);uint(checked_j_us)
        if (type(compute) is not PcMapperCompute or type(item) is not PcMapperInput
                or item.sensor_snapshot is not candidate.snapshot or item.job_id!=candidate.snapshot.job_id
                or candidate.frame.observation.rgb is not item.frame.observation.rgb
                or candidate.frame.observation.depth is not item.frame.observation.depth
                or candidate.frame.observation.nn.packet is not item.nn_admission.nn_output
                or candidate.frame.proof_bytes!=item.frame.proof_bytes):
            raise ValueError('actual scheduler input differs from original PC core candidate')
        facts=compute.current_facts(item);snapshot=candidate.snapshot;inputs=snapshot.metric_inputs
        snapshot.validate_frame(job_id=item.job_id,source_generation=item.source_generation,source_key=item.source_key,
            expires_us=item.expires_us,now_us=checked_j_us,fence_revision=facts.fence_revision)
        candidate.frame.validate_current(facts.current_pc_view,checked_j_us,binding=self.binding,owner_token=self._pc_owner_token)
        if facts.current_sensors is None:raise PcCoreNotReady('CURRENT_SENSOR_FACTORY_PROOF_REQUIRED')
        facts.current_sensors.validate_current(snapshot,owner_token=self._pc_owner_token,
            fence_revision=facts.fence_revision,now_us=checked_j_us)
        self._accept_refs(candidate)

    def _accept_refs(self,candidate):
        if candidate.storage_draft is not None:self.storage.accepted(candidate.storage_draft)
        self._retired=self._pipeline;self._pipeline=candidate._pipeline
        self._terminal_token=candidate._token;self._staged=None

    def cancelled(self,candidate,reason=None):
        """RAM 후보만 닫는다. D-issued ID와 main sensor history를 되돌리지 않는다."""
        self._owner()
        if self._staged is None and type(candidate) is PcCoreCandidate and candidate._token is self._terminal_token:return False
        self._original(candidate)
        if candidate.storage_draft is not None:self.storage.cancelled(candidate.storage_draft)
        self._terminal_token=candidate._token;self._staged=None
        return True

    def maintenance(self,*,now_us,catalog_arguments=None):
        self._owner()
        if self._staged is not None:raise RuntimeError('PC candidate is not terminal')
        self._retired=None
        return self.storage.maintenance(now_us=now_us,catalog_arguments=catalog_arguments)
