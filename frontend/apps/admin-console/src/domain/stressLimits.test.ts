import { describe, expect, it } from 'vitest';
import { defaultMemoryStressMib, maxMemoryStressMib, memoryStressHint } from './stressLimits';

describe('메모리 부하 상한 (백엔드와 같은 규칙)', () => {
  it('VM 메모리의 70%를 버림하고 512 MiB를 넘지 않는다', () => {
    expect(maxMemoryStressMib(256)).toBe(179);
    expect(maxMemoryStressMib(512)).toBe(358);
    expect(maxMemoryStressMib(832)).toBe(512);
    expect(maxMemoryStressMib(1024)).toBe(512);
  });

  it('처음 값은 256 이지만 상한보다 크면 상한으로 줄인다', () => {
    expect(defaultMemoryStressMib(512)).toBe(256);
    expect(defaultMemoryStressMib(256)).toBe(179);
  });

  it('안내 문구에 상한과 기준을 함께 보인다', () => {
    expect(memoryStressHint(512)).toBe('1~358 MiB (할당 512 MiB의 70%, 최대 512 MiB)');
  });
});
