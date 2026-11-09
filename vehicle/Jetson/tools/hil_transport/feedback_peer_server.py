#!/usr/bin/env python3
"""실제 RPi serial bridge를 기존 가상 Feedback3 peer/plant에 연결하는 개발 서버.

POSIX/WSL에서만 실행한다. TCP socket 자체를 원 peer FD로 주며 wire codec을 복제하지
않는다. C 앱/Gateway는 RPi에 남는다. renderer는 같은 plant.snapshot()의 reader이며
그 pose를 인식/지도/FREE의 근거로 전달하는 기능은 없다.
"""

import argparse
from hashlib import sha256
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import time
from uuid import UUID


def bounded_seconds(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('finite positive bounded experiment time required')
    return float(value)


def canonical_uuid(value):
    if type(value) is not str or str(UUID(value)) != value or not UUID(value).int:
        raise ValueError('explicit canonical nonzero session UUID required')
    return value


def accept_once(*, host, port, timeout_s, ready):
    """loopback 단일 연결만 수락한다. 반환 socket은 caller가 닫고 재사용하지 않는다."""
    if os.name != 'posix':
        raise RuntimeError('POSIX/WSL peer FD required; native Windows is unsupported')
    address = ipaddress.ip_address(host)
    if address.version != 4 or not address.is_loopback:
        raise ValueError('IPv4 loopback required; use SSH forwarding for remote RPi')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('explicit TCP port required')
    bounded_seconds(timeout_s, 120)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((str(address), port))
        listener.listen(1)
        listener.settimeout(timeout_s)
        ready({'kind': 'hil-peer-listening-1', 'host': str(address), 'port': port,
               'single_session': True, 'physical_qualified': False})
        connection, _ = listener.accept()
    try:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
        connection.setblocking(False)
        return connection
    except BaseException:
        connection.close()
        raise


def run_peer_session(connection, plant, protocol_profile, *, device_session,
                     duration_s, poll_interval_s, brake_drain_s,
                     on_session=None, cancelled=lambda: False):
    """동일 caller thread의 기존 peer.poll_once가 유일한 plant mutation owner다.

    on_session(plant, peer)는 renderer가 같은 plant를 읽도록 한 번 호출하는 접점이다.
    별도 plant.step/apply_target 소유자를 만들면 안 된다. EOF 뒤에는 원 peer의 target0/
    제동 dt를 유한하게 진행하되 실제 정지라고 보고하지 않는다. 재연결은 새 session이다.
    connection은 이 함수가 소유하며 정상·실패·취소 모두 닫는다.
    """
    peer = None
    result = None
    try:
        if os.name != 'posix':
            raise RuntimeError('POSIX/WSL required')
        canonical_uuid(device_session)
        duration_s = bounded_seconds(duration_s, 3600)
        poll_interval_s = bounded_seconds(poll_interval_s, .005)
        brake_drain_s = bounded_seconds(brake_drain_s, 1)
        from xenova_gateway import feedback, feedback3, scene_authority
        from xenova_vehicle.simulation.feedback_peer import VirtualFeedbackPeer
        caps = feedback3.REQUIRED_CAPS | 0x400
        if plant.profile.steering_sensor_profile is not None:
            caps |= feedback.CAP_STEERING
        peer = VirtualFeedbackPeer(connection.fileno(), plant, feedback=feedback,
            feedback3=feedback3, scene_authority=scene_authority,
            protocol_profile=protocol_profile,
            configuration_sha256=protocol_profile.vehicle_config_sha256,
            firmware_sha256=protocol_profile.firmware_sha256,
            device_session=device_session, capabilities=caps)
        # peer가 dup한 뒤 caller socket을 닫아 다른 FD가 EOF를 숨기지 않게 한다.
        connection.close()
        if on_session is not None:
            on_session(plant, peer)
        started = time.monotonic()
        failed_at = None
        while not cancelled() and time.monotonic() - started < duration_s:
            try:
                peer.poll_once()
            except Exception:
                if failed_at is None:
                    failed_at = time.monotonic()
            view = peer.snapshot()
            if (view['link_lost'] and failed_at is None):
                failed_at = time.monotonic()
            if not plant.snapshot().valid:
                break
            if failed_at is not None and time.monotonic() - failed_at >= brake_drain_s:
                break
            time.sleep(poll_interval_s)
        view = peer.snapshot()
        result = {'kind': 'hil-peer-session-ended-1', 'peer': view,
                  'physical_qualified': False, 'stationary_confirmed': False,
                  'closed_loop_accepted': False}
        return result
    finally:
        connection.close()
        if peer is not None:
            peer.close()
            if result is not None:
                result['peer'] = peer.snapshot()
        else:
            plant.stop()


def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise ValueError('duplicate experiment JSON key')
        value[key] = item
    return value


def _reject_constant(_):
    raise ValueError('nonfinite experiment JSON value')


def read_profile(path, expected_sha256, maximum):
    """선택한 원 파일만 읽는다. 같은 내용의 JSON 재직렬화를 원 bytes로 주장하지 않는다."""
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in '0123456789abcdef' for c in expected_sha256)):
        raise ValueError('explicit original file SHA256 required')
    with Path(path).open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('selected experiment file cap/SHA256 mismatch')
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    if type(value) is not dict:
        raise ValueError('experiment JSON object required')
    return raw, value


