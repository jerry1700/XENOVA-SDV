import { useEffect } from 'react';
import { useRouteError } from 'react-router';
import { RELOAD_AFTER_MS } from './displayReload';

/**
 * 차량 화면 라우트 오류 (QA 2-1·3-A2). React Router 기본 화면("Unexpected Application Error!")이
 * HDMI 에 영어 스택으로 남지 않게, 어두운 화면에 한 줄 안내만 두고 잠시 뒤 다시 불러온다.
 */
export function DisplayRouteError() {
  const error = useRouteError();
  useEffect(() => {
    console.error(error);
    const timer = window.setTimeout(() => window.location.reload(), RELOAD_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [error]);
  return (
    <main
      role="alert"
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        background: '#05080d',
        color: '#c9d4e3',
        fontSize: '28px',
        margin: 0,
      }}
    >
      <p>차량 화면을 다시 불러오는 중입니다</p>
    </main>
  );
}
