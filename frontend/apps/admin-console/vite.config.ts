import { rmSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { dropMockWorker } from './src/mocks/dropMockWorker';

// 개발 서버는 /api·/ws 를 백엔드로 넘긴다 (CORS 없이 쿠키·토큰 그대로).
// /monitor/ 는 모니터 화면 개발 서버(VITE_BASE=/monitor/ 로 띄운 vm-screens)로 넘긴다 — 같은 origin 이라
// 모니터가 콘솔 mock 시나리오를 브라우저 채널로 따라갈 수 있다 (README "모니터를 콘솔과 같이 보기").
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const backend = env.VITE_BACKEND_ORIGIN || 'http://localhost:8080';
  const monitor = env.VITE_MONITOR_ORIGIN || 'http://localhost:5174';
  // 셸 환경변수(Dockerfile ENV 등)가 .env 파일보다 우선한다 — Vite 의 import.meta.env 와 같은 순서
  const mock = (process.env.VITE_MOCK ?? env.VITE_MOCK) === 'true';
  return {
    plugins: [
      react(),
      // 운영 빌드 dist 에는 MSW 워커를 남기지 않는다 (S15P31E110-470)
      dropMockWorker({
        mock,
        remove: (root, outDir, file) => rmSync(resolve(root, outDir, file), { force: true }),
      }),
    ],
    server: {
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/ws': { target: backend.replace(/^http/, 'ws'), ws: true, changeOrigin: true },
        '/monitor': { target: monitor, ws: true },
      },
    },
  };
});
