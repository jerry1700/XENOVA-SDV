import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setSession } from './client';
import { REJECTED_CLOSE_CODES, RETRY_MAX_MS, retryDelayMs, useAuthedSocket } from './drive';
import type { TokenResponse } from './types';

const refresh = vi.hoisted(() => ({ result: true, calls: 0 }));
vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return {
    ...actual,
    refreshSession: () => {
      refresh.calls++;
      if (!refresh.result) actual.setSession(null);
      return Promise.resolve(refresh.result);
    },
  };
});

/** 열린 순서대로 쌓이는 가짜 WebSocket */
class FakeSocket {
  static OPEN = 1;
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void | Promise<void>) | null = null;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {}
  /** 서버가 인증을 받아 준다 */
  authenticate() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
    this.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) });
  }
  async serverClose(code: number) {
    this.readyState = 3;
    await this.onclose?.({ code });
  }
}

const TOKEN = { access_token: 'tok' } as TokenResponse;
const last = () => FakeSocket.all.at(-1)!;
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}
const render = () => renderHook(() => useAuthedSocket('/ws/v1/drive/state', () => {}));

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', FakeSocket);
  FakeSocket.all = [];
  refresh.result = true;
  refresh.calls = 0;
  setSession(TOKEN);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setSession(null);
});

describe('인증 소켓 재연결 정책 (S15P31E110-469)', () => {
  it('간격: 2초부터 두 배씩, 최대 30초', () => {
    expect([0, 1, 2, 3, 4, 5, 10].map(retryDelayMs)).toEqual([2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    expect(RETRY_MAX_MS).toBe(30_000);
  });

  it('일반 종료는 2·4·8초 간격으로 다시 붙고, 인증되면 처음 간격으로 돌린다', async () => {
    const { result } = render();
    expect(FakeSocket.all).toHaveLength(1);

    await act(() => last().serverClose(1006));
    expect(result.current.state).toBe('closed');
    await advance(1999);
    expect(FakeSocket.all).toHaveLength(1);
    await advance(1);
    expect(FakeSocket.all).toHaveLength(2);

    await act(() => last().serverClose(1006));
    await advance(3999);
    expect(FakeSocket.all).toHaveLength(2);
    await advance(1);
    expect(FakeSocket.all).toHaveLength(3);

    await act(() => last().serverClose(1011));
    await advance(7999);
    expect(FakeSocket.all).toHaveLength(3);
    await advance(1);
    expect(FakeSocket.all).toHaveLength(4);

    act(() => last().authenticate());
    expect(result.current.state).toBe('open');
    await act(() => last().serverClose(1006));
    await advance(2000);
    expect(FakeSocket.all).toHaveLength(5);
  });

  it.each([...REJECTED_CLOSE_CODES])('%i 이면 다시 붙지 않고 rejected', async (code) => {
    const { result } = render();
    await act(() => last().serverClose(code));
    expect(result.current.state).toBe('rejected');
    await advance(120_000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it('4401 은 토큰을 갱신한 뒤 2초 뒤 다시 붙는다 (백오프 없이)', async () => {
    render();
    await act(() => last().serverClose(1006)); // 백오프 1단계를 써 둔다
    await advance(2000);
    await act(() => last().serverClose(4401));
    expect(refresh.calls).toBe(1);
    await advance(2000);
    expect(FakeSocket.all).toHaveLength(3);
  });

  it('4401 갱신에 실패하면 세션이 다시 생길 때 붙는다', async () => {
    refresh.result = false;
    const { result } = render();
    await act(() => last().serverClose(4401));
    expect(result.current.state).toBe('closed');
    expect(FakeSocket.all).toHaveLength(1);
    act(() => setSession(TOKEN));
    expect(FakeSocket.all).toHaveLength(2);
  });

  it('연결 순간 토큰이 없어도 closed 로 끝나지 않고, 세션이 생기면 바로 붙는다', async () => {
    setSession(null);
    const { result } = render();
    expect(FakeSocket.all).toHaveLength(0);
    expect(result.current.state).toBe('closed');
    act(() => setSession(TOKEN));
    expect(FakeSocket.all).toHaveLength(1);
    expect(result.current.state).toBe('connecting');
  });

  it('토큰이 계속 없으면 백오프 간격으로 다시 확인한다', async () => {
    setSession(null);
    render();
    await advance(2000);
    expect(FakeSocket.all).toHaveLength(0);
    // 그 사이 세션이 알림 없이 채워져도(다른 경로) 다음 확인 때 붙는다
    vi.spyOn(await import('./client'), 'getSession').mockReturnValue(TOKEN);
    await advance(4000);
    expect(FakeSocket.all).toHaveLength(1);
  });

  it('화면을 떠나면 타이머·세션 구독을 정리한다', async () => {
    setSession(null);
    const { unmount } = render();
    unmount();
    act(() => setSession(TOKEN));
    await advance(60_000);
    expect(FakeSocket.all).toHaveLength(0);
  });
});
