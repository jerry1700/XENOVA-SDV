"""명시 PC 관측 상주 실행의 조립 도구다. import만으로 연결·worker·SDK를 시작하지 않는다.

원 metadata를 공개 constructor로 재구성해 고정 hash와 대조한다. private JIO 파일은
기존 owner-only loader만 읽는다. 연결 한 개를 넘기는 API와 명시 유한 accept CLI를
제공하며 프로토콜 협상·runtime-init·설치·자격 설정·READY 승인은 하지 않는다.
"""

import argparse
from dataclasses import dataclass, field
from hashlib import sha256
import ipaddress
import json
import math
from pathlib import Path
import selectors
import socket
import sys
import time


PROFILE = 'hil-observation-resident-run-1'
MAX_CONFIG_BYTES = 64 * 1024
CLOCK_FIELDS = ('max_rtt_us', 'max_offset_width_us', 'drift_ppm', 'lease_us', 'lookback_us')
PINNED_DEVELOPMENT_SHA256 = '32f79148b2ef9982b54eacc48da73f50d369f037fa4fe8aa4b0dec31217b4c0f'
PINNED_MOTION_SHA256 = 'd63ee963fe61cf2d6266d6d5b21606e20f1515038ed84386fa66abc6904e1e43'
PINNED_CALIBRATION_SHA256 = 'c11a8143f657c20db2cb38e822430b822326249dd90a451e67a8b66ef402217a'
PINNED_MAPPER_SHA256 = 'e80b7cb6d929eeaff0572598d6d57ad16f40fd8f4fac07c33beffc635903a061'
FOLLOW_MAPPER_SHA256 = 'a9f83a6de41d45c765baf4d342f1f25d9eeca89e5515fab7be8a5e38bc15c7bd'


def _need(value, reason):
    if not value:
        raise ValueError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        allow_nan=False).encode('ascii')


def _json(raw, cap):
    _need(type(raw) is bytes and 0 < len(raw) <= cap, 'bounded original JSON bytes required')

    def pairs(rows):
        result = {}
        for name, child in rows:
            _need(name not in result, 'duplicate configuration key')
            result[name] = child
        return result

    def constant(_):
        raise ValueError('nonfinite configuration number')

    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=pairs, parse_constant=constant)
        nodes = 0

        def visit(item, depth):
            nonlocal nodes
            nodes += 1
            _need(nodes <= 16384 and depth <= 16, 'configuration tree exceeds work bound')
            kind = type(item)
            if item is None or kind is bool:
                return
            if kind is int:
                _need(item.bit_length() <= 64, 'configuration integer exceeds bound')
            elif kind is float:
                _need(math.isfinite(item), 'nonfinite configuration number')
            elif kind is str:
                _need(len(item) <= cap and not any(0xd800 <= ord(c) <= 0xdfff for c in item),
                    'configuration string exceeds bound')
            elif kind is dict:
                _need(len(item) <= 128, 'configuration object exceeds bound')
                for key, child in item.items():
                    _need(type(key) is str and 0 < len(key) <= 128, 'configuration key exceeds bound')
                    visit(child, depth+1)
            elif kind is list:
                _need(len(item) <= 4096, 'configuration list exceeds bound')
                for child in item:
                    visit(child, depth+1)
            else:
                raise ValueError('closed native JSON required')
        visit(value, 0)
        _need(type(value) is dict, 'configuration object required')
        return value
    except (UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError('invalid bounded configuration JSON') from error


def _keys(value, keys):
    _need(type(value) is dict and set(value) == set(keys), 'missing or unknown configuration fields')


def _digest(value):
    _need(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value),
        'canonical SHA256 required')
    return value


def _read(path, cap):
    with Path(path).open('rb') as source:
        raw = source.read(cap+1)
    _need(0 < len(raw) <= cap, 'input file exceeds its original bound')
    return raw


def _path(base, value):
    _need(type(value) is str and 0 < len(value) <= 4096 and '\x00' not in value,
        'explicit bounded path required')
    path = Path(value).expanduser()
    return (base/path).absolute() if not path.is_absolute() else path.absolute()


