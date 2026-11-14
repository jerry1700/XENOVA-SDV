import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  blockVmCreationUnload,
  estimatedRemainingTime,
  finishVmCreation,
  formatVmCreationTime,
  isVmCreationPending,
  matchesVmCreation,
  PENDING_CREATION_TTL_MS,
  readLastVmCreationDuration,
  saveLastVmCreationDuration,
  startVmCreation,
  usePendingVmCreations,
  VM_CREATION_LEAVE_MESSAGE,
} from './vmCreationProgress';

const PENDING_KEY = 'xenova.vm-create.pending';

describe('VM 생성 진행 상태', () => {
  beforeEach(() => {
    localStorage.clear();
    finishVmCreation('create-test');
  });

  afterEach(() => {
    finishVmCreation('create-test');
    vi.useRealTimers();
  });

  it('경과 시간과 직전 생성 기준 남은 시간을 계산한다', () => {
    expect(formatVmCreationTime(65_900)).toBe('01:05');
    expect(estimatedRemainingTime(30_000, 90_000)).toBe(60_000);
    expect(estimatedRemainingTime(100_000, 90_000)).toBe(0);
  });

  it('직전 생성 소요 시간을 저장하고 읽는다', () => {
    expect(readLastVmCreationDuration()).toBeNull();
    saveLastVmCreationDuration(75_250);
    expect(readLastVmCreationDuration()).toBe(75_250);
  });

  it('새로고침 이탈 안내에 실제 동작을 설명한다', () => {
    const event = new Event('beforeunload', { cancelable: true }) as BeforeUnloadEvent;
    expect(blockVmCreationUnload(event)).toBe(VM_CREATION_LEAVE_MESSAGE);
    expect(event.defaultPrevented).toBe(true);
  });

  it('생성 시작과 종료를 브라우저 저장소에 기록한다', () => {
    startVmCreation('create-test', Date.now());
    expect(localStorage.getItem(PENDING_KEY)).toContain('create-test');
    finishVmCreation('create-test');
    expect(localStorage.getItem(PENDING_KEY)).toBe('[]');
  });

  it('목록에서 기록만 된 VM은 생성 중, 실행된 VM은 완료로 구분한다', () => {
    const creations = [{ name: 'create-test', startedAt: 1_000 }];
    // 이 탭이 보낸 생성만 카드를 잠근다 (S15P31E110-473)
    startVmCreation('create-test', 1_000);
    expect(isVmCreationPending({ vm_id: 'vm-1', name: 'create-test', state: 'stopped' }, creations)).toBe(
      true,
    );
    expect(isVmCreationPending({ vm_id: 'vm-1', name: 'create-test', state: 'running' }, creations)).toBe(
      false,
    );
    expect(isVmCreationPending({ vm_id: 'vm-2', name: 'another-vm', state: 'stopped' }, creations)).toBe(
      false,
    );
  });

  it('요청 전부터 같은 이름으로 있던 VM은 생성 중으로 표시하지 않는다', () => {
    const creations = [{ name: 'create-test', startedAt: 1_000, excludedVmIds: ['vm-existing'] }];
    startVmCreation('create-test', 1_000, ['vm-existing']);
    expect(
      isVmCreationPending({ vm_id: 'vm-existing', name: 'create-test', state: 'stopped' }, creations),
    ).toBe(false);
    expect(isVmCreationPending({ vm_id: 'vm-new', name: 'create-test', state: 'stopped' }, creations)).toBe(
      true,
    );
  });

  it('앱을 켜 둔 채로 TTL(21분)이 지나면 새로고침 없이 기록을 지운다', () => {
    expect(PENDING_CREATION_TTL_MS).toBe(21 * 60 * 1000);
    vi.useFakeTimers();
    const { result } = renderHook(() => usePendingVmCreations());
    act(() => startVmCreation('create-test', Date.now()));
    expect(result.current.map((creation) => creation.name)).toEqual(['create-test']);

    act(() => vi.advanceTimersByTime(PENDING_CREATION_TTL_MS - 1_000));
    expect(result.current).toHaveLength(1);

    act(() => vi.advanceTimersByTime(1_000));
    expect(result.current).toEqual([]);
    expect(localStorage.getItem(PENDING_KEY)).toBe('[]');
  });

  it('다른 탭에서 바뀐 생성 기록을 이 탭에도 반영한다', () => {
    const { result } = renderHook(() => usePendingVmCreations());
    expect(result.current).toEqual([]);

    const value = JSON.stringify([{ name: 'create-test', startedAt: Date.now() }]);
    act(() => {
      localStorage.setItem(PENDING_KEY, value);
      window.dispatchEvent(new StorageEvent('storage', { key: PENDING_KEY, newValue: value }));
    });
    expect(result.current.map((creation) => creation.name)).toEqual(['create-test']);

    act(() => {
      localStorage.setItem(PENDING_KEY, '[]');
      window.dispatchEvent(new StorageEvent('storage', { key: PENDING_KEY, newValue: '[]' }));
    });
    expect(result.current).toEqual([]);
  });
});

describe('생성 기록과 VM 맞추기 — DB 기준 이름 (S15P31E110-473)', () => {
  it('대소문자·악센트만 다른 이름도 같은 생성으로 본다 (서버가 같은 VM 으로 찾으므로)', () => {
    const creation = { name: 'Drive-VM', startedAt: 0 };
    expect(matchesVmCreation({ vm_id: 'vm-1', name: 'drive-vm' }, creation)).toBe(true);
    expect(matchesVmCreation({ vm_id: 'vm-1', name: 'drivé-vm' }, creation)).toBe(true);
    expect(matchesVmCreation({ vm_id: 'vm-1', name: 'drive-vm-2' }, creation)).toBe(false);
    // 요청 전부터 있던 VM 은 여전히 제외
    expect(
      matchesVmCreation({ vm_id: 'vm-1', name: 'drive-vm' }, { ...creation, excludedVmIds: ['vm-1'] }),
    ).toBe(false);
  });
});

describe('이 탭이 보내지 않은 생성 기록 (S15P31E110-473)', () => {
  const orphan = { name: 'from-other-tab', startedAt: Date.now() };

  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  it('새로고침 전(저장소에만 있던) 기록은 이 탭 소유가 아니라 카드를 잠그지 않고, 기록을 지울 수 있다', async () => {
    localStorage.setItem(PENDING_KEY, JSON.stringify([orphan]));
    vi.resetModules();
    const progress = await import('./vmCreationProgress');
    const { result } = renderHook(() => progress.usePendingVmCreations());
    expect(result.current).toEqual([orphan]);
    expect(progress.isOwnedVmCreation(result.current[0]!)).toBe(false);
    expect(
      progress.isVmCreationPending(
        { vm_id: 'vm-1', name: 'from-other-tab', state: 'stopped' },
        result.current,
      ),
    ).toBe(false);

    act(() => progress.clearVmCreation(result.current[0]!));
    expect(result.current).toEqual([]);
    expect(localStorage.getItem(PENDING_KEY)).toBe('[]');
  });

  it('이 탭에서 시작한 기록은 소유이고 카드를 잠근다', async () => {
    vi.resetModules();
    const progress = await import('./vmCreationProgress');
    progress.startVmCreation('mine', Date.now());
    const { result } = renderHook(() => progress.usePendingVmCreations());
    expect(progress.isOwnedVmCreation(result.current[0]!)).toBe(true);
    expect(
      progress.isVmCreationPending({ vm_id: 'vm-1', name: 'mine', state: 'stopped' }, result.current),
    ).toBe(true);
    progress.finishVmCreation('mine');
  });
});
