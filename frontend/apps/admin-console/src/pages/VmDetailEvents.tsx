import type { VmEvent } from '../api/types';
import { EventRow, EventRowHead } from './EventRow';
import styles from './pages.module.css';
import { shortClock } from './vmDetailEventFormat';

/**
 * VM 상세 "최근 생명주기·복구 이벤트" 표시 전용. 줄 모양·문구는 이벤트 화면과 같은 EventRow(eventLine),
 * VM 상세라 대상(VM 이름) 열은 두지 않고 시각은 밀리초 없이.
 */
export function DetailEvents({
  events,
  pending,
  error,
}: {
  events: VmEvent[];
  pending: boolean;
  error: boolean;
}) {
  return (
    <div className={styles.detailEvents}>
      {events.length > 0 && <EventRowHead />}
      {events.map((event) => (
        <EventRow key={event.event_id} event={event} time={shortClock(event.occurred_at)} />
      ))}
      {!pending && events.length === 0 && (
        <p className={styles.dim}>최근 lifecycle·장애·복구 이벤트가 없습니다.</p>
      )}
      {error && <p className={styles.error}>이벤트를 불러오지 못했습니다.</p>}
    </div>
  );
}
