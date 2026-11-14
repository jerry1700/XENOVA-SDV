import { Card } from '@xenova/ui';
import { lazy, Suspense, type CSSProperties } from 'react';
import { Link, useParams } from 'react-router';
import { ApiError } from '../api/client';
import { useEvents, useVm, vmGone } from '../api/queries';
import { AGENT_LABEL, STATE_HEALTH, STATE_LABEL } from '../format';
import { MODE_LABEL, osLabel, roleOf } from '../domain/roles';
import { observedControl, vmAppObservations } from '../domain/driveObservations';
import { useDrive } from '../layout/DriveContext';
import { AGENT_HEALTH } from './collectorStatus';
import { PublisherStatus, StatusDot } from './PublisherStatus';
import { StressTestPanel } from './StressTestPanel';
import { DetailEvents } from './VmDetailEvents';
import { DetailCharts, ExecutionTable } from './VmDetailTelemetry';
import styles from './pages.module.css';

const ConsolePanel = lazy(() =>
  import('../console/ConsolePanel').then((module) => ({ default: module.ConsolePanel })),
);

/** vmAppObservations 가 돌려주는 신선한 heartbeat 문구 (domain/driveObservations HEARTBEAT 와 같은 값) */
const FRESH_HEARTBEAT = new Set(['동작 중', '대기', '유휴', '오류']);
/** lifecycle 이 신선하지 않을 때 phaseLabel 로 오는 문구 */
const PHASE_UNAVAILABLE = new Set(['미수신', '관측 만료', '연결 끊김']);

/**
 * 운영 상태 타일 중 앱 상태. 앱마다 한 줄, 줄 앞에 앱 한글 라벨 ("자율주행 · 대기 · 준비 미충족 · 상태 준비 중").
 * 사유는 한글로 보이고 원문 코드는 title 로 남긴다.
 */
function AppStateTile({
  vmId,
  drive,
  socket,
}: {
  vmId: string;
  drive: Parameters<typeof vmAppObservations>[1];
  socket: Parameters<typeof vmAppObservations>[2];
}) {
  const reports = vmAppObservations(vmId, drive, socket);
  return (
    <div>
      <span>앱 상태</span>
      {reports.length > 0 ? (
        reports.map(({ appId, appLabel, stage, reason, reasonLabel, phaseLabel, heartbeatLabel }) =>
          // 단계(lifecycle)가 없고 heartbeat 가 신선하면 heartbeat 문구로 대신 보이고 출처를 작게 적는다
          PHASE_UNAVAILABLE.has(phaseLabel) && FRESH_HEARTBEAT.has(heartbeatLabel) ? (
            <strong key={appId} data-app={appId}>
              {appLabel} · {heartbeatLabel}
              <small className={styles.detailTileNote}>heartbeat 기준 · 단계 {phaseLabel}</small>
            </strong>
          ) : (
            <strong key={appId} data-app={appId}>
              {appLabel} · {stage}
              {reason && (
                <>
                  {' · '}
                  <span title={reason}>{reasonLabel}</span>
                </>
              )}
            </strong>
          ),
        )
      ) : (
        <strong>미확인</strong>
      )}
    </div>
  );
}

/** S15P31E110-299: 제어 작업과 분리한 관측 중심 VM 상세 화면. */
/** 조회가 실패했는데 '불러오는 중'이 남지 않게 (QA S15P31E110-469) */
const VM_LOAD_FAILED_TEXT = 'VM 정보를 불러오지 못했습니다.';

