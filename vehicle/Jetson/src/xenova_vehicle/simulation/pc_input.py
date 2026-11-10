"""PC 매핑 입력의 불변 carrier. tools/소켓/시계 승인을 import하거나 실행하지 않는다.

등록된 tools owner의 opaque token과 현재 view가 별도로 필요하다. descriptor·JSON·bool
자체는 clock 수락이나 S/A/M/D 등록·READY가 아니다. same-host/physical binding과 별개다.
"""

from hashlib import sha256
import json
from uuid import UUID

from ..perception.inputs import PerceptionInput


MAX_PROOF_BYTES = 16 * 1024
FIELDS = {'profile', 'service_instance', 'source_id', 'pc_clock_epoch', 'jetson_session',
          'stream_instances', 'destination_clock_epoch', 'calibration_profile_sha256',
          'motion_profile_sha256', 'nn_manifest_sha256'}


def _need(value, message):
    if not value:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _json(raw, cap):
    _need(type(raw) is bytes and 0 < len(raw) <= cap, 'bounded original canonical bytes required')
    def pairs(rows):
        value = {}
        for key, child in rows:
            _need(key not in value, 'duplicate PC proof key')
            value[key] = child
        return value
    def constant(_):
        raise ValueError('nonfinite PC proof')
    value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs, parse_constant=constant)
    _need(type(value) is dict and _canonical(value) == raw, 'noncanonical PC proof')
    return value


def _uuid(value):
    _need(type(value) is str and len(value) == 36, 'original UUID required')
    parsed = UUID(value)
    _need(str(parsed) == value and parsed.int != 0, 'canonical nonzero UUID required')


def _sha(value):
    _need(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value), 'canonical SHA required')


class _Frozen:
    __slots__ = ()

    def __setattr__(self, name, value):
        raise AttributeError('immutable PC input')


class PcSourceBinding(_Frozen):
    """exact10 PC descriptor다. service/clock/NN 실존·등록은 caller가 별도로 검산한다."""
    __slots__ = ('canonical_bytes', 'sha256')

    def __init__(self, fields):
        _need(type(fields) is dict and set(fields) == FIELDS and fields['profile'] == 'development-pc-source-1',
            'exact development PC binding required')
        for name in ('service_instance', 'source_id', 'pc_clock_epoch', 'jetson_session', 'destination_clock_epoch'):
            _uuid(fields[name])
        streams = fields['stream_instances']
        _need(type(streams) is dict and set(streams) == {'rgb_video', 'depth_right'}, 'exact PC stream UUID pair required')
        for value in streams.values():
            _uuid(value)
        _need(streams['rgb_video'] != streams['depth_right'], 'PC streams must differ')
        for name in ('calibration_profile_sha256', 'motion_profile_sha256', 'nn_manifest_sha256'):
            _sha(fields[name])
        raw = _canonical(fields)
        _need(len(raw) <= 2048, 'PC descriptor exceeds2048 bytes')
        object.__setattr__(self, 'canonical_bytes', raw)
        object.__setattr__(self, 'sha256', sha256(raw).hexdigest())

    @classmethod
    def from_bytes(cls, raw):
        return cls(_json(raw, 2048))

    @classmethod
    def from_fields(cls, fields):
        return cls(fields)

    @property
    def fields(self):
        return json.loads(self.canonical_bytes)

    @property
    def source_id(self):
        return 'sim-pc:' + self.fields['source_id']

    @property
    def clock_id(self):
        return 'jetson-monotonic:' + self.fields['destination_clock_epoch']

    def check_profile(self, motion_profile):
        from .motion_profile import DevelopmentMotionProfile
        _need(type(motion_profile) is DevelopmentMotionProfile
            and motion_profile.bound_calibration.kind == 'SYNTHETIC'
            and motion_profile.bound_calibration.sha256 == self.fields['calibration_profile_sha256']
            and motion_profile.sha256 == self.fields['motion_profile_sha256'], 'PC binding differs from original motion/calibration')


