"""실제 FD Vehicle·MQTT 4시각·returned trust를 개발용 full-capture pose에 잇는다.

caller가 제공한 frozen B08/common 패키지와 기존 mosquitto만 사용한다. MatchedStop의
원 Vehicle 직렬화 경로이며 DriveLinkProvider.tick, NN, FREE, Authority 또는 실물
오차 수락을 대신하지 않는다. GT pose는 Wall renderer에만 들어간다. main/센서 메서드는
한 owner에서 호출하고 render만 caller의 별도 owner로 분리할 수 있다. 원 E는 유지한다.
RAW 등록은 caller가 제공한 개발 binding의 명시 시험 경계다. A의 실제 RAW BIND/ACK나
Resident D 프로세스가 없으며, D 자료는 독립 _Database가 실제 생성·검산한 EMPTY다.
이 파일을 읽거나 import하는 것만으로 broker·FD·thread·DB를 만들지 않는다.
"""

from base64 import b64decode, b64encode
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from uuid import UUID, uuid4


def _j_us():
    return time.monotonic_ns() // 1000


def _seconds(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('explicit finite bounded diagnostic wait required')
    return value


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError('original caller UUID required')
    return value


def _wire(raw):
    return dict(base64=b64encode(raw).decode('ascii'), bytes=len(raw), sha256=sha256(raw).hexdigest())


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


@dataclass(frozen=True, slots=True)
class ClockExchange:
    """실제 local receive의 원 body와 decoded header 사본이다. 원 header wire는 아니다."""

    decoded_header: tuple
    candidate_bytes: bytes

    @property
    def fields(self):
        # 수신 hot path 밖에서만 원 protobuf를 진단한다. accept의 입력/반환은 바꾸지 않는다.
        from xenova_drive_world import decode, schema
        from xenova_drive_world.mapper_clock import parse_candidate
        probe_raw, reply_raw, p4 = parse_candidate(self.candidate_bytes)
        probe = decode(probe_raw, schema.MapperClockProbe)
        reply = decode(reply_raw, schema.MapperClockReply)
        header = dict(self.decoded_header)
        if int(header['issued_pi_us']) != p4 or not probe.pi_send_us <= p4:
            raise ValueError('actual candidate original local/P4 time differs')
        return dict(decoded_header=header, candidate=_wire(self.candidate_bytes),
            probe=_wire(probe_raw), reply=_wire(reply_raw), pi_p1_us=str(probe.pi_send_us),
            jetson_j2_us=str(reply.j2), jetson_j3_us=str(reply.j3), pi_p4_us=str(p4),
            acceptance='OBSERVED_CANDIDATE_NOT_AN_INDEPENDENT_ACCEPTANCE_RECEIPT')


class _ObservedLink:
    """원 receive return은 그대로 두고 최대4개의 불변 candidate만 보존한다."""

    def __init__(self, link):
        self.original = link
        self.records = ()
        self.omitted = 0

    @property
    def pending(self): return self.original.pending

    @property
    def socket(self): return self.original.socket

    def offer(self, *args): return self.original.offer(*args)

    def flush(self, now): return self.original.flush(now)

    def receive(self, now):
        result = self.original.receive(now)
        for header, body in result:
            if header['kind'] == 'TRUST_CANDIDATE':
                if len(self.records) == 4:
                    self.omitted += 1
                self.records = (self.records + (ClockExchange(tuple(header.items()), body),))[-4:]
        return result


class _IsolatedBroker:
    """기존 executable의 loopback 전용 인스턴스다. 설치·시스템 broker 접근은 없다."""

    def __init__(self, binary, directory, *, startup_timeout_s, cleanup_timeout_s):
        self.binary = Path(binary).resolve(strict=True)
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise ValueError('explicit existing executable mosquitto path required')
        self.directory = Path(directory).absolute()
        self.startup_timeout_s = startup_timeout_s
        self.cleanup_timeout_s = cleanup_timeout_s
        self.process = self.log = None
        self.port = None
        self.binary_sha256 = None

    def start(self, deadline):
        self.directory.mkdir(mode=0o700, exist_ok=False)
        digest = sha256()
        with self.binary.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
        self.binary_sha256 = digest.hexdigest()
        with socket.socket() as endpoint:
            endpoint.bind(('127.0.0.1', 0))
            self.port = endpoint.getsockname()[1]
        config = self.directory / 'mosquitto.conf'
        with config.open('xb') as stream:
            stream.write((f'listener {self.port} 127.0.0.1\nallow_anonymous true\n'
                          'persistence false\nlog_type error\n').encode('ascii'))
        self.log = (self.directory / 'mosquitto.log').open('xb')
        self.process = subprocess.Popen([str(self.binary), '-c', str(config)],
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            close_fds=True, start_new_session=True)
        end = min(deadline, time.monotonic() + self.startup_timeout_s)
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise RuntimeError('isolated existing broker exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.05):
                    return self
            except OSError:
                time.sleep(.005)
        raise TimeoutError('original isolated broker startup deadline')

    def close(self):
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=self.cleanup_timeout_s)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=self.cleanup_timeout_s)
                else:
                    self.process.wait(timeout=0)
        finally:
            if self.log is not None:
                self.log.close()


