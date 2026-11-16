import { Button, Card, Modal, StatusBadge } from '@xenova/ui';
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Link } from 'react-router';
import { ApiError } from '../api/client';
import { commandErrorCode } from '../api/commandStatus';
import type { DriveMode } from '../api/drive';
import { agentErrorText, commandStateText } from '../api/errorText';
import {
  useCommandStatus,
  useEvents,
  useMetrics,
  usePerceptionFaultOperation,
  useStartActiveVmCrash,
  useStartActiveDriveAutoDelay,
  useStartActiveAppExit,
  useStartActiveAutoTxBlock,
  useStartStress,
  useStartAutoInputUnavailable,
  useStartPerceptionBlock,
  useVms,
} from '../api/queries';
import type { PerceptionFaultOperation, Vm } from '../api/types';
import { MODE_HEALTH, MODE_LABEL, ROLES } from '../domain/roles';
import { appReasonLabel } from '../domain/appLabels';
import { cpuPercentOf, latest, memoryPercentOf } from '../domain/vmMetrics';
import { STATE_HEALTH, STATE_LABEL } from '../format';
import { useDrive } from '../layout/DriveContext';
import styles from './pages.module.css';

type ScenarioId =
  'vm-crash' | 'delay' | 'interaction-stress' | 'app-exit' | 'tx-block' | 'jetson-loss' | 'dual-fault';
const SCENARIOS: { id: ScenarioId; label: string; title: string; ready: boolean }[] = [
  { id: 'vm-crash', label: '시연 1', title: '주행 VM 장애', ready: true },
  { id: 'delay', label: '시연 2', title: '연산 지연 반복', ready: true },
  { id: 'interaction-stress', label: '시연 3', title: 'Interaction 자원 폭주', ready: true },
  { id: 'app-exit', label: '시연 4', title: '자율주행 앱 장애', ready: true },
  { id: 'tx-block', label: '시연 5', title: '결과 송신 장애', ready: true },
  { id: 'jetson-loss', label: '시연 6', title: 'Jetson 인지 장애', ready: true },
  { id: 'dual-fault', label: '시연 7', title: 'AUTO 이중 장애', ready: true },
];

interface Run {
  targetId: string;
  targetName: string;
  initialMode: DriveMode | null;
  commandId: string;
  startedAt: string;
}

interface CrashRun extends Run {
  initialBootId: string | null;
}

interface DelayRun extends Run {
  delayMs: number;
  durationS: number;
  initialGeneration: number | null;
}

const DELAY_SCENARIO = { delay_ms: 100, duration_s: 30 } as const;

export function failureText(error: unknown): string {
  if (
    error instanceof ApiError &&
    (error.status === 404 || error.status === 405) &&
    error.code !== 'VM_NOT_FOUND'
  )
    return '배포된 백엔드에 장애 시연 API가 아직 없습니다.';
  return error instanceof Error ? error.message : '요청이 실패했습니다.';
}

export function perceptionFaultErrorText(error: unknown): string {
  if (error instanceof ApiError && (error.status === 404 || error.status === 405))
    return '배포된 백엔드에 Jetson 인지 단절 시연 API가 아직 없습니다.';
  return error instanceof Error ? error.message : 'Jetson 인지 단절 요청이 실패했습니다.';
}

export function DemoPage() {
  const [selected, setSelected] = useState<ScenarioId>('vm-crash');
  const scenario = SCENARIOS.find((item) => item.id === selected)!;
  return (
    <div className={styles.page}>
      <section className={styles.pageHead}>
        <div>
          <div className={styles.eyebrow}>Simulator / Fault demo</div>
          <h1>장애 시연</h1>
          <p className={styles.dim}>장애 발생부터 주행 제어권 승계와 VM 복구까지 한 화면에서 확인합니다.</p>
        </div>
      </section>
      <nav className={styles.simulatorTabs} aria-label="Simulator 하위 메뉴">
        <Link to="/simulator" aria-current="page">
          장애 시연
        </Link>
        <Link to="/simulator/stress">부하 테스트</Link>
      </nav>
      <nav className={styles.scenarioTabs} aria-label="장애 시연 선택">
        {SCENARIOS.map((item) => (
          <button
            type="button"
            key={item.id}
            aria-pressed={selected === item.id}
            onClick={() => setSelected(item.id)}
          >
            <span>{item.label}</span>
            <strong>{item.title}</strong>
            {!item.ready && <small>준비 중</small>}
          </button>
        ))}
      </nav>
      {scenario.ready ? (
        scenario.id === 'vm-crash' ? (
          <VmCrashScenario />
        ) : scenario.id === 'delay' ? (
          <DelayScenario />
        ) : scenario.id === 'interaction-stress' ? (
          <InteractionStressScenario />
        ) : scenario.id === 'app-exit' ? (
          <AppExitScenario />
        ) : scenario.id === 'tx-block' ? (
          <TxBlockScenario />
        ) : scenario.id === 'jetson-loss' ? (
          <JetsonLossScenario />
        ) : scenario.id === 'dual-fault' ? (
          <DualFaultScenario />
        ) : (
          <Card title={`${scenario.label} · ${scenario.title}`}>
            <div className={styles.scenarioEmpty}>
              <StatusBadge health="stopped" label="준비 중" size="sm" />
              <p>시연 조건과 복구 판정 API가 연결되면 이 탭에 전용 제어 및 관측 화면이 추가됩니다.</p>
            </div>
          </Card>
        )
      ) : (
        <Card title={`${scenario.label} · ${scenario.title}`}>
          <div className={styles.scenarioEmpty}>
            <StatusBadge health="stopped" label="준비 중" size="sm" />
            <p>시연 조건과 복구 판정 API가 연결되면 이 탭에 전용 제어 및 관측 화면이 추가됩니다.</p>
          </div>
        </Card>
      )}
    </div>
  );
}

function DelayScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<DelayRun | null>(null);
  const [handoffTimedOut, setHandoffTimedOut] = useState(false);
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const controlFresh = socket === 'open' && control != null && drive?.source?.status === 'fresh';
  const start = useStartActiveDriveAutoDelay();
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);

  const commandAccepted = Boolean(
    run && command.data && ['ACCEPTED', 'RUNNING', 'SUCCEEDED'].includes(command.data.status),
  );
  const delayRunning = command.data?.status === 'RUNNING' || command.data?.status === 'SUCCEEDED';
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  useEffect(() => {
    if (
      !run ||
      command.data?.status !== 'SUCCEEDED' ||
      !command.data.finished_at ||
      activeId !== run.targetId
    )
      return;
    const elapsed = Date.now() - Date.parse(command.data.finished_at);
    const timer = window.setTimeout(() => setHandoffTimedOut(true), Math.max(0, 3_000 - elapsed));
    return () => window.clearTimeout(timer);
  }, [activeId, command.data?.finished_at, command.data?.status, run]);

  const handoffGraceExpired = Boolean(
    handoffTimedOut && command.data?.status === 'SUCCEEDED' && activeId === run?.targetId,
  );
  const sawHandoff = Boolean(run && activeId && activeId !== run.targetId);
  const modePreserved = Boolean(run && sawHandoff && control?.mode === 'AUTO');
  const targetReport = run ? drive?.vm_apps?.[run.targetId]?.autonomous : null;
  const targetLifecycle = targetReport?.lifecycle_source.status === 'fresh' ? targetReport.lifecycle : null;
  const realtimeDegraded = Boolean(
    run && targetLifecycle?.readiness === 'NOT_READY' && targetLifecycle.reason === 'CALCULATION_FAILED',
  );
  const targetAlive = Boolean(run && drivingVms.find((vm) => vm.vm_id === run.targetId)?.state === 'running');
  const activeCommandGeneration = drive?.apps?.autonomous?.last_command?.generation ?? null;
  const controlGeneration = control?.generation ?? null;
  const appliedGeneration = drive?.vehicle?.applied_generation ?? null;
  const generationConsistent = Boolean(
    modePreserved &&
    controlGeneration != null &&
    activeCommandGeneration === controlGeneration &&
    (appliedGeneration == null || appliedGeneration === controlGeneration),
  );
  const returnedAsStandby = Boolean(
    command.data?.status === 'SUCCEEDED' &&
    sawHandoff &&
    targetAlive &&
    targetLifecycle?.phase === 'WAITING' &&
    targetLifecycle.readiness === 'QUALIFIED',
  );
  const finished = sawHandoff && modePreserved && generationConsistent && returnedAsStandby;
  const failed = commandFailed || handoffGraceExpired;
  const unavailableReason = !controlFresh
    ? '주행 제어 상태가 최신이 아닙니다.'
    : control?.mode !== 'AUTO'
      ? '자동주행 중에만 연산 지연 시연을 시작할 수 있습니다.'
      : !activeVm
        ? '현재 활성 주행 VM을 VM 목록에서 찾지 못했습니다.'
        : activeVm.guest_kind !== 'zephyr'
          ? '현재 활성 VM이 Zephyr가 아닙니다.'
          : activeVm.state !== 'running'
            ? '현재 활성 VM이 실행 중이 아닙니다.'
            : null;

  const begin = () => {
    if (!activeVm || start.isPending) return;
    setRun(null);
    setHandoffTimedOut(false);
    start.mutate(DELAY_SCENARIO, {
      onSuccess: (submitted) =>
        setRun({
          targetId: submitted.vm_id,
          targetName: activeVm.name,
          initialMode: control?.mode ?? null,
          commandId: submitted.command_id,
          startedAt: submitted.submitted_at,
          delayMs: submitted.delay_ms,
          durationS: submitted.duration_s,
          initialGeneration: control?.generation ?? null,
        }),
    });
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 2</span>
          <h2>반복 연산 지연과 주행 제어권 승계</h2>
          <p>현재 AUTO 주행 VM에 100ms 지연을 반복해 deadline miss와 백업 VM 승계를 관측합니다.</p>
        </div>
        <Button
          variant="danger"
          disabled={Boolean(unavailableReason) || start.isPending || Boolean(run && !finished && !failed)}
          title={unavailableReason ?? undefined}
          onClick={() => setConfirmOpen(true)}
        >
          {start.isPending ? '지연 명령 전송 중…' : finished || failed ? '시연 다시 시작' : '시연 시작'}
        </Button>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {failureText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary label="현재 활성 VM" value={activeVm?.name ?? '미확인'} mono={activeVm?.vm_id.slice(0, 8)} />
        <Summary
          label="주행 모드"
          value={control?.mode ? MODE_LABEL[control.mode] : '미확인'}
          health={control?.mode ? MODE_HEALTH[control.mode] : 'stopped'}
        />
        <Summary
          label="지연 프리셋"
          value={`${DELAY_SCENARIO.delay_ms}ms · ${DELAY_SCENARIO.duration_s}초`}
          health={run && !finished && !failed ? 'recovering' : finished ? 'ok' : 'stopped'}
        />
      </div>
      <Card title="주행 VM 상태">
        <div className={styles.drivingVmGrid}>
          {drivingVms.length ? (
            drivingVms.map((vm) => <DrivingVm key={vm.vm_id} vm={vm} active={vm.vm_id === activeId} />)
          ) : (
            <p className={styles.dim}>주 구동·백업 역할이 지정된 Zephyr VM이 없습니다.</p>
          )}
        </div>
      </Card>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>시연을 시작하면 연산 지연 실행부터 AUTO 제어권 승계까지 실제 관측 결과가 표시됩니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                시작 대상 <b>{run.targetName}</b>
              </span>
              <span>
                {run.delayMs}ms · {run.durationS}초
              </span>
              <span className={styles.mono}>{run.commandId.slice(0, 8)}</span>
              <time>{new Date(run.startedAt).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={commandAccepted}
                failed={commandFailed}
                title="연산 지연 명령 수락"
                detail={
                  command.data
                    ? commandFailed
                      ? agentErrorText(commandErrorCode(command.data))
                      : commandStateText(command.data.status)
                    : 'Zephyr 명령 응답 대기 중'
                }
              />
              <ProgressStep
                done={delayRunning}
                failed={commandFailed}
                title="반복 지연 실행"
                detail={
                  delayRunning
                    ? `${run.delayMs}ms 지연을 적용하고 있습니다.`
                    : '명령 실행 상태를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={realtimeDegraded}
                failed={handoffGraceExpired && !realtimeDegraded}
                title="Deadline miss와 준비 상태 악화"
                detail={
                  realtimeDegraded
                    ? `${targetLifecycle?.phase} · ${appReasonLabel(targetLifecycle?.reason ?? 'CALCULATION_FAILED')}`
                    : '활성 VM의 CALCULATION_FAILED lifecycle 보고를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={sawHandoff}
                failed={handoffGraceExpired}
                title="백업 VM으로 제어권 승계"
                detail={
                  sawHandoff
                    ? `${run.targetName}에서 ${activeVm?.name ?? activeId}으로 전환됐습니다.`
                    : handoffGraceExpired
                      ? '지연 명령 종료 후 유예 시간 내 active_vm_id 변경을 확인하지 못했습니다.'
                      : '반복 deadline miss 판정과 active_vm_id 변경을 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={modePreserved}
                failed={sawHandoff && control?.mode !== 'AUTO'}
                title="AUTO 주행 유지"
                detail={`시작: ${run.initialMode ? MODE_LABEL[run.initialMode] : '미확인'} · 현재: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
              />
              <ProgressStep
                done={generationConsistent}
                failed={Boolean(sawHandoff && control?.mode === 'AUTO' && !generationConsistent)}
                title="Generation 일관성 확인"
                detail={`시작 gen ${run.initialGeneration ?? '미확인'} · 제어 gen ${controlGeneration ?? '미확인'} · 명령 gen ${activeCommandGeneration ?? '미확인'} · 차량 적용 gen ${appliedGeneration ?? '미확인'}`}
              />
              <ProgressStep
                done={returnedAsStandby}
                failed={Boolean(command.data?.status === 'SUCCEEDED' && !targetAlive)}
                title="장애 VM 예비 상태 복귀"
                detail={
                  returnedAsStandby
                    ? '장애 VM은 실행 상태를 유지하며 WAITING · QUALIFIED 예비 상태로 복귀했습니다.'
                    : targetLifecycle
                      ? `${targetLifecycle.phase} · ${targetLifecycle.readiness}${targetLifecycle.reason ? ` · ${appReasonLabel(targetLifecycle.reason)}` : ''}`
                      : '장애 종료 후 lifecycle 복귀를 기다리는 중입니다.'
                }
              />
            </ol>
          </div>
        )}
      </Card>
      {activeVm && (
        <Modal
          open={confirmOpen}
          title="자동주행 연산 지연 시연"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                취소
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin();
                  setConfirmOpen(false);
                }}
              >
                지연 발생
              </Button>
            </>
          }
        >
          <p>
            현재 활성 VM <b>{activeVm.name}</b>의 AUTO 계산 뒤에 100ms 지연을 30초 동안 반복합니다. 백업 VM이
            준비되지 않았다면 안전 정지할 수 있습니다.
          </p>
        </Modal>
      )}
    </div>
  );
}

interface InteractionStressRun extends Run {
  initialActiveId: string | null;
  durationS: number;
  kind: 'cpu' | 'memory';
}

const INTERACTION_STRESS_DURATION_S = 30;
const STRESS_REACHED_PERCENT = 85;

function percent(value: number | null): string {
  return value == null ? '미확인' : `${value.toFixed(1)}%`;
}

function InteractionStressScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const control = drive?.control ?? null;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<InteractionStressRun | null>(null);
  const interactionVm = (vms.data?.items ?? []).find((vm) => vm.role === 'interaction');
  const metrics = useMetrics(interactionVm?.vm_id ?? '', Boolean(interactionVm), 5);
  const samples = metrics.data?.items ?? [];
  const cpu = latest(samples, cpuPercentOf);
  const memory = interactionVm ? latest(samples, (sample) => memoryPercentOf(sample, interactionVm)) : null;
  const start = useStartStress(interactionVm?.vm_id ?? '');
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);
  const commandAccepted = Boolean(
    command.data && ['ACCEPTED', 'RUNNING', 'SUCCEEDED'].includes(command.data.status),
  );
  const commandRunning = command.data?.status === 'RUNNING';
  const commandSucceeded = command.data?.status === 'SUCCEEDED';
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const loadReached =
    run?.kind === 'cpu'
      ? cpu != null && cpu >= STRESS_REACHED_PERCENT
      : run?.kind === 'memory'
        ? memory != null && memory >= STRESS_REACHED_PERCENT
        : false;
  const driveIsolated = Boolean(
    run &&
    control?.mode === 'AUTO' &&
    control.active_vm_id === run.initialActiveId &&
    drive?.source?.status === 'fresh',
  );
  const recovered = Boolean(
    commandSucceeded && (run?.kind === 'cpu' ? cpu != null && cpu < 80 : memory != null && memory < 85),
  );
  const terminal = commandSucceeded || commandFailed;
  const controlFresh = socket === 'open' && drive?.source?.status === 'fresh' && control != null;
  const unavailableReason = !interactionVm
    ? 'Interaction 역할 VM을 찾지 못했습니다.'
    : interactionVm.guest_kind !== 'linux'
      ? 'Interaction VM이 Linux가 아닙니다.'
      : interactionVm.state !== 'running'
        ? 'Interaction VM이 실행 중이 아닙니다.'
        : interactionVm.agent_status !== 'ONLINE'
          ? 'Interaction VM Agent가 온라인 상태가 아닙니다.'
          : !controlFresh
            ? '주행 제어 상태가 최신이 아닙니다.'
            : control?.mode !== 'AUTO'
              ? 'AUTO 주행 중에만 격리 시연을 시작할 수 있습니다.'
              : null;

  const begin = (kind: 'cpu' | 'memory') => {
    if (!interactionVm || start.isPending) return;
    setRun(null);
    const request =
      kind === 'cpu'
        ? ({
            kind,
            body: {
              workers: interactionVm.vcpus ?? 1,
              load_percent: 90,
              duration_s: INTERACTION_STRESS_DURATION_S,
            },
          } as const)
        : ({
            kind,
            body: {
              memory_mib: Math.min(Math.floor(interactionVm.memory_mib * 0.9), 512),
              duration_s: INTERACTION_STRESS_DURATION_S,
            },
          } as const);
    start.mutate(request, {
      onSuccess: (submitted) =>
        setRun({
          targetId: interactionVm.vm_id,
          targetName: interactionVm.name,
          initialMode: control?.mode ?? null,
          initialActiveId: control?.active_vm_id ?? null,
          commandId: submitted.command_id,
          startedAt: submitted.submitted_at,
          durationS: submitted.duration_s,
          kind,
        }),
    });
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 3</span>
          <h2>Interaction VM 자원 폭주와 주행 격리</h2>
          <p>CPU와 메모리 부하를 각각 실행해 90% 수준에서도 Zephyr의 AUTO 주행이 유지되는지 확인합니다.</p>
        </div>
        <Button
          variant="danger"
          disabled={Boolean(unavailableReason) || start.isPending || Boolean(run && !terminal)}
          title={unavailableReason ?? undefined}
          onClick={() => setConfirmOpen(true)}
        >
          {start.isPending ? '부하 명령 전송 중…' : terminal ? '시연 다시 시작' : '시연 시작'}
        </Button>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {failureText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary
          label="Interaction VM"
          value={interactionVm?.name ?? '미확인'}
          mono={interactionVm?.vm_id.slice(0, 8)}
        />
        <Summary
          label="CPU 사용률"
          value={percent(cpu)}
          health={cpu != null && cpu >= STRESS_REACHED_PERCENT ? 'recovering' : 'stopped'}
        />
        <Summary
          label="메모리 사용률"
          value={percent(memory)}
          health={memory != null && memory >= STRESS_REACHED_PERCENT ? 'recovering' : 'stopped'}
        />
      </div>
      <Card title="격리 확인">
        <div className={styles.scenarioSummary}>
          <Summary label="주행 모드" value={control?.mode ? MODE_LABEL[control.mode] : '미확인'} />
          <Summary
            label="활성 주행 VM"
            value={(vms.data?.items ?? []).find((vm) => vm.vm_id === control?.active_vm_id)?.name ?? '미확인'}
            mono={control?.active_vm_id?.slice(0, 8)}
          />
          <Summary
            label="시연 판정"
            value={
              driveIsolated && loadReached
                ? '격리 유지 중'
                : recovered && driveIsolated
                  ? '성공 · 자원 복귀'
                  : '관측 중'
            }
            health={recovered && driveIsolated ? 'ok' : run ? 'recovering' : 'stopped'}
          />
        </div>
      </Card>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>CPU 또는 메모리 개별 부하를 선택하면 90% 도달, AUTO 격리, 자원 복귀를 순서대로 표시합니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                대상 <b>{run.targetName}</b> · {run.durationS}초
              </span>
              <span className={styles.mono}>{run.commandId.slice(0, 8)}</span>
              <time>{new Date(run.startedAt).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={commandAccepted}
                failed={commandFailed}
                title={`${run.kind === 'cpu' ? 'CPU' : '메모리'} 개별 부하 시작`}
                detail={command.data ? commandStateText(command.data.status) : 'VM Agent 응답 대기 중'}
              />
              <ProgressStep
                done={loadReached}
                failed={commandSucceeded && !loadReached}
                title={`${run.kind === 'cpu' ? 'CPU' : '메모리'} 사용률 90% 근방 도달`}
                detail={`CPU ${percent(cpu)} · 메모리 ${percent(memory)} · 성공 판정 ${STRESS_REACHED_PERCENT}% 이상`}
              />
              <ProgressStep
                done={driveIsolated && (commandRunning || commandSucceeded)}
                failed={Boolean(run && control?.mode !== 'AUTO')}
                title="Zephyr AUTO 주행 격리 유지"
                detail={`시작 모드 ${run.initialMode ? MODE_LABEL[run.initialMode] : '미확인'} · 현재 ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
              />
              <ProgressStep
                done={recovered}
                failed={commandFailed}
                title="부하 종료 후 자원 복귀"
                detail={
                  recovered
                    ? `CPU ${percent(cpu)} · 메모리 ${percent(memory)}`
                    : '명령 종료와 자원 하락을 대기 중'
                }
              />
            </ol>
          </div>
        )}
      </Card>
      {interactionVm && (
        <Modal
          open={confirmOpen}
          title="Interaction VM 개별 부하 시연"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                취소
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin('cpu');
                  setConfirmOpen(false);
                }}
              >
                CPU 90% 부하
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin('memory');
                  setConfirmOpen(false);
                }}
              >
                메모리 90% 부하
              </Button>
            </>
          }
        >
          <p>
            <b>{interactionVm.name}</b>에 CPU 또는 메모리 부하를 개별 명령으로 실행합니다.
          </p>
          <p className={styles.dim}>Zephyr VM의 AUTO 모드와 활성 VM이 유지되는지 함께 관측합니다.</p>
        </Modal>
      )}
    </div>
  );
}

