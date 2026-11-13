import { ApiError } from './client';
import type { CommandState, KnownErrorCode } from './types';

/**
 * 화면에 보일 오류 문구 한 곳 (프론트 QA 1-F12·3-A6).
 * 서버 message 는 보드 영어 원문(`domain … is running; stop it before delete`)이 섞여 오므로 쓰지 않는다.
 * 문구에 `(CODE)` 를 붙이지 않는다 — 코드가 필요하면 요소의 title 로.
 * KnownErrorCode 가 늘면 typecheck 가 빠진 문구를 잡는다.
 */
export const ERROR_TEXT = {
  UNAUTHORIZED: '로그인이 필요합니다. 다시 로그인해 주세요.',
  INVALID_TOKEN: '로그인 정보가 올바르지 않습니다. 다시 로그인해 주세요.',
  TOKEN_REVOKED: '로그인이 만료되었습니다. 다시 로그인해 주세요.',
  INVALID_CREDENTIALS: '아이디 또는 비밀번호가 올바르지 않습니다.',
  INVALID_REFRESH_TOKEN: '로그인이 만료되었습니다. 다시 로그인해 주세요.',
  LOGIN_RATE_LIMITED: '로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.',
  VALIDATION_ERROR: '입력한 값을 다시 확인해 주세요.',
  TEMPLATE_NOT_FOUND: '선택한 템플릿을 찾을 수 없습니다. 템플릿을 다시 선택해 주세요.',
  VM_NOT_FOUND: 'VM을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.',
  VM_NAME_CONFLICT: '같은 이름의 VM이 있습니다. 다른 이름을 입력해 주세요.',
  INVALID_VM_STATE: '지금 VM 상태에서는 할 수 없는 작업입니다. VM 상태를 확인해 주세요.',
  VM_ALREADY_EXISTS: '이미 같은 VM이 있습니다.',
  INVALID_ARGUMENT: '보드가 요청 값을 받아들이지 않았습니다. 템플릿과 자원 값을 확인해 주세요.',
  INSUFFICIENT_RESOURCES: '사용 가능한 CPU 또는 메모리가 부족합니다. 할당량을 조정해 주세요.',
  VM_DRIVER_ERROR: '보드에서 VM 작업을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.',
  VM_CONTROL_NOT_SUPPORTED: '이 VM에서는 지원하지 않는 작업입니다.',
  VM_CONTROL_UNAVAILABLE: '보드에 연결할 수 없습니다. 보드 상태를 확인해 주세요.',
  VM_CONTROL_TIMEOUT: '보드 응답이 늦어 결과를 확인하지 못했습니다. 잠시 후 VM 목록에서 확인해 주세요.',
  VM_CONTROL_BAD_RESPONSE: '보드 응답을 해석하지 못했습니다. 잠시 후 다시 시도해 주세요.',
  RESOURCE_UNSUPPORTED: '이 VM에서는 자원을 바꿀 수 없습니다.',
  INTERNAL_SERVER_ERROR: '서버에서 오류가 났습니다. 잠시 후 다시 시도해 주세요.',
} as const satisfies Record<KnownErrorCode, string>;

const DEFAULT_TEXT = '요청을 처리하지 못했습니다.';
const NETWORK_TEXT = '서버에 연결할 수 없습니다. 네트워크와 서버 상태를 확인해 주세요.';

const isKnown = (code: string): code is KnownErrorCode => Object.hasOwn(ERROR_TEXT, code);

/** ApiError 면 코드별 한글 문구, 서버에 닿지 못했으면 연결 문구, 그 밖(모르는 코드·일반 예외)은 fallback 또는 기본 문구 */
export function errorText(error: unknown, fallback: string = DEFAULT_TEXT): string {
  if (error instanceof ApiError) {
    if (isKnown(error.code)) return ERROR_TEXT[error.code];
    if (error.status === 0) return NETWORK_TEXT;
  }
  return fallback;
}

const COMMAND_STATE_TEXT: Record<CommandState, string> = {
  PENDING: '대기',
  ACCEPTED: '접수',
  RUNNING: '실행 중',
  SUCCEEDED: '완료',
  FAILED: '실패',
  REJECTED: '거절',
};

export function commandStateText(state: CommandState): string {
  return COMMAND_STATE_TEXT[state];
}
