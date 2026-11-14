import { Button, Card, Modal, useToast } from '@xenova/ui';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { ApiError } from '../api/client';
import { errorText } from '../api/errorText';
import { useVm, useVmResources, useVms, type VmAction, useVmIdsUpdatingResources } from '../api/queries';
import type { Vm } from '../api/types';
import { osLabel, sortVms } from '../domain/roles';
import {
  AGENT_LABEL,
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
import { ResourceAdjustmentPanel } from './ResourceAdjustmentPanel';
import { VcpuPinningPanel } from './VcpuPinningPanel';
import { VmOperationNotice } from './VmOperationProgress';
import { useVmOperationProgress } from './useVmOperationProgress';
import { LiveDataAlert } from '../layout/LiveDataAlert';

const VM_ACCENTS = ['var(--x-app-autonomous)', 'var(--x-app-manual)', 'var(--x-app-interaction)'] as const;

/** Dashboard의 관측 상세와 분리된 VM Control 전용 관리 상세. */
/** 조회가 실패했는데 '불러오는 중'이 남지 않게 (QA S15P31E110-469) */
const VM_LOAD_FAILED_TEXT = 'VM 정보를 불러오지 못했습니다.';

export function ControlVmDetailPage() {
  const { vmId = '' } = useParams();
  const vm = useVm(vmId);
  const vms = useVms();
  const vmOperations = useVmOperationProgress();
  const toast = useToast();
  const navigate = useNavigate();
  const [deleteOpen, setDeleteOpen] = useState(false);
  // 삭제 요청은 길게 걸릴 수 있다. 그사이 다른 화면으로 갔으면 끝났을 때 /control 로 끌고 가지 않는다
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const index = sortVms(vms.data?.items ?? []).findIndex((item) => item.vm_id === vmId);
  const accent = (index >= 0 ? VM_ACCENTS[index % VM_ACCENTS.length] : undefined) ?? 'var(--x-primary)';

  const run = (target: Vm, kind: VmAction) =>
    vmOperations.run(target, kind, {
      onSuccess: () => {
        toast.show(`${target.name}: ${VM_ACTION_LABEL[kind]} 요청 완료`, 'ok');
        if (kind === 'delete' && mounted.current) navigate('/control');
      },
      onError: (_error, message) => toast.show(message, 'fault', 5000),
    });

  const operation = vmOperations.operations[vmId];
  const operationPending = operation?.status === 'pending';
  // 서버가 "다른 작업 진행 중"(409)을 줬으면 10초 잠그고, 이 탭에서 자원 적용 중이면 시작·정지를 잠근다 (S15P31E110-473).
  // 핀닝 적용 중은 서버 409 로만 막는다 (VcpuPinningPanel 은 이 이슈에서 건드리지 않음)
  const busy = vmOperations.isBusy(vmId);
  const updatingResources = useVmIdsUpdatingResources().includes(vmId);
  const actionsLocked = operationPending || busy;
  const powerLocked = actionsLocked || updatingResources;
  // Zephyr 템플릿은 1 vCPU · 16 MiB 고정 프로필 (ResourceAdjustmentPanel 의 fixedProfile 과 같은 판단)
  const zephyr = vm.data?.guest_kind === 'zephyr';

  return (
    <div className={styles.controlDetailPage} style={{ '--vm-accent': accent } as CSSProperties}>
      <header className={styles.controlDetailHero}>
        <div className={styles.controlDetailTop}>
          <Link className={styles.controlBackLink} to="/control">
            <span aria-hidden="true">←</span>
            <strong>VM Management</strong>
          </Link>
          {vm.data && (
            <span className={styles.controlDetailStatus} data-health={STATE_HEALTH[vm.data.state]}>
              <i aria-hidden="true" />
              {STATE_LABEL[vm.data.state]}
            </span>
          )}
        </div>
        <div className={styles.controlDetailTitle}>
          <span className={styles.controlCardKicker}>VIRTUAL MACHINE CONTROL</span>
          <h1>{vm.data?.name ?? 'VM 관리 상세'}</h1>
          <p>
            {vm.data
              ? `${osLabel(vm.data)} · ${vm.data.template_id}`
              : vm.error
                ? VM_LOAD_FAILED_TEXT
                : 'VM 정보를 불러오는 중입니다.'}
          </p>
        </div>
      </header>

      {vm.isPending && <div className={styles.statePanel}>VM 관리 정보를 불러오는 중입니다…</div>}
      <LiveDataAlert />
      {vm.error instanceof ApiError && vm.error.status === 404 && (
        <div className={styles.statePanel} role="alert" title={vm.error.code}>
          {errorText(vm.error, VM_LOAD_FAILED_TEXT)}
        </div>
      )}

      {vm.data && (
        <>
          <div className={styles.controlDetailGrid}>
            <Card title="VM 기본 정보" accent={accent}>
              <div className={styles.identityGrid}>
                <div className={styles.identityWide}>
                  <span className={styles.identityLabel}>VM ID</span>
                  <div className={styles.identityId}>
                    <code>{vm.data.vm_id}</code>
                  </div>
                </div>
                <div className={styles.identityFact}>
                  <span className={styles.identityLabel}>운영체제</span>
                  <strong>{osLabel(vm.data)}</strong>
                </div>
                <div className={styles.identityFact}>
                  <span className={styles.identityLabel}>예약 IPv4</span>
                  <strong>{vm.data.assigned_ipv4 ?? '—'}</strong>
                </div>
                <div className={styles.identityFact}>
                  <span className={styles.identityLabel}>템플릿</span>
                  <strong>{osLabel(vm.data)}</strong>
                  <code>
                    {vm.data.template_id} · v{vm.data.template_version}
                  </code>
                </div>
                <div className={styles.identityFact}>
                  <span className={styles.identityLabel}>Agent</span>
                  <span
                    className={styles.identityStatus}
                    data-tone={
                      vm.data.agent_status === 'ONLINE'
                        ? 'ok'
                        : vm.data.agent_status === 'STALE'
                          ? 'warning'
                          : vm.data.agent_status === 'OFFLINE'
                            ? 'fault'
                            : 'waiting'
                    }
                  >
                    <i aria-hidden="true" />
                    {AGENT_LABEL[vm.data.agent_status ?? 'UNKNOWN']}
                  </span>
                </div>
              </div>
            </Card>

            <Card title="생명주기 제어">
              <div className={styles.lifecycleState}>
                <span>현재 상태</span>
                <strong>{STATE_LABEL[vm.data.state]}</strong>
                <small>명령 실행 후 서버에서 다시 조회한 상태가 화면에 반영됩니다.</small>
              </div>
              <VmOperationNotice operation={operation} now={vmOperations.now} />
              {(busy || updatingResources) && (
                <p className={styles.dim} role="status">
                  {busy
                    ? '다른 작업이 진행 중이라 잠시 뒤 다시 누를 수 있습니다.'
                    : '자원을 적용하는 중이라 시작·정지를 잠시 막습니다.'}
                </p>
              )}
              <div className={styles.lifecycleActions}>
                <Button
                  onClick={() => run(vm.data, 'start')}
                  disabled={!canStartVm(vm.data.state) || powerLocked}
                >
                  VM 시작
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => run(vm.data, 'stop')}
                  disabled={!canStopVm(vm.data.state) || powerLocked}
                >
                  VM 정지
                </Button>
              </div>
            </Card>
          </div>

          <Card title="할당 자원">
            <div className={styles.controlResourceSummary}>
              <div data-available="true">
                <span>vCPU</span>
                <strong>{vm.data.vcpus}</strong>
                <small>개</small>
              </div>
              <div data-available="true">
                <span>메모리</span>
                <strong>{vm.data.memory_mib}</strong>
                <small>MiB</small>
              </div>
              <PinningSummary vmId={vm.data.vm_id} />
            </div>
          </Card>

          {/* Zephyr는 1 vCPU · 16 MiB 고정 프로필이라 vCPU·메모리 조정은 Linux VM만.
              핀닝은 Zephyr도 전용 코어를 바꿀 수 있어 모든 VM에 둔다 (백엔드 PUT /resources/vcpu-pinning) */}
          {!zephyr && (
            <ResourceAdjustmentPanel key={`tuning-${vm.data.vm_id}`} vm={vm.data} accent={accent} />
          )}
          <VcpuPinningPanel key={`pinning-${vm.data.vm_id}`} vm={vm.data} accent={accent} />

          <section className={styles.dangerZone} aria-labelledby="danger-zone-title">
            <div>
              <span>DANGER ZONE</span>
              <h2 id="danger-zone-title">VM 삭제</h2>
              <p>{DELETE_CONFIRM_TEXT}</p>
              {!canDeleteVm(vm.data.state) && <p role="note">{DELETE_BLOCKED_HINT}</p>}
            </div>
            <Button
              variant="danger"
              onClick={() => setDeleteOpen(true)}
              disabled={!canDeleteVm(vm.data.state) || actionsLocked}
              title={canDeleteVm(vm.data.state) ? undefined : DELETE_BLOCKED_HINT}
            >
              VM 삭제
            </Button>
          </section>
        </>
      )}

      <Modal
        open={deleteOpen}
        title="VM 삭제"
        onClose={() => setDeleteOpen(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteOpen(false)}>
              취소
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (vm.data) run(vm.data, 'delete');
                setDeleteOpen(false);
              }}
            >
              VM 삭제
            </Button>
          </>
        }
      >
        {vm.data && (
          <div className={styles.deleteConfirm}>
            <span aria-hidden="true">!</span>
            <div>
              <strong>‘{vm.data.name}’ VM을 삭제하시겠습니까?</strong>
              <p>{DELETE_CONFIRM_TEXT}</p>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

/** 할당 자원 카드의 핀닝 칸. 핀닝 패널과 같은 조회(useVmResources)를 공유한다 */
function PinningSummary({ vmId }: { vmId: string }) {
  const resources = useVmResources(vmId);
  const pcpus = resources.data?.vcpu_pinning[0]?.pcpus;
  return (
    <div data-available={pcpus ? 'true' : 'false'}>
      <span>vCPU 핀닝</span>
      <strong>{pcpus ? pcpus.join(', ') : '—'}</strong>
      <small>{pcpus ? 'pCPU' : resources.error ? '조회 실패' : '조회 중'}</small>
    </div>
  );
}
