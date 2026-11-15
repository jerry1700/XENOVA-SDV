import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { presentationFixture } from './fixture';
import {
  DISPLAY_PROBLEM_TEXT,
  FAILURE_LIMIT,
  POLL_MS,
  REQUEST_TIMEOUT_MS,
  STALL_MS,
  useDisplay,
} from './useDisplay';

type Reply = (signal: AbortSignal) => Promise<Response>;

const json = (body: unknown, status = 200) =>
  ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;

/** sequence 를 바꾼 정상 응답 */
const snapshot = (sequence: number) => {
  const p = presentationFixture();
  p.state.sequence = sequence;
  return json(JSON.parse(JSON.stringify(p)));
};

let replies: Reply[] = [];
/** 응답 목록을 차례로 쓰고, 다 쓰면 마지막 응답을 계속 쓴다 */
function reply(...next: Reply[]) {
  replies = next;
}
const ok =
  (sequence: number): Reply =>
  async () =>
    snapshot(sequence);

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, 'debug').mockImplementation(() => {});
  vi.stubGlobal(
    'fetch',
    vi.fn((_url: string, init: RequestInit) => {
      const next = replies.length > 1 ? replies.shift()! : replies[0]!;
      return next(init.signal!);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function start() {
  const hook = renderHook(() => useDisplay());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  return hook;
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** 즉시 실패하는 응답이 연속 실패 기준에 닿을 때까지 폴링을 돌린다 (첫 요청은 start 에서 이미 실패) */
async function failToLimit() {
  for (let i = 1; i < FAILURE_LIMIT; i += 1) await tick(POLL_MS);
}

const down: Reply = async () => {
  throw new TypeError('Failed to fetch');
};

describe('표시 데이터 요청 실패는 종류별 고정 한국어 문구', () => {
  it('시간 초과(AbortError)는 원문 대신 "응답이 늦습니다"', async () => {
    // 응답하지 않다가 시간 제한에 끊기면 브라우저처럼 AbortError 로 거절한다
    reply(
      (signal) =>
        new Promise((_resolve, reject) =>
          signal.addEventListener('abort', () =>
            reject(new DOMException('signal is aborted without reason', 'AbortError')),
          ),
        ),
    );
    const { result } = await start();
    await tick(REQUEST_TIMEOUT_MS);
    for (let i = 1; i < FAILURE_LIMIT; i += 1) await tick(POLL_MS + REQUEST_TIMEOUT_MS);
    expect(result.current).toMatchObject({
      connected: false,
      problem: 'timeout',
      error: DISPLAY_PROBLEM_TEXT.timeout,
    });
    expect(result.current.error).not.toMatch(/signal|abort/i);
  });

  it('연결 실패(TypeError)는 "연결할 수 없습니다"', async () => {
    reply(async () => {
      throw new TypeError('Failed to fetch');
    });
    const { result } = await start();
    await failToLimit();
    expect(result.current.problem).toBe('network');
    expect(result.current.error).toBe('수집기에 연결할 수 없습니다');
  });

  it('응답 오류: 401 은 인증, 그 밖의 HTTP 오류는 응답 오류', async () => {
    reply(async () => json({}, 401));
    const auth = await start();
    await failToLimit();
    expect(auth.result.current.error).toBe('화면 인증이 필요합니다');
    auth.unmount();

    reply(async () => json({}, 503));
    const http = await start();
    await failToLimit();
    expect(http.result.current.problem).toBe('http');
    expect(http.result.current.error).toBe(DISPLAY_PROBLEM_TEXT.http);
  });

  it('형식 오류: JSON 해석 실패·규격 불일치는 "형식 오류"', async () => {
    reply(
      async () =>
        ({
          ok: true,
          status: 200,
          json: () => Promise.reject(new SyntaxError('Unexpected token <')),
        }) as Response,
    );
    const broken = await start();
    await failToLimit();
    expect(broken.result.current.problem).toBe('format');
    expect(broken.result.current.error).toBe('표시 데이터 형식 오류입니다');
    broken.unmount();

    reply(async () => json({ schema_version: 99 }));
    const invalid = await start();
    await failToLimit();
    expect(invalid.result.current.error).toBe(DISPLAY_PROBLEM_TEXT.format);
  });
});

describe('같은 sequence 재수신', () => {
  it('같은 sequence 는 변화 없음: 연결 상태·값을 유지한다', async () => {
    reply(ok(5), ok(5));
    const { result } = await start();
    const first = result.current.presentation;
    expect(result.current.connected).toBe(true);
    await tick(POLL_MS);
    expect(result.current.connected).toBe(true);
    expect(result.current.problem).toBeNull();
    expect(result.current.presentation).toBe(first);
  });

  it(`같은 sequence 가 ${STALL_MS / 1000}초 넘게 이어지면 갱신 멈춤, 새 sequence 가 오면 회복`, async () => {
    reply(ok(5));
    const { result } = await start();
    await tick(STALL_MS);
    // 아직 3초를 넘지 않았다
    expect(result.current.connected).toBe(true);
    await tick(POLL_MS);
    expect(result.current).toMatchObject({
      connected: false,
      problem: 'stalled',
      error: '데이터 갱신이 멈췄습니다',
    });
    reply(ok(6));
    await tick(POLL_MS);
    expect(result.current).toMatchObject({ connected: true, problem: null, error: null });
  });

  it('더 작은(이전) sequence 는 지금처럼 오류', async () => {
    reply(ok(5), ok(4));
    const { result } = await start();
    await tick(POLL_MS * FAILURE_LIMIT);
    expect(result.current).toMatchObject({ connected: false, problem: 'old' });
  });
});

describe('연속 실패 기준 (한 번 실패로는 끊김이 아니다)', () => {
  it('첫 응답 전에는 연결 중, 받으면 연결됨', async () => {
    reply(() => new Promise(() => {}));
    const pending = renderHook(() => useDisplay());
    expect(pending.result.current).toMatchObject({ connected: false, connecting: true, problem: null });
    pending.unmount();

    reply(ok(1));
    const { result } = await start();
    expect(result.current).toMatchObject({ connected: true, connecting: false });
  });

  it('첫 응답 전 실패도 기준에 닿기 전까지는 연결 중으로 둔다', async () => {
    reply(down);
    const { result } = await start();
    expect(result.current).toMatchObject({ connected: false, connecting: true, problem: null, error: null });
    await failToLimit();
    expect(result.current).toMatchObject({ connected: false, connecting: false, problem: 'network' });
  });

  it('한 번 실패(5xx·시간 초과)하고 다음에 회복하면 끊김 없이 마지막 값을 유지한다', async () => {
    reply(ok(1), async () => json({}, 503), ok(2));
    const { result } = await start();
    const first = result.current.presentation;
    await tick(POLL_MS);
    // 실패 1회: 값·연결 상태 그대로
    expect(result.current).toMatchObject({ connected: true, problem: null, error: null });
    expect(result.current.presentation).toBe(first);
    await tick(POLL_MS);
    expect(result.current).toMatchObject({ connected: true, problem: null });
    expect(result.current.presentation?.state.sequence).toBe(2);
  });

  it(`${FAILURE_LIMIT}번 연속 실패하면 끊김, 사이에 성공이 끼면 다시 센다`, async () => {
    reply(ok(1), down, ok(2), down, down);
    const { result } = await start();
    await tick(POLL_MS * 3);
    // 실패 · 성공 · 실패: 연속 1회
    expect(result.current.connected).toBe(true);
    await tick(POLL_MS);
    expect(result.current).toMatchObject({ connected: false, problem: 'network' });
    expect(result.current.presentation?.state.sequence).toBe(2);
  });

  it('같은 종류 실패가 이어지면 debug 로그는 한 번만, 종류가 바뀌면 다시 남긴다', async () => {
    reply(down, down, down, async () => json({}, 503));
    await start();
    await tick(POLL_MS * 2);
    expect(console.debug).toHaveBeenCalledTimes(1);
    await tick(POLL_MS);
    expect(console.debug).toHaveBeenCalledTimes(2);
    await tick(POLL_MS * 3);
    expect(console.debug).toHaveBeenCalledTimes(2);
  });
});
