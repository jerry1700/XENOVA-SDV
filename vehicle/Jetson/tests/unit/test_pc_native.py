"""fresh Linux SHM/실제 spawn Semaphore의 PC copy 경계다. 실제 S 등록/ACK는 아니다.

원 clock/NN/capture는 codec 시험의 명시 software fixture다. 실제 publish/copy/token
반환만 실행하며 EARLY snapshot ACK, NN enqueue, actual actor/Device/READY를 주장하지 않는다.
"""

from dataclasses import replace
import multiprocessing
from multiprocessing import shared_memory
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_pc_slot as slot_fixtures
from xenova_vehicle.runtime.pc_native import PcArenaWriter, PcArenaReader
from xenova_vehicle.runtime.pc_slot import ExpectedPcSlot, PcSlotError, METADATA_BYTES
from xenova_vehicle.runtime.native_resources import NativeResourceError
from xenova_vehicle.runtime.raw_leases import (ARENA_BYTES, Endpoint, LeaseRequest, WriteGrant,
    ReadKey, ReadRequest, ReadLease, Offer)


def now_us(): return time.monotonic_ns() // 1000


@unittest.skipUnless(sys.platform == 'linux', 'Linux shared-memory/token test')
class PcNativeTests(unittest.TestCase):
    def setUp(self):
        arena = str(uuid4())
        self.frame, self.encoding, _, _ = slot_fixtures.bundle(now=now_us(), arena_id=arena)
        self.scope = self.encoding.context.write_key.scope
        self.shm = shared_memory.SharedMemory(name=self.scope.arena_name[1:], create=True, size=ARENA_BYTES)
        self.addCleanup(self.shm.unlink); self.addCleanup(self.shm.close)
        ctx = multiprocessing.get_context('spawn')
        self.tokens = tuple((ctx.Semaphore(1), ctx.Semaphore(0)) for _ in range(2))
        self.writer, self.reader = PcArenaWriter(self.scope, self.tokens), PcArenaReader(self.scope, self.tokens)
        self.addCleanup(self.writer.close); self.addCleanup(self.reader.close)

    def publish(self, encoding=None):
        encoding = self.encoding if encoding is None else encoding
        now = now_us()
        request = LeaseRequest(str(uuid4()), now, encoding.expires_j_us)
        grant = WriteGrant(encoding.context.write_key, request, now)
        return grant, self.writer.publish(grant, encoding)

    def lease(self, info, encoding=None):
        encoding = self.encoding if encoding is None else encoding
        now = now_us(); reader = Endpoint(str(uuid4()), str(uuid4()))
        request = ReadRequest(Offer(info), reader, LeaseRequest(str(uuid4()), now, encoding.expires_j_us))
        lease = ReadLease(ReadKey(info.key, reader, str(uuid4())), request, now)
        wanted = ExpectedPcSlot(encoding.context, info, encoding.job_id, encoding.phase, encoding.expires_j_us,
            encoding.full_key, slot_fixtures.sha256(encoding.pc_frame_proof).hexdigest())
        return lease, wanted

    def values(self): return tuple((w.get_value(), r.get_value()) for w, r in self.tokens)

    def test_copy_finishes_after_frame_expiry_and_still_returns_actual_tokens(self):
        _, info = self.publish()
        lease, wanted = self.lease(info)
        expiry = self.encoding.expires_j_us
        with patch('xenova_vehicle.runtime.pc_native._now', side_effect=[now_us(),expiry+1,expiry+2]):
            owned, returned = self.reader.copy_owned(lease,wanted)
        self.assertEqual(owned.expires_j_us,expiry)
        self.assertEqual(returned.writable_posted_us,expiry+2)
        self.assertEqual(self.values(),((1,0),(1,0)))

    def test_native_write_ownership_budget_can_outlive_frame_without_retiming_payload(self):
        now = now_us(); expiry = self.encoding.expires_j_us
        request = LeaseRequest(str(uuid4()),now,expiry+1_000_000)
        grant = WriteGrant(self.encoding.context.write_key,request,now)
        with patch('xenova_vehicle.runtime.pc_native._now',side_effect=[now,expiry+1,expiry+2]):
            info = self.writer.publish(grant,self.encoding)
        self.assertEqual(info.completed_us,expiry+2)
        self.assertEqual(self.encoding.expires_j_us,expiry)
        self.assertEqual(self.values(),((0,1),(1,0)))

    def test_actual_tokens_copy_then_next_generation_nn_preserve_original_job_and_e(self):
        grant, info = self.publish()
        self.assertEqual(self.values(), ((0, 1), (1, 0)))
        self.assertIs(self.writer.publish(grant, self.encoding), info)
        self.assertEqual(self.values(), ((0, 1), (1, 0)))
        lease, wanted = self.lease(info); owned, returned = self.reader.copy_owned(lease, wanted)
        self.assertIs(returned.lease, lease)
        self.assertLessEqual(lease.reserved_us, returned.views_ended_us)
        self.assertLessEqual(returned.views_ended_us, returned.writable_posted_us)
        self.assertEqual(owned.raw, self.encoding.raw)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))
        new_key = replace(self.encoding.context.write_key, generation=2, write_token=str(uuid4()))
        new_context = replace(self.encoding.context, write_key=new_key)
        _, second, _, _ = slot_fixtures.bundle(phase='NN', frame=self.frame, context=new_context, nn_received=now_us())
        _, second_info = self.publish(second); second_lease, second_wanted = self.lease(second_info, second)
        nn_owned, _ = self.reader.copy_owned(second_lease, second_wanted)
        self.assertEqual((nn_owned.job_id, nn_owned.full_key, nn_owned.expires_j_us, nn_owned.pc_frame_proof),
            (owned.job_id, owned.full_key, owned.expires_j_us, owned.pc_frame_proof))
        self.assertEqual(nn_owned.phase, 'NN'); self.assertIsNotNone(nn_owned.nn_output)
        self.assertEqual(owned.phase, 'EARLY'); self.assertEqual(owned.raw, self.encoding.raw)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))
        with self.assertRaises(NativeResourceError): self.reader.copy_owned(second_lease, second_wanted)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))

    def test_decode_failure_posts_actual_writable_once_after_real_ready_acquire(self):
        _, info = self.publish(); lease, wanted = self.lease(info)
        self.shm.buf[METADATA_BYTES] ^= 1
        with self.assertRaises(PcSlotError): self.reader.copy_owned(lease, wanted)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))
        with self.assertRaises(NativeResourceError): self.reader.copy_owned(lease, wanted)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))

    def test_expired_or_foreign_grant_does_not_take_or_manufacture_tokens(self):
        now = now_us(); request = LeaseRequest(str(uuid4()), now - 2, now - 1)
        expired = WriteGrant(self.encoding.context.write_key, request, now - 2)
        with self.assertRaises(ValueError): self.writer.publish(expired, self.encoding)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))
        key = replace(self.encoding.context.write_key, write_token=str(uuid4()))
        grant = WriteGrant(key, LeaseRequest(str(uuid4()), now, self.encoding.expires_j_us), now)
        with self.assertRaises(NativeResourceError): self.writer.publish(grant, self.encoding)
        self.assertEqual(self.values(), ((1, 0), (1, 0)))
