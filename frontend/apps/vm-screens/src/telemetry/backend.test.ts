import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MAX_AUTH_RETRY_MS, openSocket, parseFrame, RETRY_MS, retryDelay, UNAUTHORIZED } from './backend';

class SocketFake {
  static OPEN = 1;
  static all: SocketFake[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor(readonly url: string) {
    SocketFake.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {}
  open() {
    this.readyState = SocketFake.OPEN;
    this.onopen?.();
  }
}

const latest = () => SocketFake.all[SocketFake.all.length - 1]!;

beforeEach(() => {
  vi.useFakeTimers();
  SocketFake.all = [];
  vi.stubGlobal('WebSocket', SocketFake);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('레거시 WS 프레임 해석', () => {
  it('깨진 JSON·type 없는 프레임은 버린다', () => {
    expect(parseFrame('{"type":"x"')).toBeNull();
    expect(parseFrame('not json')).toBeNull();
    expect(parseFrame('[1,2]')).toBeNull();
    expect(parseFrame('null')).toBeNull();
    expect(parseFrame('{"value":1}')).toBeNull();
    expect(parseFrame(new ArrayBuffer(2))).toBeNull();
    expect(parseFrame('{"type":"telemetry","v":1}')).toEqual({ type: 'telemetry', v: 1 });
  });

  it('깨진 프레임이 와도 예외 없이 다음 프레임을 받는다', async () => {
    const onFrame = vi.fn();
    const stop = openSocket({
      path: '/ws',
      getToken: async () => 'token',
      onFrame,
      onConnection: () => {},
    });
    await vi.advanceTimersByTimeAsync(0);
    const ws = latest();
    ws.open();
    expect(() => ws.onmessage!({ data: '{"type":' })).not.toThrow();
    ws.onmessage!({ data: '{"type":"telemetry"}' });
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(onFrame).toHaveBeenCalledWith({ type: 'telemetry' });
    stop();
  });
});

describe('인증 실패 재시도 간격', () => {
  it('연속 인증 실패마다 두 배로 늘리고 상한에서 멈춘다', () => {
    expect(retryDelay(0)).toBe(RETRY_MS);
    expect([1, 2, 3, 4, 5, 6, 10].map(retryDelay)).toEqual([2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    expect(MAX_AUTH_RETRY_MS).toBe(30_000);
  });

  it('4401 이 이어지면 다시 붙는 간격이 늘고, 인증되면 처음 간격으로 돌아간다', async () => {
    const getToken = vi.fn(async () => 'token');
    const states: string[] = [];
    const stop = openSocket({
      path: '/ws',
      getToken,
      onFrame: () => {},
      onConnection: (state) => states.push(state),
    });
    await vi.advanceTimersByTimeAsync(0);
    const reject = async () => {
      latest().onclose!({ code: UNAUTHORIZED });
    };
    const attemptsAfter = async (ms: number) => {
      await vi.advanceTimersByTimeAsync(ms);
      return getToken.mock.calls.length;
    };

    await reject(); // 실패 1 → 2초
    expect(await attemptsAfter(RETRY_MS - 1)).toBe(1);
    expect(await attemptsAfter(1)).toBe(2);
    await reject(); // 실패 2 → 4초
    expect(await attemptsAfter(RETRY_MS * 2 - 1)).toBe(2);
    expect(await attemptsAfter(1)).toBe(3);
    await reject(); // 실패 3 → 8초
    expect(await attemptsAfter(RETRY_MS * 4 - 1)).toBe(3);
    expect(await attemptsAfter(1)).toBe(4);
    // 4401 뒤에는 토큰을 새로 받는다
    expect(getToken).toHaveBeenLastCalledWith(true);

    // 인증 성공 → 다음 끊김(일반)은 2초
    latest().open();
    latest().onmessage!({ data: '{"type":"authenticated"}' });
    expect(states).toContain('live');
    latest().onclose!({ code: 1006 });
    expect(await attemptsAfter(RETRY_MS)).toBe(5);
    stop();
  });

  it('토큰을 못 받아도(refresh·login 실패) 같은 간격으로 늘린다', async () => {
    const getToken = vi.fn(async () => null);
    const stop = openSocket({ path: '/ws', getToken, onFrame: () => {}, onConnection: () => {} });
    await vi.advanceTimersByTimeAsync(0);
    expect(getToken).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(RETRY_MS);
    expect(getToken).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(RETRY_MS * 2 - 1);
    expect(getToken).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(getToken).toHaveBeenCalledTimes(3);
    stop();
  });
});

describe('토큰 재발급은 관리 콘솔과 같은 탭 간 잠금 안에서 (S15P31E110-472)', () => {
  const fetchMock = vi.fn<typeof fetch>();

  /** 이름별로 한 번에 하나만 콜백을 돌리는 락 */
  function fakeLocks() {
    const tails = new Map<string, Promise<unknown>>();
    const requested: string[] = [];
    return {
      requested,
      request<T>(name: string, callback: () => Promise<T>): Promise<T> {
        requested.push(name);
        const next = (tails.get(name) ?? Promise.resolve()).then(() => callback());
        tails.set(
          name,
          next.catch(() => undefined),
        );
        return next;
      },
    };
  }

  async function load(locks: ReturnType<typeof fakeLocks>) {
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('navigator', { ...navigator, locks });
    vi.resetModules();
    return import('./backend');
  }

  afterEach(() => {
    fetchMock.mockReset();
  });

  it('refresh 는 xenova-refresh 잠금을 얻은 뒤에 보낸다 (콘솔 탭이 재발급 중이면 기다린다)', async () => {
    const locks = fakeLocks();
    const backend = await load(locks);
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ access_token: 'screen' }), { status: 200 }));
    // 콘솔 탭이 먼저 잠금을 잡고 재발급 중
    let releaseConsole!: () => void;
    void locks.request('xenova-refresh', () => new Promise<void>((resolve) => (releaseConsole = resolve)));

    const pending = backend.accessToken(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(locks.requested).toEqual(['xenova-refresh', 'xenova-refresh']);
    expect(fetchMock).not.toHaveBeenCalled();

    releaseConsole();
    await expect(pending).resolves.toBe('screen');
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/auth/refresh');
    expect(backend.REFRESH_LOCK).toBe('xenova-refresh');
  });

  it('refresh 가 10초 동안 답이 없으면 끊고 잠금을 푼다', async () => {
    const locks = fakeLocks();
    const backend = await load(locks);
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) =>
          init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))),
        ),
    );
    const pending = backend.accessToken(true);
    await vi.advanceTimersByTimeAsync(backend.AUTH_TIMEOUT_MS);
    await expect(pending).resolves.toBeNull();

    let nextGotLock = false;
    await locks.request('xenova-refresh', async () => {
      nextGotLock = true;
    });
    expect(nextGotLock).toBe(true);
  });
});
