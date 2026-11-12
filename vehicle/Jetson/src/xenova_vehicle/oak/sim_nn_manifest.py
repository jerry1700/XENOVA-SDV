"""HOST_SIM NN의 사전 선언과 실제 start 반환값을 분리해 닫는다.

manifest는 파일/설정 선언이며 Device readback·추론 완료·pixel 동치·source 등록 증거가
아니다. from_current만 원 asset bytes를 검사한다. from_bytes는 구조만 읽으며 실제
start 비교에 필요한 검증 asset closure를 만들지 않는다. SDK/I/O 기동·시각 발급은 없다.
"""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path


PROFILE = 'host-sim-nn-manifest-1'
SCOPE = 'HOST_SIM_DECLARATION_NOT_DEVICE_READBACK_OR_READINESS'
MAX_MANIFEST_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
_PACKAGE = Path(__file__).resolve().parents[1]
_GRAPH_SOURCES = ('oak/sim_nn.py', 'oak/yolop_asset.py', 'oak/sim_nn_manifest.py')
_PARSER_SOURCES = ('perception/yolop.py', 'perception/decoders.py', 'oak/yolop_frames.py', 'oak/sim_nn.py')


class SimNnManifestError(ValueError):
    """원 SIM 선언·asset closure·start 반환값이 닫힌 계약과 다르다."""


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (TypeError, ValueError, RecursionError) as error:
        raise SimNnManifestError('bounded native JSON required') from error


def _copy(value, *, cap):
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 2048 or depth > 12:
            raise SimNnManifestError('JSON closure exceeds depth/node bounds')
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int and -(2**63) <= item < 2**64:
            return item
        if kind is float and math.isfinite(item):
            return item
        if kind is str and len(item) <= 4096 and all(ord(c) >= 32 and not 0xd800 <= ord(c) <= 0xdfff for c in item):
            return item
        if kind is list:
            return [visit(child, depth + 1) for child in item]
        if kind is dict and all(type(key) is str and 1 <= len(key) <= 128 for key in item):
            return {key: visit(child, depth + 1) for key, child in item.items()}
        raise SimNnManifestError('exact finite JSON values required')

    try:
        raw = _canonical(visit(value, 0))
    except RecursionError as error:
        raise SimNnManifestError('JSON closure exceeds recursion bound') from error
    if not 0 < len(raw) <= cap:
        raise SimNnManifestError('JSON closure exceeds byte bound')
    return raw


def _parse(raw, cap):
    if type(raw) is not bytes or not 0 < len(raw) <= cap:
        raise SimNnManifestError('bounded owned JSON bytes required')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SimNnManifestError('duplicate JSON key')
            result[key] = value
        return result

    def reject_constant(_):
        raise SimNnManifestError('nonfinite JSON')

    try:
        value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
            parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise SimNnManifestError('invalid bounded JSON bytes') from error
    return json.loads(_copy(value, cap=cap))


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise SimNnManifestError('missing or unknown SIM manifest fields')


def _same(actual, expected, reason):
    # JSON bytes 비교로 bool/int 등 Python ==의 수치 alias도 거절한다.
    if _canonical(actual) != _canonical(expected):
        raise SimNnManifestError(reason)


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise SimNnManifestError('lowercase SHA256 required')


def _decimal(value):
    if (type(value) is not str or not 1 <= len(value) <= 20 or not value.isascii()
            or not value.isdecimal() or str(int(value)) != value or int(value) >= 2**64):
        raise SimNnManifestError('canonical original uint64 decimal required')
    return int(value)


def _sources(names):
    result = {}
    for name in names:
        path = _PACKAGE / name
        with path.open('rb') as source:
            raw = source.read(1024 * 1024 + 1)
        if not 0 < len(raw) <= 1024 * 1024:
            raise SimNnManifestError('fixed source exceeds one MiB')
        result[name] = sha256(raw).hexdigest()
    return result


