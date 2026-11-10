import { useState } from 'react';
import { useMetrics, useProcesses, useThreads } from '../api/queries';
import type { MetricSample, ProcessInfo, ThreadsResponse, Vm } from '../api/types';
import { buildCpuChartScale } from '../domain/cpuChart';
import { poolLevel } from '../domain/memoryPools';
import { networkRxKiBPerSecond } from '../domain/networkRate';
import { sharedMemoryObservation } from '../domain/sharedMemoryObservation';
import {
  buildZephyrHeapSeries,
  cpuPercentOf,
  LINUX_MEMORY_USED_METRIC,
  metricValue,
} from '../domain/vmMetrics';
import { useNow } from '../useNow';
import { RxTxChart } from './resourceCharts';
import { formatKiB, type ChartPoint } from './resourceFormat';
import styles from './pages.module.css';

const MIB = 1024 * 1024;
const ZEPHYR_RX_RATE_METRIC = 'rx_bytes_per_s';
/** 프로세스·스레드 표에 처음 보이는 행 수. 나머지는 "N개 더 보기" */
const VISIBLE_ROWS = 10;

const byTime = (a: MetricSample, b: MetricSample) => Date.parse(a.observed_at) - Date.parse(b.observed_at);
const point = (observedAt: string, value: number | null): ChartPoint => ({ observedAt, rx: value, tx: null });

/**
 * 네트워크 수신 값(networkRxKiBPerSecond)에 시각을 붙인다. Zephyr 측정값(rx_bytes_per_s)은 표본과 1:1 이라 그 표본 시각을,
 * 카운터 차분은 중복 보고를 건너뛰어 개수가 달라질 수 있으므로 첫·마지막 카운터 표본 사이에 고르게 놓는다
 */
function networkPoints(samples: readonly MetricSample[], kind: Vm['guest_kind']): ChartPoint[] {
  const ordered = [...samples].sort(byTime);
  const values = networkRxKiBPerSecond(ordered, kind);
  const direct = ordered.filter((sample) =>
    sample.values.some((value) => value.name === ZEPHYR_RX_RATE_METRIC),
  );
  if (kind === 'zephyr' && direct.length === values.length)
    return values.map((value, index) => point(direct[index]!.observed_at, value));
  const counters = ordered.filter((sample) =>
    sample.values.some((value) => value.name === 'net_rx_bytes_total'),
  );
  if (!counters.length || !values.length) return [];
  const start = Date.parse(counters[0]!.observed_at);
  const end = Date.parse(counters.at(-1)!.observed_at);
  return values.map((value, index) =>
    point(new Date(start + ((end - start) * index) / Math.max(1, values.length - 1)).toISOString(), value),
  );
}

/** VM 상세 "CPU·메모리 추이 · 최근 5분" 카드 내용: CPU · 메모리(Linux)/커널 힙(Zephyr) · 네트워크 수신 */
export function DetailCharts({ vm }: { vm: Vm }) {
  const metrics = useMetrics(vm.vm_id, true);
  const now = useNow();
  const samples = metrics.data?.items ?? [];
  if (metrics.isPending) return <p className={styles.dim}>불러오는 중…</p>;
  if (metrics.isError && samples.length === 0)
    return (
      <p role="alert" className={styles.error}>
        메트릭을 불러오지 못했습니다.
      </p>
    );
  if (samples.length === 0) return <p className={styles.dim}>최근 5분 동안 수신된 메트릭이 없습니다.</p>;
  const ordered = [...samples].sort(byTime);
  // Zephyr 는 family 별로 표본이 나뉘므로 CPU 값이 있는 표본만 쓴다 (CPU 탭과 같은 기준)
  const cpu = ordered.flatMap((sample) => {
    const value = cpuPercentOf(sample);
    return value == null ? [] : [point(sample.observed_at, value)];
  });
  const rate = networkPoints(ordered, vm.guest_kind);
  return (
    <>
      {metrics.isError && (
        <p role="alert" className={styles.error}>
          메트릭 갱신에 실패했습니다.
        </p>
      )}
      <div className={styles.detailCharts}>
        <DetailChart title="CPU" unit="%" points={cpu} current={cpu.at(-1)?.rx ?? null} percent />
        {vm.guest_kind === 'zephyr' ? (
          <KernelHeapChart samples={ordered} now={now} />
        ) : (
          <LinuxMemoryChart samples={ordered} />
        )}
        <DetailChart title="네트워크 수신" unit="KiB/s" points={rate} current={rate.at(-1)?.rx ?? null} />
      </div>
    </>
  );
}

