"""수동 실행하는 Linux 개발 계측기. 서비스·인증·출력 권한에는 연결하지 않는다.

선택한 프로세스 계보와 스레드의 원 /proc ticks를 유한 기간 수집한다. 프로세스
합계에는 utime/stime만 사용하며 TID와 부모의 cutime/cstime을 다시 더하지 않는다.
각 read는 순차 관측이다. 실제 시각·scan skew·누락과 계측자 비용을 함께 남긴다.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import sys
import time

MAX_SCAN_PIDS = 8192
MAX_PROCESSES = 256
MAX_TIDS = 1024
MAX_ROWS = 8000
MAX_SAMPLES = 601
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_STAT_BYTES = 16384


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ppid: int
    state: str
    utime_ticks: int
    stime_ticks: int
    nice: int
    num_threads: int
    starttime_ticks: int

    @property
    def identity(self):
        return self.pid, self.starttime_ticks


def parse_stat(raw: bytes) -> ProcStat:
    """comm에 공백·괄호가 있어도 필드 위치를 보존한다. comm 자체는 저장하지 않는다."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_STAT_BYTES:
        raise ValueError('STAT_SIZE')
    left = raw.find(b' ('); right = raw.rfind(b') ')
    if left < 1 or right <= left:
        raise ValueError('STAT_LAYOUT')
    fields = raw[right + 2:].split()
    if len(fields) < 20 or len(fields[0]) != 1 or not fields[0].isalpha():
        raise ValueError('STAT_FIELDS')
    try:
        value = ProcStat(int(raw[:left]), int(fields[1]), fields[0].decode('ascii'),
                         int(fields[11]), int(fields[12]), int(fields[16]),
                         int(fields[17]), int(fields[19]))
    except (ValueError, UnicodeError) as error:
        raise ValueError('STAT_NUMBERS') from error
    if (value.pid <= 0 or value.ppid < 0 or value.starttime_ticks <= 0
            or min(value.utime_ticks, value.stime_ticks) < 0
            or not -20 <= value.nice <= 19 or value.num_threads < 1):
        raise ValueError('STAT_RANGE')
    return value


def cpu_delta(current, previous, hz):
    """같은 PID/starttime의 연속 실제 관측만 차분한다. 새 PID의 누계는 기간 CPU가 아니다."""
    if previous is None:
        return dict(state='FIRST_OBSERVATION')
    if (current['pid'], current['starttime_ticks']) != (previous['pid'], previous['starttime_ticks']):
        return dict(state='IDENTITY_CHANGED')
    dt = current['observed_monotonic_ns'] - previous['observed_monotonic_ns']
    du = current['utime_ticks'] - previous['utime_ticks']
    ds = current['stime_ticks'] - previous['stime_ticks']
    if dt <= 0 or min(du, ds) < 0:
        return dict(state='INVALID_COUNTER_OR_TIME')
    seconds = (du + ds) / hz
    return dict(state='MATCHED', utime_ticks=du, stime_ticks=ds,
                cpu_seconds=seconds, wall_seconds=dt / 1e9,
                cpu_pct_one_core=seconds * 1e11 / dt)


def memberships(table, seeds, retained, limit=MAX_PROCESSES):
    """현재 일치한 root/이전에 발견한 동일 실행의 자식만 합친다. PID 재사용을 따라가지 않는다."""
    found = {}
    for source in (seeds, retained):
        for identity, labels in source.items():
            if identity[0] in table and table[identity[0]].identity == identity:
                found.setdefault(identity, set()).update(labels)
    children = defaultdict(list)
    for value in table.values():
        children[value.ppid].append(value)
    queue = deque(found)
    saturated = len(found) > limit
    if saturated:found = dict(list(found.items())[:limit])
    while queue and not saturated:
        parent = queue.popleft()
        for child in children[parent[0]]:
            if child.identity == parent:
                continue
            labels = found.setdefault(child.identity, set())
            old = len(labels); labels.update(found[parent])
            if len(found) > limit:
                found.pop(child.identity, None); saturated = True; break
            if len(labels) != old:
                queue.append(child.identity)
    return found, saturated


