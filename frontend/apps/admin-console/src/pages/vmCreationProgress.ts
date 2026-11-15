import { useSyncExternalStore } from 'react';
import { sameVmName } from '../domain/vmLimits';
import { formatElapsedTime } from '../format';

export const VM_CREATION_LEAVE_MESSAGE =
  '다른 화면으로 이동해도 VM 생성은 계속됩니다. 완료 여부는 VM 목록에서 확인하세요.';

const LAST_DURATION_KEY = 'xenova.vm-create.last-duration-ms';
const PENDING_CREATIONS_KEY = 'xenova.vm-create.pending';
/** 백엔드 생성 요청 타임아웃(command-timeout 20m)보다 1분 길게 둔다 */
export const PENDING_CREATION_TTL_MS = 21 * 60 * 1000;

export type PendingVmCreation = {
  name: string;
  startedAt: number;
  /** 생성 요청 시점에 이미 같은 이름으로 있던 VM. 이 VM 카드는 생성 중으로 표시하지 않는다 */
  excludedVmIds?: readonly string[];
};

type VmIdentity = { vm_id: string; name: string };

let pendingCreations = loadPendingCreations();
/**
 * 이 탭(이 페이지 실행)이 보낸 생성 요청 (S15P31E110-473). 다른 탭·새로고침 전에서 남은 기록은 여기에 없다 —
 * 그 요청의 결과(성공·실패)는 이 탭이 받지 못하므로 "생성 중" 대신 "생성 결과 확인 필요"로 보이고 카드를 잠그지 않는다.
 */
const ownedCreations = new Set<string>();
const creationKey = (creation: PendingVmCreation) => `${creation.startedAt}:${creation.name}`;
let expiryTimer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();
scheduleExpiry();

export function usePendingVmCreations(): readonly PendingVmCreation[] {
  return useSyncExternalStore(subscribe, getPendingCreations, getPendingCreations);
}

export function startVmCreation(
  name: string,
  startedAt = Date.now(),
  excludedVmIds: readonly string[] = [],
): void {
  const creation: PendingVmCreation =
    excludedVmIds.length > 0 ? { name, startedAt, excludedVmIds: [...excludedVmIds] } : { name, startedAt };
  ownedCreations.add(creationKey(creation));
  replacePendingCreations([...pendingCreations.filter((item) => item.name !== name), creation]);
}

export function finishVmCreation(name: string): void {
  const next = pendingCreations.filter((creation) => creation.name !== name);
  if (next.length === pendingCreations.length) return;
  replacePendingCreations(next);
}

/** 이 탭이 보낸 생성 요청의 기록인가 (아니면 결과를 알 수 없다) */
export function isOwnedVmCreation(creation: PendingVmCreation): boolean {
  return ownedCreations.has(creationKey(creation));
}

/** 결과를 모르는 기록을 사용자가 지운다 ([기록 지우기]) */
export function clearVmCreation(creation: PendingVmCreation): void {
  const key = creationKey(creation);
  const next = pendingCreations.filter((item) => creationKey(item) !== key);
  if (next.length === pendingCreations.length) return;
  replacePendingCreations(next);
}

/** 이름이 같고(DB 기준, 대소문자·악센트 무시), 요청 전부터 있던 VM이 아닐 때만 그 생성 기록에 해당하는 VM으로 본다 */
export function matchesVmCreation(vm: VmIdentity, creation: PendingVmCreation): boolean {
  return sameVmName(creation.name, vm.name) && !(creation.excludedVmIds ?? []).includes(vm.vm_id);
}

/** 이 탭이 보낸 생성이 아직 끝나지 않은 VM 인가 — 결과를 모르는 기록(다른 탭·새로고침 전)은 카드를 잠그지 않는다 */
export function isVmCreationPending(
  vm: VmIdentity & { state: string },
  creations: readonly PendingVmCreation[],
): boolean {
  return (
    vm.state !== 'running' &&
    creations.some((creation) => isOwnedVmCreation(creation) && matchesVmCreation(vm, creation))
  );
}

