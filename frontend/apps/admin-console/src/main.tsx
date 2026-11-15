import '@xenova/ui/tokens.css';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';
import { ApiError } from './api/client';
import { consoleRoutes } from './routes';

// 관리자 콘솔에만 XENOVA 브랜드 테마를 적용한다. HDMI 화면은 기존 테마를 유지한다.
document.documentElement.dataset.theme = 'xenova';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 인증·검증 오류는 다시 시도해도 같다. 연결·서버 오류만 한 번 더
      retry: (count, error) =>
        count < 1 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
    },
  },
});

const router = createBrowserRouter(consoleRoutes());

async function start() {
  // VITE_MOCK=true 면 MSW 가 /api 를 대신 답한다 (src/mocks, 사용법은 frontend/README.md)
  if (import.meta.env.VITE_MOCK === 'true') {
    const { startMock } = await import('./mocks/browser');
    await startMock();
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <RouterProvider router={router} />
        </ToastProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}

void start();
