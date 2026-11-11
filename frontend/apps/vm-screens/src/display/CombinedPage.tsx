import { useEffect, useState, type ReactNode } from 'react';
import { ArcGauge, type GaugeTone } from './ArcGauge';
import { CarTop } from './CarTop';
import { DisplayAudioControl, useDisplayAudio } from './DisplayAudio';
import {
  BatteryIcon,
  CarIcon,
  LinkIcon,
  NoteIcon,
  SpeedIcon,
  SteeringIcon,
  WarningIcon,
} from './DriverIcons';
import {
  batteryOf,
  driverMode,
  gaugeMaxOf,
  MAX_STEER_RAD,
  maxSpeedOf,
  steeringOf,
  vehicleOf,
  type DriverModeTone,
} from './driveExtras';
import { driveSummary } from './presentation';
import { averageSpeed, useSpeedHistory } from './speedHistory';
import { SpeedTrend } from './SpeedTrend';
import { useDisplay } from './useDisplay';
import styles from './driver.module.css';

/**
 * 운전자용 차량 화면 (S15P31E110-452). 휴대폰 가로(780×360)가 기준이고 HDMI(16:9)는 같은 배치를 확대한다.
 * 3열 패널: 속도 게이지·속도 추이·주행 모드 | 시각·위에서 본 차량·배터리·조향 | 주행 상태·음악. VM 정보는 보이지 않는다.
 * 항상 어두운 테마 (Xenova 브랜드 토큰 data-theme="xenova"). 색은 주행 모드가 정한다 (data-tone → --tone, driver.module.css):
 * 자율 = 자율 앱 색, 수동 = 수동 앱 색, 준비 중·재개 대기 = 주의, 안전 정지 = 위험 + 경고 아이콘, 미확인 = 흐린 회색.
 * 값은 /api/v1/display/presentation 하나에서 온다. 신선하지 않거나 없는 값은 "—"/수신 대기/미확인으로 둔다.
 * 속도는 RC 차 실측과 맞대어 보도록 m/s 로 표시한다.
 */
function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 15_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** 0.5 처럼 짧게 */
const short = (v: number) => String(Number(v.toFixed(2)));

/** 조향 최대각 (도) */
const MAX_STEER_DEG = Math.round((MAX_STEER_RAD * 180) / Math.PI);

/**
 * 주행 모드 표시: 색 점 + 색 글자. 안전 정지만 옅은 위험색 바탕·위험색 테두리 + 작은 경고 아이콘 (글자 크기는 같다)
 */
function ModeChip({
  tone,
  children,
  className,
  testId,
}: {
  tone: DriverModeTone;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <span className={className} data-tone={tone} data-band={tone === 'danger'} data-testid={testId}>
      {tone === 'danger' ? (
        <WarningIcon className={styles.chipIcon} />
      ) : (
        <i className={styles.chipDot} aria-hidden="true" />
      )}
      {children}
    </span>
  );
}

const GEARS = [
  { key: 'AUTO', text: '자율' },
  { key: 'MANUAL', text: '수동' },
  { key: 'AUTO_READY', text: '대기' },
  { key: 'SAFE_STOP', text: '정지' },
] as const;

/** 차량 위 모서리 정보: 아이콘·영문 소제목, 값, 작은 보조 줄 */
function SideInfo({
  id,
  icon,
  caption,
  label,
  value,
  sub,
  tone,
  align,
}: {
  id: string;
  icon: ReactNode;
  /** 작은 영문 대문자 소제목 */
  caption: string;
  /** 보조기기·툴팁 설명 */
  label: string;
  value: string;
  sub: string;
  tone: GaugeTone;
  align: 'start' | 'end';
}) {
  return (
    <div
      className={styles.sideInfo}
      data-tone={tone}
      data-align={align}
      aria-label={label}
      title={label}
      role="group"
    >
      <span className={styles.caption}>
        <span className={styles.sideIcon}>{icon}</span>
        {caption}
      </span>
      <strong data-testid={`${id}-value`}>{value}</strong>
      <small data-testid={`${id}-sub`}>{sub}</small>
    </div>
  );
}

/** 차량 양옆 얇은 세로 막대 (아래부터 채움) */
function SideBar({ id, fraction, tone }: { id: string; fraction: number | null; tone: GaugeTone }) {
  return (
    <span
      className={styles.barTrack}
      data-tone={tone}
      data-testid={`${id}-bar`}
      data-empty={fraction === null}
    >
      {fraction !== null && (
        <i style={{ height: `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%` }} />
      )}
    </span>
  );
}

