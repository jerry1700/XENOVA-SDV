import { Card, StatusBadge } from '@xenova/ui';
import styles from './pages.module.css';
import { useVisionStream, type VisionStreamState } from './useVisionStream';

const UNAVAILABLE_TEXT: Record<Extract<VisionStreamState, { status: 'unavailable' }>['code'], string> = {
  VISION_NOT_CONFIGURED: 'Jetson 카메라 주소가 아직 설정되지 않았습니다. (서버 JETSON_VIDEO_URL)',
  VISION_UNAVAILABLE: 'Jetson 카메라 영상을 받을 수 없습니다. Jetson 영상 서버와 연결을 확인해 주세요.',
  NETWORK: '서버에 연결할 수 없습니다.',
  AUTH: '로그인이 필요합니다.',
};

/** Jetson 카메라 화면(S15P31E110-306). 백엔드 /api/v1/vision/snapshot 을 초당 4장까지 받아 보여 준다. */
export function VisionPage() {
  const stream = useVisionStream();
  const live = stream.status === 'live';
  return (
    <div className={styles.page}>
      <section className={styles.pageHead}>
        <div>
          <div className={styles.eyebrow}>VISION · JETSON</div>
          <h1>카메라·인지 모니터링</h1>
        </div>
        <span className={styles.grow} />
        <StatusBadge
          health={live ? 'ok' : 'stopped'}
          label={live ? '영상 수신 중' : stream.status === 'loading' ? '연결 중' : '영상 없음'}
          size="sm"
        />
      </section>
      <div className={styles.cols2}>
        <Card title="카메라 스트림">
          {live ? (
            <img
              className={styles.visionFrame}
              src={stream.url}
              alt="Jetson 전방 카메라 실시간 영상"
              width={640}
              height={360}
            />
          ) : (
            <div className={styles.placeholder} role="status">
              <strong>{stream.status === 'loading' ? '영상을 불러오는 중' : '영상 없음'}</strong>
              <span>
                {stream.status === 'unavailable'
                  ? UNAVAILABLE_TEXT[stream.code]
                  : 'Jetson 카메라에 연결하는 중…'}
              </span>
            </div>
          )}
        </Card>
        <Card title="인지 상태">
          <div className={styles.rows}>
            <div>
              <span>Jetson 카메라</span>
              <span>{live ? '연결됨' : stream.status === 'loading' ? '확인 중' : '연결 안 됨'}</span>
            </div>
            <div>
              <span>AI 인식 프로세스</span>
              <span>확인 대기</span>
            </div>
            <div>
              <span>CPU · GPU · Memory</span>
              <span>메트릭 연동 예정</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
