import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { AGENT_ERROR_TEXT, ERROR_TEXT } from '../api/errorText';
import type { CommandStatusResponse, Vm } from '../api/types';
import { SimulatorStressLab } from './SimulatorStressLab';

const linux = (id: string, name: string): Vm => ({
  vm_id: id,
  name,
  guest_kind: 'linux',
  state: 'running',
  agent_status: 'ONLINE',
  ipv4: null,
  template_id: 'debian',
  template_version: '13',
  vcpus: 2,
  memory_mib: 1024,
});
const zephyr: Vm = { ...linux('vm-z', 'autonomous'), guest_kind: 'zephyr', agent_status: undefined };

const state = vi.hoisted(() => ({
  vms: [] as Vm[],
  /** 명령 상태 조회 기록 [vmId, commandId] */
  queried: [] as [string, string | null][],
  status: undefined as Partial<CommandStatusResponse> | undefined,
  statusError: null as unknown,
  stress: vi.fn(),
  delay: vi.fn(),
  exit: vi.fn(),
}));

vi.mock('../api/queries', () => {
  const mutation = (fn: () => unknown) => ({ mutateAsync: fn, isPending: false });
  return {
    useVms: () => ({ data: { items: state.vms } }),
    useMetrics: () => ({ data: { items: [] } }),
    useProcesses: () => ({ data: undefined }),
    useThreads: () => ({ data: undefined }),
    useStartStress: () => mutation(state.stress),
    useStartAutoDelay: () => mutation(state.delay),
    useStartAgentExit: () => mutation(state.exit),
    useCommandStatus: (vmId: string, commandId: string | null) => {
      state.queried.push([vmId, commandId]);
      const failed = Boolean(commandId && state.statusError);
      return {
        data: commandId && !failed ? state.status : undefined,
        isError: failed,
        error: failed ? state.statusError : null,
      };
    },
  };
});
vi.mock('@xenova/ui', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@xenova/ui')>()),
  useToast: () => ({ show: vi.fn() }),
}));

const renderLab = () =>
  render(
    <MemoryRouter>
      <SimulatorStressLab />
    </MemoryRouter>,
  );

beforeEach(() => {
  state.vms = [linux('vm-a', 'interaction'), linux('vm-b', 'manual-linux'), zephyr];
  state.queried = [];
  state.status = { status: 'ACCEPTED', error: null };
  state.statusError = null;
  state.stress.mockReset();
  state.delay.mockReset();
  state.exit.mockReset();
});

