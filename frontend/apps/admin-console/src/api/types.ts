/**
 * 관리 콘솔 ↔ Spring 공개 API 타입. 팀이 정한 문서를 그대로 옮긴다 — 바뀌면 문서와 같이 고친다.
 *
 *   VM·템플릿·오류: docs/contracts/vm-api-contract-v1.md §2.2 §2.3 §3.3 §5 §8.2 §9
 *   인증:          노션 API 명세 "로그인"(POST /api/v1/auth/login), "토큰 재발급"(POST /api/v1/auth/refresh)
 */

// ---- 인증 (노션 API 명세) ----------------------------------------------------------

export interface LoginRequest {
  username: string;
  password: string;
}

/** 로그인·재발급 응답. refresh 토큰은 HttpOnly 쿠키(xenova_refresh)라 본문에 없다 */
export interface TokenResponse {
  access_token: string;
  token_type: 'Bearer';
  /** 초 (기본 900) */
  expires_in: number;
  /** ISO-8601 UTC */
  expires_at: string;
  user: { user_id: string; username: string; nickname: string };
}

// ---- VM (VM 관리 API 계약 §2.2, §3.3) ------------------------------------------------

export type GuestKind = 'linux' | 'zephyr';

/** §3.3 Spring 공개 state. "꺼짐"은 stopped (Spring 만 만드는 값) */
export type VmState = 'running' | 'stopping' | 'paused' | 'crashed' | 'stopped';

/** heartbeat 기준. zephyr 는 항상 UNKNOWN */
export type AgentStatus = 'UNKNOWN' | 'ONLINE' | 'STALE' | 'OFFLINE';

/** 백엔드 VmRole (DB vms.role). 배포자가 VM 등록·수정 때 지정하며 이름·app_id 로 추정하지 않는다 */
export type VmRole = 'primary_drive' | 'backup_drive' | 'interaction';

/** §2.2 프론트 공개 VM. 주소 필드는 조회용 VM 응답에 포함된다. */
export interface Vm {
  vm_id: string;
  name: string;
  guest_kind: GuestKind;
  state: VmState;
  agent_status?: AgentStatus;
  ipv4?: string | null;
  assigned_ipv4?: string | null;
  template_id: string;
  template_version: string;
  vcpus: number;
  memory_mib: number;
  /** 역할 미지정이면 null */
  role?: VmRole | null;
}

/** §5 GET /api/v1/vms */
export interface VmListResponse {
  items: Vm[];
  observed_at: string;
}

/**
 * POST /api/v1/vms/synchronizations — 보드 등록 목록을 기준으로 DB VM 테이블을 맞춘 결과.
 * VM 은 vm_id(UUID)로만 짝짓는다. 버튼 한 번에 끝난다(서버 주기 실행 없음).
 */
export interface VmSynchronizationResult {
  /** 보드가 돌려준 VM 수 (형식 오류 포함) */
  discovered: number;
  /** 보드에만 있어 새로 등록 */
  registered: number;
  /** 보드 기준으로 이름·사양·IP 갱신 */
  updated: number;
  unchanged: number;
  /** 정리됐던 vm_id 가 보드에 다시 있어 복구 */
  restored: number;
  /** 보드에 없어 정리(목록에서 빠짐) */
  retired: number;
  /** 보드에 없지만 생성·시작·정지·삭제 진행 중이라 이번엔 건드리지 않음 */
  skipped: number;
  conflicts: number;
  incomplete: number;
  results: VmSynchronizationItem[];
}

export interface VmSynchronizationItem {
  vm_id: string;
  /** 보드 VM 은 보드의 이름, 정리된 VM 은 정리 전 이름. 보드가 이름을 주지 않으면 null */
  name: string | null;
  action:
    'registered' | 'updated' | 'unchanged' | 'restored' | 'retired' | 'skipped' | 'conflict' | 'incomplete';
  /** missing_from_board · in_operation · duplicate_name · duplicate_board_vm_id · invalid_vm_id · registration_metadata_missing */
  reason: string | null;
}

/** §5 POST /api/v1/vms 요청 (§9 입력값 규칙) */
export interface CreateVmRequest {
  /** 1~63자, 앞뒤 공백·제어문자 금지 */
  name: string;
  template_id: string;
  template_version: string;
  /** 1~4 */
  vcpus: number;
  /** 템플릿 min_memory_mib ~ 1024 */
  memory_mib: number;
}

// ---- VM 리소스 제어 (Jira -107 공개 API, FE -113·-114) -------------------------------

