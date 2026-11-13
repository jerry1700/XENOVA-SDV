/**
 * 메모리 부하 상한 (백엔드 AgentStressService.maxMemoryStressMib 와 같다, QA 1-B3).
 * VM 메모리의 70%(소수점 버림)와 512 MiB 중 작은 값. 512 는 에이전트 자체 상한과 같다.
 */
export const MEMORY_STRESS_PERCENT_OF_VM = 70;
export const MEMORY_STRESS_MAX_MIB = 512;
/** 처음 보이는 값. 상한보다 크면 상한으로 줄인다 */
const DEFAULT_MEMORY_STRESS_MIB = 256;

export function maxMemoryStressMib(vmMemoryMib: number): number {
  return Math.min(Math.floor((vmMemoryMib * MEMORY_STRESS_PERCENT_OF_VM) / 100), MEMORY_STRESS_MAX_MIB);
}

export function defaultMemoryStressMib(vmMemoryMib: number): number {
  return Math.min(DEFAULT_MEMORY_STRESS_MIB, maxMemoryStressMib(vmMemoryMib));
}

/** 입력 옆 안내: "1~358 MiB (할당 512 MiB의 70%, 최대 512 MiB)" */
export function memoryStressHint(vmMemoryMib: number): string {
  return `1~${maxMemoryStressMib(vmMemoryMib)} MiB (할당 ${vmMemoryMib} MiB의 ${MEMORY_STRESS_PERCENT_OF_VM}%, 최대 ${MEMORY_STRESS_MAX_MIB} MiB)`;
}
