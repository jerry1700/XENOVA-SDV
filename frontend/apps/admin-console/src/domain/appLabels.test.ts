import { describe, expect, it } from 'vitest';
import { appLabel, appReasonLabel } from './appLabels';

describe('앱 ID·사유 한글 라벨', () => {
  it('앱 ID 는 자율주행·수동주행·인터랙션, 그 외는 원문', () => {
    expect(['autonomous', 'manual', 'interaction', 'drive'].map(appLabel)).toEqual([
      '자율주행',
      '수동주행',
      '인터랙션',
      'drive',
    ]);
    // 객체 기본 속성 이름도 원문 그대로
    expect(appLabel('toString')).toBe('toString');
  });

  it('백엔드 REASONS를 한글로 바꾸고, 모르는 코드는 원문', () => {
    const reasons = [
      'CONTROL_UNAPPLIED',
      'INPUT_UNAVAILABLE',
      'INPUT_STALE',
      'INPUT_INVALID',
      'STATE_PREPARING',
      'CALCULATION_FAILED',
      'OUTPUT_INVALID',
      'APP_STOPPING',
      'APP_EXITED',
      'RESOURCE_RETAINED',
    ];
    expect(reasons.map(appReasonLabel)).toEqual([
      '제어 미적용',
      '입력 없음',
      '입력 만료',
      '입력 이상',
      '상태 준비 중',
      '계산 실패',
      '출력 이상',
      '앱 종료 중',
      '앱 스레드 종료',
      '자원 회수 대기',
    ]);
    expect(appReasonLabel('PATH_UNAVAILABLE')).toBe('PATH_UNAVAILABLE');
  });
});
