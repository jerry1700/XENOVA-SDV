"""PC 관측용 A/M의 실제 제어 BIND와 A 소유 HOST_SIM 모델 수명이다.

기존 physical WorkerBoot/RawBindMessage를 바꾸지 않는다. S는 실제 spawn/등록과
메모리·입력 채널을 별도로 소유한다. graph start는 NN warmup/Identity/Scene/주행
READY가 아니다. M 교체를 위해 A의 모델을 닫는 포트는 제공하지 않는다.
"""

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from ..oak.sim_nn import SimNnProfile, SimNnSource
from ..oak.sim_nn_manifest import SimNnManifest, validate_start_receipt
from ..oak.yolop_asset import load_yolop_asset
from ..simulation.pc_input import PcSourceBinding
from .lifecycle_messages import LifecyclePeer
from .local_envelope import decode_structure
from .native_resources import ArenaSpec
from .pc_binding_messages import PcBindContext, PcBindMessage, PcBindAck
from .pc_native import PcArenaWriter, PcArenaReader
from .raw_leases import RawScope
from .resident_transport import ResidentLink, j_us
from .resident_workers import _Actor, _StopWork


def pc_worker_release():
    """실제 배포된 Python 원문 목록을 기동 때 고정한다. 매 frame에 호출하지 않는다."""
    package = Path(__file__).resolve().parents[1]
    tools = Path(__file__).resolve().parents[3] / 'tools' / 'hil_observation'
    rows = []
    for label, root in (('src/xenova_vehicle', package), ('tools/hil_observation', tools)):
        if not root.is_dir() or root.is_symlink():
            raise ValueError('PC worker source closure missing or linked')
        for path in sorted(root.rglob('*.py')):
            if path.is_symlink() or not path.is_file():
                raise ValueError('PC worker source closure contains a linked file')
            raw = path.read_bytes()
            if not 0 < len(raw) <= 1024 * 1024:
                raise ValueError('PC worker source member exceeds its bound')
            rows.append((label + '/' + path.relative_to(root).as_posix(), len(raw), sha256(raw).hexdigest()))
    if not rows or len(rows) > 1024:
        raise ValueError('PC worker source closure denominator exceeds its bound')
    raw = json.dumps(dict(profile='pc-worker-release-1', files=rows),
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class PcWorkerBoot:
    """실제 spawn에 전달하는 값이다. opaque frame/current-view 객체는 직렬화하지 않는다.

    binding과 NN manifest는 원 canonical bytes로 전달해 child가 다시 파싱한다.
    raw_tokens는 S가 생성한 실제 SemLock들이다. 형식 검사는 등록/출생 사실이 아니다.
    """

    peer: LifecyclePeer
    acquisition_peer: LifecyclePeer
    profile: SimNnProfile
    raw_scope: RawScope
    arena_spec: ArenaSpec
    binding_bytes: bytes
    nn_manifest_bytes: bytes
    raw_tokens: tuple
    startup_deadline_us: int
    diagnostic_directory: str
    writer_bind_raw: bytes | None = None

    def __post_init__(self):
        if (type(self.peer) is not LifecyclePeer or self.peer.role not in ('A', 'M')
                or type(self.acquisition_peer) is not LifecyclePeer or self.acquisition_peer.role != 'A'
                or self.acquisition_peer.service_instance != self.peer.service_instance
                or type(self.profile) is not SimNnProfile or type(self.raw_scope) is not RawScope
                or type(self.arena_spec) is not ArenaSpec or type(self.raw_tokens) is not tuple
                or len(self.raw_tokens) != 2 or any(type(pair) is not tuple or len(pair) != 2 for pair in self.raw_tokens)
                or type(self.startup_deadline_us) is not int or not 0 < self.startup_deadline_us < 2**63
                or type(self.diagnostic_directory) is not str or not self.diagnostic_directory):
            raise ValueError('exact PC worker scope, NN profile, tokens and startup cap required')
        binding = PcSourceBinding.from_bytes(self.binding_bytes)
        manifest = SimNnManifest.from_bytes(self.nn_manifest_bytes)
        if (self.peer.service_instance != binding.fields['service_instance']
                or self.raw_scope.service_instance != self.peer.service_instance
                or self.raw_scope.raw_binding_sha256 != binding.sha256
                or self.arena_spec.role != 'RAW'
                or (self.arena_spec.arena_id, self.arena_spec.name, self.arena_spec.size_bytes)
                    != (self.raw_scope.arena_id, self.raw_scope.arena_name, self.raw_scope.arena_size_bytes)
                or (self.profile.source_id, self.profile.clock_id, self.profile.calibration_id)
                    != (binding.source_id, binding.clock_id, binding.fields['calibration_profile_sha256'])
                or manifest.sha256 != binding.fields['nn_manifest_sha256']
                or manifest.canonical_bytes != self.nn_manifest_bytes
                or manifest.fields['sim_profile'] != self.profile.to_dict()
                or (self.acquisition_peer.channel_epoch, self.acquisition_peer.worker_instance)
                    != (self.raw_scope.writer.channel_epoch, self.raw_scope.writer.worker_instance)):
            raise ValueError('PC boot differs from its registered descriptor, arena or NN manifest')
        if self.peer.role == 'A' and (self.peer.channel_epoch, self.peer.worker_instance) != (
                self.raw_scope.writer.channel_epoch, self.raw_scope.writer.worker_instance):
            raise ValueError('A must be the registered PC arena writer')
        if self.peer.role == 'A' and (self.acquisition_peer != self.peer or self.writer_bind_raw is not None):
            raise ValueError('A boot pins itself and does not claim a previous writer BIND')
        if self.peer.role == 'M' and (self.peer.channel_epoch == self.raw_scope.writer.channel_epoch
                or self.peer.worker_instance == self.raw_scope.writer.worker_instance):
            raise ValueError('M reader must differ from the original A endpoint')
        if self.peer.role == 'M':
            if type(self.writer_bind_raw) is not bytes:
                raise ValueError('M requires the preserved original A BIND bytes')
            context = PcBindContext(self.acquisition_peer, self.raw_scope.writer,
                binding, self.raw_scope, self.arena_spec)
            PcBindMessage(decode_structure(self.writer_bind_raw), context)


class PcWorkerActor(_Actor):
    """같은 제어 loop와 원 STOP 기한을 쓰는 명시 PC actor다.

    transport/heartbeat/STOP 처리는 기존 actor의 구현을 재사용한다. physical artifact
    검사를 우회 호출하지 않고 PC 전용 release·descriptor·manifest를 독립 검산한다.
    slot·source 승인과 NN 결과 처리는 상위 A/M loop가 실제 채널로 연결한다.
    """

    def __init__(self, endpoint, boot):
        if type(boot) is not PcWorkerBoot or pc_worker_release() != boot.peer.release_sha256:
            raise ValueError('actual PC worker release differs from its registered boot')
        self.boot = boot
        self.expected_binding = PcSourceBinding.from_bytes(boot.binding_bytes)
        self.link = ResidentLink(endpoint, boot.peer, boot.peer.role, track_sent=True)
        self.state = 'STARTING'
        self.stop = self.last_progress = None
        self.last_heartbeat = self.progress_count = 0
        self.leases_released = True
        self.device_closed = True if boot.peer.role == 'A' else None
        self.failure = self.binding = self.bind = self.ack = self.ack_sent = None
        self.last_filled_sent = self.last_actual_sent = None
        self.deferred = deque()
        self.sent_observer = self.idle_hook = None
        self._idle_busy = False
        self._nn_source = self._nn_start = self._nn_manifest = None
        self._nn_close_deadline = None
        self._arena = None
        self._hello()

    def accept_bind(self, packet):
        if self.bind is not None:
            raise ValueError('PC actor accepts one original RAW BIND')
        context = PcBindContext(self.boot.peer, self.boot.raw_scope.writer,
            self.expected_binding, self.boot.raw_scope, self.boot.arena_spec)
        bind = PcBindMessage(packet, context, cap_us=self.boot.startup_deadline_us)
        if self.boot.peer.role == 'M':
            writer_context = PcBindContext(self.boot.acquisition_peer, self.boot.raw_scope.writer,
                self.expected_binding, self.boot.raw_scope, self.boot.arena_spec)
            original_writer = PcBindMessage(decode_structure(self.boot.writer_bind_raw), writer_context)
            if bind.binding_revision != original_writer.binding_revision:
                raise ValueError('M BIND differs from the preserved A binding revision')
        now = j_us()
        if not bind.issued_us <= now < bind.expires_us:
            raise ValueError('original PC BIND expired before acceptance')
        packet = self.send_required('BIND_ACK', dict(binding_revision=str(bind.binding_revision),
            binding_sha256=bind.binding.sha256, accepted=True, state=self.state, reason=None),
            domain='RAW', revision=bind.binding_revision, request_id=bind.request_id,
            issued_us=now, expires_us=bind.expires_us)
        self.bind, self.binding = bind, bind.binding
        self.ack = PcBindAck(packet, bind)
        self.drain_sent()

    def attach_arena(self):
        """실제 ACK 송신 뒤 원 scope의 SHM/token에 붙는다. S의 원장 승인을 만들지는 않는다."""
        if self.bind is None or self.ack_sent is None or self.stop is not None or self._arena is not None:
            raise RuntimeError('one actual arena attachment after PC BIND/ACK required')
        constructor = PcArenaWriter if self.boot.peer.role == 'A' else PcArenaReader
        self._arena = constructor(self.boot.raw_scope, self.boot.raw_tokens)
        return self._arena

    @property
    def arena(self):
        if self._arena is None:
            raise RuntimeError('original PC arena is not attached')
        return self._arena

    @property
    def nn_source(self):
        """A loop의 실제 source다. M은 이 포트로 Device를 열 수 없다."""
        if self.boot.peer.role != 'A' or self._nn_source is None:
            raise RuntimeError('actual A HOST_SIM source has not started')
        return self._nn_source

    @property
    def model_start(self):
        """실제 start 반환값 검산 결과다. NN parser warmup이나 S 수락을 뜻하지 않는다."""
        return self._nn_start

    def start_model(self):
        """원 BIND/실제 ACK 송신 뒤 A에서만 Device와 graph를 한 번 시작한다."""
        if (self.boot.peer.role != 'A' or self.bind is None or self.ack_sent is None
                or self._arena is None or self._nn_source is not None or self.stop is not None):
            raise RuntimeError('actual bound A starts its resident model once')
        if j_us() >= self.boot.startup_deadline_us:
            raise TimeoutError('original PC model startup cap expired')
        asset = load_yolop_asset(self.boot.profile.oak.asset_dir, self.boot.profile.oak.asset_manifest)
        manifest = SimNnManifest.from_current(self.boot.profile, asset)
        if manifest.canonical_bytes != self.boot.nn_manifest_bytes:
            raise ValueError('actual local PC NN closure differs from S pinned manifest')
        source = SimNnSource(self.boot.profile, context_key=manifest.sha256)
        self._nn_source = source
        self.device_closed = False
        def stage(value):
            if value not in ('ASSET_CHECK', 'DEVICE_BOOT', 'PIPELINE_START'):
                raise ValueError('unknown actual HOST_SIM startup stage')
            self.state = value
            self.heartbeat(force=True)
        receipt = source.start(stage, startup_deadline_us=self.boot.startup_deadline_us)
        self._nn_start = validate_start_receipt(manifest, receipt,
            context_key=manifest.sha256, startup_deadline_us=self.boot.startup_deadline_us)
        self._nn_manifest = manifest
        self.state = 'WARMUP'
        return self._nn_start

    def close_model(self, deadline_us):
        """A 종료 때만 실제 close를 호출한다. 원 close cap은 반복 호출로 늘리지 않는다."""
        if self.boot.peer.role != 'A':
            raise RuntimeError('M never closes the A-owned OAK Device')
        if type(deadline_us) is not int or not 0 < deadline_us < 2**63:
            raise ValueError('original PC close deadline required')
        self._nn_close_deadline = deadline_us if self._nn_close_deadline is None else min(
            deadline_us, self._nn_close_deadline)
        if self._nn_source is not None and not self.device_closed:
            self._nn_source.close(deadline_us=self._nn_close_deadline)
            self.device_closed = True

    def finish(self):
        try:
            if self._arena is not None:
                self._arena.close()
                self._arena = None
        finally:
            super().finish()


def pc_worker_main(endpoint, boot, loop):
    """등록한 child의 실제 HELLO/BIND/ACK/SHM/모델 기동과 종료를 수행한다.

    loop는 배포된 A/M main 함수이며 queue·lease·parser·원 STOP을 실제 채널에서 처리한다.
    정상 반환도 원 STOP이 있어야 한다. 실제 exit/reap 판정과 재시작은 S만 수행한다.
    """
    actor = None
    try:
        if type(boot) is not PcWorkerBoot or not callable(loop):
            raise ValueError('registered PC boot and actual A/M loop required')
        actor = PcWorkerActor(endpoint, boot)
        actor.await_bind()
        actor.attach_arena()
        if boot.peer.role == 'A':
            actor.start_model()
        loop(actor)
        if actor.stop is None:
            raise RuntimeError('PC worker loop returned without an original S STOP')
    except _StopWork:
        pass
    except BaseException as error:
        if actor is None:
            raise
        actor.fault(error)
    finally:
        if actor is not None:
            try:
                if boot.peer.role == 'A':
                    now = j_us()
                    cap = actor.stop.expires_us if actor.stop is not None else now + int(
                        boot.profile.oak.close_timeout_s * 1_000_000)
                    actor.close_model(cap)
            except BaseException as error:
                actor.fault(error)
            finally:
                actor.finish()
        else:
            endpoint.close()
