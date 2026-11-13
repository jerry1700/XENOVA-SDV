"""원 READY/출력 수명을 보존하는 AUTO 재시도. 실제 보드·센서 성공 증거는 아니다."""

from pathlib import Path
import tempfile
import unittest

from test_function_failure_scope import _World, A, B
from test_warm import WarmWorld, report
from xenova_gateway.machine import ForwardDrive, ForwardStop, Mode


class PipelineTransientRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = _World(Path(self.tmp.name)/'authority')
        self.w.arm()

    def test_four_distinct_late_results_keep_owner_then_fifth_can_select_b(self):
        w=self.w;generation=w.m.generation;output=w.m._last_forwarded_t
        for _ in range(4):
            w.tick(reasons={(A,'autonomous'):'DEADLINE'})
            self.assertEqual((w.m.mode,w.m.active_vm,w.m.generation),(Mode.AUTO,A,generation))
            self.assertFalse(any(isinstance(a,(ForwardDrive,ForwardStop)) for a in w.actions))
            self.assertEqual(w.m._last_forwarded_t,output)
        w.tick(reasons={(A,'autonomous'):'DEADLINE'})
        self.assertEqual((w.m.mode,w.m.active_vm),(Mode.AUTO,B))
        self.assertGreater(w.m.generation,generation)

    def test_same_failed_progress_cannot_count_five_or_renew_command(self):
        w=self.w;generation=w.m.generation;output=w.m._last_forwarded_t
        w.tick(.12,reasons={(A,'autonomous'):'DEADLINE'},stagnant={(A,'autonomous')})
        self.assertEqual((w.m.active_vm,w.m.generation),(A,generation))
        self.assertEqual(w.m._last_forwarded_t,output)
        self.assertTrue(any(isinstance(a,ForwardStop) for a in w.actions_seen))
        w.tick(.2,reasons={(A,'autonomous'):'DEADLINE'},stagnant={(A,'autonomous')})
        self.assertEqual(w.m.active_vm,B)

    def test_single_standby_input_gap_keeps_hold_history_without_qualifying_bad_b(self):
        w=self.w;old=w.m.qualification[B,'autonomous']
        w.tick(reasons={(B,'autonomous'):'WORLD_UNAVAILABLE'})
        self.assertEqual(w.m.qualification[B,'autonomous'],old)
        self.assertFalse(w.m._qualified(B,'autonomous',w.t,held=True))
        w.tick()
        self.assertTrue(w.m._qualified(B,'autonomous',w.t,held=True))
        self.assertEqual(w.m.qualification[B,'autonomous'][2],old[2])

    def test_recovery_requires_new_completed_ready_without_restarting_hold(self):
        w=self.w;old=w.m.prepared[A,'autonomous'].since
        w.tick(reasons={(A,'autonomous'):'DEADLINE'},bad_commands=True)
        p=w.m.prepared[A,'autonomous'];observed=p.observed
        self.assertFalse(any(isinstance(a,ForwardDrive) for a in w.actions))
        w.count[A,'autonomous']+=1
        self.assertFalse(w.m.on_lifecycle('autonomous',A,report(w.m.generation,
            w.count[A,'autonomous'],p.progress,'EXECUTING','READY',None),w.t))
        self.assertEqual(p.observed,observed)
        self.assertEqual(p.state,'NOT_READY')
        w.tick()
        self.assertEqual(p.since,old)
        self.assertTrue(any(isinstance(a,ForwardDrive) for a in w.actions))

    def test_current_input_failure_on_b_cannot_be_used_as_failover_permission(self):
        w=self.w
        w.tick(.1,reasons={(A,'autonomous'):'DEADLINE',(B,'autonomous'):'WORLD_UNAVAILABLE'})
        self.assertEqual(w.m.mode,Mode.SAFE_STOP)
        self.assertFalse(w.m.output_permitted)

    def test_hard_invalidity_and_binding_loss_are_not_debounced(self):
        w=self.w
        self.assertFalse(w.m._qualified('cccccccc-cccc-4ccc-8ccc-cccccccccccc','autonomous',w.t))
        w.tick(reasons={(A,'autonomous'):'INPUT_INVALID'})
        self.assertEqual(w.m.active_vm,B)
        w=self.w=_World(Path(self.tmp.name)/'binding');w.arm()
        w.tick(reasons={(A,'autonomous'):'DEADLINE'})
        w.alive.remove(A);w.tick()
        self.assertEqual(w.m.active_vm,B)

    def test_auto_ready_and_countdown_survive_short_gap_but_need_fresh_qualification(self):
        w=WarmWorld(Path(self.tmp.name)/'countdown',mode='AUTO_READY')
        w.tick(3.2);self.assertTrue(w.resume());w.tick(.02)
        countdown=w.m._countdown;generation=w.m.generation
        w.failures[A,'autonomous']='WORLD_UNAVAILABLE';w.tick(.02)
        self.assertEqual((w.m.mode,w.m.generation),(Mode.AUTO_READY,generation))
        self.assertIs(w.m._countdown,countdown)
        self.assertFalse(w.m._qualified(A,'autonomous',w.t,held=True))
        w.failures.clear();w.tick(.02)
        self.assertIs(w.m._countdown,countdown)
        self.assertTrue(w.m._qualified(A,'autonomous',w.t,held=True))


if __name__=='__main__':unittest.main()
