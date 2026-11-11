import { render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CombinedPage } from './CombinedPage';
import { presentationFixture } from './fixture';
import type { Presentation } from './model';
import { themeAt } from './theme';
import { useDisplay } from './useDisplay';

vi.mock('./useDisplay', () => ({ useDisplay: vi.fn() }));

/** 음원은 jsdom 에서 재생되지 않으므로 이벤트만 흉내 낸다 */
class AudioFake extends EventTarget {
  loop = false;
  preload = '';
  volume = 1;
  paused = true;
  currentTime = 0;
  duration = Number.NaN;
  load = vi.fn();
  play = vi.fn(async () => undefined);
  pause = vi.fn();
}

const fresh = { status: 'fresh', age_ms: 50 } as const;

interface Overrides {
  v_mps?: number;
  steer_rad?: number;
  batt_v?: number | null;
  max?: number | null;
  /** 수집기 생성 시각 (초, 2026-11-11T00:00:00Z 기준) */
  at?: number;
}

function driving(o: Overrides = {}): Presentation {
  const p = presentationFixture();
  const [vm1] = p.state.vms;
  p.state.generated_at = new Date(Date.UTC(2026, 10, 11) + (o.at ?? 0) * 1000).toISOString();
  p.state.drive = {
    active_vm_id: vm1!.vm_id,
    mode: 'AUTO',
    ai_status: 'ok',
    source: fresh,
    authority: {
      selected_vm_id: vm1!.vm_id,
      selected_mode: 'AUTO',
      generation: 4,
      phase: 'APPLIED',
      output_permitted: true,
      storage_uncertain: false,
    },
  };
  const max = o.max === undefined ? 0.5 : o.max;
  Object.assign(p.state, {
    vehicle: {
      v_mps: o.v_mps ?? 0.42,
      steer_rad: o.steer_rad ?? 0.05,
      batt_v: o.batt_v === undefined ? 12.18 : o.batt_v,
      flags: { estop: false, watchdog: false, obstacle: false },
      source: fresh,
    },
    ...(max === null ? {} : { drive_limits: { profile: 'lattice-lab-1', max_v_mps: max } }),
    trip: { distance_m: 3.25, driving_s: 75, source: fresh },
  });
  return p;
}

function show(p: Presentation | null, connected = true) {
  vi.mocked(useDisplay).mockReturnValue({ presentation: p, connected, error: connected ? null : '끊김' });
  return render(<CombinedPage />);
}

function next(p: Presentation, rerender: (ui: React.ReactElement) => void, connected = true) {
  vi.mocked(useDisplay).mockReturnValue({ presentation: p, connected, error: connected ? null : '끊김' });
  rerender(<CombinedPage />);
}