class PcCurrentView(_Frozen):
    """원 owner가 현재 검산한 frame/lease view다. caller는 최종 채택 때 최신 view를 공급한다."""
    __slots__ = ('_issuer', '_frame_token', '_lease_token', '_revision', 'checked_j_us', 'expires_j_us')

    def __init__(self, *args, **kwargs):
        raise TypeError('PC current view requires original owner issuance')


class PcFrameInput(_Frozen):
    """원 payload는 관측에만 두고 snapshot에는 작은 proof_bytes만 전달한다."""
    __slots__ = ('binding', 'observation', 'proof_bytes', 'expires_j_us', 'packet_header_bytes',
                 'packet_sha256', 'rgb_pc_interval', 'depth_pc_interval', 'mapped_rgb_j_us', 'mapped_depth_j_us',
                 'frame_received_j_us', 'clock_probe_bytes', 'clock_reply_bytes', 'clock_j4_us',
                 '_issuer', '_frame_token', '_lease_token', '_revision', '_rgb_payload', '_depth_payload',
                 '_rgb_sha256', '_depth_sha256', '_validation_token')

    def __init__(self, *args, **kwargs):
        raise TypeError('PC input requires original mapped-owner issuance')

    @property
    def source_binding_sha256(self):
        return self.binding.sha256

    @property
    def proof_fields(self):
        return json.loads(self.proof_bytes)

    @property
    def expires_pc_us(self):
        return json.loads(self.packet_header_bytes)['expires_pc_us']

    def validate_original(self):
        """작은 원문 closure를 재검산한다. payload는 발급 때 hash한 immutable 원객체여야 한다."""
        proof = _json(self.proof_bytes, MAX_PROOF_BYTES)
        header = _json(self.packet_header_bytes, 16 * 1024)
        _need(set(proof) == {'profile', 'binding_sha256', 'packet_header', 'packet_sha256', 'clock',
            'mapped_j_us', 'frame_received_j_us', 'expires_j_us', 'destination_clock_id', 'revision'}
            and set(header) == {'profile', 'kind', 'sequence', 'scope', 'rgb', 'depth',
                'render_completed_pc_us', 'expires_pc_us'}
            and header['profile'] == 'hil-observation-1' and header['kind'] == 'FRAME'
            and set(proof['clock']) == {'policy', 'samples'}
            and set(proof['clock']['policy']) == {'max_rtt_us', 'max_offset_width_us', 'drift_ppm', 'lease_us', 'lookback_us'},
            'exact PC mapping proof required')
        for name, height, encoding in (('rgb', 360, 'NV12'), ('depth', 480, 'RAW16LE')):
            image = header[name]
            _need(set(image) == {'width', 'height', 'encoding', 'pc_interval_us', 'sha256'}
                and type(image['width']) is int and image['width'] == 640
                and type(image['height']) is int and image['height'] == height and image['encoding'] == encoding,
                'original fixed PC format required')
        fields = self.binding.fields
        scope = dict(source_id=fields['source_id'], session_id=fields['jetson_session'],
            source_descriptor_sha256=self.binding.sha256, calibration_sha256=fields['calibration_profile_sha256'],
            motion_bundle_sha256=fields['motion_profile_sha256'], pc_clock_epoch=fields['pc_clock_epoch'])
        observed = self.observation
        _need(proof['profile'] == 'pc-frame-input-1' and proof['binding_sha256'] == self.binding.sha256
            and proof['packet_header'] == header and proof['packet_sha256'] == self.packet_sha256
            and proof['mapped_j_us'] == [list(self.mapped_rgb_j_us), list(self.mapped_depth_j_us)]
            and proof['frame_received_j_us'] == self.frame_received_j_us
            and proof['expires_j_us'] == self.expires_j_us and proof['destination_clock_id'] == self.binding.clock_id
            and proof['revision'] == list(self._revision) and header['scope'] == scope
            and header['rgb']['pc_interval_us'] == list(self.rgb_pc_interval)
            and header['depth']['pc_interval_us'] == list(self.depth_pc_interval)
            and header['rgb']['sha256'] == self._rgb_sha256 and header['depth']['sha256'] == self._depth_sha256
            and observed.kind == 'SYNTHETIC' and observed.calibration_id == fields['calibration_profile_sha256']
            and observed.rgb.payload is self._rgb_payload and observed.depth is not None
            and observed.depth.payload is self._depth_payload
            and observed.rgb.stamp.source_id == observed.depth.stamp.source_id == self.binding.source_id
            and observed.rgb.stamp.clock_id == observed.depth.stamp.clock_id == self.binding.clock_id
            and observed.rgb.stamp.stream_id == fields['stream_instances']['rgb_video']
            and observed.depth.stamp.stream_id == fields['stream_instances']['depth_right']
            and observed.rgb.stamp.sequence == observed.depth.stamp.sequence == header['sequence']
            and observed.rgb.stamp.captured_us == self.mapped_rgb_j_us
            and observed.depth.stamp.captured_us == self.mapped_depth_j_us
            and observed.rgb.stamp.available_us == observed.depth.stamp.available_us == self.frame_received_j_us,
            'original PC frame/header/mapped interval closure differs')
        samples = proof['clock']['samples']
        _need(type(samples) is list and len(samples) == 2, 'exact original clock sample pair required')
        source = dict(source_id=fields['source_id'], pc_session=fields['pc_clock_epoch'], jetson_session=fields['jetson_session'])
        for index, sample in enumerate(samples):
            raw_reply = self.clock_reply_bytes[index]
            reply = _json(raw_reply, 1024)
            _need(set(reply) == {'profile', 'probe', 'p2_us', 'p3_us'}
                and reply['profile'] == 'hil-observation-clock-reply-1'
                and set(reply['probe']) == {'profile', 'source', 'sequence', 'nonce', 'j1_us', 'expires_j_us'}
                and reply['probe']['profile'] == 'hil-observation-clock-probe-1'
                and sample == dict(reply=reply, j4_us=self.clock_j4_us[index])
                and _canonical(reply['probe']) == self.clock_probe_bytes[index]
                and reply['probe']['source'] == source, 'original clock raw/probe/reply/J4 closure differs')

    def with_observation(self, joined):
        if joined is self.observation:
            return self
        original = self.observation
        _need(type(joined) is PerceptionInput and joined.kind == 'SYNTHETIC'
            and joined.calibration_id == original.calibration_id and joined.rgb is original.rgb
            and joined.depth is original.depth and original.nn is None and joined.nn is not None,
            'only original RGB/RIGHT identity-preserving NN join is supported')
        result = object.__new__(PcFrameInput)
        for name in self.__slots__:
            object.__setattr__(result, name, joined if name == 'observation' else getattr(self, name))
        return result

    def validate_current(self, current_view, now_us, *, binding, owner_token):
        """caller가 최종 시점의 최신 view를 준다. 과거 view가 미래 fence를 관측하지는 않는다."""
        _need(type(binding) is PcSourceBinding and binding.canonical_bytes == self.binding.canonical_bytes
            and self._issuer.owner_token is owner_token and type(current_view) is PcCurrentView
            and self._validation_token is self._issuer.owner_token
            and current_view._issuer is self._issuer and current_view._frame_token is self._frame_token
            and current_view._lease_token is self._lease_token and current_view._revision == self._revision
            and type(now_us) is int and current_view.checked_j_us <= now_us
            and self.expires_j_us == current_view.expires_j_us,
            'original PC owner/session/fence/lease/frame/E is not current')
        if now_us + 1 >= self.expires_j_us:
            from ..runtime.health_policy import FrameExpired
            raise FrameExpired('original PC frame expired')


