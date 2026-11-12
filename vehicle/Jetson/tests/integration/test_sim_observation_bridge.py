"""mailbox/파일 export 경계의 software fixture다. 실제 finish_capture/NN 수락 시험은 아니다.

원 완료 DTO class와 실제 exporter를 사용하되 내부 pose/FD/context는 명시 최소 자료다.
caller fixture만 non-daemon diagnostic thread를 만들고 stop/join한다. library는 만들지 않는다.
"""

from dataclasses import dataclass
import gc
import json
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sim_observation_bridge as bridging
from sim_mapper_motion_rig import MapperMotionObservation, SimMapperMotionRig
from sim_observation_bridge import SimObservationBridge
import test_sim_observation_export as export_fixtures


@dataclass(frozen=True)
class _PoseSample:
    epoch: str
    clock_id: str
    calibration_id: str
    at_us: int
    x: float
    y: float
    yaw: float
    position_error_m: float
    yaw_error_rad: float
    origin: str


@dataclass(frozen=True)
class _Pose:
    sample: _PoseSample
    captured_us: tuple
    bracket_us: tuple


def original(sequence=1, *, source=None, fd=None):
    """실제 actor 승인/원 wire를 만들지 않는 exact DTO의 명시 software leaf다."""
    fields, image = export_fixtures.original(sequence)
    if source is not None:
        fields['capture']['source_id'] = source
        image.rgb.stamp.source_id = image.depth.stamp.source_id = source
    pose = fields['pose']
    pose = _Pose(_PoseSample(**pose['sample']), tuple(pose['captured_us']), tuple(pose['bracket_us']))
    capture = SimpleNamespace(observation=image, receipt=fields['capture'])
    context = SimpleNamespace(sha256=fields['context_sha256'], fields={'fixture': 'NOT_CURRENT_ACTOR_CONTEXT'})
    inputs = SimpleNamespace(pose=pose, storage_context=context, reasons=())
    snapshot = SimpleNamespace(job_id='00000000-0000-4000-8000-000000000020',
        source_generation='software-only-not-raw-registration', source_key=image.rgb.stamp.key
        if hasattr(image.rgb.stamp, 'key') else (image.rgb.stamp.source_id, 'rgb_video', image.rgb.stamp.clock_id, sequence),
        snapshot_j_us=int(fields['checked_j_us']), expires_us=int(fields['capture']['expires_j_us']),
        fence_revision=0, byte_length=0, p4_evidence=None)
    return MapperMotionObservation(SimpleNamespace(fields=fields['fd']) if fd is None else fd,
        capture, b'{"fixture":"not-an-actor-context"}', inputs, SimpleNamespace(originals=()),
        b'software-probe-not-protobuf', b'software-trust-not-protobuf', (), (),
        int(fields['checked_j_us']), (), snapshot)


