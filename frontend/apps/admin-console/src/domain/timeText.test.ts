import { describe, expect, it } from 'vitest';
import { formatElapsedTime } from '../format';
import { tooltipTime } from '../pages/resourceFormat';
import { shortClock } from '../pages/vmDetailEventFormat';
import { clock } from './events';
import { ago } from './roles';

/** 화면 시각 문구에 NaN·Invalid Date·음수가 나오지 않는다 (S15P31E110-471) */
const BROKEN = /NaN|Invalid Date|-\d/;

describe('시각·경과 시간 문구', () => {
  it('ago: 값이 없거나 숫자가 아니면 —, 음수(시계 차이)는 0초', () => {
    expect(ago(Number.NaN)).toBe('—');
    expect(ago(Number.POSITIVE_INFINITY)).toBe('—');
    expect(ago(-300)).toBe('0.0초 전');
    expect(ago(1500)).toBe('2초 전');
  });

  it('clock: 읽을 수 없는 시각은 —', () => {
    expect(clock('not-a-date')).toBe('—');
    expect(clock('')).toBe('—');
    expect(clock('2026-11-14T01:02:03.004Z')).not.toMatch(BROKEN);
  });

  it('shortClock(VM 상세 이벤트 시계): 읽을 수 없는 시각은 —', () => {
    expect(shortClock('not-a-date')).toBe('—');
    expect(shortClock('')).toBe('—');
    expect(shortClock('2026-11-14T01:02:03.004Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it('formatElapsedTime: NaN·음수는 00:00', () => {
    expect(formatElapsedTime(Number.NaN)).toBe('00:00');
    expect(formatElapsedTime(-5000)).toBe('00:00');
    expect(formatElapsedTime(65_000)).toBe('01:05');
  });

  it('tooltipTime: 읽을 수 없는 시각은 시각 미상', () => {
    expect(tooltipTime('broken')).toBe('시각 미상');
    const now = Date.parse('2026-11-14T00:00:10Z');
    // 미래 시각(시계 차이)도 음수 대신 0초
    expect(tooltipTime('2026-11-14T00:00:20Z', now)).toMatch(/^0초 전 · /);
    expect(tooltipTime('2026-11-14T00:00:20Z', now)).not.toMatch(BROKEN);
  });
});