/** Xen vCPU 하나가 실행될 수 있는 물리 CPU 집합(hard affinity). */
export interface VcpuPinning {
  vcpu: number;
  pcpus: number[];
}

export interface PcpuStatus {
  pcpu: number;
  /** 최근 호스트 메트릭이 없으면 null. 프론트에서 추정하지 않는다. */
  load_percent: number | null;
}

export interface ResourceLimits {
  vcpus_min: number;
  vcpus_max: number;
  memory_mib_min: number;
  memory_mib_max: number;
  memory_mib_step: number;
}

/** GET /api/v1/vms/{id}/resources */
export interface VmResources {
  vm_id: string;
  vcpus: number;
  memory_mib: number;
  pcpu_count: number;
  vcpu_pinning: VcpuPinning[];
  /** dom0와 공유 VM이 사용할 수 있는 pCPU 집합. 전용 VM은 선택할 수 없다. */
  shared_pcpus: number[];
  /** 서버가 전용으로 검증한 코어만 강조한다. 이름이나 OS로 전용 여부를 추론하지 않는다. */
  exclusive_pcpus: number[];
  pcpus: PcpuStatus[];
  limits: ResourceLimits;
  observed_at: string;
}

/** PUT /api/v1/vms/{id}/resources/vcpu-pinning */
export interface UpdateVcpuPinningRequest {
  mappings: VcpuPinning[];
}

export interface UpdateVmResourcesRequest {
  vcpus: number;
  memory_mib: number;
}

/** Mock 핀닝 계약의 기존 개별 필드 타입. */
export interface UpdateVcpusRequest {
  vcpus: number;
}

export interface UpdateMemoryRequest {
  memory_mib: number;
}

export interface VmResourceUpdateResult {
  vm_id: string;
  operation_id: string | null;
  before: { vcpus: number; memory_mib: number };
  after: { vcpus: number; memory_mib: number };
  applies_on_next_start: boolean;
}

/** POST /api/v1/vms/{id}/stress/cpu — workers 1~4, duration_s 1~120 */
export interface CpuStressRequest {
  workers: number;
  duration_s: number;
}

/** POST /api/v1/vms/{id}/stress/memory — memory_mib 1~512, duration_s 1~120 */
export interface MemoryStressRequest {
  memory_mib: number;
  duration_s: number;
}

export type StressRequest =
  { kind: 'cpu'; body: CpuStressRequest } | { kind: 'memory'; body: MemoryStressRequest };

export type StressType = 'CPU' | 'MEMORY';

/** 202 응답. Agent 에 MQTT agent.command(action=stress)를 보냈다는 뜻이고, 실제 부하는 메트릭으로 확인한다 */
export interface SubmittedCommand {
  command_id: string;
  operation_id: string;
  vm_id: string;
  submitted_at: string;
  expires_at: string;
}

export interface StressCommand extends SubmittedCommand {
  type: StressType;
  /** CPU 는 워커 수, MEMORY 는 MiB */
  amount: number;
  load_percent?: number | null;
  duration_s: number;
}

/** POST /api/v1/vms/{id}/faults/auto-delay — Zephyr 주행 연산에 지연을 주는 데모 명령 */
export interface AutoDelayRequest {
  delay_ms: number;
  duration_s: number;
}

export interface AutoDelayCommand extends SubmittedCommand {
  delay_ms: number;
  duration_s: number;
}

/** 활성 Zephyr VM의 AUTO 계산 입력만 일정 시간 사용할 수 없게 만드는 시연 명령 */
export interface AutoInputUnavailableRequest {
  duration_s: number;
}

export interface AutoInputUnavailableCommand extends SubmittedCommand {
  duration_s: number;
}

/** POST /api/v1/vms/{id}/faults/agent-exit — Linux Agent 종료 데모 명령 */
export type AgentExitCommand = SubmittedCommand;

/** POST /api/v1/vms/{id}/faults/vm-crash — 실행 중인 Zephyr 도메인이 스스로 crash 하게 하는 시연 명령 */
export type VmCrashCommand = SubmittedCommand;

/** POST /api/v1/vms/{id}/faults/app-exit — Zephyr 주행 앱의 한 앱(지금은 자율)만 멈추는 장애 주입. VM 은 그대로 */
export interface AppExitRequest {
  app_id: 'autonomous';
}

export type AppExitCommand = SubmittedCommand;

/** POST /api/v1/vms/{id}/faults/domain-kill — 도메인 즉시 소멸(xl destroy). 의도(running)는 남아 복구 워커가 다시 띄운다 */
export interface DomainKillResponse {
  operation_id: string;
  vm_id: string;
  submitted_at: string;
}

