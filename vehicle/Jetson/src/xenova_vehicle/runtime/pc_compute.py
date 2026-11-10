"""PC owned input 전용 carrier와 기존 M scheduler의 명시 type/current hook이다.

physical CaptureSample/MapperInput으로 cast하지 않는다. 실제 PcNnAdmission의 packet과
원 EARLY frame/snapshot을 보존하며 decoder/core는 processor_factory의 compute thread만
호출한다. main은 현재 view/context/fence를 갱신하고 실제 parser/World 송신을 소유한다.
이 port의 예약·완료 metadata 자체는 실제 송신·Source/Scene/주행 승인이 아니다.
"""

from collections import deque
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import threading
from uuid import UUID

from .mapper_compute import (MapperCompute, MapperComputeError, MapperCancelled, MapperEvent,
    MAX_INPUT_BYTES, MAX_CONTEXT_BYTES, PARSER_BUDGET_US, _generation, _uint)
from .mapper_sensor_state import SensorFrameSnapshot, MAX_SENSOR_SNAPSHOT_BYTES
from .pc_frame_admission import PcNnAdmission
from .pc_channel import PACKET_CAP
from ..oak.sim_nn_manifest import SimNnManifest
from ..oak.yolop_frames import NNInputStamp, NNPacket
from ..simulation.pc_input import PcSourceBinding, PcCurrentView
from ..storage.contracts import canonical, json_bytes, decimal


CURRENT_SENSOR_BYTES=16*1024
# 원 initial fact, 계산 단계의 현재 fact, main이 교체한 registry fact의 동시 소유 상한이다.
CURRENT_SENSOR_RESERVE_BYTES=3*CURRENT_SENSOR_BYTES
# EARLY/NN 각각 원 packet+S forward의 raw/body/header와 작은 lease fact를 선예약한다.
# 네 packet의 각 raw/body<=32KiB, header<=2KiB이며 opaque token은 frame bytes를 추가하지 않는다.
ADMISSION_CONTROL_BYTES=10*PACKET_CAP


@dataclass(frozen=True)
class PcCurrentFacts:
    input_token: object
    job_id: str
    current_pc_view: PcCurrentView
    source_context_bytes: bytes
    fence_revision: int
    updated_j_us: int
    current_sensors: object = None


