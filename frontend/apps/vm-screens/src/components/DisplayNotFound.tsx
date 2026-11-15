import { Navigate, useLocation, useParams } from 'react-router';

/**
 * 없는 화면 주소 (S15P31E110-472). 오류 화면(DisplayRouteError)은 10초마다 새로고침하므로, 주소가 틀린 경우
 * 키오스크가 새로고침만 반복했다. 새로고침·자동 이동 없이 요청한 주소만 보인다.
 * 쿼리(토큰 등)는 화면에 내지 않는다.
 */
export function DisplayNotFound() {
  const { pathname } = useLocation();
  return (
    <main
      role="alert"
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeContent: 'center',
        gap: 12,
        padding: 24,
        textAlign: 'center',
        background: '#05080d',
        color: '#c9d4e3',
        margin: 0,
      }}
    >
      <p style={{ fontSize: '28px', margin: 0 }}>없는 화면 주소입니다.</p>
      <p style={{ fontSize: '18px', margin: 0, overflowWrap: 'anywhere' }}>
        요청한 주소: <code>{pathname}</code>
      </p>
    </main>
  );
}

/** 플랫폼 화면 출력 id (platform display/router outputs.py output_id) — 옛 모니터 주소 */
const OUTPUT_ID = /^monitor-\d+$/;

/**
 * /display/:outputId — 옛 모니터 주소(monitor-1·monitor-2)만 합친 차량 화면으로 보내고(쿼리 유지),
 * 그 밖의 /display/… 는 없는 화면 주소로 본다.
 */
export function DisplayOutput() {
  const { outputId = '' } = useParams();
  const { search } = useLocation();
  if (!OUTPUT_ID.test(outputId)) return <DisplayNotFound />;
  return <Navigate to={{ pathname: '/display/combined', search }} replace />;
}
