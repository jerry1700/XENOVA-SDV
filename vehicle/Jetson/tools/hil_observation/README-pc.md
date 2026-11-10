# 동일 PC plant의 Feedback3와 관측 송신

`python -m tools.hil_observation.pc`는 실제 RPi의 Gateway/C와 Nano의 PC 관측 상주
실행을 잇는 개발용 PC 입구다. POSIX/WSL에서 실행하며 같은 process 안의 기존
`tools.hil_transport.feedback_peer_server.run_peer_session`을 사용한다. 별도 Feedback3
서버 CLI나 두 번째 plant를 함께 켜지 않는다.

물리는 기존 peer caller 하나가 진행한다. renderer는 같은 plant의 잠긴 pose reader만
읽어 RGB NV12와 RIGHT RAW16_LE를 만든다. 그 pose와 scene은 Jetson에 전송하지 않는다.
Jetson은 실제 A의 OAK 모델 추론, M의 sensor/pose/지도 계산을 수행하고, 실제 RPi의
C/Gateway가 결과를 소비한다. PC 실행 결과는 실물 보정·정지·주행 수락이 아니다.

## 시작 순서

1. 같은 frozen source의 package, `tools`, 원 virtual-peer closure에 지정한 Gateway/common
   package를 올바른 import 경로에 둔다. Gateway package를 PC에서 실행하지 않는다.
   feedback codec·profile loader만 기존 가상 peer가 import한다. `numpy`와 `cv2`는
   이미 준비된 PC renderer 환경에서 사용하며 새 설치를 이 도구가 수행하지 않는다.
2. Nano의 `tools.hil_observation.resident` listener를 명시 실행한다. PC CLI는 그 실제
   socket 또는 caller가 만든 SSH forward에 연결한다. PC가
   `pc-observation-connected-1`을 출력하면 아직 영상·clock 수락은 시작 전이다.
3. 별도 orchestration caller가 SSH로 이번 S output의
   `artifacts/pc-source-binding.json`, `artifacts/pc-observation-scope.json`을 읽어 PC의 새
   bootstrap copy directory에 복사한다. 두 파일이 완성된 뒤에만 아래 receipt를 마지막에
   게시한다. 원 바이트 크기와 SHA는 실제 S 파일과 대조한다. credential 파일은 복사하지 않는다.
4. CLI는 receipt를 원 `--bootstrap-timeout-s` 안에서 기다린다. 잘못 게시한 receipt나
   다른 motion에 속한 descriptor는 실패로 종료한다. canonical binding/scope 검산 뒤 실제
   PC session을 먼저 시작하므로 Pi의 feedback accept를 기다리는 동안에도 A의 원 clock
   probe에 응답한다. scope 전달이 늦어 A의 원200ms probe가 만료하면 실패로 남는다.
5. 기존 Feedback3 listener의 `hil-peer-listening-1` 출력 뒤 caller가 기존 Pi serial bridge와
   실제 Gateway의 한정 HIL 설정을 선택한다. 실제 accepted socket을 받은 뒤에만 가상
   plant 하나를 만들고 `on_session=producer.attach`로 같은 객체를 renderer에 연결한다.
   Pi의 서비스·권한·MQTT·SSH tunnel 설정은 이 CLI가 바꾸지 않는다.
6. 정상 종료와 오류 모두 원 peer FD를 닫고 renderer/session의 실제 종료를 원 join cap
   안에서 확인한다. source 단절·만료 뒤 자동 재연결, AUTO 재출발, epoch 재발급은 없다.

bootstrap receipt는 최대4096B의 로컬 복사 선언이며 READY나 source authority가 아니다.
`binding`, `scope`는 receipt directory 안의 명시 상대 경로를 받는다.

```json
{
  "profile": "pc-observation-bootstrap-copy-1",
  "binding": {"path": "pc-source-binding.json", "bytes": 123, "sha256": "실제 원 파일 SHA256"},
  "scope": {"path": "pc-observation-scope.json", "bytes": 123, "sha256": "실제 원 파일 SHA256"}
}
```

