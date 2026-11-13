import { fireEvent, render, screen, within } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { DemoPage, failureText } from './DemoPage';

const vmCrash = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const driveState = vi.hoisted(() => ({ activeId: 'vm-a', mode: 'AUTO' as string, age: 100 }));

vi.mock('../api/queries', () => ({
  useVms: () => ({
    data: {
      items: [
        {
          vm_id: 'vm-a',
          name: 'Zephyr 주행 A',
          role: 'primary_drive',
          state: 'running',
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
  useStartVmCrash: () => vmCrash,
  useCommandStatus: () => ({ data: undefined }),
  useEvents: () => ({ data: { items: [] } }),
}));
vi.mock('../layout/DriveContext', () => ({
  useDrive: () => ({
    drive: {
      control: {
        mode: driveState.mode,
        active_vm_id: driveState.activeId,
        received_ago_ms: driveState.age,
      },
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

describe('장애 시연 1', () => {
  beforeEach(() => {
    vmCrash.mutate.mockClear();
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.age = 100;
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('네 시연 책갈피와 기존 부하 테스트 메뉴를 표시한다', () => {
    show();
    expect(screen.getByRole('link', { name: '부하 테스트' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /시연 [1-4]/ })).toHaveLength(4);
  });

  it('활성 VM과 두 주행 VM, 현재 모드를 표시한다', () => {
    show();
    expect(screen.getAllByText('Zephyr 주행 A').length).toBeGreaterThan(0);
    expect(screen.getByText('Zephyr 주행 B')).toBeInTheDocument();
    expect(screen.getByText('자율주행')).toBeInTheDocument();
    expect(screen.getByText('ACTIVE')).toBeInTheDocument();
  });

  it('확인 후 현재 활성 VM에 vm-crash 명령을 보낸다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    expect(vmCrash.mutate).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '장애 발생' }));
    expect(vmCrash.mutate).toHaveBeenCalledTimes(1);
  });

  it('활성 VM을 확인할 수 없으면 시작할 수 없다', () => {
    driveState.activeId = 'missing';
    show();
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('현재 활성 주행 VM을 VM 목록에서 찾지 못했습니다.')).toBeInTheDocument();
  });

  it('배포 API가 없으면 별도 문구로 안내한다', () => {
    expect(failureText(new ApiError(405, { code: 'METHOD_NOT_ALLOWED', message: '지원하지 않음' }))).toBe(
      '배포된 백엔드에 VM 장애 시연 API가 아직 없습니다.',
    );
  });
});