beforeEach(() => {
  window.history.replaceState(null, '', '/display/combined');
  vi.stubGlobal('Audio', AudioFake);
});
afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('속도 패널', () => {
  it('m/s 소수 2자리, 눈금 0 ~ 최대 속도×2, 최대 속도 경계선, 위에 "MAX 0.5 m/s"', () => {
    show(driving());
    expect(screen.getByTestId('speed-value')).toHaveTextContent('0.42');
    expect(screen.getByTestId('speed-max')).toHaveTextContent('MAX 0.5 m/s');
    expect(screen.getByTestId('speed-scale-start')).toHaveTextContent('0');
    expect(screen.getByTestId('speed-scale-end')).toHaveTextContent('1');
    expect(screen.getByTestId('speed-boundary')).toBeInTheDocument();
    expect(screen.getByTestId('speed-fill')).toBeInTheDocument();
    // 채움 색 = 주행 모드 색 (자율)
    expect(screen.getByTestId('speed-gauge')).toHaveAttribute('data-tone', 'auto');
  });

  it('최대 속도를 모르면 눈금 0 ~ 1.0, 경계선 없이 "MAX —"', () => {
    show(driving({ max: null }));
    expect(screen.getByTestId('speed-scale-end')).toHaveTextContent('1');
    expect(screen.queryByTestId('speed-boundary')).toBeNull();
    expect(screen.getByTestId('speed-max')).toHaveTextContent('MAX —');
  });

  it('최대 속도 0.8 이면 눈금 끝 1.6, 최대 속도를 넘으면 경계선 강조 (채움 색은 모드 색 그대로)', () => {
    show(driving({ max: 0.8, v_mps: 0.9 }));
    expect(screen.getByTestId('speed-scale-end')).toHaveTextContent('1.6');
    expect(screen.getByTestId('speed-gauge')).toHaveAttribute('data-over', 'true');
    expect(screen.getByTestId('speed-gauge')).toHaveAttribute('data-tone', 'auto');
  });

  it('게이지 채움 색은 주행 모드를 따라 바뀌고, 미확인이면 중립색', () => {
    const tone = () => screen.getByTestId('speed-gauge').getAttribute('data-tone');
    const manual = driving();
    manual.state.drive.mode = 'MANUAL';
    const { rerender } = show(manual);
    expect(tone()).toBe('manual');
    const ready = driving();
    ready.state.drive.mode = 'AUTO_READY';
    next(ready, rerender);
    expect(tone()).toBe('warning');
    const stop = driving();
    stop.state.drive.mode = 'SAFE_STOP';
    next(stop, rerender);
    expect(tone()).toBe('danger');
    const unknown = driving();
    unknown.state.drive.source = { status: 'stale', age_ms: 4000 };
    next(unknown, rerender);
    expect(tone()).toBe('muted');
  });

  it('속도가 신선하지 않으면 호를 비우고 "—" 와 속도 수신 대기', () => {
    const p = driving();
    (p.state as unknown as { vehicle: { source: unknown } }).vehicle.source = {
      status: 'stale',
      age_ms: 900,
    };
    show(p);
    expect(screen.getByTestId('speed-value')).toHaveTextContent('—');
    expect(screen.getByTestId('speed-gauge')).toHaveTextContent('속도 수신 대기');
    expect(screen.queryByTestId('speed-fill')).toBeNull();
    expect(screen.getByTestId('speed-gauge')).toHaveAttribute('data-empty', 'true');
    expect(screen.queryByTestId('speed-inner')).toBeNull();
  });

  it('안쪽 보조 호는 조향 (가운데 0 기준, 조향이 0이면 그리지 않음)', () => {
    const { rerender } = show(driving({ steer_rad: 0.2 }));
    expect(screen.getByTestId('speed-inner')).toBeInTheDocument();
    next(driving({ steer_rad: 0 }), rerender);
    expect(screen.queryByTestId('speed-inner')).toBeNull();
  });

  it('속도 추이는 받은 값을 최근 60초만 쌓고 평균을 보인다', () => {
    const { rerender } = show(driving({ v_mps: 0.2, at: 0 }));
    expect(screen.getByTestId('speed-trend')).toHaveAttribute('data-points', '1');
    next(driving({ v_mps: 0.4, at: 1 }), rerender);
    next(driving({ v_mps: 0.3, at: 2 }), rerender);
    expect(screen.getByTestId('speed-trend')).toHaveAttribute('data-points', '3');
    expect(screen.getByTestId('speed-average')).toHaveTextContent('평균 0.30 m/s');
    // 70초 뒤 값: 60초보다 오래된 점은 버린다
    next(driving({ v_mps: 0.1, at: 70 }), rerender);
    expect(screen.getByTestId('speed-trend')).toHaveAttribute('data-points', '1');
    expect(screen.getByTestId('speed-average')).toHaveTextContent('평균 0.10 m/s');
    // 구간마다 그 시점의 주행 모드 색: 자율 → 수동으로 바뀌면 구간이 둘
    const manual = driving({ v_mps: 0.2, at: 72 });
    manual.state.drive.mode = 'MANUAL';
    next(driving({ v_mps: 0.15, at: 71 }), rerender);
    next(manual, rerender);
    const tones = screen.getAllByTestId('trend-run').map((g) => g.getAttribute('data-tone'));
    expect(tones).toEqual(['auto', 'manual']);
    // 표시 연결이 끊기면 비운다
    next(driving({ at: 73 }), rerender, false);
    expect(screen.getByTestId('speed-trend')).toHaveAttribute('data-points', '0');
    expect(screen.getByTestId('speed-average')).toHaveTextContent('평균 —');
  });
});

