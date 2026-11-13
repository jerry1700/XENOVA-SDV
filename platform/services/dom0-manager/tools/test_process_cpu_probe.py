"""가상 /proc만 사용하는 stdlib 회귀. 서비스·실제 CPU 계측·제품 import는 하지 않는다."""

from dataclasses import asdict
import argparse
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from process_cpu_probe import (
    Scanner, cpu_delta, memberships, next_sample_slot, parse_stat, root_argument,
)


def stat_bytes(pid, *, parent=1, start=10, user=10, system=5, comm=b'worker', child_user=0):
    # fields3..22. child_user는 의도적으로 자기 utime과 다른 필드에 둔다.
    fields = ['S', str(parent), '0', '0', '0', '0', '0', '0', '0', '0', '0',
              str(user), str(system), str(child_user), '99999', '20', '0', '1', '0', str(start)]
    return str(pid).encode() + b' (' + comm + b') ' + ' '.join(fields).encode() + b'\n'


def row(pid=100, *, start=10, user=10, system=5, at=1_000_000_000):
    return {**asdict(parse_stat(stat_bytes(pid, start=start, user=user, system=system))),
            'observed_monotonic_ns':at}


class CpuProbeTests(unittest.TestCase):
    def test_stat_comm_parentheses_and_child_cpu_do_not_shift_own_ticks(self):
        value = parse_stat(stat_bytes(100, parent=99, user=17, system=3,
                                     comm=b'a name ) with (parentheses)', child_user=2**60))
        self.assertEqual((value.pid, value.ppid, value.utime_ticks, value.stime_ticks), (100, 99, 17, 3))
        self.assertNotIn('comm', asdict(value))
        self.assertNotIn('cutime', asdict(value))

    def test_malformed_partial_negative_and_wrong_field_layout_are_rejected(self):
        invalid = [b'', b'100 comm S 1', b'100 (comm) S 1', stat_bytes(0),
                   stat_bytes(100, start=0), stat_bytes(100, user=-1), b'x' * 16385]
        for raw in invalid:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ValueError):parse_stat(raw)

    def test_delta_uses_hz_actual_wall_and_can_exceed_one_core(self):
        delta = cpu_delta(row(user=40, system=15, at=1_100_000_000), row(), 100)
        self.assertEqual(delta['state'], 'MATCHED')
        self.assertAlmostEqual(delta['cpu_seconds'], .4)
        self.assertAlmostEqual(delta['wall_seconds'], .1)
        self.assertAlmostEqual(delta['cpu_pct_one_core'], 400.)

    def test_reused_identity_regression_and_zero_time_never_become_cpu_delta(self):
        old = row()
        for current, expected in [(row(start=11), 'IDENTITY_CHANGED'),
                                  (row(user=9, at=2_000_000_000), 'INVALID_COUNTER_OR_TIME'),
                                  (row(), 'INVALID_COUNTER_OR_TIME')]:
            with self.subTest(expected=expected):self.assertEqual(cpu_delta(current, old, 100), dict(state=expected))
        self.assertEqual(cpu_delta(row(user=5000), None, 100), dict(state='FIRST_OBSERVATION'))

    def test_nested_roots_union_and_retained_reparented_child(self):
        values = [parse_stat(stat_bytes(100)), parse_stat(stat_bytes(101, parent=100, start=11)),
                  parse_stat(stat_bytes(102, parent=101, start=12))]
        table = {value.pid:value for value in values}
        found, saturated = memberships(table, {(100,10):{'manager'}, (101,11):{'worker'}}, {})
        self.assertFalse(saturated)
        self.assertEqual(found[(102,12)], {'manager','worker'})
        moved = parse_stat(stat_bytes(102, parent=1, start=12))
        retained, _ = memberships({102:moved}, {}, {(102,12):found[(102,12)]})
        self.assertEqual(retained[(102,12)], {'manager','worker'})

    def test_reused_root_cannot_attach_new_process_and_descendant_limit_is_explicit(self):
        root = parse_stat(stat_bytes(100, start=99)); child = parse_stat(stat_bytes(101, parent=100, start=100))
        found, _ = memberships({100:root,101:child}, {(100,10):{'manager'}}, {})
        self.assertEqual(found, {})
        found, saturated = memberships({100:root,101:child}, {root.identity:{'manager'}}, {}, limit=1)
        self.assertEqual(list(found), [root.identity]); self.assertTrue(saturated)

    def test_skipped_periods_are_not_replayed_as_burst_samples(self):
        self.assertEqual(next_sample_slot(0, 1000, 1100, 500), 1)
        self.assertEqual(next_sample_slot(0, 1000, 2700, 500), 4)
        self.assertEqual(next_sample_slot(4, 1000, 3000, 500), 5)

    def test_cli_root_names_and_pid_limits_are_bounded(self):
        self.assertEqual(root_argument('gateway=1515'), ('gateway',1515))
        for value in ['observer=1','union=1','x=0','x=-1','x=2147483648','x='+('9'*100),'bad label=1']:
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):root_argument(value)


class FakeProcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.proc = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def put(self, pid, *, parent=1, start=10, user=10, system=5, tids=()):
        base = self.proc / str(pid); (base/'task').mkdir(parents=True, exist_ok=True)
        raw = stat_bytes(pid, parent=parent, start=start, user=user, system=system)
        (base/'stat').write_bytes(raw)
        (base/'task'/str(pid)).mkdir(exist_ok=True); (base/'task'/str(pid)/'stat').write_bytes(raw)
        for tid, task_start, task_user in tids:
            path = base/'task'/str(tid); path.mkdir(exist_ok=True)
            (path/'stat').write_bytes(stat_bytes(tid, parent=parent, start=task_start, user=task_user))

    def scanner(self, roots=None):
        return Scanner(roots or {'manager':100}, 100, proc=self.proc, observer=900,
                       clock=lambda:next(self.times))

    def test_roots_only_has_bounded_direct_reads_without_pid_or_task_enumeration(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(101, parent=100, start=11); self.put(900)
        scanner = Scanner({'manager':100},100,proc=self.proc,observer=900,
                          clock=lambda:next(self.times),scope='roots')
        with patch.object(Path,'iterdir',side_effect=AssertionError('no global/TID scan')), \
             patch.object(scanner,'read',wraps=scanner.read) as reads:
            scanner.capture()
            self.put(100,user=20); self.put(101,parent=100,start=11,user=1000); self.put(900,user=12)
            sample = scanner.capture()
            self.assertEqual(reads.call_count,8)  # 두 PID의 초기·재검산을 두 번씩 수행했다.
            self.assertTrue(all(call.args[1] in (100,900) for call in reads.call_args_list))
        self.assertEqual({row['pid'] for row in sample['processes']},{100,900})
        self.assertEqual(sample['threads'],[])
        self.assertEqual(sample['total_stat_read_attempts'],4)
        self.assertEqual(sample['task_stat_reads'],0)
        self.assertAlmostEqual(sample['known_process_subtotals']['union']['known_cpu_seconds'],.1)
        self.assertAlmostEqual(sample['known_process_subtotals']['observer']['known_cpu_seconds'],.02)

    def test_roots_only_reuse_or_exit_does_not_adopt_unobserved_descendants(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(900)
        scanner = Scanner({'manager':100},100,proc=self.proc,observer=900,
                          clock=lambda:next(self.times),scope='roots')
        scanner.capture(); self.put(100,start=99); self.put(103,parent=100,start=100)
        sample = scanner.capture()
        self.assertEqual(sample['roots']['manager']['state'],'REUSED')
        self.assertEqual({row['pid'] for row in sample['processes']},{900})
        self.assertEqual(sample['process_identity_events'][0]['state'],'REUSED')
        (self.proc/'100'/'stat').unlink()
        sample = scanner.capture()
        self.assertEqual(sample['roots']['manager']['state'],'VANISHED')
        self.assertEqual({row['pid'] for row in sample['processes']},{900})
        self.assertEqual(sample['threads'],[])

    def test_selected_tasks_only_process_union_does_not_add_thread_cpu(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(101, parent=100, start=11); self.put(900)
        # 다른 프로세스의 task 디렉터리는 일부러 없다. 전체 TID를 읽으면 실패가 생긴다.
        unrelated = self.proc/'777'; unrelated.mkdir(); (unrelated/'stat').write_bytes(stat_bytes(777))
        scanner = self.scanner({'manager':100,'worker':101}); scanner.capture()
        self.put(100, user=20, tids=((102,12,5000),)); self.put(101, parent=100, start=11, user=30); self.put(900,user=12)
        observed = scanner.capture()
        self.assertEqual({r['pid'] for r in observed['processes']}, {100,101,900})
        self.assertNotIn(777, {r['tgid'] for r in observed['threads']})
        self.assertEqual(observed['selected_errors'], [])
        self.assertAlmostEqual(observed['known_process_subtotals']['union']['known_cpu_seconds'], .3)
        self.assertAlmostEqual(observed['known_process_subtotals']['worker']['known_cpu_seconds'], .2)
        self.assertAlmostEqual(observed['known_process_subtotals']['observer']['known_cpu_seconds'], .02)
        task = next(r for r in observed['threads'] if r['pid']==102)
        self.assertEqual(task['delta']['state'], 'FIRST_OBSERVATION')

    def test_root_reuse_is_reported_without_following_new_owner(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(900); scanner = self.scanner(); scanner.capture()
        self.put(100, start=99); self.put(103, parent=100, start=100)
        observed = scanner.capture()
        self.assertEqual(observed['roots']['manager']['state'], 'REUSED')
        self.assertEqual({r['pid'] for r in observed['processes']}, {900})
        self.assertEqual(observed['process_identity_events'][0]['state'], 'REUSED')

    def test_root_missing_at_baseline_is_not_adopted_later(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(900); scanner = self.scanner(); scanner.capture(); self.put(100)
        observed = scanner.capture()
        self.assertEqual(observed['roots']['manager']['state'], 'BASELINE_UNAVAILABLE')
        self.assertEqual({r['pid'] for r in observed['processes']}, {900})

    def test_identity_change_while_reading_tasks_discards_mixed_rows(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(900); scanner = self.scanner()
        original = scanner.read; reads = 0
        def replaced(path, expected):
            nonlocal reads
            if path == self.proc/'100'/'stat':
                reads += 1
                if reads == 2:return parse_stat(stat_bytes(100,start=999))
            return original(path, expected)
        scanner.read = replaced
        observed = scanner.capture()
        self.assertEqual({r['pid'] for r in observed['processes']}, {900})
        self.assertEqual({r['tgid'] for r in observed['threads']}, {900})
        self.assertEqual(observed['roots']['manager']['state'], 'UNAVAILABLE_DURING_SCAN')
        self.assertEqual(observed['selected_errors'][0]['reason'], 'IDENTITY_CHANGED_DURING_SCAN')

    def test_root_exit_does_not_drop_original_reparented_child_or_add_parent_child_cpu(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(101, parent=100, start=11); self.put(900)
        scanner = self.scanner(); scanner.capture()
        (self.proc/'100').rename(self.proc/'exited-100')
        self.put(101, parent=1, start=11, user=20)
        observed = scanner.capture()
        self.assertEqual(observed['roots']['manager']['state'], 'ABSENT')
        self.assertEqual({r['pid'] for r in observed['processes']}, {101,900})
        child = next(r for r in observed['processes'] if r['pid']==101)
        self.assertEqual((child['ppid'], child['roots']), (1,['manager']))
        self.assertAlmostEqual(observed['known_process_subtotals']['union']['known_cpu_seconds'], .1)

    def test_stat_permission_failure_is_unavailable_not_exit_or_zero_cpu(self):
        self.times = itertools.count(1_000_000_000, 1_000_000)
        self.put(100); self.put(900); scanner = self.scanner(); scanner.capture()
        original = scanner.read
        def denied(path, expected):
            if expected == 100:raise PermissionError(13, 'deliberate fixture denial')
            return original(path, expected)
        scanner.read = denied; observed = scanner.capture()
        self.assertEqual(observed['roots']['manager']['state'], 'PERMISSION_DENIED')
        self.assertEqual(observed['process_identity_events'][0]['state'], 'PERMISSION_DENIED')
        self.assertIsNone(next((r for r in observed['processes'] if r['pid']==100),None))


if __name__ == '__main__':
    unittest.main()