class _PcInputIssuer(_Frozen):
    """tools factory가 보유하는 capability다. 계산기는 등록한 owner_token과 별도로 대조한다."""
    __slots__ = ('binding', 'owner_token')

    def __init__(self, binding):
        _need(type(binding) is PcSourceBinding, 'typed original PC binding required')
        object.__setattr__(self, 'binding', binding)
        object.__setattr__(self, 'owner_token', object())

    def issue(self, *, observation, proof_bytes, packet_header_bytes, packet_sha256, rgb_pc_interval,
              depth_pc_interval, mapped_rgb_j_us, mapped_depth_j_us, frame_received_j_us,
              expires_j_us, clock_probe_bytes, clock_reply_bytes, clock_j4_us, lease_token, revision):
        proof = _json(proof_bytes, MAX_PROOF_BYTES)
        header = _json(packet_header_bytes, 16 * 1024)
        _sha(packet_sha256)
        _need(type(observation) is PerceptionInput and observation.kind == 'SYNTHETIC'
            and observation.calibration_id == self.binding.fields['calibration_profile_sha256']
            and observation.rgb.stamp.source_id == self.binding.source_id
            and observation.rgb.stamp.clock_id == self.binding.clock_id
            and observation.depth is not None and observation.rgb.stamp.captured_us == mapped_rgb_j_us
            and observation.depth.stamp.captured_us == mapped_depth_j_us
            and observation.rgb.stamp.available_us == observation.depth.stamp.available_us == frame_received_j_us
            and proof['binding_sha256'] == self.binding.sha256 and proof['packet_header'] == header
            and proof['packet_sha256'] == packet_sha256 and proof['expires_j_us'] == expires_j_us
            and header['rgb']['pc_interval_us'] == list(rgb_pc_interval)
            and header['depth']['pc_interval_us'] == list(depth_pc_interval)
            and header['rgb']['sha256'] == observation.rgb.sha256
            and header['depth']['sha256'] == observation.depth.sha256,
            'mapped PC proof changed original bytes/time/source')
        _need(all(type(value) is tuple and len(value) == 2 for value in
            (clock_probe_bytes, clock_reply_bytes, clock_j4_us, rgb_pc_interval, depth_pc_interval,
             mapped_rgb_j_us, mapped_depth_j_us, revision))
            and all(type(value) is bytes for value in clock_probe_bytes + clock_reply_bytes)
            and all(type(value) is int and 0 <= value < 2**63 for value in
                clock_j4_us + rgb_pc_interval + depth_pc_interval + mapped_rgb_j_us + mapped_depth_j_us + revision)
            and type(frame_received_j_us) is int and type(expires_j_us) is int
            and max(mapped_rgb_j_us[1], mapped_depth_j_us[1]) <= frame_received_j_us < expires_j_us < 2**63
            and lease_token is not None,
            'original two-sample clock issuance required')
        result = object.__new__(PcFrameInput)
        fields = dict(binding=self.binding, observation=observation, proof_bytes=proof_bytes, expires_j_us=expires_j_us,
            packet_header_bytes=packet_header_bytes, packet_sha256=packet_sha256,
            rgb_pc_interval=rgb_pc_interval, depth_pc_interval=depth_pc_interval, mapped_rgb_j_us=mapped_rgb_j_us,
            mapped_depth_j_us=mapped_depth_j_us, frame_received_j_us=frame_received_j_us,
            clock_probe_bytes=clock_probe_bytes, clock_reply_bytes=clock_reply_bytes, clock_j4_us=clock_j4_us,
            _issuer=self, _frame_token=object(), _lease_token=lease_token, _revision=revision,
            _rgb_payload=observation.rgb.payload, _depth_payload=observation.depth.payload,
            _rgb_sha256=observation.rgb.sha256, _depth_sha256=observation.depth.sha256,
            _validation_token=self.owner_token)
        for name, value in fields.items():
            object.__setattr__(result, name, value)
        result.validate_original()
        return result

    def view(self, frame, checked_j_us):
        _need(type(frame) is PcFrameInput and frame._issuer is self
            and type(checked_j_us) is int and 0 <= checked_j_us < frame.expires_j_us, 'foreign PC issuer/frame/time')
        result = object.__new__(PcCurrentView)
        for name, value in dict(_issuer=self, _frame_token=frame._frame_token, _lease_token=frame._lease_token,
                _revision=frame._revision, checked_j_us=checked_j_us, expires_j_us=frame.expires_j_us).items():
            object.__setattr__(result, name, value)
        return result
