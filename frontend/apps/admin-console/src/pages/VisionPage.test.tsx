import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { VisionPage } from './VisionPage';
import { FRAME_INTERVAL_MS, RETRY_INTERVAL_MS } from './useVisionStream';

const auth = vi.hoisted(() => ({ getSession: vi.fn(), refreshSession: vi.fn() }));
vi.mock('../api/client', () => auth);

const jpeg = () =>
  new Response(new Blob([new Uint8Array([0xff, 0xd8, 1])], { type: 'image/jpeg' }), {
    status: 200,
    headers: { 'Content-Type': 'image/jpeg' },
  });
const error = (code: string, status = 503) =>
  new Response(JSON.stringify({ code }), { status, headers: { 'Content-Type': 'application/json' } });

describe('Vision page camera', () => {
  const fetchMock = vi.fn();
  let urls = 0;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
    urls = 0;
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => `blob:frame-${++urls}`),
      revokeObjectURL: vi.fn(),
    });
    auth.getSession.mockReturnValue({ access_token: 'jwt', expires_at: '2099-01-01T00:00:00Z' });
    auth.refreshSession.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('shows frames from the backend with the login token and keeps refreshing them', async () => {
    fetchMock.mockImplementation(async () => jpeg());
    const { unmount } = render(<VisionPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const img = screen.getByRole('img', { name: /Jetson 전방 카메라/ });
    expect(img).toHaveAttribute('src', 'blob:frame-1');
    expect(screen.getByText('영상 수신 중')).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/v1/vision/snapshot');
    expect((init as RequestInit).headers).toMatchObject({ Authorization: 'Bearer jwt' });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRAME_INTERVAL_MS);
    });
    expect(img).toHaveAttribute('src', 'blob:frame-2');
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:frame-1');
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:frame-2');
  });

  it('explains a missing camera address and retries slowly', async () => {
    fetchMock.mockImplementation(async () => error('VISION_NOT_CONFIGURED'));
    render(<VisionPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText(/JETSON_VIDEO_URL/)).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RETRY_INTERVAL_MS - 1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('renews an expired login once before giving up', async () => {
    fetchMock.mockResolvedValueOnce(error('UNAUTHORIZED', 401)).mockResolvedValueOnce(jpeg());
    auth.refreshSession.mockResolvedValue(true);
    render(<VisionPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(auth.refreshSession).toHaveBeenCalledOnce();
    expect(screen.getByRole('img')).toBeInTheDocument();
  });

  it('pauses while the tab is hidden', async () => {
    fetchMock.mockImplementation(async () => jpeg());
    render(<VisionPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRAME_INTERVAL_MS * 10);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
