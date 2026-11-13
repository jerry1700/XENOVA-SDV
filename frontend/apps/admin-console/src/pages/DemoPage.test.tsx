import { fireEvent, render, screen, within } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { DemoPage, failureText } from './DemoPage';

const vmCrash = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const inputFault = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const inputStatus = vi.hoisted(() => ({ data: undefined as unknown }));
const autoDelay = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const driveState = vi.hoisted(() => ({ activeId: 'vm-a', mode: 'AUTO' as string, age: 100 }));
/** 명령 상태 조회 결과 (기본: 응답 전) */
const commandStatus = vi.hoisted(() => ({ data: undefined as unknown }));

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
  useStartActiveDriveAutoDelay: () => autoDelay,
  useStartAutoInputUnavailable: () => inputFault,
  useCommandStatus: (vmId: string) => (vmId === 'vm-a' && inputStatus.data ? inputStatus : commandStatus),
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
    vmCrash.mutate.mockReset();
    commandStatus.data = undefined;
    autoDelay.mutate.mockClear();
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

  it('장애 명령 단계는 응답 전·접수·거절 사유를 한글로 보인다 (명령 상태 응답에 message 없음)', () => {
    vmCrash.mutate.mockImplementation((_: unknown, options?: { onSuccess?: (value: unknown) => void }) =>
      options?.onSuccess?.({ command_id: 'cmd-1', submitted_at: new Date().toISOString() }),
    );
    const { rerender } = render(
      <ToastProvider>
        <MemoryRouter>
          <DemoPage />
        </MemoryRouter>
      </ToastProvider>,
    );
    const again = () =>
      rerender(
        <ToastProvider>
          <MemoryRouter>
            <DemoPage />
          </MemoryRouter>
        </ToastProvider>,
      );
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '장애 발생' }));
    expect(screen.getByText('VM 응답 대기 중')).toBeInTheDocument();

    commandStatus.data = { vm_id: 'vm-a', command_id: 'cmd-1', status: 'ACCEPTED', error: null };
    again();
    expect(screen.getByText('접수')).toBeInTheDocument();

    commandStatus.data = {
      vm_id: 'vm-a',
      command_id: 'cmd-1',
      status: 'REJECTED',
      error: { code: 'DEMO_DISABLED', message: 'demo commands are disabled on this agent' },
    };
    again();
    expect(screen.getByText('이 VM에서는 시연용 명령이 꺼져 있습니다.')).toBeInTheDocument();
    expect(screen.queryByText(/DEMO_DISABLED|REJECTED|demo commands/)).toBeNull();

    commandStatus.data = { vm_id: 'vm-a', command_id: 'cmd-1', status: 'FAILED', error: 'broken' };
    again();
    expect(screen.getByText('명령을 처리하지 못했습니다.')).toBeInTheDocument();
  });

  it('활성 VM을 확인할 수 없으면 시작할 수 없다', () => {
    driveState.activeId = 'missing';
    show();
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('현재 활성 주행 VM을 VM 목록에서 찾지 못했습니다.')).toBeInTheDocument();
  });

  it('배포 API가 없으면 별도 문구로 안내한다', () => {
    expect(failureText(new ApiError(405, { code: 'METHOD_NOT_ALLOWED', message: '지원하지 않음' }))).toBe(
      '배포된 백엔드에 장애 시연 API가 아직 없습니다.',
    );
  });
});

describe('장애 시연 2', () => {
  beforeEach(() => {
    autoDelay.mutate.mockClear();
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.age = 100;
  });

  it('현재 AUTO VM과 고정 지연 프리셋을 표시한다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 2/ }));
    expect(screen.getByRole('heading', { name: '반복 연산 지연과 주행 제어권 승계' })).toBeInTheDocument();
    expect(screen.getAllByText('100ms · 30초').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Zephyr 주행 A').length).toBeGreaterThan(0);
  });

  it('확인 후 VM ID 없이 활성 주행 지연 API를 호출한다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 2/ }));
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '지연 발생' }));
    expect(autoDelay.mutate).toHaveBeenCalledWith(
      { delay_ms: 100, duration_s: 30 },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it('AUTO가 아니면 시연을 시작할 수 없다', () => {
    driveState.mode = 'MANUAL';
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 2/ }));
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('자동주행 중에만 연산 지연 시연을 시작할 수 있습니다.')).toBeInTheDocument();
  });
});

describe('장애 시연 3', () => {
  beforeEach(() => {
    inputFault.mutate.mockReset();
    commandStatus.data = undefined;
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.activeApp = 'autonomous';
    driveState.age = 100;
    driveState.inputReason = null;
  });

  function selectScenario() {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 3/ }));
  }

  it('지속 시간을 조절하고 확인 후 현재 활성 VM에만 명령을 보낸다', () => {
    selectScenario();
    const duration = screen.getByRole('spinbutton', { name: '장애 지속 시간' });
    fireEvent.change(duration, { target: { value: '50' } });
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    expect(screen.getByText(/한 대의 AUTO 계산 입력만 50초/)).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '50초 장애 발생' }));
    expect(inputFault.mutate).toHaveBeenCalledWith(
      { duration_s: 50 },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it('허용 범위를 벗어난 지속 시간과 AUTO가 아닌 상태를 차단한다', () => {
    selectScenario();
    fireEvent.change(screen.getByRole('spinbutton', { name: '장애 지속 시간' }), {
      target: { value: '121' },
    });
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('장애 지속 시간은 1초부터 120초까지 입력해 주세요.')).toBeInTheDocument();
  });

  it('INPUT_UNAVAILABLE와 AUTO를 유지한 VM 승계를 표시한다', () => {
    inputFault.mutate.mockImplementation(
      (_: unknown, options?: { onSuccess?: (value: Record<string, unknown>) => void }) =>
        options?.onSuccess?.({
          command_id: 'input-command',
          submitted_at: new Date().toISOString(),
          duration_s: 50,
        }),
    );
    const { rerender } = render(
      <ToastProvider>
        <MemoryRouter>
          <DemoPage />
        </MemoryRouter>
      </ToastProvider>,
    );
    const again = () =>
      rerender(
        <ToastProvider>
          <MemoryRouter>
            <DemoPage />
          </MemoryRouter>
        </ToastProvider>,
      );
    fireEvent.click(screen.getByRole('button', { name: /시연 3/ }));
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '50초 장애 발생' }));
    driveState.inputReason = 'INPUT_UNAVAILABLE';
    commandStatus.data = { status: 'RUNNING', error: null };
    again();
    expect(screen.getAllByText('INPUT_UNAVAILABLE').length).toBeGreaterThan(0);

    driveState.activeId = 'vm-b';
    driveState.inputReason = null;
    again();
    expect(screen.getByText('AUTO 유지 · 승계 완료')).toBeInTheDocument();
  });
});
