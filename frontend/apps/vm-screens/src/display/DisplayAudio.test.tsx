import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DisplayAudioControl, useDisplayAudio } from './DisplayAudio';
import { MUSIC } from './music';

class AudioFake extends EventTarget {
  static latest: AudioFake | null = null;
  static rejectPlay = false;
  readonly src: string;
  loop = false;
  preload = '';
  volume = 1;
  paused = true;
  currentTime = 0;
  duration = Number.NaN;
  load = vi.fn();
  play = vi.fn(async () => {
    if (AudioFake.rejectPlay) throw new DOMException('not allowed', 'NotAllowedError');
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

describe('DisplayAudio (운전자 화면 음악 카드)', () => {
  beforeEach(() => {
    AudioFake.latest = null;
    AudioFake.rejectPlay = false;
    vi.stubGlobal('Audio', AudioFake);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('music.ts 의 곡을 반복 재생으로 준비하고, 버튼을 누르기 전에는 재생하지 않는다', () => {
    render(<Player />);
    const audio = AudioFake.latest!;

    expect(audio.src).toBe(MUSIC.src);
    expect(MUSIC.src).toMatch(/interaction-theme.*\.mp3$/);
    expect(audio.src).not.toMatch(/^\/audio\//);
    expect(audio.loop).toBe(true);
    expect(audio.load).toHaveBeenCalledOnce();
    expect(audio.play).not.toHaveBeenCalled();

    expect(screen.getByTestId('music-title')).toHaveTextContent('Interaction Theme');
    expect(screen.getByTestId('music-artist')).toHaveTextContent('Xenova');
    act(() => audio.dispatchEvent(new Event('canplay')));
    expect(screen.getByTestId('music-state')).toHaveTextContent('재생 대기');
  });

  it('재생 버튼으로 재생·일시정지한다', async () => {
    render(<Player />);
    const audio = AudioFake.latest!;

    await act(async () => screen.getByRole('button', { name: '음악 재생' }).click());
    expect(audio.play).toHaveBeenCalledOnce();
    expect(screen.getByTestId('music-state')).toHaveTextContent('재생 중');

    act(() => screen.getByRole('button', { name: '음악 일시정지' }).click());
    expect(audio.pause).toHaveBeenCalledOnce();
    expect(screen.getByTestId('music-state')).toHaveTextContent('일시정지');
    expect(screen.getByRole('button', { name: '음악 재생' })).toBeInTheDocument();
  });

  it('파형 진행 바는 진행된 막대만 켜고 현재 시각/길이를 보인다 (보기 전용)', () => {
    render(<Player />);
    const audio = AudioFake.latest!;
    expect(screen.getByTestId('music-time-now')).toHaveTextContent('0:00');
    expect(screen.getByTestId('music-time-total')).toHaveTextContent('-:--');

    audio.duration = 160;
    audio.currentTime = 65;
    act(() => {
      audio.dispatchEvent(new Event('durationchange'));
      audio.dispatchEvent(new Event('timeupdate'));
    });
    expect(screen.getByTestId('music-time-now')).toHaveTextContent('1:05');
    expect(screen.getByTestId('music-time-total')).toHaveTextContent('2:40');
    const wave = screen.getByRole('progressbar', { name: '재생 위치' });
    expect(wave).toHaveAttribute('aria-valuenow', '65');
    expect(wave).toHaveAttribute('aria-valuemax', '160');
    expect(wave).toHaveAttribute('aria-valuetext', '1:05 / 2:40');
    const bars = wave.querySelectorAll('i');
    const on = wave.querySelectorAll('i[data-on="true"]');
    // 65 / 160 ≈ 40.6% → 36개 중 앞 15개
    expect(bars).toHaveLength(36);
    expect(on).toHaveLength(15);
    expect(bars[0]).toHaveAttribute('data-on', 'true');
    expect(bars[35]).toHaveAttribute('data-on', 'false');
  });

  it('VM 문구와 음량 슬라이더가 없다', () => {
    render(<Player />);
    expect(screen.getByTestId('display-audio').textContent).not.toMatch(/VM|인포테인먼트|음량/);
    expect(screen.queryByRole('slider')).toBeNull();
  });

  it('브라우저가 재생을 막으면 버튼 안내, 파일 오류면 버튼을 막는다', async () => {
    AudioFake.rejectPlay = true;
    render(<Player />);
    const audio = AudioFake.latest!;

    await act(async () => screen.getByRole('button', { name: '음악 재생' }).click());
    expect(screen.getByTestId('music-state')).toHaveTextContent('재생 버튼을 눌러 시작');

    act(() => audio.dispatchEvent(new Event('error')));
    expect(screen.getByTestId('music-state')).toHaveTextContent('음원 파일 확인 필요');
    expect(screen.getByRole('button', { name: '음악 재생' })).toBeDisabled();
  });
});