def load_motion_profile(development_raw, motion_raw, *, expected_development_sha256,
                        expected_motion_sha256, expected_calibration_sha256):
    """원 개발 metadata를 constructor만으로 재구성한다. 원문·모든 파생 필드도 비교한다.

    이 함수는 SDK/clock/plant를 만들지 않는다. motion1은 명시 estimated 원문만,
    motion2는 원 가상 steering leaf를 재구성한다. 누락 필드에 기본값을 넣지 않는다.
    """
    from xenova_vehicle.simulation.profile import DevelopmentProfile
    from xenova_vehicle.simulation.motion_profile import DevelopmentMotionProfile
    from xenova_vehicle.simulation.steering_sensor import VirtualSteeringSensorProfile

    for digest in (expected_development_sha256, expected_motion_sha256, expected_calibration_sha256):
        _digest(digest)
    _need(type(development_raw) is bytes and 0 < len(development_raw) <= 16384
        and sha256(development_raw).hexdigest() == expected_development_sha256,
        'original development bytes SHA differs')
    _need(type(motion_raw) is bytes and 0 < len(motion_raw) <= 4096
        and sha256(motion_raw).hexdigest() == expected_motion_sha256,
        'original motion bytes SHA differs')
    development = DevelopmentProfile.from_bytes(development_raw)
    _need(development.canonical_bytes == development_raw, 'original canonical development bytes required')
    value = _json(motion_raw, 4096)
    profile = value.get('profile')
    names = {'profile', 'neutral_calibration_sha256', 'calibration_model_sha256', 'identity',
        'encoder', 'odometry', 'sensor', 'clock', 'qualification'}
    _need(profile in ('course-development-motion-1', 'course-development-motion-2'),
        'unsupported explicit motion metadata profile')
    _keys(value, names | ({'steering_sensor'} if profile == 'course-development-motion-2' else set()))
    _keys(value['encoder'], ('wheel_radius_m', 'encoder_cpr', 'metres_per_tick', 'metres_per_tick_upper',
        'track_width_m', 'encoder_bits', 'steering_error_rad'))
    _keys(value['odometry'], ('wheelbase_m', 'maximum_speed_mps', 'maximum_clock_error_us',
        'maximum_sample_age_us', 'sample_period_ms', 'position_error_m', 'heading_error_rad',
        'distance_error_ratio', 'heading_error_per_m'))
    _keys(value['sensor'], ('vehicle_max_age_us', 'max_yaw_rate_rps', 'max_gap_us', 'pose_capacity'))
    _keys(value['clock'], ('timestamp_error_us', 'drift_ppm', 'max_offset_width_us', 'lease_us', 'lookback_us'))
    sensor_profile = None
    if profile == 'course-development-motion-2':
        leaf = value['steering_sensor']
        _keys(leaf, ('leaf_sha256', 'profile', 'combined_wire_quality'))
        body = leaf['profile']
        _keys(body, ('profile', 'sensor_id', 'source_kind', 'quantization_rad', 'angle_error_rad',
            'timestamp_error_us', 'max_rate_rps', 'quantization_rule', 'wire_rule',
            'steering_quality_bit', 'physical_qualified'))
        sensor_profile = VirtualSteeringSensorProfile(sensor_id=body['sensor_id'],
            quantization_rad=body['quantization_rad'], angle_error_rad=body['angle_error_rad'],
            timestamp_error_us=body['timestamp_error_us'], max_rate_rps=body['max_rate_rps'])
        _need(_canonical(sensor_profile.fields) == _canonical(body)
            and sensor_profile.sha256 == leaf['leaf_sha256'], 'original steering model/leaf differs')
    encoder, sensor = value['encoder'], value['sensor']
    arguments = {name: child for name, child in value['odometry'].items() if name != 'wheelbase_m'}
    arguments.update(value['clock'])
    arguments.update(encoder_bits=encoder['encoder_bits'], metres_per_tick_upper=encoder['metres_per_tick_upper'],
        vehicle_max_age_us=sensor['vehicle_max_age_us'], max_yaw_rate_rps=sensor['max_yaw_rate_rps'],
        max_gap_us=sensor['max_gap_us'], pose_capacity=sensor['pose_capacity'],
        steering_sensor_profile=sensor_profile)
    motion = DevelopmentMotionProfile(development, **arguments)
    _need(motion.canonical_bytes == motion_raw and _canonical(motion.fields) == _canonical(value)
        and motion.sha256 == expected_motion_sha256
        and motion.bound_calibration.sha256 == expected_calibration_sha256,
        'reconstructed motion fields/canonical/whole bundle/local calibration differ')
    return motion


@dataclass(frozen=True, slots=True)
class ResidentInstallation:
    """원 caller 설정의 typed 사본이다. private JIO credential을 repr/JSON으로 내보내지 않는다."""
    profile: object = field(repr=False)
    capture_clock_policy: object
    storage: object
    state_directory: Path
    output: Path
    output_root: bool
    duration_s: float
    service_scope: str | None


