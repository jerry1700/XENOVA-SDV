import { fireEvent, render, screen, within } from '@testing-library/react';
import { ToastProvider } from '@xenova/ui';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { DemoPage, failureText, perceptionFaultErrorText } from './DemoPage';

const vmCrash = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const inputFault = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const stress = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const appExit = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const inputStatus = vi.hoisted(() => ({ data: undefined as unknown }));
const autoDelay = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }));
const driveState = vi.hoisted(() => ({
  activeId: 'vm-a',
  mode: 'AUTO' as string,
  activeApp: 'autonomous' as string,
  age: 100,
  inputReason: null as string | null,
  appPhase: 'EXECUTING' as string,
  appInstance: 'instance-a-1',
  bootId: 'boot-a',
}));
/** 명령 상태 조회 결과 (기본: 응답 전) */
const commandStatus = vi.hoisted(() => ({ data: undefined as unknown }));
const perceptionBlock = vi.hoisted(() => ({
  mutate: vi.fn(),
  isPending: false,
  isError: false,
  error: null,
}));
const perceptionOperation = vi.hoisted(() => ({ data: undefined as unknown }));

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
          vm_id: 'vm-interaction',
          name: 'Interaction VM',
          role: 'interaction',
          state: 'running',
          guest_kind: 'linux',
          agent_status: 'ONLINE',
          memory_mib: 512,
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
  useStartActiveVmCrash: () => vmCrash,
  useStartActiveDriveAutoDelay: () => autoDelay,
    useStartStress: () => stress,
  useStartActiveAppExit: () => appExit,
  useMetrics: () => ({ data: { items: [] } }),
  useStartAutoInputUnavailable: () => inputFault,
  useCommandStatus: (vmId: string) => (vmId === 'vm-a' && inputStatus.data ? inputStatus : commandStatus),
  useEvents: () => ({ data: { items: [] } }),
  useStartPerceptionBlock: () => perceptionBlock,
  usePerceptionFaultOperation: () => perceptionOperation,
}));
vi.mock('../layout/DriveContext', () => ({
  useDrive: () => ({
    drive: {
      source: { status: 'fresh', age_ms: 10 },
      control: {
        mode: driveState.mode,
        active_app: driveState.activeApp,
        active_vm_id: driveState.activeId,
        received_ago_ms: driveState.age,
      },
      perception: { valid: true, received_ago_ms: 20, hz: 15 },
      vm_apps: {
        'vm-a': {
          autonomous: {
            boot_id: driveState.bootId,
            app_instance_id: driveState.appInstance,
            lifecycle: {
              phase: driveState.appPhase,
              readiness: driveState.inputReason ? 'NOT_READY' : 'READY',
              reason: driveState.inputReason,
            },
            lifecycle_source: { status: 'fresh', age_ms: 10 },
          },
        },
        'vm-b': {
          autonomous: {
            boot_id: 'boot-b',
            app_instance_id: 'instance-b-1',
            lifecycle: { phase: 'WAITING', readiness: 'QUALIFIED', reason: null },
            lifecycle_source: { status: 'fresh', age_ms: 10 },
          },
        },
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
    perceptionBlock.mutate.mockReset();
    commandStatus.data = undefined;
    autoDelay.mutate.mockClear();
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.activeApp = 'autonomous';
    driveState.age = 100;
    driveState.inputReason = null;
    driveState.appPhase = 'EXECUTING';
    driveState.appInstance = 'instance-a-1';
    driveState.bootId = 'boot-a';
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('시연 6에서 장애 시간을 정하고 Jetson 인지 차단 API를 호출한다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 6/ }));
    expect(screen.getByText('Jetson 인지 송신 단절과 안전 전환')).toBeInTheDocument();
    expect(screen.getByText('15 Hz · 정상')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('장애 지속 시간'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    expect(perceptionBlock.mutate).toHaveBeenCalledWith(30, expect.any(Object));
  });

  it('시연 6 API가 배포되지 않았으면 전용 안내를 표시한다', () => {
    expect(
      perceptionFaultErrorText(new ApiError(405, { code: 'METHOD_NOT_ALLOWED', message: '지원하지 않음' })),
    ).toBe('배포된 백엔드에 Jetson 인지 단절 시연 API가 아직 없습니다.');
  });

  it('일곱 시연 책갈피와 기존 부하 테스트 메뉴를 표시한다', () => {
    show();
    expect(screen.getByRole('link', { name: '부하 테스트' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /시연 [1-7]/ })).toHaveLength(7);
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

  it('AUTO 자율주행 상태가 아니면 VM 장애 시연을 시작할 수 없다', () => {
    driveState.mode = 'MANUAL';
    driveState.activeApp = 'manual';
    show();
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.')).toBeInTheDocument();
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
      stress.mutate.mockReset();
    commandStatus.data = undefined;
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.activeApp = 'autonomous';
    driveState.age = 100;
  });

  function selectScenario() {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 3/ }));
  }

  it('Interaction VM의 CPU 90% 개별 부하를 시작한다', () => {
    selectScenario();
    expect(screen.getByRole('heading', { name: 'Interaction VM 자원 폭주와 주행 격리' })).toBeInTheDocument();
    expect(screen.getAllByText('Interaction VM').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'CPU 90% 부하' }));
    expect(stress.mutate).toHaveBeenCalledWith(
      { kind: 'cpu', body: { workers: 1, load_percent: 90, duration_s: 30 } },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it('AUTO가 아니면 격리 시연을 시작할 수 없다', () => {
    driveState.mode = 'MANUAL';
    selectScenario();
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('AUTO 주행 중에만 격리 시연을 시작할 수 있습니다.')).toBeInTheDocument();
  });
});

describe('장애 시연 4', () => {
  beforeEach(() => {
    appExit.mutate.mockReset();
    commandStatus.data = undefined;
    driveState.activeId = 'vm-a';
    driveState.mode = 'AUTO';
    driveState.activeApp = 'autonomous';
    driveState.appPhase = 'EXECUTING';
    driveState.appInstance = 'instance-a-1';
    driveState.bootId = 'boot-a';
  });

  it('확인 후 백엔드에 활성 AUTO 앱 종료를 요청한다', () => {
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 4/ }));
    expect(screen.getByRole('heading', { name: '자율주행 앱 종료·재시작과 백업 승계' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '시연 시작' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '앱 종료' }));
    expect(appExit.mutate).toHaveBeenCalledWith(
      undefined,
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it('AUTO가 아니면 시연을 차단한다', () => {
    driveState.mode = 'MANUAL';
    driveState.activeApp = 'manual';
    show();
    fireEvent.click(screen.getByRole('button', { name: /시연 4/ }));
    expect(screen.getByRole('button', { name: '시연 시작' })).toBeDisabled();
    expect(screen.getByText('자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.')).toBeInTheDocument();
  });
});
