import { Navigate, type RouteObject } from 'react-router';

/** 개발 서버이거나 mock 빌드일 때만 개발용 화면을 연다 (S15P31E110-470) */
export const DEV_TOOLS = import.meta.env.DEV || import.meta.env.VITE_MOCK === 'true';

const showcase = (path: string, enabled: boolean): RouteObject =>
  enabled
    ? {
        path,
        // DEV_TOOLS 는 빌드 상수라 운영 빌드에서는 견본 화면 import 가 번들(청크)에서 빠진다
        lazy: async () =>
          DEV_TOOLS
            ? { Component: (await import('./pages/ShowcasePage')).ShowcasePage }
            : { element: <Navigate to="/dashboard/overview" replace /> },
      }
    : // 운영 빌드: 개발용 견본 화면 대신 콘솔 첫 화면으로
      { path, element: <Navigate to="/dashboard/overview" replace /> };

/**
 * 컴포넌트 견본(ShowcasePage) 주소.
 * - console: 콘솔 레이아웃 안 /components
 * - standalone: 서버 연결 없이 디자인만 보는 /components-public (레이아웃·자동 로그인 밖)
 */
export function showcaseRoutes(enabled: boolean = DEV_TOOLS): {
  console: RouteObject;
  standalone: RouteObject;
} {
  return { console: showcase('/components', enabled), standalone: showcase('/components-public', enabled) };
}
