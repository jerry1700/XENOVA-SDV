/**
 * fetch 래퍼. Access Token 을 붙이고, 401 이면 한 번 재발급(쿠키) 후 다시 시도한다.
 * 인증 흐름은 노션 API 명세 "로그인"·"토큰 재발급"을 따른다.
 */
import type { ApiErrorBody, TokenResponse } from './types';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fieldErrors: ApiErrorBody['errors'];

  constructor(status: number, body: Partial<ApiErrorBody>) {
    super(body.message || `요청이 실패했습니다 (${status})`);
    this.status = status;
    this.code = body.code || (status === 0 ? 'NETWORK_ERROR' : 'UNKNOWN_ERROR');
    this.fieldErrors = body.errors;
  }
}

// ---- 세션 (Access Token 은 메모리에만 둔다. 새로고침하면 refresh 쿠키로 다시 받는다) ----

type Listener = (session: TokenResponse | null) => void;
let session: TokenResponse | null = null;
const listeners = new Set<Listener>();

export function getSession(): TokenResponse | null {
  return session;
}

export function setSession(next: TokenResponse | null): void {
  session = next;
  listeners.forEach((listener) => listener(next));
}

export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// ---- 요청 -----------------------------------------------------------------------------

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  headers?: Record<string, string>;
  /** 인증 API(/api/v1/auth/**)는 토큰을 붙이지 않고 재발급도 하지 않는다 */
  auth?: boolean;
  signal?: AbortSignal;
}

async function parse<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (options.auth !== false && session) headers.Authorization = `Bearer ${session.access_token}`;
  try {
    return await fetch(`${BASE}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: 'include', // refresh 쿠키 (Path=/api/v1/auth)
      signal: options.signal,
    });
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    throw new ApiError(0, { message: '서버에 연결할 수 없습니다.' });
  }
}

// ---- 탭 사이 재발급 (QA 2-2) ---------------------------------------------------------------
// 서버는 refresh 토큰을 쓸 때마다 회전시키고, 이미 쓴 토큰이 다시 오면 재사용으로 보고 그 계정의 토큰을 전부 폐기한다.
// 탭 여러 개가 동시에 새로고침하면 같은 쿠키로 refresh 가 겹쳐 모든 탭이 로그아웃됐다.
// → navigator.locks 로 탭 사이를 줄 세우고, 먼저 받은 탭이 새 토큰을 BroadcastChannel 로 나눈다.
//   락을 기다린 탭은 그 사이 받은 토큰이 있으면 refresh 를 보내지 않는다. 둘 중 하나가 없는 브라우저는 예전 동작.

const REFRESH_LOCK = 'xenova-refresh';
type SessionMessage = { type: 'refreshed'; session: TokenResponse };

const sessionChannel: BroadcastChannel | null =
  typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('xenova-session');
/** 다른 탭이 나눈 토큰을 마지막으로 받은 순번 (재발급을 기다리는 동안 받았는지 가른다) */
let sharedSeq = 0;

if (sessionChannel) {
  sessionChannel.onmessage = (event: MessageEvent<SessionMessage>) => {
    if (event.data?.type !== 'refreshed') return;
    sharedSeq += 1;
    setSession(event.data.session);
  };
}

let refreshing: Promise<boolean> | null = null;

// ---- 공용 계정 자동 로그인 ------------------------------------------------------------------
// 콘솔은 사용자 계정 없이 누구나 쓴다. 백엔드는 여전히 토큰을 요구하므로, 화면 없이 공용 계정으로 로그인해
// 토큰을 받는다. refresh 가 실패하면(쿠키 없음·만료·재사용 폐기) 로그인 화면 대신 다시 공용 계정으로 로그인한다.
// 값은 빌드 환경변수로 바꿀 수 있다. 누구나 쓰는 계정이라 비밀이 아니다(번들에 들어간다).
export const SHARED_ACCOUNT = {
  username: import.meta.env.VITE_SHARED_USERNAME || 'xenova',
  password: import.meta.env.VITE_SHARED_PASSWORD || 'xenova-console',
} as const;

async function sharedLogin(): Promise<TokenResponse | null> {
  const response = await send('/api/v1/auth/login', {
    method: 'POST',
    body: { username: SHARED_ACCOUNT.username, password: SHARED_ACCOUNT.password },
    auth: false,
  });
  return response.ok ? parse<TokenResponse>(response) : null;
}

async function refreshOnce(seqAtStart: number): Promise<boolean> {
  // 락을 기다리는 동안 다른 탭이 재발급해 토큰을 나눴다 — 같은 쿠키로 또 보내면 재사용 판정이 난다
  if (sharedSeq !== seqAtStart && session) return true;
  try {
    const response = await send('/api/v1/auth/refresh', { method: 'POST', auth: false });
    const next = response.ok ? await parse<TokenResponse>(response) : await sharedLogin();
    if (!next) {
      setSession(null);
      return false;
    }
    setSession(next);
    sessionChannel?.postMessage({ type: 'refreshed', session: next } satisfies SessionMessage);
    return true;
  } catch {
    setSession(null);
    return false;
  }
}

/** 동시에 여러 요청이 401 을 받아도 재발급은 한 번만 (서버가 토큰을 회전시키므로) — 탭 사이도 한 번에 하나 */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    const seqAtStart = sharedSeq;
    try {
      const locks = typeof navigator === 'undefined' ? undefined : navigator.locks;
      return locks
        ? await locks.request(REFRESH_LOCK, () => refreshOnce(seqAtStart))
        : await refreshOnce(seqAtStart);
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await requestWithStatus<T>(path, options)).data;
}

/** request 와 같고 성공 상태 코드를 함께 준다 — 같은 요청의 200(기존 것)과 201(새로 만듦)을 화면이 가를 때 */
export async function requestWithStatus<T>(
  path: string,
  options: RequestOptions = {},
): Promise<{ data: T; status: number }> {
  let response = await send(path, options);
  if (response.status === 401 && options.auth !== false && (await refreshSession())) {
    response = await send(path, options);
  }
  if (!response.ok) {
    const body = await parse<Partial<ApiErrorBody>>(response).catch(() => ({}));
    if (response.status === 401 && options.auth !== false) setSession(null);
    throw new ApiError(response.status, body ?? {});
  }
  return { data: await parse<T>(response), status: response.status };
}
