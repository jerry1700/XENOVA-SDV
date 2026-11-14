import { useState } from 'react';
import type { DriverModeTone } from './driveExtras';
import type { Presentation } from './model';

export interface SpeedSample {
  /** 수집기 생성 시각 (ms) */
  t: number;
  /** 속도 (m/s) */
  v: number;
  /** 그 시점의 주행 모드 색 */
  tone: DriverModeTone;
}

export const HISTORY_MS = 60_000;

/**
 * 최근 60초 속도·주행 모드 기록을 화면 메모리에만 쌓는다 (저장·전송 없음).
 * 새 presentation 을 받을 때마다 한 점 (시각은 수집기 generated_at).
 * 표시 연결이 끊겨도 지우지 않는다: 짧게 끊겼다 돌아오면 기록이 이어지고, 오래 끊겼으면
 * 돌아온 첫 점 기준 60초보다 오래된 점이 빠진다. 끊긴 동안 그릴지는 화면이 정한다.
 * 시각이 뒤로 가면(수집기 재시작) 처음부터 다시 쌓는다.
 */
export function useSpeedHistory(
  p: Presentation | null,
  speed: number | null,
  connected: boolean,
  tone: DriverModeTone,
): SpeedSample[] {
  const [seen, setSeen] = useState<{ p: Presentation | null; connected: boolean }>({
    p: null,
    connected: false,
  });
  const [samples, setSamples] = useState<SpeedSample[]>([]);
  if (seen.p !== p || seen.connected !== connected) {
    setSeen({ p, connected });
    const t = p ? Date.parse(p.state.generated_at) : Number.NaN;
    if (connected && speed !== null && Number.isFinite(t)) {
      setSamples((prev) => {
        const last = prev[prev.length - 1];
        if (last && t === last.t) return prev;
        const kept = last && t < last.t ? [] : prev.filter((s) => s.t > t - HISTORY_MS);
        return [...kept, { t, v: speed, tone }];
      });
    }
  }
  return samples;
}

/** 평균 속도 (m/s, 절댓값). 기록이 없으면 null */
export function averageSpeed(samples: SpeedSample[]): number | null {
  if (!samples.length) return null;
  return samples.reduce((sum, s) => sum + Math.abs(s.v), 0) / samples.length;
}
