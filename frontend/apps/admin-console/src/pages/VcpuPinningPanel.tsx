import { Button, Card, useToast } from '@xenova/ui';
import { useEffect, useMemo, useState } from 'react';
import { ApiError } from '../api/client';
import { useAuthedSocket } from '../api/drive';
import { useUpdateVcpuPinning, useVmResources } from '../api/queries';
import type { MetricSample, PcpuStatus, VcpuPinning, Vm } from '../api/types';
import { mappingLabel, normalizePinning, pinningEqual, toggleVmPcpu } from '../domain/vcpuPinning';
import styles from './pages.module.css';

const MOCK = import.meta.env.VITE_MOCK === 'true';
const PCPU_METRIC_NAMES = new Set(['pcpu_percent', 'pcpu_usage_percent']);

type Feedback = { tone: 'ok' | 'fault'; message: string } | null;

function loadMap(pcpus: PcpuStatus[]): Map<number, number | null> {
  return new Map(pcpus.map(({ pcpu, load_percent }) => [pcpu, load_percent]));
}

function usePcpuLoads(vmId: string, initial: PcpuStatus[]) {
  const initialLoads = useMemo(() => loadMap(initial), [initial]);
  const [liveLoadsByVm, setLiveLoadsByVm] = useState<Map<string, Map<number, number>>>(new Map());

  const { state, send } = useAuthedSocket(MOCK ? '' : '/ws/v1/metrics', (frame) => {
    if (frame.type !== 'metrics.sample') return;
    const sample = frame.payload as MetricSample | undefined;
    if (!sample) return;
    const updates = sample.values.flatMap((value) => {
      const rawPcpu = value.labels?.pcpu;
      if (!PCPU_METRIC_NAMES.has(value.name) || rawPcpu == null || value.value == null) return [];
      const pcpu = Number(rawPcpu);
      return Number.isInteger(pcpu) ? ([[pcpu, value.value]] as const) : [];
    });
    if (updates.length) {
      setLiveLoadsByVm((current) => {
        const next = new Map(current);
        next.set(vmId, new Map([...(current.get(vmId) ?? new Map()), ...updates]));
        return next;
      });
    }
  });

  useEffect(() => {
    if (state !== 'open') return;
    send({
      type: 'subscribe',
      request_id: `pcpu-${vmId}`,
      vm_ids: [vmId],
      names: [...PCPU_METRIC_NAMES],
    });
  }, [send, state, vmId]);

  const loads = useMemo(
    () => new Map([...initialLoads, ...(liveLoadsByVm.get(vmId) ?? new Map())]),
    [initialLoads, liveLoadsByVm, vmId],
  );
  return { loads, live: !MOCK && state === 'open' };
}

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return String(error);
  if (error.status === 404 || error.code === 'VM_CONTROL_NOT_SUPPORTED') {
    return '리소스 제어 API가 아직 연결되지 않았습니다. 백엔드 107 연동 후 실제 매핑이 표시됩니다.';
  }
  if (error.code === 'CPU_PLACEMENT_CONFLICT') {
    return '선택한 코어는 dom0 공유 영역이거나 다른 VM이 전용으로 사용 중입니다. 최신 배치를 다시 확인해 주세요.';
  }
  if (error.code === 'INVALID_VM_STATE') {
    return 'CPU 핀닝을 변경하려면 모든 VM을 먼저 정지해야 합니다.';
  }
  return `${error.message} (${error.code})`;
}

interface VcpuPinningPanelProps {
  vm: Vm;
  accent: string;
}

