import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { getSession, refreshSession, request, requestWithStatus, setSession } from '../api/client';
import {
  VM_CPU_PERCENT_METRIC,
  type AgentExitCommand,
  type CommandStatusResponse,
  type MetricHistoryResponse,
  type TokenResponse,
  type StressCommand,
  type VmResources,
  type Vm,
  type VmListResponse,
} from '../api/types';
import { canStartVm, canStopVm } from '../format';
import { setMockBoardDown } from './board';
import { MOCK_PASSWORD, MOCK_USER, VM_IDS } from './data';
import { handlers, resetMockState } from './handlers';

// 화면이 쓰는 실제 클라이언트(api/client.ts)로 mock 흐름을 돌린다
const server = setupServer(...handlers);

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
  // Node fetch 는 상대 경로를 못 받는다. 브라우저처럼 현재 origin 을 붙인다 (MSW 가 감싼 fetch 위에)
  const mocked = globalThis.fetch;
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    mocked(typeof input === 'string' ? new URL(input, window.location.origin) : input, init),
  );
});
afterEach(() => {
  resetMockState();
  setSession(null);
});
afterAll(() => {
  vi.unstubAllGlobals();
  server.close();
});

async function login(password = MOCK_PASSWORD) {
  const token = await request<TokenResponse>('/api/v1/auth/login', {
    method: 'POST',
    body: { username: MOCK_USER.username, password },
    auth: false,
  });
  setSession(token);
  return token;
}

