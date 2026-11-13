import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { initialVms, VM_IDS } from '../mocks/data';
import { ControlVmDetailPage } from './ControlVmDetailPage';

const VMS = initialVms();

vi.mock('../api/queries', () => ({
  useVm: (vmId: string) => ({ data: VMS.find((vm) => vm.vm_id === vmId), error: null, isPending: false }),
  useVms: () => ({ data: { items: VMS }, error: null, isPending: false }),
  useVmResources: () => ({ data: undefined, error: null, isPending: false }),
}));

vi.mock('./useVmOperationProgress', () => ({
  useVmOperationProgress: () => ({ operations: {}, now: 0, run: vi.fn() }),
}));

// 패널 내부(자원 조회·스트림)는 이 테스트의 관심 밖이라 자리만 확인한다
vi.mock('./ResourceAdjustmentPanel', () => ({
  ResourceAdjustmentPanel: () => <section aria-label="vCPU 및 메모리 조정" />,
}));
vi.mock('./VcpuPinningPanel', () => ({
  VcpuPinningPanel: () => <section aria-label="vCPU 핀닝 설정" />,
}));

function renderDetail(vmId: string) {
  const router = createMemoryRouter([{ path: '/control/vms/:vmId', element: <ControlVmDetailPage /> }], {
    initialEntries: [`/control/vms/${vmId}`],
  });
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe('VM Control 상세 자원 패널', () => {
  it('Zephyr VM은 조정·핀닝 패널을 숨긴다', () => {
    renderDetail(VM_IDS.autonomous);
    expect(screen.queryByLabelText('vCPU 및 메모리 조정')).toBeNull();
    expect(screen.queryByLabelText('vCPU 핀닝 설정')).toBeNull();
    // 요약 카드의 할당 자원은 그대로 보인다
    expect(screen.getByText('할당 자원')).toBeInTheDocument();
  });

  it('Linux VM은 자원 조정과 핀닝 미연결 상태를 보인다', () => {
    renderDetail(VM_IDS.interaction);
    expect(screen.getByLabelText('vCPU 및 메모리 조정')).toBeInTheDocument();
    expect(screen.getByText('vCPU 핀닝 변경 API가 아직 연결되지 않았습니다.')).toBeInTheDocument();
  });
});