/** 응답을 붙잡아 두었다가 resolve 로 보낸다 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

describe('부하 실험실 명령 조회 (S15P31E110-468)', () => {
  it('명령 응답 전에 VM 을 바꾸면, 응답 뒤에도 이전 VM 의 명령 ID 로 새 VM 을 조회하지 않는다', async () => {
    const reply = deferred<{ command_id: string }>();
    state.stress.mockReturnValue(reply.promise);
    renderLab();
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    await userEvent.click(screen.getByRole('button', { name: /manual-linux/ }));
    await act(async () => reply.resolve({ command_id: 'cmd-a' }));
    expect(state.queried.filter(([, id]) => id === 'cmd-a')).toEqual([]);

    // 다시 보낸 VM 으로 돌아오면 그 VM 의 명령으로만 조회한다
    await userEvent.click(screen.getByRole('button', { name: /^LINUX\s*interaction|interaction/ }));
    expect(state.queried.filter(([, id]) => id === 'cmd-a').every(([vm]) => vm === 'vm-a')).toBe(true);
  });

  it('선택 VM 이 목록에서 사라져 첫 VM 으로 바뀌어도 이전 명령 ID 를 붙이지 않는다', async () => {
    state.stress.mockResolvedValue({ command_id: 'cmd-b' });
    const view = renderLab();
    await userEvent.click(screen.getByRole('button', { name: /manual-linux/ }));
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    expect(state.queried).toContainEqual(['vm-b', 'cmd-b']);

    state.vms = [linux('vm-a', 'interaction'), zephyr];
    state.queried = [];
    view.rerender(
      <MemoryRouter>
        <SimulatorStressLab />
      </MemoryRouter>,
    );
    expect(state.queried.length).toBeGreaterThan(0);
    expect(state.queried.every(([vm, id]) => id === null || vm === 'vm-b')).toBe(true);
    expect(state.queried).not.toContainEqual(['vm-a', 'cmd-b']);
  });
});

describe('부하 실험실 결과 문구 (영어 상태값·명령 ID·(CODE) 없이)', () => {
  const ID = '9a1c2e3f-4b5d-4e6f-8a7b-0c1d2e3f4a5b';
  const sendCpu = async () => {
    renderLab();
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    return screen.findByRole('status');
  };
  const noRaw = (box: HTMLElement) => {
    expect(box).not.toHaveTextContent(/ACCEPTED|RUNNING|SUCCEEDED|FAILED|REJECTED|BUSY|[A-Z_]{6,}/);
    expect(box).not.toHaveTextContent(ID);
    expect(box).not.toHaveTextContent(/\(/);
  };

  it.each([
    ['ACCEPTED', null, '명령 접수'],
    ['RUNNING', null, '명령 실행 중'],
    ['SUCCEEDED', null, '명령 완료'],
  ] as const)('%s → "%s"', async (status, error, headline) => {
    state.stress.mockResolvedValue({ command_id: ID });
    state.status = { status, error };
    const box = await sendCpu();
    expect(box).toHaveTextContent(headline);
    expect(box).toHaveAttribute('title', ID);
    expect(box).toHaveAttribute('data-tone', 'ok');
    noRaw(box);
  });

  it('거절(BUSY)·실패는 한글 사유와 위험색, 모르는 코드는 일반 문구', async () => {
    state.stress.mockResolvedValue({ command_id: ID });
    state.status = { status: 'REJECTED', error: { code: 'BUSY', message: 'stress x is running' } };
    const box = await sendCpu();
    expect(box).toHaveTextContent(`명령 거절${AGENT_ERROR_TEXT.BUSY}`);
    expect(box).toHaveAttribute('data-tone', 'fault');
    noRaw(box);
  });

  it('실패 사유 형식이 달라도 일반 문구', async () => {
    state.stress.mockResolvedValue({ command_id: ID });
    state.status = { status: 'FAILED', error: { code: 404 } as never };
    const box = await sendCpu();
    expect(box).toHaveTextContent('명령 실패명령을 처리하지 못했습니다.');
    noRaw(box);
  });

  it('보내기 실패는 공용 한글 문구, 명령 조회 404 는 "명령을 찾을 수 없습니다"', async () => {
    state.stress.mockRejectedValue(new ApiError(503, { code: 'MQTT_UNAVAILABLE', message: 'broker down' }));
    const box = await sendCpu();
    expect(box).toHaveTextContent(`명령 전송 실패${ERROR_TEXT.MQTT_UNAVAILABLE}`);
    expect(box).toHaveAttribute('data-tone', 'fault');
    noRaw(box);
  });

  it('명령 조회가 404 면 "명령을 찾을 수 없습니다"', async () => {
    state.stress.mockResolvedValue({ command_id: ID });
    state.statusError = new ApiError(404, { code: 'COMMAND_NOT_FOUND', message: 'x' });
    const box = await sendCpu();
    expect(box).toHaveTextContent(ERROR_TEXT.COMMAND_NOT_FOUND);
    expect(box).toHaveAttribute('data-tone', 'fault');
    noRaw(box);
  });
});

describe('Agent 종료·연산 지연 확인 창', () => {
  beforeEach(() => {
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('[Agent 종료 실행]은 대상 VM·영향을 보이고, 취소하면 요청을 보내지 않는다', async () => {
    state.exit.mockResolvedValue({ command_id: 'cmd-exit' });
    renderLab();
    await userEvent.click(screen.getByRole('button', { name: 'Agent 종료 실행' }));
    expect(state.exit).not.toHaveBeenCalled();
    expect(screen.getByText('‘interaction’ VM의 Agent를 종료하시겠습니까?')).toBeInTheDocument();
    expect(screen.getByText(/heartbeat가 끊깁니다/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '취소' }));
    expect(state.exit).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Agent 종료 실행' }));
    await userEvent.click(screen.getByRole('button', { name: 'Agent 종료' }));
    expect(state.exit).toHaveBeenCalledTimes(1);
  });

  it('[연산 지연 시작]은 지연값·시간을 보이고, 확인해야 요청을 보낸다', async () => {
    state.delay.mockResolvedValue({ command_id: 'cmd-delay' });
    renderLab();
    await userEvent.click(screen.getByRole('button', { name: /autonomous/ }));
    await userEvent.click(screen.getByRole('button', { name: '연산 지연 시작' }));
    expect(state.delay).not.toHaveBeenCalled();
    expect(screen.getByText('‘autonomous’ VM의 주행 연산에 지연을 넣으시겠습니까?')).toBeInTheDocument();
    expect(screen.getByText(/20 ms 지연을 30초 동안/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: '취소' }));
    expect(state.delay).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: '연산 지연 시작' }));
    await userEvent.click(screen.getByRole('button', { name: '지연 시작' }));
    expect(state.delay).toHaveBeenCalledTimes(1);
    expect(state.delay).toHaveBeenCalledWith({ delay_ms: 20, duration_s: 30 });
  });
});

describe('부하 실험실 VM 목록 라벨', () => {
  it('OS·VM 상태를 원문(linux·RUNNING) 대신 OS 이름과 한글 상태로', () => {
    state.vms = [
      linux('vm-a', 'interaction'),
      { ...linux('vm-b', 'manual-b'), state: 'stopped' },
      { ...zephyr, state: 'crashed' },
    ];
    renderLab();
    const grid = screen.getByRole('button', { name: /interaction/ }).parentElement!;
    expect(grid).toHaveTextContent('Debian');
    expect(grid).toHaveTextContent('Zephyr');
    expect(grid).toHaveTextContent('실행 중');
    expect(grid).toHaveTextContent('꺼짐');
    expect(grid).toHaveTextContent('비정상 종료');
    expect(grid).not.toHaveTextContent(/linux|zephyr|RUNNING|STOPPED|CRASHED/);
  });
});
