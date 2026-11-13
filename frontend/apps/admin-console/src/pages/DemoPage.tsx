import { Button, Card, Modal, StatusBadge } from '@xenova/ui';
import { useState } from 'react';
import { Link } from 'react-router';
import { ApiError } from '../api/client';
import { useCommandStatus, useStartAppExit, useStartDomainKill, useVms } from '../api/queries';
import type { Vm } from '../api/types';
import { MODE_HEALTH, MODE_LABEL, ROLES, vmOf } from '../domain/roles';
import { STATE_LABEL } from '../format';
import { useDrive } from '../layout/DriveContext';
import styles from './pages.module.css';

/**
 * 시뮬레이션 제어 — VM1(주 구동 VM) 장애 주입 두 가지만 둔다 (2026-11-13 정리: 격리 시연 카드·트랙 조작·수동주행 입력·재개 제거).
 * - 자율주행 앱 종료: POST /vms/{id}/faults/app-exit — VM 은 그대로, 앱만 명령·생존 신호를 멈춘다
 *   → 게이트웨이가 수동주행으로 넘기고(MANUAL), 생존 신호 15초 무수신이면 복구 서비스가 VM 을 다시 띄운다.
 * - VM1 강제 종료: POST /vms/{id}/faults/domain-kill — 도메인을 즉시 없앤다(의도는 running 그대로)
 *   → 복구 서비스가 DOMAIN_ABSENT(60초 유예) 뒤 다시 띄운다. 정지(stop)와 달리 운영자가 끈 것으로 남지 않는다.
 * 두 API 는 계약안 단계 — 백엔드에 없으면 그 사실을 그대로 알린다.
 */
export function DemoPage() {
  return (
    <div className={styles.page}>
      <section className={styles.pageHead}>
        <div>
          <div className={styles.eyebrow}>시뮬레이션 제어</div>
          <h1>장애 주입</h1>
        </div>
      </section>
      <nav className={styles.simulatorTabs} aria-label="Simulator 하위 메뉴">
        <Link to="/simulator" aria-current="page">
          시뮬레이션 제어
        </Link>
        <Link to="/simulator/stress">부하 테스트</Link>
      </nav>
      <FaultCard />
    </div>
  );
}

type Fault = 'app' | 'vm';

const FAULT: Record<Fault, { button: string; title: string; effect: string }> = {
  app: {
    button: '자율주행 앱 종료',
    title: 'VM1 자율주행 앱 종료',
    effect:
      'VM 은 켜진 채 자율주행 앱만 멈춘다. 명령이 끊겨 수동주행으로 넘어가고, 생존 신호가 15초 끊기면 복구 서비스가 VM 을 다시 띄운다.',
  },
  vm: {
    button: 'VM1 강제 종료',
    title: 'VM1 강제 종료',
    effect:
      '전원을 뽑듯 VM 을 즉시 없앤다. 명령이 끊겨 수동주행으로 넘어가고, 약 60초 뒤 복구 서비스가 VM 을 다시 띄운다.',
  },
};

/** 404·405 는 백엔드에 아직 이 API 가 없다는 뜻 — 일반 실패와 구분해 알린다 */
export function failureText(error: unknown): string {
  if (
    error instanceof ApiError &&
    (error.status === 404 || error.status === 405) &&
    error.code !== 'VM_NOT_FOUND'
  )
    return '백엔드에 이 장애 주입 API가 아직 없습니다.';
  return error instanceof Error ? error.message : '요청이 실패했습니다.';
}

