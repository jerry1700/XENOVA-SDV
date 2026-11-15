import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TokenResponse } from './types';

/**
 * QA 2-2: 탭 여러 개가 동시에 refresh 하면 서버가 같은 refresh 토큰의 재사용으로 보고 계정의 토큰을 전부 폐기한다.
 * 탭 사이를 navigator.locks 로 줄 세우고, 먼저 받은 탭이 새 토큰을 BroadcastChannel 로 나눈다.
 * 한 테스트 안에서 "다른 탭"은 같은 이름의 채널 인스턴스와 락을 먼저 잡은 쪽으로 흉내 낸다.
 */

const token = (value: string): TokenResponse => ({
  access_token: value,
  token_type: 'Bearer',
  expires_in: 900,
  expires_at: '2026-11-13T00:15:00Z',
  user: { user_id: 'u-1', username: 'admin', nickname: '관리자' },
});

const json = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

/** 같은 이름의 인스턴스끼리 메시지를 주고받는 채널 (보낸 인스턴스는 받지 않는다 — 브라우저와 같다) */
class FakeChannel {
  static all: FakeChannel[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly name: string) {
    FakeChannel.all.push(this);
  }
  postMessage(data: unknown) {
    for (const other of FakeChannel.all)
      if (other !== this && other.name === this.name) other.onmessage?.({ data } as MessageEvent);
  }
  close() {}
}

/** 이름별로 한 번에 하나만 콜백을 돌리는 락 */
function fakeLocks() {
  const tails = new Map<string, Promise<unknown>>();
  const requested: string[] = [];
  return {
    requested,
    request<T>(name: string, callback: () => Promise<T>): Promise<T> {
      requested.push(name);
      const previous = tails.get(name) ?? Promise.resolve();
      const next = previous.then(() => callback());
      tails.set(
        name,
        next.catch(() => undefined),
      );
      return next;
    },
  };
}

const fetchMock = vi.fn<typeof fetch>();

async function loadClient() {
  vi.resetModules();
  return import('./client');
}

beforeEach(() => {
  FakeChannel.all = [];
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('BroadcastChannel', FakeChannel);
});
afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe('탭 사이 세션 재발급 (QA 2-2)', () => {
  it('다른 탭이 재발급하는 동안 기다렸다가, 그 탭이 나눈 토큰을 쓰고 refresh 를 다시 보내지 않는다', async () => {
    const locks = fakeLocks();
    vi.stubGlobal('navigator', { ...navigator, locks });
    const client = await loadClient();
    const otherTab = new FakeChannel('xenova-session');

    // 다른 탭이 락을 먼저 잡고 재발급 중
    let releaseOther!: () => void;
    void locks.request('xenova-refresh', () => new Promise<void>((resolve) => (releaseOther = resolve)));

    const refreshing = client.refreshSession();
    await Promise.resolve();
    otherTab.postMessage({ type: 'refreshed', session: token('from-other-tab') });
    releaseOther();

    await expect(refreshing).resolves.toBe(true);
    expect(locks.requested.filter((name) => name === 'xenova-refresh')).toHaveLength(2);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(client.getSession()?.access_token).toBe('from-other-tab');
  });

  it('먼저 락을 잡은 탭은 한 번만 재발급하고 새 토큰을 다른 탭에 나눈다', async () => {
    vi.stubGlobal('navigator', { ...navigator, locks: fakeLocks() });
    const client = await loadClient();
    const otherTab = new FakeChannel('xenova-session');
    const received: unknown[] = [];
    otherTab.onmessage = (event) => received.push(event.data);
    fetchMock.mockResolvedValueOnce(json(200, token('fresh')));

    await expect(client.refreshSession()).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/auth/refresh');
    expect(received).toEqual([{ type: 'refreshed', session: token('fresh') }]);
  });

  it('navigator.locks 가 없는 브라우저는 예전처럼 바로 재발급한다', async () => {
    vi.stubGlobal('navigator', { ...navigator, locks: undefined });
    const client = await loadClient();
    fetchMock.mockResolvedValueOnce(json(200, token('fresh')));
    await expect(client.refreshSession()).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('다른 탭이 나눈 토큰은 이 탭이 재발급 중이 아니어도 받아 둔다 (곧 만료될 옛 토큰 대신)', async () => {
    vi.stubGlobal('navigator', { ...navigator, locks: fakeLocks() });
    const client = await loadClient();
    client.setSession(token('old'));
    new FakeChannel('xenova-session').postMessage({ type: 'refreshed', session: token('newer') });
    expect(client.getSession()?.access_token).toBe('newer');
  });
});

describe('인증 요청 시간 제한 (S15P31E110-472)', () => {
  /** 응답 없이 매달리다 signal 이 끊기면 그 사유로 실패하는 fetch */
  const hanging = (_url: RequestInfo | URL, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(init.signal!.reason), { once: true });
    });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('refresh 가 10초 동안 답이 없으면 실패로 끝내고 탭 간 잠금을 푼다', async () => {
    vi.useFakeTimers();
    const locks = fakeLocks();
    vi.stubGlobal('navigator', { ...navigator, locks });
    const client = await loadClient();
    client.setSession(token('old'));
    fetchMock.mockImplementation(hanging);

    const result = client.refreshSession();
    await vi.advanceTimersByTimeAsync(client.AUTH_TIMEOUT_MS - 1);
    // 아직 기다리는 중 — 다른 탭은 잠금을 얻지 못한다
    let otherTabGotLock = false;
    void locks.request('xenova-refresh', async () => {
      otherTabGotLock = true;
    });
    await Promise.resolve();
    expect(otherTabGotLock).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    await expect(result).resolves.toBe(false);
    expect(client.getSession()).toBeNull();
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
    // 잠금이 풀려 다른 탭이 이어서 연결한다
    await vi.advanceTimersByTimeAsync(0);
    expect(otherTabGotLock).toBe(true);
  });

  it('refresh 가 거절되고 공용 계정 login 이 매달려도 10초 뒤 실패로 끝난다', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('navigator', { ...navigator, locks: fakeLocks() });
    const client = await loadClient();
    fetchMock
      .mockResolvedValueOnce(json(401, { code: 'INVALID_REFRESH_TOKEN' }))
      .mockImplementationOnce(hanging);

    const result = client.refreshSession();
    await vi.advanceTimersByTimeAsync(client.AUTH_TIMEOUT_MS);
    await expect(result).resolves.toBe(false);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/auth/refresh',
      '/api/v1/auth/login',
    ]);
  });

  it('timeoutSignal: 시간 제한과 호출하는 쪽 signal 중 먼저 오는 쪽으로 끊긴다', async () => {
    vi.useFakeTimers();
    const client = await loadClient();
    const outer = new AbortController();
    const byOuter = client.timeoutSignal(10_000, outer.signal);
    outer.abort();
    expect(byOuter.signal.aborted).toBe(true);
    byOuter.done();

    const byTime = client.timeoutSignal(10_000);
    vi.advanceTimersByTime(10_000);
    expect(byTime.signal.aborted).toBe(true);
    expect((byTime.signal.reason as DOMException).name).toBe('TimeoutError');

    const finished = client.timeoutSignal(10_000);
    finished.done();
    vi.advanceTimersByTime(10_000);
    expect(finished.signal.aborted).toBe(false);
  });
});