describe('주행 모드 (기어 표시)', () => {
  const view = (edit: (p: Presentation) => void) => {
    const p = driving();
    edit(p);
    const { unmount } = show(p);
    const active = ['AUTO', 'MANUAL', 'AUTO_READY', 'SAFE_STOP'].filter(
      (key) => screen.getByTestId(`gear-${key}`).getAttribute('data-active') === 'true',
    );
    const gear = active[0] ? screen.getByTestId(`gear-${active[0]}`).getAttribute('data-tone') : null;
    const result = { label: screen.getByTestId('drive-mode').textContent, active, gear };
    unmount();
    return result;
  };

  it('현재 모드 칸만 강조색, 재개 대기는 주의색, 안전 정지는 위험색. 문구에 VM 이름 없음', () => {
    expect(view(() => undefined)).toEqual({ label: '자율 주행', active: ['AUTO'], gear: 'auto' });
    expect(view((p) => (p.state.drive.mode = 'MANUAL'))).toEqual({
      label: '수동 주행',
      active: ['MANUAL'],
      gear: 'manual',
    });
    expect(view((p) => (p.state.drive.mode = 'AUTO_READY'))).toEqual({
      label: '자율 재개 대기',
      active: ['AUTO_READY'],
      gear: 'warning',
    });
    expect(
      view((p) => {
        p.state.drive.mode = 'SAFE_STOP';
        p.state.drive.active_vm_id = null;
      }),
    ).toEqual({ label: '안전 정지', active: ['SAFE_STOP'], gear: 'danger' });
  });

  it('안전 정지는 왼쪽 카드와 정지 칸에 경고 아이콘 (수동 색과 구분)', () => {
    const p = driving();
    p.state.drive.mode = 'SAFE_STOP';
    p.state.drive.active_vm_id = null;
    show(p);
    expect(screen.getByTestId('drive-mode')).toHaveAttribute('data-tone', 'danger');
    expect(screen.getByTestId('drive-mode')).toHaveAttribute('data-band', 'true');
    expect(screen.getByTestId('gear-SAFE_STOP')).toHaveAttribute('data-band', 'true');
    expect(screen.getByTestId('drive-mode').querySelector('svg')).not.toBeNull();
    expect(screen.getByTestId('gear-SAFE_STOP').querySelector('svg')).not.toBeNull();
  });

  it('수동 주행에는 경고 아이콘이 없다', () => {
    const p = driving();
    p.state.drive.mode = 'MANUAL';
    show(p);
    expect(screen.getByTestId('drive-mode')).toHaveAttribute('data-tone', 'manual');
    expect(screen.getByTestId('drive-mode').querySelector('svg')).toBeNull();
    // 색 점 + 색 글자 (안전 정지의 옅은 위험색 바탕·테두리 없음)
    expect(screen.getByTestId('drive-mode').querySelector('i')).not.toBeNull();
    expect(screen.getByTestId('drive-mode')).toHaveAttribute('data-band', 'false');
    expect(screen.getByTestId('gear-MANUAL')).toHaveAttribute('data-band', 'false');
    expect(screen.getByTestId('gear-MANUAL').querySelector('svg')).toBeNull();
  });

  it('준비 중은 그 모드 칸을 주의색, 미확인은 모두 흐리게', () => {
    expect(view((p) => (p.state.drive.authority!.phase = 'PENDING'))).toEqual({
      label: '자율 주행 준비 중',
      active: ['AUTO'],
      gear: 'warning',
    });
    expect(view((p) => (p.state.drive.source = { status: 'stale', age_ms: 4000 }))).toEqual({
      label: '미확인',
      active: [],
      gear: null,
    });
  });
});

