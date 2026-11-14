import type { Plugin } from 'vite';

/** msw 가 public/ 에 두는 워커 파일 (package.json msw.workerDirectory) */
export const MOCK_WORKER_FILE = 'mockServiceWorker.js';

/**
 * 운영 빌드 dist 에서 MSW 워커를 지운다 (S15P31E110-470).
 * public/ 은 빌드 때 그대로 복사되므로, mock 빌드(VITE_MOCK=true)가 아니면 빌드 끝에 지운다.
 * 개발 서버는 public/ 을 직접 제공하므로 영향이 없다 (apply: 'build').
 * remove 는 vite.config 가 node fs 로 넘긴다 (앱 코드는 node 타입을 쓰지 않는다).
 */
export function dropMockWorker(options: {
  mock: boolean;
  remove: (root: string, outDir: string, file: string) => void;
}): Plugin {
  let root = '';
  let outDir = 'dist';
  return {
    name: 'xenova-drop-mock-worker',
    apply: 'build',
    configResolved(config) {
      root = config.root;
      outDir = config.build.outDir;
    },
    closeBundle() {
      if (options.mock) return;
      options.remove(root, outDir, MOCK_WORKER_FILE);
    },
  };
}
