import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider, useToast } from './Toast';

type Call = [text: string, tone?: 'info' | 'fault', durationMs?: number];

/** 버튼을 누르면 calls 를 차례로 show 한다 */
function Show({ calls }: { calls: Call[] }) {
  const { show } = useToast();
  return (
    <button type="button" onClick={() => calls.forEach(([text, tone, ms]) => show(text, tone, ms))}>
      보이기
    </button>
  );
}

function showAll(calls: Call[]) {
  render(
    <ToastProvider>
      <Show calls={calls} />
    </ToastProvider>,
  );
  act(() => screen.getByRole('button', { name: '보이기' }).click());
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('Toast', () => {
  it('한 번에 3개까지 보이고, 넘치면 오래된 것부터 정리한다', () => {
    showAll([['첫째'], ['둘째'], ['셋째'], ['넷째']]);
    expect(screen.queryByText('첫째')).toBeNull();
    expect(screen.getByText('둘째')).toBeInTheDocument();
    expect(screen.getByText('넷째')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '알림 닫기' })).toHaveLength(3);
  });

  it('닫기 버튼으로 그 토스트만 닫고, 자동으로 사라지는 시간은 그대로', () => {
    showAll([
      ['남는 알림', 'info', 5000],
      ['닫을 알림', 'fault'],
    ]);
    const close = screen.getAllByRole('button', { name: '알림 닫기' })[1]!;
    act(() => close.click());
    expect(screen.queryByText('닫을 알림')).toBeNull();
    expect(screen.getByText('남는 알림')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(4999));
    expect(screen.getByText('남는 알림')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByText('남는 알림')).toBeNull();
  });

  it('긴 문구는 닫기 버튼과 따로, 줄바꿈되는 글 칸 안에 들어간다', () => {
    const long = 'E'.repeat(2000);
    showAll([[long, 'fault']]);
    const text = screen.getByText(long);
    expect(text.tagName).toBe('DIV');
    expect(text.parentElement?.querySelector('button')).toHaveAccessibleName('알림 닫기');
  });
});
