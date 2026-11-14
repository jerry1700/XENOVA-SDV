/**
 * 이벤트 표시 규칙 (화면 정의서 W-08). 종류는 계약 이벤트에서 나눈다 — "자동 복귀·재합류·재무장" 같은 분류는 없다.
 * 자율주행 복귀는 항상 AUTO_READY → 사람의 재개 (MQTT 계약 v1.1 §5.2·§6).
 */
import type { EventType, VmEvent } from '../api/types';

export type EventCategory = 'mode' | 'fault' | 'lifecycle' | 'demo';

export const CATEGORY_LABEL: Record<EventCategory, string> = {
  mode: '모드 전환',
  fault: 'VM 장애',
  lifecycle: 'VM 수명주기',
  demo: '시연 조작',
};

const CATEGORY: Record<EventType, EventCategory> = {
  'drive.mode.changed': 'mode',
  'drive.fault.detected': 'fault',
  'drive.resume.accepted': 'mode',
  'drive.resume.cancelled': 'mode',
  'recovery.failed': 'fault',
  'vm.created': 'lifecycle',
  'vm.deleted': 'lifecycle',
  'vm.state.changed': 'lifecycle',
  'agent.status.changed': 'lifecycle',
  'recovery.started': 'lifecycle',
  'recovery.completed': 'lifecycle',
  'recovery.recovered': 'lifecycle',
  'recovery.cancelled': 'lifecycle',
  'demo.command.sent': 'demo',
  'demo.command.completed': 'demo',
  'demo.command.failed': 'demo',
  'display.route.changed': 'demo',
};

export const categoryOf = (event: VmEvent): EventCategory => CATEGORY[event.type] ?? 'lifecycle';

/** 화면이 모르는 이벤트 종류(백엔드에 새로 생긴 type)의 표시 이름. 원문 type 은 title 로만 남긴다 */
export const UNKNOWN_EVENT_LABEL = '기타 이벤트';

/** 화면이 아는 이벤트 종류인가 (모르면 UNKNOWN_EVENT_LABEL 로 보이고 원문은 title 로) */
export const isKnownEventType = (type: string): type is EventType => Object.hasOwn(CATEGORY, type);

/** 화면 title 용 원문 type — 아는 종류면 null */
export const unknownEventType = (event: VmEvent): string | null =>
  isKnownEventType(event.type) ? null : String(event.type);

const REASON: Record<string, string> = {
  start: '시작',
  command_timeout: '명령 끊김',
  resume_requested: '재개 요청',
  no_fresh_standby: '넘길 앱 없음',
  autonomous_recovered: '자율주행 3초 연속 정상',
  autonomous_lost: '자율주행이 다시 끊김',
  manual_lost: '수동주행 끊김',
  not_auto_ready: 'AUTO_READY 아님',
  operator_cancelled: '운영자 취소',
  readiness_lost: '준비 상태 깨짐',
};

/**
 * payload 값을 한 줄 글자로. 글자·유한한 숫자·참거짓만 보이고, 객체·배열·NaN 처럼 글자로 바꾸면
 * `[object Object]`·`NaN` 이 되는 값은 비운다 (S15P31E110-469)
 */
export function payloadText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'boolean') return String(value);
  return '';
}

/** payload 의 유한한 숫자만, 아니면 null */
export function payloadNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const str = payloadText;

/** 한 줄 제목과 보조 설명 */
export function describe(
  event: VmEvent,
  vmName: (vmId: string | null) => string,
): { title: string; detail: string } {
  const p = event.payload ?? {};
  switch (event.type) {
    case 'drive.mode.changed':
      return {
        title: `${str(p.from) || '—'} → ${str(p.to)}`,
        detail: [
          REASON[str(p.reason)] ?? str(p.reason),
          str(p.generation) && `gen ${str(p.generation)}`,
          payloadNumber(p.handoff_ms) != null && `인계 ${str(p.handoff_ms)} ms`,
        ]
          .filter(Boolean)
          .join(' · '),
      };
    case 'drive.fault.detected':
      return {
        title: `${vmName(str(p.vm_id) || event.vm_id)} 장애 감지`,
        detail: REASON[str(p.reason)] ?? str(p.reason),
      };
    case 'drive.resume.accepted':
      return {
        title: `재개 승인 — ${(payloadNumber(p.switch_in_ms) ?? 3000) / 1000}초 뒤 AUTO`,
        detail: [
          str(p.requested_by) && `요청 ${str(p.requested_by)}`,
          str(p.generation) && `gen ${str(p.generation)}`,
        ]
          .filter(Boolean)
          .join(' · '),
      };
    case 'drive.resume.cancelled':
      return { title: '재개 취소', detail: REASON[str(p.reason)] ?? str(p.reason) };
    case 'vm.state.changed':
      return { title: `${vmName(event.vm_id)} ${str(p.from)} → ${str(p.to)}`, detail: '도메인 상태' };
    case 'recovery.started':
      return { title: `${vmName(event.vm_id)} 재시작`, detail: '복구 서비스' };
    case 'recovery.completed':
      return {
        title: `${vmName(event.vm_id)} 재시작 실행 확인`,
        detail: '새 Xen 실행 관측 · 앱 준비·주행권은 별도 확인',
      };
    case 'recovery.recovered':
      return {
        title: `${vmName(event.vm_id)} 회복 관측`,
        detail: '복구 시도 외 경로에서 회복 · 앱 준비·주행권은 별도 확인',
      };
    case 'recovery.cancelled':
      return {
        title: `${vmName(event.vm_id)} 복구 취소`,
        detail: '정지·삭제·의도 변경 등으로 복구 대상에서 제외',
      };
    case 'recovery.failed':
      return { title: `${vmName(event.vm_id)} 복구 실패`, detail: str(p.reason) };
    case 'demo.command.sent':
      return {
        title: `${vmName(event.vm_id)} ${str(p.command) === 'force_kill' ? '강제 종료' : str(p.command)}`,
        detail: '운영자 시연 조작',
      };
    case 'agent.status.changed':
      return {
        title: `${vmName(event.vm_id)} 에이전트 ${str(p.from)} → ${str(p.to)}`,
        detail: 'heartbeat 5초 주기',
      };
    case 'vm.created':
      return { title: `${vmName(event.vm_id)} 생성`, detail: 'VM 등록' };
    case 'vm.deleted':
      return { title: `${vmName(event.vm_id)} 삭제`, detail: 'VM 등록 해제' };
    case 'demo.command.completed':
      return { title: `${vmName(event.vm_id)} 시연 명령 완료`, detail: '운영자 시연 조작' };
    case 'demo.command.failed':
      return { title: `${vmName(event.vm_id)} 시연 명령 실패`, detail: '운영자 시연 조작' };
    case 'display.route.changed':
      return { title: `${vmName(event.vm_id)} 화면 배치 변경`, detail: '모니터 화면 배치' };
    default:
      // 모르는 종류: 원문 type(영문)은 화면 문구에 넣지 않는다 — 부르는 쪽이 unknownEventType 으로 title 에
      return { title: `${vmName(event.vm_id)} ${UNKNOWN_EVENT_LABEL}`, detail: '' };
  }
}

/** 14:31:20.112 (화면 시계 기준, 계약 §1) — 로캘마다 모양이 달라지지 않게 직접 만든다 */
export const clock = (iso: string) => {
  const d = new Date(iso);
  // 읽을 수 없는 시각은 NaN:NaN:NaN 대신
  if (Number.isNaN(d.getTime())) return '—';
  const two = (n: number) => String(n).padStart(2, '0');
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, '0')}`;
};
