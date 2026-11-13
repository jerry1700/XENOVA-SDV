import { describe, expect, it } from 'vitest';
import { OBSTACLE_SEEDS } from './course';
import {
  batteryOf,
  batteryPercent,
  courseOf,
  driveAppStatus,
  driverMode,
  gaugeMaxOf,
  MAX_STEER_RAD,
  maxSpeedOf,
  RATED_RUNTIME_S,
  remainingRuntimeS,
  steeringOf,
  tripOf,
  vmLineStatus,
} from './driveExtras';
import { presentationFixture } from './fixture';
import type { Presentation } from './model';

const fresh = { status: 'fresh', age_ms: 50 } as const;

function withExtras(extra: Record<string, unknown>): Presentation {
  const p = presentationFixture();
  Object.assign(p.state, extra);
  return p;
}

describe('운전자 화면 배터리 추정 (3S, 셀당 4.2 V 완충 / 3.5 V 하한)', () => {
  it('팩 전압을 선형으로 정수 %로 바꾸고 0~100 으로 자른다', () => {
    expect(batteryPercent(12.6)).toBe(100);
    expect(batteryPercent(10.5)).toBe(0);
    expect(batteryPercent(11.55)).toBe(50);
    expect(batteryPercent(12.45)).toBe(93);
    expect(batteryPercent(11.1)).toBe(29);
    expect(batteryPercent(13.2)).toBe(100);
    expect(batteryPercent(9.6)).toBe(0);
  });

  it('신선한 vehicle.status 의 batt_v 만 쓴다', () => {
    const vehicle = (batt_v: unknown, source: unknown = fresh) =>
      withExtras({ vehicle: { v_mps: 0, steer_rad: 0, batt_v, flags: null, source } });
    expect(batteryOf(vehicle(12.18))).toBe(80);
    expect(batteryOf(vehicle(null))).toBeNull();
    expect(batteryOf(vehicle(12.18, { status: 'stale', age_ms: 900 }))).toBeNull();
    expect(batteryOf(presentationFixture())).toBeNull();
    expect(batteryOf(null)).toBeNull();
  });
});

describe('운전자 화면 주행 모드 문구', () => {
  it('모드별 문구·색, 준비 중, 미확인', () => {
    expect(driverMode({ mode: 'AUTO', preparing: false })).toEqual({ label: '자율 주행', tone: 'auto' });
    expect(driverMode({ mode: 'MANUAL', preparing: null })).toEqual({ label: '수동 주행', tone: 'manual' });
    expect(driverMode({ mode: 'AUTO', preparing: true })).toEqual({
      label: '자율 주행 준비 중',
      tone: 'warning',
    });
    expect(driverMode({ mode: 'MANUAL', preparing: true }).label).toBe('수동 주행 준비 중');
    expect(driverMode({ mode: 'AUTO_READY', preparing: false })).toEqual({
      label: '자율 재개 대기',
      tone: 'warning',
    });
    expect(driverMode({ mode: 'SAFE_STOP', preparing: false })).toEqual({
      label: '안전 정지',
      tone: 'danger',
    });
    expect(driverMode({ mode: 'unknown', preparing: false })).toEqual({ label: '미확인', tone: 'muted' });
  });
});

describe('조향 표시 (최대각 0.55 rad, + = 왼쪽)', () => {
  it('정수 도·좌우·최대각 대비 비율', () => {
    expect(MAX_STEER_RAD).toBe(0.55);
    expect(steeringOf(0.21)).toEqual({ deg: 12, side: '좌', ratio: 0.21 / 0.55 });
    expect(steeringOf(-0.21)).toEqual({ deg: 12, side: '우', ratio: -0.21 / 0.55 });
    expect(steeringOf(0.004)).toMatchObject({ deg: 0, side: null });
    expect(steeringOf(1)!.ratio).toBe(1);
    expect(steeringOf(-1)!.ratio).toBe(-1);
    expect(steeringOf(null)).toBeNull();
    expect(steeringOf(Number.NaN)).toBeNull();
  });
});