describe('가운데 차량 패널', () => {
  it('배터리: 큰 값 % 아래 작은 전압, "추정" 은 툴팁·aria-label 에만', () => {
    show(driving({ batt_v: 11.94 }));
    // (11.94 / 3 − 3.5) / 0.7 = 0.686
    expect(screen.getByTestId('battery-value')).toHaveTextContent('69%');
    expect(screen.getByTestId('battery-sub')).toHaveTextContent('11.9 V');
    const group = screen.getByRole('group', { name: '배터리 잔량 (전압 기반 추정)' });
    expect(group).toHaveAttribute('title', '배터리 잔량 (전압 기반 추정)');
    expect(group).toHaveTextContent('BATTERY');
    expect(screen.getByTestId('combined').textContent).not.toContain('추정');
    expect(screen.getByTestId('battery-bar').querySelector('i')).toHaveStyle({ height: '69%' });
  });

  it('배터리 50% 초과는 정상색, 50% 이하는 주의색, 20% 이하는 위험색, 없으면 "—" 와 잔량 수신 대기', () => {
    const tone = () =>
      screen.getByRole('group', { name: '배터리 잔량 (전압 기반 추정)' }).getAttribute('data-tone');
    const { rerender } = show(driving({ batt_v: 11.94 }));
    expect(tone()).toBe('ok');
    next(driving({ batt_v: 11.55 }), rerender);
    expect(tone()).toBe('warning');
    next(driving({ batt_v: 10.8 }), rerender);
    expect(screen.getByTestId('battery-value')).toHaveTextContent('14%');
    expect(tone()).toBe('danger');
    next(driving({ batt_v: null }), rerender);
    expect(screen.getByTestId('battery-value')).toHaveTextContent('—');
    expect(screen.getByTestId('battery-sub')).toHaveTextContent('수신 대기');
    expect(screen.getByTestId('battery-bar')).toHaveAttribute('data-empty', 'true');
  });

  it('조향 각도를 "N° 좌/우" 로 보이고 앞바퀴만 그만큼 돈다 (steer_rad + = 왼쪽)', () => {
    const { rerender } = show(driving({ steer_rad: 0.21 }));
    expect(screen.getByTestId('steer-value')).toHaveTextContent('12° 좌');
    expect(screen.getByRole('group', { name: '조향 각도' })).toHaveAttribute('data-tone', 'interaction');
    // SVG 회전은 시계 방향 + → 왼쪽 조향은 −
    expect(screen.getByTestId('wheel-front-left')).toHaveAttribute('data-angle', '-12.0');
    expect(screen.getByTestId('wheel-front-right')).toHaveAttribute('data-angle', '-12.0');
    next(driving({ steer_rad: -0.21 }), rerender);
    expect(screen.getByTestId('steer-value')).toHaveTextContent('12° 우');
    expect(screen.getByTestId('wheel-front-left')).toHaveAttribute('data-angle', '12.0');
    next(driving({ steer_rad: 0.004 }), rerender);
    expect(screen.getByTestId('steer-value')).toHaveTextContent('0°');
  });

  it('위쪽 알약 칸에 현재 시각', () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 11, 9, 5));
    show(driving());
    const expected = new Date(2026, 10, 11, 9, 5).toLocaleTimeString('ko-KR', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
    const panel = screen.getByRole('region', { name: '차량' });
    expect(within(panel).getByTestId('clock')).toHaveTextContent(expected);
    expect(expected).toContain('05');
    expect(within(panel).getByTestId('car-top')).toBeInTheDocument();
  });
});