예시의123과 SHA 문자열은 실행 값이 아니다. caller는 실제 이번 S 원본으로 채워야 한다.
Nano는 자기 descriptor와 실제 clock·frame·source·operation 사실을 다시 검사한다.

## 명시 실행 인자

일반적인 `--help`는 앱·renderer·연결을 시작하지 않는다. 모든 실험 값은 caller가 지정한다.

| 인자 | 의미 |
|---|---|
| `--host`, `--port`, `--accept-timeout-s` | 기존 Feedback3 단일 loopback listener와 유한 accept cap |
| `--development-profile`, `--development-sha256` | 원 public development metadata와 파일 SHA |
| `--motion-arguments`, `--motion-sha256` | 기존 server의 motion constructor 인자 JSON과 원 SHA |
| `--feedback-profile`, `--feedback-sha256` | 기존 Feedback3 profile과 원 SHA |
| `--device-session`, `--plant-epoch`, `--initial-pose X Y YAW` | 서로 구분한 실험 신원과 명시 후륜축 초기 pose(m/rad) |
| `--scene`, `--scene-sha256` | 기존 canonical scene과 원 SHA |
| `--capture-epoch` | 이번 renderer의 canonical nonzero UUID |
| `--capture-lifetime-us`, `--render-interval-us` | 원 capture 수명(최대500000us)과 명시 렌더 간격 |
| `--observation-host`, `--observation-port`, `--observation-connect-timeout-s` | 실제 Nano 또는 기존 SSH forward의 numeric IPv4 연결 |
| `--bootstrap-receipt`, `--bootstrap-timeout-s` | 이번 공개 metadata copy receipt와 최초 대기 cap |
| `--observation-duration-s` | 원 PC clock/frame session의 전체 시간 한도 |
| `--duration-s`, `--poll-interval-s`, `--brake-drain-s` | 원 peer loop/물리 poll/단절 후 유한 제동 구간 |
| `--join-timeout-s` | 종료 요청 때 한 번 정한 renderer/session 회수 cap |

원 profile3은 HIL 설정에서 `experiment-profile.json`(2063B, `32f79148…`),
`motion-arguments.json`(650B, `78888b4e…`), `feedback3.json`(621B, `99c9cb17…`)이다.
motion constructor 인자 파일 SHA와 whole motion bundle `d63ee963…`는 다르다.
virtual firmware `1189701d…`는 원 virtual-peer source closure12의 SHA이며 실물 ARM
펌웨어 해시가 아니다. 전체 SHA와 원 파일은 이번 frozen cohort의 public inputs를 사용한다.

## 시간·큐·종료 경계

renderer의 `WallPoseSample` before/after는 실제 PC monotonic 구간이다. 내부 wall capture의
이름에 있는 `j_us`나 `jetson-monotonic` label을 wire에 전송하지 않는다. 기존
`ObservationFrame`의 PC capture interval·render 완료·원 E에 실제 PC 값만 투영한다.
render가 원 E를 넘기면 drop하며 완료 시각으로 capture/E를 다시 만들지 않는다.

renderer active1/latest1과 기존 session의 active/pending frame은 별도 소유다.
session callback은 latest 원 DTO take만 하며 그 안에서 렌더·파일·JSON을 계산하지 않는다.
모듈 import는 plant 생성 전에 준비하지만 실제 렌더/poll 비용이 원100ms continuity나
Feedback3 10ms cutoff를 만족하는지는 실행에서 확인한다. 부하 때문에 수명을 늘리지 않는다.

출력의 `session_done`, `failed`, `closed`, `threads_alive`는 실제 sender 종료 진단이다.
target0·peer FD close·renderer 종료가 실제 stationary나 STOP/APPLIED를 뜻하지 않는다.
actual current Vehicle/clock/Source/World와 RPi의 제어·정지 수락은 각 실제 owner가 판단한다.
