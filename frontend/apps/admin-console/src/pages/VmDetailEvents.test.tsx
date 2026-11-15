import { render, screen } from '@testing-library/react';
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
  it('shows time, warning/error as small colored words, Korean text only, kind and notes in the title', () => {
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
    expect(screen.queryByText('도메인 상태')).not.toBeInTheDocument();
    expect(screen.queryByText('복구 서비스')).not.toBeInTheDocument();
    expect(screen.queryByText(/INFO|WARNING|ERROR/)).not.toBeInTheDocument();
    expect(screen.getByText('연결됨 → 응답 늦음')).toBeInTheDocument();
    expect(screen.getByText('명령 끊김')).toBeInTheDocument();
    expect(screen.getByText('강제 종료')).toBeInTheDocument();

    // 종류 라벨·보조 글(사유)·원문 코드는 줄에서 빼고 행 title 툴팁으로
    for (const hidden of [
      'VM 상태',
      '자동 복구',
      '장애 감지',
      '도메인 없음',
      'XEN_TIMEOUT',
      '운영자 시연 조작',
    ])
      expect(screen.queryByText(hidden)).not.toBeInTheDocument();
    const rowOf = (text: string) => screen.getByText(text).closest('[title]:not(time)')!;
    expect(rowOf('일시정지 → 실행 중')).toHaveAttribute('title', expect.stringContaining('VM 상태'));
    expect(rowOf('재시작')).toHaveAttribute(
      'title',
      expect.stringContaining('자동 복구 · 재시작 · 도메인 없음'),
    );
    expect(rowOf('복구 실패')).toHaveAttribute('title', expect.stringContaining('XEN_TIMEOUT'));
    expect(screen.queryByText('DOMAIN_ABSENT')).not.toBeInTheDocument();

    // 상태 칸: 경고·오류만 작은 색 글자, 정보는 빈 칸. aria-label 에는 정보를 포함한 심각도와 종류
    const info = screen.getByRole('img', { name: '심각도 정보 · VM 상태' });
    expect(info).toHaveTextContent(/^$/);
    expect(info.querySelector('i')).toBeNull();
    expect(screen.getByRole('img', { name: '심각도 경고 · 장애 감지' })).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: /^심각도 경고/ })).toHaveLength(3);
    const failed = screen.getByRole('img', { name: '심각도 오류 · 자동 복구' });
    expect(failed).toHaveAttribute('data-tone', 'error');
    expect(failed).toHaveTextContent(/^오류$/);
    expect(screen.getByRole('img', { name: '심각도 경고 · 장애 감지' })).toHaveTextContent(/^경고$/);
    // 맨 위 열 제목 줄 (VM 상세는 대상 열 없음)
    const head = document.querySelector('[data-head]')!;
    expect([...head.children].map((cell) => cell.textContent)).toEqual(['시각', '상태', '내용']);
  });

  it('keeps the empty and error messages', () => {
    const { rerender } = render(<DetailEvents pending={false} error={false} events={[]} />);
    expect(screen.getByText('최근 lifecycle·장애·복구 이벤트가 없습니다.')).toBeInTheDocument();
    rerender(<DetailEvents pending={false} error events={[]} />);
    expect(screen.getByText('이벤트를 불러오지 못했습니다.')).toBeInTheDocument();
  });
});

describe('VM 상세 이벤트 — 읽을 수 없는 시각 (S15P31E110-471)', () => {
  it('시각이 깨진 이벤트도 NaN·Invalid Date 없이 — 와 시각 미상으로 보인다', () => {
    const broken = {
      ...event('vm.state.changed', 'INFO', { from: 'paused', to: 'running' }, 'x'),
      occurred_at: 'bad',
    };
    const { container } = render(<DetailEvents pending={false} error={false} events={[broken]} />);
    expect(container).not.toHaveTextContent(/NaN|Invalid Date/);
    const time = container.querySelector('time')!;
    expect(time).toHaveTextContent('—');
    expect(time).toHaveAttribute('title', '시각 미상');
  });
});