export type CommandState = 'PENDING' | 'ACCEPTED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'REJECTED';

/**
 * Agent 가 보낸 실패·거절 사유 (백엔드 AgentCommandStatusResponse.error, JsonNode 그대로).
 * 형식이 보장되지 않으므로 값은 commandErrorCode 로 꺼낸다
 */
export interface CommandError {
  code?: unknown;
  message?: unknown;
}

/** GET /api/v1/vms/{vmId}/commands/{commandId} — 백엔드 AgentCommandStatusResponse (snake_case) */
export interface CommandStatusResponse {
  vm_id: string;
  command_id: string;
  operation_id?: string | null;
  status: CommandState;
  submitted_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  result?: unknown;
  error?: CommandError | null;
  received_at?: string | null;
  contract_violation?: boolean;
  violation_detail?: string | null;
}

/** §2.3 */
export interface Template {
  template_id: string;
  version: string;
  display_name: string;
  min_memory_mib: number;
  guest_kind: GuestKind;
}

// ---- 오류 (§8.2, 노션 공통 에러) ----------------------------------------------------

export interface ApiErrorBody {
  code: string;
  message: string;
  /** 입력 검증 실패(422)일 때만 */
  errors?: { field: string; reason: string }[];
}

/** 프론트가 분기에 쓰는 코드 (§8.2 + 인증) */
export type KnownErrorCode =
  | 'UNAUTHORIZED'
  | 'INVALID_TOKEN'
  | 'TOKEN_REVOKED'
  | 'INVALID_CREDENTIALS'
  | 'INVALID_REFRESH_TOKEN'
  | 'VALIDATION_ERROR'
  | 'TEMPLATE_NOT_FOUND'
  | 'VM_NOT_FOUND'
  | 'VM_NAME_CONFLICT'
  | 'INVALID_VM_STATE'
  | 'VM_OPERATION_IN_PROGRESS'
  | 'VM_ALREADY_EXISTS'
  | 'INVALID_ARGUMENT'
  | 'INSUFFICIENT_RESOURCES'
  | 'VM_DRIVER_ERROR'
  | 'VM_CONTROL_NOT_SUPPORTED'
  | 'VM_CONTROL_UNAVAILABLE'
  | 'VM_CONTROL_TIMEOUT'
  | 'VM_CONTROL_BAD_RESPONSE'
  | 'RESOURCE_UNSUPPORTED'
  | 'INTERNAL_SERVER_ERROR'
  | 'AGENT_UNAVAILABLE'
  | 'MQTT_UNAVAILABLE'
  | 'COMMAND_NOT_FOUND'
  | 'NOT_FOUND'
  | 'OPERATION_NOT_FOUND'
  | 'RESOURCE_UPDATE_UNSUPPORTED'
  | 'CPU_PLACEMENT_CONFLICT'
  | 'CPU_POLICY_NOT_CONFIGURED'
  | 'EVENT_CURSOR_EXPIRED'
  | 'TIME_RANGE_INVALID'
  | 'SIM_UNAVAILABLE'
  | 'PHONE_RELAY_NOT_LISTENING'
  | 'PHONE_LINK_UNAVAILABLE'
  | 'LOG_SOURCE_UNAVAILABLE'
  | 'PHONE_NOT_CONNECTED'
  | 'DISPLAY_MODE_BUSY'
  | 'DISPLAY_MODE_NOT_APPLIED'
  | 'DISPLAY_SETUP_REQUIRED'
  | 'DISPLAY_MODE_ROLLBACK_INCOMPLETE'
  | 'DISPLAY_MODE_UNAVAILABLE'
  | 'DISPLAY_MODE_TIMEOUT';

/** VM Agent 가 명령을 거절·실패할 때 명령 상태 error.code 로 보내는 코드 (platform vm-agent commands.py) */
export type AgentErrorCode =
  | 'BUSY'
  | 'INVALID_PARAMETERS'
  | 'INVALID_COMMAND'
  | 'AGENT_EXITING'
  | 'RESOURCE_UNAVAILABLE'
  | 'STATE_UNAVAILABLE'
  | 'UNSUPPORTED_ACTION'
  | 'DEMO_DISABLED'
  | 'EXPIRED'
  | 'CLOCK_UNTRUSTED'
  | 'BOOT_MISMATCH'
  | 'IDEMPOTENCY_CONFLICT';

// ---- 이벤트 (노션 API 명세 REST-18 "이벤트 조회", 백엔드 EventResponse) -------------------