def _settings():
    return dict(input_stream='host_sim_rgb', input_size_wh=[640, 360], input_type='NV12',
        input_max_bytes=345600, host_input_queue_size=1, host_input_blocking=False,
        nn_size_wh=[320, 320], content_size_wh=[320, 180], padding_ltrb=[0, 70, 0, 70],
        padding_bgr=[0, 0, 0], nn_type='BGR888p', inference_threads=1, pool_frames=2,
        host_output_sizes={'nn_input': 8, 'nn_output': 2}, device_queue_size=1,
        device_queue_blocking=False, passthrough_metadata_only=True, pixel_equivalence_verified=False,
        device_timestamp_basis='HOST_SIM_SDK_sample_not_device_measurement')


def _profile(value):
    from .sim_nn import SimNnProfile, PROFILE_ID
    from ..yolop_profile import YolopBenchProfile
    _keys(value, ('profile_id', 'input_kind', 'camera_nodes', 'source_id', 'clock_id',
        'calibration_id', 'approved_asset_device_settings'))
    settings = value['approved_asset_device_settings']
    names = tuple(name for name in YolopBenchProfile.__dataclass_fields__
                  if name != 'rgb_sensor_mode' or 'rgb_sensor_mode' in settings)
    _keys(settings, (*names, 'schema_version', 'profile_id', 'nn_requirement', 'jpeg_enabled'))
    try:
        owned = YolopBenchProfile(**{name: settings[name] for name in names})
        current = SimNnProfile(owned, value['source_id'], value['clock_id'], value['calibration_id']).to_dict()
    except (TypeError, ValueError, OSError) as error:
        raise SimNnManifestError('invalid exact SIM profile') from error
    if value['profile_id'] != PROFILE_ID:
        raise SimNnManifestError('physical or unknown SIM profile')
    _same(value, current, 'SIM profile differs from its explicit supported settings')


def _asset_summary():
    from .yolop_asset import ASSET_ID, BLOB_SIZE, BLOB_SHA256, MANIFEST_SHA256
    return dict(asset_id=ASSET_ID, manifest_canonical_sha256=MANIFEST_SHA256,
        blob_sha256=BLOB_SHA256, blob_bytes=BLOB_SIZE)


def _checked_asset(asset, *, expected_asset_id, expected_blob_bytes,
                   expected_blob_sha256, expected_manifest_sha256):
    """순수 바이트 비교 seam이다. 반환 dict는 승인 manifest/start 증거 타입이 아니다.

    production caller는 아래 from_current의 고정 asset 상수만 전달한다. 작은 시험용 원본도
    자기 일관성을 검사할 수 있으나 production factory에 수락되는 대체 asset은 아니다.
    """
    from .yolop_asset import ValidatedYolopAsset, BLOB_NAME
    if type(asset) is not ValidatedYolopAsset or type(asset.blob_bytes) is not bytes:
        raise SimNnManifestError('original owned validated asset record required')
    if len(asset.blob_bytes) != expected_blob_bytes or sha256(asset.blob_bytes).hexdigest() != expected_blob_sha256:
        raise SimNnManifestError('actual blob bytes differ from pinned length/SHA')
    manifest = _parse(_copy(asset.manifest, cap=65536), 65536)
    if sha256(_canonical(manifest)).hexdigest() != expected_manifest_sha256 or manifest.get('asset_id') != expected_asset_id:
        raise SimNnManifestError('actual asset canonical manifest differs from pin')
    files = manifest.get('files')
    if type(files) is not list or not 1 <= len(files) <= 16:
        raise SimNnManifestError('bounded original asset file inventory required')
    checked = []
    for item in files:
        _keys(item, ('name', 'role', 'bytes', 'sha256'))
        name = item['name']
        if (type(name) is not str or not name or '/' in name or '\\' in name
                or type(item['bytes']) is not int or not 0 < item['bytes'] < 2**63):
            raise SimNnManifestError('invalid original asset file inventory')
        _sha(item['sha256'])
        checked.append({key: item[key] for key in ('name', 'bytes', 'sha256')})
    blobs = [item for item in checked if item['name'] == BLOB_NAME]
    if len({item['name'] for item in checked}) != len(checked) or len(blobs) != 1:
        raise SimNnManifestError('original compiled blob must occur exactly once')
    _same(blobs[0], dict(name=BLOB_NAME, bytes=expected_blob_bytes, sha256=expected_blob_sha256),
        'original manifest blob entry differs')
    summary = dict(asset_id=expected_asset_id, manifest_canonical_sha256=expected_manifest_sha256,
        blob_sha256=expected_blob_sha256, blob_bytes=expected_blob_bytes)
    receipt = dict(**summary, files=checked, device_readback_verified=False, on_device_inference_verified=False)
    _same(_parse(_copy(asset.receipt, cap=65536), 65536), receipt, 'actual asset receipt differs from checked manifest/bytes')
    metadata = manifest.get('sdk_blob_metadata')
    if type(metadata) is not dict or 'host_graph_required_version' not in metadata:
        raise SimNnManifestError('original pinned SDK metadata missing')
    sdk = {name: value for name, value in metadata.items() if name != 'host_graph_required_version'}
    return summary, _canonical(receipt), _canonical(sdk)


