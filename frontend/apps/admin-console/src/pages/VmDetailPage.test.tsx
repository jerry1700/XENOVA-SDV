import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { VmDetailPage } from './VmDetailPage';

const vmQuery = vi.hoisted(() => ({
  value: {} as Record<string, unknown>,
  eventsEnabled: [] as (boolean | undefined)[],
}));

vi.mock('../api/queries', async (importOriginal) => ({
  vmGone: (await importOriginal<typeof import('../api/queries')>()).vmGone,
  useVm: () => vmQuery.value,
  useEvents: (_params: unknown, options?: { enabled?: boolean }) => {
    vmQuery.eventsEnabled.push(options?.enabled);
    return { data: undefined, error: null, isPending: true };
  },
}));
vi.mock('../layout/DriveContext', () => ({
  useDrive: () => ({ drive: null, socket: 'connecting' }),
}));

function renderDetail() {
  const router = createMemoryRouter([{ path: '/dashboard/vms/:vmId', element: <VmDetailPage /> }], {
    initialEntries: ['/dashboard/vms/missing'],
  });
  return render(<RouterProvider router={router} />);
}

describe('VM 상세 부제 (S15P31E110-469)', () => {
  it('조회 중에는 "불러오는 중"', () => {
    vmQuery.value = { data: undefined, error: null, isPending: true };
    renderDetail();
    expect(screen.getByText(/VM 정보를 불러오는 중입니다\./)).toBeInTheDocument();
  });

  it('조회가 실패하면 "불러오는 중"이 남지 않고 "불러오지 못했습니다"', () => {
    vmQuery.value = {
      data: undefined,
      error: new ApiError(404, { code: 'VM_NOT_FOUND', message: '존재하지 않는 VM입니다.' }),
      isPending: false,
    };
    renderDetail();
    expect(screen.getByText(/VM 정보를 불러오지 못했습니다\./)).toBeInTheDocument();
    expect(screen.queryByText(/불러오는 중/)).toBeNull();
    // 오류 코드는 화면 문구가 아니라 title 로만 (S15P31E110-471)
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('VM을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.');
    expect(alert).not.toHaveTextContent('VM_NOT_FOUND');
    expect(alert).toHaveAttribute('title', 'VM_NOT_FOUND');
  });

  it('VM 이 없으면(404) 이 VM 의 이벤트 조회도 끈다', () => {
    vmQuery.eventsEnabled = [];
    vmQuery.value = { data: undefined, error: null, isPending: true };
    const view = renderDetail();
    expect(vmQuery.eventsEnabled.at(-1)).toBe(true);
    view.unmount();

    vmQuery.value = {
      data: undefined,
      error: new ApiError(404, { code: 'VM_NOT_FOUND', message: '존재하지 않는 VM입니다.' }),
      isPending: false,
    };
    renderDetail();
    expect(vmQuery.eventsEnabled.at(-1)).toBe(false);
  });
});
