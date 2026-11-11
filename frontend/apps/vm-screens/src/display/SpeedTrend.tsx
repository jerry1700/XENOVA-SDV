import { useId } from 'react';
import type { DriverModeTone } from './driveExtras';
import { HISTORY_MS, type SpeedSample } from './speedHistory';
import styles from './driver.module.css';

const W = 240;
const H = 60;
const TOP = 6;
const BOTTOM = 56;
const TONES: DriverModeTone[] = ['auto', 'manual', 'warning', 'danger', 'muted'];

interface Run {
  tone: DriverModeTone;
  points: { x: number; y: number }[];
}

/**
 * 최근 60초 속도 추이 곡선 (선 빛 + 아래 그라데이션 채움). 세로 축은 계기판 눈금과 같은 0 ~ scaleMax.
 * 각 구간은 그 시점(구간 끝 표본)의 주행 모드 색으로 그린다.
 */
export function SpeedTrend({ samples, scaleMax }: { samples: SpeedSample[]; scaleMax: number }) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const latest = samples[samples.length - 1]?.t ?? 0;
  const points = samples.map((s) => ({
    x: Math.max(0, ((s.t - (latest - HISTORY_MS)) / HISTORY_MS) * W),
    y: BOTTOM - Math.min(1, Math.abs(s.v) / scaleMax) * (BOTTOM - TOP),
  }));
  // 같은 모드가 이어지는 구간끼리 묶는다 (앞 구간의 끝점에서 이어 그린다)
  const runs: Run[] = [];
  for (let i = 1; i < points.length; i += 1) {
    const tone = samples[i]!.tone;
    const last = runs[runs.length - 1];
    if (last && last.tone === tone) last.points.push(points[i]!);
    else runs.push({ tone, points: [points[i - 1]!, points[i]!] });
  }
  const line = (pts: Run['points']) =>
    pts.map((pt, i) => `${i ? 'L' : 'M'} ${pt.x.toFixed(1)} ${pt.y.toFixed(1)}`).join(' ');
  const area = (pts: Run['points']) =>
    `${line(pts)} L ${pts[pts.length - 1]!.x.toFixed(1)} ${BOTTOM} L ${pts[0]!.x.toFixed(1)} ${BOTTOM} Z`;

  return (
    <svg
      className={styles.trend}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="최근 60초 속도 추이"
      data-testid="speed-trend"
      data-points={samples.length}
    >
      <defs>
        {TONES.map((tone) => (
          <linearGradient key={tone} id={`trend-${id}-${tone}`} x1="0" y1="0" x2="0" y2="1" data-tone={tone}>
            <stop className={styles.trendFillTop} offset="0" />
            <stop className={styles.trendFillBottom} offset="1" />
          </linearGradient>
        ))}
      </defs>
      {[0.5, 1].map((r) => (
        <line
          key={r}
          className={styles.trendGrid}
          x1="0"
          x2={W}
          y1={BOTTOM - r * (BOTTOM - TOP)}
          y2={BOTTOM - r * (BOTTOM - TOP)}
        />
      ))}
      <line className={styles.trendBase} x1="0" x2={W} y1={BOTTOM} y2={BOTTOM} />
      {runs.map((run, i) => (
        <g key={i} data-tone={run.tone} data-testid="trend-run">
          <path d={area(run.points)} fill={`url(#trend-${id}-${run.tone})`} />
          <path className={styles.trendLine} d={line(run.points)} vectorEffect="non-scaling-stroke" />
        </g>
      ))}
    </svg>
  );
}
