import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { SocketState } from '../api/drive';
import { DriveSocketAlert } from './DriveSocketAlert';

const drive = vi.hoisted(() => ({ socket: 'open' as SocketState, invalid: false }));
vi.mock('./DriveContext', () => ({
  useDrive: () => ({ drive: null, socket: drive.socket, invalid: drive.invalid }),
}));

describe('주행 상태 소켓 거부 안내 (S15P31E110-469)', () => {
  it.each(['connecting', 'open', 'closed'] as const)('%s 이면 보이지 않는다', (socket) => {
    drive.socket = socket;
    render(<DriveSocketAlert />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('rejected 이면 "연결이 거부되었습니다"', () => {
    drive.socket = 'rejected';
    render(<DriveSocketAlert />);
    expect(screen.getByRole('alert')).toHaveTextContent('주행 상태 연결이 거부되었습니다');
  });
});

describe('주행 상태 형식 오류 안내 (S15P31E110-472)', () => {
  it('형식이 틀린 프레임을 받았으면 "주행 상태를 읽지 못했습니다" 하나만, 정상으로 돌아오면 내린다', () => {
    drive.socket = 'open';
    drive.invalid = true;
    const view = render(<DriveSocketAlert />);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByRole('alert')).toHaveTextContent('주행 상태를 읽지 못했습니다');
    drive.invalid = false;
    view.rerender(<DriveSocketAlert />);
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
