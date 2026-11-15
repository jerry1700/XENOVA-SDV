import { describe, expect, it } from 'vitest';
import { ApiError } from './client';
import { AGENT_ERROR_TEXT, agentErrorText, commandStateText, ERROR_TEXT, errorText } from './errorText';
import type { AgentErrorCode, CommandState, KnownErrorCode } from './types';

const DEFAULT = '요청을 처리하지 못했습니다.';

describe('errorText — 화면에 보일 오류 문구', () => {
  it('알려진 코드는 전부 한글 문구이고, 코드 이름·괄호를 붙이지 않는다', () => {
    for (const [code, text] of Object.entries(ERROR_TEXT) as [KnownErrorCode, string][]) {
      expect(text, code).toMatch(/[가-힣]/);
      expect(text, code).not.toContain(code);
      expect(text, code).not.toMatch(/[()]/);
      expect(
        errorText(new ApiError(409, { code, message: 'domain x is running; stop it before delete' })),
      ).toBe(text);
    }
  });

  it('같은 VM 에 다른 작업이 진행 중이면 다시 시도 안내 (QA 1-B2)', () => {
    expect(errorText(new ApiError(409, { code: 'VM_OPERATION_IN_PROGRESS', message: 'x' }))).toBe(
      '이 VM에 다른 작업이 진행 중입니다. 끝난 뒤 다시 시도해 주세요.',
    );
  });

  it('서버 원문은 쓰지 않는다 (보드 영어 문구가 그대로 나가던 문제)', () => {
    const error = new ApiError(409, {
      code: 'INVALID_VM_STATE',
      message: 'domain abc is running; stop it before delete',
    });
    expect(errorText(error)).not.toMatch(/running|stop it/);
  });

  it('모르는 코드·일반 예외·값은 기본 문구, fallback 을 주면 그것', () => {
    expect(errorText(new ApiError(418, { code: 'SOMETHING_NEW', message: 'teapot' }))).toBe(DEFAULT);
    expect(errorText(new Error('TypeError: x is undefined'))).toBe(DEFAULT);
    expect(errorText('oops')).toBe(DEFAULT);
    expect(errorText(undefined)).toBe(DEFAULT);
    expect(errorText(new ApiError(418, { code: 'SOMETHING_NEW' }), 'VM 동기화에 실패했습니다.')).toBe(
      'VM 동기화에 실패했습니다.',
    );
  });

  it('알려진 코드면 fallback 보다 표의 문구가 우선', () => {
    expect(errorText(new ApiError(409, { code: 'VM_NAME_CONFLICT' }), '실패')).toBe(
      ERROR_TEXT.VM_NAME_CONFLICT,
    );
  });

  it('서버에 닿지 못하면(status 0) 연결 문구', () => {
    expect(errorText(new ApiError(0, { message: '서버에 연결할 수 없습니다.' }))).toBe(
      '서버에 연결할 수 없습니다. 네트워크와 서버 상태를 확인해 주세요.',
    );
  });

  it('VmCreatePage 에 있던 안내 문구를 그대로 옮겼다', () => {
    expect(ERROR_TEXT.VM_NAME_CONFLICT).toBe('같은 이름의 VM이 있습니다. 다른 이름을 입력해 주세요.');
    expect(ERROR_TEXT.INSUFFICIENT_RESOURCES).toBe(
      '사용 가능한 CPU 또는 메모리가 부족합니다. 할당량을 조정해 주세요.',
    );
    expect(ERROR_TEXT.TEMPLATE_NOT_FOUND).toBe(
      '선택한 템플릿을 찾을 수 없습니다. 템플릿을 다시 선택해 주세요.',
    );
  });
});

describe('백엔드 새 코드·낡은 로그인 문구 (S15P31E110-471)', () => {
  it.each<KnownErrorCode>([
    'NOT_FOUND',
    'OPERATION_NOT_FOUND',
    'RESOURCE_UPDATE_UNSUPPORTED',
    'CPU_PLACEMENT_CONFLICT',
    'CPU_POLICY_NOT_CONFIGURED',
    'EVENT_CURSOR_EXPIRED',
    'TIME_RANGE_INVALID',
    'SIM_UNAVAILABLE',
    'PHONE_RELAY_NOT_LISTENING',
    'PHONE_LINK_UNAVAILABLE',
    'LOG_SOURCE_UNAVAILABLE',
    'PHONE_NOT_CONNECTED',
    'DISPLAY_MODE_BUSY',
    'DISPLAY_MODE_NOT_APPLIED',
    'DISPLAY_SETUP_REQUIRED',
    'DISPLAY_MODE_ROLLBACK_INCOMPLETE',
    'DISPLAY_MODE_UNAVAILABLE',
    'DISPLAY_MODE_TIMEOUT',
    'BAD_RESPONSE',
  ])('%s 는 기본 문구가 아니라 전용 한글 문구', (code) => {
    const text = errorText(new ApiError(409, { code, message: 'raw server text' }));
    expect(text).toBe(ERROR_TEXT[code]);
    expect(text).not.toBe(DEFAULT);
    expect(text).not.toContain(code);
  });

  it('로그인 화면이 없으므로 다시 로그인하라는 문구를 쓰지 않는다', () => {
    for (const text of Object.values(ERROR_TEXT)) expect(text).not.toContain('로그인해 주세요');
    expect(ERROR_TEXT.TOKEN_REVOKED).toBe('콘솔 연결이 만료되었습니다. 페이지를 새로고침해 주세요.');
  });

  it('백엔드가 더 보내지 않는 LOGIN_RATE_LIMITED 는 표에 없다 (모르는 코드처럼 기본 문구)', () => {
    expect(Object.hasOwn(ERROR_TEXT, 'LOGIN_RATE_LIMITED')).toBe(false);
    expect(errorText(new ApiError(429, { code: 'LOGIN_RATE_LIMITED', message: 'x' }))).toBe(DEFAULT);
  });
});