/** 커널 힙: 현재 값은 Memory 탭과 같은 현재값(관측 만료 규칙)의 용량 대비 %, 옆에 작게 사용 / 전체 · 여유 */
function KernelHeapChart({ samples, now }: { samples: MetricSample[]; now: number }) {
  const series = buildZephyrHeapSeries(samples);
  const observation = sharedMemoryObservation(
    samples,
    'memory',
    ['memory.kernel_heap.used_bytes', 'memory.kernel_heap.free_bytes'],
    now,
  );
  const view =
    (observation.state === 'fresh' || observation.state === 'expired') && observation.sample
      ? buildZephyrHeapSeries([observation.sample]).current
      : null;
  const expired = observation.state === 'expired' && view != null;
  const note = view
    ? `${formatKiB(view.usedBytes)} / ${formatKiB(view.totalBytes)} · ${expired ? '관측 당시 여유' : '여유'} ${formatKiB(view.freeBytes)}${expired ? ' · 관측 만료' : ''}`
    : (series.reason ?? observation.reason);
  return (
    <DetailChart
      title="커널 힙"
      unit="%"
      points={series.points.map((p) => point(p.observedAt, p.percent))}
      current={view?.percent ?? null}
      percent
      note={note}
      warn={expired}
    />
  );
}

function LinuxMemoryChart({ samples }: { samples: MetricSample[] }) {
  const points = samples.flatMap((sample) => {
    const value = metricValue(sample, LINUX_MEMORY_USED_METRIC);
    return value == null ? [] : [point(sample.observed_at, value / MIB)];
  });
  return <DetailChart title="메모리" unit="MiB" points={points} current={points.at(-1)?.rx ?? null} />;
}

/**
 * Network 탭 그래프(RxTxChart)를 선 하나로 쓴다: 가로 눈금선, 맨 위 눈금선 왼쪽 축 최대값, 아래 "5분 전 / 현재".
 * 머리 한 줄 = 이름 + 현재 값(+ 보조 문구). % 값은 CPU 탭과 같은 자동 축(buildCpuChartScale), 그 밖은 Network 탭 자동 축
 */
function DetailChart({
  title,
  unit,
  points,
  current,
  percent = false,
  note,
  warn = false,
}: {
  title: string;
  unit: string;
  points: ChartPoint[];
  current: number | null;
  percent?: boolean;
  note?: string | null;
  warn?: boolean;
}) {
  const values = points.map((p) => p.rx).filter((value): value is number => value != null);
  const max = percent ? buildCpuChartScale(values).max : undefined;
  return (
    <RxTxChart
      title={title}
      unit={unit}
      ariaLabel={`${title} 최근 5분`}
      points={points}
      max={max}
      span="5분"
      rxOnly
      formatMax={(value) => `${value.toFixed(0)} ${unit}`}
      formatValue={(value) => `${value.toFixed(1)} ${unit}`}
      summary={
        <span className={styles.detailChartSummary}>
          <b data-mono="true">{current == null ? '—' : current.toFixed(1)}</b>
          {current != null && <small>{unit}</small>}
          {note && (
            <small className={styles.detailChartNote} data-warn={warn || undefined} title={note}>
              {note}
            </small>
          )}
        </span>
      }
    />
  );
}

/** Linux 는 프로세스 표, Zephyr 는 스레드 표 */
export function ExecutionTable({ vm }: { vm: Vm }) {
  return vm.guest_kind === 'zephyr' ? <Threads vmId={vm.vm_id} /> : <Processes vmId={vm.vm_id} />;
}

