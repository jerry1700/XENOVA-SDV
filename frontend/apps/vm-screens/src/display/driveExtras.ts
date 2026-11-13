import type { Obstacle, P2 } from './course';
import type { DisplayVm, Presentation, Source } from './model';

/**
 * 계기판용 제안 확장 필드 (dom0 표시 수집기는 아직 보내지 않는다 — 노트북 미리보기 가짜 수집기만 보냄).
 *  - state.vehicle: vehicle.status (MQTT 계약 5.4, MQTT-10) — 게이트웨이가 ESP32 실측을 변환
 *  - state.drive_apps[vm_id].autonomous|manual: Zephyr 주행 펌웨어 보고
 *      heartbeat (6.1) · lifecycle (6.3 warm-drive-1) · command (5.4 drive.command) · perception (perception.event)
 * 규격 확정 전이라 parsePresentation 은 이 필드를 검사하지 않는다. 형식이 틀리거나 없으면 null 로 보고
 * 화면은 "미수신" 으로 그린다 (값을 지어내지 않는다).
 */
export interface VehicleStatus {
  v_mps: number | null;
  batt_v: number | null;
  steer_rad: number | null;
  flags: { estop: boolean; watchdog: boolean; obstacle: boolean } | null;
  source: Source;
  /** 숫자로 왔지만 물리 범위(VEHICLE_RANGE)를 벗어나 null 로 둔 값 — 화면은 '범위 밖'으로 보인다 */
  outOfRange: { v_mps: boolean; batt_v: boolean };
}

/**
 * 화면이 믿는 물리 범위 (RC 차 기준). 이 밖의 값은 센서·변환 오류로 보고 표시하지 않는다.
 * 플랫폼 수집기가 범위를 정하면 그 값과 맞춘다.
 *  - 속도 |v_mps| ≤ 10 m/s
 *  - 팩 전압 batt_v 9 ~ 13.5 V (3S LiPo)
 *  - 주행 프로필 최대 속도 0 < max_v_mps ≤ 5 m/s
 * 조향은 null 로 두지 않고 ±MAX_STEER_RAD 로 자른다 (steeringOf).
 */
export const VEHICLE_RANGE = {
  maxAbsSpeedMps: 10,
  minBattV: 9,
  maxBattV: 13.5,
  maxProfileSpeedMps: 5,
} as const;

export interface DriveAppReport {
  heartbeat: { state: 'active' | 'standby' | 'error'; uptime_s: number } | null;
  lifecycle: {
    phase: 'WAITING' | 'ACTIVATING' | 'EXECUTING' | 'STOPPING' | 'RESIDUAL';
    readiness: 'QUALIFIED' | 'READY' | 'NOT_READY';
    reason: string | null;
    applied_generation: number;
  } | null;
  command: { generation: number; v_mps: number; steer_rad: number } | null;
  perception: { kind: string; detail: string | null } | null;
  source: Source;
}

export interface DriveApps {
  autonomous: DriveAppReport | null;
  manual: DriveAppReport | null;
}

const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const within = (v: number, min: number, max: number) => v >= min && v <= max;
const SOURCES = ['fresh', 'stale', 'disconnected', 'unknown'];

function source(v: unknown): Source | null {
  if (!object(v) || !SOURCES.includes(String(v.status))) return null;
  return { status: v.status as Source['status'], age_ms: finite(v.age_ms) ? v.age_ms : null };
}

