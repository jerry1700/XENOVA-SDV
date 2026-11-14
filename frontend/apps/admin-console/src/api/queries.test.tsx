import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import {
  LIVE_POLL_MS,
  useMetrics,
  useProcesses,
  useTelemetryHeartbeat,
  useThreads,
  useVm,
  useVmResources,
  vmGone,
} from './queries';

const state = vi.hoisted(() => ({ calls: [] as string[], fail: null as ApiError | null }));

vi.mock('./client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./client')>()),
  request: (path: string) => {
    state.calls.push(path.split('?')[0]!);
    return state.fail ? Promise.reject(state.fail) : Promise.resolve({});
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** ms 만큼 시간을 흘리고 쌓인 Promise 를 비운다 */
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  state.calls = [];
  state.fail = null;
});
afterEach(() => {
  vi.useRealTimers();
});

const NOT_FOUND = new ApiError(404, { code: 'VM_NOT_FOUND', message: '존재하지 않는 VM입니다.' });

describe('없는 VM(404) 뒤에는 주기 조회를 멈춘다 (S15P31E110-469)', () => {
  it('vmGone: 404 만', () => {
    expect(vmGone(NOT_FOUND)).toBe(true);
    expect(vmGone(new ApiError(502, { code: 'VM_CONTROL_BAD_RESPONSE', message: '' }))).toBe(false);
    expect(vmGone(new ApiError(0, { message: '' }))).toBe(false);
    expect(vmGone(null)).toBe(false);
  });

  it.each([
    ['useVm', () => useVm('gone')],
    ['useVmResources', () => useVmResources('gone')],
    ['useMetrics', () => useMetrics('gone', true)],
    ['useProcesses', () => useProcesses('gone', true)],
    ['useThreads', () => useThreads('gone')],
    ['useTelemetryHeartbeat', () => useTelemetryHeartbeat('gone')],
  ] as [string, () => unknown][])('%s: 404 를 받으면 10초 동안 다시 요청하지 않는다', async (_name, hook) => {
    state.fail = NOT_FOUND;
    renderHook(hook, { wrapper });
    await advance(0);
    expect(state.calls).toHaveLength(1);
    await advance(10_000);
    expect(state.calls).toHaveLength(1);
  });

  it('404 가 아닌 실패(보드 끊김)는 계속 다시 읽는다', async () => {
    state.fail = new ApiError(502, { code: 'VM_CONTROL_BAD_RESPONSE', message: '' });
    renderHook(() => useVm('vm-a'), { wrapper });
    await advance(0);
    await advance(LIVE_POLL_MS * 3);
    expect(state.calls.length).toBeGreaterThanOrEqual(3);
  });

  it('정상 응답이면 LIVE_POLL_MS 마다 다시 읽는다', async () => {
    renderHook(() => useVm('vm-a'), { wrapper });
    await advance(0);
    await advance(LIVE_POLL_MS * 3);
    expect(state.calls.length).toBeGreaterThanOrEqual(4);
  });
});
