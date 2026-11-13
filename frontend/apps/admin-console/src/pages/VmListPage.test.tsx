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

describe('VM 자원 요약', () => {
  it('vCPU·메모리는 실행 중 VM 만 합산하고, 등록 VM 전체 합계는 보조 줄로 보인다', async () => {
    const items = [
      { ...VM, vm_id: 'vm-1', name: 'a', state: 'running', vcpus: 1, memory_mib: 512 },
      { ...VM, vm_id: 'vm-2', name: 'b', state: 'running', vcpus: 2, memory_mib: 1024 },
      { ...VM, vm_id: 'vm-3', name: 'c', state: 'stopped', vcpus: 1, memory_mib: 256 },
    ] as VmListResponse['items'];
    stubBackend(
      () => Response.json({ items, observed_at: new Date().toISOString() }),
      () => Promise.resolve(Response.json(sync())),
    );
    renderPage();
    const summary = await screen.findByRole('region', { name: 'VM 자원 요약' });
    await waitFor(() => expect(summary).toHaveTextContent('VCPU IN USE3 vCPU'));
    const vcpu = screen.getByText('VCPU IN USE').parentElement!;
    expect(vcpu.querySelector('strong')).toHaveTextContent(/^3 vCPU$/);
    expect(vcpu.querySelector('small')).toHaveTextContent(/^등록 4 vCPU · 물리 4코어 공유$/);
    const memory = screen.getByText('MEMORY IN USE').parentElement!;
    expect(memory.querySelector('strong')).toHaveTextContent(/^1,536 MiB$/);
    expect(memory.querySelector('small')).toHaveTextContent(/^등록 1,792 MiB$/);
    expect(summary).not.toHaveTextContent('/ 4 cores');
    expect(screen.queryByText('ALLOCATED VCPU')).not.toBeInTheDocument();
  });
});

describe('VM 삭제 — 정지된 VM만 (QA 1-F1·1-F9)', () => {
  const listWith = (state: string) => () =>
    new Response(JSON.stringify({ items: [{ ...VM, state }], observed_at: new Date().toISOString() }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

  it('실행 중이면 [삭제] 가 꺼져 있고 정지 후 삭제할 수 있다고 알린다', async () => {
    stubBackend(listWith('running'), () => Promise.resolve(new Response(null, { status: 500 })));
    renderPage();
    const button = await screen.findByRole('button', { name: '삭제' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', '정지 후 삭제할 수 있습니다.');
  });

  it('정지된 VM 은 삭제할 수 있고, 확인 창은 실제 동작대로 안내한다', async () => {
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    stubBackend(listWith('stopped'), () => Promise.resolve(new Response(null, { status: 500 })));
    renderPage();
    const button = await screen.findByRole('button', { name: '삭제' });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(screen.getByText(/정지된 VM만 삭제할 수 있습니다/)).toBeInTheDocument();
    expect(screen.getByText(/삭제한 VM은 목록에서 사라지며 되돌릴 수 없습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/VM을 종료하고 연결된 디스크를 삭제합니다/)).toBeNull();
  });
});
