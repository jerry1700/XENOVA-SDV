"""같은 PC 가상 plant의 Feedback3와 관측 송신을 조립하는 명시 개발 입구다.

C/Gateway는 실제 Pi에서 실행한다. 이 도구는 기존 peer 서버의 단일 물리 owner와
별도 renderer/session reader만 연결한다. SSH·자격·Pi 서비스·Nano 실행을 만들지 않고,
caller가 복사한 실제 S bootstrap metadata를 기다린다. import만으로 실행하지 않는다.
"""

import argparse
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time


BOOTSTRAP_PROFILE = 'pc-observation-bootstrap-copy-1'


def _need(value, reason):
    if not value:
        raise ValueError(reason)


def _pairs(rows):
    result = {}
    for key, value in rows:
        _need(key not in result, 'duplicate public bootstrap key')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('nonfinite public bootstrap value')


def _read(path, cap):
    path = Path(path)
    _need(path.is_file() and not path.is_symlink(), 'original regular public metadata required')
    with path.open('rb') as source:
        raw = source.read(cap+1)
    _need(0 < len(raw) <= cap, 'public metadata exceeds its bound')
    return raw


def _digest(value):
    _need(type(value) is str and len(value) == 64
        and all(c in '0123456789abcdef' for c in value), 'canonical public metadata SHA256 required')
    return value


def load_bootstrap(receipt_path, motion):
    """별도 SSH copy caller가 마지막에 게시한 원 두 파일과 SHA를 검산한다.

    receipt는 로컬 복사 거래의 선언이다. S의 실제 publication·clock·READY 증거는
    아니며 Nano는 자기 descriptor와 실제 session 자료를 별도로 검산한다.
    """
    from xenova_vehicle.simulation.pc_input import PcSourceBinding
    from .transport import SessionScope
    receipt_path = Path(receipt_path).absolute()
    receipt_raw = _read(receipt_path, 4096)
    value = json.loads(receipt_raw, object_pairs_hook=_pairs, parse_constant=_constant)
    _need(type(value) is dict and set(value) == {'profile', 'binding', 'scope'}
        and value['profile'] == BOOTSTRAP_PROFILE, 'exact public bootstrap-copy declaration required')
    originals = {}
    for name in ('binding', 'scope'):
        row = value[name]
        _need(type(row) is dict and set(row) == {'path', 'bytes', 'sha256'}
            and type(row['path']) is str and 0 < len(row['path']) <= 256
            and type(row['bytes']) is int and 0 < row['bytes'] <= 16384,
            'exact bounded public bootstrap member required')
        path = receipt_path.parent/row['path']
        _need(not Path(row['path']).is_absolute() and path.resolve().is_relative_to(receipt_path.parent.resolve()),
            'public bootstrap member must stay within its copy directory')
        raw = _read(path, row['bytes'])
        _need(len(raw) == row['bytes'] and sha256(raw).hexdigest() == _digest(row['sha256']),
            'public bootstrap original bytes/SHA differ')
        originals[name] = raw
    binding = PcSourceBinding.from_bytes(originals['binding'])
    binding.check_profile(motion)
    fields = binding.fields
    scope = SessionScope(fields['source_id'], fields['jetson_session'], binding.sha256,
        fields['calibration_profile_sha256'], fields['motion_profile_sha256'], fields['pc_clock_epoch'])
    _need(binding.canonical_bytes == originals['binding'] and scope.raw_bytes == originals['scope'],
        'original canonical S descriptor/scope pair required')
    return binding, scope, dict(profile=BOOTSTRAP_PROFILE,
        receipt_sha256=sha256(receipt_raw).hexdigest(), binding_sha256=binding.sha256,
        scope_sha256=sha256(originals['scope']).hexdigest(), authority=False)


def wait_bootstrap(path, timeout_s, cancelled):
    """원 copy 대기 cap 안에서 파일 존재만 기다린다. 잘못 게시한 파일은 재시도하지 않는다."""
    from tools.hil_transport.feedback_peer_server import bounded_seconds
    deadline = time.monotonic()+bounded_seconds(timeout_s, 120)
    while not Path(path).exists():
        if cancelled():
            raise InterruptedError('public bootstrap wait cancelled')
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('original public bootstrap copy deadline elapsed')
        time.sleep(min(.01, remaining))
    _need(not cancelled() and time.monotonic() < deadline, 'bootstrap arrived outside original wait cap')