export function VmDetailPage() {
  const { vmId = '' } = useParams();
  const vm = useVm(vmId);
  // 없는 VM(404)이면 이 VM 의 이벤트도 다시 읽지 않는다 (S15P31E110-469)
  const events = useEvents({ vm_id: vmId, limit: 8 }, { enabled: !vmGone(vm.error) });
  const { drive, socket } = useDrive();
  const role = vm.data ? roleOf(vm.data, drive) : null;
  const control = observedControl(drive, socket);
  const color = role?.color ?? 'var(--x-primary)';
  const authority = !role?.driving
    ? '해당 없음'
    : !control
      ? '미확인'
      : control.active_vm_id
        ? control.active_vm_id === vm.data?.vm_id
          ? 'ACTIVE'
          : 'STANDBY'
        : control.active_app
          ? '미확인'
          : 'NONE';
  // 머리말 배지: 주행 VM 은 제어권, 인터랙션 VM 은 INFOTAINMENT (Memory·Network 탭 카드와 같은 배지), 역할 미지정은 없음
  const titleBadge = !role ? null : role.driving ? authority : 'INFOTAINMENT';

  return (
    <div className={styles.vmDetailPage} style={{ '--vm-accent': color } as CSSProperties}>
      <header className={styles.controlDetailHero}>
        <div className={styles.controlDetailTop}>
          <Link className={styles.controlBackLink} to="/dashboard/overview">
            <span aria-hidden="true">←</span>
            <strong>VM Dashboard</strong>
          </Link>
          {vm.data && (
            <span className={styles.controlDetailStatus} data-health={STATE_HEALTH[vm.data.state]}>
              <i aria-hidden="true" />
              {STATE_LABEL[vm.data.state]}
            </span>
          )}
        </div>
        <div className={styles.controlDetailTitle}>
          <span className={styles.controlCardKicker}>VIRTUAL MACHINE MONITORING</span>
          {/* 제목 줄: 이름 + 역할 배지 (좁으면 배지가 다음 줄로) */}
          <div className={styles.detailTitleRow}>
            <h1>{vm.data?.name ?? 'VM 상세'}</h1>
            {titleBadge && (
              <span className={styles.authority} data-authority={titleBadge.toLowerCase()}>
                {titleBadge}
              </span>
            )}
          </div>
          <p>
            {role?.title ?? '역할 미지정'} ·{' '}
            {vm.data ? osLabel(vm.data) : vm.error ? VM_LOAD_FAILED_TEXT : 'VM 정보를 불러오는 중입니다.'}
          </p>
        </div>
      </header>

      {vm.isPending && <div className={styles.statePanel}>VM 상세 정보를 불러오는 중입니다…</div>}
      {vm.error && (
        <div className={styles.statePanel} role="alert">
          {vm.error instanceof ApiError ? `${vm.error.message} (${vm.error.code})` : String(vm.error)}
        </div>
      )}

      {vm.data && (
        <>
          <div className={styles.vmDetailGrid}>
            <Card title="운영 상태" accent={color}>
              {/* 공통 타일: 옅은 배경 + 작은 회색 라벨 + 큰 값. 기술 값만 data-mono 로 고정폭 */}
              <div className={styles.detailTiles} data-cols="2">
                <div>
                  <span>VM 상태</span>
                  <strong>
                    <StatusDot health={STATE_HEALTH[vm.data.state]}>{STATE_LABEL[vm.data.state]}</StatusDot>
                  </strong>
                </div>
                {/* 주행 VM 이 아니면 제어권·주행 모드는 "해당 없음" (타일 배치는 VM 마다 같게) */}
                <div>
                  <span>제어권</span>
                  <strong>{authority}</strong>
                </div>
                <div>
                  <span>주행 모드</span>
                  <strong>
                    {role?.driving ? (control?.mode ? MODE_LABEL[control.mode] : '미확인') : '해당 없음'}
                  </strong>
                </div>
                <AppStateTile vmId={vm.data.vm_id} drive={drive} socket={socket} />
              </div>
              <PublisherStatus vmId={vm.data.vm_id} />
              {vm.data.guest_kind === 'linux' && (
                <div className={styles.collectorLine}>
                  <div>
                    <span className={styles.collectorTitle}>Linux Agent 생존 (v1)</span>
                    <StatusDot health={AGENT_HEALTH[vm.data.agent_status ?? 'UNKNOWN']}>
                      {AGENT_LABEL[vm.data.agent_status ?? 'UNKNOWN']}
                    </StatusDot>
                  </div>
                </div>
              )}
            </Card>

            <Card title="VM 구성">
              <div className={styles.detailTiles} data-cols="3">
                <div>
                  <span>vCPU</span>
                  <strong data-mono="true">{vm.data.vcpus}</strong>
                </div>
                <div>
                  <span>메모리</span>
                  <strong data-mono="true">
                    {vm.data.memory_mib}
                    <small>MiB</small>
                  </strong>
                </div>
                <div>
                  <span>예약 IPv4</span>
                  <strong data-mono="true">{vm.data.assigned_ipv4 ?? '—'}</strong>
                </div>
              </div>
              {/* 라벨 고정 폭 열 + 값 바로 옆, 얇은 구분선 */}
              <div className={styles.detailFacts}>
                <div>
                  <span>역할</span>
                  <span>{role?.title ?? '역할 미지정'}</span>
                </div>
                <div>
                  <span>템플릿</span>
                  <span data-mono="true">{vm.data.template_id}</span>
                </div>
                <div>
                  <span>버전</span>
                  <span data-mono="true">{vm.data.template_version}</span>
                </div>
                <div>
                  <span>VM ID</span>
                  <span data-mono="true">{vm.data.vm_id}</span>
                </div>
              </div>
            </Card>
          </div>

          <Card title="CPU·메모리 추이 · 최근 5분">
            <DetailCharts vm={vm.data} />
          </Card>

          {vm.data.guest_kind === 'linux' && <StressTestPanel vm={vm.data} accent={color} />}

          <Card title={vm.data.guest_kind === 'zephyr' ? '스레드' : '프로세스'}>
            <ExecutionTable vm={vm.data} />
          </Card>

          <Card title="최근 생명주기·복구 이벤트">
            <DetailEvents
              events={events.data?.items ?? []}
              pending={events.isPending}
              error={events.error != null}
            />
          </Card>

          <Suspense fallback={<div className={styles.statePanel}>콘솔을 준비하는 중입니다…</div>}>
            <ConsolePanel key={vm.data.vm_id} vm={vm.data} />
          </Suspense>
        </>
      )}
    </div>
  );
}