describe('INVALID_VM_STATE·INVALID_ARGUMENT — 서버 한글 사유 우선 (S15P31E110-471)', () => {
  it('서버 message 가 한글이면 그 사유를 그대로 보인다', () => {
    expect(
      errorText(
        new ApiError(409, {
          code: 'INVALID_VM_STATE',
          message: '보드가 종료 중이라 새 작업을 받을 수 없습니다.',
        }),
      ),
    ).toBe('보드가 종료 중이라 새 작업을 받을 수 없습니다.');
    expect(
      errorText(
        new ApiError(400, {
          code: 'INVALID_ARGUMENT',
          message: '이 템플릿이 허용하지 않는 vCPU·메모리 값입니다.',
        }),
      ),
    ).toBe('이 템플릿이 허용하지 않는 vCPU·메모리 값입니다.');
  });

  it('한글이 아니거나 비었으면 표의 문구', () => {
    expect(errorText(new ApiError(409, { code: 'INVALID_VM_STATE', message: 'domain abc is running' }))).toBe(
      ERROR_TEXT.INVALID_VM_STATE,
    );
    expect(errorText(new ApiError(400, { code: 'INVALID_ARGUMENT', message: '   ' }))).toBe(
      ERROR_TEXT.INVALID_ARGUMENT,
    );
    // 본문에 message 가 없으면 ApiError 가 만든 기본 문구(한글)다 — 서버 사유로 보지 않는다
    expect(errorText(new ApiError(409, { code: 'INVALID_VM_STATE' }))).toBe(ERROR_TEXT.INVALID_VM_STATE);
  });

  it('다른 코드는 서버 message 가 한글이어도 표의 문구', () => {
    expect(
      errorText(
        new ApiError(409, { code: 'VM_NAME_CONFLICT', message: '보드에 같은 이름의 VM이 이미 있습니다.' }),
      ),
    ).toBe(ERROR_TEXT.VM_NAME_CONFLICT);
  });
});

describe('commandStateText — 명령 상태', () => {
  it.each<[CommandState, string]>([
    ['PENDING', '대기'],
    ['ACCEPTED', '접수'],
    ['RUNNING', '실행 중'],
    ['SUCCEEDED', '완료'],
    ['FAILED', '실패'],
    ['REJECTED', '거절'],
  ])('%s → %s', (state, text) => {
    expect(commandStateText(state)).toBe(text);
  });
});

describe('부하·장애 명령 오류 (S15P31E110-468)', () => {
  it('HTTP 오류 AGENT_UNAVAILABLE·MQTT_UNAVAILABLE·COMMAND_NOT_FOUND 도 표의 한글 문구', () => {
    for (const code of ['AGENT_UNAVAILABLE', 'MQTT_UNAVAILABLE', 'COMMAND_NOT_FOUND'] as const) {
      expect(
        errorText(new ApiError(code === 'COMMAND_NOT_FOUND' ? 404 : 409, { code, message: 'raw' })),
      ).toBe(ERROR_TEXT[code]);
    }
    expect(ERROR_TEXT.COMMAND_NOT_FOUND).toMatch(/^명령을 찾을 수 없습니다/);
  });
});

describe('agentErrorText — Agent 거절·실패 사유', () => {
  it('알려진 코드는 전부 한글 문구이고, 코드 이름·괄호를 붙이지 않는다', () => {
    expect(Object.keys(AGENT_ERROR_TEXT)).toHaveLength(12);
    for (const [code, text] of Object.entries(AGENT_ERROR_TEXT) as [AgentErrorCode, string][]) {
      expect(text, code).toMatch(/[가-힣]/);
      expect(text, code).not.toContain(code);
      expect(text, code).not.toMatch(/[()]/);
      expect(agentErrorText(code)).toBe(text);
    }
    expect(agentErrorText('BUSY')).toBe('이미 실행 중인 부하가 있습니다. 끝난 뒤 다시 시도해 주세요.');
  });

  it('코드가 없거나 모르는 코드는 기본 문구, fallback 을 주면 그것', () => {
    expect(agentErrorText(null)).toBe('명령을 처리하지 못했습니다.');
    expect(agentErrorText(undefined)).toBe('명령을 처리하지 못했습니다.');
    expect(agentErrorText('PROCESS_EXITED')).toBe('명령을 처리하지 못했습니다.');
    expect(agentErrorText('toString')).toBe('명령을 처리하지 못했습니다.');
    expect(agentErrorText('', '부하 명령이 실패했습니다.')).toBe('부하 명령이 실패했습니다.');
  });
});
