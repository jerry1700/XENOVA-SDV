import { useEffect, useRef, useState } from 'react';
import styles from './displayAudio.module.css';

const DEFAULT_AUDIO_SRC = '/audio/interaction-theme.mp3';

type PlaybackState = 'loading' | 'ready' | 'playing' | 'paused' | 'blocked' | 'unavailable';

export interface DisplayAudioController {
  state: PlaybackState;
  volume: number;
  toggle: () => Promise<void>;
  setVolume: (volume: number) => void;
}

const labelOf = (state: PlaybackState) => {
  switch (state) {
    case 'loading':
      return '음원 준비 중';
    case 'ready':
    case 'paused':
      return '재생 대기';
    case 'playing':
      return '차량 테마 재생 중';
    case 'blocked':
      return '재생 버튼을 눌러 시작';
    case 'unavailable':
      return '음원 파일 확인 필요';
  }
};

export function useDisplayAudio(): DisplayAudioController {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<PlaybackState>('loading');
  const [volume, updateVolume] = useState(0.55);

  useEffect(() => {
    const source = import.meta.env.VITE_DISPLAY_AUDIO_SRC || DEFAULT_AUDIO_SRC;
    const audio = new Audio(source);
    audio.loop = true;
    audio.preload = 'metadata';
    audio.volume = volume;
    audioRef.current = audio;

    const ready = () => setState((current) => (current === 'playing' ? current : 'ready'));
    const playing = () => setState('playing');
    const paused = () => setState((current) => (current === 'unavailable' ? current : 'paused'));
    const unavailable = () => setState('unavailable');
    audio.addEventListener('canplay', ready);
    audio.addEventListener('playing', playing);
    audio.addEventListener('pause', paused);
    audio.addEventListener('error', unavailable);
    audio.load();

    if (import.meta.env.VITE_DISPLAY_AUDIO_AUTOPLAY === '1') {
      void audio.play().catch(() => setState('blocked'));
    }

    return () => {
      audio.pause();
      audio.removeEventListener('canplay', ready);
      audio.removeEventListener('playing', playing);
      audio.removeEventListener('pause', paused);
      audio.removeEventListener('error', unavailable);
      audioRef.current = null;
    };
    // 오디오 인스턴스는 화면 생명주기 동안 하나만 유지한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = async () => {
    const audio = audioRef.current;
    if (!audio || state === 'unavailable') return;
    if (!audio.paused) {
      audio.pause();
      return;
    }
    try {
      await audio.play();
    } catch {
      setState('blocked');
    }
  };

  const setVolume = (next: number) => {
    const bounded = Math.max(0, Math.min(1, next));
    updateVolume(bounded);
    if (audioRef.current) audioRef.current.volume = bounded;
  };

  return { state, volume, toggle, setVolume };
}

export function DisplayAudioControl({
  controller,
  compact = false,
}: {
  controller: DisplayAudioController;
  compact?: boolean;
}) {
  const playing = controller.state === 'playing';
  const unavailable = controller.state === 'unavailable';
  return (
    <div
      className={styles.player}
      data-compact={compact}
      data-state={controller.state}
      data-testid="display-audio"
    >
      <button
        type="button"
        className={styles.toggle}
        onClick={() => void controller.toggle()}
        disabled={unavailable}
        aria-label={playing ? '음악 일시정지' : '음악 재생'}
      >
        {playing ? (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M7.5 5.5v13M16.5 5.5v13" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m9 6 9 6-9 6Z" />
          </svg>
        )}
      </button>
      <div className={styles.copy}>
        <small>VM3 · 인포테인먼트</small>
        <strong>{labelOf(controller.state)}</strong>
      </div>
      <label className={styles.volume}>
        <span>음량</span>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={controller.volume}
          onChange={(event) => controller.setVolume(Number(event.currentTarget.value))}
          aria-label="음악 음량"
        />
      </label>
    </div>
  );
}
