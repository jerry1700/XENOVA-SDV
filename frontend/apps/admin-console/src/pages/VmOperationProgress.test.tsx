import { act, render, renderHook, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { formatElapsedTime, vmOperationErrorMessage } from './useVmOperationProgress';
import { ApiError } from '../api/client';
import { VmOperationNotice } from './VmOperationProgress';
import { useVmOperationProgress } from './useVmOperationProgress';
import type { Vm } from '../api/types';

const vmActionMutation = vi.hoisted(() => ({ mutateAsync: vi.fn() }));

vi.mock('../api/queries', () => ({
  useVmAction: () => vmActionMutation,
}));

const VM: Vm = {
  vm_id: 'vm-a',
  name: 'VM A',
  guest_kind: 'linux',
  state: 'running',
  agent_status: 'ONLINE',
  ipv4: '192.0.2.10',
  template_id: 'linux-default',
  template_version: '1',
  vcpus: 2,
  memory_mib: 1024,
};

describe('VM 작업 진행 표시', () => {
  beforeEach(() => {
    vmActionMutation.mutateAsync.mockReset();
  });

  it('경과 시간을 분:초 형식으로 표시한다', () => {
    expect(formatElapsedTime(0)).toBe('00:00');
    expect(formatElapsedTime(65_900)).toBe('01:05');
    expect(formatElapsedTime(-1000)).toBe('00:00');
  });

  it('서버·보드 원문 대신 한글 문구를 쓴다 (실행 중 삭제의 영어 원문 토스트, QA 1-F1)', () => {
    expect(
      vmOperationErrorMessage(
        new ApiError(409, {
          code: 'INVALID_VM_STATE',
          message: 'domain abc is running; stop it before delete',
        }),
      ),
    ).toBe('지금 VM 상태에서는 할 수 없는 작업입니다. VM 상태를 확인해 주세요.');
    expect(vmOperationErrorMessage(new ApiError(500, { code: 'VM_COMMAND_FAILED', message: 'x' }))).toBe(
      'VM 작업을 처리하지 못했습니다.',
    );
    expect(vmOperationErrorMessage(new Error('network failed'))).toBe('VM 작업을 처리하지 못했습니다.');
  });

  it('진행 작업의 종류와 경과 시간을 표시한다', () => {
    render(
      <VmOperationNotice operation={{ action: 'stop', startedAt: 1_000, status: 'pending' }} now={66_000} />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('정지 중…');
    expect(screen.getByRole('status')).toHaveTextContent('경과 01:05');
  });

  it('실패한 작업과 오류 사유를 표시한다', () => {
    render(
      <VmOperationNotice
        operation={{ action: 'delete', startedAt: 1_000, status: 'failed', error: 'dom0 응답 시간 초과' }}
        now={2_000}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('삭제 실패');
    expect(screen.getByRole('alert')).toHaveTextContent('dom0 응답 시간 초과');
  });

  it('같은 VM에서 진행 중인 요청은 중복 실행하지 않는다', () => {
    vmActionMutation.mutateAsync.mockReturnValue(new Promise(() => undefined));
    const { result } = renderHook(() => useVmOperationProgress());

    act(() => {
      result.current.run(VM, 'stop');
      result.current.run(VM, 'delete');
    });

    expect(vmActionMutation.mutateAsync).toHaveBeenCalledTimes(1);
    expect(vmActionMutation.mutateAsync).toHaveBeenCalledWith({ vmId: 'vm-a', action: 'stop' });
    expect(result.current.operations['vm-a']).toMatchObject({ action: 'stop', status: 'pending' });
  });
});