export type EventType =
  | 'vm.created'
  | 'vm.deleted'
  | 'vm.state.changed'
  | 'agent.status.changed'
  | 'recovery.started'
  | 'recovery.completed'
  | 'recovery.recovered'
  | 'recovery.cancelled'
  | 'recovery.failed'
  | 'demo.command.sent'
  | 'demo.command.completed'
  | 'demo.command.failed'
  | 'drive.mode.changed'
  | 'drive.fault.detected'
  | 'drive.resume.accepted'
  | 'drive.resume.cancelled'
  // 콘솔이 모니터 화면 배치를 바꿈 (REST-24, backend EventType.DISPLAY_ROUTE_CHANGED)
  | 'display.route.changed';

export type EventSeverity = 'INFO' | 'WARNING' | 'ERROR';

export interface VmEvent {
  event_id: string;
  type: EventType;
  severity: EventSeverity;
  vm_id: string | null;
  operation_id: string | null;
  /** ISO-8601 UTC */
  occurred_at: string;
  recorded_at: string;
  /** drive.mode.changed: {from, to, generation, reason} · drive.fault.detected: {app_id, vm_id, reason} (MQTT 계약 §6) */
  payload: Record<string, unknown>;
}

export interface EventListResponse {
  items: VmEvent[];
}

// ---- 메트릭·프로세스·스레드 (Spring VM 조회 계약) ----

/** VM에 할당된 vCPU 용량을 기준으로 계산한 전체 CPU 사용률. 프로세스 cpu_percent와 구분한다. */
export const VM_CPU_PERCENT_METRIC = 'cpu_percent_allocated';

export interface MetricValue {
  name: string;
  value: number | null;
  unit: string | null;
  labels: Record<string, string> | null;
  unavailable_reason: string | null;
  counter_value?: string | null;
  /** v2 정수의 원문. value 숫자 projection보다 우선한다. */
  raw_value?: string | null;
  status?: string | null;
  source_rc?: number | null;
}

/** 소유 버퍼의 내용·업무 상태. 관측 성공이나 출력 허가와 다른 경계다. */
export interface ManualBufferObservation {
  phase: 'empty' | 'stored' | 'pending' | 'running' | 'retained';
  content_bytes: string;
  sequence: string | null;
  source_uptime_ms: string | null;
  owner_lifetime: string | null;
  context_revision: string;
  connection_revision: string;
  input_status: 'missing' | 'unclassified' | 'accepted' | 'invalid' | 'expired' | 'context_changed';
  source_rc: number;
  performed: boolean | null;
  revoked: boolean;
  last_read_sequence: string | null;
}

export interface TelemetryExecutionContext {
  binding: string;
  host_boot_id: string | null;
  execution_id: string | null;
  receipt_id: string | null;
}

export interface MetricSample {
  sample_id: string;
  vm_id: string;
  boot_id?: string | null;
  source: string;
  observed_at: string;
  received_at?: string;
  window_ms?: number | null;
  network?: {
    publisher_instance_id: string;
    observed_uptime_ms: string | null;
  };
  telemetry?: {
    family: string;
    publisher_instance_id: string;
    message_seq?: string | null;
    source_sample_seq: string | null;
    observed_uptime_ms: string | null;
    sent_uptime_ms: string;
    sent_at: string | null;
    utc_quality: string | null;
    utc_source: string | null;
    uncertainty_ms: number | null;
    collection: {
      sample_id: string;
      start_uptime_ms: string;
      end_uptime_ms: string;
      elapsed_us: string | null;
      status: string;
      source_rc: number | null;
    };
    context: TelemetryExecutionContext;
    buffer?: ManualBufferObservation | null;
  };
  values: MetricValue[];
}

export interface MetricHistoryResponse {
  items: MetricSample[];
}

export interface ProcessInfo {
  pid: number;
  name: string;
  cpu_percent: number | null;
  rss_bytes: number | null;
}

export interface ProcessesResponse {
  vm_id: string;
  observed_at: string | null;
  stale: boolean;
  items: ProcessInfo[];
  process_collection?: {
    status: 'first_sample' | 'ok' | 'partial' | 'read_failed' | 'disabled';
    discovered: number;
    read_ok: number;
    vanished: number;
    failed: number;
    truncated: boolean;
    error: string | null;
  } | null;
}

export interface ThreadsResponse {
  vm_id: string;
  window_ms: number | null;
  received_at: string | null;
  stale: boolean;
  threads_status: string | null;
  parts_received: number;
  part_count: number | null;
  complete: boolean;
  conflicting_pages: boolean;
  stack_snapshot?: {
    snapshot_id: string;
    source_status: string;
    rows_seen: number | null;
    received_at: string;
    expires_at: string;
    items: {
      row_id: number;
      name: string;
      name_truncated: boolean;
      reserved_bytes: number;
      peak_used_bytes: number | null;
      status: string;
    }[];
  } | null;
  items: {
    thread_id: string;
    thread_name: string;
    name_truncated: boolean;
    cpu_percent: number | null;
    unavailable_reason: string | null;
  }[];
}