class Scanner:
    """/proc stat 목록은 얇게 읽고 task 상세는 선택한 계보와 계측자에만 한정한다."""
    def __init__(self, roots, hz, *, proc=Path('/proc'), observer=None,
                 clock=time.monotonic_ns, affinity=False, scope='tree'):
        if scope not in ('tree', 'roots'):raise ValueError('SCOPE')
        self.roots = roots; self.hz = hz; self.proc = proc
        self.observer = os.getpid() if observer is None else observer
        self.clock = clock; self.affinity = affinity; self.scope = scope; self.pinned = None
        self.retained = {}; self.previous = None

    def read(self, path, expected):
        # 원 stat/comm은 저장하지 않으며 cmdline/environ은 열지 않는다.
        with path.open('rb') as stream:
            raw = stream.read(MAX_STAT_BYTES + 1)
        value = parse_stat(raw)
        if value.pid != expected:
            raise ValueError('STAT_PID_MISMATCH')
        return value

    @staticmethod
    def error(error):
        if isinstance(error, FileNotFoundError):return 'VANISHED'
        if isinstance(error, PermissionError):return 'PERMISSION_DENIED'
        if isinstance(error, ValueError):return str(error)
        return 'OS_ERROR_' + str(error.errno)

    def row(self, value):
        result = asdict(value); result['observed_monotonic_ns'] = self.clock()
        if self.affinity:
            try:
                cpus = sorted(os.sched_getaffinity(value.pid))
                result['affinity'] = dict(cpus=cpus[:64], cpu_count=len(cpus), truncated=len(cpus) > 64)
            except OSError as error:
                result['affinity'] = dict(unavailable=self.error(error))
        return result

    def capture(self):
        begin = self.clock(); table = {}; failures = {}; process_rows = []; task_rows = []
        names = []
        if self.scope == 'roots':
            # 낮은 비용의 고정 PID 관측이다. 발견하지 않은 자식 CPU는 합산하지 않는다.
            names = sorted({*self.roots.values(), self.observer})
        else:
            for path in self.proc.iterdir():
                if path.name.isascii() and path.name.isdecimal():
                    names.append(int(path.name))
                    if len(names) > MAX_SCAN_PIDS:break
        scan_saturated = len(names) > MAX_SCAN_PIDS; names.sort()
        for pid in names[:MAX_SCAN_PIDS]:
            try:table[pid] = self.read(self.proc / str(pid) / 'stat', pid)
            except (OSError, ValueError) as error:failures[pid] = self.error(error)
        if self.pinned is None:
            self.pinned = {label:table[pid].identity if pid in table else None for label, pid in self.roots.items()}
        roots = {}; seeds = {}
        for label, pid in self.roots.items():
            pin = self.pinned[label]; value = table.get(pid)
            state = ('BASELINE_UNAVAILABLE' if pin is None else
                     'REUSED' if value is not None and value.identity != pin else
                     'CURRENT' if value is not None else
                     failures.get(pid, 'NOT_OBSERVED_SCAN_LIMIT' if scan_saturated else 'ABSENT'))
            roots[label] = dict(pid=pid, starttime_ticks=None if pin is None else pin[1], state=state)
            if state == 'CURRENT':seeds.setdefault(pin, set()).add(label)
        selected, descendants_saturated = (({key:set(value) for key,value in seeds.items()}, False)
            if self.scope == 'roots' else memberships(table, seeds, self.retained))
        self.retained = {key:set(value) for key, value in selected.items()}
        # Observer는 서비스 합계에서 항상 제외한다. 출력 표에만 별도로 남긴다.
        observer = table.get(self.observer)
        if observer is not None:selected.setdefault(observer.identity, set())
        tids_saturated = False; selected_errors = []; tasks_seen = 0; rechecks = 0
        order = sorted(selected, key=lambda key:(key[0] != self.observer, key[0]))
        for identity in order:
            pid = identity[0]; original = table[pid]; these_tasks = []
            try:
                task_names = []
                paths = () if self.scope == 'roots' else (self.proc / str(pid) / 'task').iterdir()
                for path in paths:
                    if path.name.isascii() and path.name.isdecimal():
                        task_names.append(int(path.name))
                        if len(task_names) > max(0, MAX_TIDS - tasks_seen):break
                task_names.sort()
            except OSError as error:
                task_names = []; selected_errors.append(dict(pid=pid, stage='TASK_LIST', reason=self.error(error)))
            remaining = max(0, MAX_TIDS - tasks_seen)
            if len(task_names) > remaining:tids_saturated = True
            for tid in task_names[:remaining]:
                tasks_seen += 1
                try:
                    task = self.row(self.read(self.proc / str(pid) / 'task' / str(tid) / 'stat', tid))
                    task['tgid'] = pid; task['tgid_starttime_ticks'] = identity[1]; these_tasks.append(task)
                except (OSError, ValueError) as error:
                    selected_errors.append(dict(pid=pid, tid=tid, stage='TASK_STAT', reason=self.error(error)))
            try:
                rechecks += 1
                final = self.read(self.proc / str(pid) / 'stat', pid)
                if final.identity != original.identity:raise ValueError('IDENTITY_CHANGED_DURING_SCAN')
                row = self.row(final); row['roots'] = sorted(selected[identity])
                row['observer'] = pid == self.observer
                if pid == self.observer:row['roots'] = []
                process_rows.append(row); task_rows.extend(these_tasks)
            except (OSError, ValueError) as error:
                selected_errors.append(dict(pid=pid, stage='PROCESS_RECHECK', reason=self.error(error)))
                for label in selected[identity]:
                    if self.roots[label] == pid:
                        roots[label].update(state='UNAVAILABLE_DURING_SCAN', reason=self.error(error))
        previous = self.previous
        old_process = {} if previous is None else {r['pid']:r for r in previous['processes']}
        task_key = lambda row:(row['tgid'], row['tgid_starttime_ticks'], row['pid'])
        old_task = {} if previous is None else {task_key(r):r for r in previous['threads']}
        for row in process_rows:row['delta'] = cpu_delta(row, old_process.get(row['pid']), self.hz)
        for row in task_rows:row['delta'] = cpu_delta(row, old_task.get(task_key(row)), self.hz)
        present = {r['pid']:r for r in process_rows}; changes = []
        for pid, old in old_process.items():
            if pid in table and table[pid].starttime_ticks != old['starttime_ticks']:
                changes.append(dict(pid=pid, previous_starttime_ticks=old['starttime_ticks'], state='REUSED'))
            elif pid not in present:
                reason = failures.get(pid, 'ABSENT' if pid not in names else 'UNAVAILABLE')
                if scan_saturated and pid not in names:reason = 'NOT_OBSERVED_SCAN_LIMIT'
                changes.append(dict(pid=pid, previous_starttime_ticks=old['starttime_ticks'], state=reason))
        present_tasks = {task_key(r):r for r in task_rows}; task_changes = []
        for key, old in old_task.items():
            current = present_tasks.get(key)
            if current is not None and current['starttime_ticks'] != old['starttime_ticks']:
                task_changes.append(dict(tgid=key[0], tid=key[2], state='REUSED', previous_starttime_ticks=old['starttime_ticks']))
            elif current is None:
                error = next((e['reason'] for e in selected_errors
                              if e['pid'] == key[0] and e.get('tid') == key[2]), None)
                state = (error if error is not None else 'PARENT_UNAVAILABLE' if key[0] not in present else
                         'NOT_OBSERVED_TID_LIMIT' if tids_saturated else
                         'UNAVAILABLE' if any(e['pid']==key[0] and e['stage']=='TASK_LIST' for e in selected_errors) else 'ABSENT')
                task_changes.append(dict(tgid=key[0], tid=key[2], state=state, previous_starttime_ticks=old['starttime_ticks']))
        wall = None if previous is None else (begin - previous['scan_started_monotonic_ns']) / 1e9
        subtotal = {}
        for label in ('union', *self.roots, 'observer'):
            rows = [r for r in process_rows if (r['observer'] if label == 'observer' else
                    not r['observer'] and (bool(r['roots']) if label == 'union' else label in r['roots']))]
            matched = [r['delta'] for r in rows if r['delta']['state'] == 'MATCHED']
            seconds = sum(d['cpu_seconds'] for d in matched)
            subtotal[label] = dict(process_count=len(rows), matched_process_count=len(matched),
                known_cpu_seconds=seconds if previous is not None else None,
                cpu_pct_one_core_common_window=None if not wall or previous is None else seconds / wall * 100)
        end = self.clock()
        result = dict(scan_started_monotonic_ns=begin, scan_ended_monotonic_ns=end,
            scan_seconds=(end - begin) / 1e9, common_wall_seconds=wall,
            enumerated_processes=len(names), stat_reads=len(table) + len(failures),
            global_pid_scan_performed=self.scope == 'tree',
            task_stat_reads=tasks_seen, selected_stat_rechecks=rechecks,
            total_stat_read_attempts=len(table) + len(failures) + tasks_seen + rechecks,
            scan_stat_failures=len(failures), roots=roots, processes=process_rows, threads=task_rows,
            process_identity_events=changes, thread_identity_events=task_changes, selected_errors=selected_errors,
            limits=dict(pid_scan=scan_saturated, descendants=descendants_saturated, tids=tids_saturated),
            coverage_flags=dict(root_unavailable=any(row['state'] != 'CURRENT' for row in roots.values()),
                selected_read_incomplete=bool(selected_errors), identity_changes=bool(changes or task_changes),
                first_or_changed_identity=any(row['delta']['state'] != 'MATCHED' for row in process_rows)),
            known_process_subtotals=subtotal)
        self.previous = result
        return result


