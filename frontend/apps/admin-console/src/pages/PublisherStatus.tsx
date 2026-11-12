import type { Health } from '@xenova/ui';
import type { ReactNode } from 'react';
import { useTelemetryHeartbeat } from '../api/queries';
import { publisherObservation } from '../domain/telemetryPublisher';
import { useNow } from '../useNow';
import { agoWhole, heartbeatCount, lastCollectionSuccess } from './collectorStatus';
import styles from './pages.module.css';

/** 상태 점 색: 정상(초록)·주의(주황)·오류(빨강), 그 밖은 회색 */
const DOT_TONE: Record<Health, string | undefined> = {
  ok: 'ok',
  recovering: 'warning',
  fault: 'fault',
  stopped: undefined,
};

/** 상태 색 점 + 문구. 점만 색을 바꾸고 글자는 그대로 둔다 */
export function StatusDot({ health, children }: { health: Health; children: ReactNode }) {
  return (
    <span className={styles.statusDot} data-tone={DOT_TONE[health]}>
      <i aria-hidden="true" />
      {children}
    </span>
  );
}

/**
 * VM 상세 "운영 상태" 카드 아래 수집기 요약 두 줄: 상태 / heartbeat·마지막 성공·실패. 시각은 모두 상대 시간(정수).
 * Publisher diagnostics are independent from VM, v1 Agent and application liveness.
 */
export function PublisherStatus({ vmId }: { vmId: string }) {
  const query = useTelemetryHeartbeat(vmId);
  const now = useNow();
  const { report, label, tone } = publisherObservation(vmId, query);
  const received = Date.parse(report?.received_at ?? '');
  const heartbeat = Number.isFinite(received) ? agoWhole(Math.max(0, now - received)) : '—';
  const failures = heartbeatCount(report?.collection_failures_total);
  return (
    <section className={styles.collectorLine} aria-label="텔레메트리 수집기 상태">
      <div>
        <span className={styles.collectorTitle}>수집기</span>
        <StatusDot health={tone}>{label}</StatusDot>
      </div>
      <small>
        heartbeat {heartbeat} · 마지막 성공 {lastCollectionSuccess(report, now, agoWhole)} · 실패{' '}
        {/^[\d,]+$/.test(failures) ? `${failures}회` : failures}
      </small>
    </section>
  );
}
