import { describe, expect, it } from 'vitest';
import {
  categoryOf,
  describe as describeEvent,
  isKnownEventType,
  payloadNumber,
  payloadText,
  UNKNOWN_EVENT_LABEL,
  unknownEventType,
} from './events';
import type { EventType, VmEvent } from '../api/types';

function event(type: EventType): VmEvent {
  return {
    event_id: 'event',
    type,
    severity: 'INFO',
    vm_id: 'vm',
    operation_id: null,
    occurred_at: '2026-10-26T00:00:00Z',
    recorded_at: '2026-10-26T00:00:00Z',
    payload: {},
  };
}

describe('recovery outcome labels', () => {
  it('keeps completion separate from app readiness and vehicle control', () => {
    expect(describeEvent(event('recovery.completed'), () => 'VM1').detail).toContain('별도 확인');
    expect(describeEvent(event('recovery.completed'), () => 'VM1').detail).not.toBe('앱 명령 재개');
  });
  it.each(['recovery.recovered', 'recovery.cancelled'] as const)(
    'does not classify %s as failure',
    (type) => {
      expect(categoryOf(event(type))).toBe('lifecycle');
      expect(describeEvent(event(type), () => 'VM1').title).not.toContain('실패');
    },
  );
});

describe('payload 값 글자 처리 — [object Object]·NaN·undefined 미표시 (S15P31E110-469)', () => {
  const RAW = /\[object Object\]|NaN|undefined/;
  const of = (type: EventType, payload: Record<string, unknown>) =>
    describeEvent({ ...event(type), payload }, () => 'vm');

  it('payloadText: 글자·유한한 숫자·참거짓만, 나머지는 빈 글자', () => {
    expect(payloadText('AUTO')).toBe('AUTO');
    expect(payloadText(120)).toBe('120');
    expect(payloadText(false)).toBe('false');
    for (const value of [{ a: 1 }, [1, 2], null, undefined, Number.NaN, Infinity]) {
      expect(payloadText(value)).toBe('');
    }
    expect(payloadNumber(120)).toBe(120);
    expect(payloadNumber('120')).toBeNull();
    expect(payloadNumber({ ms: 1 })).toBeNull();
  });

  it('객체·배열이 와도 제목·설명에 원문이 새지 않는다', () => {
    const odd = {
      from: { a: 1 },
      to: ['MANUAL'],
      reason: { code: 'x' },
      generation: { n: 1 },
      handoff_ms: { v: 1 },
    };
    const types: EventType[] = ['drive.mode.changed', 'drive.fault.detected', 'vm.state.changed'];
    for (const type of types) {
      const { title, detail } = of(type, odd);
      expect(`${title} ${detail}`).not.toMatch(RAW);
    }
    const resume = of('drive.resume.accepted', {
      switch_in_ms: { v: 1 },
      requested_by: { id: 1 },
      generation: [],
    });
    expect(`${resume.title} ${resume.detail}`).not.toMatch(RAW);
    expect(resume.title).toContain('3초 뒤');
  });

  it('숫자 인계 시간은 그대로 보인다', () => {
    expect(of('drive.mode.changed', { from: 'AUTO', to: 'MANUAL', handoff_ms: 120 }).detail).toContain(
      '인계 120 ms',
    );
  });
});

describe('이벤트 종류 원문을 화면 문구에 쓰지 않는다 (S15P31E110-471)', () => {
  const ALL: EventType[] = [
    'vm.created',
    'vm.deleted',
    'vm.state.changed',
    'agent.status.changed',
    'recovery.started',
    'recovery.completed',
    'recovery.recovered',
    'recovery.cancelled',
    'recovery.failed',
    'demo.command.sent',
    'demo.command.completed',
    'demo.command.failed',
    'drive.mode.changed',
    'drive.fault.detected',
    'drive.resume.accepted',
    'drive.resume.cancelled',
    'display.route.changed',
  ];

  it.each(ALL)('%s 의 제목에 원문 type 이 없다', (type) => {
    const { title, detail } = describeEvent(event(type), () => 'VM1');
    expect(title).not.toContain(type);
    expect(detail).not.toContain(type);
    expect(isKnownEventType(type)).toBe(true);
    expect(unknownEventType(event(type))).toBeNull();
  });

  it('모르는 종류는 "기타 이벤트"이고 원문 type 은 unknownEventType 으로만 얻는다', () => {
    const unknown = event('vm.migrated' as EventType);
    expect(describeEvent(unknown, () => 'VM1')).toEqual({ title: `VM1 ${UNKNOWN_EVENT_LABEL}`, detail: '' });
    expect(UNKNOWN_EVENT_LABEL).toBe('기타 이벤트');
    expect(isKnownEventType('vm.migrated')).toBe(false);
    expect(unknownEventType(unknown)).toBe('vm.migrated');
  });
});