export function readLastVmCreationDuration(): number | null {
  const value = readStorage(LAST_DURATION_KEY);
  if (value === null) return null;
  const duration = Number(value);
  return Number.isFinite(duration) && duration > 0 ? duration : null;
}

export function saveLastVmCreationDuration(durationMs: number): void {
  if (!Number.isFinite(durationMs) || durationMs <= 0) return;
  writeStorage(LAST_DURATION_KEY, String(Math.round(durationMs)));
}

/** 생성 경과·예상 시간 — 정지·삭제 진행 표시와 같은 mm:ss */
export const formatVmCreationTime = formatElapsedTime;

export function estimatedRemainingTime(elapsedMs: number, previousDurationMs: number): number {
  return Math.max(0, previousDurationMs - Math.max(0, elapsedMs));
}

export function blockVmCreationUnload(event: BeforeUnloadEvent): string {
  event.preventDefault();
  event.returnValue = VM_CREATION_LEAVE_MESSAGE;
  return VM_CREATION_LEAVE_MESSAGE;
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0 && typeof window !== 'undefined') {
    window.addEventListener('storage', handleStorage);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && typeof window !== 'undefined') {
      window.removeEventListener('storage', handleStorage);
    }
  };
}

function getPendingCreations(): readonly PendingVmCreation[] {
  return pendingCreations;
}

function emitChange(): void {
  listeners.forEach((listener) => listener());
}

function replacePendingCreations(next: PendingVmCreation[]): void {
  pendingCreations = next;
  persistPendingCreations();
  emitChange();
  scheduleExpiry();
}

/** 다른 탭에서 생성 기록이 바뀌면 이 탭의 목록에도 반영한다 */
function handleStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== PENDING_CREATIONS_KEY) return;
  pendingCreations = loadPendingCreations();
  emitChange();
  scheduleExpiry();
}

/** 가장 먼저 만료되는 기록 시각에 맞춰 정리한다. 앱을 켜 둔 채로 TTL이 지나도 기록이 남지 않는다 */
function scheduleExpiry(): void {
  if (expiryTimer !== undefined) clearTimeout(expiryTimer);
  expiryTimer = undefined;
  if (pendingCreations.length === 0) return;
  const nextExpiry =
    Math.min(...pendingCreations.map((creation) => creation.startedAt)) + PENDING_CREATION_TTL_MS;
  expiryTimer = setTimeout(removeExpiredCreations, Math.max(0, nextExpiry - Date.now()));
}

function removeExpiredCreations(): void {
  expiryTimer = undefined;
  const cutoff = Date.now() - PENDING_CREATION_TTL_MS;
  const next = pendingCreations.filter((creation) => creation.startedAt > cutoff);
  if (next.length === pendingCreations.length) {
    scheduleExpiry();
    return;
  }
  replacePendingCreations(next);
}

function loadPendingCreations(): PendingVmCreation[] {
  const value = readStorage(PENDING_CREATIONS_KEY);
  if (!value) return [];
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) return [];
    const cutoff = Date.now() - PENDING_CREATION_TTL_MS;
    return parsed.filter(isPendingVmCreation).filter((creation) => creation.startedAt > cutoff);
  } catch {
    return [];
  }
}

function isPendingVmCreation(value: unknown): value is PendingVmCreation {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<PendingVmCreation>;
  return (
    typeof candidate.name === 'string' &&
    candidate.name.length > 0 &&
    Number.isFinite(candidate.startedAt) &&
    (candidate.excludedVmIds === undefined ||
      (Array.isArray(candidate.excludedVmIds) &&
        candidate.excludedVmIds.every((vmId) => typeof vmId === 'string')))
  );
}

function persistPendingCreations(): void {
  writeStorage(PENDING_CREATIONS_KEY, JSON.stringify(pendingCreations));
}

function readStorage(key: string): string | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    if (typeof window !== 'undefined') window.localStorage.setItem(key, value);
  } catch {
    // 저장소가 차단되어도 현재 탭의 생성 요청은 계속 추적한다.
  }
}
