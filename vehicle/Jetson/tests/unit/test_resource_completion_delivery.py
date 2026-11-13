"""원 자원 작업 완료와 supervisor의 수신 지연을 구분한다."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xenova_vehicle.runtime.async_resource_log import ResourceLogCompletion
from xenova_vehicle.runtime.resident_runtime import _Runtime
from test_async_resource_log import FakeStore
from xenova_vehicle.runtime.resource_manifest import ResourceManifest
from resource_guard_fixtures import encode


REQUEST = '00000000-0000-0000-0000-000000000001'


class CompletionDeliveryTests(unittest.TestCase):
    def runtime(self, completion):
        runtime = object.__new__(_Runtime)
        runtime.log = SimpleNamespace(poll=lambda: completion)
        runtime.log_pending = (REQUEST, 'PREPARE', 'PREPARE', 200, 100)
        runtime.stopping = False
        runtime._request = Mock()
        runtime.bootstrap_request = REQUEST
        return runtime

    def test_completed_in_budget_can_be_received_after_deadline(self):
        completion = ResourceLogCompletion(REQUEST, 'PREPARE', 100, 200, 190, value={'model': 'pinned'})
        self.assertFalse(completion.timely(210))
        self.assertTrue(completion.completed_in_time(210))
        runtime = self.runtime(completion)
        with patch('xenova_vehicle.runtime.resident_runtime.j_us', return_value=210):
            runtime._complete_log()
        self.assertEqual(runtime.artifacts, {'model': 'pinned'})
        runtime._request.assert_called_once_with('BOOTSTRAP', 'BOOTSTRAP', request_id=REQUEST)

    def test_actual_late_failed_or_future_completion_cannot_advance(self):
        for completed, error, now in ((200, None, 210), (190, 'failed', 210), (190, None, 180)):
            with self.subTest(completed=completed, error=error, now=now):
                runtime = self.runtime(ResourceLogCompletion(REQUEST, 'PREPARE', 100, 200, completed, error=error))
                with patch('xenova_vehicle.runtime.resident_runtime.j_us', return_value=now):
                    with self.assertRaises(RuntimeError): runtime._complete_log()
                runtime._request.assert_not_called()

    def test_completion_cannot_change_original_request_budget(self):
        for issued, expires in ((101, 200), (100, 201)):
            runtime = self.runtime(ResourceLogCompletion(REQUEST, 'PREPARE', issued, expires, 190))
            with self.assertRaisesRegex(RuntimeError, 'original outstanding request'):
                runtime._complete_log()
            self.assertIsNotNone(runtime.log_pending)
            runtime._request.assert_not_called()

    def test_stopping_records_completion_without_starting_next_stage(self):
        runtime = self.runtime(ResourceLogCompletion(REQUEST, 'PREPARE', 100, 200, 190))
        runtime.stopping = True
        with patch('xenova_vehicle.runtime.resident_runtime.j_us', return_value=210):
            runtime._complete_log()
        runtime._request.assert_not_called()

    def test_late_delivery_reconciles_original_durable_write_and_receipt(self):
        store = FakeStore('/unused')
        old = store.current
        fields = old.fields
        fields['revision'] = str(old.revision + 1)
        target = ResourceManifest(encode(fields))
        proposal = store.propose(store.load(), target, REQUEST)
        result = store.publish(proposal)
        completion = ResourceLogCompletion(REQUEST, 'WRITE', 100, 200, 190,
            ack=result.ack, receipt=store.load())
        runtime = self.runtime(completion)
        runtime.manifest, runtime.stopping = old, True
        runtime.log_pending = (REQUEST, 'WRITE', 'CANDIDATE', 200, 100)
        with patch('xenova_vehicle.runtime.resident_runtime.j_us', return_value=210):
            runtime._complete_log()
        self.assertEqual(runtime.manifest.ref, target.ref)
        self.assertIs(runtime.latest_ack, result.ack)
        self.assertTrue(runtime.candidate_written)
        runtime._request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
