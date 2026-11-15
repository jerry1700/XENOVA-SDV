/** TanStack Query 훅. 경로·응답은 VM 관리 API 계약 §5, 인증은 노션 API 명세 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
  useMutationState,
} from '@tanstack/react-query';
import { useState } from 'react';
import { ApiError, request, requestWithStatus } from './client';
import type {
  CreateVmRequest,
  AutoInputUnavailableCommand,
  AutoInputUnavailableRequest,
  AgentExitCommand,
  AppExitCommand,
  AppExitRequest,
  AutoDelayCommand,
  AutoDelayRequest,
  CommandStatusResponse,
  DomainKillResponse,
  Display,
  DisplayListResponse,
  DisplayModeName,
  DisplayModeStatus,
  EventListResponse,
  MetricHistoryResponse,
  PhoneLinkStatus,
  PhonePairingCode,
  PerceptionFaultOperation,
  ProcessesResponse,
  RouteRequest,
  StressCommand,
  StressRequest,
  TelemetryHeartbeatResponse,
  Template,
  ThreadsResponse,
  UpdateVcpuPinningRequest,
  UpdateVmResourcesRequest,
  Vm,
  VmCrashCommand,
  VmListResponse,
  VmResourceUpdateResult,
  VmSynchronizationResult,
  VmResources,
} from './types';
import { clearVmList, loadVmList, saveVmList } from './vmListCache';

export const keys = {
  vms: ['vms'] as const,
  vm: (id: string) => ['vms', id] as const,
  vmResources: (id: string) => ['vms', id, 'resources'] as const,
  templates: ['templates'] as const,
  events: (query: string) => ['events', query] as const,
  metrics: (id: string, minutes = 5) => ['vms', id, 'metrics', minutes] as const,
  processes: (id: string) => ['vms', id, 'processes'] as const,
  threads: (id: string) => ['vms', id, 'threads'] as const,
  telemetryHeartbeat: (id: string) => ['vms', id, 'telemetry-heartbeat'] as const,
  command: (id: string, commandId: string) => ['vms', id, 'commands', commandId] as const,
  displays: ['displays'] as const,
  phoneLink: ['display', 'phone-link'] as const,
  displayMode: ['display', 'mode'] as const,
  perceptionFault: (id: string) => ['drive', 'faults', 'perception-block', id] as const,
};

/**
 * 실시간 화면은 모두 이 주기 이하로 다시 읽는다. 에이전트 metrics·heartbeat 송신 주기(2초)와 같아
 * 새 값이 들어오면 늦어도 한 주기 안에 화면에 보인다.
 */
export const LIVE_POLL_MS = 2000;

/** 마지막 조회 결과가 실패인가. 성공 시각보다 실패 시각이 늦으면 실패 중이다 */
export function lastFetchFailed(state: { errorUpdatedAt: number; dataUpdatedAt: number }): boolean {
  return state.errorUpdatedAt > state.dataUpdatedAt;
}

/**
 * 마지막 조회가 실패했으면 이전 값을 지우고, 다시 성공할 때까지 실패 상태를 그대로 유지한다.
 * 보드가 꺼져도 직전 목록("실행 중")이 정상처럼 남지 않게 한다.
 * TanStack Query 는 값이 없는 조회를 다시 읽기 시작할 때마다 status 를 pending, error 를 null 로 되돌려
 * 오류 표시가 주기마다 사라졌다 나타나므로, 실패 여부는 성공·실패 시각으로 판정하고 마지막 오류를 기억해 둔다.
 * 한 번 실패는 재시도(main.tsx retry)가 먼저 흡수하므로 일시적인 끊김에는 값이 사라지지 않는다.
 */
function useWithoutStaleData<TData, TError>(
  result: UseQueryResult<TData, TError>,
  keepLastData = false,
): UseQueryResult<TData, TError> {
  // 렌더 중 이전 값 갱신 (React 문서 "Storing information from previous renders")
  const [lastError, setLastError] = useState<TError | null>(null);
  if (result.error && result.error !== lastError) setLastError(result.error);
  if (!lastFetchFailed(result)) return result;
  return {
    ...result,
    data: keepLastData ? result.data : undefined,
    error: result.error ?? lastError,
    status: 'error',
    isError: true,
    isPending: false,
    isLoading: false,
    isLoadingError: false,
    isSuccess: false,
  } as UseQueryResult<TData, TError>;
}

