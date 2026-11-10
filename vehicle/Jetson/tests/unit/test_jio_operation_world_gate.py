"""S/JIO의 명시 World-only gate와 실제 final claim 경계를 검사한다.

World·Vehicle·returned Probe13·Scene은 명시 software codec fixture다. 일부 시험은
실제 non-daemon JIO thread와 FakeClient IO leaf까지 사용하며 broker/Pi 수락이나
이미 송신한 World 회수, 주행 금지·물리 clock·원 E의 연장을 주장하지 않는다.
"""

from hashlib import sha256
import threading
import time
import unittest
from unittest.mock import patch

import test_jetson_jio as fixtures
import test_runtime_status as status_fixtures
import test_scene_source_ports as scene_fixtures
from xenova_drive_world import decode, encode, schema
from xenova_vehicle.runtime.runtime_status import RuntimeStatus
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.jetson_jio import JetsonJio, PREFIX, VEHICLE_TOPIC, WORLD_TOPIC


class JioOperationWorldGateTests(unittest.TestCase):
    def owner(self, *, client=None, owner_type=JetsonJio):
        clock = fixtures.Clock(1_300_200)
        owner = owner_type(fixtures.profile(), clock=clock,
            client_factory=None if client is None else lambda *_: client)
        fixtures.bind(owner)
        fixtures.qualify_clock(owner)
        return clock, owner

    def offer(self, owner, sequence=1, *, expires=1_400_000):
        return owner.offer_world(fixtures.world(sequence), expires_j_us=expires,
            publication_sha256=sha256(canonical(fixtures.publication())).hexdigest())

    def selected(self, owner):
        # 실제 _run의 선택 단계처럼 lock 안에서 원 pending을 떼고 같은 원객체를 반환한다.
        with owner._lock:
            original, owner._world = owner._world, None
        self.assertIsNotNone(original)
        return original

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertTrue(predicate(), 'bounded original dispatch event did not occur')

    def cleanup(self, owner, release):
        release.set()
        owner.stop()
        owner.join(2)
        self.assertFalse(owner.snapshot()['alive'])
        self.assertIsNone(owner.snapshot()['failure'])

    def test_default_true_exact_bool_and_idempotence_withdraw_only_pending_world(self):
        _, owner = self.owner()
        self.assertTrue(self.offer(owner))
        original = owner._world
        head = owner._world_head
        self.assertFalse(owner.set_world_dispatch_enabled(True))
        self.assertIs(owner._world, original)
        generation = owner._world_dispatch_generation
        for bad in (0, 1, None, 'False', object()):
            with self.subTest(bad_type=type(bad).__name__), self.assertRaises(ValueError):
                owner.set_world_dispatch_enabled(bad)
        self.assertIs(owner._world, original)
        self.assertEqual(owner._world_dispatch_generation, generation)
        self.assertTrue(owner.set_world_dispatch_enabled(False))
        self.assertIsNone(owner._world)
        self.assertIs(owner._world_head, head)
        self.assertFalse(owner.set_world_dispatch_enabled(False))
        self.assertFalse(self.offer(owner, 2))
        self.assertIs(owner._world_head, head)
        self.assertFalse(owner._claim_world(original))
        self.assertEqual(original.expires_us, 1_400_000)
        self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 0)

    def test_selected_disabled_enabled_aba_rejects_old_and_requires_new_original_world(self):
        clock, owner = self.owner()
        self.assertTrue(self.offer(owner))
        original = self.selected(owner)
        head = owner._world_head
        self.assertTrue(owner.set_world_dispatch_enabled(False))
        self.assertTrue(owner.set_world_dispatch_enabled(True))
        self.assertFalse(owner._claim_world(original))
        self.assertIs(owner._world_head, head)
        self.assertFalse(self.offer(owner))  # 이전 원 seq/E를 새 RUN 결과로 부활시키지 않는다.
        self.assertTrue(self.offer(owner, 2))
        fresh = owner._world
        self.assertEqual(fresh.expires_us, original.expires_us)
        self.assertTrue(owner._claim_world(fresh))
        clock.now = fresh.expires_us
        self.assertFalse(owner._claim_world(fresh))
        self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)

    def test_offer_decode_race_checks_generation_again_before_accepting_or_advancing_hwm(self):
        _, owner = self.owner()
        entered = threading.Event()
        release = threading.Event()
        results, errors = [], []
        original_decode = decode

        def paused(raw, message_type=schema.WorldSnapshot):
            value = original_decode(raw, message_type)
            if message_type is schema.WorldSnapshot:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('bounded decode observation gate expired')
            return value

        def offer():
            try:
                results.append(self.offer(owner))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=offer, name='test-world-offer', daemon=False)
        try:
            # 원 codec를 그대로 호출한 뒤 멈추는 scheduling leaf이며 반환 기하는 수정하지 않는다.
            with patch('xenova_drive_world.decode', side_effect=paused):
                thread.start()
                self.assertTrue(entered.wait(2))
                owner.set_world_dispatch_enabled(False)
                owner.set_world_dispatch_enabled(True)
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [False])
            self.assertIsNone(owner._world)
            self.assertIsNone(owner._world_head)
            self.assertTrue(self.offer(owner))  # 거절된 offer는 원 HWM을 소비하지 않았다.
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_world_gate_preserves_original_clock_source_sensor_scene_runtime_and_sequence_heads(self):
        clock, owner, grant = scene_fixtures.JioSurveyPortTests(methodName='runTest').owner()
        fixtures.deliver(owner, VEHICLE_TOPIC, fixtures.vehicle())
        fixtures.deliver(owner, f'{PREFIX}/sensors/tof/0/observation.pb', fixtures.tof(0))
        summary_raw = encode(scene_fixtures.summary(grant, clock.now))
        self.assertTrue(owner.offer_survey_summary(summary_raw,
            grant_sha256=sha256(encode(grant)).hexdigest(), expires_j_us=clock.now + 1000))
        status = status_fixtures.status_fields()
        status.update(vehicle=owner.profile.source_policy.vehicle, device=owner.profile.device,
            snapshot_j_us=str(clock.now))
        self.assertTrue(owner.offer_runtime(RuntimeStatus(status)))
        self.assertTrue(self.offer(owner))
        original_refs = {name: getattr(owner, name) for name in (
            '_source', '_token', '_publication', '_lease', '_clock_window', '_probe', '_probe_input',
            '_vehicle', '_current_scene', '_scene_window', '_summary', '_runtime', '_runtime_head',
            '_runtime_identity', '_world_head')}
        heads = dict(owner._heads)
        tof = owner._tof[0]
        sequence = owner._runtime_sequence
        owner.set_world_dispatch_enabled(False)
        owner.set_world_dispatch_enabled(True)
        for name, original in original_refs.items():
            with self.subTest(name=name):
                self.assertIs(getattr(owner, name), original)
        self.assertEqual(owner._heads, heads)
        self.assertIs(owner._tof[0], tof)
        self.assertEqual(owner._runtime_sequence, sequence)
        self.assertIsNotNone(owner._vehicle)
        self.assertTrue(tof)
        self.assertEqual(encode(decode(owner._reply(owner._probe), schema.MapperClockReply).survey_summary), summary_raw)
        owner.set_world_dispatch_enabled(False)
        fixtures.deliver(owner, VEHICLE_TOPIC, fixtures.vehicle(2))
        latest = owner._vehicle
        self.assertEqual(decode(latest.raw, schema.VehicleSample).sequence, 2)
        fixtures.deliver(owner, VEHICLE_TOPIC, fixtures.vehicle())
        self.assertIs(owner._vehicle, latest)  # gate는 수신 HWM을 초기화하지 않는다.
        self.assertIs(owner.current_lease, original_refs['_lease'])
        self.assertIs(owner.current_scene, original_refs['_current_scene'])
        self.assertIn(latest, owner.take_inputs())

    def test_actual_jio_selected_before_aba_never_dispatches_old_but_fresh_run_can_publish(self):
        entered = threading.Event()
        release = threading.Event()

        class Paused(JetsonJio):
            def _claim_world(self, item):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('bounded selected World gate expired')
                return super()._claim_world(item)

        client = fixtures.FakeClient()
        _, owner = self.owner(client=client, owner_type=Paused)
        self.assertTrue(self.offer(owner))
        owner.start()
        try:
            self.assertTrue(entered.wait(2))
            owner.set_world_dispatch_enabled(False)
            owner.set_world_dispatch_enabled(True)
            release.set()
            self.wait(lambda: owner.snapshot()['counts']['world_fenced'] >= 1)
            self.assertEqual([row for row in client.published if row[0] == WORLD_TOPIC], [])
            self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 0)
            self.assertTrue(self.offer(owner, 2))
            self.wait(lambda: any(row[0] == WORLD_TOPIC for row in client.published))
            self.assertEqual([row[1] for row in client.published if row[0] == WORLD_TOPIC], [fixtures.world(2)])
            self.assertFalse(owner._thread.daemon)
        finally:
            self.cleanup(owner, release)

    def test_gate_after_claim_does_not_recall_io_and_does_not_hold_lock_across_publish(self):
        entered = threading.Event()
        release = threading.Event()

        class PausedClient(fixtures.FakeClient):
            def publish(self, topic, raw, **kwargs):
                if topic == WORLD_TOPIC:
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('bounded original IO leaf gate expired')
                return super().publish(topic, raw, **kwargs)

        client = PausedClient()
        _, owner = self.owner(client=client)
        raw = fixtures.world()
        self.assertTrue(self.offer(owner))
        owner.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)
            # 실제 publish leaf가 멈춰 있어도 S-main의 world-only gate는 즉시 교환된다.
            self.assertTrue(owner.set_world_dispatch_enabled(False))
            release.set()
            self.wait(lambda: any(row[0] == WORLD_TOPIC for row in client.published))
            self.assertEqual([row[1] for row in client.published if row[0] == WORLD_TOPIC], [raw])
            self.assertFalse(self.offer(owner, 2))
            self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)
        finally:
            self.cleanup(owner, release)


if __name__ == '__main__':
    unittest.main()
