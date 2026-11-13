import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { ERROR_TEXT } from '../api/errorText';
import type { CommandStatusResponse, StressRequest, Vm } from '../api/types';
import { StressTestPanel } from './StressTestPanel';

const state = vi.hoisted(() => ({
  mutateAsync: vi.fn<(input: StressRequest) => Promise<unknown>>(),
  /** 명령 상태 조회 결과 (명령 ID 가 있을 때) */
  command: { data: undefined, isError: false, error: null } as {
    data: Partial<CommandStatusResponse> | undefined;
    isError: boolean;
    error: unknown;
  },
  queried: [] as (string | null)[],
}));

vi.mock('../api/queries', () => ({
  useStartStress: () => ({ mutateAsync: state.mutateAsync, isPending: false }),
  useCommandStatus: (_vmId: string, commandId: string | null) => {
    state.queried.push(commandId);
    return commandId ? state.command : { data: undefined, isError: false, error: null };
  },
}));
vi.mock('@xenova/ui', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@xenova/ui')>()),
  useToast: () => ({ show: vi.fn() }),
}));

const vm: Vm = {
  vm_id: 'vm-linux',
  name: 'linux-a',
  guest_kind: 'linux',
  state: 'running',
  agent_status: 'ONLINE',
  ipv4: null,
  template_id: 'ubuntu',
  template_version: '1',
  vcpus: 2,
  memory_mib: 1024,
};

const COMMAND_ID = '3f2b8c1e-0d5a-4f7e-9b61-2c4d8e9fa012';
const command = (type: 'CPU' | 'MEMORY', amount: number, duration_s: number) => ({
  command_id: COMMAND_ID,
  operation_id: 'o',
  vm_id: vm.vm_id,
  type,
  amount,
  duration_s,
  submitted_at: '2026-11-06T00:00:00Z',
  expires_at: '2026-11-06T00:00:30Z',
});

