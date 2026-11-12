import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { VmListResponse, VmSynchronizationResult } from '../api/types';
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

function sync(overrides: Partial<VmSynchronizationResult> = {}): VmSynchronizationResult {
  return {
    discovered: 1,
    registered: 0,
    updated: 0,
    unchanged: 1,
    conflicts: 0,
    incomplete: 0,
    missing_from_board: 0,
    retired: 0,
    results: [],
    ...overrides,
  };
}

/** 목록 GET 과 등록 동기화 POST 를 URL·메서드로 나눠 답한다 */
function stubBackend(list: () => Response, synchronize: () => Promise<Response>) {
  const calls: string[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.endsWith('/api/v1/vms/synchronizations') && method === 'POST') {
      calls.push('sync');
      return synchronize();
    }
    calls.push('list');
    return Promise.resolve(list());
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

describe('VM 등록 동기화 버튼', () => {
  it('등록 동기화를 한 번 요청하고, 끝날 때까지 중복 클릭을 막고, 결과를 알린 뒤 목록을 다시 읽는다', async () => {
    let release = (_value: Response) => {};
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    let name = VM.name;
    const calls = stubBackend(
      () => Response.json(response('2026-11-12T01:00:00Z', name)),
      () => pending,
    );
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));

    const pendingButton = screen.getByRole('button', { name: '동기화 중…' });
    expect(pendingButton).toBeDisabled();
    await user.click(pendingButton);
    expect(calls.filter((call) => call === 'sync')).toHaveLength(1);

    name = 'debian-registered';
    const listsBefore = calls.filter((call) => call === 'list').length;
    await act(async () => release(Response.json(sync({ discovered: 2, registered: 1, retired: 1 }))));

    expect(await screen.findByText('VM 동기화 완료: 보드 2대 · 추가 1 · 정리 1')).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((call) => call === 'list').length).toBeGreaterThan(listsBefore));
    expect(await screen.findByText('debian-registered')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'VM 동기화' })).toBeEnabled();
  });

  it('바뀐 것이 없으면 그렇게 알린다', async () => {
    stubBackend(
      () => Response.json(response('2026-11-12T01:00:00Z')),
      async () => Response.json(sync()),
    );
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));
    expect(await screen.findByText('VM 동기화 완료: 보드 1대, 바뀐 것 없음')).toBeInTheDocument();
  });

  it('503이면 기존 목록을 유지하고 오류를 알린다', async () => {
    stubBackend(
      () => Response.json(response('2026-11-12T01:00:00Z')),
      async () =>
        Response.json(
          { code: 'VM_CONTROL_UNAVAILABLE', message: 'VM 제어 서비스에 연결할 수 없습니다.' },
          { status: 503 },
        ),
    );
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));

    expect(
      await screen.findByText(
        'VM 동기화에 실패했습니다. VM 제어 서비스에 연결할 수 없습니다. (VM_CONTROL_UNAVAILABLE)',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('debian-dev')).toBeInTheDocument();
  });
});
