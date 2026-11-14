import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { showcaseRoutes } from './showcaseRoutes';

vi.mock('./pages/ShowcasePage', () => ({ ShowcasePage: () => <h1>컴포넌트 견본</h1> }));

function renderAt(path: string, enabled: boolean) {
  const { console: inConsole, standalone } = showcaseRoutes(enabled);
  const router = createMemoryRouter(
    [inConsole, standalone, { path: '/dashboard/overview', element: <h1>콘솔 첫 화면</h1> }],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe('컴포넌트 견본 라우트 (S15P31E110-470)', () => {
  it.each(['/components', '/components-public'])(
    '운영 빌드: %s 는 콘솔 첫 화면으로 넘어간다',
    async (path) => {
      const router = renderAt(path, false);
      expect(await screen.findByRole('heading', { name: '콘솔 첫 화면' })).toBeInTheDocument();
      expect(router.state.location.pathname).toBe('/dashboard/overview');
      expect(screen.queryByText('컴포넌트 견본')).toBeNull();
    },
  );

  it.each(['/components', '/components-public'])('개발·mock 모드: %s 는 견본 화면이 열린다', async (path) => {
    const router = renderAt(path, true);
    expect(await screen.findByRole('heading', { name: '컴포넌트 견본' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(path);
  });

  it('기본값은 테스트(개발) 환경이라 열린다', () => {
    expect(showcaseRoutes().standalone.lazy).toBeTypeOf('function');
  });
});