def load_installation(path):
    """명시 실행 JSON을 읽고 원 metadata/profile의 핀을 검사한다. 실행·연결은 하지 않는다."""
    from xenova_vehicle.yolop_profile import load_yolop_profile
    from xenova_vehicle.runtime.mapper_profile import MapperProfile
    from xenova_vehicle.runtime.pc_resident_profile import PcResidentProfile
    from xenova_vehicle.runtime.resident_storage import StorageProfile
    from xenova_vehicle.runtime.slot_messages import SlotTimeLimits
    from xenova_vehicle.runtime.slot_cleanup import CleanupTimeLimits
    from xenova_vehicle.transport.jio_profile import JetsonJioProfile
    from .clock import ClockPolicy

    path = Path(path).expanduser().absolute()
    value = _json(_read(path, MAX_CONFIG_BYTES), MAX_CONFIG_BYTES)
    _keys(value, ('profile', 'inputs', 'jio_profile_path', 'storage', 'runtime', 'pc',
        'capture_clock', 'probe_interval_us', 'observation_duration_s', 'slot_limits', 'cleanup_limits'))
    _need(value['profile'] == PROFILE, 'explicit PC resident run profile required')
    inputs = value['inputs']
    _keys(inputs, ('yolop_profile', 'development_profile', 'motion_profile', 'mapper_profile',
        'calibration_profile_sha256'))
    raw, paths = {}, {}
    for name, cap in (('yolop_profile', 16384), ('development_profile', 16384),
            ('motion_profile', 4096), ('mapper_profile', 65536)):
        row = inputs[name]
        _keys(row, ('path', 'sha256'))
        paths[name] = _path(path.parent, row['path'])
        raw[name] = _read(paths[name], cap)
        _need(sha256(raw[name]).hexdigest() == _digest(row['sha256']), 'pinned original '+name+' bytes differ')
    # 원 e80 진단 또는 별도 a9f83 crossing 선언만 명시 선택한다. 원문을 보정하지 않는다.
    _need(inputs['development_profile']['sha256'] == PINNED_DEVELOPMENT_SHA256
        and inputs['motion_profile']['sha256'] == PINNED_MOTION_SHA256
        and inputs['mapper_profile']['sha256'] in (PINNED_MAPPER_SHA256, FOLLOW_MAPPER_SHA256)
        and inputs['calibration_profile_sha256'] == PINNED_CALIBRATION_SHA256,
        'this development assembly requires its original pinned metadata family')
    motion = load_motion_profile(raw['development_profile'], raw['motion_profile'],
        expected_development_sha256=inputs['development_profile']['sha256'],
        expected_motion_sha256=inputs['motion_profile']['sha256'],
        expected_calibration_sha256=inputs['calibration_profile_sha256'])
    mapper = MapperProfile.from_bytes(raw['mapper_profile'])
    _need(mapper.canonical_bytes == raw['mapper_profile'], 'original canonical mapper profile bytes required')
    oak = load_yolop_profile(paths['yolop_profile'])
    _need(_read(paths['yolop_profile'], 16384) == raw['yolop_profile'], 'YOLOP profile changed during public loader read')
    # 기존 loader만 credential 파일 소유자/mode/no-follow/상한을 검산한다. 내용은 기록하지 않는다.
    jio = JetsonJioProfile.load(_path(path.parent, value['jio_profile_path']))
    _keys(value['capture_clock'], CLOCK_FIELDS)
    capture_clock = ClockPolicy(**value['capture_clock'])
    _keys(value['slot_limits'], ('copy_budget_us', 'offer_budget_us', 'read_budget_us'))
    _keys(value['cleanup_limits'], ('transport_budget_us',))
    _keys(value['pc'], ('source_id', 'pc_clock_epoch', 'jetson_session', 'rgb_stream', 'depth_stream'))
    pc = value['pc']
    profile = PcResidentProfile(oak=oak, motion=motion, mapper=mapper, jio=jio,
        **pc, capture_clock_fields=tuple((name, value['capture_clock'][name]) for name in CLOCK_FIELDS),
        probe_interval_us=value['probe_interval_us'], observation_duration_s=value['observation_duration_s'],
        slot_limits=SlotTimeLimits(**value['slot_limits']), cleanup_limits=CleanupTimeLimits(**value['cleanup_limits']))
    storage = value['storage']
    _keys(storage, ('database_path', 'database_open_policy', 'schema_version', 'selection_policy'))
    storage = StorageProfile(**{**storage, 'database_path': str(_path(path.parent, storage['database_path']))})
    runtime = value['runtime']
    _keys(runtime, ('state_directory', 'output', 'output_root', 'duration_s', 'service_scope'))
    _need(type(runtime['output_root']) is bool and runtime['service_scope'] in (None, 'systemd'),
        'explicit output mode and existing systemd-or-null scope required')
    _need(type(runtime['duration_s']) in (int, float) and math.isfinite(runtime['duration_s'])
        and 0.1 <= runtime['duration_s'] <= 300, 'explicit bounded residence duration0.1..300seconds required')
    return ResidentInstallation(profile, capture_clock, storage,
        _path(path.parent, runtime['state_directory']), _path(path.parent, runtime['output']),
        runtime['output_root'], runtime['duration_s'], runtime['service_scope'])


