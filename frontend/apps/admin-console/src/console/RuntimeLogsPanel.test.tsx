import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RuntimeLogsPanel } from './RuntimeLogsPanel';
import { fetchLogs, fetchLogSources, type LogPage } from '../api/logs';

vi.mock('../api/logs', async (original) => ({
  ...(await original<typeof import('../api/logs')>()),
  fetchLogs: vi.fn(),
  fetchLogSources: vi.fn(),
}));
function page(message = '<script>plain log</script>', epoch = 'one'): LogPage {
  return {
    epoch,
    next_cursor: 1,
    has_more: false,
    history_truncated: false,
    items: [
      {
        id: 1,
        event_id: 'one',
        source_id: 'dom0',
        vm_id: null,
        component: 'api.service',
        boot_id: 'boot',
        observed_at: '2026-10-28T00:00:00Z',
        received_at: 1,
        level: 'INFO',
        message,
      },
    ],
  };
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(fetchLogs).mockResolvedValue(page());
  vi.mocked(fetchLogSources).mockResolvedValue({ epoch: 'one', items: [] });
});
afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe('execution logs beside the terminal', () => {
  it('renders plain text and retains records while paused', async () => {
    await act(async () => {
      render(<RuntimeLogsPanel />);
    });
    expect(screen.getByLabelText('수집된 로그')).toHaveTextContent('<script>plain log</script>');
    expect(document.querySelector('script')).toBeNull();
    fireEvent.click(screen.getByLabelText('일시정지'));
    expect(screen.getByRole('status')).toHaveTextContent('일시정지');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(fetchLogs).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('수집된 로그')).toHaveTextContent('plain log');
  });
  it('marks existing records as old when polling fails', async () => {
    await act(async () => {
      render(<RuntimeLogsPanel />);
    });
    vi.mocked(fetchLogs).mockRejectedValue(new Error('offline'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByRole('status')).toHaveTextContent('연결 끊김 · 마지막 기록 유지');
    // 오류 메시지는 상태 문구와 섞지 않고 툴팁과 보조 줄로
    expect(screen.getByRole('status')).not.toHaveTextContent('offline');
    expect(screen.getByRole('status')).toHaveAttribute('title', 'offline');
    expect(screen.getByText('오류: offline')).toBeInTheDocument();
    expect(screen.getByLabelText('수집된 로그')).toHaveTextContent('plain log');
  });
  it('shows each record in fixed columns and the collection sources as chips', async () => {
    vi.mocked(fetchLogSources).mockResolvedValue({
      epoch: 'one',
      items: [
        { source_id: 'dom0', vm_id: null, components: ['api.service'], state: 'connected' },
        { source_id: 'vm-agent', vm_id: 'vm-3', components: [], state: 'disconnected' },
      ],
    });
    await act(async () => {
      render(<RuntimeLogsPanel />);
    });
    expect(screen.getByRole('status')).toHaveTextContent('수신 중 · 1초 간격');
    const row = screen.getByLabelText('수집된 로그').querySelector('li')!;
    const cells = [...row.children].map((cell) => cell.textContent);
    expect(cells).toHaveLength(5);
    expect(cells[0]).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(cells.slice(1)).toEqual(['INFO', 'dom0/api.service', '—', '<script>plain log</script>']);
    expect(row.children[1]).toHaveAttribute('data-tone', 'ok');
    const chips = screen.getByRole('list', { name: '수집 소스' }).querySelectorAll('li');
    expect([...chips].map((chip) => [chip.textContent, chip.getAttribute('data-tone')])).toEqual([
      ['dom0', 'ok'],
      ['vm-agent', 'muted'],
    ]);
  });
  it('resets the cursor after a replaced log store', async () => {
    await act(async () => {
      render(<RuntimeLogsPanel />);
    });
    vi.mocked(fetchLogs).mockResolvedValue(page('new history', 'two'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByRole('alert')).toHaveTextContent('기록 저장소 변경');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(vi.mocked(fetchLogs).mock.lastCall?.[0].after).toBeUndefined();
    expect(screen.getByLabelText('수집된 로그')).toHaveTextContent('new history');
  });
});
