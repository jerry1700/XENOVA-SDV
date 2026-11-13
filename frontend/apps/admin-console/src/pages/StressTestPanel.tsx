import { Button, Card, useToast } from '@xenova/ui';
import { useEffect, useRef, useState } from 'react';
import { commandErrorCode } from '../api/commandStatus';
import { agentErrorText, commandStateText, errorText } from '../api/errorText';
import { useCommandStatus, useStartStress } from '../api/queries';
import type { CommandState, StressCommand, StressRequest, Vm } from '../api/types';
import { defaultMemoryStressMib, maxMemoryStressMib, memoryStressHint } from '../domain/stressLimits';
import styles from './pages.module.css';

/** 백엔드 CpuStressRequest·MemoryStressRequest 검증 범위와 같다. 메모리 상한은 VM 별(domain/stressLimits) */
const STRESS_LIMITS = {
  workersMax: 4,
  durationSMax: 120,
} as const;

type Feedback = { tone: 'ok' | 'fault' | 'pending'; message: string; title?: string } | null;

const inRange = (value: number, max: number) => Number.isInteger(value) && value >= 1 && value <= max;

const TERMINAL = new Set<CommandState>(['SUCCEEDED', 'FAILED', 'REJECTED']);

const what = (command: StressCommand) =>
  `${command.type === 'CPU' ? `CPU 워커 ${command.amount}개` : `메모리 ${command.amount} MiB`} · ${command.duration_s}초`;

function DurationInput({
  label,
  value,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div className={styles.memoryInputGroup}>
      <input
        type="number"
        aria-label={label}
        min={1}
        max={STRESS_LIMITS.durationSMax}
        step={1}
        value={value}
        disabled={disabled}
        aria-invalid={!inRange(value, STRESS_LIMITS.durationSMax)}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <span>초</span>
    </div>
  );
}

