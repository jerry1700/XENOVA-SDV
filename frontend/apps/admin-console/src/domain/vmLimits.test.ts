import { describe, expect, it } from 'vitest';
import {
  DEFAULT_MEMORY_MIN_MIB,
  memoryMinMib,
  memoryProblem,
  sameVmName,
  vcpusProblem,
  vmNameKey,
  vmNameProblem,
} from './vmLimits';

describe('VM 메모리·vCPU 규칙 (생성·조정 공용)', () => {
  it('메모리는 템플릿 최소값~1024 MiB, 16 MiB 단위', () => {
    expect(memoryProblem(256, 256)).toBeNull();
    expect(memoryProblem(1024, 256)).toBeNull();
    expect(memoryProblem(272, 256)).toBeNull();
    expect(memoryProblem(260, 256)).toBe('메모리는 16 MiB 단위로 입력해 주세요.');
    expect(memoryProblem(240, 256)).toBe('메모리는 256~1024 MiB 사이로 입력해 주세요.');
    expect(memoryProblem(1040, 256)).toBe('메모리는 256~1024 MiB 사이로 입력해 주세요.');
    expect(memoryProblem(Number.NaN, 256)).toBe('메모리는 MiB 단위 정수로 입력해 주세요.');
    expect(memoryProblem(256.5, 256)).toBe('메모리는 MiB 단위 정수로 입력해 주세요.');
  });

  it('템플릿 최소값을 모르면 기본 하한', () => {
    expect(memoryMinMib(512)).toBe(512);
    expect(memoryMinMib(undefined)).toBe(DEFAULT_MEMORY_MIN_MIB);
    expect(memoryMinMib(null)).toBe(DEFAULT_MEMORY_MIN_MIB);
  });

  it('VM 이름: 빈 값·앞뒤 공백·제어문자·63자 초과마다 다른 이유', () => {
    expect(vmNameProblem('drive-vm-a')).toBeNull();
    expect(vmNameProblem('가운데 공백 허용')).toBeNull();
    expect(vmNameProblem('a'.repeat(63))).toBeNull();
    expect(vmNameProblem('가'.repeat(63))).toBeNull();
    expect(vmNameProblem('')).toBe('VM 이름을 입력해 주세요.');
    expect(vmNameProblem(' a')).toBe('VM 이름 앞뒤에는 공백을 넣을 수 없습니다.');
    expect(vmNameProblem('a\t')).toBe('VM 이름 앞뒤에는 공백을 넣을 수 없습니다.');
    expect(vmNameProblem('a\nb')).toBe('VM 이름에는 줄바꿈·탭 같은 제어문자를 쓸 수 없습니다.');
    expect(vmNameProblem('a\u0007b')).toBe('VM 이름에는 줄바꿈·탭 같은 제어문자를 쓸 수 없습니다.');
    expect(vmNameProblem(`a${String.fromCharCode(0x2028)}b`)).toBe(
      'VM 이름에는 줄바꿈·탭 같은 제어문자를 쓸 수 없습니다.',
    );
    expect(vmNameProblem('a'.repeat(64))).toBe('VM 이름은 63자 이하로 입력해 주세요. (지금 64자)');
  });

  it('vCPU 는 1~4 정수', () => {
    expect(vcpusProblem(1)).toBeNull();
    expect(vcpusProblem(4)).toBeNull();
    for (const bad of [0, 5, 1.5, Number.NaN]) {
      expect(vcpusProblem(bad)).toBe('vCPU는 1~4개 사이 정수로 입력해 주세요.');
    }
  });
});

describe('VM 이름 비교 — DB(utf8mb4_0900_ai_ci) 기준 (S15P31E110-473)', () => {
  it('대소문자를 무시한다', () => {
    expect(sameVmName('Drive-VM', 'drive-vm')).toBe(true);
    expect(sameVmName('DEBIAN-DEV', 'debian-dev')).toBe(true);
    expect(sameVmName('drive-vm', 'drive_vm')).toBe(false);
  });

  it('악센트를 무시한다 (é = e)', () => {
    expect(sameVmName('café', 'cafe')).toBe(true);
    expect(sameVmName('Crème-VM', 'creme-vm')).toBe(true);
    expect(sameVmName('naïve', 'naive')).toBe(true);
  });

  it('한글은 같은 글자끼리만 같다', () => {
    expect(sameVmName('주행-가상머신', '주행-가상머신')).toBe(true);
    expect(sameVmName('주행', '주차')).toBe(false);
    expect(sameVmName('주행', '주행2')).toBe(false);
  });

  it('이모지 이름: 같은 이모지는 같고 다른 이모지는 다르다', () => {
    expect(sameVmName('🚗car', '🚗CAR')).toBe(true);
    expect(sameVmName('🚗', '🚕')).toBe(false);
  });

  it('앞뒤 공백은 지우지 않는다 — 공백 규칙은 기존대로 vmNameProblem 이 막는다', () => {
    expect(sameVmName(' drive', 'drive')).toBe(false);
    expect(vmNameKey('drive ')).toBe('drive ');
    expect(vmNameProblem(' drive')).toBe('VM 이름 앞뒤에는 공백을 넣을 수 없습니다.');
  });
});