def _fields(profile, asset):
    from .sim_nn import SimNnProfile
    from ..perception.yolop import PARSER_ID, PREPROCESSING_ID, YolopPostprocessConfig
    if type(profile) is not SimNnProfile:
        raise SimNnManifestError('exact original SIM profile required')
    return dict(profile=PROFILE, sim_profile=profile.to_dict(), asset=asset,
        graph=dict(source_sha256=_sources(_GRAPH_SOURCES), declared_settings=_settings()),
        parser=dict(parser_id=PARSER_ID, preprocessing_id=PREPROCESSING_ID,
            configuration=YolopPostprocessConfig().to_dict(), source_sha256=_sources(_PARSER_SOURCES)), scope=SCOPE)


def _validate(value):
    from ..perception.yolop import PARSER_ID, PREPROCESSING_ID, YolopPostprocessConfig
    _keys(value, ('profile', 'sim_profile', 'asset', 'graph', 'parser', 'scope'))
    if value['profile'] != PROFILE or value['scope'] != SCOPE:
        raise SimNnManifestError('unknown or physical manifest scope')
    _profile(value['sim_profile'])
    _same(value['asset'], _asset_summary(), 'manifest must declare the pinned production asset')
    for name, sources in (('graph', _GRAPH_SOURCES), ('parser', _PARSER_SOURCES)):
        source_map = value[name].get('source_sha256') if type(value[name]) is dict else None
        _keys(source_map, sources)
        for digest in source_map.values():
            _sha(digest)
    _keys(value['graph'], ('source_sha256', 'declared_settings'))
    _same(value['graph']['declared_settings'], _settings(), 'unknown SIM graph declaration settings')
    _keys(value['parser'], ('parser_id', 'preprocessing_id', 'configuration', 'source_sha256'))
    _same({key: item for key, item in value['parser'].items() if key != 'source_sha256'},
        dict(parser_id=PARSER_ID, preprocessing_id=PREPROCESSING_ID, configuration=YolopPostprocessConfig().to_dict()),
        'unsupported SIM parser/configuration')