@dataclass(frozen=True, slots=True)
class MapperMotionObservation:
    """같은 owner가 실제 read한 원 job 자료다. close 뒤에는 역사 진단 값이다."""

    fd_observation: object
    capture: object
    source_snapshot: bytes
    metric_inputs: object
    bracket: object
    returned_probe_bytes: bytes
    trust_bytes: bytes
    exchanges: tuple
    pretrust_rows: tuple
    checked_j_us: int
    clock_counts: tuple
    sensor_snapshot: object

    @property
    def observation(self): return self.capture.observation

    @property
    def pose(self): return self.metric_inputs.pose

    @property
    def context(self): return self.metric_inputs.storage_context

    @property
    def fields(self):
        value = dict(profile='sim-mapper-motion-observation-1',
            scope='ACTUAL_FD_MQTT_RETURNED_TRUST_SYNTHETIC_ODOMETRY_DIAGNOSTIC',
            vehicle_serialization='ACTUAL_MATCHED_STOP_RAW_BOOTSTRAP_NOT_DRIVELINK_TICK',
            raw_registration='CALLER_DECLARED_DEVELOPMENT_BINDING_NOT_A_RAW_ACK',
            storage_bootstrap='ACTUAL_PRIVATE_SQLITE_EMPTY_NOT_RESIDENT_D_IPC',
            fd=self.fd_observation.fields, capture=self.capture.receipt,
            source_snapshot=_wire(self.source_snapshot),
            checked_j_us=str(self.checked_j_us), pose=asdict(self.pose),
            context_sha256=self.context.sha256, original_context=self.context.fields,
            sensor_snapshot=dict(job_id=self.sensor_snapshot.job_id,
                source_generation=self.sensor_snapshot.source_generation,
                source_key=list(self.sensor_snapshot.source_key),
                snapshot_j_us=str(self.sensor_snapshot.snapshot_j_us),
                expires_j_us=str(self.sensor_snapshot.expires_us),
                fence_revision=self.sensor_snapshot.fence_revision,
                proof_bytes=self.sensor_snapshot.byte_length, p4_evidence=self.sensor_snapshot.p4_evidence),
            bracket=[dict(vehicle=_wire(row.evidence.wire), received_j_us=str(row.received_j_us),
                admitted_j_us=str(row.admitted_j_us), pi_interval=list(map(str, row.evidence.pi_interval)),
                j_interval=list(map(str, row.evidence.j_interval))) for row in self.bracket.originals],
            returned_probe=_wire(self.returned_probe_bytes), returned_trust=_wire(self.trust_bytes),
            clock_exchanges=[item.fields for item in self.exchanges], clock_counts=dict(self.clock_counts),
            pretrust_rows=[dict(vehicle=_wire(raw), received_j_us=str(received), clock_probe_sha256=None)
                           for raw, received in self.pretrust_rows],
            reasons=list(self.metric_inputs.reasons), nn_executed=False, free_positive=False,
            authority=False, physical_acceptance=False, closed_loop=False)
        raw = _canonical(value)
        if len(raw) > 256 * 1024:
            raise ValueError('bounded original mapper diagnostic exceeds256KiB')
        return json.loads(raw)