class _DiagnosticOwner:
    """명시 caller thread와 한 command/reply다. 제품 thread inventory 증거는 아니다."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.commands = queue.Queue(maxsize=1)
        self.replies = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, daemon=False, name='test-diagnostic-owner')
        self.thread.start()

    def _run(self):
        while True:
            name = self.commands.get()
            if name is None:
                return
            try:
                value = getattr(self.bridge, name)()
                self.replies.put((True, value), timeout=2)
            except BaseException as error:
                self.replies.put((False, error), timeout=2)

    def submit(self, name):
        self.commands.put(name, timeout=2)

    def complete(self):
        ok, value = self.replies.get(timeout=5)
        if not ok:
            raise value
        return value

    def call(self, name):
        self.submit(name)
        return self.complete()

    def close(self):
        if not self.thread.is_alive():
            return
        try:
            self.call('close')
        finally:
            self.commands.put(None, timeout=2)
            self.thread.join(2)
            if self.thread.is_alive():
                raise AssertionError('caller diagnostic owner was not joined')


class SimObservationBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'snapshot'

    def owner(self, bridge):
        value = _DiagnosticOwner(bridge)
        self.addCleanup(value.close)
        return value

    def test_offer_is_exact_reference_only_without_fields_json_hash_or_export_io(self):
        bridge = SimObservationBridge(self.directory)
        value = original()
        def forbidden(_):
            raise AssertionError('offer must not read/serialize fields')
        with patch.object(MapperMotionObservation, 'fields', property(forbidden)), \
                patch.object(bridging.json, 'dumps', side_effect=AssertionError('JSON in offer')), \
                patch.object(bridging, 'SimObservationExporter', side_effect=AssertionError('I/O in offer')):
            self.assertTrue(bridge.offer(value))
        self.assertIs(bridge._pending, value)
        self.assertFalse(self.directory.exists())
        with self.assertRaises(ValueError):
            bridge.offer(SimpleNamespace())
        self.owner(bridge).call('close')
        self.assertIsNone(bridge._pending)

    def test_offer_trylock_drop_and_counters_saturate_without_waiting_or_replacing_original(self):
        bridge = SimObservationBridge(self.directory)
        first = original()
        bridge.offer(first)
        bridge._lock.acquire()
        try:
            self.assertFalse(bridge.offer(original(2)))
        finally:
            bridge._lock.release()
        self.assertIs(bridge._pending, first)
        self.assertEqual(bridge.snapshot()['counts']['dropped_busy'], 1)
        bridge._offer_counts = (bridging._COUNTER_MAX,) * 4
        self.assertTrue(bridge.offer(original(3)))
        self.assertEqual(tuple(bridge.snapshot()['counts'][key] for key in bridging._OFFER_KEYS),
            (bridging._COUNTER_MAX,) * 4)
        self.owner(bridge).call('close')

    def test_latest_replacement_releases_original_reference_after_unlock(self):
        bridge = SimObservationBridge(self.directory)
        released = []
        class Leaf:
            def __del__(self):
                released.append(bridge._lock.locked())
        value = original(fd=Leaf())
        bridge.offer(value)
        del value
        bridge.offer(original(2))
        gc.collect()
        self.assertEqual(released, [False])
        self.assertEqual(bridge.snapshot()['counts']['replaced'], 1)
        owner = self.owner(bridge)
        self.assertTrue(owner.call('start'))
        self.assertEqual(owner.call('drain').source_sequence, 2)

    def test_roles_require_independent_single_diagnostic_owner_and_original_producer(self):
        bridge = SimObservationBridge(self.directory)
        for name in ('start', 'drain', 'close'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                getattr(bridge, name)()
        owner = self.owner(bridge)
        self.assertTrue(owner.call('start'))
        foreign = self.owner(bridge)
        with self.assertRaises(RuntimeError):
            foreign.call('drain')
        # foreign.close에도 role 오류가 나므로 이 helper의 thread만 caller가 종료한다.
        foreign.commands.put(None, timeout=2)
        foreign.thread.join(2)
        self.assertFalse(foreign.thread.is_alive())
        errors = []
        def offer_from_foreign():
            try:
                bridge.offer(original())
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=offer_from_foreign, daemon=False)
        thread.start(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(bridge.snapshot()['pending'])

    def test_rig_rejects_foreign_producer_bridge_before_fd_or_product_startup(self):
        returned = queue.Queue(maxsize=1)
        def make():
            returned.put(SimObservationBridge(self.directory), timeout=2)
        maker = threading.Thread(target=make, daemon=False)
        maker.start(); maker.join(2)
        self.assertFalse(maker.is_alive())
        bridge = returned.get(timeout=2)
        rig = SimMapperMotionRig.__new__(SimMapperMotionRig)
        # observer 역할 거절은 실제 FD/profile constructor imports보다 먼저 일어나야 한다.
        import builtins
        actual_import = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'sim_fd_motion_rig' or name.startswith(('xenova_vehicle.', 'xenova_gateway.')):
                raise AssertionError('product import before observer producer validation')
            return actual_import(name, *args, **kwargs)
        with patch.object(builtins, '__import__', importing), self.assertRaises(RuntimeError):
            rig.__init__(None, raw_binding=None, mapper_instance=None, mapper_boot=None,
                source_policy=None, clock_policy=None, coherent_platform_root=None, broker_binary=None,
                broker_workdir=None, startup_timeout_s=None, poll_interval_s=None,
                broker_startup_timeout_s=None, cleanup_timeout_s=None, observer=bridge)
        self.assertFalse(self.directory.exists())

    def test_actual_export_reads_fields_once_and_keeps_original_stamp_bytes_and_unknown_authority(self):
        bridge = SimObservationBridge(self.directory)
        owner = self.owner(bridge)
        self.assertTrue(owner.call('start'))
        value = original()
        field_reads = []
        observed_threads = []
        getter = MapperMotionObservation.fields.fget
        export = bridge._exporter.export
        def fields(item):
            field_reads.append(item)
            return getter(item)
        def observed(raw, image, **kwargs):
            observed_threads.append(threading.get_ident())
            self.assertIs(image, value.observation)
            self.assertEqual(kwargs, {'display_gt_layers': None})
            return export(raw, image, **kwargs)
        with patch.object(MapperMotionObservation, 'fields', property(fields)), \
                patch.object(bridge._exporter, 'export', observed):
            bridge.offer(value)
            result = owner.call('drain')
        self.assertEqual(field_reads, [value])
        self.assertEqual(observed_threads, [owner.thread.ident])
        self.assertEqual(result.status, 'WRITTEN')
        body = json.loads((self.directory / 'snapshot.json').read_bytes())
        raw = (self.directory / body['raw_payload']['path']).read_bytes()
        fields = json.loads(raw)
        self.assertEqual(fields['checked_j_us'], str(value.checked_j_us))
        self.assertEqual(fields['capture'], value.capture.receipt)
        self.assertIsNone(body['operation'])
        self.assertEqual(body['source']['kind'], 'OFFLINE_SYNTHETIC')
        self.assertFalse(any(layer['kind'] == 'GT' for layer in body['map']['layers']))
        for item, frame in zip(body['media'], (value.observation.rgb, value.observation.depth)):
            self.assertEqual((self.directory / item['path']).read_bytes(), frame.payload)
        status = bridge.snapshot()
        self.assertFalse(status['active']); self.assertFalse(status['pending'])
        status['counts']['written'] = -1
        self.assertEqual(bridge.snapshot()['counts']['written'], 1)
        self.assertIsNone(owner.call('drain'))

    def test_slow_diagnostic_export_keeps_active_one_and_latest_pending_one_without_blocking_producer(self):
        bridge = SimObservationBridge(self.directory)
        owner = self.owner(bridge)
        owner.call('start')
        entered, release = threading.Event(), threading.Event()
        export = bridge._exporter.export
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('caller did not release its diagnostic fixture')
            return export(*args, **kwargs)
        first, second, third = original(), original(2), original(3)
        bridge.offer(first)
        with patch.object(bridge._exporter, 'export', slow):
            owner.submit('drain')
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(bridge.offer(second))
                self.assertTrue(bridge.offer(third))
                self.assertIs(bridge._active, first)
                self.assertIs(bridge._pending, third)
                self.assertEqual(bridge.snapshot()['counts']['replaced'], 1)
            finally:
                release.set()
                result = owner.complete()
        self.assertEqual(result.source_sequence, 1)
        self.assertEqual(owner.call('drain').source_sequence, 3)
        self.assertEqual(bridge.snapshot()['counts']['written'], 2)

    def test_export_failure_is_sticky_diagnostic_and_preserves_previous_snapshot_and_original_result(self):
        bridge = SimObservationBridge(self.directory)
        owner = self.owner(bridge)
        owner.call('start'); bridge.offer(original()); owner.call('drain')
        previous = (self.directory / 'snapshot.json').read_bytes()
        value = original(2)
        original_receipt = dict(value.capture.receipt)
        with patch.object(bridge._exporter, 'export', side_effect=OSError('declared diagnostic disk failure')):
            bridge.offer(value)
            self.assertIsNone(owner.call('drain'))
        self.assertEqual(value.capture.receipt, original_receipt)
        self.assertEqual((self.directory / 'snapshot.json').read_bytes(), previous)
        self.assertFalse(bridge.offer(original(3)))
        state = bridge.snapshot()
        self.assertEqual(state['state'], 'FAILED')
        self.assertEqual(state['failure']['stage'], 'EXPORT')
        self.assertEqual(state['failure']['error'], 'OSError')
        self.assertEqual(state['counts']['failures'], 1)
        self.assertIsNone(owner.call('drain'))

    def test_source_change_requires_new_dedicated_folder_and_does_not_replace_old_snapshot(self):
        bridge = SimObservationBridge(self.directory)
        owner = self.owner(bridge)
        owner.call('start'); bridge.offer(original()); owner.call('drain')
        previous = (self.directory / 'snapshot.json').read_bytes()
        changed = original(2, source=export_fixtures.SOURCE + '-next')
        bridge.offer(changed)
        self.assertIsNone(owner.call('drain'))
        self.assertIn('SOURCE_CHANGED_USE_NEW_DIRECTORY', bridge.snapshot()['failure']['detail'])
        self.assertEqual((self.directory / 'snapshot.json').read_bytes(), previous)
        replacement = SimObservationBridge(self.directory.parent / 'new-source')
        next_owner = self.owner(replacement)
        self.assertTrue(next_owner.call('start'))
        self.assertTrue(replacement.offer(changed))
        self.assertEqual(next_owner.call('drain').status, 'WRITTEN')

    def test_start_failure_and_close_before_start_drop_only_diagnostic_references(self):
        self.directory.mkdir()
        foreign = self.directory / 'user-data'
        foreign.write_bytes(b'keep')
        bridge = SimObservationBridge(self.directory)
        bridge.offer(original())
        owner = self.owner(bridge)
        self.assertFalse(owner.call('start'))
        self.assertEqual(bridge.snapshot()['failure']['stage'], 'START')
        self.assertFalse(bridge.snapshot()['pending'])
        self.assertFalse(bridge.offer(original(2)))
        self.assertEqual(foreign.read_bytes(), b'keep')
        unopened = SimObservationBridge(self.directory.parent / 'not-opened')
        unopened.offer(original())
        other = self.owner(unopened)
        other.call('close')
        self.assertEqual(unopened.snapshot()['state'], 'CLOSED')
        self.assertFalse(unopened.snapshot()['pending'])
        self.assertFalse(unopened.offer(original(2)))
        self.assertFalse((self.directory.parent / 'not-opened').exists())
        with self.assertRaises(RuntimeError):
            other.call('start')


if __name__ == '__main__':
    unittest.main()
