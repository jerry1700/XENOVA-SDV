import { PhoneLinkCard } from './PhoneLinkCard';
import styles from './pages.module.css';

/**
 * 휴대폰 화면 연결. 휴대폰이 열 접속 주소를 보여 주고 연결번호를 발급한다.
 * 차량 화면은 휴대폰 브라우저가 그 주소로 직접 연다 — 이 콘솔은 화면을 띄우지 않는다.
 */
export function ScreensPage() {
  return (
    <div className={styles.detail}>
      <PhoneLinkCard />
    </div>
  );
}
