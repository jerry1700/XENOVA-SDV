import type { ResolvedConfig } from 'vite';
import { describe, expect, it, vi } from 'vitest';
import { dropMockWorker, MOCK_WORKER_FILE } from './dropMockWorker';

/** 빌드 한 번을 흉내 낸다: 설정 확정 → 번들 끝 */
function build(mock: boolean) {
  const remove = vi.fn();
  const plugin = dropMockWorker({ mock, remove });
  const config = { root: '/app', build: { outDir: 'dist' } } as ResolvedConfig;
  (plugin.configResolved as (c: ResolvedConfig) => void)(config);
  (plugin.closeBundle as () => void)();
  return { plugin, remove };
}

describe('운영 빌드 MSW 워커 제외 (S15P31E110-470)', () => {
  it('운영 빌드(VITE_MOCK≠true)는 빌드 끝에 dist 의 워커를 지운다', () => {
    const { remove } = build(false);
    expect(remove).toHaveBeenCalledWith('/app', 'dist', MOCK_WORKER_FILE);
    expect(MOCK_WORKER_FILE).toBe('mockServiceWorker.js');
  });

  it('mock 빌드(VITE_MOCK=true)는 워커를 남긴다', () => {
    expect(build(true).remove).not.toHaveBeenCalled();
  });

  it('빌드에서만 동작한다 (개발 서버는 public/ 을 그대로 제공)', () => {
    expect(build(false).plugin.apply).toBe('build');
  });
});