/** CPU 높은 순(값 없음은 뒤), 같으면 받은 순서 그대로 */
function byCpu<T extends { cpu_percent: number | null }>(items: readonly T[]): T[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => (b.item.cpu_percent ?? -1) - (a.item.cpu_percent ?? -1) || a.index - b.index)
    .map(({ item }) => item);
}

/** 표 아래 "N개 더 보기" / "접기" */
function useVisibleRows<T>(rows: T[]) {
  const [expanded, setExpanded] = useState(false);
  const hidden = Math.max(0, rows.length - VISIBLE_ROWS);
  return {
    rows: expanded ? rows : rows.slice(0, VISIBLE_ROWS),
    toggle:
      hidden > 0 ? (
        <button type="button" className={styles.detailTableMore} onClick={() => setExpanded((open) => !open)}>
          {expanded ? '접기' : `${hidden}개 더 보기`}
        </button>
      ) : null,
  };
}

/**
 * Zephyr idle 스레드: 커널이 붙이는 이름 "idle" (SMP 빌드는 CPU 번호를 붙인 "idle 00" 형식).
 * 텔레메트리는 k_thread_foreach 목록의 k_thread_name_get 이름을 그대로 보내므로 이 이름으로 구분한다
 */
const IDLE_THREAD_NAME = /^idle( \d+)?$/;

/** CPU % 숫자(왼쪽 정렬, 열 제목과 시작점 같음) + 바로 뒤 짧은 막대(0~100%, Memory 탭 막대 행과 같은 색·두께·80/95% 규칙) */
function CpuCells({ value, reason }: { value: number | null; reason?: string | null }) {
  const bounded = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <>
      <td data-num="true" data-cpu="true" title={reason ?? undefined}>
        {value == null ? '—' : value.toFixed(1)}
      </td>
      <td className={styles.detailBarCell}>
        <span className={styles.memRowBar} data-level={poolLevel(value)} aria-hidden="true">
          <i style={{ width: `${bounded}%` }} />
        </span>
      </td>
    </>
  );
}

/** 막대 열 제목은 비워 둔다 (숫자 열 "CPU %" 가 이 값을 설명) */
const BAR_HEADER = <th className={styles.detailBarCell} aria-hidden="true" />;