@dataclass(frozen=True,init=False)
class PcMapperInput:
    """이미 view 반환/RELEASE를 마친 실제 M NN admission이다. 큰 원 bytes는 공유한다.

    golden=None인 실제 SDK 경로만 지원한다. diagnostic dict를 묵시 copy/relabel하지
    않는다. snapshot은 metric에 필수이며 parser warmup에는 명시 None이다.
    """
    nn_admission: PcNnAdmission
    purpose: str
    job_id: str
    source_generation: str
    expires_us: int
    parser_cap_us: int | None
    context_bytes: bytes
    source_key: tuple
    nn_sequence: int
    nn_source_key: tuple
    sensor_snapshot: SensorFrameSnapshot | None
    byte_length: int
    _available_us: int
    _capture_lower_us: int
    _token: object
    _initial_current: PcCurrentFacts

    def __init__(self,nn_admission,*,source_generation,current_pc_view,pc_owner_token,now_us,
                 sensor_snapshot=None,current_sensors=None,context_bytes=None,parser_cap_us=None):
        if type(nn_admission) is not PcNnAdmission:raise MapperComputeError('actual original PcNnAdmission required')
        admission=nn_admission;frame=admission.frame;obs=frame.observation;packet=admission.nn_output
        if type(admission.nn_input) is not NNInputStamp or type(packet) is not NNPacket:
            raise MapperComputeError('exact original owned SDK NN records required')
        _uint(now_us);_generation(source_generation)
        if source_generation!=frame.binding.sha256:raise MapperComputeError('PC generation differs from original descriptor')
        frame.validate_original();frame.validate_current(current_pc_view,now_us,binding=frame.binding,owner_token=pc_owner_token)
        if (admission.purpose not in ('PARSER_WARMUP','METRIC_MAP') or obs.nn is not None
                or admission.expires_j_us!=frame.expires_j_us or packet.golden is not None):
            raise MapperComputeError('original PC purpose/E and SDK golden=None path required')
        try:valid_id=type(admission.job_id) is str and len(admission.job_id)==36 and str(UUID(admission.job_id))==admission.job_id
        except ValueError:valid_id=False
        if not valid_id:raise MapperComputeError('canonical original PC job ID required')
        admission.nn_input.validate();packet.validate()
        key=admission.nn_key
        if (type(key) is not tuple or len(key)!=8 or key[:4]!=obs.rgb.stamp.key
                or key[4:6]!=(packet.device_us,packet.sdk_us) or key[6]!=frame.binding.fields['nn_manifest_sha256']
                or key[7]!=frame.proof_fields['packet_header']['rgb']['sha256']
                or (admission.nn_input.sequence,admission.nn_input.device_us,admission.nn_input.sdk_us,admission.nn_input.context_key)
                    !=(packet.sequence,packet.device_us,packet.sdk_us,packet.context_key)
                or packet.sequence!=obs.rgb.stamp.sequence or packet.context_key!=key[6]):
            raise MapperComputeError('original PC full NN key/context/parent differs')
        if parser_cap_us is not None:_uint(parser_cap_us)
        metric=admission.purpose=='METRIC_MAP'
        if metric:
            if (type(sensor_snapshot) is not SensorFrameSnapshot or type(sensor_snapshot.byte_length) is not int
                    or not 0<sensor_snapshot.byte_length<=MAX_SENSOR_SNAPSHOT_BYTES):
                raise MapperComputeError('whole original EARLY sensor snapshot required for metric PC input')
            context=sensor_snapshot.source_context_bytes if context_bytes is None else context_bytes
            sensor_snapshot.validate_frame(job_id=admission.job_id,source_generation=source_generation,
                source_key=obs.rgb.stamp.key,expires_us=frame.expires_j_us,now_us=now_us,context_bytes=context)
            if (sensor_snapshot.pc_frame_proof!=frame.proof_bytes or sensor_snapshot.pc_binding_bytes!=frame.binding.canonical_bytes
                    or sensor_snapshot.capture_stamp.captured_us!=obs.capture_interval):
                raise MapperComputeError('EARLY snapshot differs from original PC NN frame proof/capture')
            revision=sensor_snapshot.fence_revision
            if current_sensors is not None:
                from .pc_current_sensors import PcCurrentSensors
                if (type(current_sensors) is not PcCurrentSensors or type(current_sensors.byte_length) is not int
                        or not 0<current_sensors.byte_length<=CURRENT_SENSOR_BYTES):
                    raise MapperComputeError('bounded actual current sensor factory proof required')
                revision=current_sensors.fence_revision
                current_sensors.validate_current(sensor_snapshot,owner_token=pc_owner_token,fence_revision=revision,now_us=now_us)
        else:
            if sensor_snapshot is not None or current_sensors is not None:
                raise MapperComputeError('parser warmup cannot masquerade as metric sensor input')
            context=b'' if context_bytes is None else context_bytes;revision=0
        if type(context) is not bytes or len(context)>MAX_CONTEXT_BYTES:raise MapperComputeError('bounded original source context bytes required')
        payloads=(obs.rgb.payload,obs.depth.payload,packet.payload)
        if sum(len(p) for p in payloads)>MAX_INPUT_BYTES:raise MapperComputeError('original PC payload exceeds2MiB')
        available=max(frame.frame_received_j_us,admission.nn_input.received_j_us,packet.received_j_us,admission.filled_completed_j_us)
        capture=obs.capture_interval
        if capture is None or not capture[0]<capture[1]<=available<=now_us<frame.expires_j_us:
            raise MapperComputeError('original PC capture/NN availability/E differs')
        token=object();facts=PcCurrentFacts(token,admission.job_id,current_pc_view,context,revision,now_us,current_sensors)
        # source-current 업데이트가 소유할 수 있는 별도 최대64KiB도 입력마다 미리 센다.
        metadata=(frame.proof_bytes,frame.packet_header_bytes,frame.binding.canonical_bytes,*frame.clock_probe_bytes,*frame.clock_reply_bytes)
        size=(sum(len(p) for p in payloads)+sum(len(p) for p in metadata)+len(context)
            +MAX_CONTEXT_BYTES+ADMISSION_CONTROL_BYTES+4096)
        if sensor_snapshot is not None:size+=sensor_snapshot.byte_length+CURRENT_SENSOR_RESERVE_BYTES
        for name,value in dict(nn_admission=admission,purpose=admission.purpose,job_id=admission.job_id,
                source_generation=source_generation,expires_us=frame.expires_j_us,parser_cap_us=parser_cap_us,
                context_bytes=context,source_key=obs.rgb.stamp.key,nn_sequence=packet.sequence,nn_source_key=key,
                sensor_snapshot=sensor_snapshot,byte_length=size,_available_us=available,_capture_lower_us=capture[0],
                _token=token,_initial_current=facts).items():object.__setattr__(self,name,value)

    @property
    def frame(self):return self.nn_admission.frame

    def fresh(self,now_us):
        _uint(now_us)
        return self._available_us<=now_us<self.expires_us and now_us-self._capture_lower_us<=PARSER_BUDGET_US


