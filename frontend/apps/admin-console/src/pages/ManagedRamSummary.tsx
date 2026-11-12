import type { MetricSample } from '../api/types';
import {
  buildManagedRamSeries,
  formatManagedRamPart,
  MANAGED_RAM_PART_LABEL,
  managedRamParts,
  managedRamReason,
} from '../domain/managedRam';
import { MemoryUsageSummary } from './MemoryUsageSummary';
import { useNow } from '../useNow';

/** Zephyr 관리 RAM 사용률 요약 (카드 첫 섹션). 관측 시각은 카드 제목의 "마지막 갱신"에 보인다. */
export function ManagedRamSummary({
  vmId,
  vmName,
  samples,
}: {
  vmId: string;
  vmName: string;
  samples: MetricSample[];
}) {
  // 화면 전체가 같이 쓰는 1초 시계 — 관측 만료 판정에 쓴다
  const now = useNow();
  const { current, recentObservation } = buildManagedRamSeries(samples, vmId, now);
  const display = recentObservation ?? current;
  const usable = display.status === 'ok';
  const expired = usable && current.status !== 'ok';
  // 사용 중 구성(정적 데이터·펌웨어 이미지·할당 페이지) 큰 순서 한 줄. 표시 중인 관측값 기준, 검증 실패·값 없음이면 생략
  const parts = usable ? managedRamParts(display) : null;
  const detail = parts?.length
    ? parts
        .map((part) => `${MANAGED_RAM_PART_LABEL[part.key]} ${formatManagedRamPart(part.bytes)}`)
        .join(' · ')
    : null;
  return (
    <MemoryUsageSummary
      label="메모리 사용률"
      ariaLabel={`${vmName} 메모리 사용률`}
      percent={usable ? Number(display.percent) : null}
      usedText={formatRamMiB(usable ? display.committedBytes : null)}
      totalText={formatRamMiB(usable ? display.totalBytes : null)}
      freeLabel={expired ? '관측 당시 여유' : '여유'}
      freeText={formatRamMiB(usable ? display.freeBytes : null)}
      expired={expired}
      notice={expired ? '관측 만료 · 최근 관측값' : usable ? null : managedRamReason(current.reason)}
      detail={detail}
    />
  );
}

function formatRamMiB(value: string | null): string {
  if (value == null) return '—';
  const divisor = 1_048_576n;
  const thousandths = (BigInt(value) * 1000n + divisor / 2n) / divisor;
  return `${(thousandths / 1000n).toLocaleString('ko-KR')}.${(thousandths % 1000n).toString().padStart(3, '0')} MiB`;
}
