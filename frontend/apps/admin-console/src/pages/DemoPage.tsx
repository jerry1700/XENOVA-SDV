import { Button, Card, Modal, StatusBadge } from '@xenova/ui';
import { useMemo, useState, type CSSProperties } from 'react';
import { Link } from 'react-router';
import { ApiError } from '../api/client';
import { commandErrorCode } from '../api/commandStatus';
import type { DriveMode } from '../api/drive';
import { agentErrorText, commandStateText } from '../api/errorText';
import {
  useCommandStatus,
  useEvents,
  useStartActiveDriveAutoDelay,
  useStartVmCrash,
  useVms,
} from '../api/queries';
import type { Vm } from '../api/types';
import { MODE_HEALTH, MODE_LABEL, ROLES } from '../domain/roles';
import { STATE_HEALTH, STATE_LABEL } from '../format';
import { useDrive } from '../layout/DriveContext';
import styles from './pages.module.css';

type ScenarioId = 'vm-crash' | 'delay' | 'empty-result' | 'jetson-loss';
const SCENARIOS: { id: ScenarioId; label: string; title: string; ready: boolean }[] = [
  { id: 'vm-crash', label: '시연 1', title: '주행 VM 장애', ready: true },
  { id: 'delay', label: '시연 2', title: '연산 지연 반복', ready: true },
  { id: 'empty-result', label: '시연 3', title: '연산 결과 누락', ready: false },
  { id: 'jetson-loss', label: '시연 4', title: 'Jetson 통신 단절', ready: false },
];

interface Run {
  targetId: string;
  targetName: string;
  initialMode: DriveMode | null;
  commandId: string;
  startedAt: string;
}

interface DelayRun extends Run {
  delayMs: number;
  durationS: number;
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

export function DemoPage() {
  const [selected, setSelected] = useState<ScenarioId>('vm-crash');
  const scenario = SCENARIOS.find((item) => item.id === selected)!;
  return (
    <div className={styles.page}>
      <section className={styles.pageHead}>
        <div>
          <div className={styles.eyebrow}>Simulator</div>
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
        ) : (
          <DelayScenario />
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
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const controlFresh = socket === 'open' && control != null && control.received_ago_ms < 3000;
  const start = useStartActiveDriveAutoDelay();
  const command = useCommandStatus(run?.targetId ?? '', run?.commandId ?? null);

  const commandAccepted = Boolean(
    run && command.data && ['ACCEPTED', 'RUNNING', 'SUCCEEDED'].includes(command.data.status),
  );
  const delayRunning = command.data?.status === 'RUNNING' || command.data?.status === 'SUCCEEDED';
  const commandFailed = command.data?.status === 'FAILED' || command.data?.status === 'REJECTED';
  const commandFinishedWithoutHandoff = command.data?.status === 'SUCCEEDED' && activeId === run?.targetId;
  const sawHandoff = Boolean(run && activeId && activeId !== run.targetId);
  const modePreserved = Boolean(run && sawHandoff && control?.mode === 'AUTO');
  const finished = sawHandoff && modePreserved;
  const failed = commandFailed || commandFinishedWithoutHandoff;
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
                detail={command.data?.message ?? 'Zephyr 명령 응답 대기 중'}
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
                done={sawHandoff}
                failed={commandFinishedWithoutHandoff}
                title="백업 VM으로 제어권 승계"
                detail={
                  sawHandoff
                    ? `${run.targetName}에서 ${activeVm?.name ?? activeId}으로 전환됐습니다.`
                    : commandFinishedWithoutHandoff
                      ? '지연 명령은 끝났지만 active_vm_id 변경을 확인하지 못했습니다.'
                      : '반복 deadline miss 판정과 active_vm_id 변경을 기다리는 중입니다.'
                }
              />
              <ProgressStep
                done={modePreserved}
                failed={sawHandoff && control?.mode !== 'AUTO'}
                title="AUTO 주행 유지"
                detail={`시작: ${run.initialMode ? MODE_LABEL[run.initialMode] : '미확인'} · 현재: ${control?.mode ? MODE_LABEL[control.mode] : '미확인'}`}
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

function VmCrashScenario() {
  const vms = useVms();
  const { drive, socket } = useDrive();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const drivingVms = useMemo(
    () => (vms.data?.items ?? []).filter((vm) => vm.role === 'primary_drive' || vm.role === 'backup_drive'),
    [vms.data?.items],
  );
  const control = drive?.control ?? null;
  const activeId = control?.active_vm_id ?? null;
  const activeVm = drivingVms.find((vm) => vm.vm_id === activeId);
  const target = run ? drivingVms.find((vm) => vm.vm_id === run.targetId) : undefined;
  const controlFresh = socket === 'open' && control != null && control.received_ago_ms < 3000;
  const start = useStartVmCrash(activeVm?.vm_id ?? '');
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
  const recovered = Boolean(run && sawFailure && target?.state === 'running' && recoveryFinished);
  const modePreserved = Boolean(run && sawHandoff && control?.mode === run.initialMode);
  const finished = recovered && sawHandoff && modePreserved;
  const unavailableReason = !controlFresh
    ? '주행 제어 상태가 최신이 아닙니다.'
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
    start.mutate(undefined, {
      onSuccess: (submitted) =>
        setRun({
          targetId: activeVm.vm_id,
          targetName: activeVm.name,
          initialMode: control?.mode ?? null,
          commandId: submitted.command_id,
          startedAt: submitted.submitted_at,
        }),
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
                    ? '복구 완료 이벤트와 running 상태를 모두 확인했습니다.'
                    : `현재 대상 상태: ${target ? STATE_LABEL[target.state] : '미확인'}`
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
    <div className={styles.summaryItem}>
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
