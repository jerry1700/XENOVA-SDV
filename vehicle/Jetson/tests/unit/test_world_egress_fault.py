"""World 장애 필터의 순수 시간/원 객체 수명 회귀. 실제 JIO/발행/모터 수락 시험은 아니다."""

from dataclasses import dataclass
import unittest

from xenova_vehicle.transport.world_egress_fault import WorldEgressFault


@dataclass(frozen=True)
class OriginalWorld:
    # 실제 _World와 같은 raw/E 인터페이스. 나머지 필드도 같은 원 객체로 반환되는지 검사한다.
    raw: bytes
    expires_us: int
    capture_us: int
    sequence: int
    publication: object
    token: object


def world(sequence=1, *, expires=1000):
    return OriginalWorld(f'original-world-{sequence}'.encode('ascii'), expires,
        50 + sequence, sequence, object(), object())


class WorldEgressFaultTests(unittest.TestCase):
    def setUp(self):
        self.fault = WorldEgressFault()

    def empty(self):
        view = self.fault.snapshot()
        self.assertFalse(view['held'])
        self.assertFalse(view['pending'])

    def test_pass_returns_same_original_for_owner_revalidation_without_copy_or_new_E(self):
        original = world()
        self.assertTrue(self.fault.offer(original, now_us=100))
        self.assertIs(self.fault.take_due(now_us=100), original)
        self.assertEqual((original.capture_us, original.sequence, original.expires_us), (51, 1, 1000))
        self.empty()
        self.assertIsNone(self.fault.take_due(now_us=100))

    def test_delay_never_restarts_due_for_same_held_or_pending(self):
        self.fault.configure('DELAY', delay_us=40, now_us=100)
        first, second = world(), world(2)
        self.fault.offer(first, now_us=100)
        self.fault.offer(second, now_us=110)
        for stamp in (120, 130, 139):
            self.fault.offer(first, now_us=stamp)
            self.fault.offer(second, now_us=stamp)
            self.assertIsNone(self.fault.take_due(now_us=stamp))
        self.assertIs(self.fault.take_due(now_us=140), first)
        self.assertIsNone(self.fault.take_due(now_us=149))
        self.assertIs(self.fault.take_due(now_us=150), second)
        self.empty()

    def test_latest_pending_replace_preserves_first_due_and_own_due_after_promotion(self):
        self.fault.configure('DELAY', delay_us=50, now_us=100)
        first, replaced, latest = world(), world(2), world(3)
        self.fault.offer(first, now_us=100)
        self.fault.offer(replaced, now_us=110)
        self.fault.offer(latest, now_us=130)
        self.assertIsNone(self.fault.take_due(now_us=149))
        self.assertIs(self.fault.take_due(now_us=150), first)
        self.assertIsNone(self.fault.take_due(now_us=179))
        self.assertIs(self.fault.take_due(now_us=180), latest)
        self.assertEqual(self.fault.snapshot()['counts']['replaced'], 1)
        self.empty()

    def test_continuous_new_frames_faster_than_delay_do_not_starve_first_held(self):
        self.fault.configure('DELAY', delay_us=30, now_us=0)
        originals, offered, released = {}, {}, []
        for now in range(0, 201, 10):
            original = world(now + 1)
            originals[original.sequence] = original
            offered[original.sequence] = now
            self.fault.offer(original, now_us=now)
            item = self.fault.take_due(now_us=now)
            if item is not None:
                self.assertIs(item, originals[item.sequence])
                self.assertGreaterEqual(now - offered[item.sequence], 30)
                self.assertLess(now, item.expires_us)
                released.append((now, item.sequence))
            view = self.fault.snapshot()
            self.assertLessEqual(int(view['held']) + int(view['pending']), 2)
        self.assertEqual(released[0], (30, 1))
        self.assertGreaterEqual(len(released), 5)
        self.assertGreater(self.fault.snapshot()['counts']['replaced'], 0)

    def test_overdue_pending_does_not_receive_new_delay_when_promoted(self):
        self.fault.configure('DELAY', delay_us=10, now_us=100)
        first, second = world(), world(2)
        self.fault.offer(first, now_us=100)
        self.fault.offer(second, now_us=101)
        self.assertIs(self.fault.take_due(now_us=150), first)
        self.assertIs(self.fault.take_due(now_us=150), second)

    def test_long_delay_discards_original_E_at_boundary_instead_of_refreshing(self):
        self.fault.configure('DELAY', delay_us=50, now_us=100)
        first, second = world(expires=120), world(2, expires=160)
        self.fault.offer(first, now_us=100)
        self.fault.offer(second, now_us=110)
        self.assertIsNone(self.fault.take_due(now_us=150))
        self.assertIsNone(self.fault.take_due(now_us=160))
        self.assertEqual((first.expires_us, second.expires_us), (120, 160))
        self.assertEqual(self.fault.snapshot()['counts']['expired'], 2)
        self.empty()

    def test_block_discards_both_slots_and_clear_never_flushes_old_results(self):
        self.fault.configure('DELAY', delay_us=50, now_us=100)
        self.fault.offer(world(), now_us=100)
        self.fault.offer(world(2), now_us=101)
        self.fault.configure('BLOCK', now_us=102)
        self.assertFalse(self.fault.offer(world(3), now_us=103))
        self.assertIsNone(self.fault.take_due(now_us=160))
        self.fault.clear(now_us=160)
        self.assertIsNone(self.fault.take_due(now_us=160))
        fresh = world(4)
        self.assertTrue(self.fault.offer(fresh, now_us=161))
        self.assertIs(self.fault.take_due(now_us=161), fresh)
        self.assertEqual(self.fault.snapshot()['counts']['discarded'], 2)
        self.assertEqual(self.fault.snapshot()['counts']['blocked'], 1)

    def test_explicit_configuration_update_even_same_mode_drops_both_slots(self):
        self.fault.configure('DELAY', delay_us=10, now_us=100)
        self.fault.offer(world(), now_us=100)
        self.fault.offer(world(2), now_us=101)
        self.fault.configure('DELAY', delay_us=10, now_us=102)
        self.assertIsNone(self.fault.take_due(now_us=200))
        self.assertEqual(self.fault.snapshot()['counts']['discarded'], 2)

    def test_clear_delay_to_pass_requires_a_new_offer_and_keeps_external_owner_separate(self):
        self.fault.configure('DELAY', delay_us=100, now_us=100)
        original = world()
        self.fault.offer(original, now_us=100)
        self.fault.clear(now_us=101)
        self.assertIsNone(self.fault.take_due(now_us=200))
        # 이 반환은 실제 source/clock/운영 gate 검산에 앞선 후보일 뿐이다.
        fresh = world(2)
        self.fault.offer(fresh, now_us=201)
        self.assertIs(self.fault.take_due(now_us=201), fresh)

    def test_invalid_settings_and_original_bounds_do_not_destroy_pending(self):
        original = world()
        self.fault.offer(original, now_us=100)
        for mode, delay in ((None, 0), ('delay', 1), ('DELAY', 0), ('DELAY', True),
                ('DELAY', -1), ('DELAY', 1 << 63), ('PASS', 1), ('BLOCK', 1)):
            with self.subTest(mode=mode, delay=delay), self.assertRaises(ValueError):
                self.fault.configure(mode, delay_us=delay, now_us=101)
        for raw, expiry in ((b'', 1000), (b'x' * 32769, 1000), (bytearray(b'x'), 1000),
                (b'x', 0), (b'x', True), (b'x', 1 << 63)):
            with self.subTest(size=len(raw), expiry=expiry), self.assertRaises(ValueError):
                self.fault.offer(OriginalWorld(raw, expiry, 1, 2, object(), object()), now_us=101)
        self.assertIs(self.fault.take_due(now_us=100), original)

    def test_same_clock_and_original_E_numeric_boundaries_are_exact(self):
        original = world(expires=100)
        self.assertFalse(self.fault.offer(original, now_us=100))
        for stamp in (99, -1, True, 100.0, 1 << 63):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.fault.take_due(now_us=stamp)
        self.empty()
        maximum = (1 << 63) - 1
        self.fault.configure('DELAY', delay_us=10, now_us=maximum - 20)
        with self.assertRaises(ValueError):
            self.fault.offer(world(expires=maximum), now_us=maximum - 5)
        self.empty()

    def test_mutating_internal_caller_cannot_replace_original_E_while_held(self):
        class Mutable:
            raw = b'original'
            expires_us = 1000
        original = Mutable()
        self.fault.configure('DELAY', delay_us=10, now_us=100)
        self.fault.offer(original, now_us=100)
        original.expires_us = 2000
        with self.assertRaises(ValueError):
            self.fault.take_due(now_us=110)
        self.fault.clear(now_us=110)
        self.empty()

    def test_snapshot_is_observation_copy_without_mutating_filter_state(self):
        self.fault.offer(world(), now_us=100)
        view = self.fault.snapshot()
        view['mode'] = 'BLOCK'
        view['counts']['released'] = 999
        self.assertEqual(self.fault.snapshot()['mode'], 'PASS')
        self.assertEqual(self.fault.snapshot()['counts']['released'], 0)


if __name__ == '__main__':
    unittest.main()
