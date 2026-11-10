"""Linux spawn socket 상태와 실제 A 수신 owner의 초기 생성 경계를 검사한다.

실제 TCP socket, spawn child, 제어 BIND/ACK와 JetsonObservationPump를 사용한다.
RAW arena/manifest는 구조 검사용 software 선언이며 SHM attach, SDK/Device, NN,
frame/clock 교환, Source 또는 READY를 수락하는 시험이 아니다. Driver 생성 직전에
멈추며 child의 유한 종료와 socket close는 시험 caller가 직접 확인한다.
"""

import json
import multiprocessing
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from uuid import UUID


def _uid(value):
    return str(UUID(int=value))


def _settings(observation, metadata):
    from tools.hil_observation.acquisition import PcAcquisitionLoop
    from tools.hil_observation.clock import ClockPolicy
    from tools.hil_observation.transport import SessionScope
    from xenova_vehicle.oak import sim_nn_manifest as manifests
    from xenova_vehicle.oak.sim_nn import SimNnProfile
    from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
    from xenova_vehicle.runtime.pc_workers import pc_worker_release
    from xenova_vehicle.runtime.slot_messages import SlotTimeLimits
    from xenova_vehicle.simulation.pc_input import PcSourceBinding
    from xenova_vehicle.yolop_profile import YolopBenchProfile

    fields = dict(profile='development-pc-source-1', service_instance=_uid(1),
        source_id=_uid(10), pc_clock_epoch=_uid(11), jetson_session=_uid(12),
        stream_instances=dict(rgb_video=_uid(13), depth_right=_uid(14)),
        destination_clock_epoch=_uid(15), calibration_profile_sha256='c' * 64,
        motion_profile_sha256='b' * 64, nn_manifest_sha256='d' * 64)
    profile = SimNnProfile(YolopBenchProfile('software-only-no-device',
        Path('software-only-assets'), Path('software-only-manifest.json')),
        'sim-pc:' + fields['source_id'], 'jetson-monotonic:' + fields['destination_clock_epoch'],
        fields['calibration_profile_sha256'])
    # from_bytes는 구조만 검사한다. 실제 blob/SDK start receipt로 사용하지 않는다.
    manifest = manifests.SimNnManifest.from_bytes(
        manifests._canonical(manifests._fields(profile, manifests._asset_summary())))
    fields['nn_manifest_sha256'] = manifest.sha256
    binding = PcSourceBinding(fields)
    scope = SessionScope(fields['source_id'], fields['jetson_session'], binding.sha256,
        fields['calibration_profile_sha256'], fields['motion_profile_sha256'], fields['pc_clock_epoch'])
    release = pc_worker_release()
    mapper = LifecyclePeer(_uid(1), _uid(4), _uid(5), 'M', release)
    settings = PcAcquisitionLoop(observation, metadata, mapper, scope.raw_bytes,
        ClockPolicy(max_rtt_us=150_000, max_offset_width_us=150_000, drift_ppm=100,
            lease_us=1_000_000, lookback_us=500_000),
        1.0, 50_000, SlotTimeLimits(150_000, 150_000, 150_000))
    return settings, binding.canonical_bytes, manifest.canonical_bytes, profile, release


