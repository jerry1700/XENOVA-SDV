import { Button, Card, Modal, StatusBadge, type Health } from '@xenova/ui';
import { useState } from 'react';
import { ApiError } from '../api/client';
import { useDisplayMode, useSetDisplayMode } from '../api/queries';
import type { DisplayModeName, DisplayModeStatus } from '../api/types';
import styles from './DisplayModeCard.module.css';

const LABEL: Record<DisplayModeName, string> = { hdmi: 'HDMI 모니터', mobile: '휴대폰' };

/** 선택된 모드의 서비스가 실제로 어떤 상태인지 배지로 보인다 */
function badge(status: DisplayModeStatus): { health: Health; label: string } {
  const service = status.mode === 'hdmi' ? status.services.hdmi : status.services.mobile;
  const name = `${LABEL[status.mode]} 모드`;
  if (service === 'active') return { health: 'ok', label: name };
  if (service === 'activating' || service === 'deactivating')
    return { health: 'recovering', label: `${name} 준비 중` };
  return { health: 'fault', label: `${name} · 서비스 확인 필요` };
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : '화면을 전환하지 못했습니다.';
}

/**
 * 차량 화면을 HDMI 모니터와 USB 테더링 휴대폰 중 어디에 띄울지 고른다 (REST-40·REST-41).
 * 휴대폰 모드로 바꾸면 휴대폰 중계가 켜지므로, 보드에 SSH 로 들어가지 않아도 된다.
 */
export function DisplayModeCard() {
  const status = useDisplayMode();
  const change = useSetDisplayMode();
  const [asking, setAsking] = useState<DisplayModeName | null>(null);

  const data = status.data;
  const busy = change.isPending;

  function confirm() {
    const mode = asking;
    setAsking(null);
    if (mode) change.mutate(mode);
  }

  return (
    <Card
      title="차량 화면 출력"
      actions={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => status.refetch()}
          disabled={status.isFetching || busy}
        >
          새로고침
        </Button>
      }
    >
      {status.isPending && <p className={styles.dim}>보드에서 화면 모드를 읽는 중…</p>}
      {status.isError && !data && (
        <p className={styles.error} role="alert">
          {errorMessage(status.error)}
        </p>
      )}
      {data && (
        <div className={styles.body}>
          <StatusBadge {...badge(data)} />
          <p className={styles.dim}>
            {data.mode === 'hdmi'
              ? '지금 차량 화면은 HDMI 모니터에 나옵니다. 휴대폰으로 보려면 휴대폰 모드를 켜세요.'
              : '지금 차량 화면은 휴대폰으로 나갑니다. 아래 주소로 접속하고 연결번호를 입력하세요.'}
          </p>
          <div className={styles.actions}>
            <Button onClick={() => setAsking('mobile')} disabled={busy || data.mode === 'mobile'}>
              휴대폰 모드 켜기
            </Button>
            <Button
              variant="secondary"
              onClick={() => setAsking('hdmi')}
              disabled={busy || data.mode === 'hdmi'}
            >
              HDMI로 돌아가기
            </Button>
          </div>
          {busy && (
            <p className={styles.dim} aria-live="polite">
              {LABEL[change.variables!]} 모드로 바꾸는 중입니다… 보통 몇 초, 길면 몇 분 걸릴 수 있어요.
            </p>
          )}
          {change.isError && !busy && (
            <p className={styles.error} role="alert">
              {errorMessage(change.error)}
            </p>
          )}
        </div>
      )}
      <Modal
        open={asking !== null}
        title={asking === 'mobile' ? '휴대폰 모드를 켤까요?' : 'HDMI로 돌아갈까요?'}
        onClose={() => setAsking(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setAsking(null)}>
              취소
            </Button>
            <Button onClick={confirm}>{asking === 'mobile' ? '휴대폰 모드 켜기' : 'HDMI로 돌아가기'}</Button>
          </>
        }
      >
        {asking === 'mobile' ? (
          <>
            <p>먼저 아래 두 가지를 해 주세요. 연결이 없으면 보드가 전환을 거절합니다.</p>
            <ol className={styles.steps}>
              <li>휴대폰을 USB 케이블로 보드에 연결</li>
              <li>휴대폰 설정에서 USB 테더링 켜기</li>
            </ol>
            <p>전환하면 HDMI 모니터 화면은 꺼집니다.</p>
          </>
        ) : (
          <p>휴대폰 화면 연결이 끊기고, 차량 화면이 HDMI 모니터에 다시 나옵니다.</p>
        )}
      </Modal>
    </Card>
  );
}
