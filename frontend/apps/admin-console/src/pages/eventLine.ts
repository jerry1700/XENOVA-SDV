import type { DriveMode } from '../api/drive';
import type { AgentStatus, EventSeverity, VmEvent, VmState } from '../api/types';
import { describe } from '../domain/events';
import { MODE_LABEL } from '../domain/roles';
import { AGENT_LABEL, STATE_LABEL } from '../format';
import { RECOVERY_CODE_LABEL } from './vmDetailEventFormat';

/**
 * 이벤트 한 줄 표시 문구 (VM 상세 "최근 생명주기·복구 이벤트"와 이벤트 화면 공용).
 * 조회(useEvents)·문구(describe)는 그대로 쓰고, VM 이름은 빼고(대상 열·VM 상세 제목이 대신) 상태 값은 한글로.
 */

/** 이벤트 종류 라벨 (describe 의 부제 "도메인 상태"·"복구 서비스" 대신) */
export const EVENT_KIND: Partial<Record<VmEvent['type'], string>> = {
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

export const SEVERITY_LABEL: Record<EventSeverity, string> = { INFO: '정보', WARNING: '경고', ERROR: '오류' };

const str = (value: unknown) => (value == null ? '' : String(value));
const stateLabel = (value: unknown) => STATE_LABEL[str(value) as VmState] ?? str(value);
const agentLabel = (value: unknown) => AGENT_LABEL[str(value) as AgentStatus] ?? (str(value) || '—');
const modeLabel = (value: unknown) => MODE_LABEL[str(value) as DriveMode] ?? (str(value) || '—');

export interface EventLine {
  /** 세부 종류 라벨 (VM 상태·자동 복구 …) */
  kind: string;
  /** 한글 내용 */
  text: string;
  /** 작은 회색 보조 문구 */
  note: string;
  /** 한글로 못 바꾼 원문 코드 (작은 회색 고정폭) */
  code: string | null;
  /** 줄 전체 설명 (title). 한글로 바꾼 상태 값의 원문을 포함한다 */
  full: string;
}

export function eventLine(event: VmEvent): EventLine {
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
  // 한글로 바꾼 값의 원문 (title 에만)
  let raw = '';
  switch (event.type) {
    case 'vm.state.changed':
      text = `${stateLabel(p.from)} → ${stateLabel(p.to)}`;
      raw = `${str(p.from)} → ${str(p.to)}`;
      break;
    case 'agent.status.changed':
      text = `${agentLabel(p.from)} → ${agentLabel(p.to)}`;
      raw = `${str(p.from)} → ${str(p.to)}`;
      break;
    case 'drive.mode.changed':
      text = `${modeLabel(p.from)} → ${modeLabel(p.to)}`;
      raw = `${str(p.from) || '—'} → ${str(p.to)}`;
      note = base.detail;
      break;
    case 'drive.resume.accepted':
      text = `재개 승인 — ${Number(p.switch_in_ms ?? 3000) / 1000}초 뒤 ${MODE_LABEL.AUTO}`;
      raw = title;
      note = base.detail;
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
  const kind = EVENT_KIND[event.type] ?? event.type;
  return {
    kind,
    text,
    note,
    code,
    full: [kind, text, note, code, raw && `원문 ${raw}`, base.detail !== note && base.detail]
      .filter(Boolean)
      .join(' · '),
  };
}
