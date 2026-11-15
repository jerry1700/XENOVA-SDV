import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Vm } from '../api/types';
import { ExecutionTable } from './VmDetailTelemetry';

const threads = vi.hoisted(() => ({
  items: [] as { thread_id: string; thread_name: string; cpu_percent: number | null }[],
}));

vi.mock('../api/queries', () => ({
  useThreads: () => ({
    data: {
      vm_id: 'vm-z',
      window_ms: 1000,
      received_at: new Date().toISOString(),
      stale: false,
      threads_status: 'ok',
      parts_received: 1,
      part_count: 1,
      complete: true,
      conflicting_pages: false,
      items: threads.items,
    },
    error: null,
    isPending: false,
  }),
  useProcesses: () => ({ data: undefined, error: null, isPending: true }),
  useMetrics: () => ({ data: undefined, error: null, isPending: true }),
}));

const zephyr = { vm_id: 'vm-z', name: 'autonomous', guest_kind: 'zephyr', state: 'running' } as Vm;

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Zephyr 스레드 표 key (S15P31E110-469)', () => {
  it('같은 thread_id 가 두 번 와도 두 행 모두 그리고 key 중복 경고가 없다', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    threads.items = [
      { thread_id: '0x20001000', thread_name: 'drive', cpu_percent: 40 },
      { thread_id: '0x20001000', thread_name: 'drive-restarted', cpu_percent: 10 },
    ];
    render(<ExecutionTable vm={zephyr} />);
    expect(screen.getByText('drive')).toBeInTheDocument();
    expect(screen.getByText('drive-restarted')).toBeInTheDocument();
    expect(screen.getAllByText('0x20001000')).toHaveLength(2);
    const keyWarnings = consoleError.mock.calls.filter((args) => String(args[0]).includes('same key'));
    expect(keyWarnings).toEqual([]);
  });
});
