import { useId } from 'react';
import styles from './driver.module.css';

/** 바퀴 중심 (viewBox 120×220, 앞이 위) */
const WHEELS = {
  frontLeft: { x: 15, y: 52 },
  frontRight: { x: 105, y: 52 },
  rearLeft: { x: 15, y: 166 },
  rearRight: { x: 105, y: 166 },
};

function Wheel({ x, y, angle = 0, testId }: { x: number; y: number; angle?: number; testId?: string }) {
  return (
    <g transform={`rotate(${angle.toFixed(1)} ${x} ${y})`} data-testid={testId} data-angle={angle.toFixed(1)}>
      <rect className={styles.carTire} x={x - 7} y={y - 15} width="14" height="30" rx="4" />
      <rect className={styles.carTread} x={x - 2} y={y - 11} width="4" height="22" rx="2" />
    </g>
  );
}

/**
 * 위에서 본 차량 (SVG 그림, 3D 아님). 앞바퀴가 조향각만큼 돈다.
 * steerDeg: + 왼쪽 (MQTT 계약 steer_rad + = 왼쪽). SVG 회전은 시계 방향이 + 라서 부호를 바꾼다.
 */
export function CarTop({ steerDeg }: { steerDeg: number | null }) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const wheel = -(steerDeg ?? 0);
  return (
    <svg
      className={styles.car}
      viewBox="0 0 120 220"
      role="img"
      aria-label="위에서 본 차량"
      data-testid="car-top"
    >
      <defs>
        <linearGradient id={`body-${id}`} x1="0" y1="0" x2="1" y2="0">
          <stop className={styles.carBodyEdge} offset="0" />
          <stop className={styles.carBodyMid} offset="0.5" />
          <stop className={styles.carBodyEdge} offset="1" />
        </linearGradient>
        <linearGradient id={`glass-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop className={styles.carGlassTop} offset="0" />
          <stop className={styles.carGlassBottom} offset="1" />
        </linearGradient>
        <radialGradient id={`shadow-${id}`} cx="0.5" cy="0.5" r="0.5">
          <stop className={styles.carShadowIn} offset="0.55" />
          <stop className={styles.carShadowOut} offset="1" />
        </radialGradient>
      </defs>
      <ellipse cx="60" cy="112" rx="58" ry="108" fill={`url(#shadow-${id})`} />
      <Wheel {...WHEELS.frontLeft} angle={wheel} testId="wheel-front-left" />
      <Wheel {...WHEELS.frontRight} angle={wheel} testId="wheel-front-right" />
      <Wheel {...WHEELS.rearLeft} />
      <Wheel {...WHEELS.rearRight} />
      <path
        className={styles.carBody}
        fill={`url(#body-${id})`}
        d="M60 8C86 8 100 20 102 44L104 176C104 200 90 212 60 212C30 212 16 200 16 176L18 44C20 20 34 8 60 8Z"
      />
      <path className={styles.carMirror} d="M18 72c-5 0-8 2-8 5s3 4 8 3ZM102 72c5 0 8 2 8 5s-3 4-8 3Z" />
      <path
        className={styles.carGlass}
        fill={`url(#glass-${id})`}
        d="M30 66Q60 54 90 66L86 96Q60 89 34 96Z"
      />
      <rect className={styles.carRoof} x="33" y="100" width="54" height="56" rx="10" />
      <path
        className={styles.carGlass}
        fill={`url(#glass-${id})`}
        d="M34 160Q60 166 86 160L90 184Q60 193 30 184Z"
      />
      <path className={styles.carLine} d="M60 18V52M26 46Q60 34 94 46" />
      <path className={styles.carHeadlight} d="M28 17Q36 12 44 12M92 17Q84 12 76 12" />
      <path className={styles.carTaillight} d="M28 205Q36 209 46 209M92 205Q84 209 74 209" />
    </svg>
  );
}
