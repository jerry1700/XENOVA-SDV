"""결과 거절, 정상 planner 대기, AUTO 기능 상실을 실제 warm actor로 구분한다.

표준 unittest로도 실행한다. 장치 정지/VM 재기동을 흉내 내지 않고 출력 action,
원 세대/명령 시각 및 현재 binding에 연결된 상태 증거를 검사한다.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from xenova_mqtt.drive_binding import BindingCache
from xenova_mqtt.lifecycle import PROFILE
from xenova_gateway.authority import AuthorityStore
from xenova_gateway.machine import ForwardDrive, ForwardStop, Mode
from xenova_gateway.warm import WarmDriveMachine

A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
HOST = '11111111-1111-4111-8111-111111111111'
BOOT = '22222222-2222-4222-8222-222222222222'


class _World:
    def __init__(self, path, *, stateful=()):
        self.t = 100.
        self.alive = {A, B}
        self.ids = {vm: {app: str(uuid4()) for app in ('autonomous', 'manual')} for vm in (A, B)}
        self.executions = {vm: str(uuid4()) for vm in (A, B)}
        self.count = {}
        self.progress = {}
        self.stateful = set(stateful)
        self.auth = AuthorityStore(path, HOST, uid=os.geteuid(), mountpoint=None)
        self.auth.initialize(A)
        self.auth.update(mode='AUTO', generation=42, phase='DISARMED')
        self.cache = BindingCache(HOST, clock=lambda: self.t)
        self.bind()
        self.m = WarmDriveMachine(vm_ids=(A, B), authority=self.auth, bindings=self.cache, clock=lambda: self.t)
        self.m.start(self.t)
        self.m.note_transport(True, self.t)
        self.actions = []
        self.actions_seen = []

    def bind(self):
        self.cache.install(dict(schema_version='1.0-drive-bindings', host_boot_id=HOST,
            observed_monotonic_ms=int(self.t * 1000), expires_monotonic_ms=int(self.t * 1000) + 3000,
            vms={vm: dict(status='observed', profile=PROFILE, execution_id=self.executions[vm], boot_id=BOOT,
                app_instance_id=self.ids[vm]['autonomous'], app_instances=self.ids[vm], domid=i + 1,
                xen_unique_id=str(i + 1), app_state_models={
                    'autonomous': 'stateful' if vm in self.stateful else 'stateless', 'manual': 'stateless'},
                intent_revision=1, lifecycle_generation=1, observed_monotonic_ms=int(self.t * 1000))
                for i, vm in enumerate((A, B)) if vm in self.alive}))

    def tick(self, seconds=.02, *, reasons=None, stagnant=(), silent=(), commandless=(), bad_commands=False):
        reasons = reasons or {}
        for _ in range(round(seconds / .02)):
            self.t += .02
            self.bind()
            self.m.note_delivery(self.m.generation, 'acknowledged', self.t)
            for vm in self.alive:
                for app in ('autonomous', 'manual'):
                    key = vm, app
                    if key in silent:
                        continue
                    assert self.m.accept_session(vm, BOOT, self.ids[vm][app], self.t, app=app)
                    self.count[key] = self.count.get(key, 0) + 1
                    active = vm == self.m.active_vm and app == self.m.active_app
                    if active and key not in stagnant:
                        self.progress[key] = self.progress.get(key, 0) + 1
                    reason = reasons.get(key)
                    payload = dict(profile=PROFILE, applied_generation=self.m.generation,
                        check_seq=str(self.count[key]), progress_seq=str(self.progress.get(key, 0)),
                        phase='EXECUTING' if active else 'WAITING',
                        readiness='NOT_READY' if reason else 'READY' if active else 'QUALIFIED', reason=reason)
                    accepted = self.m.on_lifecycle(app, vm, payload, self.t)
                    assert accepted or (app == 'autonomous' and
                        self.auth.value['auto_recovery'][vm]['instance'] == self.ids[vm][app])
                    if active and key not in commandless and (not reason or bad_commands):
                        self.m.on_command(app, vm, dict(generation=self.m.generation, v_mps=.1, steer_rad=0), self.t,
                            dict(message_id=str(uuid4()), received_monotonic=self.t))
            self.actions = self.m.step(self.t)
            self.actions_seen.extend(self.actions)

    def arm(self):
        self.tick(3.2)
        assert self.m.on_resume(self.t, 'test', expected_generation=self.m.generation, message_id=str(uuid4()))
        self.tick(6.4)
        assert self.m.output_permitted


class FunctionFailureScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = _World(Path(self.tmp.name) / 'authority')
        self.w.arm()

    def test_one_late_result_is_not_an_owner_or_authority_change(self):
        w = self.w
        generation = w.m.generation
        original_command = w.m.nodes[A]['autonomous'].last_command_t
        w.tick(reasons={(A, 'autonomous'): 'DEADLINE'})
        self.assertEqual(w.m.prepared[A, 'autonomous'].state, 'NOT_READY')
        self.assertFalse(any(isinstance(action, ForwardDrive) for action in w.actions))
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, A, generation))
        self.assertEqual(w.m.nodes[A]['autonomous'].last_command_t, original_command)
        w.tick()
        self.assertTrue(any(isinstance(action, ForwardDrive) for action in w.actions))
        self.assertEqual((w.m.active_vm, w.m.generation), (A, generation))
        self.assertNotIn('auto_recovery', w.auth.value)

    def test_repeated_rejections_do_not_renew_original_command(self):
        w = self.w
        generation = w.m.generation
        original_command = w.m.nodes[A]['autonomous'].last_command_t
        before=len(w.actions_seen)
        w.tick(.12, reasons={(A, 'autonomous'): 'CALCULATION_FAILED'},stagnant={(A,'autonomous')})
        self.assertTrue(any(isinstance(action, ForwardStop) for action in w.actions_seen[before:]))
        self.assertEqual(w.m.nodes[A]['autonomous'].last_command_t, original_command)
        self.assertEqual((w.m.active_vm, w.m.generation), (A, generation))

    def test_one_command_gap_stops_at_original_expiry_without_changing_owner(self):
        w = self.w
        generation = w.m.generation
        original = w.m._last_forwarded_t
        before = len(w.actions_seen)
        w.tick(.12, commandless={(A, 'autonomous')})
        self.assertTrue(w.m._ready(A, 'autonomous', w.t))
        self.assertTrue(any(isinstance(action, ForwardStop) for action in w.actions_seen[before:]))
        self.assertFalse(any(isinstance(action, ForwardDrive) for action in w.actions_seen[before:]))
        self.assertEqual((w.m.active_vm, w.m.generation, w.m._last_forwarded_t), (A, generation, original))
        w.tick()
        self.assertTrue(any(isinstance(action, ForwardDrive) for action in w.actions))
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, A, generation))

    def test_ready_progress_does_not_renew_command_gap_confirmation(self):
        w = self.w
        generation = w.m.generation
        w.tick(.26, commandless={(A, 'autonomous')})
        self.assertEqual((w.m.active_vm, w.m.generation), (A, generation))
        w.tick(.06, commandless={(A, 'autonomous')})
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))
        self.assertGreater(w.m.generation, generation)

    def test_new_valid_command_resets_only_its_own_delivery_gap(self):
        w = self.w
        generation = w.m.generation
        w.tick(.12, commandless={(A, 'autonomous')})
        w.tick()
        original = w.m._last_forwarded_t
        w.tick(.26, commandless={(A, 'autonomous')})
        self.assertEqual((w.m.active_vm, w.m.generation, w.m._last_forwarded_t), (A, generation, original))

    def test_binding_loss_during_command_gap_is_not_delayed(self):
        w = self.w
        w.tick(.12, commandless={(A, 'autonomous')})
        w.alive.remove(A)
        w.tick()
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))

    def test_first_output_gap_uses_the_original_activation_work_anchor(self):
        w = _World(Path(self.tmp.name) / 'first-output-authority')
        w.tick(3.2, commandless={(A, 'autonomous')})
        self.assertTrue(w.m.on_resume(w.t, 'test', expected_generation=w.m.generation, message_id=str(uuid4())))
        w.tick(3.08, commandless={(A, 'autonomous')})
        self.assertTrue(w.m.output_permitted)
        self.assertIsNone(w.m._last_forwarded_t)
        generation = w.m.generation
        activated = w.m.prepared[A, 'autonomous'].since
        w.tick(.12, commandless={(A, 'autonomous')})
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, A, generation))
        self.assertEqual(w.m.prepared[A, 'autonomous'].since, activated)
        w.tick(.2, commandless={(A, 'autonomous')})
        # 새 세대 B의 기존3초 qualification hold가 아직 끝나지 않았다.
        self.assertEqual(w.m.mode, Mode.SAFE_STOP)
        self.assertFalse(w.m.output_permitted)

    def test_ready_after_normal_wait_gets_one_fixed_delivery_window(self):
        w = self.w
        generation = w.m.generation
        w.tick(.6, reasons={(A, 'autonomous'): 'SEARCH_NOT_FOUND'})
        w.tick(.12, commandless={(A, 'autonomous')})
        activated = w.m.prepared[A, 'autonomous'].since
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, A, generation))
        w.tick(.12, commandless={(A, 'autonomous')})
        self.assertEqual(w.m.prepared[A, 'autonomous'].since, activated)
        self.assertEqual(w.m.active_vm, A)
        w.tick(.1, commandless={(A, 'autonomous')})
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))

    def test_stopping_and_residual_phases_override_wait_or_input_reason(self):
        for phase, reason in (('STOPPING', 'CALCULATION_FAILED'), ('RESIDUAL', 'PATH_UNAVAILABLE'),
                ('STOPPING', 'WORLD_UNAVAILABLE')):
            with self.subTest(phase=phase, reason=reason):
                w = _World(Path(self.tmp.name) / ('phase-' + phase + reason))
                w.arm()
                w.count[A, 'autonomous'] += 1
                report = dict(profile=PROFILE, applied_generation=w.m.generation,
                    check_seq=str(w.count[A, 'autonomous']), progress_seq=str(w.progress[A, 'autonomous']),
                    phase=phase, readiness='NOT_READY', reason=reason)
                self.assertTrue(w.m.on_lifecycle('autonomous', A, report, w.t))
                w.m.step(w.t)
                self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))

    def test_rejected_commands_cannot_extend_the_previous_output(self):
        w = self.w
        generation = w.m.generation
        before = len(w.actions_seen)
        w.tick(.12, reasons={(A, 'autonomous'): 'CALCULATION_FAILED'},
            stagnant={(A,'autonomous')},bad_commands=True)
        self.assertTrue(any(isinstance(action, ForwardStop) for action in w.actions_seen[before:]))
        self.assertFalse(any(isinstance(action, ForwardDrive) for action in w.actions_seen[before:]))
        self.assertEqual((w.m.active_vm, w.m.generation), (A, generation))

    def test_sustained_calculation_failure_selects_current_qualified_b(self):
        w = self.w
        generation = w.m.generation
        executions = dict(w.executions)
        reasons = {(A, 'autonomous'): 'CALCULATION_FAILED'}
        w.tick(.08, reasons=reasons)
        self.assertEqual((w.m.active_vm, w.m.generation), (A, generation))
        w.tick(.02, reasons=reasons)
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))
        self.assertGreater(w.m.generation, generation)
        self.assertNotIn('auto_recovery', w.auth.value)
        self.assertEqual(w.executions, executions)
        w.tick(.1, reasons=reasons)
        self.assertTrue(w.m.output_permitted)

    def test_deadlines_commit_auto_only_withdrawal_then_new_state_can_rejoin(self):
        w = self.w
        generation = w.m.generation
        old = dict(w.ids[A])
        reasons = {(A, 'autonomous'): 'DEADLINE'}
        w.tick(.08, reasons=reasons)
        self.assertNotIn('auto_recovery', w.auth.value)
        w.tick(.02, reasons=reasons)
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))
        fault = w.auth.read_current()['auto_recovery'][A]
        self.assertEqual((fault['instance'], fault['generation'], fault['execution_id']),
            (old['autonomous'], generation, w.executions[A]))
        w.tick(.1)
        self.assertFalse(w.m._qualified(A, 'autonomous', w.t))
        self.assertTrue(w.m._qualified(A, 'manual', w.t))

        w.ids[A]['autonomous'] = str(uuid4())
        w.count[A, 'autonomous'] = 0
        w.progress[A, 'autonomous'] = 0
        w.stateful.add(A)
        w.tick(.1)
        self.assertFalse(w.m.accept_session(A, BOOT, old['autonomous'], w.t, app='autonomous'))
        self.assertFalse(w.m._qualified(A, 'autonomous', w.t))  # Lifecycle alone is insufficient.
        payload = dict(profile='drive-state-1', execution_id=w.executions[A], intent_revision=1,
            lifecycle_generation=1, copy_message_id=None, state_revision='1', origin='rebuilt', phase='APPLIED',
            context=dict(frame='1', configuration='2', maneuver='3', odom_epoch=BOOT,
                road_revision='4', route_revision='5'))
        report = SimpleNamespace(retained=False, type='app.state.applied', payload=payload,
            envelope=dict(vm_id=A, app_id='autonomous', boot_id=BOOT, agent_instance_id=w.ids[A]['autonomous']))
        self.assertTrue(w.m.states.receive(report))
        w.tick(3.2)
        self.assertTrue(w.m._qualified(A, 'autonomous', w.t, held=True))
        self.assertEqual(w.ids[A]['manual'], old['manual'])
        self.assertEqual(w.m.active_vm, B)
        self.assertTrue(w.m.output_permitted)

    def test_repeated_deadlines_without_qualified_backup_commit_safe_stop(self):
        w = self.w
        w.tick(.1, reasons={(A, 'autonomous'): 'DEADLINE', (B, 'autonomous'): 'OUTPUT_INVALID'})
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.output_permitted), (Mode.SAFE_STOP, A, False))
        self.assertEqual(set(w.auth.read_current()['auto_recovery']), {A})

    def test_late_completion_followed_by_a_stall_commits_app_restart(self):
        w = self.w
        key = (A, 'autonomous')
        w.tick(.02, reasons={key: 'DEADLINE'})
        w.tick(.26, reasons={key: 'DEADLINE'}, stagnant={key})
        self.assertEqual(w.m.active_vm, A)
        self.assertNotIn('auto_recovery', w.auth.value)
        w.tick(.06, reasons={key: 'CALCULATION_FAILED'}, stagnant={key})
        self.assertEqual(w.m.active_vm, B)
        self.assertEqual(w.auth.value['auto_recovery'][A]['instance'], w.ids[A]['autonomous'])

    def test_late_completion_then_silence_also_commits_restart(self):
        w = self.w
        key = (A, 'autonomous')
        w.tick(.02, reasons={key: 'DEADLINE'})
        w.tick(.32, silent={key})
        self.assertEqual(w.m.active_vm, B)
        self.assertIn(A, w.auth.value['auto_recovery'])

    def test_healthy_result_breaks_the_five_consecutive_misses(self):
        w = self.w
        reasons = {(A, 'autonomous'): 'DEADLINE'}
        w.tick(.08, reasons=reasons)
        w.tick()
        w.tick(.08, reasons=reasons)
        self.assertEqual(w.m.active_vm, A)
        self.assertNotIn('auto_recovery', w.auth.value)
        w.tick(.02, reasons=reasons)
        self.assertEqual(w.m.active_vm, B)
        self.assertIn(A, w.auth.value['auto_recovery'])

    def test_repeated_bad_output_is_not_a_deadline_recovery(self):
        w = self.w
        w.tick(.1, reasons={(A, 'autonomous'): 'OUTPUT_INVALID'})
        self.assertEqual(w.m.active_vm, B)
        self.assertNotIn('auto_recovery', w.auth.value)

    def test_no_qualified_b_stops_then_prepares_disarmed_manual(self):
        w = self.w
        w.tick(.34, reasons={(A, 'autonomous'): 'CALCULATION_FAILED',
            (B, 'autonomous'): 'CALCULATION_FAILED'})
        self.assertEqual(w.m.mode, Mode.SAFE_STOP)
        self.assertFalse(w.m.output_permitted)
        w.tick(3.2, reasons={(A, 'autonomous'): 'CALCULATION_FAILED',
            (B, 'autonomous'): 'CALCULATION_FAILED'})
        self.assertEqual(w.m.mode, Mode.MANUAL)
        self.assertEqual(w.m.active_vm, A)
        self.assertFalse(w.m.output_permitted)
        self.assertTrue(w.auth.value['rearm_required'])

    def test_normal_planner_wait_progress_does_not_trigger_vm_switch(self):
        w = self.w
        generation = w.m.generation
        for reason in ('PATH_UNAVAILABLE', 'PATH_INVALIDATED', 'SEARCH_NOT_FOUND', 'WORK_BUDGET', 'COLLISION',
                'STATE_PREPARING', 'CONTEXT_CHANGED', 'STATE_FRAME_CHANGED'):
            with self.subTest(reason=reason):
                before = len(w.actions_seen)
                w.tick(.4, reasons={(A, 'autonomous'): reason})
                self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, A, generation))
                self.assertTrue(any(isinstance(action, ForwardStop) for action in w.actions_seen[before:]))
                self.assertFalse(any(isinstance(action, ForwardDrive) for action in w.actions_seen[before:]))
        w.tick()
        self.assertTrue(any(isinstance(action, ForwardDrive) for action in w.actions))

    def test_fresh_supervision_cannot_hide_stalled_planner_work(self):
        w = self.w
        w.tick(.34, reasons={(A, 'autonomous'): 'PATH_UNAVAILABLE'}, stagnant={(A, 'autonomous')})
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))

    def test_common_input_loss_uses_manual_without_ab_flapping(self):
        w = self.w
        for reason in ('WORLD_UNAVAILABLE', 'POSE_UNAVAILABLE', 'CLOCK_ERROR'):
            # 각 경우는 정상 AUTO에서 시작하며 이전 장애 전환을 이어받지 않는다.
            with self.subTest(reason=reason):
                if w.m.mode is not Mode.AUTO:
                    w = _World(Path(self.tmp.name) / ('authority-' + reason))
                    w.arm()
                w.tick(.1,reasons={(A, 'autonomous'): reason, (B, 'autonomous'): reason})
                self.assertEqual((w.m.mode, w.m.active_vm), (Mode.MANUAL, A))
                w.tick(.2, reasons={(A, 'autonomous'): reason, (B, 'autonomous'): reason})
                self.assertEqual((w.m.mode, w.m.active_vm), (Mode.MANUAL, A))

    def test_only_a_input_failure_does_not_disqualify_prepared_b(self):
        w = self.w
        w.tick(.1,reasons={(A, 'autonomous'): 'WORLD_UNAVAILABLE'})
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))

    def test_real_binding_loss_still_selects_b_immediately(self):
        w = self.w
        old_instance = w.ids[A]['autonomous']
        w.alive.remove(A)
        w.tick()
        self.assertEqual((w.m.mode, w.m.active_vm), (Mode.AUTO, B))
        w.tick(.1)
        self.assertTrue(w.m.output_permitted)
        generation = w.m.generation

        w.alive.add(A)
        w.executions[A] = str(uuid4())
        w.ids[A] = {app: str(uuid4()) for app in ('autonomous', 'manual')}
        w.tick(silent={(A, 'autonomous'), (A, 'manual')})
        self.assertFalse(w.m.accept_session(A, BOOT, old_instance, w.t, app='autonomous'))
        self.assertFalse(w.m._qualified(A, 'autonomous', w.t))

        w.tick(w.m.timing.ready_hold_s + .1)
        self.assertTrue(w.m._qualified(A, 'autonomous', w.t, held=True))
        self.assertTrue(w.m.output_permitted)
        self.assertEqual((w.m.mode, w.m.active_vm, w.m.generation), (Mode.AUTO, B, generation))

    def test_rejected_work_preserves_applied_state_without_creating_readiness(self):
        w = self.w
        row = w.cache.current(A, w.t)
        row['app_state_models']['autonomous'] = 'stateful'
        rows = {A: row, B: w.cache.current(B, w.t)}
        w.cache.install(dict(schema_version='1.0-drive-bindings', host_boot_id=HOST,
            observed_monotonic_ms=int(w.t * 1000), expires_monotonic_ms=int(w.t * 1000) + 3000, vms=rows))
        payload = dict(profile='drive-state-1', execution_id=row['execution_id'], intent_revision=1,
            lifecycle_generation=1, copy_message_id=None, state_revision='1', origin='rebuilt', phase='APPLIED',
            context=dict(frame='1', configuration='2', maneuver='3', odom_epoch=BOOT,
                road_revision='4', route_revision='5'))
        message = SimpleNamespace(retained=False, type='app.state.applied', payload=payload,
            envelope=dict(vm_id=A, app_id='autonomous', boot_id=BOOT, agent_instance_id=w.ids[A]['autonomous']))
        self.assertTrue(w.m.states.receive(message))
        proof = w.m.states.evidence(A, 'autonomous')
        for reason in ('CALCULATION_FAILED', 'PATH_UNAVAILABLE', 'WORLD_UNAVAILABLE'):
            w.count[A, 'autonomous'] += 1
            report = dict(profile=PROFILE, applied_generation=w.m.generation,
                check_seq=str(w.count[A, 'autonomous']), progress_seq=str(w.progress[A, 'autonomous']),
                phase='EXECUTING', readiness='NOT_READY', reason=reason)
            self.assertTrue(w.m.on_lifecycle('autonomous', A, report, w.t))
            self.assertEqual(w.m.states.evidence(A, 'autonomous'), proof)
            self.assertFalse(w.m._ready(A, 'autonomous', w.t))
        w.count[A, 'autonomous'] += 1
        report.update(check_seq=str(w.count[A, 'autonomous']), reason='STATE_FRAME_CHANGED')
        self.assertTrue(w.m.on_lifecycle('autonomous', A, report, w.t))
        self.assertIsNone(w.m.states.evidence(A, 'autonomous'))


if __name__ == '__main__':
    unittest.main()
