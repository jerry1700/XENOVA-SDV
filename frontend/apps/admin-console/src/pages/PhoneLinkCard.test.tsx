import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import type { PhoneLinkStatus, PhonePairingCode } from '../api/types';
import { PhoneLinkCard } from './PhoneLinkCard';
import { phoneLinkHint } from './phoneLinkHint';

const state = vi.hoisted(() => ({
  status: {} as { data?: PhoneLinkStatus; isPending: boolean; isError: boolean; error: unknown },
  mutateAsync: vi.fn<() => Promise<PhonePairingCode>>(),
  issueError: null as unknown,
}));

vi.mock('../api/queries', () => ({
  usePhoneLink: () => ({ ...state.status, isFetching: false, refetch: vi.fn() }),
  useIssuePhonePairingCode: () => ({
    mutateAsync: state.mutateAsync,
    isPending: false,
    isError: state.issueError !== null,
    error: state.issueError,
  }),
}));

const listening: PhoneLinkStatus = {
  configured: true,
  relay: 'listening',
  url: 'http://10.117.188.25:8084/display/mobile',
  usb: { interface: 'usb0', present: true, ipv4: ['10.117.188.25/24'], gateway: '10.117.188.86' },
  configured_phone_ipv4: '10.117.188.86',
  subnet_mismatch: false,
  pairing: { active: false, expires_at: null },
  observed_at: '2026-11-12T08:29:10Z',
};

function show(data: PhoneLinkStatus) {
  state.status = { data, isPending: false, isError: false, error: null };
}

describe('휴대폰 화면 연결 카드', () => {
  beforeEach(() => {
    state.mutateAsync.mockReset();
    state.issueError = null;
    show(listening);
    // jsdom 에는 <dialog> 메서드가 없다
    HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
      this.open = true;
    };
    HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
      this.open = false;
    };
  });

  it('접속 주소와 복사 버튼, 보드·휴대폰 주소를 보여 준다', () => {
    render(<PhoneLinkCard />);
    expect(screen.getByText('휴대폰 접속 가능')).toBeInTheDocument();
    expect(screen.getByText(listening.url!)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '주소 복사' })).toBeInTheDocument();
    expect(screen.getByText('10.117.188.25/24')).toBeInTheDocument();
    expect(screen.getByText('10.117.188.86')).toBeInTheDocument();
  });

  it('확인을 거쳐 연결번호를 발급하고 번호와 남은 시간을 보여 준다', async () => {
    state.mutateAsync.mockResolvedValue({
      code: '123456',
      expires_at: new Date(Date.now() + 120_000).toISOString(),
    });
    render(<PhoneLinkCard />);
    await userEvent.click(screen.getByRole('button', { name: '연결번호 발급' }));
    expect(screen.getByText(/지금 연결된 휴대폰은 끊어지고/)).toBeInTheDocument();
    expect(state.mutateAsync).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '발급' }));
    expect(state.mutateAsync).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('123456')).toBeInTheDocument();
    expect(screen.getByText(/[12]:\d\d 남음/)).toBeInTheDocument();
  });

  it('만료된 번호는 보여 주지 않는다', async () => {
    state.mutateAsync.mockResolvedValue({
      code: '654321',
      expires_at: new Date(Date.now() - 1000).toISOString(),
    });
    render(<PhoneLinkCard />);
    await userEvent.click(screen.getByRole('button', { name: '연결번호 발급' }));
    await userEvent.click(screen.getByRole('button', { name: '발급' }));
    await waitFor(() => expect(state.mutateAsync).toHaveBeenCalled());
    expect(screen.queryByText('654321')).not.toBeInTheDocument();
  });

  it('보드가 이 번호를 보인 뒤 사라지면(휴대폰 연결 성공) 번호를 지운다', async () => {
    const expires = new Date(Date.now() + 120_000).toISOString();
    state.mutateAsync.mockResolvedValue({ code: '111222', expires_at: expires });
    const { rerender } = render(<PhoneLinkCard />);
    await userEvent.click(screen.getByRole('button', { name: '연결번호 발급' }));
    await userEvent.click(screen.getByRole('button', { name: '발급' }));
    expect(await screen.findByText('111222')).toBeInTheDocument();

    show({ ...listening, pairing: { active: true, expires_at: expires } });
    rerender(<PhoneLinkCard />);
    expect(screen.getByText('111222')).toBeInTheDocument();

    show({ ...listening, pairing: { active: false, expires_at: null } });
    rerender(<PhoneLinkCard />);
    await waitFor(() => expect(screen.queryByText('111222')).not.toBeInTheDocument());
  });

  it('중계가 꺼져 있으면 발급 버튼을 막고 할 일을 안내한다', () => {
    show({ ...listening, relay: 'stopped', url: null });
    render(<PhoneLinkCard />);
    expect(screen.getByRole('button', { name: '연결번호 발급' })).toBeDisabled();
    expect(screen.getByRole('note')).toHaveTextContent('휴대폰 모드 켜기');
    expect(screen.queryByRole('button', { name: '주소 복사' })).not.toBeInTheDocument();
  });

  it('발급 실패와 조회 실패 메시지를 보여 준다', () => {
    state.issueError = new ApiError(409, {
      code: 'PHONE_RELAY_NOT_LISTENING',
      message: '휴대폰 화면 중계가 꺼져 있거나 휴대폰이 연결되지 않았습니다.',
    });
    const { unmount } = render(<PhoneLinkCard />);
    expect(screen.getByRole('alert')).toHaveTextContent('휴대폰 화면 중계가 꺼져 있거나');
    unmount();

    state.status = {
      isPending: false,
      isError: true,
      error: new ApiError(503, {
        code: 'PHONE_LINK_UNAVAILABLE',
        message: '보드의 휴대폰 화면 중계 상태를 확인할 수 없습니다.',
      }),
    };
    render(<PhoneLinkCard />);
    expect(screen.getByRole('alert')).toHaveTextContent('보드의 휴대폰 화면 중계 상태를 확인할 수 없습니다.');
    expect(screen.getByRole('alert')).not.toHaveTextContent('PHONE_LINK_UNAVAILABLE');
  });
});

