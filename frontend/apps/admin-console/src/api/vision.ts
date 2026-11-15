import { getSession, refreshSession } from './client';

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

/** Latest Jetson camera JPEG through the backend (the browser never talks to the Jetson). */
export type VisionFrame =
  | { kind: 'frame'; blob: Blob }
  | { kind: 'unavailable'; code: 'VISION_NOT_CONFIGURED' | 'VISION_UNAVAILABLE' | 'NETWORK' | 'AUTH' };

async function get(signal?: AbortSignal): Promise<Response> {
  const session = getSession();
  return fetch(`${BASE}/api/v1/vision/snapshot`, {
    headers: {
      Accept: 'image/jpeg, application/json',
      ...(session ? { Authorization: `Bearer ${session.access_token}` } : {}),
    },
    credentials: 'include',
    cache: 'no-store',
    signal,
  });
}

export async function fetchVisionFrame(signal?: AbortSignal): Promise<VisionFrame> {
  let response: Response;
  try {
    response = await get(signal);
    if (response.status === 401 && (await refreshSession())) response = await get(signal);
  } catch (error) {
    if ((error as Error).name === 'AbortError') throw error;
    return { kind: 'unavailable', code: 'NETWORK' };
  }
  if (response.ok && (response.headers.get('Content-Type') ?? '').startsWith('image/jpeg')) {
    return { kind: 'frame', blob: await response.blob() };
  }
  if (response.status === 401) return { kind: 'unavailable', code: 'AUTH' };
  const body = (await response.json().catch(() => ({}))) as { code?: string };
  return {
    kind: 'unavailable',
    code: body.code === 'VISION_NOT_CONFIGURED' ? 'VISION_NOT_CONFIGURED' : 'VISION_UNAVAILABLE',
  };
}