@dataclass(frozen=True)
class PcParserCompletion:
    job_id: str
    source_generation: str
    generation_serial: int
    source_key: tuple
    nn_source_key: tuple
    completed_us: int
    expires_us: int
    metadata_bytes: bytes

    @property
    def metadata(self):return json.loads(self.metadata_bytes)


class PcMapperCompute(MapperCompute):
    """같은 scheduler/ACK/8MiB/operation 정책을 쓴다. PC 입력은 latest NN1만 차지한다.

    현재 facts는 원 input token과 작은 view/context만 보유해 끝난 frame을 숨은 네 번째
    입력으로 보관하지 않는다. main은 current_inputs()의 원 job만 갱신/철회할 수 있다.
    """

    def __init__(self,processor_factory,*,source_generation,binding,pc_owner_token,manifest,clock,
                 maintenance=None,adoption_lock=None):
        if (type(binding) is not PcSourceBinding or type(manifest) is not SimNnManifest or pc_owner_token is None
                or manifest.sha256!=binding.fields['nn_manifest_sha256'] or source_generation!=binding.sha256):
            raise MapperComputeError('registered PC descriptor/owner/pinned NN manifest required')
        self._pc_binding,self._pc_owner_token,self._pc_manifest=binding,pc_owner_token,manifest
        self._current={};self._parser_completion=None
        super().__init__(processor_factory,source_generation=source_generation,clock=clock,
            maintenance=maintenance,adoption_lock=adoption_lock)

    def _validate_input(self,item):
        if type(item) is not PcMapperInput or item.frame.binding.canonical_bytes!=self._pc_binding.canonical_bytes:
            raise MapperComputeError('exact original PcMapperInput for this registered descriptor required')

    def _held(self):
        values=[v[0] for v in self._pending.values() if v is not None and v[1]==self._serial]
        if self._active is not None and self._active['serial']==self._serial:values.append(self._active['item'])
        return tuple(values)

    def _prune_current(self):
        keys={item._token for item in self._held()}
        self._current={k:v for k,v in self._current.items() if k in keys}

    def current_inputs(self):
        self._main()
        with self._condition:
            self._prune_current();return self._held()

    def _facts(self,item):
        if not any(x is item for x in self._held()):raise MapperCancelled('PC_INPUT_NOT_CURRENT')
        value=self._current.get(item._token,item._initial_current)
        if type(value) is str:raise MapperCancelled(value)
        if type(value) is not PcCurrentFacts or value.input_token is not item._token:raise MapperCancelled('PC_CURRENT_VIEW_UNAVAILABLE')
        return value

    def current_facts(self,item):
        """main/compute의 readonly 참조다. 실제 admission owner를 compute에서 호출하지 않는다."""
        self._validate_input(item)
        with self._condition:return self._facts(item)

    def current_metric_facts(self):
        """유지 단계가 읽을 실제 held metric의 snapshot/facts만 반환한다.

        끝난 입력을 새 current로 복구하지 않는다. 큰 frame bytes나 mutable sensor owner를
        반환하지 않으며, 원 input token/current serial 검사는 같은 condition 안에서 한다.
        """
        if threading.current_thread() is not self._thread:
            raise MapperComputeError('only actual compute reads maintenance metric facts')
        with self._condition:
            result=[]
            for item in self._held():
                if item.purpose!='METRIC_MAP' or item.sensor_snapshot is None:continue
                try:facts=self._facts(item)
                except MapperCancelled:continue
                if facts.current_sensors is not None:result.append((item.sensor_snapshot,facts))
            return tuple(result)

    def update_current(self,item,*,current_pc_view,source_context_bytes,fence_revision,now_us,current_sensors=None):
        self._main();self._validate_input(item);_uint(now_us);_uint(fence_revision)
        if type(source_context_bytes) is not bytes or len(source_context_bytes)>MAX_CONTEXT_BYTES:
            raise MapperComputeError('bounded original current source context required')
        try:
            item.frame.validate_current(current_pc_view,now_us,binding=self._pc_binding,owner_token=self._pc_owner_token)
            if item.sensor_snapshot is not None:
                item.sensor_snapshot.validate_frame(job_id=item.job_id,source_generation=item.source_generation,
                    source_key=item.source_key,expires_us=item.expires_us,now_us=now_us,context_bytes=source_context_bytes)
        except ValueError:
            self.invalidate_current(item,reason='PC_CURRENT_CONTEXT_OR_VIEW_FENCED')
            return False
        if current_sensors is not None:
            from .pc_current_sensors import PcCurrentSensors
            if (item.sensor_snapshot is None or type(current_sensors) is not PcCurrentSensors
                    or type(current_sensors.byte_length) is not int or not 0<current_sensors.byte_length<=CURRENT_SENSOR_BYTES):
                raise MapperComputeError('bounded actual current sensor factory proof required')
            try:current_sensors.validate_current(item.sensor_snapshot,owner_token=self._pc_owner_token,
                fence_revision=fence_revision,now_us=now_us)
            except ValueError:current_sensors=None  # parser 사실과 현재 geometry 자격은 별개다.
        facts=PcCurrentFacts(item._token,item.job_id,current_pc_view,source_context_bytes,fence_revision,now_us,current_sensors)
        with self._condition:
            self._prune_current()
            if not any(x is item for x in self._held()):return False
            checked=self._now()
            try:item.frame.validate_current(current_pc_view,checked,binding=self._pc_binding,owner_token=self._pc_owner_token)
            except ValueError:
                self._current[item._token]='PC_CURRENT_VIEW_FENCED_OR_EXPIRED';self._condition.notify_all();return False
            self._current[item._token]=facts;self._condition.notify_all();return True

    def invalidate_current(self,item,*,reason='PC_CURRENT_VIEW_UNAVAILABLE'):
        self._main();self._validate_input(item)
        if type(reason) is not str or not 1<=len(reason)<=64 or not reason.isascii():raise MapperComputeError('bounded PC cancellation reason required')
        with self._condition:
            self._prune_current()
            held=any(x is item for x in self._held())
            result=self._result
            pending_result=result is not None and result.job_id==item.job_id and result.generation_serial==self._serial
            if not held and not pending_result:return False
            if held:self._current[item._token]=reason
            if pending_result:self._result=None
            self._events=deque(replace(event,accepted=False,reason=reason)
                if event.kind=='GEOMETRY_COMPLETED' and event.job_id==item.job_id else event for event in self._events)
            self._counts['pc_input_fenced']+=1;self._condition.notify_all();return True

    def _check_current_input(self,active,now):
        item=active['item'];facts=self._facts(item)
        try:item.frame.validate_current(facts.current_pc_view,now,binding=self._pc_binding,owner_token=self._pc_owner_token)
        except ValueError as error:raise MapperCancelled('PC_CURRENT_VIEW_FENCED_OR_EXPIRED') from error
        if active.get('metric_phase') and item.purpose=='METRIC_MAP':
            snapshot=item.sensor_snapshot
            if facts.fence_revision!=snapshot.fence_revision:raise MapperCancelled('ADMISSION_FENCE')
            current=facts.current_sensors
            if current is None:raise MapperCancelled('CURRENT_SENSORS_UNAVAILABLE')
            try:current.validate_current(snapshot,owner_token=self._pc_owner_token,fence_revision=facts.fence_revision,now_us=now)
            except ValueError as error:raise MapperCancelled('CURRENT_SENSORS_EXPIRED_OR_FENCED') from error

    def record_parser_completion(self,item,metadata):
        """compute 실제 decode 뒤에만 호출한다. latest1 원 metadata이며 송신 성공은 아니다."""
        if threading.current_thread() is not self._thread:raise MapperComputeError('only actual compute records parser completion')
        self._validate_input(item)
        from .pc_job_messages import PARSE_FIELDS, parser_manifest_sha256
        if type(metadata) is not dict or set(metadata)!=PARSE_FIELDS:raise MapperComputeError('exact original parser completion metadata required')
        raw=canonical(metadata)
        if len(raw)>2048:raise MapperComputeError('bounded parser completion metadata required')
        completed=decimal(metadata['completed_j_us']);admission=item.nn_admission
        if (metadata['profile']!='pc-parser-complete-1' or metadata['purpose']!=item.purpose
                or tuple(metadata['full_key'])!=item.source_key or tuple(metadata['nn_key'])!=item.nn_source_key
                or metadata['pc_frame_proof_sha256']!=sha256(item.frame.proof_bytes).hexdigest()
                or metadata['slot_descriptor_sha256']!=admission.slot_descriptor_sha256
                or metadata['nn_manifest_sha256']!=self._pc_manifest.sha256
                or metadata['parser_manifest_sha256']!=parser_manifest_sha256(self._pc_manifest)):
            raise MapperComputeError('parser completion changed original NN admission/manifest/key')
        with self._condition:
            active=self._active;now=self._now()
            if (active is None or active['item'] is not item or not active.get('parsing')
                    or self._stopping or active['serial']!=self._serial):raise MapperCancelled('PARSER_SOURCE_FENCED')
            self._check_current_input(active,now)
            if not active['decode_started']<=completed<=now<min(item.expires_us,active['parser_deadline']):
                raise MapperCancelled('PARSER_COMPLETION_EXPIRED')
            self._counts['parser_completion_replaced']+=int(self._parser_completion is not None)
            self._parser_completion=PcParserCompletion(item.job_id,item.source_generation,active['serial'],
                item.source_key,item.nn_source_key,completed,item.expires_us,raw)

    def take_parser_completion(self,event):
        """같은 accepted NN_COMPLETED 사건에만 대응한다. 만료/교체 사실을 새 E로 보내지 않는다."""
        self._main()
        if type(event) is not MapperEvent or event.kind!='NN_COMPLETED':raise MapperComputeError('actual NN_COMPLETED event required')
        with self._condition:
            value=self._parser_completion
            if value is None or value.job_id!=event.job_id:return None
            self._parser_completion=None
            if (not event.accepted or self._stopping or value.generation_serial!=self._serial
                    or value.generation_serial!=event.generation_serial or value.source_key!=event.source_key
                    or value.nn_source_key!=event.nn_source_key or value.expires_us!=event.expires_us
                    or not value.completed_us<=event.completed_us<=self._now()
                    or self._now()>=value.expires_us):
                self._counts['parser_completion_dropped']+=1;return None
            return value

    def replace_source(self,source_generation):
        super().replace_source(source_generation)
        with self._condition:self._current.clear();self._parser_completion=None

    def request_stop(self):
        super().request_stop()
        with self._condition:self._current.clear();self._parser_completion=None
