import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { VmEvent } from '../api/types';
import { EventRow } from './EventRow';

const at = '2026-11-10T01:02:03.456Z';
const failed: VmEvent = {
  event_id: 'e1',
  type: 'recovery.failed',
  severity: 'ERROR',
  vm_id: 'vm-1',
  operation_id: null,
  occurred_at: at,
  recorded_at: at,
  payload: { error: { code: 'XEN_TIMEOUT' } },
};

describe('이벤트 행 펼치기 (터치 기기에서 전체 내용 보기)', () => {
  it('기본은 한 줄, 행을 누르면 그 자리에서 펼쳐 설명을 보이고 다시 누르면 접는다', async () => {
    const user = userEvent.setup();
    const { container } = render(<EventRow event={failed} time="10:02:03" />);
    const row = container.firstElementChild as HTMLElement;
    const toggle = screen.getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(row).not.toHaveAttribute('data-open');
    expect(screen.queryByText(/XEN_TIMEOUT/)).toBeNull();

    await user.click(screen.getByText('10:02:03')); // 행의 다른 칸을 눌러도 펼친다
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(row).toHaveAttribute('data-open', 'true');
    expect(screen.getByText(/XEN_TIMEOUT/)).toBeInTheDocument();

    await user.click(toggle); // 내용 칸을 눌러도 한 번만 바뀐다
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText(/XEN_TIMEOUT/)).toBeNull();
  });

  it('키보드: 내용 버튼에서 Enter·Space 로 펼치고 접는다', async () => {
    const user = userEvent.setup();
    render(<EventRow event={failed} time="10:02:03" />);
    const toggle = screen.getByRole('button');
    await user.tab();
    expect(toggle).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard(' ');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('모르는 이벤트 종류 (S15P31E110-471)', () => {
  it('화면에는 "기타 이벤트", 원문 type 은 행 title 에만 있다 (펼쳐도 보이지 않는다)', async () => {
    const user = userEvent.setup();
    const unknown = { ...failed, type: 'vm.migrated', severity: 'INFO', payload: {} } as unknown as VmEvent;
    const { container } = render(<EventRow event={unknown} time="10:02:03" />);
    const row = container.firstElementChild as HTMLElement;
    expect(screen.getByRole('button')).toHaveTextContent('기타 이벤트');
    expect(row).toHaveAttribute('title', expect.stringContaining('원문 vm.migrated'));
    await user.click(screen.getByRole('button'));
    expect(row).not.toHaveTextContent('vm.migrated');
  });

  it('VM 생성 이벤트는 원문 vm.created 대신 한글로 보인다', () => {
    const created = { ...failed, type: 'vm.created', severity: 'INFO', payload: {} } as VmEvent;
    const { container } = render(<EventRow event={created} time="10:02:03" />);
    const row = container.firstElementChild as HTMLElement;
    expect(row).toHaveTextContent('생성');
    expect(row).not.toHaveTextContent('vm.created');
    expect(row.getAttribute('title')).not.toContain('vm.created');
  });
});

describe('읽을 수 없는 시각 (S15P31E110-471)', () => {
  it('시각 title 에 Invalid Date 대신 "시각 미상"', () => {
    const broken = { ...failed, occurred_at: 'not-a-date' } as VmEvent;
    const { container } = render(<EventRow event={broken} time="—" />);
    const time = container.querySelector('time')!;
    expect(time).toHaveAttribute('title', '시각 미상');
    expect(container).not.toHaveTextContent(/Invalid Date|NaN/);
  });
});
