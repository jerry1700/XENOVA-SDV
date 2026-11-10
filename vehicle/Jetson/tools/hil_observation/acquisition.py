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
from xenova_vehicle.runtime.health_policy import FrameExpired
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


class _AcquisitionDiagnostic:
    """A owner의 고정8단계와 pump 간격만 집계한다. 추가 j_us 호출·계수 비용이 있다."""

    _STAGES = ('actor_poll', 'metadata_flush', 'metadata_poll', 'raw_publish',
               'raw_await_sent', 'sdk_enqueue', 'sdk_poll', 'pump_step')

    def __init__(self):
        self.stages = {name: dict(calls=0, errors=0, max_duration_us=0) for name in self._STAGES}
        self.pump_last_us = None
        self.pump_max_gap_us = 0
        self.positions_before_last_pump = self.copy_request = self.final = None
        self.dropped = 0
        self.save_attempted = False

    def _tick(self):
        try:
            value = j_us()
            if type(value) is int and 0 <= value < 2**63:
                return value
        except BaseException:
            pass
        self.dropped = min(2**63-1, self.dropped+1)
        return None

    def call(self, stage, function, *args, **kwargs):
        began = self._tick()
        row = self.stages[stage]
        row['calls'] = min(2**63-1, row['calls']+1)
        if stage == 'pump_step' and began is not None:
            if self.pump_last_us is not None and began >= self.pump_last_us:
                self.pump_max_gap_us = max(self.pump_max_gap_us, began-self.pump_last_us)
            self.pump_last_us = began
        try:
            return function(*args, **kwargs)
        except BaseException:
            row['errors'] = min(2**63-1, row['errors']+1)
            raise
        finally:
            ended = self._tick()
            if began is not None and ended is not None and ended >= began:
                row['max_duration_us'] = max(row['max_duration_us'], ended-began)

    @staticmethod
    def _positions(pump):
        session = pump._session
        sender, receiver = session.sender, session.receiver
        return dict(sender_active_E=None if sender._active is None else sender._active[1],
            sender_pending_E=None if sender._pending is None else sender._pending[1],
            sender_part=sender._part, sender_offset=sender._offset,
            receiver_partial_bytes=receiver.partial_bytes, receiver_E=receiver.deadline_us,
            session_send_E=session.send_end,
            clock_probe_E=None if session.waiting is None else session.waiting.expires_j_us)

    def step(self, pump):
        try:
            # 원 fail/close는 partial/E를 지운다. 직전 scalar만 보존하며 packet은 참조하지 않는다.
            self.positions_before_last_pump = self._positions(pump)
        except BaseException:
            self.dropped = min(2**63-1, self.dropped+1)
        return self.call('pump_step', pump.step)

    def pending_copy(self, issued, expires, phase, frame_E, *, selected):
        self.copy_request = dict(issued_us=issued, expires_us=expires, phase=phase,
            frame_expires_us=frame_E, selected=selected)

    def capture_exit(self, actor, pump, driver, *, source_fenced, request_draft, metadata_draft, start_draft):
        try:
            result = None if pump is None else pump.result
            terminal = None if result is None else dict(reason=result.reason, failure=result.failure,
                counters=result.counts)
            transport = None if pump is None else dict(positions=self._positions(pump),
                sender_stats=pump._session.sender.stats, receiver_stats=pump._session.receiver.stats)
            self.final = dict(source_fenced=source_fenced, actor_state=actor.state,
                driver_phase=None if driver is None else driver.phase, leases_released=actor.leases_released,
                driver_counts={} if driver is None else dict(getattr(driver,'counts',{})),
                last_enqueue_timing=None if driver is None else getattr(driver,'last_enqueue_timing',None),
                nn_counts=dict(getattr(getattr(actor,'nn_source',None),'counts',{})),
                nn_last_unmatched=getattr(getattr(actor,'nn_source',None),'last_unmatched',None),
                nn_last_submission_timing=getattr(getattr(actor,'nn_source',None),'last_submission_timing',None),
                active_frame_expires_us=None if driver is None or driver.active is None else driver.active.expires_us,
                copy_request=self.copy_request, request_draft_pending=request_draft is not None,
                metadata_draft_pending=metadata_draft is not None, start_draft_pending=start_draft is not None,
                pump_result_before_cleanup=terminal, transport_at_loop_exit=transport)
        except BaseException:
            self.dropped = min(2**63-1, self.dropped+1)

    def save_once(self, actor):
        if self.save_attempted:
            return
        self.save_attempted = True
        try:
            value = dict(schema_version=1, profile='pc-acquisition-diagnostic-1', stages=self.stages,
                pump_max_call_gap_us=self.pump_max_gap_us,
                positions_before_last_pump=self.positions_before_last_pump, exit=self.final,
                diagnostic_reads_dropped=self.dropped,
                basis='original j_us values; extra reads/accounting overhead; not timing acceptance',
                state_basis='loop exit may follow original fence/close; pre-pump positions are separate',
                includes_payload_or_lease_evidence=False)
            raw = (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)+'\n').encode('ascii')
            if len(raw) > 16*1024:
                return
            actor.save('pc-acquisition-diagnostic.json', value)
        except BaseException:
            # 진단 실패는 원 STOP/예외나 pc_worker_main의 Device close를 대체하지 않는다.
            pass


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
    diagnostic = _AcquisitionDiagnostic()
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
            expires_us=now + 5_000_000)

        def flush_pc():
            nonlocal start_draft, metadata_draft
            pending = link.pending
            if pending is not None and pending.kind in ('PC_EARLY_META', 'PC_NN_RESULT_META') and j_us() >= pending.expires_us:
                link.discard_expired_work(pending)
                metadata_draft = None
            if metadata_draft is not None and j_us() >= metadata_draft['expires_us']:
                metadata_draft = None
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
            incoming = diagnostic.call('actor_poll', actor.poll)  # 원 STOP은 pc_worker_main에 전파한다.
            for packet in incoming:
                if actor.cancelled_grant(packet, context, settings.slot_limits, PcSlotMessage):continue
                if source_fenced or packet.kind != 'SLOT' or packet.payload.get('op') != 'GRANT' or request is None:
                    raise ValueError('unexpected PC A control statement or grant without original request')
                grant = PcSlotMessage(packet, context, request, limits=settings.slot_limits)
                if j_us() >= request.expires_us or driver.active is None or j_us()+1 >= driver.active.expires_us:
                    actor.cancel_request(request)
                    request = request_draft = None
                    diagnostic.copy_request = None
                    driver.expire()
                    continue
                try:encoding, filled = diagnostic.call('raw_publish', driver.publish, grant.record)
                except FrameExpired:
                    actor.cancel_request(request)
                    request = request_draft = None
                    diagnostic.copy_request = None
                    driver.expire()
                    continue
                out = actor.required_slot(pc_filled_payload(grant, filled), request_id=grant.request_id,
                    issued=filled.completed_us, expires=filled.completed_us + 1_000_000)
                PcSlotMessage(out, context, grant, limits=settings.slot_limits)
                driver.note_filled_sent(diagnostic.call('raw_await_sent', actor.await_sent, out))
                if j_us()+1 >= driver.active.expires_us:
                    metadata_draft = None
                else:metadata_draft = dict(kind='PC_EARLY_META' if encoding.phase == 'EARLY' else 'PC_NN_RESULT_META',
                    metadata=pc_slot_meta_fields(encoding, purpose=driver.active.purpose), body=encoding.descriptor_bytes,
                    request_id=driver.active.job_id, issued_us=filled.completed_us,
                    expires_us=driver.active.expires_us)
                request = request_draft = None
                diagnostic.copy_request = None

            if source_fenced:
                # metadata/새 관측은 진행하지 않고 실제 S STOP과 원 NN drain만 처리한다.
                diagnostic.call('sdk_poll', actor.nn_source.poll)
                continue
            diagnostic.call('metadata_flush', flush_pc)
            for received in diagnostic.call('metadata_poll', link.poll):
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
                diagnostic.call('sdk_poll', actor.nn_source.poll)
                continue

            result = diagnostic.step(pump)
            if result is not None:
                # 수신 owner는 pump가 이미 fence/close했다. S는 이 원 사건으로 Source를 철회한다.
                driver.fence(); source_fenced = True; actor.state = 'DEGRADED'
                if request is not None:
                    actor.cancel_request(request)
                    request = None
                    diagnostic.copy_request = None
                request_draft = metadata_draft = None
                while link.pending is not None:
                    if j_us()>=link.pending.expires_us and link.pending.kind in ('PC_EARLY_META','PC_NN_RESULT_META'):
                        link.discard_expired_work(link.pending)
                        break
                    diagnostic.call('metadata_flush', link.flush)
                    for message in diagnostic.call('actor_poll', actor.poll):
                        if not actor.cancelled_grant(message,context,settings.slot_limits,PcSlotMessage):
                            raise ValueError('unresolved control during source disconnect cleanup')
                    time.sleep(.002)
                stamp = j_us()
                link.send('PC_SOURCE_FENCE', dict(reason=result.reason, failure=result.failure),
                    request_id=str(uuid4()), issued_us=stamp, expires_us=stamp + 5_000_000)
                # full-send는 이 호출의 실제 반환으로만 확인하며 다음 loop에서 draft를 재발급하지 않는다.
                while link.pending is not None:
                    for message in diagnostic.call('actor_poll', actor.poll):
                        if not actor.cancelled_grant(message,context,settings.slot_limits,PcSlotMessage):
                            raise ValueError('unresolved original control arrived after PC source fence')
                    diagnostic.call('metadata_flush', link.flush)
                    time.sleep(.002)
                continue

            if start_draft is not None: continue
            if request is not None:
                if j_us() >= request.expires_us:
                    actor.cancel_request(request)
                    request = None
                    diagnostic.copy_request = None
            elif request_draft is not None and j_us() >= request_draft['expires']:
                request_draft = None
            if request is not None or metadata_draft is not None or link.pending is not None: continue
            driver.expire()
            if driver.waiting_enqueue: diagnostic.call('sdk_enqueue', driver.enqueue)
            diagnostic.call('sdk_poll', driver.poll_nn)
            driver.promote()
            if driver.waiting_enqueue:diagnostic.call('sdk_enqueue',driver.enqueue)
            job = driver.active
            if job is not None and driver.phase in ('EARLY', 'NN'):
                if request_draft is None:
                    issued = j_us()
                    request_draft = dict(payload=pc_write_request_payload(binding, actor.boot.raw_scope,
                        job_id=job.job_id, phase=driver.phase, expires_j_us=job.expires_us, full_key=job.full_key,
                        pc_frame_proof_sha256=job.proof_sha256, purpose=job.purpose), request_id=str(uuid4()),
                        issued=issued, expires=issued + settings.slot_limits.copy_budget_us)
                    diagnostic.pending_copy(request_draft['issued'], request_draft['expires'],
                        driver.phase, job.expires_us, selected=False)
                out = actor.slot(**request_draft)
                if out is not None:
                    request = PcSlotMessage(out, context, limits=settings.slot_limits)
                    diagnostic.pending_copy(request.record.issued_us, request.record.expires_us,
                        driver.phase, job.expires_us, selected=True)
                    request_draft = None
    finally:
        diagnostic.capture_exit(actor, pump, driver, source_fenced=source_fenced,
            request_draft=request_draft, metadata_draft=metadata_draft, start_draft=start_draft)
        try:
            try:
                if pump is not None: pump.close()
            finally:
                try:
                    if driver is not None: driver.fence()
                finally:
                    if link is not None: link.close()
                    else: settings.metadata_endpoint.close()
                    if pump is None: settings.observation_endpoint.close()
        finally:
            diagnostic.save_once(actor)