interface AppExitRun extends Run {
  initialBootId: string | null;
  initialInstanceId: string | null;
  initialGeneration: number | null;
}

function AppExitScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const control = drive?.control ?? null;
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<AppExitRun | null>(null);
  const [sawExited, setSawExited] = useState(false);
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const activeVm = drivingVms.find((vm) => vm.vm_id === control?.active_vm_id);
  const activeReport = activeVm ? drive?.vm_apps?.[activeVm.vm_id]?.autonomous : null;
  const start = useStartActiveAppExit();
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);
  const targetVm = run ? drivingVms.find((vm) => vm.vm_id === run.targetId) : undefined;
  const targetReport = run ? drive?.vm_apps?.[run.targetId]?.autonomous : null;
  const lifecycle = targetReport?.lifecycle_source.status === 'fresh' ? targetReport.lifecycle : null;
  const exitedNow = lifecycle?.phase === 'STOPPED' && lifecycle.reason === 'APP_EXITED';

  useEffect(() => {
    if (!run || !exitedNow) return;
    const timer = window.setTimeout(() => setSawExited(true), 0);
    return () => window.clearTimeout(timer);
  }, [run, exitedNow]);

  const commandAccepted = Boolean(
    command.data && ['ACCEPTED', 'RUNNING', 'SUCCEEDED'].includes(command.data.status),
  );
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const sawHandoff = Boolean(run && control?.active_vm_id && control.active_vm_id !== run.targetId);
  const autoPreserved = sawHandoff && control?.mode === 'AUTO';
  const safeStopped = Boolean(run && control?.mode === 'SAFE_STOP');
  const bootPreserved = Boolean(
    run && run.initialBootId && targetReport?.boot_id && targetReport.boot_id === run.initialBootId,
  );
  const instanceChanged = Boolean(
    run &&
    run.initialInstanceId &&
    targetReport?.app_instance_id &&
    targetReport.app_instance_id !== run.initialInstanceId,
  );
  const rejoined = Boolean(
    instanceChanged &&
    bootPreserved &&
    lifecycle &&
    lifecycle.phase !== 'STOPPED' &&
    (lifecycle.readiness === 'READY' || lifecycle.readiness === 'QUALIFIED'),
  );
  const vmStayedRunning = targetVm?.state === 'running';
  const finished = commandFailed || rejoined || safeStopped;
  const controlFresh = socket === 'open' && drive?.source?.status === 'fresh' && control != null;
  const unavailableReason = !controlFresh
    ? '주행 제어 상태가 최신이 아닙니다.'
    : control.mode !== 'AUTO' || control.active_app !== 'autonomous'
      ? '자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.'
      : !activeVm
        ? '현재 활성 주행 VM을 찾지 못했습니다.'
        : activeVm.guest_kind !== 'zephyr' || activeVm.state !== 'running'
          ? '활성 VM이 실행 중인 Zephyr VM이 아닙니다.'
          : null;

  const begin = () => {
    if (!activeVm || start.isPending) return;
    const report = activeReport;
    setRun(null);
    setSawExited(false);
    start.mutate(undefined, {
      onSuccess: (submitted) =>
        setRun({
          targetId: submitted.vm_id,
          targetName: activeVm.name,
          initialMode: control?.mode ?? null,
          initialBootId: report?.boot_id ?? null,
          initialInstanceId: report?.app_instance_id ?? null,
          initialGeneration: control?.generation ?? null,
          commandId: submitted.command_id,
          startedAt: submitted.submitted_at,
        }),
    });
  };

  const resultLabel =
    rejoined && autoPreserved
      ? '성공 · 앱만 재시작'
      : safeStopped
        ? '안전 정지 · 백업 미준비'
        : sawHandoff
          ? '백업 승계 · 앱 복구 중'
          : '관측 중';

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 4</span>
          <h2>자율주행 앱 종료·재시작과 백업 승계</h2>
          <p>
            활성 Zephyr VM은 재부팅하지 않고 AUTO worker만 종료·재생성하며, 그 동안 백업 VM이 AUTO를
            이어받습니다.
          </p>
        </div>
        <Button
          variant="danger"
          disabled={Boolean(unavailableReason) || start.isPending || Boolean(run && !finished)}
          title={unavailableReason ?? undefined}
          onClick={() => setConfirmOpen(true)}
        >
          {start.isPending ? '앱 종료 명령 전송 중…' : finished ? '시연 다시 시작' : '시연 시작'}
        </Button>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {failureText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary
          label="장애 대상"
          value={run?.targetName ?? activeVm?.name ?? '미확인'}
          mono={(run?.targetId ?? activeVm?.vm_id)?.slice(0, 8)}
        />
        <Summary
          label="현재 제어"
          value={
            control?.mode
              ? `${MODE_LABEL[control.mode]} · ${drivingVms.find((vm) => vm.vm_id === control.active_vm_id)?.name ?? '미확인'}`
              : '미확인'
          }
          health={control?.mode === 'AUTO' ? 'ok' : control?.mode === 'SAFE_STOP' ? 'fault' : 'recovering'}
        />
        <Summary
          label="시연 판정"
          value={resultLabel}
          health={rejoined && autoPreserved ? 'ok' : safeStopped ? 'fault' : run ? 'recovering' : 'stopped'}
        />
      </div>
      <Card title="장애 VM의 앱·VM 신원">
        <div className={styles.scenarioSummary}>
          <Summary
            label="VM boot ID"
            value={bootPreserved ? '유지됨' : run?.initialBootId ? '확인 중' : '관측값 없음'}
            mono={targetReport?.boot_id?.slice(0, 8) ?? run?.initialBootId?.slice(0, 8)}
            health={bootPreserved ? 'ok' : 'stopped'}
          />
          <Summary
            label="AUTO app instance"
            value={instanceChanged ? '새 instance 확인' : run ? '재시작 대기 중' : '대기'}
            mono={targetReport?.app_instance_id?.slice(0, 8) ?? run?.initialInstanceId?.slice(0, 8)}
            health={instanceChanged ? 'ok' : run ? 'recovering' : 'stopped'}
          />
          <Summary
            label="VM 상태"
            value={targetVm?.state ?? activeVm?.state ?? '미확인'}
            health={vmStayedRunning || (!run && activeVm?.state === 'running') ? 'ok' : 'fault'}
          />
        </div>
      </Card>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>시연을 시작하면 앱 종료, 제어권 승계, 앱 재시작, 예비 복귀를 표시합니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                대상 <b>{run.targetName}</b>
              </span>
              <span>gen {run.initialGeneration ?? '—'}</span>
              <span className={styles.mono}>{run.commandId.slice(0, 8)}</span>
              <time>{new Date(run.startedAt).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={commandAccepted}
                failed={commandFailed}
                title="app-exit 명령 접수"
                detail={command.data ? commandStateText(command.data.status) : 'Zephyr 응답 대기 중'}
              />
              <ProgressStep
                done={sawExited}
                failed={commandFailed}
                title="AUTO worker 종료"
                detail={
                  sawExited
                    ? 'STOPPED · APP_EXITED 확인'
                    : lifecycle?.reason
                      ? appReasonLabel(lifecycle.reason)
                      : '앱 종료 보고 대기 중'
                }
              />
              <ProgressStep
                done={autoPreserved}
                failed={safeStopped}
                title="백업 VM으로 AUTO 승계"
                detail={
                  autoPreserved
                    ? `${drivingVms.find((vm) => vm.vm_id === control?.active_vm_id)?.name ?? '백업 VM'}이 AUTO를 이어받았습니다.`
                    : safeStopped
                      ? '백업이 준비되지 않아 SAFE_STOP으로 전환됐습니다.'
                      : '제어권 변경 대기 중'
                }
              />
              <ProgressStep
                done={bootPreserved && vmStayedRunning}
                failed={Boolean(targetVm && targetVm.state !== 'running')}
                title="VM 재부팅 없음"
                detail={bootPreserved ? `boot ${targetReport?.boot_id?.slice(0, 8)} 유지` : 'boot ID 비교 중'}
              />
              <ProgressStep
                done={rejoined}
                failed={commandFailed}
                title="새 AUTO worker로 예비 복귀"
                detail={
                  rejoined
                    ? `${lifecycle?.phase} · ${lifecycle?.readiness} · 제어권은 백업 VM에 유지`
                    : instanceChanged
                      ? '새 instance의 READY/QUALIFIED 대기 중'
                      : '새 app instance 대기 중'
                }
              />
            </ol>
          </div>
        )}
      </Card>
      {activeVm && (
        <Modal
          open={confirmOpen}
          title="자율주행 앱 장애 시연"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                취소
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin();
                  setConfirmOpen(false);
                }}
              >
                앱 종료
              </Button>
            </>
          }
        >
          <p>백엔드가 명령 시점의 활성 AUTO VM을 다시 확인한 뒤, 해당 VM의 자율주행 worker만 종료합니다.</p>
          <p className={styles.dim}>
            VM은 재부팅하지 않으며, 백업 VM이 준비되지 않았다면 SAFE_STOP으로 전환될 수 있습니다.
          </p>
        </Modal>
      )}
    </div>
  );
}