describe('MSW mock API', () => {
  it('로그인 → 목록 → 상세', async () => {
    const token = await login();
    expect(token.user.username).toBe(MOCK_USER.username);

    const list = await request<VmListResponse>('/api/v1/vms');
    expect(list.items.map((vm) => vm.name)).toEqual(['autonomous', 'manual', 'interaction']);

    const vm = await request<Vm>(`/api/v1/vms/${list.items[2]!.vm_id}`);
    expect(vm.guest_kind).toBe('linux');
    expect(vm.agent_status).toBe('ONLINE');
  });

  it('mock 보드를 끊으면 VM 목록·상세가 502 VM_CONTROL_BAD_RESPONSE, 다시 이으면 정상', async () => {
    await login();
    setMockBoardDown(true);
    await expect(request('/api/v1/vms')).rejects.toMatchObject({
      status: 502,
      code: 'VM_CONTROL_BAD_RESPONSE',
    });
    await expect(request(`/api/v1/vms/${VM_IDS.interaction}`)).rejects.toMatchObject({ status: 502 });

    setMockBoardDown(false);
    const list = await request<VmListResponse>('/api/v1/vms');
    expect(list.items).toHaveLength(3);
  });

  it('틀린 비밀번호는 INVALID_CREDENTIALS, 토큰 없이는 401', async () => {
    await expect(login('wrong')).rejects.toMatchObject({ status: 401, code: 'INVALID_CREDENTIALS' });
    await expect(request('/api/v1/vms', { auth: false })).rejects.toMatchObject({ status: 401 });
  });

  it('refresh 쿠키로 새 토큰을 받는다 (새로고침 복원)', async () => {
    const first = await login();
    setSession(null);
    expect(await refreshSession()).toBe(true);
    expect(getSession()?.access_token).not.toBe(first.access_token);
  });

  it('정지 → 시작, 같은 상태로 다시 보내면 그대로 200 (계약 §5, backend VmService 와 같음)', async () => {
    await login();
    const id = '11111111-1111-4111-8111-111111111111';
    expect((await request<Vm>(`/api/v1/vms/${id}/stop`, { method: 'POST' })).state).toBe('stopped');
    expect((await request<Vm>(`/api/v1/vms/${id}/stop`, { method: 'POST' })).state).toBe('stopped');
    expect((await request<Vm>(`/api/v1/vms/${id}/start`, { method: 'POST' })).state).toBe('running');
    expect((await request<Vm>(`/api/v1/vms/${id}/start`, { method: 'POST' })).state).toBe('running');
  });

  it('canStartVm·canStopVm — crashed·paused 는 정지만 되고, 정지 뒤 시작한다', () => {
    expect(canStartVm('stopped')).toBe(true);
    for (const state of ['running', 'crashed', 'paused', 'stopping'] as const)
      expect(canStartVm(state)).toBe(false);
    for (const state of ['running', 'crashed', 'paused', 'stopping'] as const)
      expect(canStopVm(state)).toBe(true);
    expect(canStopVm('stopped')).toBe(false);
  });

  it('생성은 입력값을 검사하고, 같은 이름·다른 사양은 VM_NAME_CONFLICT, 같은 사양 재요청은 200 + 기존 VM (backend VmService.create)', async () => {
    await login();
    const body = {
      name: 'extra',
      template_id: 'debian-13-arm64',
      template_version: '1.0.0',
      vcpus: 1,
      memory_mib: 256,
    };
    const created = await requestWithStatus<Vm>('/api/v1/vms', { method: 'POST', body });
    expect(created.status).toBe(201);
    expect(created.data.name).toBe('extra');
    const again = await requestWithStatus<Vm>('/api/v1/vms', { method: 'POST', body });
    expect(again.status).toBe(200);
    expect(again.data.vm_id).toBe(created.data.vm_id);
    await expect(
      request('/api/v1/vms', { method: 'POST', body: { ...body, memory_mib: 512 } }),
    ).rejects.toMatchObject({
      status: 409,
      code: 'VM_NAME_CONFLICT',
      message: '이미 사용 중인 VM 이름입니다.',
    });
    await expect(
      request('/api/v1/vms', { method: 'POST', body: { ...body, name: ' x', vcpus: 9 } }),
    ).rejects.toMatchObject({
      status: 422,
      code: 'VALIDATION_ERROR',
      message: '요청 값이 올바르지 않습니다.',
      fieldErrors: [
        { field: 'name', reason: 'name은 1~63자이며 제어문자와 앞뒤 공백은 사용할 수 없습니다.' },
        { field: 'vcpus', reason: 'vcpus는 1~4여야 합니다.' },
      ],
    });
  });

  it('생성 검증 문구·순서가 backend 원문과 같다 (CreateVmRequest → 템플릿 → 템플릿 최소 메모리)', async () => {
    await login();
    const body = {
      name: 'extra',
      template_id: 'debian-13-arm64',
      template_version: '1.0.0',
      vcpus: 1,
      memory_mib: 256,
    };
    const fields = (patch: object) =>
      request('/api/v1/vms', { method: 'POST', body: { ...body, ...patch } }).catch(
        (e: { fieldErrors?: { field: string; reason: string }[] }) => e.fieldErrors,
      );
    expect(await fields({ name: '' })).toEqual([
      { field: 'name', reason: 'name은 필수입니다.' },
      { field: 'name', reason: 'name은 1~63자이며 제어문자와 앞뒤 공백은 사용할 수 없습니다.' },
    ]);
    expect(await fields({ name: 'a'.repeat(64) })).toEqual([
      { field: 'name', reason: 'name은 1~63자이며 제어문자와 앞뒤 공백은 사용할 수 없습니다.' },
    ]);
    expect(await fields({ name: 'a\u0007b' })).toHaveLength(1);
    expect(await fields({ name: 'a'.repeat(63) })).toMatchObject({ name: 'a'.repeat(63) });
    expect(await fields({ memory_mib: 2048 })).toEqual([
      { field: 'memory_mib', reason: 'memory_mib는 16~1024여야 합니다.' },
    ]);
    // 요청 검증을 통과해야 템플릿을 본다
    await expect(
      request('/api/v1/vms', { method: 'POST', body: { ...body, template_id: 'nope', vcpus: 9 } }),
    ).rejects.toMatchObject({ status: 422, fieldErrors: [{ field: 'vcpus' }] });
    await expect(
      request('/api/v1/vms', { method: 'POST', body: { ...body, template_id: 'nope' } }),
    ).rejects.toMatchObject({
      status: 404,
      code: 'TEMPLATE_NOT_FOUND',
      message: '존재하지 않는 템플릿입니다.',
    });
    // 16~1024 안이어도 템플릿 최소값보다 작으면 errors 없는 422
    const min = await request('/api/v1/vms', { method: 'POST', body: { ...body, memory_mib: 64 } }).catch(
      (e: unknown) => e,
    );
    expect(min).toMatchObject({
      status: 422,
      code: 'VALIDATION_ERROR',
      message: expect.stringMatching(/^memory_mib는 이 템플릿의 최소값\(\d+\) 이상이어야 합니다\.$/),
      fieldErrors: undefined,
    });
  });

  it('실행 중 VM 삭제는 409 INVALID_VM_STATE (보드 계약 — 정지된 VM만 삭제)', async () => {
    await login();
    const id = '22222222-2222-4222-8222-222222222222';
    await expect(request(`/api/v1/vms/${id}`, { method: 'DELETE' })).rejects.toMatchObject({
      status: 409,
      code: 'INVALID_VM_STATE',
      message: '실행 중인 VM은 정지한 뒤 삭제할 수 있습니다.',
    });
    await expect(request<Vm>(`/api/v1/vms/${id}`)).resolves.toMatchObject({ vm_id: id });
  });

  it('정지 뒤 삭제하면 상세는 VM_NOT_FOUND', async () => {
    await login();
    const id = '22222222-2222-4222-8222-222222222222';
    await request(`/api/v1/vms/${id}/stop`, { method: 'POST' });
    await request(`/api/v1/vms/${id}`, { method: 'DELETE' });
    await expect(request(`/api/v1/vms/${id}`)).rejects.toMatchObject({
      status: 404,
      code: 'VM_NOT_FOUND',
      message: '존재하지 않는 VM입니다.',
    });
  });

  it('VM CPU 메트릭은 할당 vCPU 기준 필드명을 사용한다', async () => {
    await login();
    const metrics = await request<MetricHistoryResponse>(
      `/api/v1/vms/${VM_IDS.interaction}/metrics?from=${encodeURIComponent(new Date(0).toISOString())}`,
    );
    expect(metrics.items.at(-1)?.values).toContainEqual(
      expect.objectContaining({ name: VM_CPU_PERCENT_METRIC, unit: '%' }),
    );
    expect(
      metrics.items.flatMap((sample) => sample.values).some((value) => value.name === 'cpu_percent'),
    ).toBe(false);
  });

  it('vCPU 핀닝을 적용한 뒤 재조회 값이 일치한다', async () => {
    await login();
    const id = VM_IDS.interaction;
    const before = await request<VmResources>(`/api/v1/vms/${id}/resources`);
    expect(before.vcpu_pinning).toEqual([{ vcpu: 0, pcpus: [0, 3] }]);

    await Promise.all(
      Object.values(VM_IDS).map((vmId) => request(`/api/v1/vms/${vmId}/stop`, { method: 'POST' })),
    );

    await request<VmResources>(`/api/v1/vms/${id}/resources/vcpu-pinning`, {
      method: 'PUT',
      body: { mappings: [{ vcpu: 0, pcpus: [3] }] },
      headers: { 'Idempotency-Key': 'pinning-test-1' },
    });

    const after = await request<VmResources>(`/api/v1/vms/${id}/resources`);
    expect(after.vcpu_pinning).toEqual([{ vcpu: 0, pcpus: [3] }]);
  });

  it('vCPU에 pCPU를 하나도 선택하지 않으면 변경을 거절한다', async () => {
    await login();
    await expect(
      request(`/api/v1/vms/${VM_IDS.interaction}/resources/vcpu-pinning`, {
        method: 'PUT',
        body: { mappings: [{ vcpu: 0, pcpus: [] }] },
      }),
    ).rejects.toMatchObject({ status: 400, code: 'INVALID_ARGUMENT' });
  });

  it('정지된 Linux VM의 vCPU와 메모리를 변경한다', async () => {
    await login();
    const id = VM_IDS.interaction;
    await expect(
      request(`/api/v1/vms/${id}/resources`, {
        method: 'PUT',
        body: { vcpus: 2, memory_mib: 768 },
      }),
    ).rejects.toMatchObject({ status: 409, code: 'INVALID_VM_STATE' });

    await request(`/api/v1/vms/${id}/stop`, { method: 'POST' });
    const result = await request<{
      after: { vcpus: number; memory_mib: number };
      applies_on_next_start: boolean;
    }>(`/api/v1/vms/${id}/resources`, {
      method: 'PUT',
      body: { vcpus: 2, memory_mib: 768 },
    });

    const resources = await request<VmResources>(`/api/v1/vms/${id}/resources`);
    const vm = await request<Vm>(`/api/v1/vms/${id}`);
    expect(result).toMatchObject({
      after: { vcpus: 2, memory_mib: 768 },
      applies_on_next_start: true,
    });
    expect(resources).toMatchObject({ vcpus: 2, memory_mib: 768 });
    expect(vm).toMatchObject({ vcpus: 2, memory_mib: 768 });
  });

  it('Zephyr 고정 프로필은 vCPU와 메모리 변경을 거절한다', async () => {
    await login();
    await request(`/api/v1/vms/${VM_IDS.autonomous}/stop`, { method: 'POST' });
    await expect(
      request(`/api/v1/vms/${VM_IDS.autonomous}/resources/memory`, {
        method: 'PUT',
        body: { memory_mib: 64 },
      }),
    ).rejects.toMatchObject({ status: 400, code: 'RESOURCE_UNSUPPORTED' });
  });

  it('Linux Agent 종료는 접수하고 Zephyr 부하 API는 거절한다', async () => {
    await login();
    await request(`/api/v1/vms/${VM_IDS.interaction}/start`, { method: 'POST' });
    const exit = await request<AgentExitCommand>(`/api/v1/vms/${VM_IDS.interaction}/faults/agent-exit`, {
      method: 'POST',
    });
    expect(exit).toMatchObject({ vm_id: VM_IDS.interaction });
    expect(exit.command_id).toEqual(expect.any(String));

    await expect(
      request<StressCommand>(`/api/v1/vms/${VM_IDS.autonomous}/stress/cpu`, {
        method: 'POST',
        body: { workers: 1, duration_s: 30 },
      }),
    ).rejects.toMatchObject({ status: 400, code: 'UNSUPPORTED_GUEST_KIND' });
  });

  it('부하 명령 상태는 백엔드 형식: 성공, 끝나기 전 두 번째 부하는 BUSY 거절, CPU 4워커는 실패, 없는 명령은 404', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      await login();
      await request(`/api/v1/vms/${VM_IDS.interaction}/start`, { method: 'POST' });
      const stress = (body: object, kind = 'cpu') =>
        request<StressCommand>(`/api/v1/vms/${VM_IDS.interaction}/stress/${kind}`, { method: 'POST', body });
      const status = (id: string) =>
        request<CommandStatusResponse>(`/api/v1/vms/${VM_IDS.interaction}/commands/${id}`);

      const first = await stress({ workers: 1, duration_s: 30 });
      const busy = await stress({ memory_mib: 128, duration_s: 30 }, 'memory');
      expect(await status(busy.command_id)).toMatchObject({
        status: 'REJECTED',
        error: { code: 'BUSY' },
        command_id: busy.command_id,
        vm_id: VM_IDS.interaction,
      });
      const running = await status(first.command_id);
      expect(running).toMatchObject({ status: 'ACCEPTED', error: null, finished_at: null });
      expect(running).not.toHaveProperty('message');
      expect(running).not.toHaveProperty('updated_at');

      vi.setSystemTime(Date.now() + 3000);
      expect(await status(first.command_id)).toMatchObject({ status: 'SUCCEEDED', error: null });
      const failing = await stress({ workers: 4, duration_s: 30 });
      vi.setSystemTime(Date.now() + 3000);
      expect(await status(failing.command_id)).toMatchObject({
        status: 'FAILED',
        error: { code: 'PROCESS_EXITED' },
      });

      await expect(status('00000000-0000-4000-8000-000000000000')).rejects.toMatchObject({
        status: 404,
        code: 'COMMAND_NOT_FOUND',
      });
    } finally {
      vi.useRealTimers();
    }
  });
});