def root_argument(value):
    label, separator, number = value.partition('=')
    if (not separator or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,23}', label)
            or label in ('union', 'observer') or not number.isascii() or not number.isdecimal()
            or len(number) > 10 or not 0 < int(number) < 2**31):
        raise argparse.ArgumentTypeError('expected unique LABEL=positive_PID; union/observer are reserved')
    return label, int(number)


def next_sample_slot(slot, start_ns, observed_ns, interval_ns):
    """지난 주기를 재생하지 않고 다음 미래 sampling slot을 고른다."""
    return max(slot + 1, (observed_ns - start_ns) // interval_ns + 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', type=root_argument, required=True, help='LABEL=PID; repeat up to8')
    parser.add_argument('--duration-s', type=float, default=10.)
    parser.add_argument('--interval-s', type=float, default=.5)
    parser.add_argument('--affinity', action='store_true', help='observe selected task hard affinity; never change it')
    parser.add_argument('--scope', choices=('tree','roots'), default='tree',
                        help='roots: fixed PIDs only, no global discovery or TIDs; child CPU is not observed')
    parser.add_argument('--output', type=Path, required=True, help='new exclusive0600 JSON path; no overwrite')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':parser.error('Linux /proc only')
    if (not math.isfinite(args.duration_s) or not .1 <= args.duration_s <= 300
            or not math.isfinite(args.interval_s) or not .05 <= args.interval_s <= 5
            or args.interval_s > args.duration_s or math.ceil(args.duration_s / args.interval_s) + 1 > MAX_SAMPLES):
        parser.error('duration.1..300/interval.05..5 with interval<=duration and <=601 scheduled samples required')
    roots = dict(args.root)
    if len(roots) != len(args.root) or len(roots) > 8 or os.getpid() in roots.values():
        parser.error('1..8 unique root labels required; probe PID cannot be a root')
    if args.output.exists() or args.output.is_symlink():parser.error('output already exists')
    hz = os.sysconf('SC_CLK_TCK'); boot_path = Path('/proc/sys/kernel/random/boot_id')
    boot = boot_path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot) or hz <= 0:
        parser.error('valid current Linux boot ID and HZ required')
    start = time.monotonic_ns(); cpu_start = time.process_time_ns(); samples = []; stored_rows = 0
    scanner = Scanner(roots, hz, affinity=args.affinity, scope=args.scope); reason = 'DURATION_COMPLETE'; slot = 0
    while True:
        sample = scanner.capture(); now = time.monotonic_ns()
        sample['scheduled_slot'] = slot
        sample['scheduled_lag_seconds'] = max(0., (sample['scan_started_monotonic_ns'] - start) / 1e9 - slot * args.interval_s)
        sample['scan_exceeded_requested_interval'] = sample['scan_seconds'] > args.interval_s
        count = sum(len(sample[name]) for name in ('processes','threads','selected_errors',
                    'process_identity_events','thread_identity_events','roots'))
        if stored_rows + count > MAX_ROWS:reason = 'TOTAL_ROW_LIMIT'; break
        samples.append(sample); stored_rows += count
        if any(sample['limits'].values()):reason = 'OBSERVATION_LIMIT'; break
        if now - start >= args.duration_s * 1e9:break
        if len(samples) >= MAX_SAMPLES:reason = 'SAMPLE_LIMIT'; break
        # 이미 지난 주기는 건너뛴다. 따라잡기 위한 연속 scan을 만들지 않는다.
        next_slot = next_sample_slot(slot, start, now, int(args.interval_s * 1e9))
        sample['skipped_periods_after_scan'] = next_slot - slot - 1; slot = next_slot
        due = start + slot * int(args.interval_s * 1e9)
        time.sleep(max(0., min(due, start + int(args.duration_s * 1e9)) - now) / 1e9)
    end = time.monotonic_ns(); cpu_end = time.process_time_ns()
    report = dict(profile='development-process-cpu-1', host_boot_id=boot, ticks_per_second=hz,
        linux_scope='dom0 Linux process accounting; not Xen guest CPU accounting',
        observer_pid=os.getpid(), roots=roots, requested_duration_s=args.duration_s,
        requested_interval_s=args.interval_s, affinity_observed=args.affinity,
        observation_scope=args.scope, descendants_observed=args.scope == 'tree',
        thread_detail_observed=args.scope == 'tree',
        started_monotonic_ns=start, ended_monotonic_ns=end, wall_seconds=(end-start)/1e9,
        observer_process_cpu_seconds=(cpu_end-cpu_start)/1e9,
        observer_overhead_scope='includes scan/assembly/sleep wake work; excludes startup and final JSON encoding/file write',
        stop_reason=reason, host_boot_unchanged=boot_path.read_text(encoding='ascii').strip() == boot,
        coverage=('fixed root PIDs only; descendant/TID CPU not observed; no cutime/cstime recovery'
                  if args.scope == 'roots' else
                  'sampled known subtotal only; births/exits/read races may lose CPU; no cutime/cstime recovery'),
        aggregation='union deduplicates process identities; per-root groups may overlap; threads are detail only',
        runtime_dependency=False, probe_time_subtracted_from_latency=False, samples=samples,
        limits=dict(max_scan_pids=MAX_SCAN_PIDS,max_processes=MAX_PROCESSES,max_tids=MAX_TIDS,
                    max_rows=MAX_ROWS,max_samples=MAX_SAMPLES,max_json_bytes=MAX_JSON_BYTES))
    raw = json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    while len(raw) + 1 > MAX_JSON_BYTES and report['samples']:
        removed = max(1, len(report['samples']) // 2)
        report['samples'] = report['samples'][:-removed]
        report['omitted_tail_samples'] = report.get('omitted_tail_samples', 0) + removed
        report['stop_reason'] = 'OUTPUT_LIMIT'
        raw = json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:stream.write(raw + b'\n')
    print(json.dumps(dict(output=str(args.output), sample_count=len(report['samples']),
                          stop_reason=report['stop_reason'], bytes=len(raw)+1)))
    return 0 if report['stop_reason'] == 'DURATION_COMPLETE' and report['host_boot_unchanged'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
