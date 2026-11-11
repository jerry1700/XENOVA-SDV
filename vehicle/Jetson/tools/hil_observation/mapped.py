"""실제 transport/clock owner 자료를 SYNTHETIC core carrier로 옮기는 개발 factory다.

tools→src 의존만 둔다. 원 PC endpoint/bytes와 원 두 clock 교환을 보존하며 SDK/GT/
same-host WallCapture를 만들지 않는다. source 등록·독립 D 검산·READY는 후속 경계다.
"""

from dataclasses import asdict
from hashlib import sha256
import json
import threading

from xenova_vehicle.perception.inputs import FrameStamp, ImageFrame, PerceptionInput
from xenova_vehicle.simulation.pc_input import PcSourceBinding, PcFrameInput, _PcInputIssuer
from .clock import ClockSource, HilObservationClock, ClockLeaseEvidence
from .transport import SessionScope, ObservationFrame, ReceivedPacket


class PcMappingError(ValueError):
    """원 PC 입력/clock/기한/현재 source가 불충분하다. 새 시각으로 우회하지 않는다."""


def _need(value, message):
    if not value:
        raise PcMappingError(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


class MappedPcOwner:
    """main owner의 capability 발급기다. compute에는 immutable frame/view만 전달한다.

    caller는 매 clock/source 사건 뒤 최신 current_view를 최종 채택에 공급한다. 정상 lease
    renewal은 원 frame의 lease/E를 바꾸지 않는다. EOF는 fence 후 새 session/owner가 필요하다.
    """

    def __init__(self, binding, scope, clock_owner, *, destination_clock_id):
        _need(type(binding) is PcSourceBinding and type(scope) is SessionScope
            and type(clock_owner) is HilObservationClock, 'actual typed PC binding/scope/clock owner required')
        value = binding.fields
        expected = dict(source_id=value['source_id'], session_id=value['jetson_session'],
            source_descriptor_sha256=binding.sha256, calibration_sha256=value['calibration_profile_sha256'],
            motion_bundle_sha256=value['motion_profile_sha256'], pc_clock_epoch=value['pc_clock_epoch'])
        _need(scope.fields == expected and destination_clock_id == binding.clock_id, 'actual PC scope/destination J domain differs')
        self._binding, self._scope, self._clock_owner = binding, scope, clock_owner
        self._source = ClockSource(value['source_id'], value['pc_clock_epoch'], value['jetson_session'])
        _need(clock_owner.source == self._source, 'PC clock owner source differs')
        self._issuer = _PcInputIssuer(binding)
        self._thread = threading.get_ident()
        self._revision = 0
        self._fenced = False
        self._head = None

    @property
    def owner_token(self):
        return self._issuer.owner_token

    @property
    def binding(self):
        return self._binding

    @property
    def scope(self):
        return self._scope

    @property
    def clock_owner(self):
        return self._clock_owner

    def _owned(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('PC mapping factory belongs to original main owner')

    def fence(self):
        """연결/source 손실의 local fence다. 실제 socket 종료나 source 재등록을 주장하지 않는다."""
        self._owned()
        if not self._fenced:
            self._revision += 1
            self._fenced = True

    def _current(self, now):
        self._owned()
        _need(type(now) is int and 0 <= now < 2**63 - 1 and not self._fenced, 'PC owner fenced or invalid J time')
        _need(self.clock_owner.source == self._source, 'PC clock source/session changed')
        evidence = self.clock_owner.lease_evidence
        _need(type(evidence) is ClockLeaseEvidence and len(evidence.samples) == 2
            and evidence.lease is self.clock_owner.lease
            and evidence.fence_revision == self.clock_owner.fence_revision,
            'original two accepted clock samples required')
        _need(evidence.lease.j4_us <= now and now + 1 < evidence.lease.expires_j_us, 'current PC clock lease unavailable')
        return evidence

    def accept_frame(self, received, *, now_j_us):
        evidence = self._current(now_j_us)
        _need(type(received) is ReceivedPacket and type(received.packet) is ObservationFrame
            and received.packet.scope.raw_bytes == self.scope.raw_bytes
            and received.received_local_us <= now_j_us, 'actual original complete PC frame required')
        packet = received.packet
        _need(self._head is None or packet.sequence > self._head, 'PC frame sequence cannot be replayed or retimed')
        self._head = packet.sequence  # 매핑 실패도 이미 수신한 원 순번/E를 환급하지 않는다.
        lease = evidence.lease
        rgb = lease.map_interval(*packet.rgb_pc_interval, now_j_us)
        depth = lease.map_interval(*packet.depth_pc_interval, now_j_us)
        full_lower, full_upper = min(rgb[0], depth[0]), max(rgb[1], depth[1])
        expires = min(lease.deadline_lower(packet.expires_pc_us, now_j_us), full_lower + 500_000)
        _need(full_upper <= received.received_local_us and full_upper < expires and now_j_us + 1 < expires,
            'mapped capture is future, unavailable, or past its original E')
        fields = self.binding.fields
        source, clock_id = self.binding.source_id, self.binding.clock_id
        original = PerceptionInput('SYNTHETIC', fields['calibration_profile_sha256'],
            ImageFrame(FrameStamp(source, fields['stream_instances']['rgb_video'], packet.sequence, clock_id,
                rgb, received.received_local_us), 640, 360, 'NV12', packet.rgb, None),
            ImageFrame(FrameStamp(source, fields['stream_instances']['depth_right'], packet.sequence, clock_id,
                depth, received.received_local_us), 640, 480, 'RAW16_LE', packet.depth, None))
        checksum = sha256()
        for part in packet.wire_parts:
            checksum.update(part)
        packet_sha = checksum.hexdigest()
        probes = tuple(sample.reply.probe.raw_bytes for sample in evidence.samples)
        replies = tuple(sample.reply.raw_bytes for sample in evidence.samples)
        j4 = tuple(sample.j4_us for sample in evidence.samples)
        # 원 canonical 객체의 재직렬화가 원 bytes와 같은지 먼저 검산한다.
        header = json.loads(packet.header_bytes)
        _need(_canonical(header) == packet.header_bytes, 'packet header is not exact canonical bytes')
        samples = []
        for sample in evidence.samples:
            body = sample.reply.fields
            _need(_canonical(body) == sample.reply.raw_bytes, 'original clock reply bytes differ')
            samples.append(dict(reply=body, j4_us=sample.j4_us))
        revision = self._revision, self.clock_owner.fence_revision
        proof = dict(profile='pc-frame-input-1', binding_sha256=self.binding.sha256,
            packet_header=header, packet_sha256=packet_sha, clock=dict(policy=asdict(self.clock_owner.policy), samples=samples),
            mapped_j_us=[list(rgb), list(depth)], frame_received_j_us=received.received_local_us,
            expires_j_us=expires, destination_clock_id=clock_id, revision=list(revision))
        proof_raw = _canonical(proof)
        result = self._issuer.issue(observation=original, proof_bytes=proof_raw,
            packet_header_bytes=packet.header_bytes, packet_sha256=packet_sha,
            rgb_pc_interval=packet.rgb_pc_interval, depth_pc_interval=packet.depth_pc_interval,
            mapped_rgb_j_us=rgb, mapped_depth_j_us=depth, frame_received_j_us=received.received_local_us,
            expires_j_us=expires, clock_probe_bytes=probes, clock_reply_bytes=replies, clock_j4_us=j4,
            lease_token=evidence, revision=revision)
        return result

    def current_view(self, pc_frame, *, now_j_us):
        self._current(now_j_us)
        _need(type(pc_frame) is PcFrameInput and pc_frame._issuer is self._issuer
            and pc_frame.binding.canonical_bytes == self.binding.canonical_bytes,
            'original issued PC frame required')
        evidence = pc_frame._lease_token
        _need(type(evidence) is ClockLeaseEvidence and evidence.lease.source == self._source
            and pc_frame._revision == (self._revision, self.clock_owner.fence_revision)
            and evidence.fence_revision == self.clock_owner.fence_revision,
            'original PC clock/source fence changed')
        lease = evidence.lease
        _need(lease.map_interval(*pc_frame.rgb_pc_interval, now_j_us) == pc_frame.mapped_rgb_j_us
            and lease.map_interval(*pc_frame.depth_pc_interval, now_j_us) == pc_frame.mapped_depth_j_us
            and pc_frame.expires_j_us == min(lease.deadline_lower(pc_frame.expires_pc_us, now_j_us),
                min(pc_frame.mapped_rgb_j_us[0], pc_frame.mapped_depth_j_us[0]) + 500_000)
            and now_j_us + 1 < pc_frame.expires_j_us, 'original PC mapping/E no longer current')
        return self._issuer.view(pc_frame, now_j_us)
