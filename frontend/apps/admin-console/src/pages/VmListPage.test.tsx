import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '@xenova/ui';
import { act, render, screen, waitFor, within } from '@testing-library/react';
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
    restored: 0,
    retired: 0,
    skipped: 0,
    conflicts: 0,
    incomplete: 0,
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

  it('vm_id·이름이 모두 없는 항목은 "알 수 없는 VM", 같은 문구가 겹쳐도 토스트가 깨지지 않는다 (S15P31E110-473)', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    stubBackend(
      () => Response.json(response('2026-11-12T01:00:00Z')),
      async () =>
        Response.json(
          sync({
            discovered: 3,
            unchanged: 0,
            conflicts: 2,
            incomplete: 1,
            results: [
              { vm_id: null, name: null, action: 'incomplete', reason: 'invalid_vm_id' },
              { vm_id: 'dup-0001-0000', name: 'twin', action: 'conflict', reason: 'duplicate_board_vm_id' },
              { vm_id: 'dup-0001-0000', name: 'twin', action: 'conflict', reason: 'duplicate_board_vm_id' },
            ],
          }),
        ),
    );
    renderPage();
    const user = userEvent.setup();
    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));

    const problems = await screen.findByRole('list', { name: '동기화 확인 필요' });
    expect(problems).toHaveTextContent('알 수 없는 VM: UUID 형식이 아님');
    expect(within(problems).getAllByText('twin: 보드에 같은 UUID가 두 번 있음')).toHaveLength(2);
    // React key 중복 경고가 없다
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/same key/);
    errors.mockRestore();
  });

  it('보류·충돌·정보 부족은 성공 응답이어도 VM 이름과 사유를 같이 보인다', async () => {
    stubBackend(
      () => Response.json(response('2026-11-12T01:00:00Z')),
      async () =>
        Response.json(
          sync({
            discovered: 3,
            unchanged: 0,
            retired: 1,
            skipped: 1,
            conflicts: 1,
            incomplete: 1,
            results: [
              {
                vm_id: '11111111-0000-4000-8000-000000000001',
                name: 'old-debian',
                action: 'retired',
                reason: 'missing_from_board',
              },
              {
                vm_id: '11111111-0000-4000-8000-000000000002',
                name: 'zephyr-a',
                action: 'skipped',
                reason: 'in_operation',
              },
              {
                vm_id: '11111111-0000-4000-8000-000000000003',
                name: 'debian',
                action: 'conflict',
                reason: 'duplicate_name',
              },
              {
                vm_id: 'abcdef12-0000-4000-8000-000000000004',
                name: null,
                action: 'incomplete',
                reason: 'registration_metadata_missing',
              },
            ],
          }),
        ),
    );
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'VM 동기화' }));

    expect(
      await screen.findByText('VM 동기화 완료: 보드 3대 · 정리 1 · 보류 1 · 충돌 1 · 정보 부족 1'),
    ).toBeInTheDocument();
    const problems = screen.getByRole('list', { name: '동기화 확인 필요' });
    expect(problems).toHaveTextContent('zephyr-a: 작업 진행 중이라 이번엔 정리하지 않음');
    expect(problems).toHaveTextContent('debian: 이미 다른 VM이 쓰는 이름');
    expect(problems).toHaveTextContent('abcdef12: 보드 등록 정보가 부족함');
    expect(problems).not.toHaveTextContent('old-debian');
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
        'VM 동기화에 실패했습니다. 보드에 연결할 수 없습니다. 보드 상태를 확인해 주세요.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/VM_CONTROL_UNAVAILABLE/)).not.toBeInTheDocument();
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

describe('결과를 모르는 생성 기록 (S15P31E110-473)', () => {
  afterEach(() => {
    localStorage.clear();
    window.dispatchEvent(new StorageEvent('storage', { key: 'xenova.vm-create.pending' }));
  });

  it('다른 탭에서 남은 기록은 "생성 결과 확인 필요"와 [기록 지우기]로 보이고 카드는 잠그지 않는다', async () => {
    stubBackend(
      () => Response.json({ items: [{ ...VM, state: 'stopped' }], observed_at: '2026-11-15T01:00:00Z' }),
      async () => Response.json(sync()),
    );
    renderPage();
    const user = userEvent.setup();
    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    // 다른 탭이 생성 기록을 남겼다 (이 탭이 보낸 요청이 아님)
    localStorage.setItem(
      'xenova.vm-create.pending',
      JSON.stringify([{ name: 'debian-dev', startedAt: Date.now() }]),
    );
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'xenova.vm-create.pending' }));
    });

    expect(await screen.findByText('생성 결과 확인 필요')).toBeInTheDocument();
    expect(screen.queryByText('VM 생성 작업 진행 중')).toBeNull();
    // 카드는 '생성 중'으로 잠기지 않는다 — 상세로 갈 수 있고 시작할 수 있다
    expect(screen.getByRole('link', { name: 'debian-dev 상세' })).toBeInTheDocument();
    expect(screen.queryByText('dom0에서 VM을 생성하고 있습니다.')).toBeNull();
    expect(screen.getByRole('button', { name: '시작' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: 'debian-dev 생성 기록 지우기' }));
    expect(screen.queryByText('생성 결과 확인 필요')).toBeNull();
    expect(localStorage.getItem('xenova.vm-create.pending')).toBe('[]');
  });
});

describe('자원 적용 중인 VM 카드 (S15P31E110-473)', () => {
  it('이 탭에서 그 VM 자원 적용이 진행 중이면 카드의 시작·정지를 잠근다', async () => {
    stubBackend(
      () => Response.json(response('2026-11-15T01:00:00Z')),
      async () => Response.json(sync()),
    );
    const client = renderPage();
    expect(await screen.findByText('debian-dev')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '정지' })).toBeEnabled();

    // 상세 화면의 자원 적용(PUT /resources)이 아직 끝나지 않았다
    act(() => {
      void client
        .getMutationCache()
        .build(client, {
          mutationKey: ['vm-resources-update', 'vm-1'],
          mutationFn: () => new Promise(() => {}),
        })
        .execute(undefined);
    });
    await waitFor(() => expect(screen.getByRole('button', { name: '정지' })).toBeDisabled());
  });
});