def _arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='기존 Feedback3 listener의 명시 loopback IPv4')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--accept-timeout-s', type=float, required=True)
    parser.add_argument('--duration-s', type=float, required=True)
    parser.add_argument('--poll-interval-s', type=float, required=True)
    parser.add_argument('--brake-drain-s', type=float, required=True)
    for name in ('development', 'motion', 'feedback'):
        parser.add_argument('--'+name+'-sha256', required=True)
    parser.add_argument('--development-profile', required=True)
    parser.add_argument('--motion-arguments', required=True)
    parser.add_argument('--feedback-profile', required=True)
    parser.add_argument('--device-session', required=True)
    parser.add_argument('--plant-epoch', required=True)
    parser.add_argument('--initial-pose', type=float, nargs=3, required=True, metavar=('X', 'Y', 'YAW'))
    parser.add_argument('--scene', required=True)
    parser.add_argument('--scene-sha256', required=True)
    parser.add_argument('--capture-epoch', required=True)
    parser.add_argument('--capture-lifetime-us', type=int, required=True)
    parser.add_argument('--render-interval-us', type=int, required=True)
    parser.add_argument('--observation-host', required=True, help='실제 Nano 또는 기존 SSH forward의 numeric IPv4')
    parser.add_argument('--observation-port', type=int, required=True)
    parser.add_argument('--observation-connect-timeout-s', type=float, required=True)
    parser.add_argument('--observation-duration-s', type=float, required=True)
    parser.add_argument('--bootstrap-receipt', required=True)
    parser.add_argument('--bootstrap-timeout-s', type=float, required=True)
    parser.add_argument('--join-timeout-s', type=float, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = _arguments(argv)
    stopped = threading.Event()
    previous = {}
    connection = producer = feedback_connection = None
    result = None
    peer_running = closing = False
    def stop_requested(*_):
        stopped.set()
        # blocking connect/accept도 정리로 간다. 실제 peer loop에서는 원 cancel 절차를 쓴다.
        if not peer_running and not closing:
            raise InterruptedError('PC orchestration interrupted before peer loop')
    try:
        _need(os.name == 'posix', 'PC virtual-device role requires existing POSIX/WSL environment')
        from tools.hil_transport.feedback_peer_server import (accept_once, bounded_seconds,
            canonical_uuid, make_inputs, read_profile, run_peer_session)
        from xenova_vehicle.simulation.scene import Scene
        from xenova_vehicle.simulation.plant import VirtualPlant
        from .producer import PcObservationProducer
        canonical_uuid(args.device_session)
        canonical_uuid(args.plant_epoch)
        canonical_uuid(args.capture_epoch)
        motion, protocol = make_inputs(args)
        _need(protocol.vehicle_config_sha256 == motion.sha256,
            'PC Feedback3 configuration differs from the original motion bundle')
        scene_raw, _ = read_profile(args.scene, args.scene_sha256, 65536)
        scene = Scene(scene_raw)
        motion.development_profile.check_scene(scene)
        for value in (args.observation_connect_timeout_s, args.bootstrap_timeout_s):
            bounded_seconds(value, 120)
        bounded_seconds(args.accept_timeout_s, 120)
        bounded_seconds(args.duration_s, 300)
        bounded_seconds(args.observation_duration_s, 300)
        bounded_seconds(args.poll_interval_s, .005)
        bounded_seconds(args.brake_drain_s, 1)
        bounded_seconds(args.join_timeout_s, 5)
        host = str(ipaddress.IPv4Address(args.observation_host))
        _need(host == args.observation_host and type(args.observation_port) is int
            and 1 <= args.observation_port <= 65535, 'explicit numeric observation endpoint required')
        # 첫 import 비용을 실제 plant의100ms dt와 session의200ms I/O에 섞지 않는다.
        import cv2
        import numpy
        del cv2, numpy
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop_requested)
        connection = socket.create_connection((host, args.observation_port), args.observation_connect_timeout_s)
        # 분리 송신하는 작은 clock prefix/header를 Nagle 대기로 붙잡지 않는다.
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setblocking(False)
        print(json.dumps(dict(kind='pc-observation-connected-1', bootstrap_pending=True, authority=False)), flush=True)
        wait_bootstrap(args.bootstrap_receipt, args.bootstrap_timeout_s, stopped.is_set)
        binding, scope, bootstrap = load_bootstrap(args.bootstrap_receipt, motion)
        print(json.dumps(dict(kind='pc-observation-bootstrap-read-1', **bootstrap)), flush=True)
        producer = PcObservationProducer(connection, binding=binding, scope=scope,
            motion_profile=motion, scene=scene, capture_epoch=args.capture_epoch,
            capture_clock_epoch=scope.fields['pc_clock_epoch'], duration_s=args.observation_duration_s,
            capture_lifetime_us=args.capture_lifetime_us, render_interval_us=args.render_interval_us,
            join_timeout_s=args.join_timeout_s)
        connection = None
        # Pi 서비스/accept를 기다리는 동안에도 원 A clock probe에 응답한다.
        producer.start_session()
        feedback_connection = accept_once(host=args.host, port=args.port, timeout_s=args.accept_timeout_s,
            ready=lambda row: print(json.dumps(row), flush=True))
        state = producer.snapshot()
        _need(not stopped.is_set() and not state['session_done'] and not state['failed'],
            'original observation session ended before actual feedback attach')
        plant = VirtualPlant(motion, initial_pose=tuple(args.initial_pose), epoch=args.plant_epoch)
        def cancelled():
            state = producer.snapshot()
            return stopped.is_set() or state['session_done'] or state['failed']
        peer_running = True
        result = run_peer_session(feedback_connection, plant, protocol, device_session=args.device_session,
            duration_s=args.duration_s, poll_interval_s=args.poll_interval_s, brake_drain_s=args.brake_drain_s,
            on_session=producer.attach, cancelled=cancelled)
        feedback_connection = None
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, ImportError) as error:
        print(json.dumps(dict(kind='pc-observation-run-failed-1', error=type(error).__name__, authority=False)), flush=True)
    finally:
        closing = True
        if feedback_connection is not None:
            feedback_connection.close()
        if connection is not None:
            connection.close()
        if producer is not None:
            state = producer.close()
            print(json.dumps(dict(kind='pc-observation-producer-ended-1', **state)), flush=True)
        for number, handler in previous.items():
            signal.signal(number, handler)
    if result is None:
        return 1
    print(json.dumps(result), flush=True)
    state = producer.snapshot()
    return 1 if (state['failed'] or not state['closed'] or result['peer']['link_lost']
        or result['peer']['failure']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