export interface TelemetryHeartbeatResponse {
  vm_id: string;
  status: 'FRESH' | 'STALE' | 'UNKNOWN';
  publisher_state: 'running' | 'degraded' | 'stopping' | null;
  last_collection_success_uptime_ms?: string | null;
  last_collection_success_age_ms: number | null;
  collection_failures_total: number | null;
  /** MQTT 제출 실패 누계 (백엔드 BigInteger) */
  publish_submit_failures_total?: number | null;
  received_at: string | null;
  age_ms: number | null;
}

export type PerceptionFaultStatus =
  'QUERYING' | 'COMMAND_SENT' | 'APPLIED' | 'ENDED' | 'REJECTED' | 'UNKNOWN' | 'FAILED';

export interface PerceptionFaultOperation {
  operation_id: string;
  command_id: string;
  duration_s: number;
  status: PerceptionFaultStatus;
  reason: string | null;
  active_vm_id: string | null;
  mode_before: string | null;
  submitted_at: string;
  updated_at: string;
}

// ---- 화면 배치 (노션 API 명세 REST-23·REST-24, 백엔드 -129) ----

export type DisplayView = 'split' | 'os-face';
export type DisplayStatus = 'DISCONNECTED' | 'IDLE' | 'SWITCHING' | 'ACTIVE' | 'ERROR';

export interface DisplayRoute {
  /** split 이면 null */
  vm_id: string | null;
  view: DisplayView;
  request_id: string;
  /** observed_route 에만 있다 */
  applied_at: string | null;
  verification_level: string | null;
}

export interface Display {
  display_id: string;
  label: string;
  /** 모니터 브라우저가 WS /ws/v1/displays 로 붙어 있는지 */
  connected: boolean;
  status: DisplayStatus;
  supported_views: DisplayView[];
  revision: number;
  desired_route: DisplayRoute;
  observed_route: DisplayRoute | null;
  last_error: string | null;
}

export interface DisplayListResponse {
  items: Display[];
  observed_at: string;
  stale: boolean;
}

export interface RouteRequest {
  vm_id: string | null;
  view: DisplayView;
  expected_revision: number;
}

// ---- USB 휴대폰 화면 연결 (REST-38·REST-39, docs/contracts/phone-link-v1.md) ----

export type PhoneRelayState = 'listening' | 'waiting_link' | 'starting' | 'stopped' | 'failed' | 'unknown';

export interface PhoneLinkStatus {
  /** 보드에 휴대폰 중계 설정이 있음 */
  configured: boolean;
  relay: PhoneRelayState;
  /** 휴대폰 브라우저에서 열 주소. relay 가 listening 일 때만 */
  url: string | null;
  usb: {
    interface: string;
    present: boolean;
    /** 보드 쪽 주소(주소/접두어) */
    ipv4: string[];
    /** 휴대폰 주소 */
    gateway: string | null;
  };
  configured_phone_ipv4: string | null;
  /** 휴대폰이 테더링 대역을 바꿔 중계가 시작하지 못함. 판단할 수 없으면 null */
  subnet_mismatch: boolean | null;
  /** 번호 값은 없다 */
  pairing: { active: boolean; expires_at: string | null };
  observed_at: string;
}

/** 새 연결번호. 화면 상태에만 두고 저장·로그·URL 에 넣지 않는다 */
export interface PhonePairingCode {
  code: string;
  expires_at: string;
}

// ---- 표시 모드 HDMI / 휴대폰 (REST-40·REST-41, docs/contracts/phone-link-v1.md) ----

/** hdmi: HDMI 모니터에 차량 화면 · mobile: USB 테더링 휴대폰에 차량 화면(휴대폰 중계가 켜짐) */
export type DisplayModeName = 'hdmi' | 'mobile';

export interface DisplayModeStatus {
  mode: DisplayModeName;
  /** 재부팅 뒤 모드. 지금은 항상 hdmi */
  boot_default: DisplayModeName;
  /** 각 표시 서비스의 systemd 상태 (active·inactive·activating·deactivating·failed·unknown) */
  services: { hdmi: string; mobile: string; feed: string };
  /** HDMI 화면이 실제로 그려지고 있음 */
  hdmi_renderer_ready: boolean;
  observed_at: string;
}
