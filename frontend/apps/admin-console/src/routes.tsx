import { Navigate, type RouteObject } from 'react-router';
import { RequireAuth } from './auth';
import { ConsoleLayout } from './layout/ConsoleLayout';
import { DashboardLayout } from './layout/DashboardLayout';
import { KeepQueryRedirect } from './layout/KeepQueryRedirect';
import { RouteError } from './layout/RouteError';
import { ConsolePage } from './pages/ConsolePage';
import { ControlVmDetailPage } from './pages/ControlVmDetailPage';
import { DashboardPage, LegacyDashboardPage } from './pages/DashboardPage';
import { DemoPage } from './pages/DemoPage';
import { EventsPage } from './pages/EventsPage';
import { PhoneLinkPage } from './pages/PhoneLinkPage';
import { ResourcesPage } from './pages/ResourcesPage';
import { SimulatorStressLab } from './pages/SimulatorStressLab';
import { VmCreatePage } from './pages/VmCreatePage';
import { VmDetailPage } from './pages/VmDetailPage';
import { VmListPage } from './pages/VmListPage';
import { VisionPage } from './pages/VisionPage';
import { showcaseRoutes } from './showcaseRoutes';
import { NotFoundPage } from './layout/NotFoundPage';

/** 콘솔 라우트 표. main.tsx 가 브라우저 라우터로, 테스트가 메모리 라우터로 같은 표를 쓴다 */
export function consoleRoutes(): RouteObject[] {
  // 컴포넌트 견본은 개발 서버·mock 빌드에서만 연다. 운영 빌드는 첫 화면으로 보낸다 (S15P31E110-470)
  const showcase = showcaseRoutes();

  return [
    {
      // 레이아웃 밖(레이아웃 자체)의 오류 — 기본 "Unexpected Application Error!" 대신 (QA 2-1)
      errorElement: <RouteError />,
      children: [
        // 계정 없는 콘솔: 예전 로그인·회원가입 주소는 첫 화면으로 보낸다
        { path: '/login', element: <Navigate to="/dashboard/overview" replace /> },
        { path: '/signup', element: <Navigate to="/dashboard/overview" replace /> },
        {
          element: (
            <RequireAuth>
              <ConsoleLayout />
            </RequireAuth>
          ),
          children: [
            {
              // 화면 하나의 오류는 메뉴(ConsoleLayout)를 남긴 채 이 자리에 안내한다
              errorElement: <RouteError />,
              children: [
                { path: '/', element: <Navigate to="/dashboard/overview" replace /> },
                {
                  path: '/dashboard',
                  element: <DashboardLayout />,
                  children: [
                    { index: true, element: <Navigate to="/dashboard/overview" replace /> },
                    { path: 'overview', element: <DashboardPage /> },
                    { path: 'cpu', element: <ResourcesPage view="cpu" /> },
                    { path: 'memory', element: <ResourcesPage view="memory" /> },
                    { path: 'network', element: <ResourcesPage view="network" /> },
                    { path: 'console', element: <ConsolePage /> },
                    { path: 'events', element: <EventsPage /> },
                  ],
                },
                { path: '/dashboard/vms/:vmId', element: <VmDetailPage /> },
                { path: '/vision', element: <VisionPage /> },
                { path: '/control', element: <VmListPage /> },
                { path: '/control/vms/new', element: <VmCreatePage /> },
                { path: '/control/vms/:vmId', element: <ControlVmDetailPage /> },
                { path: '/simulator', element: <DemoPage /> },
                { path: '/simulator/stress', element: <SimulatorStressLab /> },
                // 실차가 안 될 때 쓰는 예비 대시보드 (주·백업 VM 카드 + 시뮬레이터). 메뉴에는 없다
                { path: '/legacy/dashboard', element: <LegacyDashboardPage /> },

                // 기존 북마크와 외부 링크를 깨지 않도록 새 정보구조로 보낸다.
                { path: '/vms', element: <Navigate to="/control" replace /> },
                { path: '/vms/new', element: <Navigate to="/control/vms/new" replace /> },
                { path: '/vms/:vmId', element: <VmDetailPage /> },
                { path: '/resources', element: <Navigate to="/dashboard/cpu" replace /> },
                { path: '/demo', element: <Navigate to="/simulator" replace /> },
                { path: '/console', element: <Navigate to="/dashboard/console" replace /> },
                { path: '/events', element: <KeepQueryRedirect to="/dashboard/events" /> },
                { path: '/phone-link', element: <PhoneLinkPage /> },
                showcase.console,
                // 없는 주소: 메뉴를 남긴 채 안내 (예전에는 말없이 Overview 로 보냈다, S15P31E110-472)
                { path: '*', element: <NotFoundPage /> },
              ],
            },
          ],
        },
        // 디자인 확인은 서버 연결 없이도 볼 수 있게 (개발 서버·mock 빌드에서만)
        showcase.standalone,
      ],
    },
  ];
}
