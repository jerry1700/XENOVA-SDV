/**
 * 백엔드 연결 — 토큰 받기와 인증 WebSocket.
 *   로그인·토큰 재발급: 노션 API 명세 "로그인"·"토큰 재발급" (refresh 쿠키, Path=/api/v1/auth)
 *   WebSocket: 첫 프레임 {"type":"auth","access_token"}, 실패하면 4401 로 닫힌다 (WS-01·WS-02 와 같은 규칙)
 */

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

export const UNAUTHORIZED = 4401;

/** API_BASE 가 비면 같은 origin (개발 서버 프록시) */
export function wsUrl(path: string): string {
  const origin = API_BASE || window.location.origin;
  return origin.replace(/^http/, 'ws') + path;
}

async function token(path: string, body?: unknown): Promise<string | null> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'include',
    });
    if (!response.ok) return null;
    const json = (await response.json()) as { access_token?: string };
    return json.access_token ?? null;
  } catch {
    return null;
  }
}

/**
 * 모니터 화면은 로그인 화면이 없다. 같은 브라우저에서 관리 콘솔에 로그인했으면 refresh 쿠키로 받고,
 * 없으면 개발용 계정(.env.local 의 VITE_SCREEN_USERNAME·PASSWORD)으로 로그인한다.
 * 키오스크 장치 인증 방식은 화면 정의서 §7 열린 질문.
 */
export function accessToken(fresh = false): Promise<string | null> {
  // 소켓 여러 개가 동시에 재발급하면 서버가 refresh 토큰을 회전시켜 두 번째가 실패한다 → 한 번만 받아 같이 쓴다
  if (!cached || (fresh && !inflight)) {
    inflight = true;
    cached = fetchToken().finally(() => (inflight = false));
  }
  return cached;
}

let cached: Promise<string | null> | null = null;
let inflight = false;

async function fetchToken(): Promise<string | null> {
  const refreshed = await token('/api/v1/auth/refresh');
  if (refreshed) return refreshed;
  const username = import.meta.env.VITE_SCREEN_USERNAME;
  const password = import.meta.env.VITE_SCREEN_PASSWORD;
  if (!username || !password) return null;
  return token('/api/v1/auth/login', { username, password });
}

export type Connection = 'connecting' | 'live' | 'offline' | 'unauthorized';

export interface SocketOptions {
  path: string;
  /** fresh 면 갖고 있던 토큰을 버리고 새로 받는다 (4401 뒤) */
  getToken: (fresh: boolean) => Promise<string | null>;
  /** 인증 뒤 보낼 프레임 (구독 등) */
  afterAuth?: (send: (frame: unknown) => void) => void;
  onFrame: (frame: { type: string } & Record<string, unknown>) => void;
  onConnection: (state: Connection) => void;
}

export const RETRY_MS = 2000;
/** 인증 실패가 이어질 때 재시도 간격의 상한 */
export const MAX_AUTH_RETRY_MS = 30_000;

/**
 * 다시 붙기까지 기다릴 시간. 인증 실패(토큰 없음·4401)가 연달아 n 번이면 2·4·8·16·30초…로 늘린다
 * (refresh·login 을 2초마다 부르지 않게). 그 밖의 끊김은 2초 그대로
 */
export function retryDelay(authFailures: number): number {
  if (authFailures <= 0) return RETRY_MS;
  return Math.min(MAX_AUTH_RETRY_MS, RETRY_MS * 2 ** (authFailures - 1));
}

/** 텍스트 프레임 해석. JSON 이 아니거나 type 이 없으면 null (버린다) */
export function parseFrame(data: unknown): ({ type: string } & Record<string, unknown>) | null {
  if (typeof data !== 'string') return null;
  let frame: unknown;
  try {
    frame = JSON.parse(data);
  } catch {
    return null;
  }
  return typeof frame === 'object' &&
    frame !== null &&
    !Array.isArray(frame) &&
    typeof (frame as { type?: unknown }).type === 'string'
    ? (frame as { type: string } & Record<string, unknown>)
    : null;
}

/** 끊기면 다시 붙는다 (간격은 retryDelay). 반환값을 부르면 닫고 멈춘다 */
export function openSocket(options: SocketOptions): () => void {
  let socket: WebSocket | null = null;
  let timer: number | undefined;
  let stopped = false;
  let fresh = false;
  // 연달아 인증에 실패한 횟수 (인증되면 0)
  let authFailures = 0;

  const retry = () => {
    if (!stopped) timer = window.setTimeout(connect, retryDelay(authFailures));
  };

  async function connect() {
    options.onConnection('connecting');
    const accessToken = await options.getToken(fresh);
    fresh = false;
    if (stopped) return;
    if (!accessToken) {
      options.onConnection('unauthorized');
      fresh = true;
      authFailures += 1;
      retry();
      return;
    }
    const ws = new WebSocket(wsUrl(options.path));
    socket = ws;
    const send = (frame: unknown) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(frame));
    ws.onopen = () => send({ type: 'auth', access_token: accessToken });
    ws.onmessage = (message) => {
      // 깨진 프레임 하나로 핸들러가 예외를 던지지 않게 버린다
      const frame = parseFrame(message.data);
      if (!frame) return;
      if (frame.type === 'authenticated') {
        authFailures = 0;
        options.onConnection('live');
        options.afterAuth?.(send);
        return;
      }
      options.onFrame(frame);
    };
    ws.onclose = (event) => {
      if (socket === ws) socket = null;
      if (stopped) return;
      fresh = event.code === UNAUTHORIZED;
      if (fresh) authFailures += 1;
      options.onConnection(fresh ? 'unauthorized' : 'offline');
      retry();
    };
  }

  void connect();
  return () => {
    stopped = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
