import { useId } from 'react';
import styles from './driver.module.css';

/** 게이지 원호 좌표 (viewBox 단위). 270° 원호, 아래가 열린 모양. 글자는 SVG 밖(HTML)에서 그려 최소 크기를 지킨다 */
const VIEW_W = 240;
const VIEW_H = 200;
const CX = 120;
const CY = 110;
const R = 88;
const INNER_R = R - 22;
const START_DEG = 135;
const SWEEP_DEG = 270;
const MAJOR = 5;

const point = (fraction: number, radius: number) => {
  const rad = ((START_DEG + SWEEP_DEG * fraction) * Math.PI) / 180;
  return { x: CX + radius * Math.cos(rad), y: CY + radius * Math.sin(rad) };
};

const arc = (from: number, to: number, radius: number) => {
  const a = point(from, radius);
  const b = point(to, radius);
  const large = SWEEP_DEG * (to - from) > 180 ? 1 : 0;
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${radius} ${radius} 0 ${large} 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`;
};

const clamp = (v: number) => Math.min(1, Math.max(0, v));

/**
 * 색 이름 (driver.module.css 에서 data-tone → --tone 토큰).
 * auto·manual·warning·danger·muted = 주행 모드, ok = 정상(배터리), interaction = 조향
 */
export type GaugeTone = 'auto' | 'manual' | 'warning' | 'danger' | 'muted' | 'ok' | 'interaction';

/**
 * 원형 게이지. 굵은 호(빈 트랙 어둡게) + 값만큼 tone 색 그라데이션·둥근 끝·은은한 빛, 주·보조 눈금,
 * 경계선(예: 최대 속도), 안쪽 얇은 보조 호(다른 강조색, 가운데 기준 좌우 대칭 가능).
 * 가운데 값·단위와 눈금 양끝 글자는 HTML 로 겹쳐 그린다.
 */
export function ArcGauge({
  id,
  label,
  fraction,
  tone,
  value,
  unit,
  scaleStart,
  scaleEnd,
  boundary = null,
  inner = null,
  over = false,
}: {
  /** data-testid 접두어 (speed → speed-gauge, speed-value …) */
  id: string;
  /** 보조기기용 설명 */
  label: string;
  /** 채울 비율 0~1. 값이 없으면 null (호를 비운다) */
  fraction: number | null;
  tone: GaugeTone;
  value: string;
  unit: string;
  scaleStart: string;
  scaleEnd: string;
  /** 원호를 가로지르는 얇은 경계선 위치 0~1 */
  boundary?: number | null;
  /** 안쪽 보조 호: from~to (0~1), center 가 있으면 그 위치에 기준 눈금 */
  inner?: { from: number; to: number; center?: number } | null;
  /** 경계(최대 속도)를 넘었는지: 경계선을 굵게 */
  over?: boolean;
}) {
  const gradient = `g-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const filled = fraction === null ? null : clamp(fraction);
  const end = filled === null ? null : point(filled, R);
  const ticks = Array.from({ length: MAJOR * 2 + 1 }, (_, i) => i / (MAJOR * 2));
  const innerFrom = inner ? clamp(Math.min(inner.from, inner.to)) : 0;
  const innerTo = inner ? clamp(Math.max(inner.from, inner.to)) : 0;

  return (
    <div className={styles.gaugeBox}>
      <div
        className={styles.gaugeInner}
        role="img"
        aria-label={label}
        data-testid={`${id}-gauge`}
        data-tone={tone}
        data-empty={filled === null}
        data-over={over}
      >
        <svg className={styles.gauge} viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} aria-hidden="true">
          <defs>
            <linearGradient id={gradient} x1="0" y1="1" x2="1" y2="0">
              <stop className={styles.stopFrom} offset="0" />
              <stop className={styles.stopTo} offset="1" />
            </linearGradient>
          </defs>
          <path className={styles.gaugeTrack} d={arc(0, 1, R)} />
          {filled !== null && filled > 0 && (
            <path
              className={styles.gaugeFill}
              d={arc(0, filled, R)}
              stroke={`url(#${gradient})`}
              data-testid={`${id}-fill`}
            />
          )}
          {end && <circle className={styles.gaugeTip} cx={end.x} cy={end.y} r={4} />}
          {ticks.map((t) => {
            const major = Math.round(t * MAJOR * 2) % 2 === 0;
            const a = point(t, R + 13);
            const b = point(t, R + (major ? 20 : 17));
            return (
              <line
                key={t}
                className={styles.gaugeTick}
                data-major={major}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
              />
            );
          })}
          {boundary !== null &&
            (() => {
              const a = point(clamp(boundary), R - 11);
              const b = point(clamp(boundary), R + 11);
              return (
                <line
                  className={styles.gaugeBoundary}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  data-testid={`${id}-boundary`}
                />
              );
            })()}
          {inner && (
            <>
              <path className={styles.innerTrack} d={arc(0, 1, INNER_R)} />
              {innerTo - innerFrom > 0.002 && (
                <path
                  className={styles.innerFill}
                  d={arc(innerFrom, innerTo, INNER_R)}
                  data-testid={`${id}-inner`}
                />
              )}
              {inner.center !== undefined &&
                (() => {
                  const a = point(clamp(inner.center), INNER_R - 5);
                  const b = point(clamp(inner.center), INNER_R + 5);
                  return <line className={styles.innerCenter} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />;
                })()}
            </>
          )}
        </svg>
        <div className={styles.gaugeReadout}>
          <strong data-testid={`${id}-value`}>{value}</strong>
          <span>{unit}</span>
        </div>
        <span className={styles.gaugeScale} data-side="start" data-testid={`${id}-scale-start`}>
          {scaleStart}
        </span>
        <span className={styles.gaugeScale} data-side="end" data-testid={`${id}-scale-end`}>
          {scaleEnd}
        </span>
      </div>
    </div>
  );
}
