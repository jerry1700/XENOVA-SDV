import { useEffect, useState } from 'react';
import { parsePresentation, SnapshotOrder, type Presentation } from './model';

/** 표시 데이터 요청 실패·멈춤의 종류. 화면에는 종류별 고정 한국어 문구만 보인다 (브라우저 오류 원문은 쓰지 않음) */
export type DisplayProblem = 'timeout' | 'network' | 'auth' | 'http' | 'format' | 'old' | 'stalled';

/** 휴대폰 가로(780px)의 상태 카드 한 줄(약 185px, 13px 글자)에 말줄임 없이 들어가는 길이로 짧게 쓴다 */
export const DISPLAY_PROBLEM_TEXT: Record<DisplayProblem, string> = {
  timeout: '표시 데이터 응답이 늦습니다',
  network: '수집기에 연결할 수 없습니다',
  auth: '화면 인증이 필요합니다',
  http: '수집기 응답 오류입니다',
  format: '표시 데이터 형식 오류입니다',
  old: '이전 세대 데이터를 받았습니다',
  stalled: '데이터 갱신이 멈췄습니다',
};

/** 요청 주기·시간 제한 */
export const POLL_MS = 1000;
export const REQUEST_TIMEOUT_MS = 2500;
/** 같은 sequence 가 이 시간을 넘게 이어지면 수집기 멈춤으로 보고 값을 미확인으로 둔다 */
export const STALL_MS = 3000;
/**
 * 요청이 이 횟수만큼 연달아 실패해야 '연결 끊김'으로 본다. 그 전에는 마지막 값을 그대로 둔다
 * (한 번 늦은 응답·5xx 로 화면 전체가 "—" 가 되지 않게). 최악은 시간 초과 2번 ≈ 7초
 */
export const FAILURE_LIMIT = 2;

export interface DisplayConnection {
  presentation: Presentation | null;
  /** 신선한 값을 쓸 수 있는가 (끊김·갱신 멈춤이면 false) */
  connected: boolean;
  /** 첫 응답을 기다리는 중 (아직 실패가 FAILURE_LIMIT 에 닿지 않음). 옛 호출부와 맞추려고 선택 필드 */
  connecting?: boolean;
  /** 실패·멈춤 종류 (정상이면 null). 옛 호출부(MobilePage)와 맞추려고 선택 필드 */
  problem?: DisplayProblem | null;
  /** problem 의 고정 한국어 문구 (없으면 null) */
  error: string | null;
}

class DisplayRequestError extends Error {
  constructor(readonly problem: DisplayProblem) {
    super(problem);
  }
}

/** 실패 종류 판단. 오류 원문은 화면에 쓰지 않는다 */
export function problemOf(error: unknown): DisplayProblem {
  if (error instanceof DisplayRequestError) return error.problem;
  if (error instanceof Error || error instanceof DOMException) {
    if (error.name === 'AbortError' || error.name === 'TimeoutError') return 'timeout';
    if (error instanceof TypeError) return 'network';
  }
  // JSON(SyntaxError)·규격 검증(model.ts) 실패
  return 'format';
}

const failed =
  (problem: DisplayProblem) =>
  (previous: DisplayConnection): DisplayConnection => ({
    ...previous,
    connected: false,
    connecting: false,
    problem,
    error: DISPLAY_PROBLEM_TEXT[problem],
  });

/** Same-origin dom0 HTTP only. No user password or service token is embedded in the page. */
export function useDisplay(): DisplayConnection {
  const [state, setState] = useState<DisplayConnection>({
    presentation: null,
    connected: false,
    connecting: true,
    problem: null,
    error: null,
  });
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    const order = new SnapshotOrder();
    // 마지막으로 sequence 가 바뀐 시각 (같은 sequence 가 STALL_MS 넘게 이어지면 갱신 멈춤)
    let changedAt = 0;
    // 연달아 실패한 횟수 (응답을 제대로 받으면 0)
    let failures = 0;
    // 마지막으로 기록한 실패 종류: 같은 종류가 이어지면 1초마다 로그를 쌓지 않는다
    let logged: DisplayProblem | null = null;
    const fail = (problem: DisplayProblem, error?: unknown) => {
      if (problem !== logged) {
        logged = problem;
        console.debug('[display] 표시 데이터 요청 실패', problem, error);
      }
      failures += 1;
      if (failures >= FAILURE_LIMIT) setState(failed(problem));
    };
    const ok = (next: (previous: DisplayConnection) => DisplayConnection) => {
      failures = 0;
      logged = null;
      setState(next);
    };
    async function poll() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), REQUEST_TIMEOUT_MS);
      try {
        // 절대 경로를 그대로 쓴다: dom0 kiosk 빌드는 base '/' 라 BASE_URL 기준과 같고, /screens/ 공개 빌드에서
        // BASE_URL 기준(/screens/api/…)으로 바꾸면 nginx 가 API 대신 정적 서버로 보낸다 (deploy/nginx /screens/ → vm-screens)
        const response = await fetch('/api/v1/display/presentation', {
          credentials: 'same-origin',
          cache: 'no-store',
          signal: controller.signal,
        });
        if (!response.ok) throw new DisplayRequestError(response.status === 401 ? 'auth' : 'http');
        const presentation = parsePresentation(await response.json());
        const kind = order.classify(presentation);
        if (kind === 'old') throw new DisplayRequestError('old');
        const now = Date.now();
        if (stopped) return;
        if (kind === 'new') {
          changedAt = now;
          ok(() => ({ presentation, connected: true, connecting: false, problem: null, error: null }));
        } else if (now - changedAt > STALL_MS) {
          // 응답은 오지만 수집기 값이 멈췄다: 시간으로 이미 판정했으므로 바로 알린다
          failures = 0;
          if (logged !== 'stalled') {
            logged = 'stalled';
            console.debug('[display] 표시 데이터 갱신 멈춤');
          }
          setState(failed('stalled'));
        } else {
          // 변화 없음: 값은 그대로 두고 연결만 정상으로 본다
          ok((previous) => ({ ...previous, connected: true, connecting: false, problem: null, error: null }));
        }
      } catch (error) {
        if (!stopped) fail(problemOf(error), error);
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(() => void poll(), POLL_MS);
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, []);
  return state;
}
