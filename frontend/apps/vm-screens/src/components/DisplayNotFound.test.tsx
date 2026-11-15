import { act, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DisplayNotFound, DisplayOutput } from './DisplayNotFound';
import { DisplayRouteError } from './DisplayRouteError';

const reload = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  reload.mockReset();
  vi.stubGlobal('location', { ...window.location, reload });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** main.tsx 의 차량 화면 라우트 중 없는 주소와 관련된 부분 */
function open(path: string) {
  const router = createMemoryRouter(
    [
      {
        errorElement: <DisplayRouteError />,
        children: [
          { path: '/display/combined', element: <p>합친 차량 화면</p> },
          { path: '/display/:outputId', element: <DisplayOutput /> },
          { path: '*', element: <DisplayNotFound /> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const at = (router: ReturnType<typeof open>) => router.state.location.pathname + router.state.location.search;

describe('차량 화면 없는 주소 (S15P31E110-472)', () => {
  it.each(['/display/nope', '/no/such/screen'])(
    '%s 는 한국어 안내와 요청한 주소를 보이고, 20초가 지나도 새로고침·이동하지 않는다',
    (path) => {
      const router = open(path);
      expect(screen.getByRole('alert')).toHaveTextContent('없는 화면 주소입니다.');
      expect(screen.getByRole('alert')).toHaveTextContent(path);
      act(() => vi.advanceTimersByTime(20_000));
      expect(reload).not.toHaveBeenCalled();
      expect(at(router)).toBe(path);
    },
  );

  it('쿼리(토큰 등)는 화면에 내지 않는다', () => {
    open('/nope?token=secret-token');
    expect(screen.getByRole('alert')).not.toHaveTextContent('secret-token');
  });

  it.each(['monitor-1', 'monitor-2'])(
    '옛 모니터 주소 /display/%s 는 쿼리를 들고 합친 화면으로 간다',
    (id) => {
      const router = open(`/display/${id}?display=1`);
      expect(at(router)).toBe('/display/combined?display=1');
      expect(screen.getByText('합친 차량 화면')).toBeInTheDocument();
    },
  );
});
