"""PC 제어가 송신 ACK 없이 여러 실제 계산 사건을 소비하는 경계를 검증한다."""

from dataclasses import replace
from threading import Condition
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from xenova_vehicle.runtime.lifecycle_policy import AbsoluteDeadline
from xenova_vehicle.runtime.mapper_compute import MapperEvent
from xenova_vehicle.runtime.pc_compute import PcMapperCompute, PcParserCompletion
from xenova_vehicle.runtime.pc_mapper_control import PcMapperControl
from xenova_vehicle.runtime.pc_workers import PcWorkerActor


class PcParserReportingTests(unittest.TestCase):
    def test_multiple_completions_keep_latest_transport_candidate_without_ack(self):
        actor = object.__new__(PcWorkerActor)
        actor.boot = SimpleNamespace(peer=SimpleNamespace(role='M'))
        actor.binding = SimpleNamespace(sha256='source')
        actor.state = 'WARMUP'
        actor.progress = Mock()
        actor.await_sent = Mock(side_effect=AssertionError('progress must not gate compute'))
        compute = object.__new__(PcMapperCompute)
        compute._condition = Condition()
        compute._terminal_error = None
        events, reports = [], {}
        for sequence in range(1, 6):
            source = ('source', 'rgb', 'depth', sequence)
            key = (*source, sequence, sequence, 'manifest', 100 + sequence)
            event = MapperEvent('PARSER_STARTED', str(sequence), 'source', 1, source, sequence,
                100 + sequence, 100 + sequence, 1000, True, nn_source_key=key)
            events.extend((event, replace(event, kind='NN_COMPLETED', completed_us=200 + sequence)))
            reports[event.job_id] = PcParserCompletion(event.job_id, 'source', 1, source, key,
                200 + sequence, 1000, b'{}')
        compute.poll = Mock(return_value=events)
        compute.take_result = Mock(return_value=None)
        compute.take_parser_completion = lambda event: reports.pop(event.job_id)
        control = PcMapperControl(actor, compute, AbsoluteDeadline(0, 1000), clock=lambda: 300,
            nn_stall_us=5000)
        control.tick()
        actor.await_sent.assert_not_called()
        self.assertTrue(control.ready)
        self.assertEqual(control.streak, 5)
        self.assertEqual(control.take_parser_completion().job_id, '5')
        self.assertIsNone(control.take_parser_completion())
        self.assertEqual([call.args[0] for call in actor.progress.call_args_list][-1], 'NN_READY')


if __name__ == '__main__':
    unittest.main()