/** 없는 VM(404). 다시 읽어도 404 라 그 VM 의 조회는 주기 재조회를 멈춘다 (S15P31E110-469) */
export function vmGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** 404 를 받기 전까지만 ms 주기로 다시 읽는다 */
const pollUntilGone =
  (ms: number) =>
  (query: { state: { error: Error | null } }): number | false =>
    vmGone(query.state.error) ? false : ms;

/**
 * pollMs: VM Control 은 1초 (관측 시각이 초 단위로 바뀌게), 나머지는 LIVE_POLL_MS.
 * keepLastData: 실패해도 마지막 목록을 남긴다 — 화면이 직접 "조회 불가" 로 표시할 때만 (대시보드 Overview 카드)
 */
export function useVms(pollMs = LIVE_POLL_MS, { keepLastData = false }: { keepLastData?: boolean } = {}) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.vms,
      queryFn: async ({ signal }) => {
        try {
          const list = await request<VmListResponse>('/api/v1/vms', { signal });
          saveVmList(list);
          return list;
        } catch (error) {
          // 새로고침해도 실패 전 목록을 다시 그리지 않는다
          clearVmList();
          throw error;
        }
      },
      refetchInterval: pollMs,
      // 새로고침 직후에는 이 탭이 마지막으로 받은 목록을 먼저 보여 주고 곧바로 다시 읽는다 (S15P31E110-345)
      initialData: () => loadVmList()?.data,
      initialDataUpdatedAt: () => loadVmList()?.savedAt,
    }),
    keepLastData,
  );
}

export function useVm(vmId: string) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.vm(vmId),
      queryFn: ({ signal }) => request<Vm>(`/api/v1/vms/${encodeURIComponent(vmId)}`, { signal }),
      refetchInterval: pollUntilGone(LIVE_POLL_MS),
    }),
  );
}

export function useVmResources(vmId: string) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.vmResources(vmId),
      queryFn: ({ signal }) =>
        request<VmResources>(`/api/v1/vms/${encodeURIComponent(vmId)}/resources`, { signal }),
      enabled: Boolean(vmId),
      refetchInterval: pollUntilGone(LIVE_POLL_MS),
    }),
  );
}

export function useUpdateVcpuPinning(vmId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: UpdateVcpuPinningRequest) =>
      request<VmResources>(`/api/v1/vms/${encodeURIComponent(vmId)}/resources/vcpu-pinning`, {
        method: 'PUT',
        body,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.vmResources(vmId) }),
  });
}

/** 자원 적용 mutation 키 — 같은 탭의 다른 화면이 "이 VM 자원 적용 중"을 알 수 있게 (S15P31E110-473) */
const RESOURCE_UPDATE_KEY = 'vm-resources-update';

export function useUpdateVmResources(vmId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [RESOURCE_UPDATE_KEY, vmId],
    mutationFn: (body: UpdateVmResourcesRequest) =>
      request<VmResourceUpdateResult>(`/api/v1/vms/${encodeURIComponent(vmId)}/resources`, {
        method: 'PUT',
        body,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.vm(vmId) });
      void client.invalidateQueries({ queryKey: keys.vms });
    },
  });
}

/** 이 탭에서 자원 적용(PUT /resources)이 진행 중인 VM id 들. 그 VM 의 시작·정지를 잠근다 */
export function useVmIdsUpdatingResources(): readonly string[] {
  return useMutationState({
    filters: { mutationKey: [RESOURCE_UPDATE_KEY], status: 'pending' },
    select: (mutation) => String(mutation.options.mutationKey?.[1] ?? ''),
  });
}

export function useStartStress(vmId: string) {
  return useMutation({
    mutationFn: ({ kind, body }: StressRequest) =>
      request<StressCommand>(`/api/v1/vms/${encodeURIComponent(vmId)}/stress/${kind}`, {
        method: 'POST',
        body,
      }),
  });
}

