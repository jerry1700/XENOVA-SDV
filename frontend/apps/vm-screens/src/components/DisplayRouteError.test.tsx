import { act, render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DisplayRouteError } from './DisplayRouteError';
import { RELOAD_AFTER_MS } from './displayReload';

function Boom(): never {
  throw new TypeError("Cannot read properties of undefined (reading 'drive')");
}

const reload = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  reload.mockReset();
  vi.stubGlobal('location', { ...window.location, reload });
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('차량 화면 오류 처리 (QA 2-1·3-A2)', () => {
  it('렌더 예외면 한국어 안내를 보이고 잠시 뒤 스스로 새로고침한다 (키오스크는 사람이 못 누른다)', () => {
    const router = createMemoryRouter(
      [{ path: '/display/combined', element: <Boom />, errorElement: <DisplayRouteError /> }],
      { initialEntries: ['/display/combined'] },
    );
    render(<RouterProvider router={router} />);
    expect(screen.getByText('차량 화면을 다시 불러오는 중입니다')).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
    expect(screen.queryByText(/Cannot read properties/)).toBeNull();

    act(() => vi.advanceTimersByTime(RELOAD_AFTER_MS - 100));
    expect(reload).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(200));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
