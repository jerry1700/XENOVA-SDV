"""F02 원 MQTT 요청이 실제 JIO 필터에 닿는 소프트웨어 경계를 검사한다.

실제 controller/codec/non-daemon owner와 FakeClient를 쓴다. broker 권한·전달,
Pi의 입력 만료·STOP·차량 적용은 증명하지 않으며 원 World/E를 재작성하지 않는다.
"""

from hashlib import sha256
import json
import time
from types import SimpleNamespace
import unittest

import test_jetson_jio as fixtures
from xenova_vehicle.storage.contracts import canonical
from xenova_vehicle.transport.jetson_jio import JetsonJio, PREFIX, WORLD_TOPIC


class JioWorldFaultControlTests(unittest.TestCase):
    def owner(self):
        clock = fixtures.Clock(1_300_200)
        client = fixtures.FakeClient()
        owner = JetsonJio(fixtures.profile(), clock=clock, client_factory=lambda *_: client,
            service_instance=fixtures.uid(50))
        fixtures.bind(owner)
        fixtures.qualify_clock(owner)
        return clock, owner, client

    def envelope(self, payload, *, query=False, number=100):
        return canonical(dict(schema_version='1.0', message_id=fixtures.uid(number),
            source_id='dom0-server', boot_id=fixtures.uid(101), agent_instance_id=fixtures.uid(102),
            type='perception.fault.query' if query else 'perception.fault.command',
            seq=number, sent_at='2026-11-09T00:00:00.000Z', payload=payload))

    def receive(self, owner, payload, *, query=False, raw=None, qos=1, retained=False):
        topic = f'{PREFIX}/mapper/oak-1/fault/' + ('query.json' if query else 'command.json')
        owner._receive(SimpleNamespace(topic=topic, payload=raw or self.envelope(payload, query=query),
            qos=qos, retain=retained))
        return owner._fault_pending

    def set_request(self, owner, *, key=200, mode='DELAY', delay=20, duration=1):
        return dict(command_id=fixtures.uid(key), service_instance=fixtures.uid(50),
            window_id=owner.snapshot()['fault_control']['window_id'], action='SET',
            mode=mode, delay_ms=delay, duration_s=duration)

    def offer(self, owner, sequence=1):
        return owner.offer_world(fixtures.world(sequence), expires_j_us=1_400_000,
            publication_sha256=sha256(canonical(fixtures.publication())).hexdigest())

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertTrue(predicate(), 'bounded local JIO event did not occur')

    def close(self, owner):
        owner.stop()
        self.assertTrue(owner.join(2))
        self.assertIsNone(owner.snapshot()['failure'])

    def test_control_waits_for_original_boot_then_status_works_before_capture(self):
        _, owner, _ = self.owner()
        self.receive(owner, {'action': 'STATUS'}, query=True)
        self.assertIsNone(owner._fault_pending)
        self.assertEqual(owner.snapshot()['last_rejection'], 'FAULT_CONTROL_UNAVAILABLE')
        self.assertTrue(owner.bind_world_fault_control(fixtures.uid(10)))
        self.assertFalse(owner.bind_world_fault_control(fixtures.uid(10)))
        with self.assertRaises(ValueError):
            owner.bind_world_fault_control(fixtures.uid(999))
        item = self.receive(owner, {'action': 'STATUS'}, query=True)
        status = json.loads(item.raw)
        self.assertEqual(status['boot_id'], fixtures.uid(10))
        self.assertEqual(status['agent_instance_id'], fixtures.uid(50))
        self.assertTrue(status['payload']['source_bound'])
        self.assertEqual(status['payload']['mode'], 'PASS')

    def test_actual_mqtt_apply_is_idempotent_and_does_not_extend_pending_reply_or_duration(self):
        clock, owner, _ = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner)
        raw = self.envelope(request)
        first = self.receive(owner, request, raw=raw)
        applied = json.loads(first.raw)['payload']
        self.assertEqual(applied['status'], 'APPLIED')
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'DELAY')
        generation = owner._world_dispatch_generation
        deadline = owner.snapshot()['fault_control']['deadline_us']
        clock.now += 100
        duplicate = self.receive(owner, request, raw=raw)
        self.assertIs(duplicate, first)
        self.assertEqual(owner._world_dispatch_generation, generation)
        self.assertEqual(owner.snapshot()['fault_control']['deadline_us'], deadline)
        self.assertTrue(self.offer(owner))
        with owner._lock:
            self.assertIsNone(owner._take_world_locked())
        clock.now += 20_000
        with owner._lock:
            selected = owner._take_world_locked()
        self.assertEqual(selected.raw, fixtures.world(1))
        self.assertEqual(selected.expires_us, 1_400_000)
        self.assertTrue(owner._claim_world(selected))

    def test_source_fence_keeps_original_fault_deadline_but_allows_exact_clear_without_AUTO(self):
        _, owner, _ = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner, mode='BLOCK', delay=0)
        self.receive(owner, request)
        before = owner.snapshot()['fault_control']
        self.assertTrue(self.offer(owner))
        self.assertFalse(owner.snapshot()['world_pending'])
        owner.set_world_dispatch_enabled(False)
        owner.fence_source()
        after = owner.snapshot()['fault_control']
        self.assertEqual(after['deadline_us'], before['deadline_us'])
        self.assertEqual(after['active_command_id'], before['active_command_id'])
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'BLOCK')
        status = json.loads(self.receive(owner, {'action': 'STATUS'}, query=True).raw)['payload']
        clear = dict(command_id=fixtures.uid(201), service_instance=fixtures.uid(50),
            window_id=status['window_id'], action='CLEAR', active_command_id=request['command_id'],
            expected_revision=status['revision'])
        cleared = json.loads(self.receive(owner, clear).raw)['payload']
        self.assertEqual(cleared['status'], 'APPLIED')
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'PASS')
        self.assertFalse(owner._world_dispatch_enabled)
        self.assertFalse(self.offer(owner, 2))

    def test_non_daemon_owner_subscribes_and_emits_probe_then_filter_result_before_delayed_World(self):
        clock, owner, client = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        self.receive(owner, self.set_request(owner))
        self.assertTrue(self.offer(owner))
        owner.start()
        try:
            self.wait(lambda: len(client.published) >= 2)
            topics = [item[0] for item in client.published]
            self.assertEqual(topics[:2], [f'{PREFIX}/mapper/oak-1/clock/reply.pb',
                f'{PREFIX}/mapper/oak-1/fault/result.json'])
            self.assertNotIn(WORLD_TOPIC, topics)
            subscriptions = [(call[2], call[3]) for call in client.calls if call[0] == 'subscribe']
            self.assertIn((f'{PREFIX}/mapper/oak-1/fault/command.json', 1), subscriptions)
            self.assertIn((f'{PREFIX}/mapper/oak-1/fault/query.json', 1), subscriptions)
            clock.now += 20_000
            self.wait(lambda: any(item[0] == WORLD_TOPIC for item in client.published))
            world = next(item for item in client.published if item[0] == WORLD_TOPIC)
            self.assertEqual(world[1], fixtures.world(1))
        finally:
            self.close(owner)

    def test_duration_ends_in_owner_without_network_progress_and_never_flushes_old_World(self):
        clock, owner, _ = self.owner()
        class Disconnected(fixtures.FakeClient):
            def connect(self, *args, **kwargs):
                raise OSError('explicit local offline fixture')
        client = Disconnected()
        owner._factory = lambda *_: client
        owner.bind_world_fault_control(fixtures.uid(10))
        self.receive(owner, self.set_request(owner, delay=2000))
        self.assertTrue(self.offer(owner))
        deadline = owner.snapshot()['fault_control']['deadline_us']
        owner.start()
        try:
            self.wait(lambda: owner.snapshot()['counts']['connect_failed'] > 0)
            clock.now = deadline
            self.wait(lambda: owner.snapshot()['world_fault']['mode'] == 'PASS')
            self.assertFalse(owner.snapshot()['world_pending'])
            self.assertFalse(any(item[0] == WORLD_TOPIC for item in client.published))
            self.assertIsNone(owner.snapshot()['fault_control']['active_command_id'])
        finally:
            self.close(owner)

    def test_scenario4_BLOCK_stops_World_then_only_fresh_World_resumes_after_duration(self):
        clock, owner, client = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner, mode='BLOCK', delay=0, duration=1)
        applied = json.loads(self.receive(owner, request).raw)['payload']
        self.assertEqual(applied['status'], 'APPLIED')
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'BLOCK')

        # BLOCK 동안 계산 결과는 JIO 경계에서 소비되고 MQTT World로 나가지 않는다.
        self.assertTrue(self.offer(owner))
        self.assertFalse(owner.snapshot()['world_pending'])
        owner.start()
        try:
            self.wait(lambda: any(item[0].endswith('/fault/result.json') for item in client.published))
            self.assertFalse(any(item[0] == WORLD_TOPIC for item in client.published))

            deadline = owner.snapshot()['fault_control']['deadline_us']
            clock.now = deadline
            self.wait(lambda: owner.snapshot()['world_fault']['mode'] == 'PASS')
            self.assertIsNone(owner.snapshot()['fault_control']['active_command_id'])
            self.assertFalse(any(item[0] == WORLD_TOPIC for item in client.published))

            # 기간 종료 뒤에는 BLOCK 전에 버린 결과가 아니라 새로 계산된 결과만 송신한다.
            value = fixtures.decode(fixtures.world(2), fixtures.schema.WorldSnapshot)
            value.completed_us, value.published_us = clock.now - 20, clock.now - 10
            fresh = fixtures.encode(value)
            self.assertTrue(owner.offer_world(fresh, expires_j_us=clock.now + 100_000,
                publication_sha256=owner.snapshot()['publication_sha256']))
            self.wait(lambda: any(item[0] == WORLD_TOPIC for item in client.published))
            worlds = [item for item in client.published if item[0] == WORLD_TOPIC]
            self.assertEqual([item[1] for item in worlds], [fresh])
        finally:
            self.close(owner)

    def test_wrong_qos_and_retained_commands_do_not_change_filter(self):
        _, owner, _ = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner)
        generation = owner._world_dispatch_generation
        self.receive(owner, request, qos=0)
        self.receive(owner, request, retained=True)
        self.assertEqual(owner.snapshot()['world_fault']['mode'], 'PASS')
        self.assertEqual(owner._world_dispatch_generation, generation)

    def test_explicit_cached_result_query_after_local_send_expiry_gets_new_attempt_only(self):
        clock, owner, _ = self.owner()
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner, mode='BLOCK', delay=0, duration=5)
        first = self.receive(owner, request)
        before = owner.snapshot()['fault_control']
        generation = owner._world_dispatch_generation
        clock.now = first.expires_us
        self.assertFalse(owner._claim_fault_publication(first))
        queried = self.receive(owner, dict(action='RESULT', command_id=request['command_id']), query=True)
        self.assertEqual(queried.raw, first.raw)
        self.assertIsNot(queried, first)
        self.assertEqual(queried.expires_us, clock.now + 1_000_000)
        self.assertEqual(owner.snapshot()['fault_control']['deadline_us'], before['deadline_us'])
        self.assertEqual(owner.snapshot()['fault_control']['revision'], before['revision'])
        self.assertEqual(owner._world_dispatch_generation, generation)

    def test_expiry_apply_failure_is_one_shot_UNKNOWN_and_keeps_input_owner_alive_until_clear(self):
        class FailedExpiry(JetsonJio):
            fail_pass = True
            pass_attempts = 0

            def _set_world_fault_locked(self, mode, *, delay_us, now_us):
                if mode == 'PASS':
                    self.pass_attempts += 1
                    if self.fail_pass:
                        self.fail_pass = False
                        raise RuntimeError('explicit local callback failure before apply')
                return super()._set_world_fault_locked(mode, delay_us=delay_us, now_us=now_us)

        clock = fixtures.Clock(1_300_200)
        client = fixtures.FakeClient()
        owner = FailedExpiry(fixtures.profile(), clock=clock, client_factory=lambda *_: client,
            service_instance=fixtures.uid(50))
        fixtures.bind(owner)
        fixtures.qualify_clock(owner)
        owner.bind_world_fault_control(fixtures.uid(10))
        request = self.set_request(owner, mode='BLOCK', delay=0)
        self.receive(owner, request)
        clock.now = owner.snapshot()['fault_control']['deadline_us']
        owner.start()
        try:
            self.wait(lambda: not owner.snapshot()['fault_control']['applied_known'])
            client.incoming.append(SimpleNamespace(topic=f'{PREFIX}/vehicle/state.pb',
                payload=fixtures.encode(fixtures.vehicle(2)), qos=0, retain=False))
            self.wait(lambda: owner._vehicle is not None)
            self.assertTrue(owner.snapshot()['alive'])
            self.assertEqual(owner.pass_attempts, 1)
            self.assertEqual(owner.snapshot()['world_fault']['mode'], 'BLOCK')
            status = json.loads(self.receive(owner, {'action': 'STATUS'}, query=True).raw)['payload']
            self.assertFalse(status['applied_known'])
            self.assertEqual(owner.pass_attempts, 1)
            clear = dict(command_id=fixtures.uid(201), service_instance=fixtures.uid(50),
                window_id=status['window_id'], action='CLEAR', active_command_id=request['command_id'],
                expected_revision=status['revision'])
            self.assertEqual(json.loads(self.receive(owner, clear).raw)['payload']['status'], 'APPLIED')
            self.assertEqual(owner.pass_attempts, 2)
            self.assertEqual(owner.snapshot()['world_fault']['mode'], 'PASS')
        finally:
            self.close(owner)

    def test_final_claim_consumes_duration_boundary_and_blocks_unknown_expiry(self):
        for fail in (False, True):
            with self.subTest(expiry_callback_fails=fail):
                class ExpiryBoundary(JetsonJio):
                    def _set_world_fault_locked(self, mode, *, delay_us, now_us):
                        if fail and mode == 'PASS':
                            raise RuntimeError('explicit boundary apply failure')
                        return super()._set_world_fault_locked(mode, delay_us=delay_us, now_us=now_us)

                clock = fixtures.Clock(1_000_000)
                owner = ExpiryBoundary(fixtures.profile(), clock=clock, service_instance=fixtures.uid(50))
                fixtures.bind(owner)
                owner.bind_world_fault_control(fixtures.uid(10))
                self.receive(owner, self.set_request(owner, delay=1))
                deadline = owner.snapshot()['fault_control']['deadline_us']
                clock.now = 1_300_200
                fixtures.qualify_clock(owner)
                clock.now = deadline - 2000
                value = fixtures.decode(fixtures.world(), fixtures.schema.WorldSnapshot)
                value.completed_us, value.published_us = clock.now - 20, clock.now - 10
                raw = fixtures.encode(value)
                self.assertTrue(owner.offer_world(raw, expires_j_us=deadline + 10_000,
                    publication_sha256=owner.snapshot()['publication_sha256']))
                clock.now += 1000
                with owner._lock:
                    selected = owner._take_world_locked()
                self.assertIsNotNone(selected)
                clock.now = deadline
                self.assertLess(clock.now, selected.expires_us)
                self.assertFalse(owner._claim_world(selected))
                self.assertEqual(owner.snapshot()['world_fault']['mode'], 'DELAY' if fail else 'PASS')
                self.assertEqual(owner.snapshot()['fault_control']['applied_known'], not fail)
                self.assertEqual(owner.snapshot()['counts']['world_dispatch_started'], 0)

    def test_continuous_STATUS_queries_cannot_starve_PASS_World(self):
        raw = self.envelope({'action': 'STATUS'}, query=True)
        topic = f'{PREFIX}/mapper/oak-1/fault/query.json'
        class StatusEveryLoop(fixtures.FakeClient):
            def loop(self, timeout):
                self.incoming.append(SimpleNamespace(topic=topic, payload=raw, qos=1, retain=False))
                return super().loop(timeout)

        clock = fixtures.Clock(1_300_200)
        client = StatusEveryLoop()
        owner = JetsonJio(fixtures.profile(), clock=clock, client_factory=lambda *_: client,
            service_instance=fixtures.uid(50))
        fixtures.bind(owner)
        fixtures.qualify_clock(owner)
        owner.bind_world_fault_control(fixtures.uid(10))
        self.assertTrue(self.offer(owner))
        owner.start()
        try:
            self.wait(lambda: any(item[0] == WORLD_TOPIC for item in client.published))
            topics = [item[0] for item in client.published]
            before_world = topics[:topics.index(WORLD_TOPIC)]
            self.assertEqual(before_world[0], f'{PREFIX}/mapper/oak-1/clock/reply.pb')
            self.assertEqual(before_world.count(f'{PREFIX}/mapper/oak-1/fault/status.json'), 1)
            self.assertEqual(owner.snapshot()['world_fault']['mode'], 'PASS')
        finally:
            self.close(owner)


if __name__ == '__main__':
    unittest.main()