class _RenderOnce:
    """큰 렌더는 lock 밖에서 한 번만 실행한다. 중복은 원 결과만 읽고 시각을 재발급하지 않는다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = self.finished = False
        self.capture = self.failure = None


@dataclass(frozen=True, slots=True)
class WallRenderRequest:
    """원 renderer 작업 하나다. sample.pose/scene은 렌더 전용이며 core 입력이 아니다.

    render()는 caller가 선택한 renderer owner에서 호출할 수 있다. 그동안 원 actor는
    pump()를 계속해야 한다. caller는 renderer를 끝내고 join한 뒤 rig를 닫는다.
    이 carrier가 thread/process나 새 sensor 시각을 만들지는 않는다.
    """

    development_profile: object
    motion_profile: object
    scene: object
    sample: object
    clock_epoch: str
    expires_j_us: int
    deadline_wall: float
    job_id: str
    source_generation: str
    source_key: tuple
    _once: object = field(default_factory=_RenderOnce, init=False, repr=False, compare=False)

    def render(self):
        from xenova_vehicle.simulation.wall_sensor import WallSyntheticCamera
        with self._once.lock:
            if self._once.started:
                if not self._once.finished:
                    raise RuntimeError('original render is already in progress')
                if self._once.failure is not None:
                    raise RuntimeError('original render failed: ' + self._once.failure)
                return self._once.capture
            self._once.started = True
        try:
            camera = WallSyntheticCamera(self.development_profile, self.scene, epoch=self.sample.epoch,
                clock_epoch=self.clock_epoch, motion_profile=self.motion_profile)
            try:
                capture = camera.capture(self.sample, expires_j_us=self.expires_j_us)
            finally:
                camera.close()
        except BaseException as error:
            with self._once.lock:
                self._once.failure = type(error).__name__
                self._once.finished = True
            raise
        with self._once.lock:
            self._once.capture = capture
            self._once.finished = True
        return capture

    def _matches(self, capture):
        with self._once.lock:
            return self._once.finished and self._once.failure is None and self._once.capture is capture


class SimMapperMotionRig:
    """실제 wallclock의 한 사용 세션이다. 자동 재연결·source 재발급·lease 연장은 없다.

    fd_rig는 아직 시작하지 않은 exact SimFdMotionRig다. raw_binding과 source/clock
    정책은 caller가 명시하며 이 진단이 sensor/graph 설치 수락을 만들지 않는다. Linux
    spawn child는 caller가 미리 선택한 같은 frozen platform 패키지를 물려받아야 한다.
    매 pump의 main callback에서 fresh ingest하며 frame query는 재-admit하지 않는다.
    caller는 render 동안에도 pump를 진행해야 한다. history capacity/maxgap/E가 부족하면
    원 bracket 부재로 실패하며 빈/fake frame read로 HWM을 진행하지 않는다.
    observer는 선택 진단 참조 sink다. 이 rig는 파일/drain/close를 호출하지 않으며,
    caller가 독립 diagnostic owner의 stop/join과 bridge close를 책임진다.
    """

    def __init__(self, fd_rig, *, raw_binding, mapper_instance, mapper_boot,
                 source_policy, clock_policy, coherent_platform_root, broker_binary,
                 broker_workdir, startup_timeout_s, poll_interval_s, broker_startup_timeout_s,
                 cleanup_timeout_s, observer=None):
        if observer is not None:
            from sim_observation_bridge import SimObservationBridge
            if type(observer) is not SimObservationBridge:
                raise ValueError('exact opt-in SimObservationBridge required')
            observer.validate_producer()
        self._observer = observer
        from sim_fd_motion_rig import SimFdMotionRig
        import xenova_gateway.mapper_uplink as uplink_module
        import xenova_drive_world.mapper_clock as clock_module
        from xenova_vehicle.runtime.raw_binding import RawBinding
        from xenova_vehicle.runtime.source_identity import SourcePolicy
        if type(fd_rig) is not SimFdMotionRig or fd_rig._started or fd_rig._closed:
            raise ValueError('fresh original FD rig required; no automatic source replacement')
        if fd_rig.clock is not time.monotonic or fd_rig.device_clock is not None:
            raise ValueError('this integration requires original wall monotonic clocks')
        if type(raw_binding) is not RawBinding or type(source_policy) is not SourcePolicy:
            raise ValueError('explicit original RAW binding and source policy required')
        if type(clock_policy) is not clock_module.ClockPolicy:
            raise ValueError('exact coherent common clock policy required')
        _uuid(mapper_instance); _uuid(mapper_boot)
        _seconds(startup_timeout_s, 30); _seconds(poll_interval_s, .01)
        _seconds(broker_startup_timeout_s, 5); _seconds(cleanup_timeout_s, 5)
        if startup_timeout_s < fd_rig.startup_timeout_s:
            raise ValueError('overall startup must include the original FD startup bound')
        model = fd_rig.profile
        source_policy.check_profile(model.bound_calibration)
        identity = model.fields['identity']
        if ((source_policy.vehicle, source_policy.configuration, source_policy.calibration) !=
                (identity['vehicle'], identity['configuration'], model.sha256)
                or raw_binding.fields['calibration_bundle'] != model.bound_calibration.sha256
                or clock_policy != model.clock_policy(device=clock_policy.device, primitive=source_policy.primitive)):
            raise ValueError('original development RAW/model/source/clock policy differs')
        platform_root = Path(coherent_platform_root).resolve(strict=True)
        expected = ((uplink_module, 'platform/services/gateway/xenova_gateway/mapper_uplink.py'),
            (clock_module, 'platform/apps/drive/world/python/xenova_drive_world/mapper_clock.py'),
            (fd_rig.fb, 'platform/services/gateway/xenova_gateway/feedback.py'),
            (fd_rig.f3, 'platform/services/gateway/xenova_gateway/feedback3.py'),
            (fd_rig.sa, 'platform/services/gateway/xenova_gateway/scene_authority.py'))
        self.module_sources = ()
        for module, suffix in expected:
            actual = Path(module.__file__).resolve(strict=True)
            wanted = (platform_root / suffix).resolve(strict=True)
            if actual != wanted:
                raise ValueError('module is outside the caller selected coherent frozen platform root')
            self.module_sources += ((suffix, sha256(actual.read_bytes()).hexdigest()),)
        self.fd, self.raw, self.policy, self.clock_policy = fd_rig, raw_binding, source_policy, clock_policy
        self.mapper_instance, self.mapper_boot = mapper_instance, mapper_boot
        self.startup_timeout_s, self.poll_interval_s = startup_timeout_s, poll_interval_s
        self.cleanup_timeout_s = cleanup_timeout_s
        self.broker = _IsolatedBroker(broker_binary, broker_workdir,
            startup_timeout_s=broker_startup_timeout_s, cleanup_timeout_s=cleanup_timeout_s)
        self._thread = threading.get_ident()
        self._started = self._closed = False
        self._provider = SimpleNamespace(trust=None, world_failure=None)
        self._offered_vehicles = OrderedDict()
        self._pretrust = OrderedDict()
        self._publications = ()
        self._first_returned_sequence = None
        self.db = self.jio = self.uplink = self.mapper = self.supervisor = None
        self.motion = self.initial_fd = None
        self._latest_metadata = None
        self.material = self.peer = self._observed_link = None
        self._capture_pending = None
        self._capture_sequence = self._capture_upper = None
        self.last_reason = 'NOT_STARTED'
        self.last_capture_sample = self.last_capture_expires = None
        self.last_frame_query = None
        self.cleanup = []

    def _owned(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('mapper diagnostic belongs to the original source/compute/renderer owner')

    def _published(self, binding, context):
        if self._publications and self._publications[-1] != (binding, context.sha256):
            raise ValueError('unexpected source/publication change in one diagnostic session')
        self._publications = ((binding, context.sha256),)

    def _offer_vehicle(self):
        observed = self.fd.current_vehicle()
        if observed is None:
            return
        raw = observed.vehicle_bytes
        digest = sha256(raw).hexdigest()
        if digest in self._offered_vehicles:
            return
        if not self.uplink.offer_vehicle(raw):
            raise RuntimeError('actual Gateway Vehicle was not accepted by the original uplink clock owner')
        self._offered_vehicles[digest] = raw
        while len(self._offered_vehicles) > 128:
            self._offered_vehicles.popitem(last=False)

    def _pump(self):
        self._owned()
        if self._closed:
            raise RuntimeError('diagnostic source owners have ended')
        self._offer_vehicle()
        self.uplink.tick(self._provider)
        # caller가 명시한 개발 RAW 등록 경계다. A의 SDK/RAW ACK를 생산한 것으로 세지 않는다.
        self.supervisor.pump(self.material, bind_ready=True, on_publication=self._published)
        self.mapper.pump(before_outputs=self._ingest)
        if self.fd.owner.first_failure is not None or self.fd.peer.failure is not None:
            raise RuntimeError('original FD/plant source failed during mapper diagnostic')
        if self.uplink.failure is not None or self.jio.snapshot()['failure'] is not None:
            raise RuntimeError('actual MQTT/uplink source failed during mapper diagnostic')

    def _ingest(self):
        """실제 main의 원 row 수락이다. pretrust metadata를 고치거나 fake frame을 만들지 않는다."""
        from xenova_vehicle.transport.sensor_samples import SensorContext
        from xenova_vehicle.simulation.motion import DevelopmentMotion
        for row in self.mapper.vehicles.values():
            if row['clock_probe_sha256'] is None:
                raw = b64decode(row['raw'], validate=True)
                key = sha256(raw).hexdigest()
                original = (raw, int(row['received_j_us']))
                if key in self._pretrust and self._pretrust[key] != original:
                    raise ValueError('pretrust original Vehicle delivery changed')
                self._pretrust[key] = original
                while len(self._pretrust) > 24:
                    self._pretrust.popitem(last=False)
            elif sha256(b64decode(row['raw'], validate=True)).hexdigest() in self._pretrust:
                raise ValueError('a pretrust Vehicle was retroactively attached to a later lease')
        if self.mapper.publication is None or self.mapper.source is None or self.mapper.origin is None:
            return
        if self.motion is None:
            context = SensorContext.for_development(self.fd.profile, self.mapper.source, **self.mapper.origin)
            self.motion = DevelopmentMotion(self.fd.profile, self.raw, self.clock_policy,
                sensor_context=context, mapper_worker=self.peer.mapper_worker, mapper_instance=self.mapper_instance)
        before = self.motion.sensor_state.fence_revision
        self._latest_metadata = self.motion.ingest(self.mapper.context_bytes(), now_us=_j_us())
        if (self.motion.sensor_state.fence_revision != before
                or self._latest_metadata.clock_lease is None or self._latest_metadata.vehicle is None):
            self.mapper.discard_outputs()

    def pump(self):
        """caller renderer owner가 작업하는 동안 원 actor를 진행한다. 센서 snapshot은 재발급하지 않는다."""
        self._owned()
        if not self._started or self.motion is None:
            raise RuntimeError('original startup must finish before external actor pumping')
        self._pump()

    def _wait(self, predicate, label, deadline):
        while time.monotonic() < deadline:
            self._pump()
            value = predicate()
            if value is not None:
                return value
            time.sleep(self.poll_interval_s)
        self.last_reason = label
        raise TimeoutError(label + ': ' + json.dumps(self.diagnostics(), sort_keys=True))

    def _returned(self):
        from xenova_drive_world.mapper_clock import restore_returned_probe, MapperClockError
        lease = self.jio.current_lease
        if (lease is None or self.mapper.source is None or self.mapper.active_probe is None
                or self.uplink.counts['clock_warmup'] < 1 or self.uplink.counts['clock_accepted'] < 1):
            return None
        row = self.mapper.probes.get(self.mapper.active_probe)
        if row is None:
            return None
        raw = b64decode(row['raw'], validate=True)
        if raw != lease.raw_probe_bytes:
            return None
        try:
            restored = restore_returned_probe(raw, self.clock_policy, self.mapper.source, self.fd.host, _j_us())
        except MapperClockError:
            return None
        if restored is None or restored.trust_bytes != lease.trust_bytes:
            return None
        return restored

    def start(self):
        from xenova_drive_world import decode, schema
        from xenova_gateway.mapper_uplink import GatewayMapperUplink, UplinkProfile
        from xenova_vehicle.runtime.data_channel import DataLink, DataPeer, data_pair
        from xenova_vehicle.runtime.resident_storage import _Database, StorageProfile
        from xenova_vehicle.runtime.source_runtime import MapperSource, SupervisorSource
        from xenova_vehicle.transport.jetson_jio import JetsonJio
        from xenova_vehicle.transport.jio_profile import JetsonJioProfile
        self._owned()
        if self._started or self._closed:
            raise RuntimeError('single original diagnostic session required')
        self._started = True
        deadline = time.monotonic() + self.startup_timeout_s
        try:
            self.broker.start(deadline)
            database_profile = StorageProfile(str(self.broker.directory / 'mapper-diagnostic.sqlite3'), 'CREATE_IF_ABSENT')
            self.db = _Database(database_profile, _j_us() + 5_000_000)
            if self.db.selected_checkpoint is not None:
                raise ValueError('new independent diagnostic database must actually be EMPTY')
            binding = dict(service_instance=self.raw.fields['service_instance'], storage_worker=str(uuid4()),
                storage_profile_sha256=database_profile.sha256, database_uuid=self.db.metadata['database_uuid'],
                db_schema_version=self.db.metadata['schema_version'])
            bootstrap = dict(self.db.metadata, status='EMPTY', allocator_ready=True, selected_checkpoint=None)
            self.material = binding, bootstrap
            self.peer = DataPeer(self.raw.fields['service_instance'], str(uuid4()), str(uuid4()))
            self.jio = JetsonJio(JetsonJioProfile(self.policy, self.clock_policy, self.clock_policy.device,
                '127.0.0.1', self.broker.port))
            self.uplink = GatewayMapperUplink(UplinkProfile(self.clock_policy, '127.0.0.1', self.broker.port,
                None, None), self.fd.host, self.fd.gateway)
            left, right = data_pair()
            try:
                self.mapper = MapperSource(DataLink(left, self.peer, 'M'), self.raw, self.policy,
                    mapper_instance=self.mapper_instance, device=self.clock_policy.device, clock=_j_us)
                self.supervisor = SupervisorSource(DataLink(right, self.peer, 'S'), self.raw, self.jio,
                    mapper_instance=self.mapper_instance, mapper_boot=self.mapper_boot,
                    clock_epoch=self.raw.fields['clock_epoch'], clock=_j_us)
            except BaseException:
                left.close(); right.close()
                raise
            self.jio.start()
            self.uplink.start()
            self._observed_link = _ObservedLink(self.uplink.link)
            self.uplink.link = self._observed_link
            self.initial_fd = self.fd.wait_ready()
            self._wait(lambda: self.mapper.publication, 'WAIT_ACTUAL_SOURCE_IDENTITY_ACK', deadline)
            if not self._pretrust:
                raise ValueError('identity did not retain an actual lease-less Vehicle delivery')
            self._wait(self._returned, 'WAIT_TWO_ACCEPTS_AND_NEXT_RETURNED_PROBE13', deadline)
            # 이후 row는 이 실제 source HWM보다 커야 한다. 과거 None key를 소급 결박하지 않는다.
            current = self._wait(self.fd.current_vehicle, 'WAIT_ORIGINAL_POST_RETURN_SOURCE_HWM', deadline)
            self._first_returned_sequence = decode(current.vehicle_bytes, schema.VehicleSample).sequence
            if self.motion is None:
                raise RuntimeError('identity ACK did not initialize the actual main admission owner')
            self.last_reason = 'RETURNED_TRUST_READY_NOT_METRIC_AUTHORITY'
            return self
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note('cleanup remains unconfirmed: ' + str(cleanup_error)[:256])
            raise

    def begin_capture(self, scene, *, sequence, lifetime_us, wait_timeout_s):
        """post-trust 원 rows를 기다린 뒤 sample/E/deadline을 한 번 고정한다. 자동 재시도는 없다."""
        from xenova_vehicle.simulation.wall_sensor import WallPoseSample
        self._owned()
        if not self._started or self._closed or self.motion is None:
            raise RuntimeError('actual returned-trust source must be started')
        if self._capture_pending is not None:
            raise RuntimeError('one original render/read request is already pending')
        if type(sequence) is not int or not 0 <= sequence < 2**64:
            raise ValueError('original explicit capture sequence required')
        if self._capture_sequence is not None and sequence <= self._capture_sequence:
            raise ValueError('original capture sequence cannot be refunded or reissued')
        if type(lifetime_us) is not int or not 0 < lifetime_us <= 500_000:
            raise ValueError('original explicit capture E must be within500ms')
        _seconds(wait_timeout_s, 5)
        deadline = time.monotonic() + wait_timeout_s
        def left_rows():
            now = _j_us()
            if self._latest_metadata is None or self._latest_metadata.vehicle is None:
                return None
            rows = tuple(row for row in self.motion.sensor_state.vehicle_admissions
                if row.evidence.anchor.sequence > self._first_returned_sequence
                and row.evidence.j_interval[1] <= now < row.evidence.valid_until_j_us)
            return rows if len(rows) >= 2 else None
        self._wait(left_rows, 'WAIT_NEW_POST_TRUST_VEHICLES', deadline)
        development = self.fd.profile.development_profile.for_scene(scene)
        self.last_capture_sample = sample = WallPoseSample.sample(self.raw.fields['capture_epoch'], sequence,
            self.fd.plant.renderer_pose)
        if self._capture_upper is not None and sample.captured_j_us[0] <= self._capture_upper:
            raise ValueError('original capture time regressed; no clock replacement')
        self._capture_sequence, self._capture_upper = sequence, sample.captured_j_us[1]
        self.last_capture_expires = expires = sample.captured_j_us[0] + lifetime_us
        source_key = ('sim-wall:' + self.raw.fields['capture_epoch'], 'rgb_video',
            'jetson-monotonic:' + self.raw.fields['clock_epoch'], sequence)
        request = WallRenderRequest(development, self.fd.profile, scene, sample,
            self.raw.fields['clock_epoch'], expires, deadline, str(uuid4()), self.raw.sha256, source_key)
        self._capture_pending = request
        return request

    def finish_capture(self, original_request, capture):
        """caller가 돌려준 원 WallCapture만 소비한다. RELEASE/렌더 완료 자체는 pose 성공이 아니다."""
        from xenova_vehicle.simulation.wall_sensor import WallCapture
        self._owned()
        if self._closed or original_request is not self._capture_pending or original_request is None:
            raise ValueError('exact pending original render request required')
        request = original_request
        if not request._matches(capture):
            raise ValueError('exact original Wall producer completion required')
        self._capture_pending = None
        expires = request.expires_j_us
        if capture is None or _j_us() >= expires or time.monotonic() >= request.deadline_wall:
            self.last_reason = 'ORIGINAL_WALL_CAPTURE_EXPIRED'
            raise TimeoutError(self.last_reason)
        if (type(capture) is not WallCapture or capture.expires_j_us != expires
                or capture.captured_j_us != request.sample.captured_j_us
                or capture.profile_sha256 != request.development_profile.sha256
                or capture.observation.rgb.stamp.sequence != request.sample.sequence
                or capture.observation.calibration_id != self.fd.profile.bound_calibration.sha256):
            raise ValueError('renderer completion changed original capture/profile/source/sequence/E')
        lower, upper = capture.observation.capture_interval
        if lower >= upper:
            raise ValueError('original wall capture must have a nonzero interval')
        # 원 E를 wall wait 상한과 함께 고정한다. pump/clock 갱신은 이 job의 E를 옮기지 않는다.
        deadline = min(request.deadline_wall, time.monotonic() + max(0, expires - _j_us()) / 1_000_000)
        def bracket_snapshot():
            now = _j_us()
            if now >= expires:
                return None
            snapshot = self.motion.frame_snapshot(capture.observation, now_us=now,
                expires_j_us=expires, job_id=request.job_id, source_generation=request.source_generation,
                source_key=request.source_key)
            inputs = snapshot.metric_inputs
            self.last_frame_query = (('job_id', request.job_id), ('query_j_us', now),
                ('expires_j_us', expires), ('clock_present', inputs.clock_lease is not None),
                ('vehicle_present', inputs.vehicle is not None), ('pose_present', inputs.pose is not None),
                ('reasons', inputs.reasons), ('fence_revision', snapshot.fence_revision))
            if inputs.pose is None or inputs.clock_lease is None or inputs.vehicle is None:
                return None
            bracket = snapshot.original_pose_bracket(inputs.pose)
            if len(bracket.originals) != 2:
                raise ValueError('actual nonzero capture requires two original bracket endpoints')
            return snapshot
        frame = self._wait(bracket_snapshot, 'WAIT_ORIGINAL_FULL_CAPTURE_BRACKET', deadline)
        result = frame.metric_inputs
        bracket = frame.original_pose_bracket(result.pose)
        if (len(bracket.originals) != 2 or result.pose.sample.origin != 'SYNTHETIC_ODOMETRY'
                or result.pose.captured_us != (lower, upper)):
            raise ValueError('actual two-row non-oracle full capture bracket was not retained')
        if any(row.evidence.wire_sha256 not in self._offered_vehicles for row in bracket.originals):
            raise ValueError('pose consumed a Vehicle not delivered by this actual FD/uplink owner')
        current = self.fd.current_vehicle()
        now = _j_us()
        if current is None or now >= expires:
            raise TimeoutError('original FD/source/capture expired before diagnostic return')
        frame.validate_frame(job_id=request.job_id, source_generation=request.source_generation,
            source_key=request.source_key, expires_us=expires, now_us=now,
            fence_revision=self.motion.sensor_state.fence_revision, context_bytes=self.mapper.context_bytes())
        if not frame.sensors.current_vehicle(result.vehicle, now):
            raise TimeoutError('original snapshot Vehicle expired before diagnostic return')
        result.clock_lease.pi_interval_to_j(
            (result.clock_lease.probe_pi_send_us, result.clock_lease.probe_pi_send_us), now)
        if frame.p4_evidence is not None or result.clock_lease.completed_pi_us is not None:
            raise ValueError('Jetson returned-Probe snapshot must not invent original Pi P4 evidence')
        value = MapperMotionObservation(current, capture, frame.source_context_bytes, result, bracket,
            result.clock_lease.raw_probe_bytes, result.clock_lease.trust_bytes,
            self._observed_link.records, tuple(self._pretrust.values()), now,
            tuple(self.uplink.counts.items()), frame)
        self.last_reason = 'ACTUAL_FULL_CAPTURE_POSE_NOT_NN_FREE_OR_AUTHORITY'
        if self._observer is not None:
            self._observer.offer(value)  # 진단 drop은 원 성공 DTO/시각/권한을 바꾸지 않는다.
        return value

    def cancel_capture(self, original_request):
        """미완료 원 작업만 폐기한다. 원 sequence/시각/E는 환급하거나 다시 발급하지 않는다."""
        self._owned()
        if original_request is not self._capture_pending or original_request is None:
            raise ValueError('exact pending original render request required')
        with original_request._once.lock:
            if original_request._once.started and not original_request._once.finished:
                raise RuntimeError('caller must finish/join the actual renderer before cancellation')
            if not original_request._once.started:
                original_request._once.started = original_request._once.finished = True
                original_request._once.failure = 'CANCELLED_BEFORE_RENDER'
        self._capture_pending = None
        self.last_reason = 'ORIGINAL_RENDER_REQUEST_CANCELLED_NOT_REISSUED'

    def capture_motion(self, scene, *, sequence, lifetime_us, wait_timeout_s):
        """paired API의 동기 편의 경로다. 긴 렌더의 actor 공백/원 age/maxgap 실패를 숨기지 않는다.

        source actor가 멈추므로 Nano의 186~336ms 렌더는 age200ms/maxgap100ms 정책에서
        성공을 보장하지 않는다. 실제 긴 렌더는 별도 owner에서 수행하고 main pump의
        fresh ingest를 계속해야 한다. history capacity/원 E는 caller profile 그대로다.
        nonzero는 원 capture interval 폭이며 주행 속도나 새 COMMAND 수락을 뜻하지 않는다.
        """
        request = self.begin_capture(scene, sequence=sequence, lifetime_us=lifetime_us,
                                     wait_timeout_s=wait_timeout_s)
        try:
            capture = request.render()
        except BaseException:
            self.cancel_capture(request)
            raise
        return self.finish_capture(request, capture)

    def diagnostics(self):
        self._owned()
        return dict(last_reason=self.last_reason, started=self._started, closed=self._closed,
            fd=self.fd.diagnostics(), jio=None if self.jio is None else self.jio.snapshot(),
            uplink_counts={} if self.uplink is None else dict(self.uplink.counts),
            uplink_failure=None if self.uplink is None else self.uplink.failure,
            uplink_exitcode=None if self.uplink is None or self.uplink.process is None else self.uplink.process.exitcode,
            actual_empty_database_uuid=None if self.db is None else self.db.metadata['database_uuid'],
            source_bound=self.mapper is not None and self.mapper.publication is not None,
            pretrust_rows=len(self._pretrust), offered_vehicle_count=len(self._offered_vehicles),
            module_sources=dict(self.module_sources), broker_binary_sha256=self.broker.binary_sha256,
            broker_exitcode=None if self.broker.process is None else self.broker.process.poll(),
            render_pending=self._capture_pending is not None,
            original_capture_sample=None if self.last_capture_sample is None else asdict(self.last_capture_sample),
            original_capture_expires_j_us=self.last_capture_expires,
            sensor_admission=None if self.motion is None else dict(
                retained_records=len(self.motion.sensor_state.vehicle_admissions),
                capacity=self.fd.profile.pose_arguments['capacity'],
                fence_revision=self.motion.sensor_state.fence_revision),
            last_frame_query=None if self.last_frame_query is None else dict(self.last_frame_query),
            cleanup=list(self.cleanup))

    def close(self):
        """원 owner의 STOP/close→peer close→uplink reap→JIO join→DB close를 모두 시도한다.

        FD close의 STOP 시도와 실제 thread 종료를 소비한다. 측정 정지나 ACK 성공을
        새로 주장하지 않으며 caller renderer의 종료 미확인도 cleanup 성공으로 감추지 않는다.
        """
        self._owned()
        if self._closed:
            return
        errors = []
        def finish(name, callback):
            try:
                callback()
                self.cleanup.append((name, 'CLOSED'))
            except BaseException as error:
                self.cleanup.append((name, type(error).__name__ + ':' + str(error)[:160]))
                errors.append(error)
        pending = self._capture_pending
        if pending is not None:
            with pending._once.lock:
                if pending._once.started and not pending._once.finished:
                    errors.append(RuntimeError('caller renderer completion/join remains unconfirmed'))
                    self.cleanup.append(('CALLER_RENDERER', 'UNCONFIRMED'))
        finish('FD_STOP_OWNER_JOIN_PEER_CLOSE', self.fd.close)
        if self.uplink is not None:
            finish('UPLINK_STOP_REAP', self.uplink.close)
        if self.jio is not None:
            self.jio.stop()
            def join_jio():
                if not self.jio.join(self.cleanup_timeout_s):
                    raise RuntimeError('actual JIO owner join remains unconfirmed')
            finish('JIO_JOIN', join_jio)
        if self.mapper is not None:
            finish('M_DATA_CLOSE', self.mapper.close)
        if self.supervisor is not None:
            finish('S_DATA_CLOSE', self.supervisor.close)
        if self.db is not None:
            finish('DB_AND_WRITER_LOCK_CLOSE', self.db.close)
        finish('BROKER_REAP_LOG_CLOSE', self.broker.close)
        if errors:
            raise RuntimeError('diagnostic cleanup remains unconfirmed: ' + str(self.cleanup)) from errors[0]
        self._closed = True

    def __enter__(self): return self.start()

    def __exit__(self, *unused): self.close()