interface InputFaultRun extends Run {
  durationS: number;
}

export function AutoInputUnavailableScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const [durationS, setDurationS] = useState(50);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<InputFaultRun | null>(null);
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const controlFresh = socket === 'open' && control != null && drive?.source?.status === 'fresh';
  const start = useStartAutoInputUnavailable(activeVm?.vm_id ?? '');
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);
  const commandRunning = command.data?.status === 'ACCEPTED' || command.data?.status === 'RUNNING';
  const commandSucceeded = command.data?.status === 'SUCCEEDED';
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const targetLifecycle = run ? drive?.vm_apps?.[run.targetId]?.autonomous?.lifecycle : null;
  const targetLifecycleFresh = run
    ? drive?.vm_apps?.[run.targetId]?.autonomous?.lifecycle_source.status === 'fresh'
    : false;
  const inputUnavailable = targetLifecycleFresh && targetLifecycle?.reason === 'INPUT_UNAVAILABLE';
  const sawHandoff = Boolean(run && activeId && activeId !== run.targetId);
  const autoPreserved = Boolean(run && sawHandoff && control?.mode === 'AUTO');
  const terminal = commandSucceeded || commandFailed;
  const durationValid = Number.isInteger(durationS) && durationS >= 1 && durationS <= 120;
  const unavailableReason = !controlFresh
    ? '주행 제어 상태가 최신이 아닙니다.'
    : control?.mode !== 'AUTO' || control.active_app !== 'autonomous'
      ? 'AUTO 모드에서 자율주행 중일 때만 시연할 수 있습니다.'
      : !activeVm
        ? '현재 활성 주행 VM을 VM 목록에서 찾지 못했습니다.'
        : activeVm.guest_kind !== 'zephyr'
          ? '현재 활성 VM이 Zephyr가 아닙니다.'
          : activeVm.state !== 'running'
            ? '현재 활성 VM이 실행 중이 아닙니다.'
            : !durationValid
              ? '장애 지속 시간은 1초부터 120초까지 입력해 주세요.'
              : null;

  const begin = () => {
    if (!activeVm || start.isPending || !durationValid) return;
    setRun(null);
    start.mutate(
      { duration_s: durationS },
      {
        onSuccess: (submitted) =>
          setRun({
            targetId: activeVm.vm_id,
            targetName: activeVm.name,
            initialMode: control?.mode ?? null,
            commandId: submitted.command_id,
            startedAt: submitted.submitted_at,
            durationS: submitted.duration_s,
          }),
      },
    );
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 3</span>
          <h2>활성 VM의 AUTO 입력 처리 장애와 승계</h2>
          <p>
            Jetson 입력 원본은 유지하고, 현재 활성 VM 내부의 AUTO 계산 입력만 차단해 백업 VM 승계를
            확인합니다.
          </p>
        </div>
        <div className={styles.scenarioAction}>
          <label htmlFor="input-fault-duration">장애 지속 시간</label>
          <div>
            <input
              id="input-fault-duration"
              type="number"
              min={1}
              max={120}
              step={1}
              value={durationS}
              aria-describedby="input-fault-duration-help"
              onChange={(event) => setDurationS(Number(event.target.value))}
            />
            <span>초</span>
          </div>
          <small id="input-fault-duration-help">1~120초 · 안정적인 승계 시연은 10초 이상 권장</small>
          <Button
            variant="danger"
            disabled={Boolean(unavailableReason) || start.isPending || Boolean(run && !terminal)}
            title={unavailableReason ?? undefined}
            onClick={() => setConfirmOpen(true)}
          >
            {start.isPending ? '장애 명령 전송 중…' : terminal ? '시연 다시 시작' : '시연 시작'}
          </Button>
        </div>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {failureText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary label="현재 활성 VM" value={activeVm?.name ?? '미확인'} mono={activeVm?.vm_id.slice(0, 8)} />
        <Summary
          label="주행 모드"
          value={control?.mode ? MODE_LABEL[control.mode] : '미확인'}
          health={control?.mode === 'AUTO' ? 'ok' : 'fault'}
        />
        <Summary
          label="시연 결과"
          value={autoPreserved ? 'AUTO 유지 · 승계 완료' : sawHandoff ? '승계 후 모드 확인 중' : '대기 중'}
          health={autoPreserved ? 'ok' : sawHandoff ? 'recovering' : 'stopped'}
        />
      </div>
      <Card title="주행 VM과 AUTO 입력 상태">
        <div className={styles.drivingVmGrid}>
          {drivingVms.map((vm) => {
            const report = drive?.vm_apps?.[vm.vm_id]?.autonomous;
            const lifecycle = report?.lifecycle_source.status === 'fresh' ? report.lifecycle : null;
            const affected = run?.targetId === vm.vm_id;
            return (
              <article
                key={vm.vm_id}
                className={styles.inputVm}
                data-active={vm.vm_id === activeId}
                data-fault={affected && lifecycle?.reason === 'INPUT_UNAVAILABLE'}
              >
                <div>
                  <span>{vm.role === 'primary_drive' ? '주 구동 VM' : '백업 VM'}</span>
                  {vm.vm_id === activeId && <b>ACTIVE</b>}
                </div>
                <h3>{vm.name}</h3>
                <p className={styles.mono}>{vm.vm_id.slice(0, 8)}</p>
                <dl>
                  <div>
                    <dt>AUTO 상태</dt>
                    <dd>{lifecycle?.reason ?? lifecycle?.readiness ?? '미확인'}</dd>
                  </div>
                  <div>
                    <dt>실행 단계</dt>
                    <dd>{lifecycle?.phase ?? '미확인'}</dd>
                  </div>
                </dl>
              </article>
            );
          })}
        </div>
      </Card>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>시연을 시작하면 활성 VM의 입력 장애 관측부터 AUTO 승계까지 표시됩니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                대상 <b>{run.targetName}</b> · {run.durationS}초
              </span>
              <span className={styles.mono}>{run.commandId.slice(0, 8)}</span>
              <time>{new Date(run.startedAt).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={commandRunning || commandSucceeded}
                failed={commandFailed}
                title="입력 장애 명령 실행"
                detail={
                  command.data
                    ? commandFailed
                      ? agentErrorText(commandErrorCode(command.data))
                      : commandStateText(command.data.status)
                    : 'VM 응답 대기 중'
                }
              />
              <ProgressStep
                done={inputUnavailable || sawHandoff}
                title="활성 VM의 INPUT_UNAVAILABLE 관측"
                detail={
                  inputUnavailable
                    ? 'AUTO 계산 입력을 사용할 수 없는 상태가 확인됐습니다.'
                    : sawHandoff
                      ? '장애 판정 후 제어권이 이미 이동했습니다.'
                      : 'VM lifecycle 보고를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={sawHandoff}
                title="반복 장애 판정과 백업 VM 승계"
                detail={
                  sawHandoff
                    ? `새 활성 VM: ${activeVm?.name ?? activeId}`
                    : 'active_vm_id 변경을 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={autoPreserved}
                failed={sawHandoff && control?.mode !== 'AUTO'}
                title="AUTO 주행 모드 유지"
                detail={`시작: ${run.initialMode ? MODE_LABEL[run.initialMode] : '미확인'} · 현재: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
              />
            </ol>
          </div>
        )}
      </Card>
      {activeVm && (
        <Modal
          open={confirmOpen}
          title="AUTO 입력 처리 장애 시연"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                취소
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin();
                  setConfirmOpen(false);
                }}
              >
                {durationS}초 장애 발생
              </Button>
            </>
          }
        >
          <p>
            <b>{activeVm.name}</b> 한 대의 AUTO 계산 입력만 {durationS}초 동안 차단합니다. 백업 VM과 Jetson
            입력 원본에는 영향을 주지 않습니다.
          </p>
        </Modal>
      )}
    </div>
  );
}

interface TxBlockRun extends Run {
  durationS: number;
  initialGeneration: number | null;
}

function TxBlockScenario() {
  const { drive, socket } = useDrive();
  const vms = useVms();
  const start = useStartActiveAutoTxBlock();
  const [durationS, setDurationS] = useState(10);
  const [run, setRun] = useState<TxBlockRun | null>(null);
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const targetVm = run ? drivingVms.find((vm) => vm.vm_id === run.targetId) : null;
  const targetReport = run ? drive?.vm_apps?.[run.targetId]?.autonomous : null;
  const lifecycle = targetReport?.lifecycle_source.status === 'fresh' ? targetReport.lifecycle : null;
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const commandRunning = command.data?.status === 'RUNNING' || command.data?.status === 'SUCCEEDED';
  const calculationHealthy = Boolean(
    targetReport?.heartbeat_source.status === 'fresh' &&
    lifecycle &&
    lifecycle.phase !== 'STOPPED' &&
    lifecycle.readiness !== 'NOT_READY',
  );
  const sawHandoff = Boolean(run && activeId && activeId !== run.targetId);
  const autoPreserved = Boolean(sawHandoff && control?.mode === 'AUTO');
  const generationConsistent = Boolean(
    autoPreserved &&
    control?.generation != null &&
    drive?.apps?.autonomous?.last_command?.generation === control.generation,
  );
  const returnedAsStandby = Boolean(
    command.data?.status === 'SUCCEEDED' &&
    sawHandoff &&
    targetVm?.state === 'running' &&
    lifecycle?.phase === 'WAITING' &&
    lifecycle.readiness === 'QUALIFIED',
  );
  const durationValid = Number.isInteger(durationS) && durationS >= 1 && durationS <= 120;
  const unavailableReason =
    socket !== 'open' || drive?.source?.status !== 'fresh' || !control
      ? '주행 제어 상태가 최신이 아닙니다.'
      : control.mode !== 'AUTO' || control.active_app !== 'autonomous'
        ? '자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.'
        : !activeVm
          ? '현재 활성 주행 VM을 찾지 못했습니다.'
          : !durationValid
            ? '장애 지속 시간은 1~120초여야 합니다.'
            : null;
  const terminal = commandFailed || returnedAsStandby;

  const begin = () => {
    if (!activeVm || unavailableReason || start.isPending) return;
    setRun(null);
    start.mutate(
      { duration_s: durationS },
      {
        onSuccess: (submitted) =>
          setRun({
            targetId: submitted.vm_id,
            targetName: drivingVms.find((vm) => vm.vm_id === submitted.vm_id)?.name ?? submitted.vm_id,
            initialMode: control?.mode ?? null,
            initialGeneration: control?.generation ?? null,
            commandId: submitted.command_id,
            startedAt: submitted.submitted_at,
            durationS,
          }),
      },
    );
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 5</span>
          <h2>AUTO 결과 송신 차단과 백업 승계</h2>
          <p>계산과 heartbeat는 유지하고 활성 VM의 drive.command 발행만 차단합니다.</p>
        </div>
        <div className={styles.scenarioAction}>
          <label>
            <span>차단 시간</span>
            <span className={styles.memoryInputGroup}>
              <input
                aria-label="차단 시간"
                type="number"
                min={1}
                max={120}
                value={durationS}
                disabled={start.isPending || Boolean(run && !terminal)}
                onChange={(event) => setDurationS(Number(event.target.value))}
              />
              <span>초</span>
            </span>
          </label>
          <Button
            variant="danger"
            disabled={Boolean(unavailableReason) || start.isPending || Boolean(run && !terminal)}
            title={unavailableReason ?? undefined}
            onClick={begin}
          >
            {start.isPending ? '차단 명령 전송 중…' : terminal ? '시연 다시 시작' : '시연 시작'}
          </Button>
        </div>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && <p className={styles.scenarioWarning}>{failureText(start.error)}</p>}
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            차단을 시작하면 계산 유지, 명령 stale, 승계와 복구를 표시합니다.
          </div>
        ) : (
          <ol className={styles.scenarioProgress}>
            <ProgressStep
              done={commandRunning}
              failed={commandFailed}
              title="auto_tx_block 적용"
              detail={`${run.targetName} · ${run.durationS}초`}
            />
            <ProgressStep
              done={calculationHealthy && commandRunning}
              failed={commandFailed}
              title="계산 및 heartbeat 유지"
              detail={lifecycle ? `${lifecycle.phase} · ${lifecycle.readiness}` : 'lifecycle 보고 대기 중'}
            />
            <ProgressStep
              done={sawHandoff}
              title="drive.command stale 및 백업 승계"
              detail={
                sawHandoff ? `현재 활성 VM: ${activeVm?.name ?? activeId}` : '명령 갱신 중단 감지 대기 중'
              }
            />
            <ProgressStep
              done={generationConsistent}
              title="AUTO와 generation 유지"
              detail={`시작 gen ${run.initialGeneration ?? '미확인'} · 현재 gen ${control?.generation ?? '미확인'}`}
            />
            <ProgressStep
              done={returnedAsStandby}
              failed={commandFailed}
              title="송신 복구 후 예비 복귀"
              detail={
                returnedAsStandby
                  ? '차단이 해제되고 원래 VM이 WAITING · QUALIFIED 상태로 복귀했습니다.'
                  : command.data
                    ? commandStateText(command.data.status)
                    : '명령 결과 대기 중'
              }
            />
          </ol>
        )}
      </Card>
    </div>
  );
}

interface DualFaultRun {
  firstVmId: string;
  firstVmName: string;
  crashCommandId: string;
  startedAt: string;
  secondVmId: string | null;
  secondVmName: string | null;
  appExitCommandId: string | null;
}

function DualFaultScenario() {
  const { drive, socket } = useDrive();
  const vms = useVms();
  const crash = useStartActiveVmCrash();
  const appExit = useStartActiveAppExit();
  const [run, setRun] = useState<DualFaultRun | null>(null);
  const secondRequested = useRef(false);
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const firstVm = run ? drivingVms.find((vm) => vm.vm_id === run.firstVmId) : null;
  const secondReport = run?.secondVmId ? drive?.vm_apps?.[run.secondVmId]?.autonomous : null;
  const secondLifecycle = secondReport?.lifecycle_source.status === 'fresh' ? secondReport.lifecycle : null;
  const firstHandoff = Boolean(run && activeId && activeId !== run.firstVmId && control?.mode === 'AUTO');
  const safeMode = control?.mode === 'SAFE_STOP' || control?.mode === 'MANUAL';
  const secondExited = secondLifecycle?.phase === 'STOPPED' || safeMode;
  const firstRecovered = Boolean(run && firstVm?.state === 'running');
  const secondRecovered = Boolean(
    run?.secondVmId &&
    secondLifecycle &&
    secondLifecycle.phase !== 'STOPPED' &&
    (secondLifecycle.readiness === 'READY' || secondLifecycle.readiness === 'QUALIFIED'),
  );
  const recoveredWithoutAuto = Boolean(firstRecovered && secondRecovered && safeMode);
  const unavailableReason =
    socket !== 'open' || drive?.source?.status !== 'fresh' || !control
      ? '주행 제어 상태가 최신이 아닙니다.'
      : control.mode !== 'AUTO' || control.active_app !== 'autonomous'
        ? '자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.'
        : drivingVms.length < 2 || !activeVm
          ? '실행 가능한 주행 VM 두 대가 필요합니다.'
          : null;

  useEffect(() => {
    if (!run || !firstHandoff || run.secondVmId || !activeId || secondRequested.current) return;
    secondRequested.current = true;
    appExit.mutate(undefined, {
      onSuccess: (submitted) =>
        setRun((current) =>
          current
            ? {
                ...current,
                secondVmId: submitted.vm_id,
                secondVmName: drivingVms.find((vm) => vm.vm_id === submitted.vm_id)?.name ?? submitted.vm_id,
                appExitCommandId: submitted.command_id,
              }
            : current,
        ),
    });
  }, [activeId, appExit, drivingVms, firstHandoff, run]);

  const begin = () => {
    if (!activeVm || unavailableReason || crash.isPending) return;
    secondRequested.current = false;
    setRun(null);
    crash.mutate(undefined, {
      onSuccess: (submitted) =>
        setRun({
          firstVmId: submitted.vm_id,
          firstVmName: drivingVms.find((vm) => vm.vm_id === submitted.vm_id)?.name ?? submitted.vm_id,
          crashCommandId: submitted.command_id,
          startedAt: submitted.submitted_at,
          secondVmId: null,
          secondVmName: null,
          appExitCommandId: null,
        }),
    });
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 7</span>
          <h2>AUTO 이중 장애와 안전 모드 전환</h2>
          <p>활성 VM을 crash한 뒤 승계된 백업 VM의 자율주행 앱까지 종료합니다.</p>
        </div>
        <Button
          variant="danger"
          disabled={
            Boolean(unavailableReason) || crash.isPending || appExit.isPending || Boolean(run && !safeMode)
          }
          title={unavailableReason ?? undefined}
          onClick={begin}
        >
          {crash.isPending || appExit.isPending ? '장애 주입 중…' : safeMode ? '시연 다시 시작' : '시연 시작'}
        </Button>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {(crash.isError || appExit.isError) && (
        <p className={styles.scenarioWarning}>{failureText(crash.error ?? appExit.error)}</p>
      )}
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            첫 VM 장애부터 이중화 소진과 안전 전환까지 순서대로 표시합니다.
          </div>
        ) : (
          <ol className={styles.scenarioProgress}>
            <ProgressStep
              done
              title="활성 VM crash"
              detail={`${run.firstVmName} · ${run.crashCommandId.slice(0, 8)}`}
            />
            <ProgressStep
              done={firstHandoff}
              title="백업 VM으로 AUTO 승계"
              detail={firstHandoff ? `현재 활성 VM: ${activeVm?.name ?? activeId}` : '첫 번째 승계 대기 중'}
            />
            <ProgressStep
              done={Boolean(run.secondVmId)}
              failed={appExit.isError}
              title="백업 VM 자율주행 앱 종료"
              detail={run.secondVmName ?? '승계 확인 후 app_exit 명령을 전송합니다.'}
            />
            <ProgressStep
              done={secondExited && safeMode}
              title="AUTO 이중화 소진"
              detail={secondExited ? '사용 가능한 AUTO 경로가 없어졌습니다.' : '두 번째 장애 관측 대기 중'}
            />
            <ProgressStep
              done={safeMode}
              failed={Boolean(run.secondVmId && control?.mode === 'AUTO' && secondExited)}
              title="SAFE_STOP 또는 MANUAL 전환"
              detail={`현재 모드: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
            />
            <ProgressStep
              done={recoveredWithoutAuto}
              title="복구 후 AUTO 자동 재개 없음"
              detail={
                recoveredWithoutAuto
                  ? '두 AUTO 경로가 복구됐지만 명시적 요청 전까지 안전 모드를 유지합니다.'
                  : `A 복구 ${firstRecovered ? '완료' : '대기'} · B 앱 복구 ${secondRecovered ? '완료' : '대기'}`
              }
            />
          </ol>
        )}
      </Card>
    </div>
  );
}

function JetsonLossScenario() {
  const { drive, socket } = useDrive();
  const vms = useVms();
  const start = useStartPerceptionBlock();
  const [duration, setDuration] = useState(50);
  const [run, setRun] = useState<PerceptionFaultOperation | null>(null);
  const operation = usePerceptionFaultOperation(run?.operation_id ?? null);
  const current = operation.data ?? run;
  const control = drive?.control ?? null;
  const perception = drive?.perception ?? null;
  const activeId = control?.active_vm_id ?? null;
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const durationValid = Number.isInteger(duration) && duration >= 1 && duration <= 120;
  const perceptionReady = perception?.valid === true && perception.received_ago_ms < 200;
  const inProgress = Boolean(current && !['ENDED', 'REJECTED', 'UNKNOWN', 'FAILED'].includes(current.status));
  const operationFailed =
    current?.status === 'REJECTED' || current?.status === 'UNKNOWN' || current?.status === 'FAILED';
  const applied = current?.status === 'APPLIED' || current?.status === 'ENDED';
  const ended = current?.status === 'ENDED';
  const sawLoss = Boolean(run && (perception?.valid === false || ended));
  const manual = control?.mode === 'MANUAL';
  const safeStop = control?.mode === 'SAFE_STOP';
  const safeMode = manual || safeStop;
  const noUnexpectedHandoff = Boolean(
    run?.active_vm_id && (activeId === run.active_vm_id || (safeStop && activeId == null)),
  );
  const bothInputsUnavailable = Boolean(
    run &&
    drivingVms.length >= 2 &&
    drivingVms.every((vm) => {
      const reason = drive?.vm_apps?.[vm.vm_id]?.autonomous?.lifecycle?.reason ?? '';
      return reason.includes('WORLD_UNAVAILABLE') || reason.includes('INPUT_UNAVAILABLE');
    }),
  );
  const recovered = Boolean(ended && sawLoss && perceptionReady && safeMode);

  const unavailableReason =
    socket !== 'open'
      ? '주행 상태 연결이 끊겨 있습니다.'
      : control?.mode !== 'AUTO'
        ? '자율주행 중일 때만 시연을 시작할 수 있습니다.'
        : !activeVm
          ? '현재 활성 주행 VM을 확인할 수 없습니다.'
          : !perceptionReady
            ? 'Jetson 인지 결과가 정상 수신 중이 아닙니다.'
            : !durationValid
              ? '장애 지속 시간은 1~120초여야 합니다.'
              : null;

  const begin = () => {
    if (unavailableReason || start.isPending) return;
    setRun(null);
    start.mutate(duration, { onSuccess: setRun });
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 6</span>
          <h2>Jetson 인지 송신 단절과 안전 전환</h2>
          <p>
            Jetson 계산은 유지한 채 World 송신만 끊고, 두 주행 VM의 공통 입력 소실과 SAFE_STOP 또는 MANUAL
            전환을 확인합니다.
          </p>
        </div>
        <div className={styles.scenarioAction}>
          <label>
            <span>장애 지속 시간</span>
            <span className={styles.memoryInputGroup}>
              <input
                aria-label="장애 지속 시간"
                type="number"
                min={1}
                max={120}
                step={1}
                value={duration}
                disabled={inProgress || start.isPending}
                aria-invalid={!durationValid}
                onChange={(event) => setDuration(Number(event.target.value))}
              />
              <span>초</span>
            </span>
          </label>
          <Button
            variant="danger"
            disabled={Boolean(unavailableReason) || inProgress || start.isPending}
            onClick={begin}
          >
            {start.isPending
              ? 'Jetson 상태 확인 중…'
              : ended || operationFailed
                ? '시연 다시 시작'
                : '시연 시작'}
          </Button>
        </div>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {perceptionFaultErrorText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary
          label="현재 활성 VM"
          value={safeStop && !activeId ? '없음 · SAFE_STOP' : (activeVm?.name ?? '미확인')}
          mono={activeId?.slice(0, 8)}
        />
        <Summary
          label="주행 모드"
          value={control?.mode ? MODE_LABEL[control.mode] : '미확인'}
          health={control?.mode === 'AUTO' ? 'ok' : safeMode ? 'recovering' : 'fault'}
        />
        <Summary
          label="Jetson 인지 입력"
          value={perceptionReady ? `${perception?.hz ?? 0} Hz · 정상` : sawLoss ? '송신 단절' : '미확인'}
          health={perceptionReady ? 'ok' : 'fault'}
        />
      </div>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>시연을 시작하면 Jetson 명령 적용부터 안전 전환, 인지 송신 복구까지 표시됩니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                시작 active VM <b>{run.active_vm_id?.slice(0, 8) ?? '미확인'}</b>
              </span>
              <span className={styles.mono}>{run.command_id.slice(0, 8)}</span>
              <time>{new Date(run.submitted_at).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={Boolean(current && current.status !== 'QUERYING')}
                failed={operationFailed}
                title="Jetson 제어 window 확인"
                detail={
                  current?.status === 'QUERYING'
                    ? 'Jetson STATUS 응답 대기 중'
                    : (current?.reason ?? '현재 서비스와 window를 확인했습니다.')
                }
              />
              <ProgressStep
                done={applied}
                failed={operationFailed}
                title="인지 결과 송신 차단 적용"
                detail={
                  applied
                    ? `World 송신을 ${run.duration_s}초 동안 차단했습니다.`
                    : 'BLOCK 적용 결과를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={sawLoss}
                title="두 주행 VM의 공통 인지 입력 소실"
                detail={
                  bothInputsUnavailable
                    ? '두 VM 모두 입력 없음 상태를 보고했습니다.'
                    : sawLoss
                      ? '인지 입력 만료를 확인했고 VM 상태 보고를 기다립니다.'
                      : '인지 결과 만료를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={safeMode}
                title="AUTO 출력 차단 및 안전 전환"
                detail={
                  manual
                    ? '자동 복귀하지 않고 명시적인 수동 입력을 기다립니다.'
                    : safeStop
                      ? '활성 출력을 제거하고 SAFE_STOP 정지 상태를 유지합니다.'
                      : `현재 모드: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`
                }
              />
              <ProgressStep
                done={noUnexpectedHandoff && safeMode}
                failed={Boolean(run.active_vm_id && activeId && activeId !== run.active_vm_id)}
                title="불필요한 A/B 승계 없음"
                detail={
                  safeStop && !activeId
                    ? '공통 입력 장애이므로 다른 AUTO VM을 선택하지 않고 제어권을 해제했습니다.'
                    : noUnexpectedHandoff
                      ? '공통 입력 장애이므로 다른 주행 VM으로 승계하지 않았습니다.'
                      : 'active_vm_id와 주행 모드를 확인하는 중입니다.'
                }
              />
              <ProgressStep
                done={recovered}
                title="Jetson 인지 송신 복구"
                detail={
                  recovered
                    ? `새 인지 결과 수신이 재개됐으며 주행 모드는 ${control?.mode ? MODE_LABEL[control.mode] : '안전 상태'}로 유지됩니다. AUTO는 명시적 요청 전까지 재개되지 않습니다.`
                    : ended
                      ? '차단은 종료됐고 새 인지 결과를 기다립니다.'
                      : '설정한 지속 시간이 끝나기를 기다립니다.'
                }
              />
            </ol>
          </div>
        )}
      </Card>
    </div>
  );
}

function VmCrashScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<CrashRun | null>(null);
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const target = run ? drivingVms.find((vm) => vm.vm_id === run.targetId) : undefined;
  const controlFresh = socket === 'open' && control != null && drive?.source?.status === 'fresh';
  const start = useStartActiveVmCrash();
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null, true);
  const events = useEvents({ vm_id: run?.targetId ?? '__not-started__', limit: 30 });

  const commandAccepted = command.data?.status === 'ACCEPTED' || command.data?.status === 'SUCCEEDED';
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const sawHandoff = Boolean(run && activeId && activeId !== run.targetId);
  const sawFailure = Boolean(
    run && (target?.state === 'crashed' || target?.state === 'stopped' || sawHandoff),
  );
  const runEvents = (events.data?.items ?? []).filter(
    (event) => run && Date.parse(event.occurred_at) >= Date.parse(run.startedAt),
  );
  const recoveryStarted = runEvents.some((event) => event.type === 'recovery.started');
  const recoveryFinished = runEvents.some(
    (event) => event.type === 'recovery.completed' || event.type === 'recovery.recovered',
  );
  const recoveryFailed = runEvents.some((event) => event.type === 'recovery.failed');
  const recoveredBootId = run ? (drive?.vm_apps?.[run.targetId]?.autonomous?.boot_id ?? null) : null;
  const bootChanged = Boolean(run?.initialBootId && recoveredBootId && recoveredBootId !== run.initialBootId);
  const recovered = Boolean(
    run && sawFailure && target?.state === 'running' && recoveryFinished && bootChanged,
  );
  const modePreserved = Boolean(run && sawHandoff && control?.mode === 'AUTO');
  const returnedAsStandby = Boolean(recovered && activeId !== run?.targetId);
  const finished = returnedAsStandby && sawHandoff && modePreserved;
  const unavailableReason = !controlFresh
    ? '주행 제어 상태가 최신이 아닙니다.'
    : !activeVm
      ? '현재 활성 주행 VM을 VM 목록에서 찾지 못했습니다.'
      : control?.mode !== 'AUTO' || control.active_app !== 'autonomous'
        ? '자율주행 앱이 AUTO 제어 중일 때만 시연할 수 있습니다.'
        : activeVm.guest_kind !== 'zephyr'
          ? '현재 활성 VM이 Zephyr가 아닙니다.'
          : activeVm.state !== 'running'
            ? '현재 활성 VM이 실행 중이 아닙니다.'
            : null;

  const begin = () => {
    if (!activeVm || start.isPending) return;
    setRun(null);
    start.mutate(undefined, {
      onSuccess: (submitted) => {
        const submittedTarget = drivingVms.find((vm) => vm.vm_id === submitted.vm_id);
        setRun({
          targetId: submitted.vm_id,
          targetName: submittedTarget?.name ?? submitted.vm_id,
          initialMode: control?.mode ?? null,
          initialBootId: drive?.vm_apps?.[submitted.vm_id]?.autonomous?.boot_id ?? null,
          commandId: submitted.command_id,
          startedAt: submitted.submitted_at,
        });
      },
    });
  };

  return (
    <div className={styles.scenarioStack}>
      <section className={styles.scenarioIntro}>
        <div>
          <span className={styles.scenarioNumber}>시연 1</span>
          <h2>현재 주행 VM 장애와 자동 승계</h2>
          <p>활성 Zephyr VM을 crash 상태로 만들고, 백업 VM 승계와 기존 주행 모드 유지 여부를 관측합니다.</p>
        </div>
        <Button
          variant="danger"
          disabled={
            Boolean(unavailableReason) || start.isPending || Boolean(run && !finished && !commandFailed)
          }
          title={unavailableReason ?? undefined}
          onClick={() => setConfirmOpen(true)}
        >
          {start.isPending
            ? '장애 명령 전송 중…'
            : finished || commandFailed
              ? '시연 다시 시작'
              : '시연 시작'}
        </Button>
      </section>
      {unavailableReason && <p className={styles.scenarioWarning}>{unavailableReason}</p>}
      {start.isError && (
        <p className={styles.scenarioWarning} role="alert">
          {failureText(start.error)}
        </p>
      )}
      <div className={styles.scenarioSummary}>
        <Summary label="현재 활성 VM" value={activeVm?.name ?? '미확인'} mono={activeVm?.vm_id.slice(0, 8)} />
        <Summary
          label="주행 모드"
          value={control?.mode ? MODE_LABEL[control.mode] : '미확인'}
          health={control?.mode ? MODE_HEALTH[control.mode] : 'stopped'}
        />
        <Summary
          label="주행 상태 연결"
          value={controlFresh ? '정상 수신' : socket === 'open' ? '데이터 지연' : '연결 끊김'}
          health={controlFresh ? 'ok' : 'fault'}
        />
      </div>
      <Card title="주행 VM 상태">
        <div className={styles.drivingVmGrid}>
          {drivingVms.length ? (
            drivingVms.map((vm) => <DrivingVm key={vm.vm_id} vm={vm} active={vm.vm_id === activeId} />)
          ) : (
            <p className={styles.dim}>주 구동·백업 역할이 지정된 Zephyr VM이 없습니다.</p>
          )}
        </div>
      </Card>
      <Card title="시연 진행">
        {!run ? (
          <div className={styles.scenarioEmpty}>
            <p>시연을 시작하면 명령 수락부터 장애 VM 재기동까지 실제 관측 결과가 표시됩니다.</p>
          </div>
        ) : (
          <div>
            <div className={styles.runMeta}>
              <span>
                대상 <b>{run.targetName}</b>
              </span>
              <span className={styles.mono}>{run.commandId.slice(0, 8)}</span>
              <time>{new Date(run.startedAt).toLocaleTimeString('ko-KR')}</time>
            </div>
            <ol className={styles.scenarioProgress}>
              <ProgressStep
                done={commandAccepted}
                failed={commandFailed}
                title="장애 명령 수락"
                detail={
                  command.data
                    ? command.data.status === 'FAILED' || command.data.status === 'REJECTED'
                      ? agentErrorText(commandErrorCode(command.data))
                      : commandStateText(command.data.status)
                    : 'VM 응답 대기 중'
                }
              />
              <ProgressStep
                done={sawFailure}
                title="활성 VM 장애 관측"
                detail={
                  sawFailure
                    ? '대상 VM의 중단 또는 제어권 이탈을 확인했습니다.'
                    : 'VM 상태 변화를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={sawHandoff}
                title="백업 VM으로 제어권 승계"
                detail={
                  sawHandoff
                    ? `현재 활성 VM: ${activeVm?.name ?? activeId}`
                    : 'active_vm_id 변경을 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={modePreserved}
                title="기존 주행 모드 유지"
                detail={`시작: ${run.initialMode ? MODE_LABEL[run.initialMode] : '미확인'} · 현재: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
              />
              <ProgressStep
                done={recoveryStarted}
                failed={recoveryFailed}
                title="자동 복구 시작"
                detail={
                  recoveryFailed
                    ? '복구 실패 이벤트가 발생했습니다.'
                    : recoveryStarted
                      ? '플랫폼 복구 워커가 작업을 시작했습니다.'
                      : '복구 이벤트를 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={recovered}
                failed={recoveryFailed}
                title="장애 VM 자동 복구"
                detail={
                  recovered
                    ? `복구 완료, running 상태와 새 boot ID(${recoveredBootId?.slice(0, 8)})를 확인했습니다.`
                    : `현재 대상 상태: ${target ? STATE_LABEL[target.state] : '미확인'}`
                }
              />
              <ProgressStep
                done={returnedAsStandby}
                failed={Boolean(recovered && activeId === run.targetId)}
                title="예비 VM으로 복귀"
                detail={
                  returnedAsStandby
                    ? '복구된 VM이 현재 제어권을 빼앗지 않고 예비 상태로 복귀했습니다.'
                    : '복구 후 active_vm_id가 백업 VM에 유지되는지 확인하는 중입니다.'
                }
              />
            </ol>
          </div>
        )}
      </Card>
      {activeVm && (
        <Modal
          open={confirmOpen}
          title="현재 주행 VM 장애 시연"
          onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                취소
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  begin();
                  setConfirmOpen(false);
                }}
              >
                장애 발생
              </Button>
            </>
          }
        >
          <p>
            <b>{activeVm.name}</b>을 crash 상태로 만듭니다. 주행 제어권이 다른 VM으로 넘어가고 자동 복구되는지
            확인합니다.
          </p>
        </Modal>
      )}
    </div>
  );
}

function Summary({
  label,
  value,
  mono,
  health,
}: {
  label: string;
  value: string;
  mono?: string;
  health?: 'ok' | 'recovering' | 'fault' | 'stopped';
}) {
  return (
    <div className={styles.scenarioSummaryItem}>
      <span>{label}</span>
      {health ? <StatusBadge health={health} label={value} size="sm" /> : <strong>{value}</strong>}
      {mono && <small className={styles.mono}>{mono}</small>}
    </div>
  );
}

function DrivingVm({ vm, active }: { vm: Vm; active: boolean }) {
  const role = vm.role === 'primary_drive' ? ROLES.autonomous : ROLES.manual;
  return (
    <article
      className={styles.drivingVm}
      data-active={active}
      style={{ '--vm-accent': role.color } as CSSProperties}
    >
      <div>
        <span>{role.title}</span>
        {active && <b>ACTIVE</b>}
      </div>
      <h3>{vm.name}</h3>
      <p className={styles.mono}>{vm.vm_id.slice(0, 8)}</p>
      <StatusBadge health={STATE_HEALTH[vm.state]} label={STATE_LABEL[vm.state]} size="sm" />
    </article>
  );
}

function ProgressStep({
  done,
  failed = false,
  title,
  detail,
}: {
  done: boolean;
  failed?: boolean;
  title: string;
  detail: string;
}) {
  return (
    <li data-state={failed ? 'failed' : done ? 'done' : 'waiting'}>
      <i aria-hidden="true">{failed ? '!' : done ? '✓' : '·'}</i>
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </li>
  );
}
