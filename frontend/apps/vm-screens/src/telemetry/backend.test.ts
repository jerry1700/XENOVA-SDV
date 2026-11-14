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