function Processes({ vmId }: { vmId: string }) {
  const processes = useProcesses(vmId, true);
  const items = byCpu<ProcessInfo>(processes.data?.items ?? []);
  const visible = useVisibleRows(items);
  if (processes.isPending) return <p className={styles.dim}>불러오는 중…</p>;
  if (processes.isError)
    return (
      <p role="alert" className={styles.error}>
        프로세스 정보를 불러오지 못했습니다.
      </p>
    );
  const collection = processes.data?.process_collection;
  const collectionLabel = collection
    ? {
        first_sample: '첫 표본 · CPU 계측 구간 대기',
        ok: '수집 완료',
        partial: `일부 수집 · 읽기 실패 ${collection.failed}건`,
        read_failed: `프로세스 목록 읽기 실패${collection.error ? ` · ${collection.error}` : ''}`,
        disabled: '프로세스 수집 비활성',
      }[collection.status]
    : null;
  return (
    <>
      <p className={styles.detailTableStatus}>
        {processes.data?.stale && processes.data.observed_at && (
          <span data-warn="true">마지막 프로세스 보고가 오래되었습니다.</span>
        )}
        {/* 수집 상태는 정상(수집 완료)이 아닐 때만. 일부 수집·읽기 실패·잘림은 주의 색 */}
        {collection && collection.status !== 'ok' && collectionLabel && (
          <span
            data-warn={collection.status === 'partial' || collection.status === 'read_failed' || undefined}
          >
            {collectionLabel}
          </span>
        )}
        {collection?.truncated && <span data-warn="true">상위 목록 일부만 보고됨</span>}
        <span>CPU 높은 순 · {items.length}개</span>
      </p>
      <table className={styles.detailTable} data-kind="process">
        <colgroup>
          <col className={styles.detailNameCol} />
          <col className={styles.detailIdCol} />
          <col className={styles.detailValueCol} />
          <col className={styles.detailBarCol} />
          <col className={styles.detailRssCol} />
        </colgroup>
        <thead>
          <tr>
            <th>이름</th>
            <th data-id="true">PID</th>
            <th data-num="true" data-cpu="true">
              CPU %
            </th>
            {BAR_HEADER}
            <th data-num="true">메모리 (RSS)</th>
          </tr>
        </thead>
        <tbody>
          {visible.rows.map((p) => (
            <tr key={p.pid}>
              <td title={p.name}>{p.name}</td>
              <td data-id="true">{p.pid}</td>
              <CpuCells value={p.cpu_percent} />
              <td data-num="true">{p.rss_bytes == null ? '—' : `${(p.rss_bytes / MIB).toFixed(1)} MiB`}</td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td colSpan={5} className={styles.dim}>
                {collection?.status === 'ok'
                  ? '이 표본에 표시할 프로세스 없음'
                  : (collectionLabel ?? '프로세스 보고 없음')}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {visible.toggle}
    </>
  );
}

function Threads({ vmId }: { vmId: string }) {
  const query = useThreads(vmId);
  const report = query.data;
  const all = report?.vm_id === vmId ? report.items : [];
  // idle 은 CPU 사용 목록·개수·더 보기에서 뺀다 (유휴는 CPU 추이 그래프로 본다)
  const items = byCpu<ThreadsResponse['items'][number]>(
    all.filter((thread) => !IDLE_THREAD_NAME.test(thread.thread_name)),
  );
  const visible = useVisibleRows(items);
  if (query.isPending) return <p className={styles.dim}>스레드 정보를 불러오는 중…</p>;
  if (query.isError)
    return (
      <p role="alert" className={styles.error}>
        스레드 정보를 불러오지 못했습니다.
      </p>
    );
  if (!report?.received_at || report.vm_id !== vmId) return <p className={styles.dim}>스레드 보고 없음</p>;
  return (
    <>
      <p className={styles.detailTableStatus}>
        {report.stale && <span data-warn="true">마지막 스레드 보고가 오래되었습니다.</span>}
        {/* 이상할 때만 주의 색으로: 페이지 충돌·누락, 수집 잘림 등 */}
        {report.conflicting_pages ? (
          <span data-warn="true">페이지 충돌</span>
        ) : (
          !report.complete && (
            <span data-warn="true">
              페이지 {report.parts_received}/{report.part_count ?? '—'} 수신
            </span>
          )
        )}
        {report.threads_status && report.threads_status !== 'ok' && (
          <span data-warn="true">
            {report.threads_status === 'truncated' ? '일부 스레드만 수집됨' : report.threads_status}
          </span>
        )}
        <span>CPU 높은 순 · {items.length}개</span>
      </p>
      <table className={styles.detailTable} data-kind="thread">
        <colgroup>
          <col className={styles.detailNameCol} />
          <col className={styles.detailIdCol} />
          <col className={styles.detailValueCol} />
          <col className={styles.detailBarCol} />
        </colgroup>
        <thead>
          <tr>
            <th>이름</th>
            <th data-id="true">ID</th>
            <th data-num="true" data-cpu="true">
              CPU %
            </th>
            {BAR_HEADER}
          </tr>
        </thead>
        <tbody>
          {visible.rows.map((thread) => (
            <tr key={thread.thread_id}>
              <td title={`${thread.thread_name || '—'}${thread.name_truncated ? ' (잘림)' : ''}`}>
                {thread.thread_name || '—'}
                {thread.name_truncated ? ' (잘림)' : ''}
              </td>
              <td data-id="true">{thread.thread_id}</td>
              <CpuCells value={thread.cpu_percent} reason={thread.unavailable_reason} />
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td colSpan={4} className={styles.dim}>
                이 표본에 표시할 스레드 없음
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {visible.toggle}
    </>
  );
}
