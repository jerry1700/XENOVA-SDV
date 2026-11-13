import type { CommandStatusResponse } from './types';

/**
 * 명령 상태 응답의 실패·거절 코드 (Agent error.code). error 가 없거나 형식이 다르면 null —
 * 백엔드는 Agent 가 보낸 JSON 을 그대로 넘기므로 문자열 code 만 믿는다
 */
export function commandErrorCode(
  status: Pick<CommandStatusResponse, 'error'> | null | undefined,
): string | null {
  const error: unknown = status?.error;
  if (typeof error !== 'object' || error === null || Array.isArray(error)) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' && code.trim() ? code.trim() : null;
}
