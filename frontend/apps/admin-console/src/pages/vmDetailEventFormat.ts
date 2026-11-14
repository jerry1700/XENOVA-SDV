/** VM 상세 이벤트 표시용 상수·형식 (컴포넌트 파일과 분리) */

/**
 * 복구 판정 사유·결과 코드 → 한글 (VM API 계약 2.5 reason, error.code). domain 에 같은 매핑이 없어 VM 상세 전용으로 둔다.
 * 여기에 없는 코드는 작은 회색 고정폭 원문 그대로
 */
export const RECOVERY_CODE_LABEL: Record<string, string> = {
  HEARTBEAT_LOST: 'Agent 신호 끊김',
  APP_HEARTBEAT_LOST: '앱 신호 끊김',
  VM_UNTRUSTED: '출력 신뢰 철회',
  DOMAIN_ABSENT: '도메인 없음',
  DOMAIN_CRASHED: '도메인 비정상 종료',
  TARGET_NOT_ELIGIBLE: '복구 대상 아님',
  REOBSERVE_TIMEOUT: '재관측 시간 초과',
  RECONCILE_REQUIRED: '수동 정리 필요',
  INTERNAL_ERROR: '내부 오류',
};

const two = (n: number) => String(n).padStart(2, '0');
/** HH:mm:ss (화면 시계 기준, 밀리초 없이) */
export const shortClock = (iso: string) => {
  const d = new Date(iso);
  // 읽을 수 없는 시각은 NaN:NaN:NaN 대신 (domain/events clock 과 같은 규칙)
  if (Number.isNaN(d.getTime())) return '—';
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
};
