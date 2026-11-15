/**
 * 앱 ID·준비 제한 사유(reason)의 한글 라벨. 모르는 값은 원문 그대로 돌려준다.
 * 지금은 VM 상세 앱 상태 타일과 Overview APP 줄에서만 쓴다 (기존 화면별 앱 맵 통합은 발표 뒤 별도 정리).
 */
const APP_LABEL: Readonly<Record<string, string>> = {
  autonomous: '자율주행',
  manual: '수동주행',
  interaction: '인터랙션',
};

/**
 * app.lifecycle NOT_READY 사유 (MQTT 계약 6.2·6.3절).
 * 화면에는 짧은 한글, 원문 코드는 title 로 남긴다.
 */
const APP_REASON_LABEL: Readonly<Record<string, string>> = {
  CONTROL_UNAPPLIED: '제어 미적용',
  INPUT_UNAVAILABLE: '입력 없음',
  INPUT_STALE: '입력 만료',
  INPUT_INVALID: '입력 이상',
  STATE_PREPARING: '상태 준비 중',
  CALCULATION_FAILED: '계산 실패',
  OUTPUT_INVALID: '출력 이상',
  APP_STOPPING: '앱 종료 중',
  APP_EXITED: '앱 스레드 종료',
  RESOURCE_RETAINED: '자원 회수 대기',
};

const lookup = (table: Readonly<Record<string, string>>, key: string) =>
  Object.hasOwn(table, key) ? table[key]! : key;

/** autonomous → 자율주행, manual → 수동주행, interaction → 인터랙션, 그 외는 원문 */
export function appLabel(appId: string): string {
  return lookup(APP_LABEL, appId);
}

/** reason 코드 → 짧은 한글. 모르는 코드는 원문 */
export function appReasonLabel(reason: string): string {
  return lookup(APP_REASON_LABEL, reason);
}
