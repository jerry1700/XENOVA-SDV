"""PC RAW 취득 전 취소·재전달이 다음 프레임과 token 수명을 보존하는지 검사한다."""

from hashlib import sha256
import unittest

import test_pc_binding_messages as bindings
from test_resident_broker import Link, Token
from xenova_vehicle.runtime.pc_raw_broker import PcRawBroker
from xenova_vehicle.runtime.pc_slot_messages import PcSlotMessage, pc_write_request_payload
from xenova_vehicle.runtime.raw_leases import Endpoint
from xenova_vehicle.runtime.resident_broker import BrokerError


class PcRawCancellationTests(unittest.TestCase):
    def setUp(self):
        self.context = bindings.context()
        self.bind = bindings.bind(self.context)
        self.tokens = tuple((Token(1), Token(0)) for _ in range(2))
        reader = bindings.context('M').peer
        self.broker = PcRawBroker(self.bind, self.context.scope,
            Endpoint(reader.channel_epoch, reader.worker_instance), self.tokens)
        self.link = Link(self.broker.writer)

    def request(self, number, now):
        binding = self.context.expected_binding
        value = bindings.bind_fields(self.context)
        value.update(kind='SLOT', sender_instance=self.context.peer.worker_instance,
            request_id=bindings.uid(number), issued_j_us=str(now), expires_j_us=str(now + 1000),
            payload=pc_write_request_payload(binding, self.context.scope, job_id=bindings.uid(number + 100),
                phase='EARLY', expires_j_us=now + 400000,
                full_key=(binding.source_id, binding.fields['stream_instances']['rgb_video'], binding.clock_id, number),
                pc_frame_proof_sha256='a' * 64, purpose='PARSER_WARMUP'))
        packet = bindings.packet(value)
        self.broker.receive('A', packet, now_us=now)
        return PcSlotMessage(packet, self.broker.writer, limits=self.broker.limits)

    def cancel(self, original, now, digest=None):
        value = original.fields
        value.update(kind='RAW_CONTROL', issued_j_us=str(now), expires_j_us=str(now + 50),
            payload=dict(op='CANCEL_REQUEST', request_id=original.request_id,
                request_sha256=sha256(original.raw).hexdigest() if digest is None else digest,
                raw_binding_sha256=self.context.scope.raw_binding_sha256))
        return bindings.packet(value)

    def test_expired_grant_cancel_keeps_tokens_and_accepts_next_frame(self):
        original = self.request(501, 1000000)
        self.broker.pump(self.link, now_us=1000001)
        self.assertEqual(self.broker.snapshot()['slots'][0]['state'], 'WRITING')
        cancellation = self.cancel(original, 1001001)
        self.broker.cancel_request('A', cancellation, now_us=1001001)
        self.broker.cancel_request('A', cancellation, now_us=1001002)
        self.assertEqual(self.broker.snapshot()['slots'][0]['state'], 'FREE')
        self.assertEqual(self.broker.snapshot()['counts']['cancelled_requests'], 1)
        self.request(502, 1001003)
        self.broker.pump(self.link, now_us=1001004)
        self.assertEqual(self.link.sent[-1].payload['generation'], '2')
        for writable, ready in self.tokens:
            self.assertEqual((writable.value, ready.value, writable.posts, ready.posts), (1, 0, 0, 0))

    def test_duplicate_cancel_cannot_change_original_request_hash(self):
        original = self.request(501, 1000000)
        self.broker.cancel_request('A', self.cancel(original, 1000001), now_us=1000001)
        with self.assertRaises(BrokerError):
            self.broker.cancel_request('A', self.cancel(original, 1000002, digest='f' * 64), now_us=1000002)
        self.assertEqual(self.broker.snapshot()['counts']['cancelled_requests'], 1)


if __name__ == '__main__':
    unittest.main()
