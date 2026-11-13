/** 운전자 화면 얇은 선 아이콘 (currentColor, viewBox 24). 이모지 대신 쓴다 */
const LINE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const;

type IconProps = { className?: string };

export function BatteryIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <rect x="7" y="4" width="10" height="17" rx="2" />
      <path d="M10 2.5h4M12 9v4M10 11h4" />
    </svg>
  );
}

export function SteeringIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="2.2" />
      <path d="M3.8 10.5c2.6-.6 5.4-.9 8.2-.9s5.6.3 8.2.9M12 14.2v6.3" />
    </svg>
  );
}

export function NoteIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <path d="M9 17.5V5.5l10-2v11.5" />
      <circle cx="6.5" cy="17.5" r="2.5" />
      <circle cx="16.5" cy="15" r="2.5" />
    </svg>
  );
}

export function LinkIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <path d="M5 12.5a10 10 0 0 1 14 0M8 15.5a5.6 5.6 0 0 1 8 0" />
      <circle cx="12" cy="18.5" r="1" />
    </svg>
  );
}

export function SpeedIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <path d="M4.5 17a8 8 0 1 1 15 0" />
      <path d="m12 13 4-4.5" />
      <circle cx="12" cy="13.5" r="1" />
    </svg>
  );
}

export function CarIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <path d="M5 15.5V12l2-4.5h10l2 4.5v3.5" />
      <path d="M4 15.5h16v3H4zM7.5 11.5h9" />
    </svg>
  );
}

/** 경고 (안전 정지): 수동 주행 색과 구분되게 모양으로도 알린다 */
export function WarningIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" {...LINE}>
      <path d="M12 3.5 21.5 20h-19Z" />
      <path d="M12 9.5v5M12 17.2v.1" />
    </svg>
  );
}
