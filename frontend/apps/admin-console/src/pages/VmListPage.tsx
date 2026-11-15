import { Button, Card, Modal, StatusBadge, useToast } from '@xenova/ui';
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router';
import { errorText } from '../api/errorText';
import { useSynchronizeVms, useVms, type VmAction, useVmIdsUpdatingResources } from '../api/queries';
import type { Vm, VmSynchronizationResult } from '../api/types';
import { osLabel, sortVms } from '../domain/roles';
import {
  canDeleteVm,
  canStartVm,
  canStopVm,
  DELETE_BLOCKED_HINT,
  DELETE_CONFIRM_TEXT,
  STATE_HEALTH,
  STATE_LABEL,
  VM_ACTION_LABEL,
} from '../format';
import styles from './pages.module.css';
import { VmOperationNotice } from './VmOperationProgress';
import { useVmOperationProgress } from './useVmOperationProgress';
import {
  finishVmCreation,
  clearVmCreation,
  isOwnedVmCreation,
  isVmCreationPending,
  matchesVmCreation,
  usePendingVmCreations,
} from './vmCreationProgress';
import { LiveDataAlert } from '../layout/LiveDataAlert';

const VM_ACCENTS = ['var(--x-app-autonomous)', 'var(--x-app-manual)', 'var(--x-app-interaction)'] as const;

