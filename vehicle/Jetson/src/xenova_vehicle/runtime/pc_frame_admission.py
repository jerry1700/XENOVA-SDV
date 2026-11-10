"""등록 M-main의 실제 S relay/owned PC slot을 local opaque 입력으로 채택한다.

Root가 실제 BIND/ACK 뒤 원 reader context/link를 공급한다. 순수 descriptor나 bool은
수락되지 않는다. 주입한 실제 clock 모듈의 원 두 표본 재생은 A/S 증거의 수학 검산이며
M이 J4를 새로 관측했다는 뜻이 아니다. source/NN/Scene/World/READY의 최종 권한은
상위 actor가 재검산한다. 이 owner는 socket, SHM, SDK, thread를 만들거나 닫지 않는다.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import sys
from threading import get_ident
from types import ModuleType

from ..perception.inputs import FrameStamp, ImageFrame, PerceptionInput
from ..simulation.pc_input import PcFrameInput, _PcInputIssuer
from .pc_channel import PcActorLink, PcActorReceived, PcActorSent, decode_forward
from .pc_job_messages import PcSnapshotAck
from .pc_slot import PcOwnedSlot
from .pc_slot_messages import PcReaderSlotContext, parse_pc_slot_descriptor


U63_MAX = (1 << 63) - 1


class PcFrameAdmissionError(ValueError):
    """원 등록·relay·clock·frame·ACK 결합이 맞지 않는다. fallback 승인은 없다."""


def _need(value, reason):
    if not value: raise PcFrameAdmissionError(reason)


def _integer(value):
    _need(type(value) is int and 0 <= value < U63_MAX, 'PC_ADMISSION_NATIVE_J_REQUIRED')
    return value


@dataclass(frozen=True, slots=True, init=False)
class _AcceptedProof:
    owner_token: object
    lease: object
    received: PcActorReceived
    forward: object
    job_id: str
    purpose: str
    write_key: object
    proof_sha256: str
    revision: tuple

    def __init__(self, *args, **kwargs): raise TypeError('actual M admission seal required')


@dataclass(frozen=True, slots=True, init=False)
class PcNnAdmission:
    """원 ACK/relay와 결합한 NN facts다. decode와 최종 current 검사는 caller 책임이다."""
    frame: PcFrameInput
    job_id: str
    purpose: str
    expires_j_us: int
    nn_input: object
    nn_output: object
    nn_key: tuple
    slot_descriptor_sha256: str
    filled_completed_j_us: int
    snapshot_ack_sent: PcActorSent
    received: PcActorReceived
    forward: object

    def __init__(self, *args, **kwargs): raise TypeError('original M NN admission required')

    def with_candidates(self, candidates):
        """compute에서 실제 decode 결과만 원 RGB/RIGHT에 붙인다. 새 view/E는 만들지 않는다."""
        from ..perception.yolop import YolopCandidates
        _need(type(candidates) is YolopCandidates and candidates.packet is self.nn_output,
            'PC_ORIGINAL_NN_CANDIDATES_REQUIRED')
        old = self.frame.observation
        joined = PerceptionInput(old.kind, old.calibration_id, old.rgb, old.depth, candidates, self.nn_key)
        return self.frame.with_observation(joined)


def _fact(cls, **fields):
    result = object.__new__(cls)
    for name, value in fields.items(): object.__setattr__(result, name, value)
    return result


class PcFrameAdmission:
    """M-main owner 하나의 issuer/HWM/fence다. frame payload cache를 추가하지 않는다.

    Root가 active/pending 입력 보유량을 제한하고, 실제 A/S fence나 EOF에 fence를 호출한다.
    정상 clock 갱신·후속 EARLY는 이전 frame을 철회하지 않는다. 최종 사용 때 같은 owner의
    최신 current_view/실패 None을 공급하며 과거 view를 현재 승인으로 재사용하지 않는다.
    """

    def __init__(self, reader_context, link, *, clock_api, clock_policy):
        _need(type(reader_context) is PcReaderSlotContext and type(link) is PcActorLink,
            'PC_REGISTERED_READER_AND_LINK_REQUIRED')
        _need(type(clock_api) is ModuleType and clock_api.__name__ == 'tools.hil_observation.clock'
            and sys.modules.get(clock_api.__name__) is clock_api
            and type(clock_policy) is clock_api.ClockPolicy, 'PC_ACTUAL_COHERENT_CLOCK_API_REQUIRED')
        binding = reader_context.bind.binding
        _need(link.owner_role == 'M' and link.peer == reader_context.bind.context.peer
            and link.binding.canonical_bytes == binding.canonical_bytes, 'PC_CURRENT_M_ENDPOINT_DIFFERS')
        link._open()
        self._context, self._link, self._binding = reader_context, link, binding
        self._clock_api, self._policy = clock_api, clock_policy
        self._issuer = _PcInputIssuer(binding)
        self._thread = get_ident()
        self._last_now = None
        self._frame_sequence = None
        self._a_heads = {}
        self._source_revision = None
        self._clock_samples = ()
        self._fenced = False
        self._fence_revision = 0
        self._reason = None

    @property
    def owner_token(self): return self._issuer.owner_token

    @property
    def binding(self): return self._binding

    @property
    def fence_revision(self): return self._fence_revision

    @property
    def reason(self): return self._reason

    def _owner(self):
        if get_ident() != self._thread: raise RuntimeError('PC admission belongs to the original M-main owner')

    def fence(self, reason):
        self._owner()
        _need(type(reason) is str and 0 < len(reason) <= 128 and reason.isascii(), 'PC_FENCE_REASON_REQUIRED')
        if not self._fenced:
            self._fenced = True
            self._fence_revision += 1
        self._reason = reason

    def _current(self, now):
        self._owner(); _integer(now)
        _need(self._last_now is None or now >= self._last_now, 'PC_LOCAL_J_REGRESSION')
        self._last_now = now
        _need(not self._fenced, 'PC_SOURCE_FENCED')
        try: self._link._open()
        except (EOFError, ValueError):
            self.fence('PC_SOURCE_CHANNEL_UNAVAILABLE')
            raise

    def _event(self, received, owned, phase, now):
        self._current(now)
        self._link.require_received(received)
        _need(type(owned) is PcOwnedSlot, 'PC_ACTUAL_OWNED_SLOT_REQUIRED')
        forward = decode_forward(received.packet, self._context.writer.bind.context.peer, self.binding)
        original = forward.original_packet
        expected_kind = 'PC_EARLY_META' if phase == 'EARLY' else 'PC_NN_RESULT_META'
        _need(original.kind == expected_kind, 'PC_ORIGINAL_A_PHASE_DIFFERS')
        metadata = original.metadata
        _need(set(metadata) == {'profile', 'purpose', 'descriptor_sha256'}
            and metadata['profile'] == 'pc-slot-meta-1' and metadata['purpose'] in ('PARSER_WARMUP', 'METRIC_MAP')
            and metadata['descriptor_sha256'] == owned.descriptor_sha256 == sha256(original.body).hexdigest()
            and original.body == owned.descriptor_bytes, 'PC_ORIGINAL_A_DESCRIPTOR_DIFFERS')
        descriptor = parse_pc_slot_descriptor(original.body, self.binding)
        filled = owned.expected.original_filled_info
        _need(owned.context.write_key.scope == self._context.writer.scope
            and filled.key == owned.context.write_key and filled.descriptor_sha256 == descriptor.sha256
            and filled.payload_length == descriptor.payload_length == owned.payload_length
            and (descriptor.job_id, descriptor.phase, descriptor.expires_j_us, descriptor.full_key, descriptor.proof_sha256)
                == (owned.job_id, phase, owned.expires_j_us, owned.full_key, owned.expected.pc_frame_proof_sha256)
            and original.request_id == owned.job_id and original.expires_us == owned.expires_j_us,
            'PC_ORIGINAL_FILLED_JOB_KEY_PROOF_DIFFERS')
        _need(filled.completed_us <= original.issued_us <= forward.received_j_us <= received.received_j_us <= now,
            'PC_ORIGINAL_FORWARD_LIFETIME_DIFFERS')
        if now+1>=owned.expires_j_us:
            from .health_policy import FrameExpired
            raise FrameExpired('PC frame expired before admission')
        digest = sha256(original.raw).hexdigest()
        previous = self._a_heads.get(phase)
        if previous is not None and original.sequence <= previous[0]:
            if original.sequence == previous[0] and digest == previous[1]: return None
            if original.sequence == previous[0]: self.fence('PC_A_EVENT_CONFLICT')
            raise PcFrameAdmissionError('PC_ORIGINAL_A_EVENT_REPLAY')
        return forward, descriptor, metadata['purpose'], digest

    def _clock(self, owned, issued):
        """원 A 표본만 재생한다. M 수신시각으로 J1/J4/support/E를 다시 만들지 않는다."""
        proof = owned.descriptor()['proof']; api = self._clock_api
        policy = api.ClockPolicy(**proof['clock']['policy'])
        _need(policy == self._policy and proof['clock']['policy'] == asdict(self._policy), 'PC_PINNED_CLOCK_POLICY_DIFFERS')
        fields = self.binding.fields
        source = api.ClockSource(fields['source_id'], fields['pc_clock_epoch'], fields['jetson_session'])
        samples = tuple((api.ClockReply.from_fields(row['reply']), row['j4_us']) for row in proof['clock']['samples'])
        seed = ()
        if self._clock_samples:
            old_last = self._clock_samples[-1]
            old_reply = api.ClockReply.from_raw(old_last[0])
            old_seq, new_seq = old_reply.probe.sequence, samples[-1][0].probe.sequence
            _need(new_seq >= old_seq, 'PC_ORIGINAL_CLOCK_SEQUENCE_REPLAY')
            if new_seq == old_seq:
                _need(tuple((r.raw_bytes, j4) for r, j4 in samples) == self._clock_samples,
                    'PC_ORIGINAL_CLOCK_REPLY_OR_J4_CONFLICT')
            else:
                _need(samples[0][0].probe.sequence >= old_seq, 'PC_ORIGINAL_CLOCK_PAIR_REGRESSION')
                if samples[0][0].probe.sequence == old_seq:
                    _need((samples[0][0].raw_bytes, samples[0][1]) == old_last,
                        'PC_ORIGINAL_CLOCK_REPLY_OR_J4_CONFLICT')
                else:
                    seed = ((old_reply, old_last[1]),)
        replay = api.HilObservationClock(source, self._policy)
        lease = None
        for value, original_j4 in seed + samples:
            probe = value.probe
            _need(value.probe.source == source and original_j4 <= issued, 'PC_ORIGINAL_CLOCK_SOURCE_OR_J4_DIFFERS')
            requested = replay.probe(probe.sequence, probe.nonce, probe.j1_us, probe.expires_j_us)
            _need(requested.raw_bytes == probe.raw_bytes, 'PC_ORIGINAL_CLOCK_PROBE_DIFFERS')
            lease = replay.accept(value, original_j4)
        _need(lease is not None and replay.lease_evidence is not None, 'PC_TWO_ORIGINAL_CLOCK_SAMPLES_REQUIRED')
        header = proof['packet_header']
        rgb, depth = (lease.map_interval(*header[name]['pc_interval_us'], now_j=issued) for name in ('rgb', 'depth'))
        expires = min(lease.deadline_lower(header['expires_pc_us'], issued), min(rgb[0], depth[0]) + 500_000)
        _need(proof['mapped_j_us'] == [list(rgb), list(depth)] and expires == owned.expires_j_us
            and max(rgb[1], depth[1]) <= proof['frame_received_j_us'], 'PC_ORIGINAL_CLOCK_MAPPING_OR_E_DIFFERS')
        return lease, replay.lease_evidence

    def admit_early(self, received, owned, *, now_us):
        """실제 S 전달과 원 EARLY를 재검산한 뒤 M-local 입력만 발급한다. duplicate는 None이다."""
        event = self._event(received, owned, 'EARLY', now_us)
        if event is None: return None
        forward, descriptor, purpose, digest = event
        if self._frame_sequence is not None and owned.full_key[3] <= self._frame_sequence:
            self.fence('PC_CAPTURE_SEQUENCE_REPLAY')
            raise PcFrameAdmissionError('PC_ORIGINAL_CAPTURE_SEQUENCE_REPLAY')
        try: lease, evidence = self._clock(owned, forward.original_packet.issued_us)
        except ValueError:
            self.fence('PC_CLOCK_PROOF_CONTRADICTION')
            raise
        proof = owned.descriptor()['proof']; revision = tuple(proof['revision'])
        if self._source_revision is not None and self._source_revision != revision:
            self.fence('PC_A_CLOCK_OR_SOURCE_REVISION_CHANGED')
            raise PcFrameAdmissionError('PC_A_CLOCK_OR_SOURCE_REVISION_CHANGED')
        fields = self.binding.fields
        images = []
        for index, (name, height, encoding, payload) in enumerate((('rgb_video', 360, 'NV12', owned.rgb),
                ('depth_right', 480, 'RAW16_LE', owned.depth))):
            images.append(ImageFrame(FrameStamp(self.binding.source_id, fields['stream_instances'][name], owned.full_key[3],
                self.binding.clock_id, tuple(proof['mapped_j_us'][index]), proof['frame_received_j_us']), 640, height, encoding, payload))
        observation = PerceptionInput('SYNTHETIC', fields['calibration_profile_sha256'], *images)
        seal = _fact(_AcceptedProof, owner_token=self.owner_token, lease=lease, received=received, forward=forward,
            job_id=owned.job_id, purpose=purpose, write_key=owned.context.write_key,
            proof_sha256=descriptor.proof_sha256, revision=revision)
        header = proof['packet_header']
        frame = self._issuer.issue(observation=observation, proof_bytes=owned.pc_frame_proof,
            packet_header_bytes=owned.pc_packet_header, packet_sha256=proof['packet_sha256'],
            rgb_pc_interval=tuple(header['rgb']['pc_interval_us']), depth_pc_interval=tuple(header['depth']['pc_interval_us']),
            mapped_rgb_j_us=tuple(proof['mapped_j_us'][0]), mapped_depth_j_us=tuple(proof['mapped_j_us'][1]),
            frame_received_j_us=proof['frame_received_j_us'], expires_j_us=owned.expires_j_us,
            clock_probe_bytes=tuple(s.reply.probe.raw_bytes for s in evidence.samples),
            clock_reply_bytes=tuple(s.reply.raw_bytes for s in evidence.samples),
            clock_j4_us=tuple(s.j4_us for s in evidence.samples), lease_token=seal, revision=revision)
        self._source_revision = revision; self._frame_sequence = owned.full_key[3]
        self._clock_samples = tuple((s.reply.raw_bytes, s.j4_us) for s in evidence.samples)
        self._a_heads['EARLY'] = forward.original_packet.sequence, digest
        return frame

    def _original(self, frame):
        _need(type(frame) is PcFrameInput and frame._issuer is self._issuer
            and type(frame._lease_token) is _AcceptedProof and frame._lease_token.owner_token is self.owner_token,
            'PC_ORIGINAL_M_ISSUED_FRAME_REQUIRED')
        return frame._lease_token

    def current_view(self, frame, *, now_us):
        self._current(now_us)
        seal = self._original(frame); frame.validate_original()
        _need(seal.revision == self._source_revision == frame._revision
            and seal.proof_sha256 == sha256(frame.proof_bytes).hexdigest(), 'PC_ORIGINAL_FRAME_OR_FENCE_EXPIRED')
        if now_us+1>=frame.expires_j_us:
            from .health_policy import FrameExpired
            raise FrameExpired('PC frame expired before current admission')
        _need(seal.lease.map_interval(*frame.rgb_pc_interval, now_j=now_us) == frame.mapped_rgb_j_us
            and seal.lease.map_interval(*frame.depth_pc_interval, now_j=now_us) == frame.mapped_depth_j_us
            and frame.expires_j_us == min(seal.lease.deadline_lower(frame.expires_pc_us, now_us),
                min(frame.mapped_rgb_j_us[0], frame.mapped_depth_j_us[0]) + 500_000), 'PC_ORIGINAL_FRAME_MAPPING_DIFFERS')
        return self._issuer.view(frame, now_us)

    def admit_nn(self, original_early, received, owned, *, snapshot_ack_sent, now_us):
        self.current_view(original_early, now_us=now_us)
        seal = self._original(original_early)
        _need(original_early.observation.nn is None, 'PC_ORIGINAL_EARLY_FRAME_REQUIRED')
        event = self._event(received, owned, 'NN', now_us)
        if event is None: return None
        forward, descriptor, purpose, digest = event
        self._link.require_sent(snapshot_ack_sent)
        ack = PcSnapshotAck(snapshot_ack_sent.packet, self.binding)
        ack.validate_original(job_id=seal.job_id, expires_us=original_early.expires_j_us,
            full_key=original_early.observation.rgb.stamp.key, pc_frame_proof_sha256=seal.proof_sha256,
            purpose=seal.purpose, now_us=now_us)
        _need((owned.job_id, owned.full_key, owned.expires_j_us, owned.pc_frame_proof, purpose) ==
            (seal.job_id, original_early.observation.rgb.stamp.key, original_early.expires_j_us, original_early.proof_bytes, seal.purpose)
            and owned.context.write_key != seal.write_key
            and (owned.context.write_key.slot != seal.write_key.slot or owned.context.write_key.generation > seal.write_key.generation)
            and descriptor.nn_key == owned.nn_key and owned.nn_input is not None and owned.nn_output is not None,
            'PC_ORIGINAL_EARLY_NN_SLOT_JOIN_DIFFERS')
        # completed는 syscall 뒤 상한이다. peer NN 표본과 두 상한의 순서를 강제하지 않는다.
        _need(snapshot_ack_sent.started_us <= min(owned.nn_input.received_j_us, owned.nn_output.received_j_us)
            and snapshot_ack_sent.completed_us <= now_us, 'PC_ORIGINAL_SNAPSHOT_ACK_SEND_DIFFERS')
        result = _fact(PcNnAdmission, frame=original_early, job_id=owned.job_id, purpose=purpose,
            expires_j_us=owned.expires_j_us, nn_input=owned.nn_input, nn_output=owned.nn_output, nn_key=owned.nn_key,
            slot_descriptor_sha256=owned.descriptor_sha256,
            filled_completed_j_us=owned.expected.original_filled_info.completed_us, snapshot_ack_sent=snapshot_ack_sent,
            received=received, forward=forward)
        self._a_heads['NN'] = forward.original_packet.sequence, digest
        return result
