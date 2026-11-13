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