export function CombinedPage() {
  const audio = useDisplayAudio();
  const now = useClock();
  const { presentation: p, connected, error } = useDisplay();
  const summary = driveSummary(p, connected);
  const mode = driverMode(summary);
  const vehicle = connected ? vehicleOf(p) : null;
  const live = vehicle?.source.status === 'fresh' ? vehicle : null;
  const speed = live?.v_mps ?? null;
  const steer = steeringOf(live?.steer_rad ?? null);
  const maxSpeed = connected ? maxSpeedOf(p) : null;
  const scaleMax = gaugeMaxOf(maxSpeed);
  const battery = connected ? batteryOf(p) : null;
  const voltage = battery === null ? null : (live?.batt_v ?? null);
  const history = useSpeedHistory(p, speed, connected, mode.tone);
  const average = averageSpeed(history);
  const time = now.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false });

  const over = speed !== null && maxSpeed !== null && Math.abs(speed) > maxSpeed + 1e-9;
  const batteryTone: GaugeTone =
    battery === null ? 'muted' : battery <= 20 ? 'danger' : battery <= 50 ? 'warning' : 'ok';
  // 준비 중(자율·수동)은 그 모드 칸을, 미확인은 아무 칸도 켜지 않는다
  const current = mode.tone === 'muted' ? null : summary.mode;

  return (
    <main className={styles.screen} data-theme="xenova" data-testid="combined" data-connected={connected}>
      <h1 className={styles.srOnly}>차량 화면</h1>

      {/* 오른쪽: 주행 상태 · 음악 */}
      <section className={styles.panel} data-panel="status" aria-label="주행 상태와 음악">
        <header className={styles.panelHead}>
          <h2 className={styles.panelTitle}>
            <CarIcon className={styles.titleIcon} />
            STATUS
          </h2>
        </header>
        <div className={styles.card}>
          <span className={styles.caption}>DRIVE MODE</span>
          <ModeChip tone={mode.tone} className={styles.modeLabel} testId="drive-mode">
            {mode.label}
          </ModeChip>
          <span className={styles.link} data-connected={connected} data-testid="link">
            <LinkIcon className={styles.inlineIcon} />
            {connected ? '연결됨' : '연결 끊김'}
          </span>
          {!connected && (
            <p role="alert" className={styles.alert}>
              {error ?? '연결 대기'} · 현재 값은 미확인입니다.
            </p>
          )}
        </div>
        <div className={styles.card} data-grow="true">
          <span className={styles.caption}>
            <NoteIcon className={styles.inlineIcon} />
            MUSIC
          </span>
          <DisplayAudioControl controller={audio} />
        </div>
      </section>

      {/* 가운데: 시각 · 위에서 본 차량 · 배터리 · 조향 */}
      <section className={styles.panel} data-panel="vehicle" aria-label="차량">
        <header className={styles.panelHead} data-align="center">
          <span className={styles.clockPill}>
            <time data-testid="clock" dateTime={now.toISOString()}>
              {time}
            </time>
          </span>
        </header>
        <div className={styles.vehicle}>
          <div className={styles.vehicleInfo}>
            <SideInfo
              id="battery"
              align="start"
              icon={<BatteryIcon />}
              caption="BATTERY"
              label="배터리 잔량 (전압 기반 추정)"
              value={battery === null ? '—' : `${battery}%`}
              sub={voltage === null ? '수신 대기' : `${voltage.toFixed(1)} V`}
              tone={batteryTone}
            />
            <SideInfo
              id="steer"
              align="end"
              icon={<SteeringIcon />}
              caption="STEERING"
              label="조향 각도"
              value={steer === null ? '—' : steer.side ? `${steer.deg}° ${steer.side}` : '0°'}
              sub={steer === null ? '수신 대기' : `최대 ±${MAX_STEER_DEG}°`}
              tone="interaction"
            />
          </div>
          <div className={styles.vehicleStage}>
            <SideBar id="battery" fraction={battery === null ? null : battery / 100} tone={batteryTone} />
            <div className={styles.carStage}>
              <CarTop steerDeg={steer ? (steer.side === '우' ? -steer.deg : steer.deg) : null} />
            </div>
            <SideBar id="steer" fraction={steer === null ? null : Math.abs(steer.ratio)} tone="interaction" />
          </div>
        </div>
      </section>

      {/* 왼쪽: 속도 게이지 · 속도 추이 · 주행 모드 */}
      <section className={styles.panel} data-panel="speed" aria-label="속도">
        <header className={styles.panelHead}>
          <h2 className={styles.panelTitle}>
            <SpeedIcon className={styles.titleIcon} />
            SPEED
          </h2>
          <span className={styles.headValue} data-testid="speed-max">
            {maxSpeed === null ? 'MAX —' : `MAX ${short(maxSpeed)} m/s`}
          </span>
        </header>
        <ArcGauge
          id="speed"
          label={speed === null ? '속도 미확인' : `속도 ${speed.toFixed(2)} m/s`}
          fraction={speed === null ? null : Math.abs(speed) / scaleMax}
          // 채움 색 = 현재 주행 모드 색 (미확인이면 중립색)
          tone={mode.tone}
          over={over}
          value={speed === null ? '—' : speed.toFixed(2)}
          unit={speed === null ? '속도 수신 대기' : 'm/s'}
          scaleStart="0"
          scaleEnd={short(scaleMax)}
          boundary={maxSpeed !== null && maxSpeed <= scaleMax ? maxSpeed / scaleMax : null}
          // 안쪽 보조 호: 조향 (가운데 0, 왼쪽 조향은 왼쪽으로, 최대각 ±0.55 rad)
          inner={steer === null ? null : { from: 0.5, to: 0.5 - steer.ratio / 2, center: 0.5 }}
        />
        <div className={styles.trendBlock}>
          <div className={styles.trendHead}>
            <span>최근 60초</span>
            <span data-testid="speed-average">
              {average === null ? '평균 —' : `평균 ${average.toFixed(2)} m/s`}
            </span>
          </div>
          <SpeedTrend samples={history} scaleMax={scaleMax} />
        </div>
        <ol className={styles.gears} aria-label="주행 모드">
          {GEARS.map((gear) => (
            <li
              key={gear.key}
              data-active={current === gear.key}
              data-tone={current === gear.key ? mode.tone : undefined}
              data-band={current === gear.key && mode.tone === 'danger'}
              data-testid={`gear-${gear.key}`}
            >
              {current === gear.key &&
                (mode.tone === 'danger' ? (
                  <WarningIcon className={styles.chipIcon} />
                ) : (
                  <i className={styles.chipDot} aria-hidden="true" />
                ))}
              {gear.text}
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}
