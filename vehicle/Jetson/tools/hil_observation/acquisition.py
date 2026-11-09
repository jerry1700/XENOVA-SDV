"""등록된 A child의 실제 관측 pump·RAW copy·M ACK·HOST_SIM loop를 연결한다.

S가 만든 연결과 원 boot만 받는다. listen/자동 reconnect/Source READY/Gateway를 만들지
않는다. PC 연결 손실 뒤에는 원 owner를 fence한 채 모델을 유지하고 실제 S STOP을
기다린다. SDK Device close는 pc_worker_main의 원 종료 경계에서만 일어난다.
"""

from dataclasses import dataclass
import json
import math
import socket
import time
from uuid import uuid4

from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
from xenova_vehicle.runtime.pc_acquisition import PcAcquisitionDriver
from xenova_vehicle.runtime.pc_channel import PcActorLink, decode_forward
from xenova_vehicle.runtime.pc_slot_messages import (PcWriterSlotContext, PcSlotMessage,
    pc_write_request_payload, pc_filled_payload, pc_slot_meta_fields)
from xenova_vehicle.runtime.pc_workers import PcWorkerActor
from xenova_vehicle.runtime.resident_transport import j_us
from xenova_vehicle.runtime.slot_messages import SlotTimeLimits
from xenova_vehicle.oak.sim_nn_manifest import SimNnManifest

from .clock import ClockPolicy
from .session import JetsonObservationPump
from .transport import SessionScope


@dataclass(frozen=True)
class PcAcquisitionLoop:
    """실제 spawn에 전달하는 A loop 설정이다. scope는 opaque 객체 대신 원 bytes다.

    duration_s는 caller가 정한 원 개발 실행 한도다. 원 frame500ms·copy150ms·clock
    lease는 이 실행 한도로 늘리지 않는다. socket 등록/실제 출생은 S의 별도 사실이다.
    """
    observation_endpoint: object
    metadata_endpoint: object
    mapper_peer: LifecyclePeer
    scope_bytes: bytes
    clock_policy: ClockPolicy
    duration_s: float
    probe_interval_us: int
    slot_limits: SlotTimeLimits

    def __post_init__(self):
        if (type(self.observation_endpoint) is not socket.socket or type(self.metadata_endpoint) is not socket.socket
                or type(self.mapper_peer) is not LifecyclePeer or self.mapper_peer.role != 'M'
                or type(self.scope_bytes) is not bytes or not 0 < len(self.scope_bytes) <= 2048
                or type(self.clock_policy) is not ClockPolicy or type(self.slot_limits) is not SlotTimeLimits
                or type(self.duration_s) not in (int, float) or not math.isfinite(self.duration_s)
                or self.duration_s <= 0 or type(self.probe_interval_us) is not int
                or not 0 < self.probe_interval_us < self.clock_policy.lease_us):
            raise ValueError('registered actual PC endpoints, original scope/policies and bounded duration required')
        value = json.loads(self.scope_bytes)
        scope = SessionScope(**value)
        if scope.raw_bytes != self.scope_bytes:
            raise ValueError('original canonical PC session scope required')

    def __call__(self, actor):
        run_acquisition(actor, self)


