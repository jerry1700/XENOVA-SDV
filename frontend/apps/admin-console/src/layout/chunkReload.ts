/** 배포로 파일 이름이 바뀐 뒤 열린 탭이 옛 chunk 를 찾을 때의 오류 (브라우저마다 문구가 다르다) */
const CHUNK_ERROR =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS|ChunkLoadError/i;

export const CHUNK_RELOAD_KEY = 'xenova.chunk-reload-at';
/** 이 시간 안에 또 chunk 오류가 나면 새로고침해도 소용없는 것 — 무한 새로고침을 막는다 */
const CHUNK_RELOAD_WINDOW_MS = 30_000;

export function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && CHUNK_ERROR.test(`${error.name}: ${error.message}`);
}

/** 방금 자동 새로고침을 했는가 (읽기만 — 렌더 중에 불러도 된다). 저장소를 못 쓰면 했다고 본다(무한 반복 방지) */
export function reloadedRecently(now = Date.now()): boolean {
  try {
    const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY));
    return Boolean(last) && now - last < CHUNK_RELOAD_WINDOW_MS;
  } catch {
    return true;
  }
}

export function markReload(now = Date.now()) {
  try {
    sessionStorage.setItem(CHUNK_RELOAD_KEY, String(now));
  } catch {
    // reloadedRecently 가 true 를 돌려 다시 오지 않는다
  }
}
