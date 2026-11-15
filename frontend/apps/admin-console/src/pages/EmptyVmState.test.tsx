import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsolePage } from './ConsolePage';
import { ResourcesPage } from './ResourcesPage';

const state = vi.hoisted(() => ({
  vms: { data: { items: [] as unknown[] }, isPending: false, isSuccess: true },
}));

vi.mock('../api/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/queries')>()),
  useVms: () => state.vms,
}));
vi.mock('../layout/DriveContext', () => ({ useDrive: () => ({ drive: null, socket: null }) }));
vi.mock('../layout/LiveDataAlert', () => ({ LiveDataAlert: () => null }));
vi.mock('../console/RuntimeLogsPanel', () => ({ RuntimeLogsPanel: () => null }));

beforeEach(() => {
  state.vms = { data: { items: [] }, isPending: false, isSuccess: true };
});

describe('VM 0대일 때 빈 상태 문구 (Overview·VM 목록과 같은 모양)', () => {
  it.each(['cpu', 'memory', 'network'] as const)('%s 탭', (view) => {
    render(<ResourcesPage view={view} />);
    expect(screen.getByText('등록된 VM이 없습니다.')).toBeInTheDocument();
  });

  it('콘솔 탭', () => {
    render(
      <MemoryRouter>
        <ConsolePage />
      </MemoryRouter>,
    );
    expect(screen.getByText('등록된 VM이 없습니다.')).toBeInTheDocument();
  });

  it('불러오는 중에는 빈 상태 문구를 보이지 않는다', () => {
    state.vms = { data: undefined as never, isPending: true, isSuccess: false };
    render(<ResourcesPage view="cpu" />);
    expect(screen.getByText('VM 정보를 불러오는 중입니다…')).toBeInTheDocument();
    expect(screen.queryByText('등록된 VM이 없습니다.')).toBeNull();
  });
});