def make_inputs(args):
    """기존 생성자의 명시 local 가정을 사용한다. timing/hash/조향 측정 기본값은 없다."""
    from xenova_vehicle.simulation.profile import DevelopmentProfile
    from xenova_vehicle.simulation.motion_profile import DevelopmentMotionProfile
    from xenova_vehicle.simulation.steering_sensor import VirtualSteeringSensorProfile
    from xenova_gateway.feedback3 import Feedback3Profile
    from xenova_gateway.scene_authority import StationaryPolicy
    raw, _ = read_profile(args.development_profile, args.development_sha256, 16384)
    development = DevelopmentProfile.from_bytes(raw)
    _, motion = read_profile(args.motion_arguments, args.motion_sha256, 8192)
    if motion.get('steering_sensor_profile') is not None:
        motion['steering_sensor_profile'] = VirtualSteeringSensorProfile(**motion['steering_sensor_profile'])
    profile = DevelopmentMotionProfile(development, **motion)
    _, protocol = read_profile(args.feedback_profile, args.feedback_sha256, 8192)
    protocol['stationary'] = StationaryPolicy(**protocol['stationary'])
    return profile, Feedback3Profile(**protocol)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--accept-timeout-s', type=float, required=True)
    parser.add_argument('--duration-s', type=float, required=True)
    parser.add_argument('--poll-interval-s', type=float, required=True)
    parser.add_argument('--brake-drain-s', type=float, required=True)
    for name in ('development', 'motion', 'feedback'):
        parser.add_argument('--' + name + '-sha256', required=True)
    parser.add_argument('--development-profile', required=True)
    parser.add_argument('--motion-arguments', required=True)
    parser.add_argument('--feedback-profile', required=True)
    parser.add_argument('--device-session', required=True)
    parser.add_argument('--plant-epoch', required=True)
    parser.add_argument('--initial-pose', type=float, nargs=3, required=True, metavar=('X', 'Y', 'YAW'))
    args = parser.parse_args(argv)
    import signal
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    connection = None
    try:
        canonical_uuid(args.device_session)
        canonical_uuid(args.plant_epoch)
        motion, protocol = make_inputs(args)
        connection = accept_once(host=args.host, port=args.port, timeout_s=args.accept_timeout_s,
            ready=lambda row: print(json.dumps(row), flush=True))
        # accept 대기 전 plant를 만들면 첫 poll이 원 max_gap을 넘기므로 여기서 생성한다.
        from xenova_vehicle.simulation.plant import VirtualPlant
        plant = VirtualPlant(motion, initial_pose=tuple(args.initial_pose), epoch=args.plant_epoch)
        result = run_peer_session(connection, plant, protocol, device_session=args.device_session,
            duration_s=args.duration_s, poll_interval_s=args.poll_interval_s, brake_drain_s=args.brake_drain_s)
        print(json.dumps(result), flush=True)
        return 1 if result['peer']['link_lost'] or result['peer']['failure'] else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        print(json.dumps({'kind': 'hil-peer-failed-1', 'error': str(error)[:200]}), flush=True)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
