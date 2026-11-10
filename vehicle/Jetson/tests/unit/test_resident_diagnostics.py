"""선택 진단의 파일·직렬화 실패가 런타임 실패가 되지 않는 경계를 검증한다."""

from collections import deque
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from threading import Event
from unittest.mock import patch

from xenova_vehicle.runtime.resident_diagnostics import DiagnosticWriter
from xenova_vehicle.runtime.resident_runtime import _Runtime
from xenova_vehicle.runtime.resident_workers import _Actor
from tools.hil_observation.resident import _save_final_failure


class DiagnosticIsolationTests(unittest.TestCase):
    def test_cli_failure_is_bounded_private_and_never_overwrites(self):
        with TemporaryDirectory() as directory:
            result = dict(reason='pc_worker_failed', error='original failure', error_trace='x' * 10000)
            _save_final_failure(directory, result)
            path = Path(directory) / 'cli-failure.json'
            original = path.read_bytes()
            self.assertEqual(json.loads(original)['error'], 'original failure')
            self.assertEqual(len(json.loads(original)['error_trace']), 8192)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            _save_final_failure(directory, dict(result, error='later failure'))
            self.assertEqual(path.read_bytes(), original)

    def test_cli_optional_file_failure_does_not_change_result(self):
        result = dict(reason='pc_worker_failed', error='original failure')
        with patch('os.open', side_effect=PermissionError()):
            _save_final_failure('/unavailable', result)
        self.assertEqual(result, dict(reason='pc_worker_failed', error='original failure'))

    def test_worker_disk_full_does_not_fault_or_change_state(self):
        actor = object.__new__(_Actor)
        actor.boot = SimpleNamespace(diagnostic_directory='/unavailable')
        actor.state, actor.failure = 'RUNNING', None
        with patch.object(Path, 'mkdir', side_effect=OSError(28, 'No space left')):
            self.assertTrue(actor.save('setup.json', {'ready': True}))
            actor._diagnostics._queue.join()
        self.assertEqual(actor.state, 'RUNNING')
        self.assertIsNone(actor.failure)
        self.assertEqual(actor._diagnostics.last_error, 'OSError')

    def test_disabled_and_invalid_diagnostics_do_not_raise(self):
        writer = DiagnosticWriter()
        with patch.object(Path, 'mkdir') as mkdir:
            self.assertTrue(writer.save(None, 'ignored.json', object()))
            mkdir.assert_not_called()
        self.assertFalse(writer.save('/unused', 'bad.json', object()))
        self.assertEqual(writer.last_error, 'TypeError')
        self.assertFalse(writer.save('/unused', 'large.json', 'x' * (2 * 1024 * 1024)))
        self.assertEqual(writer.last_error, 'ValueError')

    def test_existing_file_is_preserved(self):
        with TemporaryDirectory() as directory:
            writer = DiagnosticWriter()
            self.assertTrue(writer.save(directory, 'sample.json', {'value': 1}))
            writer._queue.join()
            original = (Path(directory) / 'sample.json').read_bytes()
            self.assertTrue(writer.save(directory, 'sample.json', {'value': 2}))
            writer._queue.join()
            self.assertEqual(writer.last_error, 'FileExistsError')
            self.assertEqual((Path(directory) / 'sample.json').read_bytes(), original)

    def test_output_permission_failure_preserves_in_memory_artifacts(self):
        runtime = object.__new__(_Runtime)
        runtime.output = Path('/unavailable')
        runtime.output_owned = False
        runtime.events = deque(maxlen=64)
        artifacts = {'manifests': {'profile.json': b'{}'}}
        with patch.object(Path, 'mkdir', side_effect=PermissionError()):
            runtime._publish_diagnostics(artifacts)
            runtime._diagnostics._queue.join()
            runtime.save_result({'complete': True})
        self.assertEqual(artifacts['manifests'], {'profile.json': b'{}'})
        self.assertFalse(runtime.output_owned)
        self.assertIsNone(runtime._diagnostic_directory('A', 'worker'))
        self.assertEqual(runtime._diagnostics.last_error, 'PermissionError')

    def test_slow_disk_and_full_diagnostic_queue_do_not_wait_for_io(self):
        entered, release = Event(), Event()
        writer = DiagnosticWriter()
        def blocked():
            entered.set()
            release.wait(2)
        try:
            self.assertTrue(writer.submit(blocked))
            self.assertTrue(entered.wait(2))
            for _ in range(8): self.assertTrue(writer.submit(lambda: None))
            self.assertFalse(writer.submit(lambda: None))
            self.assertEqual(writer.dropped, 1)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            writer._queue.join()


if __name__ == '__main__':
    unittest.main()