export function vehicleOf(p: Presentation | null): VehicleStatus | null {
  const v = (p?.state as Record<string, unknown> | undefined)?.vehicle;
  if (!object(v)) return null;
  const src = source(v.source);
  if (!src) return null;
  const f = v.flags;
  const { maxAbsSpeedMps, minBattV, maxBattV } = VEHICLE_RANGE;
  // 숫자가 아니거나 Infinity 면 미수신, 숫자지만 범위 밖이면 범위 밖 (둘 다 값은 null)
  const speedOk = finite(v.v_mps) && within(v.v_mps, -maxAbsSpeedMps, maxAbsSpeedMps);
  const battOk = finite(v.batt_v) && within(v.batt_v, minBattV, maxBattV);
  return {
    v_mps: speedOk ? (v.v_mps as number) : null,
    batt_v: battOk ? (v.batt_v as number) : null,
    steer_rad: finite(v.steer_rad) ? v.steer_rad : null,
    flags:
      object(f) &&
      typeof f.estop === 'boolean' &&
      typeof f.watchdog === 'boolean' &&
      typeof f.obstacle === 'boolean'
        ? { estop: f.estop, watchdog: f.watchdog, obstacle: f.obstacle }
        : null,
    source: src,
    outOfRange: {
      v_mps: !speedOk && typeof v.v_mps === 'number' && !Number.isNaN(v.v_mps),
      batt_v: !battOk && typeof v.batt_v === 'number' && !Number.isNaN(v.batt_v),
    },
  };
}

function report(v: unknown): DriveAppReport | null {
  if (!object(v)) return null;
  const src = source(v.source);
  if (!src) return null;
  const hb = v.heartbeat;
  const lc = v.lifecycle;
  const cmd = v.command;
  const per = v.perception;
  return {
    heartbeat:
      object(hb) && ['active', 'standby', 'error'].includes(String(hb.state)) && finite(hb.uptime_s)
        ? { state: hb.state as 'active' | 'standby' | 'error', uptime_s: hb.uptime_s }
        : null,
    lifecycle:
      object(lc) &&
      ['WAITING', 'ACTIVATING', 'EXECUTING', 'STOPPING', 'RESIDUAL'].includes(String(lc.phase)) &&
      ['QUALIFIED', 'READY', 'NOT_READY'].includes(String(lc.readiness)) &&
      Number.isSafeInteger(lc.applied_generation)
        ? {
            phase: lc.phase as NonNullable<DriveAppReport['lifecycle']>['phase'],
            readiness: lc.readiness as NonNullable<DriveAppReport['lifecycle']>['readiness'],
            reason: typeof lc.reason === 'string' ? lc.reason : null,
            applied_generation: lc.applied_generation as number,
          }
        : null,
    command:
      object(cmd) && Number.isSafeInteger(cmd.generation) && finite(cmd.v_mps) && finite(cmd.steer_rad)
        ? { generation: cmd.generation as number, v_mps: cmd.v_mps, steer_rad: cmd.steer_rad }
        : null,
    perception:
      object(per) && typeof per.kind === 'string'
        ? { kind: per.kind, detail: typeof per.detail === 'string' ? per.detail : null }
        : null,
    source: src,
  };
}

export function driveAppsOf(p: Presentation | null, vmId: string): DriveApps | null {
  const all = (p?.state as Record<string, unknown> | undefined)?.drive_apps;
  if (!object(all) || !object(all[vmId])) return null;
  const apps = all[vmId];
  return { autonomous: report(apps.autonomous), manual: report(apps.manual) };
}

export type AppTone = 'drive' | 'ok' | 'warning' | 'danger' | 'muted';

type XenView = Pick<DisplayVm, 'xen_state' | 'sources'>;

/**
 * Xen 상태로 이미 정해지는 한 줄 (running 이 아니거나 수신이 끊긴 경우). running·신선하면 null.
 * 수집기는 Xen 조회가 실패해도 마지막 xen_state 를 남기고 sources.xen 만 stale 로 바꾸므로
 * 신선도를 먼저 본다 (수집기 policy.decide_vm 과 같은 순서).
 */
function xenStatus(vm: XenView): { label: string; tone: AppTone } | null {
  if (vm.sources.xen.status !== 'fresh' || vm.xen_state === null)
    return { label: 'VM 상태 미확인', tone: 'muted' };
  if (vm.xen_state === 'running') return null;
  if (vm.xen_state === 'crashed') return { label: 'VM 장애', tone: 'danger' };
  if (vm.xen_state === 'paused') return { label: '일시 정지', tone: 'warning' };
  if (vm.xen_state === 'stopping') return { label: '종료 중', tone: 'warning' };
  return { label: 'VM 꺼짐', tone: 'muted' };
}