describe('VM 상세 부하 테스트 카드', () => {
  beforeEach(() => {
    state.mutateAsync.mockReset();
    state.command = { data: { status: 'ACCEPTED', error: null }, isError: false, error: null };
    state.queried = [];
  });

  it('CPU 워커 수와 시간을 골라 cpu 부하 요청을 보낸다', async () => {
    state.mutateAsync.mockResolvedValue(command('CPU', 3, 45));
    render(<StressTestPanel vm={vm} accent="red" />);
    await userEvent.click(screen.getByRole('button', { name: /3\s*워커/ }));
    const duration = screen.getByLabelText('CPU 부하 시간(초)');
    await userEvent.clear(duration);
    await userEvent.type(duration, '45');
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    expect(state.mutateAsync).toHaveBeenCalledWith({ kind: 'cpu', body: { workers: 3, duration_s: 45 } });
    expect(await screen.findByRole('status')).toHaveTextContent('CPU 워커 3개 · 45초');
    expect(state.queried).toContain(COMMAND_ID);
  });

  it('메모리 크기를 넣어 memory 부하 요청을 보낸다', async () => {
    state.mutateAsync.mockResolvedValue(command('MEMORY', 128, 30));
    render(<StressTestPanel vm={vm} accent="red" />);
    const size = screen.getByLabelText('메모리 부하 크기(MiB)');
    await userEvent.clear(size);
    await userEvent.type(size, '128');
    await userEvent.click(screen.getByRole('button', { name: '메모리 부하 시작' }));
    expect(state.mutateAsync).toHaveBeenCalledWith({
      kind: 'memory',
      body: { memory_mib: 128, duration_s: 30 },
    });
  });

  it('범위를 넘는 값이면 시작 버튼을 막는다', async () => {
    render(<StressTestPanel vm={vm} accent="red" />);
    const size = screen.getByLabelText('메모리 부하 크기(MiB)');
    await userEvent.clear(size);
    await userEvent.type(size, '513');
    expect(screen.getByRole('button', { name: '메모리 부하 시작' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('허용 범위');
  });

  it('Agent 가 ONLINE 이 아니면 모든 시작 버튼을 막는다', () => {
    render(<StressTestPanel vm={{ ...vm, agent_status: 'STALE' }} accent="red" />);
    expect(screen.getByText('Agent 연결 필요')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '메모리 부하 시작' })).toBeDisabled();
  });

  it('AGENT_UNAVAILABLE 응답을 공용 한글 문구로 보여 준다 (원문·코드 없음)', async () => {
    state.mutateAsync.mockRejectedValue(new ApiError(409, { code: 'AGENT_UNAVAILABLE', message: 'x' }));
    render(<StressTestPanel vm={vm} accent="red" />);
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(ERROR_TEXT.AGENT_UNAVAILABLE);
    expect(status).not.toHaveTextContent(/AGENT_UNAVAILABLE|\(/);
    // 보내지 못했으면 잠그지 않는다
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeEnabled();
  });

  it('접수 뒤 명령이 끝날 때까지 "실행 중"으로 버튼을 잠그고, 완료되면 풀고 결과를 한글로', async () => {
    state.mutateAsync.mockResolvedValue(command('CPU', 1, 30));
    const { rerender } = render(<StressTestPanel vm={vm} accent="red" />);
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent(/^실행 중 — CPU 워커 1개 · 30초/);
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '메모리 부하 시작' })).toBeDisabled();
    // 명령 ID 는 글자로 보이지 않고 title 에만
    expect(status).not.toHaveTextContent(COMMAND_ID);
    expect(status).toHaveAttribute('title', COMMAND_ID);

    state.command = { data: { status: 'RUNNING', error: null }, isError: false, error: null };
    rerender(<StressTestPanel vm={vm} accent="red" />);
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeDisabled();

    state.command = { data: { status: 'SUCCEEDED', error: null }, isError: false, error: null };
    rerender(<StressTestPanel vm={vm} accent="red" />);
    expect(screen.getByRole('status')).toHaveTextContent(/^완료 — CPU 워커 1개 · 30초 부하가 끝났습니다/);
    expect(screen.getByRole('status')).toHaveAttribute('data-tone', 'ok');
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeEnabled();
  });

  it.each([
    ['REJECTED', { code: 'BUSY', message: 'stress x is running' }, /^거절 — 이미 실행 중인 부하가 있습니다/],
    ['FAILED', { code: 'PROCESS_EXITED' }, /^실패 — 명령을 처리하지 못했습니다\.$/],
    ['FAILED', null, /^실패 — 명령을 처리하지 못했습니다\.$/],
    ['REJECTED', 'BUSY', /^거절 — 명령을 처리하지 못했습니다\.$/],
    ['REJECTED', { code: 7 }, /^거절 — 명령을 처리하지 못했습니다\.$/],
  ] as const)('Agent %s (error %j) 는 성공처럼 보이지 않고 사유를 한글로', async (status, error, text) => {
    state.mutateAsync.mockResolvedValue(command('MEMORY', 128, 30));
    state.command = {
      data: { status, error: error as CommandStatusResponse['error'] },
      isError: false,
      error: null,
    };
    render(<StressTestPanel vm={vm} accent="red" />);
    await userEvent.click(screen.getByRole('button', { name: '메모리 부하 시작' }));
    const box = await screen.findByRole('status');
    expect(box).toHaveTextContent(text);
    expect(box).toHaveAttribute('data-tone', 'fault');
    expect(box).not.toHaveTextContent(/SUCCEEDED|REJECTED|FAILED|BUSY|stress x/);
    expect(screen.getByRole('button', { name: '메모리 부하 시작' })).toBeEnabled();
  });

  it('명령 조회가 404 면 잠금을 풀고 "명령을 찾을 수 없습니다"', async () => {
    state.mutateAsync.mockResolvedValue(command('CPU', 1, 30));
    state.command = {
      data: undefined,
      isError: true,
      error: new ApiError(404, { code: 'COMMAND_NOT_FOUND', message: 'x' }),
    };
    render(<StressTestPanel vm={vm} accent="red" />);
    await userEvent.click(screen.getByRole('button', { name: 'CPU 부하 시작' }));
    expect(await screen.findByRole('status')).toHaveTextContent(ERROR_TEXT.COMMAND_NOT_FOUND);
    expect(screen.getByRole('button', { name: 'CPU 부하 시작' })).toBeEnabled();
    expect(within(screen.getByRole('status')).queryByText(/\(/)).toBeNull();
  });
});
