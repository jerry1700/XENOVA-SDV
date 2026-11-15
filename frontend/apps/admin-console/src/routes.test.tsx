import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { consoleRoutes } from './routes';

// 라우트 표만 본다 — 화면·레이아웃·인증은 이름만 보이는 가짜로
const page = vi.hoisted(() => (name: string) => () => <p>화면 {name}</p>);
vi.mock('./auth', () => ({ RequireAuth: ({ children }: { children: ReactNode }) => children }));
vi.mock('./layout/ConsoleLayout', () => ({
  ConsoleLayout: () => (
    <>
      <nav>콘솔 메뉴</nav>
      <Outlet />
    </>
  ),
}));
vi.mock('./layout/DashboardLayout', () => ({ DashboardLayout: () => <Outlet /> }));
vi.mock('./pages/DashboardPage', () => ({
  DashboardPage: page('overview'),
  LegacyDashboardPage: page('legacy'),
}));
vi.mock('./pages/ResourcesPage', () => ({
  ResourcesPage: ({ view }: { view: string }) => <p>화면 resources-{view}</p>,
}));
vi.mock('./pages/ConsolePage', () => ({ ConsolePage: page('console') }));
vi.mock('./pages/EventsPage', () => ({ EventsPage: page('events') }));
vi.mock('./pages/VmDetailPage', () => ({ VmDetailPage: page('vm-detail') }));
vi.mock('./pages/VisionPage', () => ({ VisionPage: page('vision') }));
vi.mock('./pages/VmListPage', () => ({ VmListPage: page('control') }));
vi.mock('./pages/VmCreatePage', () => ({ VmCreatePage: page('vm-create') }));
vi.mock('./pages/ControlVmDetailPage', () => ({ ControlVmDetailPage: page('control-detail') }));
vi.mock('./pages/DemoPage', () => ({ DemoPage: page('simulator') }));
vi.mock('./pages/SimulatorStressLab', () => ({ SimulatorStressLab: page('stress') }));
vi.mock('./pages/PhoneLinkPage', () => ({ PhoneLinkPage: page('phone-link') }));

function open(path: string) {
  const router = createMemoryRouter(consoleRoutes(), { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const at = (router: ReturnType<typeof open>) => router.state.location.pathname + router.state.location.search;

describe('없는 주소 (S15P31E110-472)', () => {
  it('메뉴를 남긴 채 "없는 주소입니다." 와 요청한 주소를 보이고, 주소를 바꾸지 않는다', () => {
    const router = open('/no/such/page');
    expect(screen.getByText('콘솔 메뉴')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('없는 주소입니다.');
    expect(screen.getByRole('alert')).toHaveTextContent('/no/such/page');
    expect(at(router)).toBe('/no/such/page');
  });

  it('[Overview로 이동]을 누르면 Overview 로 간다', () => {
    const router = open('/typo');
    fireEvent.click(screen.getByRole('button', { name: 'Overview로 이동' }));
    expect(at(router)).toBe('/dashboard/overview');
    expect(screen.getByText('화면 overview')).toBeInTheDocument();
  });
});

describe('옛 주소 이동은 그대로 (S15P31E110-472)', () => {
  it.each([
    ['/', '/dashboard/overview'],
    ['/dashboard', '/dashboard/overview'],
    ['/login', '/dashboard/overview'],
    ['/signup', '/dashboard/overview'],
    ['/vms', '/control'],
    ['/vms/new', '/control/vms/new'],
    ['/resources', '/dashboard/cpu'],
    ['/demo', '/simulator'],
    ['/console', '/dashboard/console'],
    ['/events?vm=vm-a', '/dashboard/events?vm=vm-a'],
  ])('%s → %s', (from, to) => {
    const router = open(from);
    expect(at(router)).toBe(to);
    expect(screen.queryByText('없는 주소입니다.')).toBeNull();
  });

  it.each([
    ['/vms/vm-a', 'vm-detail'],
    ['/dashboard/vms/vm-a', 'vm-detail'],
    ['/control/vms/vm-a', 'control-detail'],
    ['/phone-link', 'phone-link'],
    ['/legacy/dashboard', 'legacy'],
  ])('%s 는 원래 화면(%s)', (path, name) => {
    open(path);
    expect(screen.getByText(`화면 ${name}`)).toBeInTheDocument();
    expect(screen.queryByText('없는 주소입니다.')).toBeNull();
  });
});