/** VM 생명주기와 할당 자원을 관리하는 목록. 주행 모드·제어권은 Overview에서만 다룬다. */
export function VmListPage() {
  const vms = useVms(1000, { keepLastData: true });
  const vmOperations = useVmOperationProgress();
  const toast = useToast();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState<Vm | null>(null);
  const pendingCreations = usePendingVmCreations();
  const updatingResources = useVmIdsUpdatingResources();

  const registrationSync = useSynchronizeVms();

  // 보드 등록 목록 ↔ DB 등록 동기화. VM 실행 상태는 목록 자동 갱신이 따로 맡는다.
  // 200 이어도 보류·충돌·정보 부족이 있으면 확인할 VM 과 사유를 같이, 더 오래 보인다.
  const synchronize = async () => {
    try {
      const result = await registrationSync.mutateAsync();
      const needsAttention = synchronizationProblems(result).length > 0;
      toast.show(
        <SynchronizationToast result={result} />,
        needsAttention ? 'recovering' : 'ok',
        needsAttention ? 10000 : 5000,
      );
    } catch (error) {
      toast.show(
        `VM 동기화에 실패했습니다. ${errorText(error, '잠시 후 다시 시도해 주세요.')}`,
        'fault',
        5000,
      );
    }
  };

  const run = (vm: Vm, kind: VmAction) =>
    vmOperations.run(vm, kind, {
      onSuccess: () => toast.show(`${vm.name}: ${VM_ACTION_LABEL[kind]} 요청 완료`, 'ok'),
      onError: (_error, message) => toast.show(message, 'fault', 5000),
    });

  const items = useMemo(() => sortVms(vms.data?.items ?? []), [vms.data?.items]);
  const runningVms = items.filter((vm) => vm.state === 'running');
  const running = runningVms.length;
  // 사용 중 = 실행 중 VM 의 할당 합계. 등록된 VM 전체 합계는 보조 줄
  const usage = resourceTotals(runningVms);
  const registered = resourceTotals(items);
  const visiblePendingCreations = useMemo(
    () =>
      pendingCreations.filter(
        (creation) => !items.some((vm) => matchesVmCreation(vm, creation) && vm.state === 'running'),
      ),
    [items, pendingCreations],
  );
  const ownedPendingCreations = visiblePendingCreations.filter(isOwnedVmCreation);
  const unknownPendingCreations = visiblePendingCreations.filter((creation) => !isOwnedVmCreation(creation));

  useEffect(() => {
    pendingCreations.forEach((creation) => {
      if (items.some((vm) => matchesVmCreation(vm, creation) && vm.state === 'running')) {
        finishVmCreation(creation.name);
      }
    });
  }, [items, pendingCreations]);

  return (
    <div className={styles.controlPage}>
      <header className={styles.contentHeading}>
        <div>
          <span className={styles.contentEyebrow}>VM CONTROL</span>
          <h2>VM Management</h2>
          <p className={styles.headingDescription}>VM 생성과 생명주기, 할당 자원을 관리합니다.</p>
        </div>
        <div className={styles.controlHeadingActions}>
          {vms.data && (
            <ObservedClock
              at={observedAt(vms.data.observed_at, vms.dataUpdatedAt)}
              receivedAt={vms.dataUpdatedAt}
              refreshing={!vms.isFetchedAfterMount}
            />
          )}
          <Button
            variant="secondary"
            onClick={() => void synchronize()}
            disabled={registrationSync.isPending}
            title="보드를 기준으로 VM 목록을 맞춥니다 (보드에만 있는 VM 추가·복구, 보드에 없는 VM 정리)"
          >
            {registrationSync.isPending ? '동기화 중…' : 'VM 동기화'}
          </Button>
          <Button onClick={() => navigate('/control/vms/new')}>+ VM 생성</Button>
        </div>
      </header>

      <section className={styles.controlSummary} aria-label="VM 자원 요약">
        <SummaryItem label="TOTAL VMS" value={`${items.length}대`} />
        <SummaryItem label="RUNNING" value={`${running}대`} tone="ok" />
        <SummaryItem
          label="VCPU IN USE"
          value={`${usage.vcpus} vCPU`}
          note={`등록 ${registered.vcpus} vCPU · 물리 4코어 공유`}
        />
        <SummaryItem
          label="MEMORY IN USE"
          value={formatMiB(usage.memoryMib)}
          note={`등록 ${formatMiB(registered.memoryMib)}`}
        />
      </section>

      <LiveDataAlert />
      {vms.isPending && <div className={styles.statePanel}>VM 목록을 불러오는 중입니다…</div>}
      {vms.data && items.length === 0 && (
        <div className={styles.controlEmpty}>
          <strong>등록된 VM이 없습니다.</strong>
          <span>새 VM을 생성해 관리 목록에 추가할 수 있습니다.</span>
          <Button onClick={() => navigate('/control/vms/new')}>VM 생성</Button>
        </div>
      )}

      {ownedPendingCreations.length > 0 && (
        <div className={styles.pendingCreationSummary} role="status">
          <i aria-hidden="true" />
          <div>
            <strong>VM 생성 작업 진행 중</strong>
            <span>{ownedPendingCreations.map((creation) => creation.name).join(', ')}</span>
          </div>
          <small>목록을 자동으로 갱신해 완료 상태를 확인합니다.</small>
        </div>
      )}

      {/* 다른 탭·새로고침 전에 보낸 생성 — 결과를 이 탭이 받지 못해 성공·실패를 모른다 (S15P31E110-473) */}
      {unknownPendingCreations.map((creation) => (
        <div
          key={`${creation.startedAt}:${creation.name}`}
          className={styles.pendingCreationSummary}
          data-unknown="true"
          role="status"
        >
          <span aria-hidden="true" />
          <div>
            <strong>생성 결과 확인 필요</strong>
            <span>
              {creation.name} · 이 화면에서 보낸 요청이 아니라 결과를 알 수 없습니다. 목록에서 상태를 확인한
              뒤 기록을 지워 주세요.
            </span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            aria-label={`${creation.name} 생성 기록 지우기`}
            onClick={() => clearVmCreation(creation)}
          >
            기록 지우기
          </Button>
        </div>
      ))}

      <div className={styles.controlGrid}>
        {items.map((vm, index) => {
          const accent = VM_ACCENTS[index % VM_ACCENTS.length];
          const operation = vmOperations.operations[vm.vm_id];
          const operationPending = operation?.status === 'pending';
          const creationPending = isVmCreationPending(vm, pendingCreations);
          // 서버가 "다른 작업 진행 중"(409)을 줬으면 잠시, 이 탭에서 자원 적용 중이면 시작·정지를 잠근다 (S15P31E110-473)
          const controlsDisabled = operationPending || creationPending || vmOperations.isBusy(vm.vm_id);
          const powerDisabled = controlsDisabled || updatingResources.includes(vm.vm_id);
          return (
            <Card
              key={vm.vm_id}
              className={`${styles.controlVmCard} ${creationPending ? '' : styles.clickCard}`}
              style={{ '--vm-accent': accent } as CSSProperties}
              role={creationPending ? undefined : 'link'}
              tabIndex={creationPending ? -1 : 0}
              aria-label={`${vm.name} ${creationPending ? '생성 중' : '상세'}`}
              onClick={() => {
                if (!creationPending) navigate(`/control/vms/${vm.vm_id}`);
              }}
              onKeyDown={(event) => {
                if (!creationPending && event.key === 'Enter' && event.target === event.currentTarget) {
                  navigate(`/control/vms/${vm.vm_id}`);
                }
              }}
            >
              <div className={styles.controlCardBody}>
                <div className={styles.controlCardTop}>
                  <div>
                    <span className={styles.controlCardKicker}>VIRTUAL MACHINE</span>
                    <h3>{vm.name}</h3>
                    <p>{osLabel(vm)}</p>
                  </div>
                  <StatusBadge
                    health={creationPending ? 'recovering' : STATE_HEALTH[vm.state]}
                    label={creationPending ? '생성 중' : STATE_LABEL[vm.state]}
                    size="sm"
                  />
                </div>

                <dl className={styles.controlFacts}>
                  <div>
                    <dt>vCPU</dt>
                    <dd>{vm.vcpus}</dd>
                  </div>
                  <div>
                    <dt>메모리</dt>
                    <dd>{vm.memory_mib} MiB</dd>
                  </div>
                  <div className={styles.controlFactWide}>
                    <dt>템플릿</dt>
                    <dd>
                      {vm.template_id} <small>v{vm.template_version}</small>
                    </dd>
                  </div>
                  <div className={styles.controlFactWide}>
                    <dt>예약 IPv4</dt>
                    <dd>{vm.assigned_ipv4 ?? '—'}</dd>
                  </div>
                </dl>

                <VmOperationNotice operation={operation} now={vmOperations.now} />
                {creationPending && (
                  <div className={styles.vmCreationCardNotice} role="status">
                    <i aria-hidden="true" />
                    <span>dom0에서 VM을 생성하고 있습니다.</span>
                  </div>
                )}

                <div
                  className={styles.controlCardActions}
                  onClick={(event) => event.stopPropagation()}
                  onKeyDown={(event) => event.stopPropagation()}
                >
                  <div>
                    <Button
                      size="sm"
                      onClick={() => run(vm, 'start')}
                      disabled={!canStartVm(vm.state) || powerDisabled}
                    >
                      시작
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => run(vm, 'stop')}
                      disabled={!canStopVm(vm.state) || powerDisabled}
                    >
                      정지
                    </Button>
                  </div>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => setDeleting(vm)}
                    disabled={!canDeleteVm(vm.state) || controlsDisabled}
                    title={canDeleteVm(vm.state) ? undefined : DELETE_BLOCKED_HINT}
                  >
                    삭제
                  </Button>
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      <Modal
        open={deleting !== null}
        title="VM 삭제"
        onClose={() => setDeleting(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleting(null)}>
              취소
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (deleting) run(deleting, 'delete');
                setDeleting(null);
              }}
            >
              VM 삭제
            </Button>
          </>
        }
      >
        {deleting && (
          <div className={styles.deleteConfirm}>
            <span aria-hidden="true">!</span>
            <div>
              <strong>‘{deleting.name}’ VM을 삭제하시겠습니까?</strong>
              <p>{DELETE_CONFIRM_TEXT}</p>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

function SummaryItem({
  label,
  value,
  tone,
  note,
}: {
  label: string;
  value: string;
  tone?: 'ok';
  /** 값 아래 작은 보조 줄 */
  note?: string;
}) {
  return (
    <div className={styles.controlSummaryItem} data-tone={tone}>
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}

/** VM 들의 vCPU·메모리 할당 합계 */
function resourceTotals(vms: readonly Pick<Vm, 'vcpus' | 'memory_mib'>[]) {
  return vms.reduce(
    (sum, vm) => ({ vcpus: sum.vcpus + vm.vcpus, memoryMib: sum.memoryMib + vm.memory_mib }),
    { vcpus: 0, memoryMib: 0 },
  );
}

const formatMiB = (mib: number) => `${mib.toLocaleString('ko-KR')} MiB`;

/** 등록 동기화 결과를 한 줄로. 바뀐 것이 없으면 그렇게 말한다 */
function synchronizationSummary(result: VmSynchronizationResult): string {
  const parts = [
    result.registered && `추가 ${result.registered}`,
    result.restored && `복구 ${result.restored}`,
    result.updated && `갱신 ${result.updated}`,
    result.retired && `정리 ${result.retired}`,
    result.skipped && `보류 ${result.skipped}`,
    result.conflicts && `충돌 ${result.conflicts}`,
    result.incomplete && `정보 부족 ${result.incomplete}`,
  ].filter(Boolean);
  return parts.length === 0
    ? `VM 동기화 완료: 보드 ${result.discovered}대, 바뀐 것 없음`
    : `VM 동기화 완료: 보드 ${result.discovered}대 · ${parts.join(' · ')}`;
}

const SYNC_REASON_LABEL: Record<string, string> = {
  in_operation: '작업 진행 중이라 이번엔 정리하지 않음',
  duplicate_name: '이미 다른 VM이 쓰는 이름',
  duplicate_board_vm_id: '보드에 같은 UUID가 두 번 있음',
  invalid_vm_id: 'UUID 형식이 아님',
  registration_metadata_missing: '보드 등록 정보가 부족함',
};

/**
 * 사용자가 확인해야 할 항목(보류·충돌·정보 부족)을 VM 이름과 사유로. 화면에 없는 VM 이라 이름이 없으면 UUID 앞부분,
 * 보드가 vm_id 도 주지 않았으면(invalid_vm_id, 백엔드 VmSynchronizationService) "알 수 없는 VM" (S15P31E110-473)
 */
function synchronizationProblems(result: VmSynchronizationResult): string[] {
  return result.results
    .filter((item) => item.action === 'skipped' || item.action === 'conflict' || item.action === 'incomplete')
    .map((item) => {
      const who = item.name ?? item.vm_id?.slice(0, 8) ?? '알 수 없는 VM';
      const why = (item.reason && SYNC_REASON_LABEL[item.reason]) ?? item.reason ?? item.action;
      return `${who}: ${why}`;
    });
}

function SynchronizationToast({ result }: { result: VmSynchronizationResult }) {
  const problems = synchronizationProblems(result);
  return (
    <div>
      <div>{synchronizationSummary(result)}</div>
      {problems.length > 0 && (
        <ul className={styles.syncProblems} aria-label="동기화 확인 필요">
          {/* 같은 이름·같은 사유가 겹칠 수 있다 (duplicate_board_vm_id) — key 가 겹치지 않게 순서를 섞는다 */}
          {problems.map((problem, index) => (
            <li key={`${index}:${problem}`}>{problem}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 서버가 dom0 목록을 받은 시각(observed_at). 읽을 수 없으면 화면이 응답을 받은 시각 */
function observedAt(iso: string | undefined, fallback: number): number {
  const parsed = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** at: 서버가 dom0 목록을 받은 시각, receivedAt: 화면이 그 응답을 받은 시각 (둘의 차이가 서버 목록의 나이) */
function ObservedClock({
  at,
  receivedAt,
  refreshing,
}: {
  at: number;
  receivedAt: number;
  refreshing: boolean;
}) {
  const time = new Date(at).toTimeString().slice(0, 8);
  const ageSeconds = Math.max(0, Math.round((receivedAt - at) / 1000));
  return (
    <span className={styles.observedTime}>
      마지막 확인
      <b key={time} className={styles.tick}>
        {time}
      </b>
      {refreshing ? ' · 갱신 중' : ageSeconds >= 3 ? ` · ${ageSeconds}초 전` : ''}
    </span>
  );
}
