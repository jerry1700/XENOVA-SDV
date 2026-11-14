import { Navigate, useLocation } from 'react-router';

/** 옛 주소를 새 주소로 보내되 쿼리(?vm= 등)와 해시는 그대로 넘긴다 (기록에 옛 주소가 남지 않게 replace) */
export function KeepQueryRedirect({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: to, search, hash }} replace />;
}
