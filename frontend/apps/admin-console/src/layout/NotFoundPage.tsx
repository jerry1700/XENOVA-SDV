import { Button } from '@xenova/ui';
import { useLocation, useNavigate } from 'react-router';
import styles from './layout.module.css';

/** 없는 주소 (S15P31E110-472). 메뉴(ConsoleLayout)는 남기고 안내와 Overview 이동만 둔다 */
export function NotFoundPage() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  return (
    <section className={styles.routeError} role="alert">
      <h2>없는 주소입니다.</h2>
      <p>
        요청한 주소 <code style={{ overflowWrap: 'anywhere' }}>{pathname}</code> 에 해당하는 화면이 없습니다.
      </p>
      <div>
        <Button onClick={() => navigate('/dashboard/overview')}>Overview로 이동</Button>
      </div>
    </section>
  );
}
