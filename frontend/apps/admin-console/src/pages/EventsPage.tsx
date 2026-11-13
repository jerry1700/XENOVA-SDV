import { Card } from '@xenova/ui';
import { Fragment, useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useSearchParams } from 'react-router';
import { useEvents, useVms } from '../api/queries';
import type { EventSeverity, Vm, VmEvent } from '../api/types';
import { CATEGORY_LABEL, categoryOf, clock, type EventCategory } from '../domain/events';
import { MODE_LABEL, plain, roleOf, sortVms } from '../domain/roles';
import { useDrive } from '../layout/DriveContext';
import { SEVERITY_LABEL } from './eventLine';
import { EventRow, EventRowHead } from './EventRow';
import styles from './pages.module.css';

const CATEGORIES = Object.keys(CATEGORY_LABEL) as EventCategory[];
const SEVERITIES: EventSeverity[] = ['INFO', 'WARNING', 'ERROR'];

/** 날짜 구분 줄 (예: 2026. 11. 12.) — 같은 날 이벤트는 시각만 보인다 */
const dayOf = (iso: string) => new Date(iso).toLocaleDateString('ko-KR');

/** 칩 앞 색 점. tone 은 심각도(info·warning·error), color 는 VM 역할색 토큰 */
type ChipDot = { tone?: string; color?: string };

/**
 * W-08 이벤트 타임라인 (화면 정의서 W-08, UI-07). Dashboard 의 Events 탭.
 * 실시간 추가는 WS /ws/v1/events 대신 3초 재조회. 줄 모양은 VM 상세 이벤트와 같은 EventRow.
 */
export function EventsPage() {
  const { drive } = useDrive();
  const vms = useVms();
  const [category, setCategory] = useState<EventCategory | null>(null);
  // 대상 VM 필터는 URL ?vm= 와 같이 움직인다 (Overview 카드에서 바로 들어올 수 있게). 기록은 쌓지 않는다
  const [params, setParams] = useSearchParams();
  const requested = params.get('vm');
  const known = !vms.data || vms.data.items.some((vm) => vm.vm_id === requested);
  // 목록에 없는 VM ID 면 필터 없이 전체 (VM 목록을 받기 전에는 요청한 값 그대로)
  const target = requested && known ? requested : null;
  const setTarget = useCallback(
    (vmId: string | null) =>
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          if (vmId) next.set('vm', vmId);
          else next.delete('vm');
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  useEffect(() => {
    if (requested && !known) setTarget(null);
  }, [requested, known, setTarget]);
  const [severity, setSeverity] = useState<EventSeverity | null>(null);
  const events = useEvents({ vm_id: target ?? undefined, severity: severity ?? undefined, limit: 100 });

  const items = useMemo(
    () => (events.data?.items ?? []).filter((e) => !category || categoryOf(e) === category),
    [events.data, category],
  );
  const vmOf = (vmId: string | null | undefined): Vm | undefined =>
    vmId ? vms.data?.items.find((v) => v.vm_id === vmId) : undefined;
  const lastHandoff = (events.data?.items ?? []).find(
    (e) => e.type === 'drive.mode.changed' && e.payload.handoff_ms != null,
  );
  const control = drive?.control;

  const chip = <T,>(
    value: T,
    current: T | null,
    set: (v: T | null) => void,
    label: string,
    dot?: ChipDot,
  ) => (
    <button
      key={String(value)}
      type="button"
      aria-pressed={current === value}
      className={styles.eventChip}
      title={label}
      onClick={() => set(current === value ? null : value)}
    >
      {dot && (
        <i data-tone={dot.tone} style={dot.color ? ({ '--dot': dot.color } as CSSProperties) : undefined} />
      )}
      <span className={styles.eventChipLabel}>{label}</span>
    </button>
  );

  /** 대상 열: VM 이면 역할색 점 + 이름, VM 이 아니면(게이트웨이 등) 회색 */
  const targetOf = (event: VmEvent) => {
    const vmId = event.vm_id ?? (typeof event.payload?.vm_id === 'string' ? event.payload.vm_id : null);
    const vm = vmOf(vmId);
    if (!vm) return <span className={styles.dim}>{vmId ?? '게이트웨이'}</span>;
    return (
      <>
        <i style={{ '--dot': roleOf(vm, drive)?.color ?? 'var(--x-text-muted)' } as CSSProperties} />
        {vm.name}
      </>
    );
  };

  return (
    <div className={styles.page}>
      <header className={styles.contentHeading}>
        <div>
          <span className={styles.contentEyebrow}>EVENTS</span>
          <h1>이벤트 타임라인</h1>
        </div>
        <span className={styles.dim}>{events.isFetching ? '갱신 중…' : '3초마다 갱신'}</span>
      </header>

      <section className={styles.eventFilters} aria-label="이벤트 필터">
        <span>종류</span>
        <div>
          {chip(null, category, () => setCategory(null), '전체')}
          {CATEGORIES.map((c) => chip(c, category, setCategory, CATEGORY_LABEL[c]))}
        </div>
        <span>대상</span>
        <div>
          {chip(null, target, () => setTarget(null), '전체 VM')}
          {sortVms(vms.data?.items ?? []).map((vm) =>
            chip(vm.vm_id, target, setTarget, vm.name, {
              color: roleOf(vm, drive)?.color ?? 'var(--x-text-muted)',
            }),
          )}
        </div>
        <span>심각도</span>
        <div>
          {chip(null, severity, () => setSeverity(null), '전체')}
          {SEVERITIES.map((s) =>
            chip(s, severity, setSeverity, SEVERITY_LABEL[s], { tone: s.toLowerCase() }),
          )}
        </div>
      </section>

      <section className={styles.eventTiles} aria-label="주행 요약">
        <div>
          <span>현재 세대</span>
          <strong>{control?.generation ?? '—'}</strong>
          <small>{control?.mode ? MODE_LABEL[control.mode] : '모드 미확인'}</small>
        </div>
        <div>
          <span>최근 인계</span>
          <strong>
            {lastHandoff ? (
              <>
                {String(lastHandoff.payload.handoff_ms)}
                <small>ms</small>
              </>
            ) : (
              '—'
            )}
          </strong>
          <small>목표 ≤150 ms</small>
        </div>
        <div>
          <span>차량</span>
          <strong>
            {plain(drive?.vehicle?.v_mps)}
            <small>m/s</small>
          </strong>
        </div>
      </section>

      <Card title={`시간순 이벤트 (${items.length}건, 최신 위)`}>
        <div className={styles.detailEvents}>
          {items.length > 0 && <EventRowHead withTarget />}
          {items.map((event, index) => {
            const day = dayOf(event.occurred_at);
            const newDay = index === 0 || dayOf(items[index - 1]!.occurred_at) !== day;
            return (
              <Fragment key={event.event_id}>
                {newDay && <p className={styles.eventDay}>{day}</p>}
                <EventRow event={event} time={clock(event.occurred_at)} target={targetOf(event)} />
              </Fragment>
            );
          })}
        </div>
        {events.data && items.length === 0 && <p className={styles.dim}>조건에 맞는 이벤트가 없다</p>}
        {events.error && <p className={styles.error}>이벤트를 불러오지 못했다</p>}
      </Card>
    </div>
  );
}
