import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { initialVms, VM_IDS } from '../mocks/data';
import { ControlVmDetailPage } from './ControlVmDetailPage';

const VMS = initialVms();
const resourcesQuery = vi.hoisted(() => ({
  data: undefined as { vcpu_pinning: { vcpu: number; pcpus: number[] }[] } | undefined,
  error: null as unknown,
  isPending: false,
}));

vi.mock('../api/queries', () => ({
  useVm: (vmId: string) => ({ data: VMS.find((vm) => vm.vm_id === vmId), error: null, isPending: false }),
  useVms: () => ({ data: { items: VMS }, error: null, isPending: false }),
  useVmResources: () => resourcesQuery,
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
  it('Zephyr VM은 고정 프로필이라 조정 패널을 숨기지만 전용 코어 핀닝 패널은 보인다', () => {
    renderDetail(VM_IDS.autonomous);
    expect(screen.queryByLabelText('vCPU 및 메모리 조정')).toBeNull();
    expect(screen.getByLabelText('vCPU 핀닝 설정')).toBeInTheDocument();
    // 요약 카드의 할당 자원은 그대로 보인다
    expect(screen.getByText('할당 자원')).toBeInTheDocument();
  });

  it('Linux VM은 자원 조정과 핀닝 패널을 모두 보이고 미연결 문구는 없다', () => {
    renderDetail(VM_IDS.interaction);
    expect(screen.getByLabelText('vCPU 및 메모리 조정')).toBeInTheDocument();
    expect(screen.getByLabelText('vCPU 핀닝 설정')).toBeInTheDocument();
    expect(screen.queryByText(/핀닝 변경 API가 아직 연결되지 않았습니다/)).toBeNull();
    expect(screen.queryByText('변경 API 미연결')).toBeNull();
  });

  it('할당 자원 요약에 현재 핀닝된 pCPU 를 보인다', () => {
    resourcesQuery.data = { vcpu_pinning: [{ vcpu: 0, pcpus: [0, 3] }] };
    renderDetail(VM_IDS.interaction);
    expect(screen.getByText('0, 3')).toBeInTheDocument();
    resourcesQuery.data = undefined;
  });

  it('핀닝 조회에 실패하면 요약에 조회 실패를 보인다', () => {
    resourcesQuery.error = new Error('boom');
    renderDetail(VM_IDS.interaction);
    expect(screen.getByText('조회 실패')).toBeInTheDocument();
    resourcesQuery.error = null;
  });
});

describe('VM Control 상세 삭제 — 정지된 VM만 (QA 1-F1·1-F9)', () => {
  it('실행 중이면 [VM 삭제] 가 꺼져 있고 정지 후 삭제할 수 있다고 알린다', () => {
    renderDetail(VM_IDS.interaction);
    expect(screen.getByRole('button', { name: 'VM 삭제' })).toBeDisabled();
    expect(screen.getAllByText(/정지 후 삭제할 수 있습니다/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/VM을 종료하고 연결된 디스크를 삭제합니다/)).toBeNull();
  });
});
