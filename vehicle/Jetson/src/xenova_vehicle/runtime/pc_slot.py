"""PC 관측 전용 owned slot codec. physical RAW 형식이나 clock 승인을 만들지 않는다.

128B prefix를 포함한 총16KiB metadata 영역 뒤에 연속 RGB/RIGHT/선택 NN bytes를 둔다. 원 PC/J proof와
binding을 보존하지만 opaque PcFrameInput/current view를 복원하지 않는다. EARLY의 실제
snapshot ACK 뒤 NN을 다음 generation에 게시하는 순서는 상위 actor/원장 책임이다.
"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import struct
from uuid import UUID
import zlib

from ..oak.yolop_frames import NNInputStamp, NNPacket, RuntimeTensor, MAX_NN_BYTES
from ..simulation.pc_input import PcSourceBinding
from .raw_leases import FilledInfo, WriteKey, SLOT_BYTES


HEADER_BYTES = 128
METADATA_BYTES = 16 * 1024
MAX_DESCRIPTOR_BYTES = METADATA_BYTES - HEADER_BYTES
MAX_PAYLOAD_BYTES = SLOT_BYTES - METADATA_BYTES
RGB_BYTES, DEPTH_BYTES = 345600, 614400
MAGIC = b'XNVPC01\0'
PROFILE = 'pc-owned-slot-1'
HEADER_STRUCT = struct.Struct('<8sHHI16sQ16s32sIII28s')
U63_MAX = (1 << 63) - 1


class PcSlotError(ValueError):
    """원 기대값, bounded layout 또는 source/proof/NN 결합이 다르다."""


def _need(value, message):
    if not value: raise PcSlotError(message)


def _int(value, maximum=U63_MAX, minimum=0):
    _need(type(value) is int and minimum <= value <= maximum, 'exact integer outside bound')
    return value


def _sha(value):
    _need(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value), 'canonical SHA required')
    return value


def _uuid(value):
    try:
        _need(type(value) is str and str(UUID(value)) == value and UUID(value).int != 0, 'canonical nonzero UUID required')
    except (ValueError, AttributeError) as error:
        raise PcSlotError('canonical nonzero UUID required') from error


def _keys(value, names):
    _need(type(value) is dict and set(value) == set(names), 'missing or unknown PC slot field')


def _canonical(value):
    try: return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')
    except (ValueError, TypeError, RecursionError) as error: raise PcSlotError('invalid PC metadata') from error


def _json(raw, cap):
    _need(type(raw) is bytes and 0 < len(raw) <= cap and raw.isascii(), 'bounded canonical ASCII bytes required')
    def pairs(rows):
        value = {}
        for key, child in rows:
            _need(key not in value, 'duplicate metadata key')
            value[key] = child
        return value
    def constant(_): raise PcSlotError('nonfinite metadata')
    try: value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError) as error: raise PcSlotError('invalid PC metadata JSON') from error
    _need(type(value) is dict and _canonical(value) == raw, 'noncanonical PC metadata')
    return value


def _decimal(value):
    _need(type(value) is str and 1 <= len(value) <= 19 and value.isascii() and value.isdecimal(), 'canonical decimal required')
    result = _int(int(value))
    _need(str(result) == value, 'canonical decimal required')
    return result


def _pair(value):
    _need(type(value) is list and len(value) == 2, 'original endpoint pair required')
    lo, hi = map(_int, value)
    _need(lo <= hi, 'reversed endpoint pair')
    return lo, hi


def _frame_key(value, binding):
    fields = binding.fields
    _need(type(value) is tuple and len(value) == 4 and value[:3] ==
        (binding.source_id, fields['stream_instances']['rgb_video'], binding.clock_id), 'original full RGB key differs')
    _int(value[3])


@dataclass(frozen=True)
class PcSlotContext:
    write_key: WriteKey
    binding: PcSourceBinding

    def __post_init__(self):
        _need(type(self.write_key) is WriteKey and type(self.binding) is PcSourceBinding, 'exact write key/PC binding required')
        _need(self.write_key.scope.raw_binding_sha256 == self.binding.sha256
            and self.write_key.scope.service_instance == self.binding.fields['service_instance'], 'registered scope/descriptor differs')


@dataclass(frozen=True)
class ExpectedPcSlot:
    context: PcSlotContext
    original_filled_info: FilledInfo
    job_id: str
    phase: str
    expires_j_us: int
    full_key: tuple
    pc_frame_proof_sha256: str

    def __post_init__(self):
        _need(type(self.context) is PcSlotContext and type(self.original_filled_info) is FilledInfo,
            'original context/FILLED required')
        _need(self.original_filled_info.key == self.context.write_key, 'original FILLED key differs')
        _uuid(self.job_id); _int(self.expires_j_us); _sha(self.pc_frame_proof_sha256)
        _need(self.phase in ('EARLY', 'NN') and type(self.phase) is str
            and self.original_filled_info.completed_us < self.expires_j_us, 'original phase/E differs')
        _frame_key(self.full_key, self.context.binding)


def _proof(raw, header_raw, binding, full_key, expires):
    """형식/원문 결합만 검사한다. 원 두 교환의 수학·peer·현재 lease를 수락하지 않는다."""
    proof, header = _json(raw, 16 * 1024), _json(header_raw, 16 * 1024)
    _keys(proof, ('profile', 'binding_sha256', 'packet_header', 'packet_sha256', 'clock', 'mapped_j_us',
        'frame_received_j_us', 'expires_j_us', 'destination_clock_id', 'revision'))
    _need(proof['profile'] == 'pc-frame-input-1' and proof['binding_sha256'] == binding.sha256
        and proof['packet_header'] == header and proof['expires_j_us'] == expires
        and proof['destination_clock_id'] == binding.clock_id, 'original PC proof/header/binding/E differs')
    _sha(proof['packet_sha256'])
    _need(type(proof['revision']) is list and len(proof['revision']) == 2, 'original fence pair required')
    for value in proof['revision']: _int(value)
    fields = binding.fields
    scope = dict(source_id=fields['source_id'], session_id=fields['jetson_session'], source_descriptor_sha256=binding.sha256,
        calibration_sha256=fields['calibration_profile_sha256'], motion_bundle_sha256=fields['motion_profile_sha256'],
        pc_clock_epoch=fields['pc_clock_epoch'])
    _keys(header, ('profile', 'kind', 'sequence', 'scope', 'rgb', 'depth', 'render_completed_pc_us', 'expires_pc_us'))
    _need(header['profile'] == 'hil-observation-1' and header['kind'] == 'FRAME' and header['scope'] == scope
        and type(header['sequence']) is int and header['sequence'] == full_key[3], 'original PC transport source/key differs')
    pc_pairs = []
    for name, height, encoding in (('rgb', 360, 'NV12'), ('depth', 480, 'RAW16LE')):
        plane = header[name]
        _keys(plane, ('width', 'height', 'encoding', 'pc_interval_us', 'sha256'))
        _need(type(plane['width']) is int and type(plane['height']) is int
            and (plane['width'], plane['height'], plane['encoding']) == (640, height, encoding), 'original image profile differs')
        pc_pairs.append(_pair(plane['pc_interval_us'])); _sha(plane['sha256'])
    _need(max(p[1] for p in pc_pairs) <= _int(header['render_completed_pc_us']) < _int(header['expires_pc_us']),
        'original PC capture/completion/E differs')
    mapped = proof['mapped_j_us']
    _need(type(mapped) is list and len(mapped) == 2, 'original mapped pair required')
    intervals = tuple(_pair(value) for value in mapped)
    lower, upper = min(p[0] for p in intervals), max(p[1] for p in intervals)
    _need(lower < upper <= _int(proof['frame_received_j_us']) < expires <= lower + 500_000,
        'original mapped capture/availability/E differs')
    clock = proof['clock']; _keys(clock, ('policy', 'samples'))
    _keys(clock['policy'], ('max_rtt_us', 'max_offset_width_us', 'drift_ppm', 'lease_us', 'lookback_us'))
    for name, low, high in (('max_rtt_us', 1, 200_000), ('max_offset_width_us', 1, U63_MAX),
            ('drift_ppm', 0, 999999), ('lease_us', 1, 1_000_000), ('lookback_us', 0, 500_000)):
        _int(clock['policy'][name], high, low)
    _need(type(clock['samples']) is list and len(clock['samples']) == 2, 'original two clock samples required')
    last_sequence = 0
    for sample in clock['samples']:
        _keys(sample, ('reply', 'j4_us'))
        reply = sample['reply']; _keys(reply, ('profile', 'probe', 'p2_us', 'p3_us'))
        probe = reply['probe']; _keys(probe, ('profile', 'source', 'sequence', 'nonce', 'j1_us', 'expires_j_us'))
        _need(reply['profile'] == 'hil-observation-clock-reply-1' and probe['profile'] == 'hil-observation-clock-probe-1'
            and probe['source'] == dict(source_id=fields['source_id'], pc_session=fields['pc_clock_epoch'],
                jetson_session=fields['jetson_session']), 'original clock source/profile differs')
        sequence = _decimal(probe['sequence'])
        _need(last_sequence < sequence, 'original clock pair sequence differs'); last_sequence = sequence
        nonce = probe['nonce']
        _need(type(nonce) is str and len(nonce) == 32 and all(c in '0123456789abcdef' for c in nonce)
            and any(c != '0' for c in nonce), 'original clock nonce differs')
        _need(_decimal(probe['j1_us']) <= _int(sample['j4_us'])
            and _decimal(probe['j1_us']) < _decimal(probe['expires_j_us']) <= _decimal(probe['j1_us']) + 200_000
            and sample['j4_us'] + 1 < _decimal(probe['expires_j_us'])
            and _decimal(reply['p2_us']) <= _decimal(reply['p3_us']), 'original clock sample order differs')
    return proof, header


def _nn(phase, value, payload, full_key, binding, available, completed, expires):
    if phase == 'EARLY':
        _need(value is None and payload is None, 'EARLY cannot contain NN facts')
        return None, None
    _keys(value, ('input', 'output'))
    inp, out = value['input'], value['output']
    _keys(inp, ('stream', 'sequence', 'device_us', 'sdk_us', 'received_j_us', 'width', 'height', 'encoding', 'context_key'))
    _keys(out, ('stream', 'sequence', 'device_us', 'sdk_us', 'received_j_us', 'context_key', 'tensors', 'golden'))
    _need(type(out['tensors']) is list and len(out['tensors']) == 3 and type(payload) is bytes
        and 0 < len(payload) <= MAX_NN_BYTES, 'actual bounded NN payload/tensors required')
    tensors = []
    for value in out['tensors']:
        _keys(value, ('name', 'data_type_code', 'order_code', 'num_dimensions', 'dims', 'strides', 'offset', 'data_type_text', 'order_text'))
        _need(type(value['dims']) is list and type(value['strides']) is list, 'original runtime layout required')
        try: tensors.append(RuntimeTensor(**dict(value, dims=tuple(value['dims']), strides=tuple(value['strides']))))
        except (ValueError, TypeError) as error: raise PcSlotError('invalid original runtime tensor') from error
    try:
        nn_input = NNInputStamp(**inp)
        nn_output = NNPacket(**dict(out, tensors=tuple(tensors), payload=payload))
    except (ValueError, TypeError) as error:
        raise PcSlotError('invalid original YOLOP metadata/storage') from error
    _need((nn_input.sequence, nn_input.device_us, nn_input.sdk_us, nn_input.context_key) ==
        (nn_output.sequence, nn_output.device_us, nn_output.sdk_us, nn_output.context_key)
        and nn_output.sequence == full_key[3] and nn_output.context_key == binding.fields['nn_manifest_sha256']
        and all(available <= item.received_j_us < expires and (completed is None or item.received_j_us <= completed)
            for item in (nn_input, nn_output)), 'original NN/RGB key/context/availability/E differs')
    return nn_input, nn_output


class PcPrefixHeader:
    """복사 길이와 원 write key만 검산한다. read lease/본문 수락은 아니다."""
    __slots__ = ('raw', 'context', 'descriptor_length', 'payload_length', 'descriptor_crc32', 'payload_crc32')

    def __init__(self, raw, context):
        _need(type(context) is PcSlotContext and type(raw) is bytes and len(raw) == HEADER_BYTES, 'exact PC128 prefix/context required')
        magic, version, size, d, arena, generation, token, binding, length, dc, pc, reserved = HEADER_STRUCT.unpack(raw)
        key = context.write_key
        _need(magic == MAGIC and version == 1 and size == HEADER_BYTES and reserved == bytes(28), 'PC prefix magic/version/reserved differs')
        _int(d, MAX_DESCRIPTOR_BYTES, 1); _int(length, MAX_PAYLOAD_BYTES, 1)
        _need(arena == UUID(key.scope.arena_id).bytes and generation == key.generation
            and token == UUID(key.write_token).bytes and binding == bytes.fromhex(context.binding.sha256), 'PC prefix write key differs')
        for name, value in dict(raw=raw, context=context, descriptor_length=d, payload_length=length,
                descriptor_crc32=dc, payload_crc32=pc).items(): object.__setattr__(self, name, value)

    def __setattr__(self, name, value): raise AttributeError('immutable PC prefix')

    @property
    def used_length(self): return METADATA_BYTES + self.payload_length


class _Access:
    def descriptor(self): return json.loads(self.descriptor_bytes)

    @property
    def phase(self): return self.descriptor()['phase']

    @property
    def job_id(self): return self.descriptor()['job_id']

    @property
    def expires_j_us(self): return self.descriptor()['expires_j_us']

    @property
    def full_key(self): return tuple(self.descriptor()['full_key'])

    @property
    def binding(self): return self.context.binding

    @property
    def pc_frame_proof(self): return _canonical(self.descriptor()['proof'])

    @property
    def pc_packet_header(self): return _canonical(self.descriptor()['proof']['packet_header'])

    @property
    def rgb(self): return self.raw[METADATA_BYTES:METADATA_BYTES + RGB_BYTES]

    @property
    def depth(self): return self.raw[METADATA_BYTES + RGB_BYTES:METADATA_BYTES + RGB_BYTES + DEPTH_BYTES]

    @property
    def nn_key(self):
        out = self.descriptor()['nn']
        if out is None: return None
        out = out['output']
        return self.full_key + (out['device_us'], out['sdk_us'], out['context_key'],
            self.descriptor()['proof']['packet_header']['rgb']['sha256'])


def _schema(metadata, context, payload, *, completed=None):
    _keys(metadata, ('profile', 'phase', 'binding', 'job_id', 'expires_j_us', 'full_key', 'proof', 'planes', 'nn'))
    _need(metadata['profile'] == PROFILE and metadata['phase'] in ('EARLY', 'NN')
        and _canonical(metadata['binding']) == context.binding.canonical_bytes, 'PC profile/phase/binding differs')
    _uuid(metadata['job_id']); expires = _int(metadata['expires_j_us'])
    _need(type(metadata['full_key']) is list, 'original full key list required')
    full_key = tuple(metadata['full_key']); _frame_key(full_key, context.binding)
    _need(type(metadata['proof']) is dict and 'packet_header' in metadata['proof'], 'original PC proof/header required')
    proof_raw = _canonical(metadata['proof'])
    proof, header = _proof(proof_raw, _canonical(metadata['proof']['packet_header']), context.binding, full_key, expires)
    _need(completed is None or max(proof['frame_received_j_us'], *(s['j4_us'] for s in proof['clock']['samples']))
        <= completed < expires, 'original FILLED completion differs')
    names = ('rgb_video', 'depth_right') + (('nn_output0',) if metadata['phase'] == 'NN' else ())
    _keys(metadata['planes'], names)
    offset = METADATA_BYTES
    for name, length in (('rgb_video', RGB_BYTES), ('depth_right', DEPTH_BYTES),
            *((('nn_output0', len(payload) - RGB_BYTES - DEPTH_BYTES),) if metadata['phase'] == 'NN' else ())):
        row = metadata['planes'][name]; _keys(row, ('offset', 'length', 'sha256'))
        _need(type(row['offset']) is int and type(row['length']) is int and row['offset'] == offset
            and row['length'] == length and length > 0, 'actual contiguous PC plane extent differs')
        raw = payload[offset - METADATA_BYTES:offset - METADATA_BYTES + length]
        _need(len(raw) == length and sha256(raw).hexdigest() == _sha(row['sha256']), 'actual PC plane SHA differs')
        if name != 'nn_output0':
            _need(row['sha256'] == header['rgb' if name == 'rgb_video' else 'depth']['sha256'], 'original PC image SHA differs')
        offset += length
    _need(offset == METADATA_BYTES + len(payload), 'actual used PC payload length differs')
    nn_raw = payload[RGB_BYTES + DEPTH_BYTES:] if metadata['phase'] == 'NN' else None
    return _nn(metadata['phase'], metadata['nn'], nn_raw, full_key, context.binding,
        proof['frame_received_j_us'], completed, expires)


@dataclass(frozen=True, init=False)
class PcSlotEncoding(_Access):
    context: PcSlotContext
    raw: bytes
    header: PcPrefixHeader
    descriptor_bytes: bytes
    descriptor_sha256: str
    payload_length: int

    def __init__(self, context, *, phase, job_id, expires_j_us, full_key, pc_frame_proof, pc_packet_header,
                 rgb, depth, nn_input=None, nn_output=None):
        _need(type(context) is PcSlotContext and phase in ('EARLY', 'NN') and type(phase) is str,
            'exact PC context/phase required')
        _frame_key(full_key, context.binding)
        _need(type(rgb) is bytes and len(rgb) == RGB_BYTES and type(depth) is bytes and len(depth) == DEPTH_BYTES,
            'owned original RGB/RIGHT bytes required')
        proof, _ = _proof(pc_frame_proof, pc_packet_header, context.binding, full_key, expires_j_us)
        nn, nn_raw = None, b''
        if phase == 'NN':
            _need(type(nn_input) is NNInputStamp and type(nn_output) is NNPacket, 'actual typed NN input/output required')
            nn_input.validate(); nn_output.validate()
            nn = dict(input=asdict(nn_input), output=dict(stream=nn_output.stream, sequence=nn_output.sequence,
                device_us=nn_output.device_us, sdk_us=nn_output.sdk_us, received_j_us=nn_output.received_j_us,
                context_key=nn_output.context_key, tensors=[dict(asdict(t), dims=list(t.dims), strides=list(t.strides))
                    for t in nn_output.tensors], golden=nn_output.golden))
            nn_raw = nn_output.payload
        else: _need(nn_input is None and nn_output is None, 'EARLY cannot contain NN metadata')
        payload = rgb + depth + nn_raw
        _need(len(payload) <= MAX_PAYLOAD_BYTES, 'PC body exceeds one slot')
        planes, offset = {}, METADATA_BYTES
        for name, raw in (('rgb_video', rgb), ('depth_right', depth), *((('nn_output0', nn_raw),) if phase == 'NN' else ())):
            planes[name] = dict(offset=offset, length=len(raw), sha256=sha256(raw).hexdigest()); offset += len(raw)
        metadata = dict(profile=PROFILE, phase=phase, binding=context.binding.fields, job_id=job_id,
            expires_j_us=expires_j_us, full_key=list(full_key), proof=proof, planes=planes, nn=nn)
        _schema(metadata, context, payload)
        descriptor = _canonical(metadata)
        _need(len(descriptor) <= MAX_DESCRIPTOR_BYTES, 'whole original PC metadata exceeds16KiB; no proof omitted')
        key = context.write_key
        header = HEADER_STRUCT.pack(MAGIC, 1, HEADER_BYTES, len(descriptor), UUID(key.scope.arena_id).bytes,
            key.generation, UUID(key.write_token).bytes, bytes.fromhex(context.binding.sha256), len(payload),
            zlib.crc32(descriptor), zlib.crc32(payload), bytes(28))
        raw = header + descriptor + bytes(MAX_DESCRIPTOR_BYTES - len(descriptor)) + payload
        for name, value in dict(context=context, raw=raw, header=PcPrefixHeader(header, context),
                descriptor_bytes=descriptor, descriptor_sha256=sha256(descriptor).hexdigest(), payload_length=len(payload)).items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, init=False)
class PcOwnedSlot(_Access):
    """원 FILLED/expected와 대조한 소유 bytes facts다. lease/source/geometry 승인은 아니다."""
    context: PcSlotContext
    expected: ExpectedPcSlot
    raw: bytes
    header: PcPrefixHeader
    descriptor_bytes: bytes
    descriptor_sha256: str
    payload_length: int
    nn_input: NNInputStamp | None
    nn_output: NNPacket | None

    def __init__(self, raw, expected, optional_prefix=None):
        _need(type(expected) is ExpectedPcSlot and type(raw) is bytes and METADATA_BYTES < len(raw) <= SLOT_BYTES,
            'exact owned PC slot/expectation required')
        header = PcPrefixHeader(raw[:HEADER_BYTES], expected.context)
        _need(optional_prefix is None or type(optional_prefix) is PcPrefixHeader
            and optional_prefix.context == expected.context and optional_prefix.raw == header.raw, 'original PC prefix differs')
        info = expected.original_filled_info
        _need(len(raw) == header.used_length and header.payload_length == info.payload_length, 'actual used PC slot extent differs')
        end = HEADER_BYTES + header.descriptor_length
        descriptor = raw[HEADER_BYTES:end]
        _need(sha256(descriptor).hexdigest() == info.descriptor_sha256 and zlib.crc32(descriptor) == header.descriptor_crc32,
            'original PC descriptor SHA/CRC differs')
        _need(not any(raw[end:METADATA_BYTES]) and zlib.crc32(memoryview(raw)[METADATA_BYTES:]) == header.payload_crc32,
            'PC metadata slack/payload CRC differs')
        metadata = _json(descriptor, MAX_DESCRIPTOR_BYTES)
        _keys(metadata, ('profile', 'phase', 'binding', 'job_id', 'expires_j_us', 'full_key', 'proof', 'planes', 'nn'))
        _need(type(metadata['full_key']) is list, 'original full key list required')
        _need((metadata['job_id'], metadata['phase'], metadata['expires_j_us'], tuple(metadata['full_key']),
            sha256(_canonical(metadata['proof'])).hexdigest()) == (expected.job_id, expected.phase, expected.expires_j_us,
                expected.full_key, expected.pc_frame_proof_sha256), 'original expected PC job/phase/E/key/proof differs')
        nn_input, nn_output = _schema(metadata, expected.context, raw[METADATA_BYTES:], completed=info.completed_us)
        for name, value in dict(context=expected.context, expected=expected, raw=raw, header=header,
                descriptor_bytes=descriptor, descriptor_sha256=info.descriptor_sha256, payload_length=header.payload_length,
                nn_input=nn_input, nn_output=nn_output).items(): object.__setattr__(self, name, value)


def prefix(raw128, context): return PcPrefixHeader(raw128, context)


def decode_owned(raw, expected, optional_prefix=None): return PcOwnedSlot(raw, expected, optional_prefix)
