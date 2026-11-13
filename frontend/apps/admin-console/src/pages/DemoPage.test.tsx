import { fireEvent, render, screen, within } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { DemoPage, failureText } from './DemoPage';

const appExit = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  isSuccess: false,
  error: null,
}));
const domainKill = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  isSuccess: false,
  error: null,
}));
const vmState = vi.hoisted(() => ({ state: 'running' as string }));

vi.mock('../api/queries', () => ({
  useVms: () => ({
    isLoading: false,
    data: {
      items: [
        {
          vm_id: 'vm-a',
          name: 'Zephyr 주행 A',
          role: 'primary_drive',
          state: vmState.state,
          guest_kind: 'zephyr',
        },
        {
          vm_id: 'vm-b',
          name: 'Zephyr 주행 B',
          role: 'backup_drive',
          state: 'running',
          guest_kind: 'zephyr',
        },
      ],
    },
  }),
  useStartAppExit: () => appExit,
  useStartDomainKill: () => domainKill,
  useCommandStatus: () => ({ data: undefined }),
}));
vi.mock('../layout/DriveContext', () => ({
  useDrive: () => ({
    drive: {
      control: { mode: 'AUTO' },
      apps: { autonomous: { command_fresh: true, command_hz: 50, heartbeat_ago_ms: 400 } },
    },
    socket: 'open',
  }),
}));

function show() {
  render(
    <ToastProvider>
      <MemoryRouter>
        <DemoPage />
      </MemoryRouter>
    </ToastProvider>,
  );
}

describe('시뮬레이션 제어 — VM1 장애 주입만 남긴다', () => {
  beforeEach(() => {
    appExit.mutate.mockClear();
    domainKill.mutate.mockClear();
    vmState.state = 'running';
    // jsdom 에는 <dialog> 메서드가 없다
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('부하 테스트 탭은 남고, 격리 시연·트랙 조작·수동주행 입력·재개는 없다', () => {
    show();
    expect(screen.getByRole('link', { name: '부하 테스트' })).toBeInTheDocument();
    expect(screen.queryByText('격리 시연')).toBeNull();
    expect(screen.queryByText('수동주행 입력')).toBeNull();
    expect(screen.queryByText(/트랙 조작/)).toBeNull();
    expect(screen.queryByText(/강제 종료 API\(-135\)/)).toBeNull();
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual([
      '자율주행 앱 종료',
      'VM1 강제 종료',
    ]);
  });

  it('VM1 = 주 구동 VM 의 상태·자율 명령·생존 신호를 보인다', () => {
    show();
    expect(screen.getByText('VM1 · Zephyr 주행 A')).toBeInTheDocument();
    expect(screen.getByText('50 Hz')).toBeInTheDocument();
    expect(screen.getByText('0.4초 전')).toBeInTheDocument();
  });

  it('[자율주행 앱 종료] 는 확인 뒤 app_id=autonomous 로 한 번만 보낸다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: '자율주행 앱 종료' }));
    expect(appExit.mutate).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '자율주행 앱 종료' }));
    expect(appExit.mutate).toHaveBeenCalledTimes(1);
    expect(appExit.mutate.mock.calls[0]![0]).toEqual({ app_id: 'autonomous' });
    expect(domainKill.mutate).not.toHaveBeenCalled();
  });

  it('[VM1 강제 종료] 는 확인 뒤 domain-kill 을 보낸다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: 'VM1 강제 종료' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'VM1 강제 종료' }));
    expect(domainKill.mutate).toHaveBeenCalledTimes(1);
    expect(appExit.mutate).not.toHaveBeenCalled();
  });

  it('VM1 이 실행 중이 아니면 두 버튼 모두 비활성', () => {
    vmState.state = 'stopped';
    show();
    expect(screen.getByRole('button', { name: '자율주행 앱 종료' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'VM1 강제 종료' })).toBeDisabled();
  });

  it('백엔드에 API 가 없으면(404) 그렇게 알리고, VM 없음 404 는 원래 문구', () => {
    expect(failureText(new ApiError(404, { code: 'NOT_FOUND', message: 'No static resource' }))).toBe(
      '백엔드에 이 장애 주입 API가 아직 없습니다.',
    );
    expect(failureText(new ApiError(404, { code: 'VM_NOT_FOUND', message: 'VM을 찾을 수 없습니다.' }))).toBe(
      'VM을 찾을 수 없습니다.',
    );
  });
});
