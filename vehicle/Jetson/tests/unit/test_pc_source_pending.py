"""S NN pending의 유한 만료 정책만 검사하는 software leaf다.

actual asset/SDK/BIND/Source 승인 fixture가 아니다. 독립된 private prune에 원 E를
가진 작은 사실을 주며, initial5/최근5s/15s 판정이나 READY 성공을 만들지 않는다.
"""

from collections import OrderedDict
from threading import get_ident
from types import SimpleNamespace
import unittest

from xenova_vehicle.runtime.pc_source_identity import MAX_PENDING_NN, PcSourceIdentityOwner


class PcSourcePendingTests(unittest.TestCase):
    def owner_policy(self):
        owner = object.__new__(PcSourceIdentityOwner)
        owner._thread = get_ident()
        owner._pending = OrderedDict()
        owner._expired_pending_nn = 0
        owner._completed = OrderedDict((('successful-original-job', 'a'*64),))
        owner._warmup = [object()]
        owner._warmup_originals = (object(),)*5
        owner._last_key = ('original-source', 'clock', 'stream', 9, 100, 101, 'd'*64, 'e'*64)
        owner._last_completed = 1000
        owner._parser_deadline = object()
        owner._admission = object()
        return owner

    def test_policy_prunes_only_original_expired_pending_and_releases_bounded_capacity(self):
        owner = self.owner_policy()
        for index in range(MAX_PENDING_NN):
            owner._pending[str(index)] = SimpleNamespace(filled=SimpleNamespace(expires_j_us=100+index))
        originals = tuple(owner._pending.items())
        self.assertEqual(owner._prune_pending(99), 0)
        self.assertEqual(tuple(owner._pending.items()), originals)
        self.assertEqual(owner._prune_pending(100), 1)
        self.assertEqual(tuple(owner._pending.items()), originals[1:])
        self.assertEqual(owner.expired_pending_nn, 1)
        self.assertEqual(owner._prune_pending(1000), MAX_PENDING_NN-1)
        self.assertEqual(len(owner._pending), 0)
        self.assertEqual(owner.expired_pending_nn, MAX_PENDING_NN)
        self.assertEqual(owner._prune_pending(1001), 0)
        self.assertEqual(owner.expired_pending_nn, MAX_PENDING_NN)

    def test_policy_keeps_success_history_warmup_originals_and_progress_without_retiming(self):
        owner = self.owner_policy()
        kept = SimpleNamespace(filled=SimpleNamespace(expires_j_us=200))
        expired = SimpleNamespace(filled=SimpleNamespace(expires_j_us=100))
        owner._pending.update(expired=expired, kept=kept)
        names = ('_completed', '_warmup', '_warmup_originals', '_last_key', '_last_completed',
            '_parser_deadline', '_admission')
        originals = {name: getattr(owner, name) for name in names}
        owner._expired_pending_nn = 2**64-1
        self.assertEqual(owner._prune_pending(100), 1)
        self.assertEqual(owner._pending, OrderedDict((('kept', kept),)))
        self.assertEqual(owner.expired_pending_nn, 2**64-1)
        self.assertEqual((expired.filled.expires_j_us, kept.filled.expires_j_us), (100, 200))
        for name, original in originals.items():
            self.assertIs(getattr(owner, name), original)
