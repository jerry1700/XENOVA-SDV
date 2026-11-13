import { Button, Card, useToast } from '@xenova/ui';
import { useState } from 'react';
import { ApiError } from '../api/client';
import { useTemplates, useUpdateVmResources } from '../api/queries';
import type { Vm } from '../api/types';
import styles from './pages.module.css';

type Feedback = { tone: 'ok' | 'fault'; message: string } | null;

function messageOf(error: unknown): string {
  return error instanceof ApiError ? `${error.message} (${error.code})` : String(error);
}

interface ResourceAdjustmentPanelProps {
  vm: Vm;
  accent: string;
}

export function ResourceAdjustmentPanel({ vm, accent }: ResourceAdjustmentPanelProps) {
  const templates = useTemplates();
  const update = useUpdateVmResources(vm.vm_id);
  const toast = useToast();
  const [sizeOverride, setSizeOverride] = useState<{ vcpus: number; memory_mib: number } | null>(null);
  const [sizeFeedback, setSizeFeedback] = useState<Feedback>(null);

  const currentSize = { vcpus: vm.vcpus, memory_mib: vm.memory_mib };
  const size = sizeOverride ?? currentSize;
  const template = templates.data?.find(
    (item) => item.template_id === vm.template_id && item.version === vm.template_version,
  );
  const limits = {
    vcpus_min: 1,
    vcpus_max: 4,
    memory_mib_min: template?.min_memory_mib ?? 128,
    memory_mib_max: 1024,
    memory_mib_step: 16,
  };
  const sizeDirty = size.vcpus !== currentSize.vcpus || size.memory_mib !== currentSize.memory_mib;
  const fixedProfile = vm.guest_kind === 'zephyr';
  const sizeEditable = !fixedProfile && vm.state === 'stopped';
  const sizeValid =
    Number.isInteger(size.vcpus) &&
    size.vcpus >= limits.vcpus_min &&
    size.vcpus <= limits.vcpus_max &&
    Number.isInteger(size.memory_mib) &&
    size.memory_mib >= limits.memory_mib_min &&
    size.memory_mib <= limits.memory_mib_max &&
    size.memory_mib % limits.memory_mib_step === 0;

  const applySize = async () => {
    setSizeFeedback(null);
    try {
      const result = await update.mutateAsync(size);
      if (
        result.after.vcpus !== size.vcpus ||
        result.after.memory_mib !== size.memory_mib ||
        !result.applies_on_next_start
      ) {
        setSizeOverride(null);
        const message = '적용 결과가 요청값과 달라 VM 정보를 다시 불러옵니다.';
        setSizeFeedback({ tone: 'fault', message });
        toast.show(message, 'fault', 5000);
        return;
      }
      setSizeOverride(null);
      setSizeFeedback({
        tone: 'ok',
        message: 'vCPU와 메모리 설정을 변경했습니다. 다음 시작부터 적용됩니다.',
      });
      toast.show(`${vm.name}: VM 리소스 적용 완료`, 'ok');
    } catch (error) {
      setSizeOverride(null);
      const message = `리소스 적용에 실패했습니다. 현재 서버 값으로 다시 동기화합니다. ${messageOf(error)}`;
      setSizeFeedback({ tone: 'fault', message });
      toast.show(message, 'fault', 5000);
    }
  };

  return (
    <Card
      title="vCPU 및 메모리 조정"
      accent={accent}
      actions={<span className={styles.pinningCardTag}>RESOURCE ALLOCATION</span>}
    >
      <section className={styles.resourceTuningSection} aria-labelledby="capacity-title">
        <div className={styles.resourceSectionHeading}>
          <div>
            <span id="capacity-title">변경 설정</span>
            <p>VM을 정지한 뒤 vCPU 수와 메모리 용량을 변경할 수 있습니다.</p>
          </div>
          <span className={styles.resourceEditState} data-enabled={sizeEditable || undefined}>
            {fixedProfile ? '고정 프로필' : sizeEditable ? '변경 가능' : 'VM 정지 필요'}
          </span>
        </div>

        <div className={styles.resourceCapacityGrid}>
          <div className={styles.resourceCapacityItem}>
            <span>vCPU</span>
            <div className={styles.vcpuOptions} role="group" aria-label="vCPU 개수 선택">
              {Array.from(
                { length: limits.vcpus_max - limits.vcpus_min + 1 },
                (_, index) => limits.vcpus_min + index,
              ).map((value) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={size.vcpus === value}
                  disabled={!sizeEditable || update.isPending}
                  onClick={() => {
                    setSizeFeedback(null);
                    setSizeOverride({ ...size, vcpus: value });
                  }}
                >
                  <strong>{value}</strong>
                  <small>vCPU</small>
                </button>
              ))}
            </div>
            <small>
              {limits.vcpus_min}~{limits.vcpus_max}개
            </small>
          </div>
          <div className={styles.resourceCapacityItem}>
            <span>메모리</span>
            <div className={styles.memoryInputGroup}>
              <input
                type="number"
                min={limits.memory_mib_min}
                max={limits.memory_mib_max}
                step={limits.memory_mib_step}
                value={size.memory_mib}
                disabled={!sizeEditable || update.isPending}
                aria-invalid={!sizeValid}
                onChange={(event) => {
                  setSizeFeedback(null);
                  setSizeOverride({ ...size, memory_mib: Number(event.target.value) });
                }}
              />
              <span>MiB</span>
            </div>
            <small>
              {limits.memory_mib_min}~{limits.memory_mib_max} MiB · {limits.memory_mib_step} MiB 단위
            </small>
          </div>
        </div>
        {!sizeValid && (
          <p className={styles.resourceValidation} role="alert">
            vCPU와 메모리의 허용 범위 및 변경 단위를 확인해 주세요.
          </p>
        )}

        <div className={styles.resourceApplyRow}>
          <span>
            {fixedProfile
              ? 'Zephyr 템플릿의 1 vCPU · 16 MiB 프로필은 변경할 수 없습니다.'
              : vm.state !== 'stopped'
                ? 'VM을 정지하면 입력과 적용 버튼이 활성화됩니다.'
                : sizeDirty
                  ? `${currentSize.vcpus} vCPU / ${currentSize.memory_mib} MiB → ${size.vcpus} vCPU / ${size.memory_mib} MiB`
                  : '현재 서버 설정과 입력값이 일치합니다.'}
          </span>
          <div>
            <Button
              variant="secondary"
              disabled={!sizeDirty || update.isPending}
              onClick={() => {
                setSizeOverride(null);
                setSizeFeedback(null);
              }}
            >
              변경 취소
            </Button>
            <Button
              disabled={!sizeEditable || !sizeDirty || !sizeValid || update.isPending}
              onClick={() => void applySize()}
            >
              {update.isPending ? '적용 중…' : '리소스 적용'}
            </Button>
          </div>
        </div>
        {sizeFeedback && (
          <div className={styles.pinningFeedback} data-tone={sizeFeedback.tone} role="status">
            <i aria-hidden="true" />
            {sizeFeedback.message}
          </div>
        )}
      </section>
    </Card>
  );
}
