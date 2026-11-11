import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DisplayAudioControl, useDisplayAudio } from './DisplayAudio';

class AudioFake extends EventTarget {
  static latest: AudioFake | null = null;
  readonly src: string;
  loop = false;
  preload = '';
  volume = 1;
  paused = true;
  load = vi.fn();
  play = vi.fn(async () => {
    this.paused = false;
    this.dispatchEvent(new Event('playing'));
  });
  pause = vi.fn(() => {
    this.paused = true;
    this.dispatchEvent(new Event('pause'));
  });

  constructor(src: string) {
    super();
    this.src = src;
    AudioFake.latest = this;
  }
}

function Player() {
  return <DisplayAudioControl controller={useDisplayAudio()} />;
}

describe('DisplayAudio', () => {
  beforeEach(() => {
    AudioFake.latest = null;
    vi.stubGlobal('Audio', AudioFake);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('기본 음원을 준비하고 재생과 일시정지를 제어한다', async () => {
    render(<Player />);
    const audio = AudioFake.latest!;

    // 빌드 결과 /assets/ 아래로 나가도록 import한 파일을 써야 한다(public/ 고정 경로 금지).
    expect(audio.src).toMatch(/interaction-theme(-[\w-]+)?\.mp3$/);
    expect(audio.src).not.toMatch(/^\/audio\//);
    expect(audio.loop).toBe(true);
    expect(audio.volume).toBe(0.55);
    expect(audio.load).toHaveBeenCalledOnce();

    act(() => audio.dispatchEvent(new Event('canplay')));
    expect(screen.getByText('재생 대기')).toBeInTheDocument();

    await act(async () => screen.getByRole('button', { name: '음악 재생' }).click());
    expect(audio.play).toHaveBeenCalledOnce();
    expect(screen.getByText('차량 테마 재생 중')).toBeInTheDocument();

    act(() => screen.getByRole('button', { name: '음악 일시정지' }).click());
    expect(audio.pause).toHaveBeenCalledOnce();
    expect(screen.getByText('재생 대기')).toBeInTheDocument();
  });

  it('음량을 적용하고 파일 오류 시 재생을 막는다', () => {
    render(<Player />);
    const audio = AudioFake.latest!;

    fireEvent.change(screen.getByRole('slider', { name: '음악 음량' }), {
      target: { value: '0.8' },
    });
    expect(audio.volume).toBe(0.8);

    act(() => audio.dispatchEvent(new Event('error')));
    expect(screen.getByText('음원 파일 확인 필요')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '음악 재생' })).toBeDisabled();
  });
});