/** 합친 차량 화면의 VM 한 줄 — 인포테인먼트는 켜짐 여부만, 주행 VM 은 주행 앱 상태 */
export function vmLineStatus(
  p: Presentation | null,
  vm: XenView & Pick<DisplayVm, 'vm_id' | 'role'>,
  connected: boolean,
): { label: string; tone: AppTone } {
  if (vm.role !== 'interaction') return driveAppStatus(driveAppsOf(p, vm.vm_id), vm, connected);
  if (!connected) return { label: '연결 미확인', tone: 'muted' };
  return xenStatus(vm) ?? { label: '인포테인먼트', tone: 'ok' };
}

/** VM 하나의 주행 앱 상태를 한 줄로 (자율·수동 중 지금 쓰는 쪽 우선) */
export function driveAppStatus(
  apps: DriveApps | null,
  vm: XenView,
  connected: boolean,
): { label: string; tone: AppTone } {
  if (!connected) return { label: '표시 연결 미확인', tone: 'muted' };
  const xen = xenStatus(vm);
  if (xen) return xen;
  if (!apps) return { label: '앱 보고 미수신', tone: 'muted' };
  const list = [apps.autonomous, apps.manual].filter((a): a is DriveAppReport => a !== null);
  if (!list.length || list.every((a) => a.source.status !== 'fresh' || !a.heartbeat))
    return { label: '응답 없음', tone: 'danger' };
  const fresh = list.filter((a) => a.source.status === 'fresh');
  if (fresh.some((a) => a.heartbeat?.state === 'error')) return { label: '앱 오류', tone: 'danger' };
  if (fresh.some((a) => a.lifecycle?.phase === 'EXECUTING' && a.heartbeat?.state === 'active'))
    return { label: '주행 계산 중', tone: 'drive' };
  if (fresh.some((a) => a.lifecycle?.phase === 'ACTIVATING')) return { label: '활성화 중', tone: 'warning' };
  if (fresh.some((a) => a.lifecycle?.readiness === 'NOT_READY'))
    return { label: '준비 안 됨', tone: 'warning' };
  if (fresh.some((a) => a.lifecycle?.readiness === 'QUALIFIED'))
    return { label: '대기 · 준비 완료', tone: 'ok' };
  return { label: '대기', tone: 'ok' };
}

/** 지금 주행 중인 앱의 명령·인지 (활성 VM 의 신선한 보고만) */
export function activeCommand(p: Presentation | null, connected: boolean) {
  const vmId = connected ? p?.state.drive.active_vm_id : null;
  if (!vmId || !p) return null;
  const apps = driveAppsOf(p, vmId);
  const app = p.state.drive.mode === 'MANUAL' ? apps?.manual : apps?.autonomous;
  if (!app || app.source.status !== 'fresh') return null;
  return { command: app.command, perception: app.perception };
}

/** 주행 프로필 한도 (state.drive_limits, 제안). 펌웨어 프로필 max_velocity. 0 이하·범위 밖이면 null */
export function maxSpeedOf(p: Presentation | null): number | null {
  const v = (p?.state as Record<string, unknown> | undefined)?.drive_limits;
  return object(v) &&
    finite(v.max_v_mps) &&
    v.max_v_mps > 0 &&
    v.max_v_mps <= VEHICLE_RANGE.maxProfileSpeedMps
    ? v.max_v_mps
    : null;
}

/** 트립 (state.trip, 제안): 거리·주행 시간 */
export function tripOf(p: Presentation | null): { distance_m: number; driving_s: number } | null {
  const v = (p?.state as Record<string, unknown> | undefined)?.trip;
  if (!object(v) || source(v.source)?.status !== 'fresh') return null;
  return finite(v.distance_m) && finite(v.driving_s)
    ? { distance_m: v.distance_m, driving_s: v.driving_s }
    : null;
}