export function VcpuPinningPanel({ vm, accent }: VcpuPinningPanelProps) {
  const resources = useVmResources(vm.vm_id);
  const update = useUpdateVcpuPinning(vm.vm_id);
  const toast = useToast();
  const [draftOverride, setDraftOverride] = useState<VcpuPinning[] | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);

  const current = useMemo(
    () => normalizePinning(resources.data?.vcpu_pinning ?? []),
    [resources.data?.vcpu_pinning],
  );
  const draft = draftOverride ?? current;

  const pcpuStatuses = resources.data?.pcpus ?? [];
  const { loads, live } = usePcpuLoads(vm.vm_id, pcpuStatuses);
  const dirty = !pinningEqual(current, draft);
  const currentMapping = current[0];
  const draftMapping = draft[0];
  const stopped = vm.state === 'stopped';
  const exclusiveVm = vm.guest_kind === 'zephyr';
  const ownExclusive = new Set(exclusiveVm ? (currentMapping?.pcpus ?? []) : []);
  const otherExclusive = new Set(
    (resources.data?.exclusive_pcpus ?? []).filter((pcpu) => !ownExclusive.has(pcpu)),
  );
  const sharedPcpus = new Set(resources.data?.shared_pcpus ?? []);

  const unavailableReason = (pcpu: number): string | null => {
    if (otherExclusive.has(pcpu)) return '다른 VM이 전용으로 사용 중입니다.';
    if (exclusiveVm && sharedPcpus.has(pcpu)) return 'dom0와 공유 VM이 사용하는 코어입니다.';
    if (!exclusiveVm && !sharedPcpus.has(pcpu)) return '이 VM은 공유 pCPU 집합 안에서만 배치할 수 있습니다.';
    return null;
  };

  const apply = async () => {
    const requested = normalizePinning(draft);
    setFeedback(null);
    try {
      await update.mutateAsync({ mappings: requested });
      const refreshed = await resources.refetch();
      if (!refreshed.data || !pinningEqual(refreshed.data.vcpu_pinning, requested)) {
        setDraftOverride(null);
        const message = '적용 결과가 요청한 매핑과 달라 서버에서 다시 조회한 값으로 복원했습니다.';
        setFeedback({ tone: 'fault', message });
        toast.show(message, 'fault', 5000);
        return;
      }
      setDraftOverride(null);
      setFeedback({
        tone: 'ok',
        message: 'CPU 정책을 저장했습니다. 호스트 준비가 끝난 뒤 VM을 시작할 수 있습니다.',
      });
      toast.show(`${vm.name}: vCPU 핀닝 적용 완료`, 'ok');
    } catch (error) {
      setDraftOverride(null);
      const message = `적용에 실패해 서버 설정으로 복원했습니다. ${errorMessage(error)}`;
      setFeedback({ tone: 'fault', message });
      toast.show(message, 'fault', 5000);
    }
  };

  return (
    <Card
      title="vCPU 핀닝"
      accent={accent}
      actions={<span className={styles.pinningCardTag}>HARD AFFINITY</span>}
    >
      <div className={styles.pinningIntro}>
        <div>
          <strong>해당 VM의 vCPU가 사용할 물리 CPU 코어를 선택합니다.</strong>
          <p>
            플랫폼 정책에 따라 이 VM의 모든 vCPU에 같은 pCPU 집합이 적용됩니다. 다른 VM의 전용 코어와 dom0
            공유 영역은 선택할 수 없습니다.
          </p>
        </div>
        {resources.data && (
          <div className={styles.pinningObservation}>
            <span>{resources.data.pcpu_count} pCPU</span>
            <span>{live ? '실시간 부하 연결됨' : MOCK ? 'Mock 부하' : '최근 조회 부하'}</span>
          </div>
        )}
      </div>

      {resources.isPending && <div className={styles.pinningState}>실제 CPU 매핑을 조회하는 중입니다…</div>}
      {resources.error && (
        <div className={styles.pinningState} data-tone="waiting" role="status">
          <strong>vCPU 핀닝 정보를 불러올 수 없습니다.</strong>
          <span>{errorMessage(resources.error)}</span>
        </div>
      )}
      {resources.data && !stopped && (
        <div className={styles.pinningState} data-tone="waiting" role="status">
          <strong>실행 중에는 CPU 핀닝을 변경할 수 없습니다.</strong>
          <span>대상 VM을 포함해 모든 VM을 정지한 뒤 변경하세요.</span>
        </div>
      )}

      {resources.data && (
        <>
          <div className={styles.pinningTableWrap}>
            <table className={styles.pinningTable}>
              <thead>
                <tr>
                  <th scope="col">
                    <span>VM CPU (vCPU)</span>
                  </th>
                  {Array.from({ length: resources.data.pcpu_count }, (_, pcpu) => {
                    const reason = unavailableReason(pcpu);
                    const own = ownExclusive.has(pcpu);
                    const shared = sharedPcpus.has(pcpu);
                    const load = loads.get(pcpu);
                    return (
                      <th
                        key={pcpu}
                        scope="col"
                        data-core-state={
                          reason ? 'unavailable' : own ? 'current' : shared ? 'shared' : 'free'
                        }
                      >
                        <span>pCPU {pcpu}</span>
                        <small>{load == null ? '부하 미수신' : `${load.toFixed(1)}%`}</small>
                        {reason ? (
                          <em>선택 불가</em>
                        ) : own ? (
                          <em>현재 전용</em>
                        ) : shared ? (
                          <em>공유 코어</em>
                        ) : (
                          <em>사용 가능</em>
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {draftMapping && (
                  <tr>
                    <th scope="row">
                      <span>VM 전체</span>
                      <small>
                        {resources.data.vcpus} vCPU · 허용 코어 {draftMapping.pcpus.length}개
                      </small>
                    </th>
                    {Array.from({ length: resources.data.pcpu_count }, (_, pcpu) => {
                      const selected = draftMapping.pcpus.includes(pcpu);
                      const wasSelected = currentMapping?.pcpus.includes(pcpu) ?? false;
                      const lastSelected = selected && draftMapping.pcpus.length === 1;
                      const reason = unavailableReason(pcpu);
                      const disabled = update.isPending || !stopped || lastSelected || reason !== null;
                      const title = !stopped
                        ? 'VM을 정지한 뒤 변경할 수 있습니다.'
                        : (reason ?? (lastSelected ? '최소 한 개의 pCPU가 필요합니다.' : undefined));
                      return (
                        <td key={pcpu}>
                          <button
                            type="button"
                            className={styles.pinningCell}
                            aria-label={`VM 전체 vCPU · pCPU ${pcpu}`}
                            aria-pressed={selected}
                            data-selected={selected || undefined}
                            data-changed={selected !== wasSelected || undefined}
                            data-unavailable={reason !== null || undefined}
                            disabled={disabled}
                            title={title}
                            onClick={() => {
                              setFeedback(null);
                              setDraftOverride((value) => toggleVmPcpu(value ?? current, pcpu));
                            }}
                          >
                            <span aria-hidden="true">{selected ? '✓' : reason ? '×' : ''}</span>
                          </button>
                        </td>
                      );
                    })}
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className={styles.pinningReview} data-dirty={dirty || undefined}>
            <div>
              <span>{dirty ? '변경 미리보기' : '현재 적용값'}</span>
              {dirty ? (
                <ul>
                  <li>
                    <strong>전체 {resources.data.vcpus} vCPU</strong>
                    <span>{currentMapping ? mappingLabel(currentMapping) : '미설정'}</span>
                    <b aria-hidden="true">→</b>
                    <span>{draftMapping ? mappingLabel(draftMapping) : '미설정'}</span>
                  </li>
                </ul>
              ) : (
                <p>서버에서 조회한 실제 매핑과 화면 선택값이 일치합니다.</p>
              )}
            </div>
            <div className={styles.pinningActions}>
              <Button
                variant="secondary"
                disabled={!dirty || update.isPending || !stopped}
                onClick={() => {
                  setDraftOverride(null);
                  setFeedback(null);
                }}
              >
                변경 취소
              </Button>
              <Button disabled={!dirty || update.isPending || !stopped} onClick={() => void apply()}>
                {update.isPending ? '적용 중…' : '핀닝 적용'}
              </Button>
            </div>
          </div>

          {feedback && (
            <div
              className={styles.pinningFeedback}
              data-tone={feedback.tone}
              role={feedback.tone === 'fault' ? 'alert' : 'status'}
            >
              <i aria-hidden="true" />
              {feedback.message}
            </div>
          )}
        </>
      )}
    </Card>
  );
}