def run_acquisition(actor, settings):
    """실제 A main에서 한 연결을 진행한다. 종료·재시작 결정은 S에 남긴다."""
    if type(actor) is not PcWorkerActor or type(settings) is not PcAcquisitionLoop:
        raise ValueError('actual bound PC A actor and original loop settings required')
    binding = actor.binding
    scope = SessionScope(**json.loads(settings.scope_bytes))
    link = pump = driver = None
    request = request_draft = metadata_draft = None
    start_draft = None
    source_fenced = False
    try:
        # spawn은 socket의 Python timeout 상태를 보존하지 않으므로 수신 owner가 다시 고정한다.
        settings.observation_endpoint.setblocking(False)
        link = PcActorLink(settings.metadata_endpoint, actor.boot.peer, 'A', binding)
        # callback은 pump 생성 뒤부터만 호출된다. 모든 port는 이 A main thread가 소유한다.
        pump = JetsonObservationPump(settings.observation_endpoint, binding=binding, scope=scope,
            clock_policy=settings.clock_policy, destination_clock_id=binding.clock_id,
            duration_s=settings.duration_s, probe_interval_us=settings.probe_interval_us,
            on_frame=lambda frame, owner: driver.offer(frame, owner),
            cancelled=lambda: actor.stop is not None)
        driver = PcAcquisitionDriver(actor, link, settings.mapper_peer, pump.pc_owner)
        manifest = SimNnManifest.from_bytes(actor.boot.nn_manifest_bytes)
        context = PcWriterSlotContext(actor.bind, actor.boot.raw_scope)
        now = j_us()
        start_draft = dict(kind='PC_NN_START', metadata=dict(nn_manifest_sha256=manifest.sha256),
            body=actor.model_start.canonical_receipt_bytes, request_id=str(uuid4()), issued_us=now,
            expires_us=min(now + 5_000_000, actor.boot.startup_deadline_us))
        if start_draft['expires_us'] <= now:
            raise TimeoutError('original actual NN start report cap expired')

        def flush_pc():
            nonlocal start_draft, metadata_draft
            sent = link.flush()
            if sent is not None:
                if sent.packet.kind == 'PC_NN_START': start_draft = None
                else:
                    if metadata_draft is None:
                        raise RuntimeError('actual PC metadata send has no original pending draft')
                    driver.note_metadata_sent(sent)
                    metadata_draft = None
            if link.pending is None:
                draft = start_draft if start_draft is not None else metadata_draft
                if draft is not None: link.send(**draft)

        while True:
            time.sleep(.002)
            incoming = actor.poll()  # 원 STOP은 _StopWork로 pc_worker_main에 전파한다.
            for packet in incoming:
                if source_fenced or packet.kind != 'SLOT' or packet.payload.get('op') != 'GRANT' or request is None:
                    raise ValueError('unexpected PC A control statement or grant without original request')
                grant = PcSlotMessage(packet, context, request, limits=settings.slot_limits)
                request.record.check(j_us())
                encoding, filled = driver.publish(grant.record)
                out = actor.required_slot(pc_filled_payload(grant, filled), request_id=grant.request_id,
                    issued=filled.completed_us, expires=grant.expires_us)
                PcSlotMessage(out, context, grant, limits=settings.slot_limits)
                driver.note_filled_sent(actor.await_sent(out, cap_us=driver.active.expires_us))
                metadata_draft = dict(kind='PC_EARLY_META' if encoding.phase == 'EARLY' else 'PC_NN_RESULT_META',
                    metadata=pc_slot_meta_fields(encoding, purpose=driver.active.purpose), body=encoding.descriptor_bytes,
                    request_id=driver.active.job_id, issued_us=filled.completed_us,
                    expires_us=driver.active.expires_us)
                request = request_draft = None

            if source_fenced:
                # metadata/새 관측은 진행하지 않고 실제 S STOP과 원 NN drain만 처리한다.
                actor.nn_source.poll()
                continue
            flush_pc()
            for received in link.poll():
                packet = received.packet
                if packet.kind == 'PC_SOURCE_STATE':
                    if not source_fenced: driver.source_state(received)
                elif packet.kind == 'PC_SOURCE_FENCE':
                    link.require_received(received)
                    pump.close(); driver.fence(); source_fenced = True
                    actor.state = 'DEGRADED'
                elif packet.kind == 'PC_FORWARD':
                    if source_fenced: continue
                    original = decode_forward(packet, settings.mapper_peer, binding).original_packet
                    if original.kind == 'PC_SNAPSHOT_ACK': driver.snapshot_ack(received)
                    elif original.kind == 'PC_PARSE_COMPLETE': driver.parser_complete(received, manifest)
                    elif original.kind == 'PC_SOURCE_FENCE':
                        pump.close(); driver.fence(); source_fenced = True
                        actor.state = 'DEGRADED'
                    else: raise ValueError('unsupported original M statement at A')
                else: raise ValueError('unsupported S statement at PC A')

            if source_fenced:
                # 원 모델/Device는 유지한다. 원 output queue를 유한 drain할 뿐 재-enqueue하지 않는다.
                actor.nn_source.poll()
                continue

            result = pump.step()
            if result is not None:
                # 수신 owner는 pump가 이미 fence/close했다. S는 이 원 사건으로 Source를 철회한다.
                driver.fence(); source_fenced = True; actor.state = 'DEGRADED'
                if request is not None or request_draft is not None or metadata_draft is not None or link.pending is not None:
                    raise RuntimeError('PC observation ended with an unresolved original copy/control transaction')
                stamp = j_us()
                link.send('PC_SOURCE_FENCE', dict(reason=result.reason, failure=result.failure),
                    request_id=str(uuid4()), issued_us=stamp, expires_us=stamp + 5_000_000)
                # full-send는 이 호출의 실제 반환으로만 확인하며 다음 loop에서 draft를 재발급하지 않는다.
                while link.pending is not None:
                    if actor.poll():
                        raise ValueError('unresolved original control arrived after PC source fence')
                    link.flush()
                    time.sleep(.002)
                continue

            if start_draft is not None: continue
            if request is not None:
                request.record.check(j_us())
            elif request_draft is not None and j_us() >= request_draft['expires']:
                raise TimeoutError('original PC write request expired before actual selection')
            if request is not None or metadata_draft is not None or link.pending is not None: continue
            driver.expire()
            if driver.waiting_enqueue: driver.enqueue()
            driver.poll_nn()
            driver.promote()
            job = driver.active
            if job is not None and driver.phase in ('EARLY', 'NN'):
                if request_draft is None:
                    issued = j_us()
                    request_draft = dict(payload=pc_write_request_payload(binding, actor.boot.raw_scope,
                        job_id=job.job_id, phase=driver.phase, expires_j_us=job.expires_us, full_key=job.full_key,
                        pc_frame_proof_sha256=job.proof_sha256, purpose=job.purpose), request_id=str(uuid4()),
                        issued=issued, expires=min(issued + settings.slot_limits.copy_budget_us, job.expires_us))
                out = actor.slot(**request_draft)
                if out is not None:
                    request = PcSlotMessage(out, context, limits=settings.slot_limits, cap_us=job.expires_us)
                    request_draft = None
    finally:
        try:
            if pump is not None: pump.close()
        finally:
            try:
                if driver is not None: driver.fence()
            finally:
                if link is not None: link.close()
                else: settings.metadata_endpoint.close()
                if pump is None: settings.observation_endpoint.close()