@dataclass(frozen=True, slots=True, init=False)
class SimNnManifest:
    """고정 실행 host의 canonical 선언이다. portable 경로 parser는 아니다.

    from_bytes도 기존 profile의 expanduser/absolute 규칙을 쓰므로 host 경로 문맥에
    의존한다. 현 파일·asset·Device 검증이나 프로세스/장치 I/O는 수행하지 않는다.
    """
    canonical_bytes: bytes
    sha256: str
    _asset_receipt: bytes | None
    _sdk_metadata: bytes | None

    def __init__(self, *args, **kwargs):
        raise TypeError('use explicit from_current or structural from_bytes')

    @classmethod
    def from_bytes(cls, raw):
        value = _parse(raw, MAX_MANIFEST_BYTES)
        _validate(value)
        result = object.__new__(cls)
        own = _canonical(value)
        for name, item in dict(canonical_bytes=own, sha256=sha256(own).hexdigest(),
                _asset_receipt=None, _sdk_metadata=None).items():
            object.__setattr__(result, name, item)
        return result

    @classmethod
    def from_current(cls, profile, asset):
        """초기 준비 owner 전용이다. 실제 blob hash와 고정 source 파일을 한 번 읽는다."""
        expected = _asset_summary()
        summary, receipt, metadata = _checked_asset(asset, expected_asset_id=expected['asset_id'],
            expected_blob_bytes=expected['blob_bytes'], expected_blob_sha256=expected['blob_sha256'],
            expected_manifest_sha256=expected['manifest_canonical_sha256'])
        result = cls.from_bytes(_copy(_fields(profile, summary), cap=MAX_MANIFEST_BYTES))
        object.__setattr__(result, '_asset_receipt', receipt)
        object.__setattr__(result, '_sdk_metadata', metadata)
        return result

    @property
    def fields(self):
        return json.loads(self.canonical_bytes)


def _graph(graph, asset, settings):
    _keys(graph, ('profile_id', 'input_kind', 'camera_nodes', 'scope', 'device_config_readback',
        'nodes', 'connections', 'asset_storage_included', 'asset', 'declared_settings'))
    from .sim_nn import PROFILE_ID
    _same({name: graph[name] for name in ('profile_id', 'input_kind', 'camera_nodes', 'scope',
        'device_config_readback', 'asset_storage_included', 'asset', 'declared_settings')},
        dict(profile_id=PROFILE_ID, input_kind='SYNTHETIC', camera_nodes=0,
            scope='host_SIM_node_connection_declaration', device_config_readback=False,
            asset_storage_included=False, asset={key: asset[key] for key in ('asset_id', 'blob_sha256', 'blob_bytes')},
            declared_settings=settings), 'actual SIM graph declaration differs')
    nodes, edges = graph['nodes'], graph['connections']
    if type(nodes) is not list or len(nodes) != 5 or type(edges) is not list or len(edges) != 4:
        raise SimNnManifestError('actual SIM graph requires five nodes/four links')
    names = {}
    for node in nodes:
        _keys(node, ('id', 'name'))
        identity = node['id']
        if type(identity) is not int or not 0 <= identity < 2**63 or identity in names or type(node['name']) is not str:
            raise SimNnManifestError('invalid actual node identity')
        names[identity] = node['name']
    if Counter(names.values()) != Counter(XLinkIn=1, ImageManip=1, NeuralNetwork=1, XLinkOut=2):
        raise SimNnManifestError('physical or unknown actual graph node')
    actual = []
    for edge in edges:
        _keys(edge, ('output_id', 'input_id', 'output_name', 'input_name'))
        if any(type(edge[key]) is not int or edge[key] not in names for key in ('output_id', 'input_id')):
            raise SimNnManifestError('foreign actual graph edge')
        if any(type(edge[key]) is not str or not 1 <= len(edge[key]) <= 128
                or any(not 33 <= ord(c) <= 126 for c in edge[key]) for key in ('output_name', 'input_name')):
            raise SimNnManifestError('invalid actual graph edge name')
        actual.append((edge['output_id'], edge['input_id'], edge['output_name'], edge['input_name']))
    host, manip, network = (next(key for key, value in names.items() if value == name)
        for name in ('XLinkIn', 'ImageManip', 'NeuralNetwork'))
    outputs = [key for key, value in names.items() if value == 'XLinkOut']
    # 고정 SDK의 native inputName은 Python .input 속성 철자와 다른 'in'이다.
    candidates = [((host, manip, 'out', 'inputImage'), (manip, network, 'out', 'in'),
        (network, outputs[at], 'passthrough', 'in'), (network, outputs[1-at], 'out', 'in')) for at in (0, 1)]
    if not any(sorted(actual) == sorted(expected) for expected in candidates):
        raise SimNnManifestError('actual SIM edge topology differs from host/manip/NN/two distinct outputs')


