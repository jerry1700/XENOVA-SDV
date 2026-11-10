import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { VmEvent } from '../api/types';
import { DetailEvents } from './VmDetailEvents';
import { shortClock } from './vmDetailEventFormat';

const at = '2026-11-10T01:02:03.456Z';
const event = (
  type: VmEvent['type'],
  severity: VmEvent['severity'],
  payload: Record<string, unknown>,
  id: string,
): VmEvent => ({
  event_id: id,
  type,
  severity,
  vm_id: 'vm-1',
  operation_id: null,
  occurred_at: at,
  recorded_at: at,
  payload,
});

describe('VM 상세 이벤트 한 줄 표시', () => {
  it('shows time without milliseconds, a severity dot, a kind label and Korean state text without the VM name', () => {
    render(
      <DetailEvents
        pending={false}
        error={false}
        events={[
          event('vm.state.changed', 'INFO', { from: 'paused', to: 'running' }, 'a'),
          event('recovery.started', 'WARNING', { reason: 'DOMAIN_ABSENT' }, 'b'),
          event('recovery.failed', 'ERROR', { error: { code: 'XEN_TIMEOUT' } }, 'c'),
          event('agent.status.changed', 'WARNING', { from: 'ONLINE', to: 'STALE' }, 'd'),
          event('drive.fault.detected', 'WARNING', { reason: 'command_timeout' }, 'e'),
          event('demo.command.sent', 'INFO', { command: 'force_kill' }, 'f'),
        ]}
      />,
    );
    const time = screen.getAllByText(shortClock(at))[0]!;
    expect(time.textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(time).toHaveAttribute('title');

    expect(screen.getByText('일시정지 → 실행 중')).toBeInTheDocument();
    expect(screen.getByText('VM 상태')).toBeInTheDocument();
    expect(screen.queryByText('도메인 상태')).not.toBeInTheDocument();
    expect(screen.queryByText('복구 서비스')).not.toBeInTheDocument();
    expect(screen.queryByText(/INFO|WARNING|ERROR/)).not.toBeInTheDocument();

    // 사유 코드는 아는 것만 한글, 모르는 것은 원문 고정폭
    expect(screen.getByText('도메인 없음')).toBeInTheDocument();
    expect(screen.queryByText('DOMAIN_ABSENT')).not.toBeInTheDocument();
    expect(screen.getByText('XEN_TIMEOUT').tagName).toBe('CODE');
    expect(screen.getByText('연결됨 → 응답 늦음')).toBeInTheDocument();

    // 심각도는 색 점 + aria-label
    expect(screen.getAllByRole('img', { name: '심각도 정보' })).toHaveLength(2);
    // 종류 라벨과 같은 문구는 내용에서 반복하지 않는다
    expect(screen.getByText('장애 감지')).toBeInTheDocument();
    expect(screen.getByText('명령 끊김')).toBeInTheDocument();
    expect(screen.getByText('강제 종료')).toBeInTheDocument();
    expect(screen.queryByText('운영자 시연 조작')).not.toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: '심각도 경고' })).toHaveLength(3);
    expect(screen.getByRole('img', { name: '심각도 오류' })).toBeInTheDocument();
    const row = screen.getByText('복구 실패').closest('div')!;
    expect(within(row).getByText('자동 복구')).toHaveAttribute('data-tone', 'error');
  });

  it('keeps the empty and error messages', () => {
    const { rerender } = render(<DetailEvents pending={false} error={false} events={[]} />);
    expect(screen.getByText('최근 lifecycle·장애·복구 이벤트가 없습니다.')).toBeInTheDocument();
    rerender(<DetailEvents pending={false} error events={[]} />);
    expect(screen.getByText('이벤트를 불러오지 못했습니다.')).toBeInTheDocument();
  });
});
