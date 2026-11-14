import '@xenova/ui/tokens.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, Navigate, Outlet, RouterProvider, useLocation } from 'react-router';
import { DevControls } from './components/DevControls';
import { DisplayNotFound, DisplayOutput } from './components/DisplayNotFound';
import { DisplayRouteError } from './components/DisplayRouteError';
import { DisplayFollower } from './components/DisplayFollower';
import { ResumeOverlay } from './components/ResumeOverlay';
import { DetailPage } from './pages/DetailPage';
import { SimPage } from './pages/SimPage';
import { SplitPage } from './pages/SplitPage';
import {
  ConsoleBusTelemetryProvider,
  LiveTelemetryProvider,
  MockTelemetryProvider,
} from './telemetry/TelemetryProvider';

/** Default/display = local HDMI. Explicit live/console/scenario = legacy app preview. */
const PROVIDERS = { live: LiveTelemetryProvider, scenario: MockTelemetryProvider };

// Production HDMI uses the local collector/policy. Legacy app views are explicit previews.
const legacyPreview = ['console', 'scenario', 'live', 'legacy-live'].includes(import.meta.env.VITE_TELEMETRY);

function ScreenEntry({ detail = false }: { detail?: boolean }) {
  if (legacyPreview) return detail ? <DetailPage /> : <SplitPage />;
  return <ToCombined />;
}

/**
 * 옛 모니터 주소(/display/monitor-1·monitor-2 등) → 합친 차량 화면. 모니터 2대 화면은 /display/combined 로 바뀌었다.
 * 키오스크·수집기가 아직 옛 주소를 열어도 화면이 깨지지 않게 쿼리스트링(토큰 등)은 그대로 넘긴다
 */
function ToCombined() {
  const { search } = useLocation();
  return <Navigate to={{ pathname: '/display/combined', search }} replace />;
}

function Root() {
  if (!legacyPreview) return <Outlet />;
  const configured = import.meta.env.VITE_TELEMETRY;
  const mode = (configured === 'legacy-live' ? 'live' : configured) as keyof typeof PROVIDERS | undefined;
  const Provider = (mode && PROVIDERS[mode]) || ConsoleBusTelemetryProvider;
  return (
    <Provider>
      <div role="note">앱 시연 화면 · HDMI 실제 상태/주·백업 승계 검증용이 아닙니다.</div>
      <DisplayFollower />
      <Outlet />
      <ResumeOverlay />
      <DevControls />
    </Provider>
  );
}

/** / → /split. ?display=·?kiosk= 는 남긴다 (모니터 번호를 잃으면 화면 배치를 못 따라간다) */
function ToSplit() {
  const { search } = useLocation();
  return <Navigate to={{ pathname: '/split', search }} replace />;
}

const router = createBrowserRouter(
  [
    {
      // 렌더 예외·배포 직후 chunk 실패에 기본 영어 오류 화면 대신 (QA 2-1)
      errorElement: <DisplayRouteError />,
      children: [
        // 휴대폰과 HDMI는 같은 합친 차량 화면을 띄운다 (S15P31E110-350).
        // 휴대폰 접속·VM3 기본 설정·이미지 점검이 이 주소를 쓰므로 경로는 그대로 두고, 주소를 바꾸지 않게 이동 없이 바로 그린다
        {
          path: '/display/mobile',
          lazy: async () => ({ Component: (await import('./display/CombinedPage')).CombinedPage }),
        },
        // 모니터 2대(계기판·인포테인먼트)를 한 화면으로 합친 차량 화면 (S15P31E110-128, 미리보기 단계)
        {
          path: '/display/combined',
          lazy: async () => ({ Component: (await import('./display/CombinedPage')).CombinedPage }),
        },
        // 옛 모니터 주소(monitor-N)만 합친 화면으로, 나머지 /display/… 는 없는 화면 주소 안내 (S15P31E110-472)
        { path: '/display/:outputId', element: <DisplayOutput /> },
        {
          element: <Root />,
          children: [
            { path: '/', element: <ToSplit /> },
            { path: '/split', element: <ScreenEntry /> },
            { path: '/detail/:app', element: <ScreenEntry detail /> },
            // 화면 배치 view sim (screen_path /sim, 화면 배치 계약 v1.1)
            { path: '/sim', element: legacyPreview ? <SimPage /> : <ToCombined /> },
          ],
        },
        // 없는 주소는 새로고침하지 않는 안내 (예전에는 오류 화면이 10초마다 새로고침을 반복했다)
        { path: '*', element: <DisplayNotFound /> },
      ],
    },
  ],
  // 관리 콘솔 개발 서버의 /monitor/ 아래로 열 때 (VITE_BASE=/monitor/)
  { basename: import.meta.env.BASE_URL.replace(/\/$/, '') || '/' },
);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
