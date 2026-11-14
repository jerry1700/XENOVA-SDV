"""World 장애를 실제 JIO 큐/claim/단일 owner와 연결하는 software 회귀다.

기존 shared owner의 returned Probe13과 typed World/Scene fixture를 사용한다. FakeClient는
네트워크 leaf만 대신하며 broker/Pi/SDK/모터 수락이나 이미 claim한 송신 회수를 증명하지 않는다.
"""

import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_jetson_jio as fixtures
import test_runtime_status as status_fixtures
import test_scene_source_ports as scene_fixtures
import test_world_publication_fence as fence_fixtures
from test_world_fault_control import load_common_mqtt_contract
from xenova_drive_world import decode, encode, schema
from xenova_vehicle.runtime.source_identity import scene_ref_fields
from xenova_vehicle.runtime.runtime_status import RuntimeStatus
from xenova_vehicle.transport.jetson_jio import JetsonJio, PREFIX, VEHICLE_TOPIC, WORLD_TOPIC


RUNTIME_TOPIC = f'{PREFIX}/mapper/oak-1/runtime'


class JioWorldEgressFaultTests(unittest.TestCase):
    def owner(self, *, client=None, owner_type=JetsonJio):
        clock = fixtures.Clock(1_300_200)
        owner = owner_type(fixtures.profile(), clock=clock,
            client_factory=None if client is None else lambda *_: client)
        fixtures.bind(owner)
        fixtures.qualify_clock(owner)
        return clock, owner

    def offer(self, owner, sequence=1, *, expires=1_400_000, raw=None):
        raw = fixtures.world(sequence) if raw is None else raw
        return owner.offer_world(raw, expires_j_us=expires,
            publication_sha256=owner.snapshot()['publication_sha256'])

    def take(self, owner):
        with owner._lock:
            return owner._take_world_locked()

    def status(self, owner, at):
        fields = status_fixtures.status_fields()
        fields.update(vehicle=owner.profile.source_policy.vehicle, device=owner.profile.device,
            mapper_boot=fixtures.source()['mapper_boot'],
            service_instance=fixtures.publication()['service_instance'], snapshot_j_us=str(at))
        return RuntimeStatus(fields)

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertTrue(predicate(), 'bounded original JIO event did not occur')

    def start(self, owner, *release_gates):
        def close():
            for gate in release_gates:
                gate.set()
            owner.stop()
            self.assertTrue(owner.join(2), 'released JIO owner did not terminate')
            view = owner.snapshot()
            self.assertFalse(view['alive'])
            self.assertIsNone(view['failure'])
        self.addCleanup(close)
        owner.start()

    def test_delay_returns_original_at_due_and_preserves_bytes_E_and_current_clock_refs(self):
        clock, owner = self.owner()
        refs = {name: getattr(owner, name) for name in
            ('_source', '_token', '_publication', '_lease', '_clock_window', '_probe', '_probe_input')}
        owner.set_world_egress_fault('DELAY', delay_us=40)
        raw = fixtures.world()
        expires = clock.now + 1000
        # 실제 core.offer를 그대로 호출하고 JIO가 넘긴 원 객체만 관측한다.
        with patch.object(owner._world_fault, 'offer', wraps=owner._world_fault.offer) as offered:
            self.assertTrue(self.offer(owner, raw=raw, expires=expires))
            original = offered.call_args.args[0]
        self.assertIs(original.raw, raw)
        original_fields = (original.expires_us, original.token, original.publication,
            original.scene_ref, original.grant_sha256, original.dispatch_generation)
        self.assertIsNone(owner._world)
        self.assertTrue(owner.snapshot()['world_pending'])
        generation = owner._world_dispatch_generation
        held = owner.snapshot()['world_fault']
        for mode, delay in (('delay', 1), ('DELAY', 0), ('DELAY', True), ('BLOCK', 1)):
            with self.subTest(mode=mode, delay=delay), self.assertRaises(ValueError):
                owner.set_world_egress_fault(mode, delay_us=delay)
        self.assertEqual(owner._world_dispatch_generation, generation)
        self.assertEqual(owner.snapshot()['world_fault'], held)
        clock.now += 39
        self.assertIsNone(self.take(owner))
        clock.now += 1
        selected = self.take(owner)
        self.assertIs(selected, original)
        self.assertIs(selected.raw, raw)
        self.assertEqual((selected.expires_us, selected.token, selected.publication,
            selected.scene_ref, selected.grant_sha256, selected.dispatch_generation), original_fields)
        for name, value in refs.items():
            self.assertIs(getattr(owner, name), value)
        self.assertTrue(owner._claim_world(selected))
        self.assertFalse(owner.snapshot()['world_pending'])

    def test_due_equal_original_E_is_dropped_and_final_claim_also_rejects_exact_E(self):
        for expiry_offset in (40, 41):
            with self.subTest(expiry_offset=expiry_offset):
                clock, owner = self.owner()
                began = clock.now
                owner.set_world_egress_fault('DELAY', delay_us=40)
                self.assertTrue(self.offer(owner, expires=began + expiry_offset))
                clock.now = began + 39
                self.assertIsNone(self.take(owner))
                clock.now = began + 40
                selected = self.take(owner)
                if expiry_offset == 40:
                    self.assertIsNone(selected)
                    self.assertEqual(owner.snapshot()['world_fault']['counts']['expired'], 1)
                else:
                    self.assertIsNotNone(selected)
                    original_E = selected.expires_us
                    clock.now = original_E
                    self.assertFalse(owner._claim_world(selected))
                    self.assertEqual(selected.expires_us, original_E)
                self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 0)
                self.assertFalse(owner.snapshot()['world_pending'])

    def test_delay_keeps_first_held_and_only_latest_pending_with_original_offer_due(self):
        clock, owner = self.owner()
        began = clock.now
        owner.set_world_egress_fault('DELAY', delay_us=40)
        originals = []
        with patch.object(owner._world_fault, 'offer', wraps=owner._world_fault.offer) as offered:
            for sequence, offset in enumerate((0, 10, 20, 30), 1):
                clock.now = began + offset
                self.assertTrue(self.offer(owner, sequence, expires=began + 1000))
                originals.append(offered.call_args.args[0])
                view = owner.snapshot()['world_fault']
                self.assertLessEqual(int(view['held']) + int(view['pending']), 2)
                self.assertIsNone(owner._world)
                self.assertEqual(view['held_due_us'], began + 40)
        view = owner.snapshot()['world_fault']
        self.assertTrue(view['held'] and view['pending'])
        self.assertEqual(view['pending_due_us'], began + 70)
        self.assertEqual(view['counts']['replaced'], 2)
        clock.now = began + 40
        self.assertIs(self.take(owner), originals[0])
        clock.now = began + 69
        self.assertIsNone(self.take(owner))
        clock.now = began + 70
        self.assertIs(self.take(owner), originals[-1])
        self.assertIsNone(self.take(owner))
        self.assertEqual([decode(item.raw, schema.WorldSnapshot).sequence
            for item in (originals[0], originals[-1])], [1, 4])
        self.assertTrue(all(item.expires_us == began + 1000 for item in originals))

    def test_block_consumes_valid_sequence_and_pass_never_flushes_or_reissues_it(self):
        clock, owner = self.owner()
        owner.set_world_egress_fault('BLOCK')
        self.assertTrue(self.offer(owner))  # 유효 seq의 수락/소비이며 보관·publish 승인이 아니다.
        head = owner._world_head
        self.assertEqual(head[0], 1)
        self.assertFalse(owner.snapshot()['world_pending'])
        self.assertEqual(owner.snapshot()['world_fault']['counts']['blocked'], 1)
        self.assertIsNone(self.take(owner))
        clock.now += 1
        owner.set_world_egress_fault('PASS')
        self.assertIs(owner._world_head, head)
        self.assertIsNone(self.take(owner))
        self.assertFalse(self.offer(owner, expires=1_450_000))
        self.assertIs(owner._world_head, head)
        self.assertTrue(self.offer(owner, 2))
        fresh = self.take(owner)
        self.assertEqual(decode(fresh.raw, schema.WorldSnapshot).sequence, 2)
        self.assertTrue(owner._claim_world(fresh))

    def test_selected_fault_configuration_and_clear_ABA_fence_original_without_resetting_hwm(self):
        clock, owner = self.owner()
        owner.set_world_egress_fault('DELAY', delay_us=1)
        self.assertTrue(self.offer(owner))
        clock.now += 1
        original = self.take(owner)
        self.assertIsNotNone(original)
        head = owner._world_head
        owner.set_world_egress_fault('BLOCK')
        owner.set_world_egress_fault('PASS')
        self.assertFalse(owner._claim_world(original))
        self.assertIs(owner._world_head, head)
        self.assertIsNone(self.take(owner))
        self.assertFalse(self.offer(owner))
        self.assertTrue(self.offer(owner, 2))
        self.assertTrue(owner._claim_world(self.take(owner)))
        self.assertEqual(original.expires_us, 1_400_000)

    def test_decode_race_rejects_preconfiguration_generation_before_hwm_acceptance(self):
        _, owner = self.owner()
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        original_decode = decode

        def paused(raw, message_type=schema.WorldSnapshot):
            value = original_decode(raw, message_type)
            if message_type is schema.WorldSnapshot:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('bounded decode scheduling gate expired')
            return value

        def offer():
            try:
                results.append(self.offer(owner))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=offer, name='test-world-fault-offer', daemon=False)
        try:
            with patch('xenova_drive_world.decode', side_effect=paused):
                thread.start()
                self.assertTrue(entered.wait(2))
                owner.set_world_egress_fault('BLOCK')
                owner.set_world_egress_fault('PASS')
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [False])
            self.assertIsNone(owner._world_head)
            self.assertFalse(owner.snapshot()['world_pending'])
            self.assertTrue(self.offer(owner))  # 최종 검산 전 거절된 offer는 HWM을 소비하지 않았다.
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_operation_OFF_AND_fault_clear_preserves_clock_input_runtime_and_independent_modes(self):
        clock, owner = self.owner()
        fixtures.deliver(owner, VEHICLE_TOPIC, fixtures.vehicle())
        fixtures.deliver(owner, f'{PREFIX}/sensors/tof/0/observation.pb', fixtures.tof(0))
        self.assertTrue(owner.offer_runtime(self.status(owner, clock.now)))
        refs = {name: getattr(owner, name) for name in ('_source', '_token', '_publication', '_lease',
            '_clock_window', '_probe', '_probe_input', '_vehicle', '_runtime', '_runtime_head')}
        tof = owner._tof[0]
        heads = dict(owner._heads)
        runtime_sequence = owner._runtime_sequence
        owner.set_world_egress_fault('DELAY', delay_us=20)
        self.assertTrue(self.offer(owner))
        self.assertTrue(owner.set_world_dispatch_enabled(False))
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'DELAY')
        self.assertFalse(owner.snapshot()['world_pending'])
        owner.set_world_egress_fault('PASS')
        self.assertFalse(owner._world_dispatch_enabled)
        self.assertFalse(self.offer(owner, 2))
        owner.set_world_egress_fault('BLOCK')
        self.assertTrue(owner.set_world_dispatch_enabled(True))
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'BLOCK')
        self.assertTrue(self.offer(owner, 2))
        self.assertIsNone(self.take(owner))
        for name, original in refs.items():
            self.assertIs(getattr(owner, name), original)
        self.assertIs(owner._tof[0], tof)
        self.assertEqual(owner._heads, heads)
        self.assertEqual(owner._runtime_sequence, runtime_sequence)
        fixtures.deliver(owner, VEHICLE_TOPIC, fixtures.vehicle(2))
        latest = owner._vehicle
        self.assertIn(latest, owner.take_inputs())
        self.assertEqual(decode(latest.raw, schema.VehicleSample).sequence, 2)
        self.assertIs(owner.current_lease, refs['_lease'])
        owner.set_world_egress_fault('PASS')
        self.assertFalse(self.offer(owner, 2))
        self.assertTrue(self.offer(owner, 3))

    def test_source_and_publication_changes_discard_both_buffers_while_delay_mode_persists(self):
        # 로컬 setter의 수명을 검사한다. 아직 결박하지 않은 MQTT controller의 source 정책은 별도다.
        for boundary in ('publication', 'source', 'fence'):
            with self.subTest(boundary=boundary):
                clock, owner = self.owner()
                owner.set_world_egress_fault('DELAY', delay_us=20)
                with patch.object(owner._world_fault, 'offer', wraps=owner._world_fault.offer) as offered:
                    self.assertTrue(self.offer(owner))
                    first = offered.call_args.args[0]
                    self.assertTrue(self.offer(owner, 2))
                self.assertTrue(owner.snapshot()['world_fault']['pending'])
                token, lease, head = owner._token, owner.current_lease, owner._world_head
                if boundary == 'publication':
                    self.assertTrue(owner.replace_publication(fixtures.publication(revision=2)))
                    self.assertIs(owner._token, token)
                    self.assertIs(owner.current_lease, lease)
                    self.assertIs(owner._world_head, head)
                elif boundary == 'source':
                    self.assertTrue(fixtures.bind(owner, dict(fixtures.source(), mapper_instance=fixtures.uid(90))))
                else:
                    owner.fence_source()
                view = owner.snapshot()['world_fault']
                self.assertEqual(view['mode'], 'DELAY')
                self.assertFalse(view['held'] or view['pending'])
                self.assertFalse(owner.snapshot()['world_pending'])
                clock.now += 20
                self.assertIsNone(self.take(owner))
                self.assertFalse(owner._claim_world(first))

    def test_actual_owner_delay_does_not_stall_probe_runtime_or_vehicle_input(self):
        # 실제 순수 codec만 격리한다. package 초기화/Paho 설치·socket은 검증 범위가 아니다.
        contract = load_common_mqtt_contract()
        codec_scope = patch.dict('sys.modules', contract.modules)
        codec_scope.start()
        self.addCleanup(codec_scope.stop)
        client = fixtures.FakeClient()
        clock, owner = self.owner(client=client)
        began = clock.now
        owner.set_world_egress_fault('DELAY', delay_us=100)
        raw = fixtures.world()
        self.assertTrue(self.offer(owner, raw=raw, expires=began + 1000))
        self.assertTrue(owner.offer_runtime(self.status(owner, clock.now)))
        client.incoming.append(SimpleNamespace(topic=VEHICLE_TOPIC, payload=encode(fixtures.vehicle(2)),
            qos=0, retain=False))
        self.start(owner)
        reply_topic = f'{PREFIX}/mapper/oak-1/clock/reply.pb'
        self.wait(lambda: any(row[0] == reply_topic for row in client.published)
            and any(row[0] == RUNTIME_TOPIC for row in client.published))
        self.assertEqual([row for row in client.published if row[0] == WORLD_TOPIC], [])
        self.assertTrue(owner.snapshot()['world_pending'])
        self.assertTrue(any(item.kind == 'VEHICLE' and decode(item.raw, schema.VehicleSample).sequence == 2
            for item in owner.take_inputs()))
        self.assertIsNotNone(owner.current_lease)
        clock.now = began + 99
        self.assertEqual([row for row in client.published if row[0] == WORLD_TOPIC], [])
        clock.now = began + 100
        self.wait(lambda: any(row[0] == WORLD_TOPIC for row in client.published))
        self.assertEqual([row[1] for row in client.published if row[0] == WORLD_TOPIC], [raw])
        self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)
        self.assertFalse(owner._thread.daemon)
        owner_id = owner._thread.ident
        self.assertEqual({row[1] for row in client.calls}, {owner_id})

    def test_preclaimed_publish_is_not_recalled_and_fault_configuration_does_not_wait_for_IO(self):
        entered, release, configured = threading.Event(), threading.Event(), threading.Event()
        errors = []

        class PausedClient(fixtures.FakeClient):
            def publish(self, topic, raw, **kwargs):
                if topic == WORLD_TOPIC:
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('bounded publish observation gate expired')
                return super().publish(topic, raw, **kwargs)

        client = PausedClient()
        _, owner = self.owner(client=client)
        raw = fixtures.world()
        self.assertTrue(self.offer(owner, raw=raw))
        self.start(owner, release)

        def configure():
            try:
                owner.set_world_egress_fault('BLOCK')
            except BaseException as error:
                errors.append(error)
            finally:
                configured.set()

        setter = threading.Thread(target=configure, name='test-world-fault-configure', daemon=False)
        try:
            self.assertTrue(entered.wait(2))
            setter.start()
            self.assertTrue(configured.wait(1), 'World fault setter waited on network IO')
            self.assertEqual(errors, [])
            self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)
            release.set()
            setter.join(2)
            self.assertFalse(setter.is_alive())
            self.wait(lambda: any(row[0] == WORLD_TOPIC for row in client.published))
            self.assertEqual([row[1] for row in client.published if row[0] == WORLD_TOPIC], [raw])
            self.assertTrue(self.offer(owner, 2))
            self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 1)
        finally:
            release.set()
            if setter.ident is not None:
                setter.join(2)
                self.assertFalse(setter.is_alive())

    def test_due_world_still_requires_actual_current_returned_clock_without_source_reset(self):
        clock, owner = self.owner()
        owner.set_world_egress_fault('DELAY', delay_us=10)
        with patch.object(owner._world_fault, 'offer', wraps=owner._world_fault.offer) as offered:
            self.assertTrue(self.offer(owner))
            original = offered.call_args.args[0]
        due = owner.snapshot()['world_fault']['held_due_us']
        lease = owner.current_lease
        renewed = decode(fixtures.returned_probe(), schema.MapperClockProbe)
        renewed.probe_seq += 1
        renewed.pi_send_us += 10
        fixtures.deliver(owner, f'{PREFIX}/mapper/oak-1/clock/request.pb', renewed)
        self.assertIsNot(owner.current_lease, lease)
        self.assertEqual(owner.snapshot()['world_fault']['held_due_us'], due)
        clock.now += 10
        self.assertIs(self.take(owner), original)
        token = owner._token
        missing = schema.MapperClockProbe()
        missing.CopyFrom(renewed)
        missing.probe_seq += 1
        missing.admission_revision += 1
        missing.ClearField('mapper_trust')
        fixtures.deliver(owner, f'{PREFIX}/mapper/oak-1/clock/request.pb', missing)
        self.assertIs(owner._token, token)
        self.assertIsNone(owner.current_lease)
        self.assertFalse(owner._claim_world(original))
        self.assertEqual(original.expires_us, 1_400_000)
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'DELAY')

    def test_due_scene_world_rechecks_known_negative_before_publication_clear(self):
        fixture = scene_fixtures.JioSurveyPortTests(methodName='runTest')
        self.addCleanup(fixture.doCleanups)
        clock, owner, grant = fixture.owner()
        binding = fixtures.publication(scene=scene_ref_fields(grant.scene), revision=2)
        owner.replace_publication(binding)
        owner.set_world_egress_fault('DELAY', delay_us=10)
        self.assertTrue(self.offer(owner, raw=fence_fixtures.scene_world(grant)))
        clock.now += 10
        original = self.take(owner)
        publication, token = owner._publication, owner._token
        negative = schema.SceneGrant()
        negative.CopyFrom(grant)
        negative.lifecycle_state = 3
        negative.lifecycle_revision += 1
        fixtures.deliver(owner, f'{PREFIX}/mapper/oak-1/scene.pb', negative, qos=1)
        self.assertIs(owner._publication, publication)
        self.assertIs(owner._token, token)
        self.assertFalse(owner._claim_world(original))
        self.assertEqual(original.expires_us, 1_400_000)
        self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 0)


if __name__ == '__main__':
    unittest.main()
