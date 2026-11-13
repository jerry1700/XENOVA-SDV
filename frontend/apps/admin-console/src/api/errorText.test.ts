import { describe, expect, it } from 'vitest';
import { ApiError } from './client';
import { commandStateText, ERROR_TEXT, errorText } from './errorText';
import type { CommandState, KnownErrorCode } from './types';

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