describe('휴대폰 연결 안내 문구', () => {
  it('상태별로 할 일을 고른다', () => {
    expect(phoneLinkHint(listening)).toBeNull();
    expect(phoneLinkHint({ ...listening, configured: false })).toContain('설정');
    expect(
      phoneLinkHint({ ...listening, subnet_mismatch: true, usb: { ...listening.usb, gateway: '10.55.3.1' } }),
    ).toContain('10.55.3.1');
    expect(
      phoneLinkHint({ ...listening, relay: 'waiting_link', usb: { ...listening.usb, present: false } }),
    ).toContain('USB 케이블');
    expect(phoneLinkHint({ ...listening, relay: 'waiting_link' })).toContain('테더링을 껐다 켜');
  });

  it('모르는 중계 상태값이어도 카드 본문을 보이고 배지는 중립 "상태 알 수 없음" (S15P31E110-473)', () => {
    show({ ...listening, relay: 'rebooting' as PhoneLinkStatus['relay'] });
    render(<PhoneLinkCard />);
    const badge = screen.getByText('상태 알 수 없음').closest('[role="status"]')!;
    // 장애(빨강)가 아닌 중립(회색) 배지 — StatusBadge 의 health 클래스
    expect(badge.className).toMatch(/stopped/);
    expect(badge.className).not.toMatch(/fault/);
    // 본문(주소·복사·보드 주소)이 비지 않는다
    expect(screen.getByText(listening.url!)).toBeInTheDocument();
    expect(screen.getByText('10.117.188.25/24')).toBeInTheDocument();
  });
});
