/**
 * VM vCPU·메모리 입력 규칙. 생성 화면(VmCreatePage)과 조정 화면(ResourceAdjustmentPanel)이 같이 쓴다 (QA S15P31E110-469).
 * 백엔드 CreateVmRequest·UpdateVmResourcesRequest 는 vCPU 1~4, 메모리 최대 1024 MiB 이고 하한은 템플릿 min_memory_mib 다.
 * 메모리는 Xen 할당 단위에 맞춰 화면에서 16 MiB 단위로만 받는다.
 */
export const VCPUS_MIN = 1;
export const VCPUS_MAX = 4;
export const MEMORY_MAX_MIB = 1024;
export const MEMORY_STEP_MIB = 16;
/** 템플릿 최소값을 모를 때의 하한 (백엔드 UpdateVmResourcesRequest @Min 128) */
export const DEFAULT_MEMORY_MIN_MIB = 128;

/** 템플릿 최소값이 있으면 그것, 없으면 기본 하한 */
export function memoryMinMib(templateMinMib: number | null | undefined): number {
  return templateMinMib ?? DEFAULT_MEMORY_MIN_MIB;
}

/** 규칙에 맞으면 null, 아니면 입력칸 아래에 보일 이유 */
export function memoryProblem(value: number, minMib: number): string | null {
  if (!Number.isInteger(value)) return '메모리는 MiB 단위 정수로 입력해 주세요.';
  if (value < minMib || value > MEMORY_MAX_MIB) {
    return `메모리는 ${minMib}~${MEMORY_MAX_MIB} MiB 사이로 입력해 주세요.`;
  }
  if (value % MEMORY_STEP_MIB !== 0) return `메모리는 ${MEMORY_STEP_MIB} MiB 단위로 입력해 주세요.`;
  return null;
}

export const VM_NAME_MAX = 63;
/** 백엔드 \s (Java 기본) 와 같은 앞뒤 공백 */
// eslint-disable-next-line no-control-regex
const EDGE_SPACE = /^[ \t\n\x0B\f\r]|[ \t\n\x0B\f\r]$/;
/** 제어문자(Cc)·줄 구분자(Zl)·문단 구분자(Zp) */
const CONTROL_CHAR = /[\p{Cc}\p{Zl}\p{Zp}]/u;

/**
 * VM 이름 규칙 (백엔드 CreateVmRequest: 1~63자, 제어문자·앞뒤 공백 금지). 규칙에 맞으면 null.
 * 길이는 백엔드처럼 문자(code point) 수로 센다.
 */
export function vmNameProblem(name: string): string | null {
  if (name.length === 0) return 'VM 이름을 입력해 주세요.';
  if (EDGE_SPACE.test(name)) return 'VM 이름 앞뒤에는 공백을 넣을 수 없습니다.';
  if (CONTROL_CHAR.test(name)) return 'VM 이름에는 줄바꿈·탭 같은 제어문자를 쓸 수 없습니다.';
  const length = [...name].length;
  if (length > VM_NAME_MAX) return `VM 이름은 ${VM_NAME_MAX}자 이하로 입력해 주세요. (지금 ${length}자)`;
  return null;
}

/**
 * VM 이름 비교 키 — 백엔드 DB(uk_vms_name, utf8mb4_0900_ai_ci)처럼 대소문자·악센트를 무시한다 (S15P31E110-473).
 * NFKD 로 분해해 결합 부호(악센트)를 빼고 소문자로 맞춘다. 서버는 이 기준으로 같은 이름을 찾으므로
 * (Drive-VM 을 만들면 drive-vm 을 찾는다) 화면도 같은 기준으로 막는다.
 * 앞뒤 공백은 지우지 않는다 — 0900 collation 은 NO PAD 이고, 공백 규칙은 vmNameProblem 이 따로 본다.
 */
export function vmNameKey(name: string): string {
  return name.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
}

/** DB 기준으로 같은 VM 이름인가 */
export function sameVmName(a: string, b: string): boolean {
  return vmNameKey(a) === vmNameKey(b);
}

/** 규칙에 맞으면 null, 아니면 입력칸 아래에 보일 이유 */
export function vcpusProblem(value: number): string | null {
  if (!Number.isInteger(value) || value < VCPUS_MIN || value > VCPUS_MAX) {
    return `vCPU는 ${VCPUS_MIN}~${VCPUS_MAX}개 사이 정수로 입력해 주세요.`;
  }
  return null;
}
