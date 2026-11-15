import { act, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, Outlet, RouterProvider, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setSession } from './api/client';
import type { TokenResponse } from './api/types';
import { RequireAuth } from './auth';

const refreshSession = vi.hoisted(() => vi.fn<() => Promise<boolean>>());
vi.mock('./api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api/client')>()),
  refreshSession,
}));

const token: TokenResponse = {
  access_token: 'a',
  token_type: 'Bearer',
  expires_in: 900,
  expires_at: '2026-11-15T00:15:00Z',
  user: { user_id: 'u-1', username: 'xenova', nickname: '공용' },
};

function Page() {
  const location = useLocation();
  return <p>화면 {location.pathname + location.search}</p>;
}

function renderConsole(path = '/control/vms/vm-a?tab=resources') {
  const router = createMemoryRouter(
    [
      {
        element: (
          <RequireAuth>
            <Outlet />
          </RequireAuth>
        ),
        children: [{ path: '*', element: <Page /> }],
      },
    ],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

/** 타이머를 ms 만큼 진행하고 그 사이 끝난 refreshSession 결과까지 반영한다 */
const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
const status = () => screen.getByRole('status').textContent;

beforeEach(() => {
  vi.useFakeTimers();
  refreshSession.mockReset();
  refreshSession.mockResolvedValue(false);
});
afterEach(() => {
  act(() => setSession(null));
  vi.useRealTimers();
});

describe('세션이 끊기면 안내와 자동 재시도 (S15P31E110-472)', () => {
  it('쓰는 중 끊기면 콘솔 전체를 안내로 바꾸고 2→5→10→30초(반복) 간격으로 다시 연결한다', async () => {
    act(() => setSession(token));
    renderConsole();
    expect(screen.getByText('화면 /control/vms/vm-a?tab=resources')).toBeInTheDocument();

    act(() => setSession(null));
    expect(screen.getByRole('alert')).toHaveTextContent('콘솔 연결이 끊겼습니다.');
    expect(screen.queryByText(/^화면 /)).toBeNull();
    await advance(0);
    // 방금 재발급이 실패해 끊긴 것이므로 바로 다시 보내지 않고 첫 간격을 기다린다
    expect(refreshSession).not.toHaveBeenCalled();
    expect(status()).toBe('2초 뒤 자동으로 다시 연결합니다.');

    const expected: [number, string][] = [
      [2_000, '5초'],
      [5_000, '10초'],
      [10_000, '30초'],
      [30_000, '30초'],
      [30_000, '30초'],
    ];
    for (const [index, [wait, next]] of expected.entries()) {
      await advance(wait - 1);
      expect(refreshSession).toHaveBeenCalledTimes(index);
      await advance(1);
      expect(refreshSession).toHaveBeenCalledTimes(index + 1);
      expect(status()).toBe(`${next} 뒤 자동으로 다시 연결합니다.`);
    }
    expect(screen.getByRole('alert')).toHaveTextContent('콘솔 연결이 끊겼습니다.');
  });

  it('다시 연결되면 보던 주소의 화면으로 돌아온다', async () => {
    act(() => setSession(token));
    const router = renderConsole();
    act(() => setSession(null));
    refreshSession
      .mockImplementationOnce(async () => false)
      .mockImplementationOnce(async () => {
        setSession(token);
        return true;
      });

    await advance(2_000);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    await advance(5_000);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('화면 /control/vms/vm-a?tab=resources')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/control/vms/vm-a');
  });

  it('[다시 연결]은 바로 다시 시도하고 간격을 처음(2초)부터 다시 센다', async () => {
    act(() => setSession(token));
    renderConsole();
    act(() => setSession(null));
    await advance(2_000);
    await advance(5_000);
    expect(refreshSession).toHaveBeenCalledTimes(2);
    expect(status()).toBe('10초 뒤 자동으로 다시 연결합니다.');

    fireEvent.click(screen.getByRole('button', { name: '다시 연결' }));
    await advance(0);
    expect(refreshSession).toHaveBeenCalledTimes(3);
    expect(status()).toBe('2초 뒤 자동으로 다시 연결합니다.');
    // 예전 10초 타이머는 정리되어 2초 뒤 한 번만 더 시도한다
    await advance(2_000);
    expect(refreshSession).toHaveBeenCalledTimes(4);
  });

  it('처음 연결이 실패하면 연결 실패 안내를 보이고 같은 간격으로 다시 시도한다', async () => {
    renderConsole('/dashboard/overview');
    expect(screen.getByText('콘솔에 연결하는 중…')).toBeInTheDocument();
    await advance(0);
    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toHaveTextContent('콘솔 서버에 연결하지 못했습니다.');
    expect(status()).toBe('2초 뒤 자동으로 다시 연결합니다.');
    await advance(2_000);
    expect(refreshSession).toHaveBeenCalledTimes(2);
  });
});
