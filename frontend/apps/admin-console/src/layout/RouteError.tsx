import { Button } from '@xenova/ui';
import { useEffect } from 'react';
import { Link, useRouteError } from 'react-router';
import { isChunkLoadError, markReload, reloadedRecently } from './chunkReload';
import styles from './layout.module.css';

/**
 * 라우트 오류 화면 (QA 2-1·3-A2). React Router 기본 화면("Unexpected Application Error!")은
 * 메뉴까지 사라지고 영어 스택만 보인다. 콘솔 레이아웃 아래에 두면 메뉴는 남는다.
 * chunk 로드 실패는 1회 자동 새로고침한다 (표시·새로고침은 effect 에서 — 렌더는 여러 번 돌 수 있다).
 */
export function RouteError() {
  const error = useRouteError();
  const chunk = isChunkLoadError(error);
  const reloading = chunk && !reloadedRecently();

  useEffect(() => {
    console.error(error);
    if (!isChunkLoadError(error) || reloadedRecently()) return;
    markReload();
    window.location.reload();
  }, [error]);

  if (reloading) return null;
  return (
    <section className={styles.routeError} role="alert">
      <h2>화면을 표시하지 못했습니다.</h2>
      <p>
        {chunk
          ? '새 버전이 배포되었을 수 있습니다. 새로고침해 주세요.'
          : '일시적인 문제일 수 있습니다. 새로고침하거나 Overview 로 이동해 주세요.'}
      </p>
      <div>
        <Button onClick={() => window.location.reload()}>새로고침</Button>
        <Link to="/dashboard/overview">Overview로</Link>
      </div>
    </section>
  );
}
