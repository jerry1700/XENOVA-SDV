import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { SocketState } from '../api/drive';
import { DriveSocketAlert } from './DriveSocketAlert';

const drive = vi.hoisted(() => ({ socket: 'open' as SocketState }));
vi.mock('./DriveContext', () => ({ useDrive: () => ({ drive: null, socket: drive.socket }) }));

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