export function useStartAutoDelay(vmId: string) {
  return useMutation({
    mutationFn: (body: AutoDelayRequest) =>
      request<AutoDelayCommand>(`/api/v1/vms/${encodeURIComponent(vmId)}/faults/auto-delay`, {
        method: 'POST',
        body,
      }),
  });
}

/** 현재 AUTO 제어권을 가진 VM을 백엔드가 판정해 연산 지연을 적용한다. */
export function useStartActiveDriveAutoDelay() {
  return useMutation({
    mutationFn: (body: AutoDelayRequest) =>
      request<AutoDelayCommand>('/api/v1/drive/faults/auto-delay', {
        method: 'POST',
        body,
      }),
  });
}

export function useStartAutoInputUnavailable(vmId: string) {
  return useMutation({
    mutationFn: (body: AutoInputUnavailableRequest) =>
      request<AutoInputUnavailableCommand>(
        `/api/v1/vms/${encodeURIComponent(vmId)}/faults/auto-input-unavailable`,
        { method: 'POST', body },
      ),
  });
}

export function useStartAgentExit(vmId: string) {
  return useMutation({
    mutationFn: () =>
      request<AgentExitCommand>(`/api/v1/vms/${encodeURIComponent(vmId)}/faults/agent-exit`, {
        method: 'POST',
      }),
  });
}

export function useStartVmCrash(vmId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<VmCrashCommand>(`/api/v1/vms/${encodeURIComponent(vmId)}/faults/vm-crash`, {
        method: 'POST',
      }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}

/** 시연 1 전용: 서버가 최신 AUTO 제어 상태에서 crash 대상 VM을 결정한다. */
export function useStartActiveVmCrash() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<VmCrashCommand>('/api/v1/drive/faults/vm-crash', {
        method: 'POST',
      }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}

export function useStartAppExit(vmId: string) {
  return useMutation({
    mutationFn: (body: AppExitRequest) =>
      request<AppExitCommand>(`/api/v1/vms/${encodeURIComponent(vmId)}/faults/app-exit`, {
        method: 'POST',
        body,
      }),
  });
}

/** 최신 AUTO 제어 주체는 백엔드가 결정한다. 프론트가 본 오래된 active_vm_id를 보내지 않는다. */
export function useStartActiveAppExit() {
  return useMutation({
    mutationFn: () =>
      request<AppExitCommand>('/api/v1/drive/faults/app-exit', {
        method: 'POST',
        body: { app_id: 'autonomous' },
      }),
  });
}

export function useStartDomainKill(vmId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<DomainKillResponse>(`/api/v1/vms/${encodeURIComponent(vmId)}/faults/domain-kill`, {
        method: 'POST',
      }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}

export function useStartPerceptionBlock() {
  return useMutation({
    mutationFn: (durationS: number) =>
      request<PerceptionFaultOperation>('/api/v1/drive/faults/perception-block', {
        method: 'POST',
        body: { duration_s: durationS },
      }),
  });
}

export function usePerceptionFaultOperation(operationId: string | null) {
  return useQuery({
    queryKey: keys.perceptionFault(operationId ?? ''),
    queryFn: ({ signal }) =>
      request<PerceptionFaultOperation>(
        `/api/v1/drive/faults/perception-block/${encodeURIComponent(operationId!)}`,
        { signal },
      ),
    enabled: Boolean(operationId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'ENDED' || status === 'REJECTED' || status === 'UNKNOWN' || status === 'FAILED'
        ? false
        : 500;
    },
  });
}

/** 명령이 없다는 응답(404 COMMAND_NOT_FOUND): 다시 물어도 같으므로 재시도·반복 조회를 멈춘다 */
const commandGone = (error: unknown) => error instanceof ApiError && error.status === 404;

export function useCommandStatus(vmId: string, commandId: string | null, acceptedIsTerminal = false) {
  return useQuery({
    queryKey: keys.command(vmId, commandId ?? ''),
    queryFn: ({ signal }) =>
      request<CommandStatusResponse>(
        `/api/v1/vms/${encodeURIComponent(vmId)}/commands/${encodeURIComponent(commandId!)}`,
        { signal },
      ),
    enabled: Boolean(vmId && commandId),
    retry: (count, error) => !commandGone(error) && count < 1,
    refetchInterval: (query) => {
      if (commandGone(query.state.error)) return false;
      const status = query.state.data?.status;
      return status === 'SUCCEEDED' ||
        status === 'FAILED' ||
        status === 'REJECTED' ||
        (acceptedIsTerminal && status === 'ACCEPTED')
        ? false
        : 1000;
    },
  });
}

export function useTemplates() {
  return useQuery({
    queryKey: keys.templates,
    queryFn: ({ signal }) => request<Template[]>('/api/v1/templates', { signal }),
    staleTime: Infinity, // 설정 파일 고정 데이터 (계약 §2.3)
  });
}

/** 이벤트 목록. 실시간 추가는 WS /ws/v1/events 대신 짧은 주기 재조회로 한다 (초안) */
export function useEvents(
  params: { vm_id?: string; type?: string; severity?: string; limit?: number } = {},
  { enabled = true }: { enabled?: boolean } = {},
) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v != null && v !== '') as [string, string][],
  ).toString();
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.events(query),
      queryFn: ({ signal }) =>
        request<EventListResponse>(`/api/v1/events${query ? `?${query}` : ''}`, { signal }),
      enabled,
      refetchInterval: LIVE_POLL_MS,
    }),
  );
}