function FaultCard() {
  const vms = useVms();
  const { drive } = useDrive();
  const [confirm, setConfirm] = useState<Fault | null>(null);
  const vm = vmOf('autonomous', vms.data?.items, drive);
  const app = drive?.apps.autonomous;
  const mode = drive?.control?.mode ?? null;
  const running = vm?.state === 'running';
  return (
    <Card title={vm ? `VM1 · ${vm.name}` : 'VM1'}>
      <div className={styles.appCard} style={{ borderTop: `3px solid ${ROLES.autonomous.color}` }}>
        {!vm ? (
          <p className={styles.dim}>
            {vms.isLoading
              ? 'VM 목록을 불러오는 중…'
              : '주 구동 역할의 VM 을 찾지 못했습니다. VM 역할을 확인하세요.'}
          </p>
        ) : (
          <div className={styles.rows}>
            <div>
              <span>VM 상태</span>
              <span>{STATE_LABEL[vm.state] ?? vm.state}</span>
            </div>
            <div>
              <span>자율주행 명령</span>
              <span className={styles.mono}>{app?.command_fresh ? `${app.command_hz} Hz` : '없음'}</span>
            </div>
            <div>
              <span>자율주행 앱 생존 신호</span>
              <span className={styles.mono}>
                {app?.heartbeat_ago_ms != null ? `${(app.heartbeat_ago_ms / 1000).toFixed(1)}초 전` : '없음'}
              </span>
            </div>
            <div>
              <span>주행 모드</span>
              <span>
                {mode ? (
                  <StatusBadge health={MODE_HEALTH[mode]} label={MODE_LABEL[mode]} size="sm" />
                ) : (
                  '미확인'
                )}
              </span>
            </div>
          </div>
        )}
        <div className={styles.row}>
          {(['app', 'vm'] as const).map((fault) => (
            <Button
              key={fault}
              variant="danger"
              style={{ flex: 1 }}
              disabled={!running}
              title={vm && !running ? 'VM1 이 실행 중일 때만 쓸 수 있습니다' : undefined}
              onClick={() => setConfirm(fault)}
            >
              {FAULT[fault].button}
            </Button>
          ))}
        </div>
        {vm && <AppExitAction vm={vm} open={confirm === 'app'} onClose={() => setConfirm(null)} />}
        {vm && <DomainKillAction vm={vm} open={confirm === 'vm'} onClose={() => setConfirm(null)} />}
      </div>
    </Card>
  );
}

function ConfirmModal({
  fault,
  vm,
  open,
  onClose,
  onConfirm,
}: {
  fault: Fault;
  vm: Vm;
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      open={open}
      title={FAULT[fault].title}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            취소
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              onConfirm();
              onClose();
            }}
          >
            {FAULT[fault].button}
          </Button>
        </>
      }
    >
      <p>
        <b>{vm.name}</b> — {FAULT[fault].effect}
      </p>
    </Modal>
  );
}

function AppExitAction({ vm, open, onClose }: { vm: Vm; open: boolean; onClose: () => void }) {
  const start = useStartAppExit(vm.vm_id);
  const [commandId, setCommandId] = useState<string | null>(null);
  const status = useCommandStatus(vm.vm_id, commandId);
  const result = status.data;
  let text: string | null = null;
  let fault = false;
  if (start.isPending) text = '자율주행 앱 종료 명령을 보내는 중…';
  else if (start.isError) {
    text = `자율주행 앱 종료 실패 — ${failureText(start.error)}`;
    fault = true;
  } else if (result?.status === 'REJECTED' || result?.status === 'FAILED') {
    // 거부를 성공처럼 보이지 않게: 사유만 보인다 (DEMO_DISABLED 등)
    text = `VM 이 명령을 거부했습니다 — ${result.message ?? result.status}`;
    fault = true;
  } else if (result) text = `자율주행 앱 종료 명령 ${result.status} — 위 명령·생존 신호가 끊기는지 보세요.`;
  else if (commandId) text = '명령을 보냈습니다. VM 의 응답을 기다리는 중…';
  return (
    <>
      <ConfirmModal
        fault="app"
        vm={vm}
        open={open}
        onClose={onClose}
        onConfirm={() => {
          if (start.isPending) return;
          setCommandId(null);
          start.mutate(
            { app_id: 'autonomous' },
            { onSuccess: (command) => setCommandId(command.command_id) },
          );
        }}
      />
      {text && (
        <p className={styles.dim} data-tone={fault ? 'fault' : 'ok'} role="status">
          {text}
        </p>
      )}
    </>
  );
}

function DomainKillAction({ vm, open, onClose }: { vm: Vm; open: boolean; onClose: () => void }) {
  const kill = useStartDomainKill(vm.vm_id);
  let text: string | null = null;
  if (kill.isPending) text = 'VM1 강제 종료 요청을 보내는 중…';
  else if (kill.isError) text = `VM1 강제 종료 실패 — ${failureText(kill.error)}`;
  else if (kill.isSuccess)
    text = 'VM1 을 강제 종료했습니다. 복구 서비스가 약 60초 뒤 다시 띄웁니다 — 위 VM 상태를 보세요.';
  return (
    <>
      <ConfirmModal
        fault="vm"
        vm={vm}
        open={open}
        onClose={onClose}
        onConfirm={() => {
          if (!kill.isPending) kill.mutate();
        }}
      />
      {text && (
        <p className={styles.dim} data-tone={kill.isError ? 'fault' : 'ok'} role="status">
          {text}
        </p>
      )}
    </>
  );
}
