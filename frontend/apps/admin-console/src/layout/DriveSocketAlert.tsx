import { useDrive } from './DriveContext';
import styles from './layout.module.css';

/**
 * 주행 상태 소켓이 다시 붙어도 같은 이유로 닫히는 종료 코드로 끝나면(api/drive REJECTED_CLOSE_CODES)
 * 재연결을 멈추므로, 화면이 조용히 멈춘 것처럼 보이지 않게 알린다 (S15P31E110-469)
 */
export function DriveSocketAlert() {
  const { socket, invalid } = useDrive();
  if (socket !== 'rejected') {
    // 형식이 틀린 주행 상태 프레임은 버린다 (api/drive isDriveState). 정상 프레임이 다시 오면 내려간다
    if (!invalid) return null;
    return (
      <div className={styles.liveAlert} role="alert">
        <strong>주행 상태를 읽지 못했습니다</strong>
        <p>서버가 보낸 주행 상태의 형식이 올바르지 않아 마지막으로 받은 상태를 보여 줍니다.</p>
      </div>
    );
  }
  return (
    <div className={styles.liveAlert} role="alert">
      <strong>주행 상태 연결이 거부되었습니다</strong>
      <p>서버가 연결을 거부해 다시 연결하지 않습니다. 새로고침하거나 서버 설정을 확인해 주세요.</p>
    </div>
  );
}
