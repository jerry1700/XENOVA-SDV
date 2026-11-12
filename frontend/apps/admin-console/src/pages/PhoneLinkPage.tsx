import { PhoneLinkCard } from './PhoneLinkCard';
import styles from './pages.module.css';

/**
 * 휴대폰 화면 연결. 휴대폰이 열 접속 주소를 보여 주고 연결번호를 발급한다.
 * 차량 화면은 휴대폰 브라우저가 그 주소로 직접 연다 — 이 콘솔은 화면을 띄우지 않는다.
 * 경로가 /screens 가 아닌 이유: 배포 Nginx 가 /screens/ 를 차량 화면 앱(vm-screens)으로 넘겨서,
 * 같은 주소를 쓰면 새로고침·직접 입력 때 콘솔 대신 차량 화면이 열린다.
 */
export function PhoneLinkPage() {
  return (
    <div className={styles.detail}>
      <PhoneLinkCard />
    </div>
  );
}
