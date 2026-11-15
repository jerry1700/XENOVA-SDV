import { useState, type ReactNode } from 'react';
import type { VmEvent } from '../api/types';
import { eventLine, SEVERITY_LABEL } from './eventLine';
import styles from './pages.module.css';

/** 시각 전체(로캘). 읽을 수 없으면 Invalid Date 대신 */
const fullTime = (iso: string) => {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? '시각 미상' : at.toLocaleString('ko-KR');
};
/**
 * 이벤트 한 줄: 시각 | 상태 | (대상) | 내용. VM 상세 "최근 생명주기·복구 이벤트"와 이벤트 화면 공용.
 * 상태는 경고·오류만 작은 색 글자 (정보는 비움, 열 폭은 유지). 종류·보조 글(사유·세대 등)·원문 코드는
 * 줄에서 빼고 title 툴팁에, 정보를 포함한 심각도와 종류는 상태 칸의 aria-label 에 남긴다.
 * 열은 grid 로 모든 행·열 제목 줄에서 같은 세로선, 글자는 기준선(baseline) 정렬.
 * 내용은 기본 한 줄(말줄임). 행을 누르면(키보드는 내용 버튼 Enter·Space) 그 자리에서 펼쳐 전체 내용과
 * title 에만 있던 설명을 보이고, 다시 누르면 접는다 — 툴팁을 볼 수 없는 터치 기기용.
 */
export function EventRow({
  event,
  time,
  target,
}: {
  event: VmEvent;
  /** 시각 문구 (VM 상세 HH:mm:ss, 이벤트 화면 HH:mm:ss.SSS) */
  time: string;
  /** 대상 열. 주면 열이 하나 늘어난다 (이벤트 화면) */
  target?: ReactNode;
}) {
  const line = eventLine(event);
  const withTarget = target !== undefined;
  const severity = SEVERITY_LABEL[event.severity] ?? event.severity;
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((value) => !value);
  return (
    <div
      className={styles.detailEventRow}
      data-target={withTarget || undefined}
      data-open={open || undefined}
      title={line.rawType ? `${line.full} · 원문 ${line.rawType}` : line.full}
      onClick={toggle}
    >
      <time dateTime={event.occurred_at} title={fullTime(event.occurred_at)}>
        {time}
      </time>
      <span
        className={styles.eventSeverity}
        data-tone={event.severity.toLowerCase()}
        role="img"
        aria-label={`심각도 ${severity} · ${line.kind}`}
      >
        {event.severity === 'INFO' ? '' : severity}
      </span>
      {withTarget && <span className={styles.eventTarget}>{target}</span>}
      <button
        type="button"
        className={styles.detailEventText}
        aria-expanded={open}
        onClick={(click) => {
          // 행 onClick 과 두 번 바뀌지 않게
          click.stopPropagation();
          toggle();
        }}
      >
        <span className={styles.detailEventLine}>{line.text}</span>
        {open && line.full !== line.text && <small className={styles.detailEventMore}>{line.full}</small>}
      </button>
    </div>
  );
}

/** 목록 맨 위 작은 회색 열 제목 줄 (행과 같은 grid 라 같은 세로선) */
export function EventRowHead({ withTarget = false }: { withTarget?: boolean }) {
  return (
    <div
      className={styles.detailEventRow}
      data-head="true"
      data-target={withTarget || undefined}
      aria-hidden="true"
    >
      <span>시각</span>
      <span>상태</span>
      {withTarget && <span>대상</span>}
      <span>내용</span>
    </div>
  );
}