def _receipt(value, fields, *, asset_receipt, sdk_metadata, context_key, startup_deadline_us):
    """원 반환값의 순수 비교다. 작은 fixture 결과를 production 증거 타입으로 승격하지 않는다."""
    _sha(context_key)
    if type(startup_deadline_us) is not int or not 0 < startup_deadline_us < 2**64:
        raise SimNnManifestError('original caller startup cap required')
    _keys(value, ('profile', 'graph_declaration', 'device_mxid', 'depthai_version', 'model_id',
        'model_asset', 'sdk_blob_metadata', 'blob_registration_count', 'pipeline_start_count',
        'input_kind', 'nn_context_key', 'nn_ready', 'metric_ready', 'live_authority',
        'model_residency_evidence', 'startup_deadline_us'))
    deadline = _decimal(value['startup_deadline_us'])
    if not 0 < deadline <= startup_deadline_us:
        raise SimNnManifestError('actual startup deadline exceeds its original caller cap')
    expected = dict(profile=fields['sim_profile'],
        device_mxid=fields['sim_profile']['approved_asset_device_settings']['device_mxid'], depthai_version='2.29.0.0',
        model_id=fields['asset']['asset_id'], model_asset=asset_receipt, sdk_blob_metadata=sdk_metadata,
        blob_registration_count=1, pipeline_start_count=1, input_kind='SYNTHETIC', nn_context_key=context_key,
        nn_ready=False, metric_ready=False, live_authority=False,
        model_residency_evidence='host_verified_asset_and_graph_start_only')
    _same({key: value[key] for key in expected}, expected, 'actual start receipt differs from original SIM declaration/asset')
    _graph(value['graph_declaration'], fields['asset'], fields['graph']['declared_settings'])


@dataclass(frozen=True, slots=True, init=False)
class SimNnStartEvidence:
    """원 start 반환값 비교 결과다. 자체 OS/SDK 실행 증거가 아니다.

    실제 start 반환을 전달한 runtime owner와 연결된 뒤에만 사용한다. 실제 추론·READY·
    RAW ACK·motion lease를 만들지 않는다.
    """
    manifest_sha256: str
    canonical_receipt_bytes: bytes
    sha256: str

    def __init__(self, *args, **kwargs):
        raise TypeError('start evidence is produced only by validate_start_receipt')

    @property
    def fields(self):
        return json.loads(self.canonical_receipt_bytes)


def validate_start_receipt(manifest, actual_receipt, *, context_key, startup_deadline_us):
    """실제 start() 직후 명시 호출한다. 원 receipt의 작은 deadline도 그대로 보존한다.

    startup_deadline_us는 상위 cap이다. start가 min(local start+timeout,cap)로 선택한 원
    deadline을 다시 계산하거나 갱신하지 않는다. parsed 선언만으로 검증 asset를 대신하지 않는다.
    """
    if (type(manifest) is not SimNnManifest or manifest._asset_receipt is None or manifest._sdk_metadata is None):
        raise SimNnManifestError('from_current original checked asset closure required')
    raw = _copy(actual_receipt, cap=MAX_RECEIPT_BYTES)
    value = json.loads(raw)
    _receipt(value, manifest.fields, asset_receipt=json.loads(manifest._asset_receipt),
        sdk_metadata=json.loads(manifest._sdk_metadata), context_key=context_key, startup_deadline_us=startup_deadline_us)
    result = object.__new__(SimNnStartEvidence)
    for name, item in dict(manifest_sha256=manifest.sha256, canonical_receipt_bytes=raw, sha256=sha256(raw).hexdigest()).items():
        object.__setattr__(result, name, item)
    return result