def _socket_state(endpoint):
    import fcntl

    return dict(blocking=endpoint.getblocking(), timeout=endpoint.gettimeout(),
        os_nonblocking=bool(fcntl.fcntl(endpoint.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK))


class _PumpConstructed(Exception):
    """실제 pump 생성 뒤 heavy A driver로 진입하지 않는 시험 전용 종료다."""


def _spawn_acquisition(settings, binding_bytes, manifest_bytes, profile, release, result_pipe):
    result = dict(error=None)
    actor = supervisor = None
    control_a = control_s = None
    try:
        result['before'] = _socket_state(settings.observation_endpoint)
        result['default_timeout'] = socket.getdefaulttimeout()
        from tools.hil_observation import acquisition
        from tools.hil_observation.mapped import MappedPcOwner
        from tools.hil_observation.session import JetsonObservationPump, ObservationSessionError
        from tools.hil_observation.transport import SessionScope
        from xenova_vehicle.runtime.lifecycle_messages import LifecyclePeer
        from xenova_vehicle.runtime.native_resources import ArenaSpec
        from xenova_vehicle.runtime.pc_workers import PcWorkerActor, PcWorkerBoot
        from xenova_vehicle.runtime.raw_leases import ARENA_BYTES, Endpoint, RawScope
        from xenova_vehicle.runtime.resident_transport import ResidentLink, j_us
        from xenova_vehicle.simulation.pc_input import PcSourceBinding

        binding = PcSourceBinding.from_bytes(binding_bytes)
        scope = SessionScope(**json.loads(settings.scope_bytes))
        # 원 session guard도 그대로 실패한다. 실패 ctor는 이 별도 dup만 닫는다.
        duplicate = socket.socket(settings.observation_endpoint.family,
            settings.observation_endpoint.type, settings.observation_endpoint.proto,
            fileno=os.dup(settings.observation_endpoint.fileno()))
        try:
            rejected = JetsonObservationPump(duplicate, binding=binding, scope=scope,
                clock_policy=settings.clock_policy, destination_clock_id=binding.clock_id,
                duration_s=settings.duration_s, probe_interval_us=settings.probe_interval_us,
                on_frame=lambda frame, owner: False, cancelled=lambda: False)
        except ObservationSessionError as error:
            result['original_guard'] = str(error)
        else:
            rejected.close()
            raise AssertionError('spawn mirror unexpectedly passed the unchanged session guard')
        finally:
            duplicate.close()

        peer = LifecyclePeer(_uid(1), _uid(2), _uid(3), 'A', release)
        writer = Endpoint(peer.channel_epoch, peer.worker_instance)
        arena = ArenaSpec(_uid(40), '/xnv_raw_' + _uid(40), ARENA_BYTES, 'RAW')
        raw_scope = RawScope(peer.service_instance, arena.arena_id, writer, binding.sha256,
            arena.name, arena.size_bytes, arena.role)
        context = multiprocessing.get_context('spawn')
        tokens = tuple((context.Semaphore(1), context.Semaphore(0)) for _ in range(2))
        now = j_us()
        boot = PcWorkerBoot(peer, peer, profile, raw_scope, arena, binding_bytes,
            manifest_bytes, tokens, now + 5_000_000, 'software-only-no-diagnostics')
        control_s, control_a = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        supervisor = ResidentLink(control_s, peer, 'S')
        actor = PcWorkerActor(control_a, boot)
        hello = supervisor.poll()
        if len(hello) != 1 or hello[0].kind != 'HELLO':
            raise AssertionError('actual actor HELLO was not received')
        issued = j_us()
        supervisor.send('BIND', dict(binding_revision='1', binding_sha256=binding.sha256,
            binding=binding.fields), domain='RAW', revision=1, request_id=_uid(20),
            issued_us=issued, expires_us=min(issued + 5_000_000, boot.startup_deadline_us))
        actor.await_bind()
        received = supervisor.poll()
        result['actual_bind_ack'] = (actor.ack_sent is not None
            and any(packet.kind == 'BIND_ACK' and packet.raw_bytes == actor.ack.packet.raw_bytes
                for packet in received))

        def stop_after_pump(actual_actor, actual_link, actual_mapper, pc_owner):
            if (actual_actor is not actor or actual_mapper is not settings.mapper_peer
                    or type(pc_owner) is not MappedPcOwner):
                raise AssertionError('run_acquisition did not pass the actual pump owner')
            result['normalized'] = _socket_state(settings.observation_endpoint)
            result['same_metadata_endpoint'] = actual_link.endpoint is settings.metadata_endpoint
            try:
                settings.observation_endpoint.recv(1)
            except BlockingIOError:
                result['recv_would_block'] = True
            else:
                raise AssertionError('empty live TCP peer must yield BlockingIOError')
            raise _PumpConstructed()

        # 실제 pump 생성은 wraps로 실행하며 다음 driver 생성만 중단한다.
        with patch.object(acquisition, 'JetsonObservationPump', wraps=JetsonObservationPump) as constructor, \
                patch.object(acquisition, 'PcAcquisitionDriver', side_effect=stop_after_pump), \
                patch.object(JetsonObservationPump, 'step', side_effect=AssertionError('frame loop forbidden')) as step:
            try:
                acquisition.run_acquisition(actor, settings)
            except _PumpConstructed:
                pass
            else:
                raise AssertionError('test boundary was not reached')
            result['actual_pump_constructors'] = constructor.call_count
            result['pump_steps'] = step.call_count
        result['model_not_started'] = actor._nn_source is None
        result['observation_closed'] = settings.observation_endpoint.fileno() == -1
        result['metadata_closed'] = settings.metadata_endpoint.fileno() == -1
    except BaseException as error:
        result['error'] = dict(type=type(error).__name__, message=str(error)[:1024])
    finally:
        for endpoint in (settings.observation_endpoint, settings.metadata_endpoint, control_a, control_s):
            if endpoint is not None:
                endpoint.close()
        try:
            result_pipe.send(result)
        finally:
            result_pipe.close()


class PcAcquisitionSpawnTests(unittest.TestCase):
    def test_spawn_reconstructed_socket_is_normalized_by_actual_acquisition_owner(self):
        context = multiprocessing.get_context('spawn')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote = observation = metadata_a = metadata_s = None
        receiver = sender = process = None
        try:
            listener.settimeout(2)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            remote = socket.create_connection(listener.getsockname(), timeout=2)
            observation, _ = listener.accept()
            observation.setblocking(False)
            self.assertEqual(_socket_state(observation),
                dict(blocking=False, timeout=0.0, os_nonblocking=True))
            metadata_s, metadata_a = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            metadata_a.setblocking(False)
            args = _settings(observation, metadata_a)
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_spawn_acquisition, args=(*args, sender))
            process.start()
            sender.close()
            observation.close()
            metadata_a.close()
            self.assertTrue(receiver.poll(20), 'spawn constructor-only child exceeded its test watchdog')
            result = receiver.recv()
            process.join(2)
            self.assertFalse(process.is_alive(), 'constructor-only child must be reaped')
            self.assertEqual(process.exitcode, 0)
            self.assertIsNone(result['error'], result['error'])
            self.assertIsNone(result['default_timeout'])
            self.assertEqual(result['before'], dict(blocking=True, timeout=None, os_nonblocking=True))
            self.assertEqual(result['original_guard'], 'NONBLOCKING_ENDPOINT_REQUIRED')
            self.assertEqual(result['normalized'], dict(blocking=False, timeout=0.0, os_nonblocking=True))
            self.assertTrue(result['actual_bind_ack'])
            self.assertTrue(result['same_metadata_endpoint'])
            self.assertTrue(result['recv_would_block'])
            self.assertEqual(result['actual_pump_constructors'], 1)
            self.assertEqual(result['pump_steps'], 0)
            self.assertTrue(result['model_not_started'])
            self.assertTrue(result['observation_closed'])
            self.assertTrue(result['metadata_closed'])
        finally:
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                self.assertFalse(process.is_alive(), 'test caller must reap its own spawn child')
                process.close()
            for endpoint in (listener, remote, observation, metadata_a, metadata_s, receiver, sender):
                if endpoint is not None:
                    endpoint.close()


if __name__ == '__main__':
    unittest.main()