export interface CourseState {
  trackId: string;
  /** 후륜축 위치 (코스 좌표 m, yaw 반시계 rad) — 시뮬레이터 sim.pose 와 같은 기준 */
  pose: { x: number; y: number; yaw: number };
  /** 코스 위 장애물 (sim.world obstacles 와 같은 꼴: 중심 x·y, 폭 w·깊이 h) */
  obstacles: Obstacle[];
  /** 주행 계획 경로 (코스 좌표, 차 앞부터). 수동 운전·정지 중이면 빈 배열 */
  plan: P2[];
}

/**
 * 코스 위 차 위치·장애물·계획 (state.course, 제안). 신선하지 않으면 null — 지도에 차를 그리지 않는다.
 * 실보드 수집기는 아직 보내지 않는다 (sim.pose·sim.world 또는 젯슨 위치 추정이 필요)
 */
export function courseOf(p: Presentation | null): CourseState | null {
  const v = (p?.state as Record<string, unknown> | undefined)?.course;
  if (!object(v) || source(v.source)?.status !== 'fresh') return null;
  const pose = v.pose;
  if (!object(pose) || !finite(pose.x) || !finite(pose.y) || !finite(pose.yaw)) return null;
  const point = (q: unknown): q is P2 => Array.isArray(q) && q.length === 2 && finite(q[0]) && finite(q[1]);
  return {
    trackId: typeof v.track_id === 'string' ? v.track_id : '',
    pose: { x: pose.x, y: pose.y, yaw: pose.yaw },
    obstacles: (Array.isArray(v.obstacles) ? v.obstacles : []).flatMap((o) =>
      object(o) && typeof o.id === 'string' && finite(o.x) && finite(o.y) && finite(o.w) && finite(o.h)
        ? [
            {
              id: o.id,
              kind: typeof o.kind === 'string' ? o.kind : 'unknown',
              x: o.x,
              y: o.y,
              w: o.w,
              h: o.h,
            },
          ]
        : [],
    ),
    plan: Array.isArray(v.plan) ? v.plan.filter(point) : [],
  };
}

/**
 * 배터리 잔량 추정 (운전자 화면). 잔량 센서가 없어 전압에서 선형으로 추정한다.
 * 실차 배터리 3S LiPo: 셀당 완충 4.2 V(팩 12.6 V) = 100%, 하한 3.5 V(팩 10.5 V) = 0%
 */
export const BATTERY_CELLS = 3;
export const CELL_FULL_V = 4.2;
export const CELL_EMPTY_V = 3.5;

/** 팩 전압 → 추정 잔량 % (정수, 0~100) */
export function batteryPercent(packV: number): number {
  const ratio = (packV / BATTERY_CELLS - CELL_EMPTY_V) / (CELL_FULL_V - CELL_EMPTY_V);
  return Math.round(Math.min(1, Math.max(0, ratio)) * 100);
}

/** 신선한 vehicle.status 의 batt_v 로 추정 잔량 %. 없거나 신선하지 않으면 null */
export function batteryOf(p: Presentation | null): number | null {
  const v = vehicleOf(p);
  return v?.source.status === 'fresh' && v.batt_v !== null && v.batt_v > 0 ? batteryPercent(v.batt_v) : null;
}

/** 운전자 화면 주행 모드 한 줄. 색은 tone (auto 청록 · manual 파랑 · warning 주황 · danger 빨강 · muted 회색) */
export type DriverModeTone = 'auto' | 'manual' | 'warning' | 'danger' | 'muted';

/**
 * state.drive.mode 를 운전자에게 보이는 문구로. VM 이름은 넣지 않는다.
 * summary 는 presentation.driveSummary 결과 (신선하지 않거나 연결이 끊기면 mode 'unknown')
 */
