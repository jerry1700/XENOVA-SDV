import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { presentationFixture } from './fixture';
import { Notices } from './Notices';
import styles from './notices.module.css';

describe('알림 배지 (모바일 화면 Notices)', () => {
  it('긴 decision 문구도 감싸개 안에서 줄바꿈하고 전체 문구는 title 로 둔다', () => {
    const p = presentationFixture();
    const vm = p.state.vms[0]!;
    const long = '앱 오류 '.repeat(60).trim();
    p.decisions[vm.vm_id] = { kind: 'operation', label: long, tone: 'danger' };
    render(<Notices p={p} connected />);
    const item = screen.getByTestId('notice');
    expect(item).toHaveAttribute('title', `VM1 · ${long}`);
    expect(item).toHaveClass(styles.item!);
    // 공용 배지(role=status)는 감싸개 안에 그대로, 문구도 잘리지 않고 다 들어 있다
    expect(item.querySelector('[role="status"]')).toHaveTextContent(`VM1 · ${long}`);
  });

  it('연결이 끊기면 배지 대신 "알림 연결 미확인"', () => {
    render(<Notices p={presentationFixture()} connected={false} />);
    expect(screen.queryByTestId('notice')).toBeNull();
    expect(screen.getByText('알림 연결 미확인')).toBeInTheDocument();
  });
});
