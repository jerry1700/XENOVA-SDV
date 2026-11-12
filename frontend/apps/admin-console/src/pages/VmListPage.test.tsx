import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { VmListResponse } from '../api/types';
import { VmListPage } from './VmListPage';

const VM = {
  vm_id: 'vm-1',
  name: 'debian-dev',
  guest_kind: 'linux',
  state: 'running',
  template_id: 'debian',
  template_version: '13',
  vcpus: 1,
  memory_mib: 512,
} as VmListResponse['items'][number];

function response(observedAt: string, name = VM.name): VmListResponse {
  return { items: [{ ...VM, name }], observed_at: observedAt };
}

function displayedTime(iso: string): string {
  return new Date(iso).toTimeString().slice(0, 8);
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <VmListPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe('VM 수동 동기화', () => {
  it('클릭 한 번에 한 번 요청하고 완료 전에는 중복 클릭을 막은 뒤 목록과 시각을 갱신한다', async () => {
    let release = (_value: Response) => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(response('2026-11-12T01:00:00Z')))
      .mockReturnValueOnce(pending);
    vi.stubGlobal('fetch', fetchMock);
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    expect(screen.getByText(displayedTime('2026-11-12T01:00:00Z'))).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'VM 동기화' });
    await user.click(button);

    const pendingButton = screen.getByRole('button', { name: '동기화 중…' });
    expect(pendingButton).toBeDisabled();
    await user.click(pendingButton);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => release(Response.json(response('2026-11-12T01:02:03Z', 'debian-synced'))));

    expect(await screen.findByText('debian-synced')).toBeInTheDocument();
    expect(screen.getByText(displayedTime('2026-11-12T01:02:03Z'))).toBeInTheDocument();
    expect(await screen.findByText('VM 목록을 동기화했습니다.')).toBeInTheDocument();
  });

  it('503이면 기존 목록을 유지하고 오류를 알린다', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(response('2026-11-12T01:00:00Z')))
      .mockResolvedValueOnce(
        Response.json(
          { code: 'VM_CONTROL_UNAVAILABLE', message: 'VM 제어 서비스에 연결할 수 없습니다.' },
          { status: 503 },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByText('debian-dev')).toBeInTheDocument();
    expect(
      await screen.findByText(
        'VM 목록 동기화에 실패했습니다. VM 제어 서비스에 연결할 수 없습니다. (VM_CONTROL_UNAVAILABLE)',
      ),
    ).toBeInTheDocument();
  });
});
