import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EventsPage } from './EventsPage';

const VMS = [
  { vm_id: 'vm-a', name: 'autonomous-a', guest_kind: 'zephyr', role: 'primary_drive' },
  { vm_id: 'vm-b', name: 'manual-b', guest_kind: 'zephyr', role: 'backup_drive' },
];

type EventItem = Record<string, unknown>;
let eventItems: EventItem[] = [];
let driveValue: unknown = null;

const useEvents = vi.fn((_params: { vm_id?: string }) => ({
  data: { items: eventItems },
  error: null,
  isFetching: false,
}));
let vmsData: { items: typeof VMS } | undefined = { items: VMS };

vi.mock('../api/queries', () => ({
  useEvents: (params: { vm_id?: string }) => useEvents(params),
  useVms: () => ({ data: vmsData, error: null, isPending: !vmsData }),
}));
vi.mock('../layout/DriveContext', () => ({ useDrive: () => ({ drive: driveValue, socket: 'open' }) }));

function renderAt(path: string) {
  const router = createMemoryRouter([{ path: '/dashboard/events', element: <EventsPage /> }], {
    initialEntries: [path],
  });
  render(<RouterProvider router={router} />);
  return router;
}

const lastVmFilter = () => useEvents.mock.calls.at(-1)![0].vm_id;
const targetChip = (name: string) => screen.getByRole('button', { name });

beforeEach(() => {
  useEvents.mockClear();
  vmsData = { items: VMS };
  eventItems = [];
  driveValue = null;
});

describe('이벤트 화면 대상 VM 필터 ↔ URL ?vm=', () => {
  it('?vm= 로 들어오면 그 VM 필터가 켜진 채 조회한다', () => {
    renderAt('/dashboard/events?vm=vm-b');
    expect(targetChip('manual-b')).toHaveAttribute('aria-pressed', 'true');
    expect(targetChip('전체 VM')).toHaveAttribute('aria-pressed', 'false');
    expect(lastVmFilter()).toBe('vm-b');
  });

  it('대상 칩을 바꾸면 URL ?vm= 도 replace 로 바뀐다 (기록이 쌓이지 않음)', async () => {
    const user = userEvent.setup();
    const router = renderAt('/dashboard/events?vm=vm-b');
    await user.click(targetChip('autonomous-a'));
    expect(router.state.location.search).toBe('?vm=vm-a');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(lastVmFilter()).toBe('vm-a');

    await user.click(targetChip('전체 VM'));
    expect(router.state.location.search).toBe('');
    expect(router.state.historyAction).toBe('REPLACE');
    expect(lastVmFilter()).toBeUndefined();

    // replace 만 했으므로 뒤로 갈 기록이 없다
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe('/dashboard/events');
  });

  it('목록에 없는 VM ID 는 필터 없이 전체로 보고 URL 에서도 뺀다', () => {
    const router = renderAt('/dashboard/events?vm=missing');
    expect(targetChip('전체 VM')).toHaveAttribute('aria-pressed', 'true');
    expect(lastVmFilter()).toBeUndefined();
    expect(router.state.location.search).toBe('');
  });

  it('VM 목록을 받기 전에는 요청한 ID 를 그대로 쓴다', () => {
    vmsData = undefined;
    const router = renderAt('/dashboard/events?vm=vm-b');
    expect(lastVmFilter()).toBe('vm-b');
    expect(router.state.location.search).toBe('?vm=vm-b');
  });
});

const event = (id: string, at: string, fields: EventItem): EventItem => ({
  event_id: id,
  severity: 'INFO',
  vm_id: null,
  operation_id: null,
  occurred_at: at,
  recorded_at: at,
  payload: {},
  ...fields,
});