/** Linux VM Agent 에 CPU·메모리 부하 명령(MQTT agent.command, action=stress)을 보낸다 */
export function StressTestPanel({ vm, accent }: { vm: Vm; accent: string }) {
  const stress = useStartStress(vm.vm_id);
  const toast = useToast();
  const [workers, setWorkers] = useState(1);
  const [cpuDuration, setCpuDuration] = useState(30);
  const memoryMibMax = maxMemoryStressMib(vm.memory_mib);
  const [memoryMib, setMemoryMib] = useState(() => defaultMemoryStressMib(vm.memory_mib));
  const [memoryDuration, setMemoryDuration] = useState(30);
  // 보낸 명령 (이 VM 의 것만 조회). 명령이 끝날 때까지 버튼을 잠근다
  const [sent, setSent] = useState<StressCommand | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const commandId = sent && sent.vm_id === vm.vm_id ? sent.command_id : null;
  const status = useCommandStatus(vm.vm_id, commandId);
  const state = status.data?.status;
  const finished = state !== undefined && TERMINAL.has(state);
  // 상태를 확인할 수 없으면(404·연결 실패) 잠근 채로 두지 않는다
  const running = commandId !== null && !finished && !status.isError;

  const ready = vm.state === 'running' && vm.agent_status === 'ONLINE';
  const busy = stress.isPending || running;
  const cpuValid =
    inRange(workers, STRESS_LIMITS.workersMax) && inRange(cpuDuration, STRESS_LIMITS.durationSMax);
  const memoryValid = inRange(memoryMib, memoryMibMax) && inRange(memoryDuration, STRESS_LIMITS.durationSMax);

  const start = async (input: StressRequest) => {
    setSendError(null);
    setSent(null);
    try {
      setSent(await stress.mutateAsync(input));
    } catch (error) {
      const message = errorText(error, '부하 명령을 보내지 못했습니다.');
      setSendError(message);
      toast.show(message, 'fault', 5000);
    }
  };

  // 화면에 보일 결과: 접수 응답만으로 성공이라 하지 않고 Agent 의 최종 상태(완료·실패·거절)를 기다린다
  const feedback: Feedback = sendError
    ? { tone: 'fault', message: sendError }
    : !sent || !commandId
      ? null
      : status.isError
        ? {
            tone: 'fault',
            message: errorText(status.error, '명령 상태를 확인하지 못했습니다.'),
            title: sent.command_id,
          }
        : state === 'SUCCEEDED'
          ? {
              tone: 'ok',
              message: `${commandStateText(state)} — ${what(sent)} 부하가 끝났습니다.`,
              title: sent.command_id,
            }
          : state === 'FAILED' || state === 'REJECTED'
            ? {
                tone: 'fault',
                message: `${commandStateText(state)} — ${agentErrorText(commandErrorCode(status.data))}`,
                title: sent.command_id,
              }
            : {
                tone: 'pending',
                message: `${commandStateText('RUNNING')} — ${what(sent)} 부하를 실행하고 있습니다. 끝날 때까지 시작 버튼이 잠깁니다.`,
                title: sent.command_id,
              };

  // 끝난 결과는 한 번만 토스트로도 알린다 (카드에서 벗어나 있어도 알 수 있게)
  const toasted = useRef<string | null>(null);
  useEffect(() => {
    if (!sent || !finished || toasted.current === sent.command_id) return;
    toasted.current = sent.command_id;
    if (state === 'SUCCEEDED') toast.show(`${vm.name}: 부하 테스트 완료`, 'ok');
    else toast.show(`${vm.name}: 부하 테스트 ${commandStateText(state!)}`, 'fault', 5000);
  }, [sent, finished, state, toast, vm.name]);

  return (
    <Card title="부하 테스트" accent={accent} actions={<span className={styles.pinningCardTag}>STRESS</span>}>
      <section className={styles.resourceTuningSection} aria-labelledby="stress-title">
        <div className={styles.resourceSectionHeading}>
          <div>
            <span id="stress-title">CPU · 메모리 부하 주기</span>
            <p>VM Agent가 지정한 시간 동안 부하 프로세스를 띄웁니다. 시간이 지나면 스스로 정리됩니다.</p>
          </div>
          <span className={styles.resourceEditState} data-enabled={ready || undefined}>
            {ready ? '실행 가능' : vm.state !== 'running' ? 'VM 실행 필요' : 'Agent 연결 필요'}
          </span>
        </div>

        <div className={styles.resourceCapacityGrid}>
          <div className={styles.resourceCapacityItem}>
            <span>CPU 부하</span>
            <div className={styles.vcpuOptions} role="group" aria-label="CPU 워커 수 선택">
              {Array.from({ length: STRESS_LIMITS.workersMax }, (_, index) => index + 1).map((value) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={workers === value}
                  disabled={!ready || busy}
                  onClick={() => setWorkers(value)}
                >
                  <strong>{value}</strong>
                  <small>워커</small>
                </button>
              ))}
            </div>
            <DurationInput
              label="CPU 부하 시간(초)"
              value={cpuDuration}
              disabled={!ready || busy}
              onChange={setCpuDuration}
            />
            <small>
              워커 1~{STRESS_LIMITS.workersMax}개 · 1~{STRESS_LIMITS.durationSMax}초
            </small>
            <Button
              disabled={!ready || busy || !cpuValid}
              onClick={() => void start({ kind: 'cpu', body: { workers, duration_s: cpuDuration } })}
            >
              CPU 부하 시작
            </Button>
          </div>

          <div className={styles.resourceCapacityItem}>
            <span>메모리 부하</span>
            <div className={styles.memoryInputGroup}>
              <input
                type="number"
                aria-label="메모리 부하 크기(MiB)"
                min={1}
                max={memoryMibMax}
                step={1}
                value={memoryMib}
                disabled={!ready || busy}
                aria-invalid={!inRange(memoryMib, memoryMibMax)}
                onChange={(event) => setMemoryMib(Number(event.target.value))}
              />
              <span>MiB</span>
            </div>
            <DurationInput
              label="메모리 부하 시간(초)"
              value={memoryDuration}
              disabled={!ready || busy}
              onChange={setMemoryDuration}
            />
            <small>
              {memoryStressHint(vm.memory_mib)} · 1~{STRESS_LIMITS.durationSMax}초
            </small>
            <Button
              disabled={!ready || busy || !memoryValid}
              onClick={() =>
                void start({ kind: 'memory', body: { memory_mib: memoryMib, duration_s: memoryDuration } })
              }
            >
              메모리 부하 시작
            </Button>
          </div>
        </div>

        {(!cpuValid || !memoryValid) && ready && (
          <p className={styles.resourceValidation} role="alert">
            부하 크기와 시간의 허용 범위를 확인해 주세요.
          </p>
        )}
        {!ready && (
          <p className={styles.dim}>
            VM이 실행 중이고 Linux Agent가 연결됨 상태일 때만 부하 명령을 보낼 수 있습니다.
          </p>
        )}
        {feedback && (
          <div
            className={styles.pinningFeedback}
            data-tone={feedback.tone}
            role="status"
            title={feedback.title}
            aria-busy={feedback.tone === 'pending' || undefined}
          >
            <i aria-hidden="true" />
            {feedback.message}
          </div>
        )}
      </section>
    </Card>
  );
}