describe('운전자 화면 공통', () => {
  it('왼쪽 패널에 주행 상태·표시 연결과 음악 카드', () => {
    show(driving());
    const panel = screen.getByRole('region', { name: '주행 상태와 음악' });
    // 카드 소제목은 작은 영문 대문자
    expect(panel).toHaveTextContent('DRIVE MODE');
    expect(panel).toHaveTextContent('MUSIC');
    expect(within(panel).getByTestId('drive-mode')).toHaveTextContent('자율 주행');
    expect(within(panel).getByTestId('link')).toHaveTextContent('연결됨');
    const card = within(panel).getByTestId('display-audio');
    expect(within(card).getByTestId('music-title')).toHaveTextContent('Interaction Theme');
    expect(within(card).getByTestId('music-artist')).toHaveTextContent('Xenova');
    expect(within(card).getByRole('button', { name: '음악 재생' })).toBeInTheDocument();
    expect(within(card).getByRole('progressbar', { name: '재생 위치' })).toBeInTheDocument();
    expect(within(card).queryByRole('slider')).toBeNull();
  });

  it('시각·배터리·연결 상태를 한 번씩만 보인다 (상단 바 없음)', () => {
    show(driving());
    expect(screen.getAllByTestId('clock')).toHaveLength(1);
    expect(screen.getAllByText('연결됨')).toHaveLength(1);
    expect(screen.getAllByText('80%')).toHaveLength(1);
    expect(screen.getByTestId('combined').querySelector(':scope > header')).toBeNull();
  });

  it('VM 정보·내비·시연 단계·예상 주행 가능 시간을 보이지 않는다', () => {
    show(driving());
    const text = screen.getByTestId('combined').textContent ?? '';
    expect(text).not.toMatch(/VM|운전 중|승계|인포테인먼트|예상 주행|트립|장애물|P1/);
    for (const id of ['parked-view', 'active-vm', 'dock-VM1', 'nav-banner', 'step-avoid', 'runtime-value']) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
  });

  it('표시 연결이 끊기면 알리고 값을 지운다. 시각은 계속 보인다', () => {
    const { rerender } = show(driving());
    next(driving(), rerender, false);
    expect(screen.getByRole('alert')).toHaveTextContent('현재 값은 미확인');
    expect(screen.getByTestId('link')).toHaveTextContent('연결 끊김');
    expect(screen.getByTestId('speed-value')).toHaveTextContent('—');
    expect(screen.getByTestId('speed-max')).toHaveTextContent('MAX —');
    expect(screen.getByTestId('battery-value')).toHaveTextContent('—');
    expect(screen.getByTestId('steer-value')).toHaveTextContent('—');
    expect(screen.getByTestId('drive-mode')).toHaveTextContent('미확인');
    expect(screen.getByTestId('clock').textContent).not.toBe('');
  });

  it('처음부터 수신 전이어도 빈 값으로 같은 화면을 그린다', () => {
    show(presentationFixture());
    expect(screen.getByTestId('speed-value')).toHaveTextContent('—');
    expect(screen.getByTestId('drive-mode')).toHaveTextContent('미확인');
    expect(screen.getByTestId('battery-value')).toHaveTextContent('—');
    expect(screen.getByTestId('steer-sub')).toHaveTextContent('수신 대기');
  });

  it('시각·주소와 상관없이 항상 어두운 Xenova 테마', () => {
    window.history.replaceState(null, '', '/display/combined?theme=light');
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 11, 12, 0));
    show(driving());
    expect(screen.getByTestId('combined')).toHaveAttribute('data-theme', 'xenova');
  });

  it('theme.ts 의 시각 규칙은 그대로 둔다 (이 화면은 쓰지 않음)', () => {
    expect(themeAt(new Date(2026, 10, 5, 12))).toBe('light');
    expect(themeAt(new Date(2026, 10, 5, 21))).toBe('dark');
  });
});