/** 최근 n 분 이력. keepLastData는 조회 오류와 원 표본 만료를 직접 표시하는 관측 화면에서만 쓴다. */
export function useMetrics(
  vmId: string,
  enabled: boolean,
  minutes = 5,
  pollMs = LIVE_POLL_MS,
  { keepLastData = false }: { keepLastData?: boolean } = {},
) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.metrics(vmId, minutes),
      queryFn: ({ signal }) => {
        const from = new Date(Date.now() - minutes * 60_000).toISOString();
        return request<MetricHistoryResponse>(
          `/api/v1/vms/${encodeURIComponent(vmId)}/metrics?from=${encodeURIComponent(from)}`,
          { signal },
        );
      },
      enabled,
      refetchInterval: pollUntilGone(pollMs),
    }),
    keepLastData,
  );
}

export function useProcesses(vmId: string, enabled: boolean) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.processes(vmId),
      queryFn: ({ signal }) =>
        request<ProcessesResponse>(`/api/v1/vms/${encodeURIComponent(vmId)}/processes`, { signal }),
      enabled,
      refetchInterval: pollUntilGone(LIVE_POLL_MS),
    }),
  );
}

export function useThreads(vmId: string) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.threads(vmId),
      queryFn: ({ signal }) =>
        request<ThreadsResponse>(`/api/v1/vms/${encodeURIComponent(vmId)}/threads`, { signal }),
      enabled: Boolean(vmId),
      refetchInterval: pollUntilGone(LIVE_POLL_MS),
    }),
  );
}

export function useTelemetryHeartbeat(vmId: string) {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.telemetryHeartbeat(vmId),
      queryFn: ({ signal }) =>
        request<TelemetryHeartbeatResponse>(`/api/v1/vms/${encodeURIComponent(vmId)}/telemetry/heartbeat`, {
          signal,
        }),
      enabled: Boolean(vmId),
      refetchInterval: pollUntilGone(LIVE_POLL_MS),
      retry: false,
    }),
  );
}

/**
 * 화면 배치 (REST-23). 모니터가 바꾼 결과(SWITCHING → ACTIVE)를 빨리 보이게 짧게 다시 읽는다.
 * 실시간 WS /ws/v1/displays 는 모니터 브라우저용이라 콘솔은 조회로 충분하다.
 */
export function useDisplays() {
  return useWithoutStaleData(
    useQuery({
      queryKey: keys.displays,
      queryFn: ({ signal }) => request<DisplayListResponse>('/api/v1/displays', { signal }),
      refetchInterval: 1000,
    }),
  );
}

