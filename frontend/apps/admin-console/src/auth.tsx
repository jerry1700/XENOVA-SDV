import { Button } from '@xenova/ui';
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { getSession, onSessionChange, refreshSession } from './api/client';

function useSession() {
  return useSyncExternalStore(onSessionChange, getSession, getSession);
}

/**
 * 콘솔 화면. 로그인 화면은 없다: Access Token 이 없으면 refresh 쿠키로 복원하고, 그것도 안 되면 공용 계정으로
 * 자동 로그인한다(api/client.ts refreshSession). 둘 다 실패하면(서버 연결 불가·공용 계정 없음) 안내와 재시도만 보인다.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const [state, setState] = useState<'connecting' | 'failed'>('connecting');

  const connect = () => {
    setState('connecting');
    return refreshSession().then((ok) => {
      if (!ok) setState('failed');
      return ok;
    });
  };

  useEffect(() => {
    if (session !== null) return;
    let alive = true;
    refreshSession().then((ok) => alive && !ok && setState('failed'));
    return () => {
      alive = false;
    };
    // 첫 진입 때만 연결한다. 이후 401 은 요청마다 refreshSession 이 다시 연결한다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (session) return children;
  if (state === 'connecting') return <p style={{ padding: 24 }}>콘솔에 연결하는 중…</p>;
  return (
    <div role="alert" style={{ padding: 24, display: 'grid', gap: 12, maxWidth: 520 }}>
      <strong>콘솔 서버에 연결하지 못했습니다.</strong>
      <span>서버가 켜져 있는지, 공용 콘솔 계정이 준비돼 있는지 확인해 주세요.</span>
      <div>
        <Button onClick={() => void connect()}>다시 연결</Button>
      </div>
    </div>
  );
}
