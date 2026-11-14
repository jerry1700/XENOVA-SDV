import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { DisplayModeName, DisplayModeStatus } from '../api/types';
import { DisplayModeCard } from './DisplayModeCard';

const state = vi.hoisted(() => ({
  status: {} as { data?: DisplayModeStatus; isPending: boolean; isError: boolean; error: unknown },
  mutate: vi.fn<(mode: DisplayModeName) => void>(),
  change: {
    isPending: false,
    isError: false,
    error: null as unknown,
    variables: undefined as DisplayModeName | undefined,
  },
}));

vi.mock('../api/queries', () => ({
  useDisplayMode: () => ({ ...state.status, isFetching: false, refetch: vi.fn() }),
  useSetDisplayMode: () => ({ ...state.change, mutate: state.mutate }),
}));

const hdmi: DisplayModeStatus = {
  mode: 'hdmi',
  boot_default: 'hdmi',
  services: { hdmi: 'active', mobile: 'inactive', feed: 'active' },
  hdmi_renderer_ready: true,
  observed_at: '2026-11-14T09:00:00Z',
};
const mobile: DisplayModeStatus = {
  ...hdmi,
  mode: 'mobile',
  services: { hdmi: 'inactive', mobile: 'active', feed: 'active' },
  hdmi_renderer_ready: false,
};

function show(data: DisplayModeStatus) {
  state.status = { data, isPending: false, isError: false, error: null };
}

describe('차량 화면 출력 카드', () => {
  beforeEach(() => {
    state.mutate.mockReset();
    state.change = { isPending: false, isError: false, error: null, variables: undefined };
    show(hdmi);
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('HDMI 모드에서는 휴대폰 모드 켜기만 누를 수 있다', () => {
    render(<DisplayModeCard />);
    expect(screen.getByText('HDMI 모니터 모드')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '휴대폰 모드 켜기' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'HDMI로 돌아가기' })).toBeDisabled();
  });

  it('휴대폰 모드 켜기는 USB 테더링 안내를 보인 뒤 확인하면 mobile 로 전환한다', async () => {
    const user = userEvent.setup();
    render(<DisplayModeCard />);
    await user.click(screen.getByRole('button', { name: '휴대폰 모드 켜기' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('USB 테더링 켜기');
    expect(state.mutate).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: '휴대폰 모드 켜기' }));
    expect(state.mutate).toHaveBeenCalledWith('mobile');
  });

  it('휴대폰 모드에서는 HDMI로 돌아가기로 hdmi 전환을 보낸다', async () => {
    show(mobile);
    const user = userEvent.setup();
    render(<DisplayModeCard />);
    expect(screen.getByText('휴대폰 모드')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '휴대폰 모드 켜기' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'HDMI로 돌아가기' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'HDMI로 돌아가기' }));
    expect(state.mutate).toHaveBeenCalledWith('hdmi');
  });

  it('전환 중에는 두 버튼을 막고 진행 문구를 보인다', () => {
    state.change = { ...state.change, isPending: true, variables: 'mobile' };
    render(<DisplayModeCard />);
    expect(screen.getByRole('button', { name: '휴대폰 모드 켜기' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'HDMI로 돌아가기' })).toBeDisabled();
    expect(screen.getByText(/휴대폰 모드로 바꾸는 중입니다/)).toBeInTheDocument();
  });

  it('보드가 거절한 사유를 보여 준다', () => {
    state.change = {
      ...state.change,
      isError: true,
      error: new ApiError(409, {
        code: 'PHONE_NOT_CONNECTED',
        message:
          '휴대폰이 USB로 연결되어 있지 않습니다. 케이블을 연결하고 USB 테더링을 켠 뒤 다시 시도해 주세요.',
      }),
    };
    render(<DisplayModeCard />);
    expect(screen.getByRole('alert')).toHaveTextContent('휴대폰이 USB로 연결되어 있지 않습니다');
  });

  it('서비스가 꺼져 있으면 확인 필요 배지를 보인다', () => {
    show({ ...hdmi, services: { ...hdmi.services, hdmi: 'failed' } });
    render(<DisplayModeCard />);
    expect(screen.getByText('HDMI 모니터 모드 · 서비스 확인 필요')).toBeInTheDocument();
  });
});