describe('이벤트 화면 표시', () => {
  it('머리말은 EVENTS 소제목 + 제목, 필터 칩은 한글 라벨과 색 점', () => {
    renderAt('/dashboard/events');
    expect(screen.getByText('EVENTS')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: '이벤트 타임라인' })).toBeInTheDocument();
    for (const label of ['정보', '경고', '오류']) {
      const severity = targetChip(label);
      expect(severity.querySelector('i')).toHaveAttribute(
        'data-tone',
        label === '정보' ? 'info' : label === '경고' ? 'warning' : 'error',
      );
    }
    expect(screen.queryByRole('button', { name: 'INFO' })).not.toBeInTheDocument();
    // VM 칩은 글자색이 아니라 역할색 점
    const vm = targetChip('manual-b');
    expect(vm).not.toHaveAttribute('style');
    expect(vm.querySelector('i')!.style.getPropertyValue('--dot')).toBe('var(--x-app-manual)');
  });

  it('요약 타일: 세대 값과 한글 모드, 인계 값과 목표 보조 줄, 차량 속도와 단위', () => {
    driveValue = {
      control: { mode: 'SAFE_STOP', generation: 4 },
      vehicle: { v_mps: 0.42 },
    };
    eventItems = [
      event('e1', '2026-11-12T03:00:00Z', {
        type: 'drive.mode.changed',
        payload: { from: 'AUTO', to: 'MANUAL', handoff_ms: 120 },
      }),
    ];
    renderAt('/dashboard/events');
    const tiles = within(screen.getByRole('region', { name: '주행 요약' }));
    expect(tiles.getByText('현재 세대').parentElement).toHaveTextContent(/^현재 세대4안전 정지$/);
    expect(tiles.getByText('최근 인계').parentElement).toHaveTextContent(/^최근 인계120ms목표 ≤150 ms$/);
    expect(tiles.getByText('차량').parentElement).toHaveTextContent(/^차량0\.42m\/s$/);
    expect(screen.queryByText(/gen 4/)).not.toBeInTheDocument();
  });

  it('한 줄 형식: 날짜가 바뀔 때만 날짜 줄, 대상은 VM 이름(또는 회색 게이트웨이), 상태 값은 한글(원문은 title)', () => {
    eventItems = [
      event('e1', '2026-11-12T03:00:00.250Z', {
        type: 'drive.mode.changed',
        severity: 'WARNING',
        payload: { from: 'AUTO', to: 'SAFE_STOP', reason: 'command_timeout' },
      }),
      event('e2', '2026-11-12T02:00:00Z', {
        type: 'vm.state.changed',
        vm_id: 'vm-b',
        payload: { from: 'stopped', to: 'running' },
      }),
      event('e3', '2026-11-10T02:00:00Z', {
        type: 'vm.state.changed',
        vm_id: 'vm-a',
        payload: { from: 'running', to: 'paused' },
      }),
    ];
    renderAt('/dashboard/events');
    const days = screen.getAllByText(/^\d{4}\. \d{1,2}\. \d{1,2}\.$/);
    expect(days.map((day) => day.textContent)).toEqual([
      new Date('2026-11-12T03:00:00.250Z').toLocaleDateString('ko-KR'),
      new Date('2026-11-10T02:00:00Z').toLocaleDateString('ko-KR'),
    ]);

    const mode = screen.getByText('자율주행 → 안전 정지').closest('div')!;
    expect(mode).toHaveAttribute('title', expect.stringContaining('원문 AUTO → SAFE_STOP'));
    // 보조 글(사유)은 줄에서 빼고 title 로
    expect(mode).toHaveAttribute('title', expect.stringContaining('명령 끊김'));
    expect(within(mode).queryByText(/명령 끊김/)).not.toBeInTheDocument();
    expect(within(mode).getByText('게이트웨이')).toBeInTheDocument();
    // 종류 라벨은 줄에 없고 심각도 점의 aria-label 에
    expect(within(mode).queryByText(/주행 모드|모드 전환/)).not.toBeInTheDocument();
    expect(within(mode).getByRole('img', { name: '심각도 경고 · 주행 모드' })).toHaveTextContent(/^경고$/);
    expect(within(mode).getByText(/^\d{2}:\d{2}:\d{2}\.250$/).tagName).toBe('TIME');

    const state = screen.getByText('꺼짐 → 실행 중').closest('div')!;
    expect(within(state).getByText('manual-b')).toBeInTheDocument();
    expect(within(state).queryByText(/VM 상태|VM 수명주기/)).not.toBeInTheDocument();
    expect(state).toHaveAttribute('title', expect.stringContaining('원문 stopped → running'));
    // 열 순서: 시각 | 심각도 점 | 대상 | 내용
    expect([...state.children].map((cell) => cell.textContent)).toEqual([
      expect.stringMatching(/^\d{2}:\d{2}:\d{2}\.\d{3}$/),
      '',
      'manual-b',
      '꺼짐 → 실행 중',
    ]);
    // 정보는 상태 칸을 비워 두고, aria-label 에는 남긴다
    expect(within(state).getByRole('img', { name: '심각도 정보 · VM 상태' })).toHaveTextContent(/^$/);
    // 대상: VM 은 역할색 점(대상 필터 칩과 같은 색), 게이트웨이는 점 없이 회색 이름
    const dot = within(state).getByText('manual-b').querySelector('i')!;
    expect(dot.style.getPropertyValue('--dot')).toBe('var(--x-app-manual)');
    expect(within(mode).getByText('게이트웨이').querySelector('i')).toBeNull();
    // 열 제목 줄: 시각 · 상태 · 대상 · 내용
    const head = document.querySelector('[data-head]')!;
    expect([...head.children].map((cell) => cell.textContent)).toEqual(['시각', '상태', '대상', '내용']);
    expect(screen.queryByText('도메인 상태')).not.toBeInTheDocument();
  });
});
