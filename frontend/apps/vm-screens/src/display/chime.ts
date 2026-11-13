/**
 * 전환 알림음 — 음원 파일 없이 Web Audio 로 짧은 음을 만든다 (저작권·용량 문제 없음).
 * 키오스크(HDMI)·휴대폰 모두 소리가 막혀 있을 수 있으므로 실패는 조용히 넘긴다. 화면 오버레이가 1차 안내다.
 */
type Tone = 'manual' | 'auto' | 'warning' | 'danger';

/** 주파수(Hz)와 길이(초)의 순서 — 수동: 올라가는 2음, 자율: 내려가는 2음, 대기: 1음, 정지: 낮은 3음 */
const PATTERN: Record<Tone, ReadonlyArray<readonly [number, number]>> = {
  manual: [
    [660, 0.12],
    [880, 0.18],
  ],
  auto: [
    [880, 0.12],
    [660, 0.18],
  ],
  warning: [[740, 0.2]],
  danger: [
    [392, 0.14],
    [392, 0.14],
    [392, 0.22],
  ],
};

let context: AudioContext | null = null;

function contextOf(): AudioContext | null {
  if (context) return context;
  const Ctor =
    (globalThis as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext })
      .AudioContext ?? (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    context = new Ctor();
  } catch {
    return null;
  }
  return context;
}

/** 톤에 맞는 짧은 알림음. 오디오를 못 쓰는 환경(테스트·소리 차단)에서는 아무것도 하지 않는다 */
export function playChime(tone: Tone): void {
  const ctx = contextOf();
  if (!ctx) return;
  try {
    if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined);
    let at = ctx.currentTime + 0.01;
    for (const [hz, seconds] of PATTERN[tone]) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = hz;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.35, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + seconds);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at);
      osc.stop(at + seconds + 0.02);
      at += seconds + 0.06;
    }
  } catch {
    // 소리는 보조 수단이다
  }
}
