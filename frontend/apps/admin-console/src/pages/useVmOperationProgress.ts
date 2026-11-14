import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import { errorText } from '../api/errorText';
import { useVmAction, type VmAction } from '../api/queries';
import type { Vm } from '../api/types';

export type VmOperation = {
  action: VmAction;
  startedAt: number;
  status: 'pending' | 'failed';
  error?: string;
};

type OperationCallbacks = {
  onSuccess?: () => void;
  onError?: (error: unknown, message: string) => void;
};

/** 서버가 409 VM_OPERATION_IN_PROGRESS 를 주면 그 VM 의 시작·정지·삭제를 잠시 잠근다 (S15P31E110-473) */
export const BUSY_LOCK_MS = 10_000;

export function useVmOperationProgress() {
  const mutation = useVmAction();
  const pendingVmIds = useRef(new Set<string>());
  const [operations, setOperations] = useState<Record<string, VmOperation>>({});
  const [now, setNow] = useState(() => Date.now());
  /** 다른 작업이 진행 중이라 서버가 거절한 VM — 잠금이 풀리는 시각 */
  const [busyUntil, setBusyUntil] = useState<Record<string, number>>({});

  useEffect(() => {
    const releases = Object.values(busyUntil);
    if (releases.length === 0) return;
    const timer = window.setTimeout(
      () => {
        const at = Date.now();
        setBusyUntil((current) =>
          Object.fromEntries(Object.entries(current).filter(([, until]) => until > at)),
        );
      },
      Math.max(0, Math.min(...releases) - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [busyUntil]);
  const hasPendingOperation = Object.values(operations).some((operation) => operation.status === 'pending');

  useEffect(() => {
    if (!hasPendingOperation) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasPendingOperation]);

  const run = (vm: Vm, action: VmAction, callbacks: OperationCallbacks = {}) => {
    if (pendingVmIds.current.has(vm.vm_id)) return;

    pendingVmIds.current.add(vm.vm_id);
    const startedAt = Date.now();
    setNow(startedAt);
    setOperations((current) => ({
      ...current,
      [vm.vm_id]: { action, startedAt, status: 'pending' },
    }));

    void mutation
      .mutateAsync({ vmId: vm.vm_id, action })
      .then(() => {
        pendingVmIds.current.delete(vm.vm_id);
        setOperations((current) => withoutOperation(current, vm.vm_id));
        callbacks.onSuccess?.();
      })
      .catch((error: unknown) => {
        pendingVmIds.current.delete(vm.vm_id);
        if (error instanceof ApiError && error.code === 'VM_OPERATION_IN_PROGRESS') {
          setBusyUntil((current) => ({ ...current, [vm.vm_id]: Date.now() + BUSY_LOCK_MS }));
        }
        const message = vmOperationErrorMessage(error);
        setOperations((current) => ({
          ...current,
          [vm.vm_id]: { action, startedAt, status: 'failed', error: message },
        }));
        callbacks.onError?.(error, message);
      });
  };

  /** 다른 작업이 진행 중이라고 서버가 알려 와 잠시 잠근 VM 인가 */
  const isBusy = (vmId: string) => busyUntil[vmId] !== undefined;

  return { operations, now, run, isBusy };
}

export { formatElapsedTime } from '../format';

/** 서버·보드 원문(예: `domain … is running; stop it before delete`) 대신 한글 문구 */
export function vmOperationErrorMessage(error: unknown): string {
  return errorText(error, 'VM 작업을 처리하지 못했습니다.');
}

function withoutOperation(
  operations: Record<string, VmOperation>,
  vmId: string,
): Record<string, VmOperation> {
  const next = { ...operations };
  delete next[vmId];
  return next;
}