export function driverMode(summary: { mode: string; preparing: unknown }): {
  label: string;
  tone: DriverModeTone;
} {
  const { mode } = summary;
  if (summary.preparing && (mode === 'AUTO' || mode === 'MANUAL'))
    return { label: `${mode === 'AUTO' ? '자율' : '수동'} 주행 준비 중`, tone: 'warning' };
  switch (mode) {
    case 'AUTO':
      return { label: '자율 주행', tone: 'auto' };
    case 'MANUAL':
      return { label: '수동 주행', tone: 'manual' };
    case 'AUTO_READY':
      return { label: '자율 재개 대기', tone: 'warning' };
    case 'SAFE_STOP':
      return { label: '안전 정지', tone: 'danger' };
    default:
      return { label: '미확인', tone: 'muted' };
  }
}

/** 계기판 최대 눈금: 최대 속도 × 2, 최대 속도를 모르거나 범위 밖이면 1.0 m/s */
export const DEFAULT_GAUGE_MAX_MPS = 1;
export const gaugeMaxOf = (maxSpeed: number | null) =>
  maxSpeed !== null && maxSpeed > 0 && maxSpeed <= VEHICLE_RANGE.maxProfileSpeedMps
    ? maxSpeed * 2
    : DEFAULT_GAUGE_MAX_MPS;

/** 소수 둘째 자리 표시. 반올림해 0 이 되는 음수는 "-0.00" 대신 "0.00" */
export function fixed2(v: number): string {
  const text = v.toFixed(2);
  return Number(text) === 0 ? (0).toFixed(2) : text;
}

/**
 * 조향 최대각 (rad). 주행 프로필 platform/apps/drive/profiles/lattice-lab-1.json max_steer 0.55 (약 31.5°).
 * steer_rad 는 + 가 왼쪽 (MQTT 계약 수동 입력 변환: left = steer_rad +)
 */
export const MAX_STEER_RAD = 0.55;
/** 조향 최대각 (정수 도, 32°) */
export const MAX_STEER_DEG = Math.round((MAX_STEER_RAD * 180) / Math.PI);
/** 측정 잡음으로 최대각을 살짝 넘는 값은 경고하지 않는다 (약 0.6°) */
const STEER_TOLERANCE_RAD = 0.01;

/**
 * 조향각 표시: 정수 도, 좌/우 (1° 미만은 방향 없음), 최대각 대비 비율 -1(오른쪽 끝) ~ 1(왼쪽 끝).
 * 최대각을 넘는 값은 ±MAX_STEER_DEG 로 자르고 clipped 로 알린다 (바퀴가 뒤집혀 그려지지 않게)
 */
export function steeringOf(
  rad: number | null,
): { deg: number; side: '좌' | '우' | null; ratio: number; clipped: boolean } | null {
  if (rad === null || !Number.isFinite(rad)) return null;
  const deg = Math.min(MAX_STEER_DEG, Math.round((Math.abs(rad) * 180) / Math.PI));
  return {
    deg,
    side: deg < 1 ? null : rad > 0 ? '좌' : '우',
    ratio: Math.max(-1, Math.min(1, rad / MAX_STEER_RAD)),
    clipped: Math.abs(rad) > MAX_STEER_RAD + STEER_TOLERANCE_RAD,
  };
}

/**
 * 완충 기준 연속 주행 가능 시간 (초). 배터리 잔량 센서가 없어 이 값에서 주행한 시간을 빼서 남은 시간을 추정한다.
 * 가정값 — 실제 차의 측정값을 알면 바꾼다
 */
export const RATED_RUNTIME_S = 60 * 60;

/** 예상 주행 가능 시간 (초): 완충 기준 시간 − 이번 주행에서 달린 시간 (트립 주행 시간). 0 아래로 내려가지 않는다 */
export function remainingRuntimeS(drivingS: number): number {
  return Math.max(0, RATED_RUNTIME_S - drivingS);
}