def run_connected(installation, observation_endpoint):
    """caller가 연결한 endpoint 하나를 인수해 실제 A/S/M/D를 실행하고 반드시 닫는다.

    새 thread/listen/reconnect를 만들지 않는다. typed installation 검사 뒤 endpoint 소유권을
    넘겨받으며 import/기동 실패도 연결을 닫는다. 호출자는 그 뒤 이 endpoint를 사용하지 않는다.
    """
    _need(type(installation) is ResidentInstallation and type(observation_endpoint) is socket.socket,
        'original typed installation and caller-connected socket required')
    try:
        from xenova_vehicle.runtime.pc_resident_runtime import run_pc_runtime
        from .acquisition import PcAcquisitionLoop
        from .mapper import PcMapperLoop
        scope = None
        if installation.service_scope == 'systemd':
            from xenova_vehicle.runtime.service_scope import SystemdServiceScope
            scope = SystemdServiceScope()
        return run_pc_runtime(installation.profile, installation.state_directory, installation.storage,
            installation.output, observation_endpoint=observation_endpoint,
            acquisition_loop_factory=PcAcquisitionLoop, mapper_loop_factory=PcMapperLoop,
            capture_clock_policy=installation.capture_clock_policy, duration_s=installation.duration_s,
            service_scope=scope, output_root=installation.output_root)
    finally:
        observation_endpoint.close()


def accept_once(host, port, timeout_s):
    """명시 IPv4/port 한 연결만 유한 대기한다. listen socket은 반환 전에 닫는다."""
    _need(type(host) is str and str(ipaddress.IPv4Address(host)) == host,
        'explicit numeric IPv4 listen address required')
    _need(type(port) is int and 1 <= port <= 65535, 'explicit listen port1..65535 required')
    _need(type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 300,
        'explicit finite accept deadline within300seconds required')
    endpoint = None
    deadline = time.monotonic()+timeout_s
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener, selectors.DefaultSelector() as selector:
            listener.setblocking(False)
            listener.bind((host, port))
            listener.listen(1)
            selector.register(listener, selectors.EVENT_READ)
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('original explicit observation accept deadline elapsed')
                if not selector.select(remaining):
                    continue
                try:
                    endpoint, _ = listener.accept()
                except BlockingIOError:
                    continue
                endpoint.setblocking(False)
                _need(time.monotonic() < deadline, 'connection completed after original accept deadline')
                return endpoint
    except BaseException:
        if endpoint is not None:
            endpoint.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='명시 PC 관측 한 연결의 개발용 A/S/M/D 상주 실행')
    parser.add_argument('--installation', type=Path, required=True)
    parser.add_argument('--listen-host', required=True, help='명시 numeric IPv4, 자동 주소 선택 없음')
    parser.add_argument('--listen-port', type=int, required=True)
    parser.add_argument('--accept-timeout-s', type=float, required=True)
    args = parser.parse_args(argv)
    endpoint = None
    try:
        installation = load_installation(args.installation)
        endpoint = accept_once(args.listen_host, args.listen_port, args.accept_timeout_s)
        result = run_connected(installation, endpoint)
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        # private JSON·Paho 오류의 상세 문자열을 CLI에 복제하지 않는다.
        print('PC_RESIDENT_FAILED: '+type(error).__name__, file=sys.stderr)
        return 2
    finally:
        if endpoint is not None:
            endpoint.close()
    print(_canonical(dict(scope=result['scope'], complete=result['complete'], exit_code=result['exit_code'],
        service_instance=result['service_instance'], reason=result['reason'],
        physical_admission=False)).decode('ascii'))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
