import { render, screen } from '@testing-library/react';
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CHUNK_RELOAD_KEY, isChunkLoadError } from './chunkReload';
import { RouteError } from './RouteError';

function Boom({ error }: { error: Error }): never {
  throw error;
}

/** main.tsx 와 같은 모양: 레이아웃(메뉴) 아래 pathless 자식이 errorElement 를 갖는다 */
function renderAt(error: Error) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <div>
            <nav>메뉴</nav>
            <Outlet />
          </div>
        ),
        errorElement: <RouteError />,
        children: [
          { errorElement: <RouteError />, children: [{ path: '/x', element: <Boom error={error} /> }] },
        ],
      },
    ],
    { initialEntries: ['/x'] },
  );
  return render(<RouterProvider router={router} />);
}

const reload = vi.fn();

beforeEach(() => {
  sessionStorage.clear();
  reload.mockReset();
  vi.stubGlobal('location', { ...window.location, reload });
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('화면 오류 처리 (QA 2-1·3-A2)', () => {
  it('화면이 렌더 중 예외를 내도 메뉴는 남고 한국어 안내와 [새로고침]·[Overview로] 를 보인다', () => {
    renderAt(new TypeError("Cannot read properties of undefined (reading 'apps')"));
    expect(screen.getByText('메뉴')).toBeInTheDocument();
    expect(screen.getByText('화면을 표시하지 못했습니다.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '새로고침' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Overview로' })).toHaveAttribute('href', '/dashboard/overview');
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
    expect(screen.queryByText(/Cannot read properties/)).toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it('배포 직후 chunk 를 못 받으면 한 번만 자동 새로고침하고, 또 실패하면 안내 화면', () => {
    const chunk = new TypeError('Failed to fetch dynamically imported module: /assets/ConsolePanel-abc.js');
    renderAt(chunk);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(CHUNK_RELOAD_KEY)).not.toBeNull();

    reload.mockReset();
    renderAt(chunk);
    expect(reload).not.toHaveBeenCalled();
    expect(screen.getAllByText(/새 버전이 배포되었을 수 있습니다/).length).toBeGreaterThan(0);
  });

  it('chunk 로드 실패 문구를 브라우저별로 알아본다', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module: x'))).toBe(true);
    expect(isChunkLoadError(new TypeError('Importing a module script failed.'))).toBe(true);
    expect(isChunkLoadError(new TypeError('error loading dynamically imported module: x'))).toBe(true);
    expect(isChunkLoadError(new Error('Unable to preload CSS for /assets/a.css'))).toBe(true);
    expect(isChunkLoadError(new TypeError('x is undefined'))).toBe(false);
    expect(isChunkLoadError('nope')).toBe(false);
  });
});
