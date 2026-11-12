import type { AgentStatus, EventSeverity, VmEvent, VmState } from '../api/types';
import { describe } from '../domain/events';
import { AGENT_LABEL, STATE_LABEL } from '../format';
import styles from './pages.module.css';
import { RECOVERY_CODE_LABEL, shortClock } from './vmDetailEventFormat';

/**
 * VM 상세 "최근 생명주기·복구 이벤트" 표시 전용. 조회(useEvents)·문구(describe)는 그대로 쓰고,
 * VM 상세라 매 줄의 VM 이름을 빼고 종류 라벨·한글 상태·사유 코드만 다듬는다.
 */

/** 이벤트 종류 라벨 (describe 의 부제 "도메인 상태"·"복구 서비스" 대신) */
const KIND: Partial<Record<VmEvent['type'], string>> = {
  'vm.state.changed': 'VM 상태',
  'vm.created': 'VM 생성',
  'vm.deleted': 'VM 삭제',
  'agent.status.changed': 'Agent 연결',
  'recovery.started': '자동 복구',
  'recovery.completed': '자동 복구',
  'recovery.recovered': '자동 복구',
  'recovery.cancelled': '자동 복구',
  'recovery.failed': '자동 복구',
  'drive.mode.changed': '주행 모드',
  'drive.resume.accepted': '주행 모드',
  'drive.resume.cancelled': '주행 모드',
  'drive.fault.detected': '장애 감지',
  'demo.command.sent': '시연 조작',
  'demo.command.completed': '시연 조작',
  'demo.command.failed': '시연 조작',
  'display.route.changed': '화면 전환',
};

const SEVERITY_LABEL: Record<EventSeverity, string> = { INFO: '정보', WARNING: '경고', ERROR: '오류' };

const str = (value: unknown) => (value == null ? '' : String(value));
const stateLabel = (value: unknown) => STATE_LABEL[str(value) as VmState] ?? str(value);
const agentLabel = (value: unknown) => AGENT_LABEL[str(value) as AgentStatus] ?? str(value);

interface EventLine {
  kind: string;
  text: string;
  /** 작은 회색 보조 문구 */
  note: string;
  /** 한글로 못 바꾼 원문 코드 (작은 회색 고정폭) */
  code: string | null;
  /** 줄 전체 설명 (title) */
  full: string;
}

function lineOf(event: VmEvent): EventLine {
  const p = event.payload ?? {};
  // describe 를 VM 이름 없이 불러 앞 공백만 정리한다
  const base = describe(event, () => '');
  const title = base.title.trim();
  const reason = str(
    event.type === 'recovery.failed' || event.type === 'recovery.cancelled'
      ? ((p.error as { code?: unknown } | null | undefined)?.code ?? p.reason)
      : p.reason,
  );
  const reasonLabel = reason ? (RECOVERY_CODE_LABEL[reason] ?? null) : null;
  const code = reason && !reasonLabel && event.type.startsWith('recovery.') ? reason : null;
  let text = title;
  let note = '';
  switch (event.type) {
    case 'vm.state.changed':
      text = `${stateLabel(p.from)} → ${stateLabel(p.to)}`;
      break;
    case 'agent.status.changed':
      text = `${agentLabel(p.from)} → ${agentLabel(p.to)}`;
      break;
    case 'recovery.started':
      text = '재시작';
      note = reasonLabel ?? '';
      break;
    case 'recovery.completed':
      text = '재시작 실행 확인';
      break;
    case 'recovery.recovered':
      text = '회복 관측';
      break;
    case 'recovery.cancelled':
      text = '복구 취소';
      note = reasonLabel ?? '';
      break;
    case 'recovery.failed':
      text = '복구 실패';
      note = reasonLabel ?? '';
      break;
    case 'drive.fault.detected':
      // 종류 라벨이 "장애 감지"라 내용에는 사유만 (예: 명령 끊김)
      text = base.detail || '장애 감지';
      break;
    case 'demo.command.sent':
      // 보조 문구 "운영자 시연 조작"은 종류 라벨 "시연 조작"과 겹친다
      break;
    default:
      note = base.detail;
  }
  const kind = KIND[event.type] ?? event.type;
  return {
    kind,
    text,
    note,
    code,
    full: [kind, text, note, code, base.detail].filter(Boolean).join(' · '),
  };
}

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
      {events.map((event) => {
        const line = lineOf(event);
        return (
          <div key={event.event_id} className={styles.detailEventRow} title={line.full}>
            <time dateTime={event.occurred_at} title={new Date(event.occurred_at).toLocaleString('ko-KR')}>
              {shortClock(event.occurred_at)}
            </time>
            <span className={styles.statusDot} data-tone={event.severity.toLowerCase()}>
              <i role="img" aria-label={`심각도 ${SEVERITY_LABEL[event.severity] ?? event.severity}`} />
              {line.kind}
            </span>
            <span className={styles.detailEventText}>
              {line.text}
              {line.note && <small>{line.note}</small>}
              {line.code && <code>{line.code}</code>}
            </span>
          </div>
        );
      })}
      {!pending && events.length === 0 && (
        <p className={styles.dim}>최근 lifecycle·장애·복구 이벤트가 없습니다.</p>
      )}
      {error && <p className={styles.error}>이벤트를 불러오지 못했습니다.</p>}
    </div>
  );
}
