import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { Vm } from '../api/types';
import { BUSY_LOCK_MS, useVmOperationProgress } from './useVmOperationProgress';

const action = vi.hoisted(() => ({ mutateAsync: vi.fn() }));
vi.mock('../api/queries', () => ({ useVmAction: () => action }));

const vm = { vm_id: 'vm-a', name: 'debian-dev', state: 'stopped' } as Vm;

beforeEach(() => {
  vi.useFakeTimers();
  action.mutateAsync.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('다른 작업이 진행 중인 VM 잠금 (S15P31E110-473)', () => {
  it('서버가 409 VM_OPERATION_IN_PROGRESS 를 주면 그 VM 을 10초 잠그고 풀어 준다', async () => {
    action.mutateAsync.mockRejectedValue(
      new ApiError(409, { code: 'VM_OPERATION_IN_PROGRESS', message: 'busy' }),
    );
    const { result } = renderHook(() => useVmOperationProgress());
    await act(async () => {
      result.current.run(vm, 'start');
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(BUSY_LOCK_MS).toBe(10_000);
    expect(result.current.isBusy('vm-a')).toBe(true);
    expect(result.current.isBusy('vm-b')).toBe(false);
    expect(result.current.operations['vm-a']?.error).toBe(
      '이 VM에 다른 작업이 진행 중입니다. 끝난 뒤 다시 시도해 주세요.',
    );

    await act(() => vi.advanceTimersByTimeAsync(BUSY_LOCK_MS - 1));
    expect(result.current.isBusy('vm-a')).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(result.current.isBusy('vm-a')).toBe(false);
  });

  it('다른 오류는 잠그지 않는다', async () => {
    action.mutateAsync.mockRejectedValue(new ApiError(409, { code: 'INVALID_VM_STATE', message: 'x' }));
    const { result } = renderHook(() => useVmOperationProgress());
    await act(async () => {
      result.current.run(vm, 'start');
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.isBusy('vm-a')).toBe(false);
  });
});
