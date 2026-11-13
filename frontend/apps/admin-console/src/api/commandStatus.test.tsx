import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { commandErrorCode } from './commandStatus';
import { useCommandStatus } from './queries';
import type { CommandStatusResponse } from './types';

describe('commandErrorCode (Agent 실패·거절 코드)', () => {
  it('문자열 code 만 꺼내고, error 가 없거나 형식이 달라도 null', () => {
    expect(commandErrorCode({ error: { code: 'BUSY', message: 'x' } })).toBe('BUSY');
    expect(commandErrorCode({ error: null })).toBeNull();
    expect(commandErrorCode({})).toBeNull();
    expect(commandErrorCode(null)).toBeNull();
    expect(commandErrorCode({ error: { message: '사유만' } })).toBeNull();
    expect(commandErrorCode({ error: { code: 42 } })).toBeNull();
    expect(commandErrorCode({ error: { code: '  ' } })).toBeNull();
    expect(commandErrorCode({ error: 'BUSY' as unknown as CommandStatusResponse['error'] })).toBeNull();
    expect(commandErrorCode({ error: ['BUSY'] as unknown as CommandStatusResponse['error'] })).toBeNull();
  });
});

describe('useCommandStatus', () => {
  let calls = 0;
  let reply: () => Response;
  beforeEach(() => {
    vi.useFakeTimers();
    calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        calls += 1;
        return reply();
      }),
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
  );
  const run = async (ms: number) => {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  };

  it('404 COMMAND_NOT_FOUND 면 재시도·반복 조회를 멈춘다', async () => {
    reply = () =>
      Response.json({ code: 'COMMAND_NOT_FOUND', message: '명령을 찾을 수 없습니다.' }, { status: 404 });
    const { result } = renderHook(() => useCommandStatus('vm-1', 'cmd-1'), { wrapper });
    await run(10_000);
    expect(calls).toBe(1);
    expect(result.current.isError).toBe(true);
  });

  it('끝나지 않은 명령은 1초마다 다시 묻고, 끝나면 멈춘다', async () => {
    let status = 'RUNNING';
    reply = () => Response.json({ vm_id: 'vm-1', command_id: 'cmd-1', status });
    renderHook(() => useCommandStatus('vm-1', 'cmd-1'), { wrapper });
    await run(2_100);
    expect(calls).toBe(3);
    status = 'REJECTED';
    await run(1_000);
    const settled = calls;
    await run(5_000);
    expect(calls).toBe(settled);
  });
});