/** 화면 배치 바꾸기 (REST-24). 202 — 모니터가 route.applied 로 답해야 ACTIVE. 409 면 다른 사람이 먼저 바꿈 */
export function useRouteDisplay() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ displayId, body }: { displayId: string; body: RouteRequest }) =>
      request<Display>(`/api/v1/displays/${encodeURIComponent(displayId)}/route`, { method: 'PUT', body }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.displays }),
  });
}

/** USB 휴대폰 화면 연결 상태 (REST-38). 케이블·테더링 변화가 보이게 몇 초마다 다시 읽는다 */
export function usePhoneLink() {
  return useQuery({
    queryKey: keys.phoneLink,
    queryFn: ({ signal }) => request<PhoneLinkStatus>('/api/v1/display/phone-link', { signal }),
    refetchInterval: 5000,
    retry: false,
  });
}

/**
 * 새 연결번호 발급 (REST-39). 이미 연결된 휴대폰이 끊기고 앞 번호가 무효가 되므로 재시도하지 않는다.
 * 번호는 호출한 컴포넌트의 상태에만 둔다 (쿼리 캐시에 넣지 않음).
 */
export function useIssuePhonePairingCode() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request<PhonePairingCode>('/api/v1/display/phone-link/pairing-code', { method: 'POST' }),
    retry: false,
    // 상태를 다시 읽는 것은 기다리지 않는다 (보드 왕복 동안 번호 표시가 늦어지지 않게)
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.phoneLink });
    },
  });
}

/** 표시 모드 HDMI / 휴대폰 (REST-40) */
export function useDisplayMode() {
  return useQuery({
    queryKey: keys.displayMode,
    queryFn: ({ signal }) => request<DisplayModeStatus>('/api/v1/display/mode', { signal }),
    refetchInterval: 5000,
    retry: false,
  });
}

/**
 * 표시 모드 전환 (REST-41). 휴대폰 모드는 휴대폰 중계를 켜고 HDMI 를 끈다.
 * 보드 전환이 끝날 때까지(보통 수 초, 최악 4분) 기다리며, 겹친 전환을 막으려 재시도하지 않는다.
 */
export function useSetDisplayMode() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (mode: DisplayModeName) =>
      request<DisplayModeStatus>('/api/v1/display/mode', { method: 'POST', body: { mode } }),
    retry: false,
    onSuccess: (status) => {
      client.setQueryData(keys.displayMode, status);
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.displayMode });
      void client.invalidateQueries({ queryKey: keys.phoneLink });
    },
  });
}

export type VmAction = 'start' | 'stop' | 'delete';

export function useVmAction() {
  const client = useQueryClient();
  return useMutation({
    /** start·stop 은 VM, delete 는 204 (계약 §5) */
    mutationFn: ({ vmId, action }: { vmId: string; action: VmAction }): Promise<Vm | void> =>
      action === 'delete'
        ? request<void>(`/api/v1/vms/${encodeURIComponent(vmId)}`, { method: 'DELETE' })
        : request<Vm>(`/api/v1/vms/${encodeURIComponent(vmId)}/${action}`, { method: 'POST' }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}

/**
 * 보드 등록 목록과 DB VM 테이블을 맞춘다 (보드에만 있는 VM 등록, 보드에서 사라진 VM 은퇴).
 * 서버가 주기적으로 돌리지 않으므로 사용자가 버튼으로 요청한다. 끝나면 VM 목록을 다시 읽는다.
 */
export function useSynchronizeVms() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => request<VmSynchronizationResult>('/api/v1/vms/synchronizations', { method: 'POST' }),
    retry: false,
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}

/** 201 = 새로 만듦. 200 = 같은 이름·같은 사양의 재요청이라 서버가 기존 VM 을 시작해 돌려줌 (backend VmService.create) */
export interface CreateVmResult {
  vm: Vm;
  created: boolean;
}

export function useCreateVm() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: CreateVmRequest): Promise<CreateVmResult> => {
      const { data, status } = await requestWithStatus<Vm>('/api/v1/vms', { method: 'POST', body });
      return { vm: data, created: status !== 200 };
    },
    onSettled: () => client.invalidateQueries({ queryKey: keys.vms }),
  });
}
