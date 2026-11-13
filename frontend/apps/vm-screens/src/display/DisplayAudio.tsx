import { useEffect, useRef, useState } from 'react';
import { MUSIC } from './music';
import styles from './displayAudio.module.css';

type PlaybackState = 'loading' | 'ready' | 'playing' | 'paused' | 'blocked' | 'unavailable';

/** 슬라이더 없이 고정. 휴대폰은 기기 음량 버튼으로 조절한다 */
const VOLUME = 0.55;

export interface DisplayAudioController {
  state: PlaybackState;
  /** 현재 재생 위치 (초) */
  position: number;
  /** 곡 길이 (초). 아직 모르면 null */
  duration: number | null;
  /** play() 응답을 기다리는 중: 이 동안 버튼을 잠근다 (연타로 play 를 pause 가 끊지 않게) */
  starting: boolean;
  toggle: () => Promise<void>;
}

/**
 * play() 거절 중 pause()·새 load 가 끊은 것(AbortError)은 실패가 아니다. 상태는 pause 이벤트가 정한다.
 * 그 밖(자동재생 차단 NotAllowedError 등)은 사용자가 직접 눌러 시작해야 한다.
 */
const interrupted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

const labelOf = (state: PlaybackState) => {
  switch (state) {
    case 'loading':
      return '음원 준비 중';
    case 'ready':
      return '재생 대기';
    case 'paused':
      return '일시정지';
    case 'playing':
      return '재생 중';
    case 'blocked':
      return '재생 버튼을 눌러 시작';
    case 'unavailable':
      return '음원 파일 확인 필요';
  }
};

/** 1:05 처럼 분:초 */
const clockOf = (seconds: number | null) => {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '-:--';
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
};

/**
 * 음원 하나를 화면 생명주기 동안 유지한다. 곡이 끝나면 처음부터 반복한다.
 * 첫 재생은 재생 버튼 터치로 시작한다 (VITE_DISPLAY_AUDIO_AUTOPLAY=1 일 때만 자동 재생 시도).
 */
export function useDisplayAudio(): DisplayAudioController {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<PlaybackState>('loading');
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  // 같은 틱 안의 두 번째 터치도 막는다 (state 는 다음 렌더에야 바뀐다)
  const startingRef = useRef(false);

  useEffect(() => {
    const source = import.meta.env.VITE_DISPLAY_AUDIO_SRC || MUSIC.src;
    const audio = new Audio(source);
    audio.loop = true;
    audio.preload = 'metadata';
    audio.volume = VOLUME;
    audioRef.current = audio;

    const ready = () => setState((current) => (current === 'loading' ? 'ready' : current));
    const playing = () => setState('playing');
    const paused = () => setState((current) => (current === 'unavailable' ? current : 'paused'));
    const unavailable = () => setState('unavailable');
    const time = () => setPosition(audio.currentTime);
    const length = () =>
      setDuration(Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null);
    audio.addEventListener('canplay', ready);
    audio.addEventListener('playing', playing);
    audio.addEventListener('pause', paused);
    audio.addEventListener('error', unavailable);
    audio.addEventListener('timeupdate', time);
    audio.addEventListener('durationchange', length);
    audio.addEventListener('loadedmetadata', length);
    audio.load();

    if (import.meta.env.VITE_DISPLAY_AUDIO_AUTOPLAY === '1') {
      void audio.play().catch((error: unknown) => {
        if (!interrupted(error)) setState('blocked');
      });
    }

    return () => {
      audio.pause();
      audio.removeEventListener('canplay', ready);
      audio.removeEventListener('playing', playing);
      audio.removeEventListener('pause', paused);
      audio.removeEventListener('error', unavailable);
      audio.removeEventListener('timeupdate', time);
      audio.removeEventListener('durationchange', length);
      audio.removeEventListener('loadedmetadata', length);
      audioRef.current = null;
    };
  }, []);

  const toggle = async () => {
    const audio = audioRef.current;
    if (!audio || state === 'unavailable' || startingRef.current) return;
    if (!audio.paused) {
      audio.pause();
      return;
    }
    startingRef.current = true;
    setStarting(true);
    try {
      await audio.play();
    } catch (error) {
      if (!interrupted(error)) setState('blocked');
    } finally {
      startingRef.current = false;
      if (audioRef.current === audio) setStarting(false);
    }
  };

  return { state, position, duration, starting, toggle };
}

/** 파형 진행 바 막대 수와 높이 (0~1, 고정 무늬 — 실제 음원 분석값이 아님) */
const BARS = Array.from(
  { length: 36 },
  (_, i) => 0.3 + 0.7 * Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.45)),
);

/**
 * 운전자 화면 음악 카드: 앨범 아트 자리 · 곡명·아티스트 · 재생/일시정지,
 * 파형 모양 진행 바(보기 전용, 진행된 막대만 강조색) · 현재 시각/길이
 */
export function DisplayAudioControl({ controller }: { controller: DisplayAudioController }) {
  const { state, position, duration, starting } = controller;
  const playing = state === 'playing';
  const unavailable = state === 'unavailable';
  const ratio = duration ? Math.min(1, Math.max(0, position / duration)) : 0;
  return (
    <div className={styles.player} data-state={state} data-testid="display-audio">
      <div className={styles.track}>
        <span className={styles.art} aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M9 17V5l11-2v12" />
            <circle cx="6.5" cy="17" r="2.5" />
            <circle cx="17.5" cy="15" r="2.5" />
          </svg>
        </span>
        <div className={styles.copy}>
          <strong data-testid="music-title">{MUSIC.title}</strong>
          <span data-testid="music-artist">{MUSIC.artist}</span>
        </div>
        <button
          type="button"
          className={styles.toggle}
          onClick={() => void controller.toggle()}
          disabled={unavailable || starting}
          aria-busy={starting}
          aria-label={playing ? '음악 일시정지' : '음악 재생'}
        >
          {playing ? (
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M8 5.5v13M16 5.5v13" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 6 9 6-9 6Z" />
            </svg>
          )}
        </button>
      </div>
      <div
        className={styles.wave}
        role="progressbar"
        aria-label="재생 위치"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration ?? 0)}
        aria-valuenow={Math.round(position)}
        aria-valuetext={`${clockOf(position)} / ${clockOf(duration)}`}
        data-testid="music-wave"
      >
        {BARS.map((height, i) => (
          <i
            key={i}
            style={{ height: `${Math.round(height * 100)}%` }}
            data-on={(i + 0.5) / BARS.length <= ratio}
          />
        ))}
      </div>
      <div className={styles.meta}>
        <span data-testid="music-time-now">{clockOf(position)}</span>
        <span data-testid="music-state">{labelOf(state)}</span>
        <span data-testid="music-time-total">{clockOf(duration)}</span>
      </div>
    </div>
  );
}
