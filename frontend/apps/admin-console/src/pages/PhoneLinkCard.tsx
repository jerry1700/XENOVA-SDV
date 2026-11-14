import { Button, Card, Modal, StatusBadge, type Health } from '@xenova/ui';
import { useState } from 'react';
import { errorText } from '../api/errorText';
import { useIssuePhonePairingCode, usePhoneLink } from '../api/queries';
import type { PhonePairingCode, PhoneRelayState } from '../api/types';
import { useNow } from '../useNow';
import styles from './PhoneLinkCard.module.css';
import { phoneLinkHint } from './phoneLinkHint';

const RELAY: Record<PhoneRelayState, { health: Health; label: string }> = {
  listening: { health: 'ok', label: '휴대폰 접속 가능' },
  waiting_link: { health: 'recovering', label: '휴대폰 연결 대기' },
  starting: { health: 'recovering', label: '중계 시작 중' },
  stopped: { health: 'stopped', label: '중계 꺼짐' },
  failed: { health: 'fault', label: '중계 실패' },
  unknown: { health: 'fault', label: '상태 알 수 없음' },
};

function errorMessage(error: unknown): string {
  return errorText(error);
}

function remaining(expiresAt: string, now: number): number {
  return Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
}

/**
 * USB 테더링 휴대폰에 차량 화면을 띄울 때 접속 주소와 연결번호를 보여 준다 (REST-38·REST-39).
 * 번호는 이 컴포넌트 상태에만 있고 만료·연결되면 지운다.
 */
export function PhoneLinkCard() {
  const status = usePhoneLink();
  const issue = useIssuePhonePairingCode();
  const now = useNow();
  const [confirming, setConfirming] = useState(false);
  const [issued, setIssued] = useState<(PhonePairingCode & { seenOnBoard: boolean }) | null>(null);
  const [copied, setCopied] = useState(false);

  const data = status.data;
  const secondsLeft = issued ? remaining(issued.expires_at, now) : 0;
  // 보드 상태가 이 번호(같은 만료 시각)를 한 번 보인 뒤 사라지거나 다른 번호로 바뀌면 지운다
  // (휴대폰 연결 성공 또는 다른 곳에서 재발급). 보드·브라우저 시계를 비교하지 않는다.
  const boardShowsThis = Boolean(
    issued && data?.pairing.active && data.pairing.expires_at === issued.expires_at,
  );
  const replacedOrUsed = Boolean(issued?.seenOnBoard && data && !boardShowsThis);
  const showCode = issued !== null && secondsLeft > 0 && !replacedOrUsed;

  // 렌더 중 한 번만 고치는 파생 상태 (React 권장 방식, effect 로 다시 그리지 않음)
  if (issued && boardShowsThis && !issued.seenOnBoard) setIssued({ ...issued, seenOnBoard: true });

  async function confirmIssue() {
    setConfirming(false);
    try {
      const code = await issue.mutateAsync();
      setIssued({ ...code, seenOnBoard: false });
    } catch {
      setIssued(null);
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  const relay = data ? RELAY[data.relay] : null;
  const hint = data ? phoneLinkHint(data) : null;

  return (
    <Card
      title="휴대폰 화면 연결 (USB 테더링)"
      actions={
        <Button variant="ghost" size="sm" onClick={() => status.refetch()} disabled={status.isFetching}>
          새로고침
        </Button>
      }
    >
      {status.isPending && <p className={styles.dim}>보드에서 상태를 읽는 중…</p>}
      {status.isError && !data && (
        <p className={styles.error} role="alert">
          {errorMessage(status.error)}
        </p>
      )}
      {data && relay && (
        <div className={styles.layout}>
          <div className={styles.info}>
            <StatusBadge health={relay.health} label={relay.label} />
            {data.url && (
              <div className={styles.urlRow}>
                <code className={styles.url}>{data.url}</code>
                <Button variant="secondary" size="sm" onClick={() => copy(data.url!)}>
                  {copied ? '복사됨' : '주소 복사'}
                </Button>
              </div>
            )}
            {hint && (
              <p className={styles.hint} role="note">
                {hint}
              </p>
            )}
            <dl className={styles.facts}>
              <dt>보드 주소</dt>
              <dd>{data.usb.ipv4.length ? data.usb.ipv4.join(', ') : '없음'}</dd>
              <dt>휴대폰 주소</dt>
              <dd>{data.usb.gateway ?? '없음'}</dd>
            </dl>
            <div className={styles.pairing}>
              {showCode ? (
                <div className={styles.code} aria-live="polite">
                  <span className={styles.codeLabel}>연결번호</span>
                  <strong className={styles.codeValue}>{issued.code}</strong>
                  <span className={styles.dim}>
                    {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, '0')} 남음 · 휴대폰
                    화면에 입력해 주세요
                  </span>
                </div>
              ) : (
                <p className={styles.dim}>
                  휴대폰 브라우저로 위 주소에 접속한 뒤, 연결번호를 발급해 입력합니다. 번호는 2분 동안 쓸 수
                  있습니다.
                </p>
              )}
              <Button
                onClick={() => setConfirming(true)}
                disabled={data.relay !== 'listening' || issue.isPending}
              >
                {issue.isPending ? '발급 중…' : showCode ? '연결번호 다시 발급' : '연결번호 발급'}
              </Button>
              {issue.isError && (
                <p className={styles.error} role="alert">
                  {errorMessage(issue.error)}
                </p>
              )}
            </div>
          </div>
        </div>
      )}
      <Modal
        open={confirming}
        title="연결번호를 발급할까요?"
        onClose={() => setConfirming(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirming(false)}>
              취소
            </Button>
            <Button onClick={confirmIssue}>발급</Button>
          </>
        }
      >
        <p>새 번호를 발급하면 지금 연결된 휴대폰은 끊어지고, 앞에서 발급한 번호도 쓸 수 없게 됩니다.</p>
      </Modal>
    </Card>
  );
}