describe('계기판 눈금', () => {
  it('최대 속도 × 2, 모르면 1.0 m/s', () => {
    expect(gaugeMaxOf(0.5)).toBe(1);
    expect(gaugeMaxOf(0.8)).toBe(1.6);
    expect(gaugeMaxOf(null)).toBe(1);
    expect(gaugeMaxOf(0)).toBe(1);
  });
});

describe('driveExtras 제안 필드', () => {
  it('최대 속도·트립·코스 위치를 읽고, 신선하지 않으면 버린다', () => {
    const p = withExtras({
      drive_limits: { profile: 'lattice-lab-1', max_v_mps: 0.5 },
      trip: { distance_m: 3.25, driving_s: 75, source: fresh },
      course: {
        track_id: 'jetson-demo',
        pose: { x: 2.81, y: 0.7, yaw: Math.PI },
        obstacles: OBSTACLE_SEEDS,
        plan: [[2.81, 0.7]],
        source: fresh,
      },
    });
    expect(maxSpeedOf(p)).toBe(0.5);
    expect(tripOf(p)).toEqual({ distance_m: 3.25, driving_s: 75 });
    expect(courseOf(p)).toMatchObject({ trackId: 'jetson-demo', pose: { x: 2.81, y: 0.7, yaw: Math.PI } });
    expect(courseOf(p)!.obstacles.map((o) => o.id)).toEqual(['A', 'B']);
    (p.state as unknown as { course: { source: unknown } }).course.source = { status: 'stale', age_ms: 900 };
    expect(courseOf(p)).toBeNull();
  });

  it('예상 주행 가능 시간(정차 화면용)은 완충 기준 시간에서 주행 시간을 빼서 추정한다', () => {
    expect(remainingRuntimeS(0)).toBe(RATED_RUNTIME_S);
    expect(remainingRuntimeS(600)).toBe(RATED_RUNTIME_S - 600);
    expect(remainingRuntimeS(RATED_RUNTIME_S + 10)).toBe(0);
  });
});

describe('VM 한 줄 상태 — Xen 신선도', () => {
  it('Xen 수신이 끊기면 마지막 xen_state(crashed)를 그대로 쓰지 않는다', () => {
    const p = presentationFixture();
    const vm = { ...p.state.vms[0]!, xen_state: 'crashed', sources: { ...p.state.vms[0]!.sources } };
    expect(driveAppStatus(null, vm, true).label).toBe('VM 장애');
    vm.sources.xen = { status: 'stale', age_ms: 9000 };
    expect(driveAppStatus(null, vm, true)).toEqual({ label: 'VM 상태 미확인', tone: 'muted' });
  });

  it('paused·stopping 은 꺼짐이 아니라 그 상태로 보인다', () => {
    const p = presentationFixture();
    const vm = p.state.vms[0]!;
    expect(driveAppStatus(null, { ...vm, xen_state: 'paused' }, true).label).toBe('일시 정지');
    expect(driveAppStatus(null, { ...vm, xen_state: 'stopping' }, true).label).toBe('종료 중');
    expect(driveAppStatus(null, { ...vm, xen_state: 'stopped' }, true).label).toBe('VM 꺼짐');
  });

  it('인포테인먼트는 running·신선할 때만 켜짐으로 본다', () => {
    const p = presentationFixture();
    const vm = p.state.vms[2]!;
    expect(vmLineStatus(p, vm, true)).toEqual({ label: '인포테인먼트', tone: 'ok' });
    expect(vmLineStatus(p, vm, false).label).toBe('연결 미확인');
    expect(vmLineStatus(p, { ...vm, xen_state: 'stopped' }, true).label).toBe('VM 꺼짐');
    expect(
      vmLineStatus(p, { ...vm, sources: { ...vm.sources, xen: { status: 'stale', age_ms: 9000 } } }, true)
        .label,
    ).toBe('VM 상태 미확인');
  });
});
