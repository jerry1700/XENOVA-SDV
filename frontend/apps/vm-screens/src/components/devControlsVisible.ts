/**
 * 개발용 아래 띠(DevControls)를 보일지 (S15P31E110-470).
 * - ?kiosk 가 있으면 언제나 숨긴다 (키오스크가 우선)
 * - 개발 모드에서는 보인다
 * - 운영 빌드에서는 숨기고, 점검할 때만 ?dev=1 로 연다
 */
export function devControlsVisible(search: string, prod: boolean = import.meta.env.PROD): boolean {
  const query = new URLSearchParams(search);
  if (query.has('kiosk')) return false;
  return !prod || query.get('dev') === '1';
}
