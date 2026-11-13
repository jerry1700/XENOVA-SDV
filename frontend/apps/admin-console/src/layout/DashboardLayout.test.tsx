import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { describe, expect, it } from 'vitest';
import { DashboardLayout } from './DashboardLayout';
import { KeepQueryRedirect } from './KeepQueryRedirect';

function EventsProbe() {
  const { pathname, search } = useLocation();
  return <p>events content {pathname + search}</p>;
}

function renderDashboard(path = '/dashboard/overview') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/dashboard" element={<DashboardLayout />}>
          <Route path="overview" element={<p>overview content</p>} />
          <Route path="cpu" element={<p>cpu content</p>} />
          <Route path="memory" element={<p>memory content</p>} />
          <Route path="network" element={<p>network content</p>} />
          <Route path="console" element={<p>console content</p>} />
          <Route path="events" element={<EventsProbe />} />
        </Route>
        <Route path="/events" element={<KeepQueryRedirect to="/dashboard/events" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('DashboardLayout', () => {
  it('대시보드 6개 탭을 Overview · CPU · Memory · Network · Console · Events 순서로 표시한다', () => {
    renderDashboard('/dashboard/memory');

    const nav = screen.getByRole('navigation', { name: 'VM Dashboard 메뉴' });
    expect(nav).toBeInTheDocument();
    expect(screen.getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Overview',
      'CPU',
      'Memory',
      'Network',
      'Console',
      'Events',
    ]);
    expect(screen.getByRole('link', { name: 'Memory' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('memory content')).toBeInTheDocument();
  });

  it('Network 탭을 누르면 /dashboard/network 화면으로 이동한다', async () => {
    const user = userEvent.setup();
    renderDashboard('/dashboard/memory');

    await user.click(screen.getByRole('link', { name: 'Network' }));

    expect(screen.getByText('network content')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Network' })).toHaveAttribute('aria-current', 'page');
  });

  it('탭을 누르면 해당 하위 화면으로 이동한다', async () => {
    const user = userEvent.setup();
    renderDashboard();

    await user.click(screen.getByRole('link', { name: 'CPU' }));

    expect(screen.getByText('cpu content')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'CPU' })).toHaveAttribute('aria-current', 'page');
  });

  it('Events 탭은 Dashboard 제목·탭 줄을 유지한 채 이벤트 화면을 연다', async () => {
    const user = userEvent.setup();
    renderDashboard();

    await user.click(screen.getByRole('link', { name: 'Events' }));

    expect(screen.getByText('events content /dashboard/events')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'VM Dashboard' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Events' })).toHaveAttribute('aria-current', 'page');
  });

  it('옛 /events 주소는 쿼리를 유지한 채 /dashboard/events 로 넘어간다', () => {
    renderDashboard('/events?vm=abc');

    expect(screen.getByText('events content /dashboard/events?vm=abc')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Events' })).toHaveAttribute('aria-current', 'page');
  });
});
