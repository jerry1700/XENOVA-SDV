import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { presentationFixture } from './fixture';
import { DISPLAY_PROBLEM_TEXT, POLL_MS, REQUEST_TIMEOUT_MS, STALL_MS, useDisplay } from './useDisplay';

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
    expect(result.current.problem).toBe('network');
    expect(result.current.error).toBe('수집기에 연결할 수 없습니다');
  });

  it('응답 오류: 401 은 인증, 그 밖의 HTTP 오류는 응답 오류', async () => {
    reply(async () => json({}, 401));
    const auth = await start();
    expect(auth.result.current.error).toBe('화면 인증이 필요합니다');
    auth.unmount();

    reply(async () => json({}, 503));
    const http = await start();
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
    expect(broken.result.current.problem).toBe('format');
    expect(broken.result.current.error).toBe('표시 데이터 형식 오류입니다');
    broken.unmount();

    reply(async () => json({ schema_version: 99 }));
    const invalid = await start();
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
    await tick(POLL_MS);
    expect(result.current).toMatchObject({ connected: false, problem: 'old' });
  });
});
