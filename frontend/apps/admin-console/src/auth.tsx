import { Button } from '@xenova/ui';
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { getSession, onSessionChange, refreshSession } from './api/client';

function useSession() {
  return useSyncExternalStore(onSessionChange, getSession, getSession);
}

/** 자동 재시도 간격 — 마지막 값(30초)으로 계속 반복한다 (S15P31E110-472) */
const RETRY_DELAYS_MS = [2_000, 5_000, 10_000, 30_000] as const;

const retryDelay = (failures: number) => RETRY_DELAYS_MS[Math.min(failures, RETRY_DELAYS_MS.length - 1)]!;

/**
 * 콘솔 화면. 로그인 화면은 없다: Access Token 이 없으면 refresh 쿠키로 복원하고, 그것도 안 되면 공용 계정으로
 * 자동 로그인한다(api/client.ts refreshSession). 둘 다 실패하면(서버 연결 불가·공용 계정 없음) 안내와 재시도만 보인다.
 *
 * 쓰는 중에 세션이 사라져도(refresh·공용 로그인 실패) 같은 자리에서 "연결이 끊겼습니다"로 바꾸고
 * 2초 → 5초 → 10초 → 30초(반복) 간격으로 다시 연결한다. 주소는 그대로라 다시 연결되면 보던 화면으로 돌아간다.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const [connecting, setConnecting] = useState(true);
  /** 다음 자동 재시도까지 간격(ms). 시도 중이면 null */
  const [nextRetryMs, setNextRetryMs] = useState<number | null>(null);
  /** [다시 연결] 누를 때마다 바뀌어 재시도 루프를 처음부터 다시 돈다 */
  const [manual, setManual] = useState(0);
  /** 이번 루프를 [다시 연결]이 시작했나 (그러면 첫 시도를 기다리지 않는다) */
  const manualRequested = useRef(false);
  /** 한 번이라도 연결됐나 — 처음 연결 실패와 쓰는 중 끊김의 문구를 가른다 (렌더 중에만 갱신) */
  const [everConnected, setEverConnected] = useState(session !== null);
  if (session !== null && !everConnected) setEverConnected(true);
  const disconnected = session === null;
  const lost = disconnected && everConnected;

  useEffect(() => {
    if (!disconnected) return;
    let alive = true;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      const delay = retryDelay(failures);
      failures += 1;
      setConnecting(false);
      setNextRetryMs(delay);
      timer = setTimeout(attempt, delay);
    };
    function attempt() {
      setConnecting(true);
      setNextRetryMs(null);
      void refreshSession().then((ok) => {
        if (!alive) return;
        if (ok) setConnecting(false);
        else schedule();
      });
    }

    // 처음 진입·[다시 연결]은 바로, 쓰는 중 끊김은 방금 재발급이 실패했으므로 첫 간격(2초) 뒤에 다시 시도한다
    const immediate = !everConnected || manualRequested.current;
    manualRequested.current = false;
    timer = setTimeout(immediate ? attempt : schedule, 0);

    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [disconnected, manual, everConnected]);

  if (session) return children;
  if (!lost && connecting) return <p style={{ padding: 24 }}>콘솔에 연결하는 중…</p>;
  return (
    <div role="alert" style={{ padding: 24, display: 'grid', gap: 12, maxWidth: 520 }}>
      <strong>{lost ? '콘솔 연결이 끊겼습니다.' : '콘솔 서버에 연결하지 못했습니다.'}</strong>
      <span>서버가 켜져 있는지, 공용 콘솔 계정이 준비돼 있는지 확인해 주세요.</span>
      <span role="status">
        {connecting
          ? '다시 연결하는 중…'
          : nextRetryMs !== null
            ? `${nextRetryMs / 1000}초 뒤 자동으로 다시 연결합니다.`
            : ''}
      </span>
      <div>
        <Button
          onClick={() => {
            manualRequested.current = true;
            setManual((value) => value + 1);
          }}
          disabled={connecting}
        >
          다시 연결
        </Button>
      </div>
    </div>
  );
}
