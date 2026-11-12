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
   plant 하나를 만들고 같은 객체를 renderer에 연결한다. 선택한
   `--capture-start-delay-s` 동안은 피드백·물리만 진행하고 영상은 생성하지 않는다.
   지연 후 현재 plant에서 새 관측을 시작하며, 이 대기는 Gateway/시계/주행 준비 승인이 아니다.
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

## 플랫폼 코스 선택

저장소 루트에서 병합된 코스를 선택하고, 기존 HIL의 개발 프로필을 전달한다.

```powershell
$env:PYTHONPATH = 'vehicle/Jetson;vehicle/Jetson/src'
python -m tools.hil_observation.prepare_course --revision origin/platform --track jetson-demo --development-profile 'path/to/development.canonical.json' --output vehicle/Jetson/build/hil-courses/jetson-demo
```

R55를 선택할 때는 `--track jetson-r55`와 별도 출력 폴더를 사용한다.
도구는 지정 커밋의 코스 카탈로그를 읽어 장면과 개발 프로필을 생성하고, 해당 파일의 SHA와
후륜축 초기 자세를 포함한 인자 배열을 출력한다. 저장소 루트에서 PC HIL을 실행하며
인자 배열로 기존 `--development-profile`·`--scene`·`--initial-pose`와 각 SHA 인자를 교체한다.
장면에는 코스 원본의 커밋과 SHA가 기록된다. 차량·카메라가 기존 개발 모델과 일치해야 내보낼 수 있다.

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
| `--capture-start-delay-s` | 개발용 피드백 선기동 시간. 기본 0초, 전체 실험 시간 이내이며 표본·기한을 재사용하지 않음 |
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

## 선택 PC 실시간 관측 피드

기존 PC 실행 인자에 `--observer-dir <명시 input 폴더>`를 더하면 별도 진단 collector가
읽을 `pc.json`을 내보낸다. 환경변수 `XENOVA_HIL_OBSERVER_DIR`도 같은 선택이다.
둘 다 없으면 관측 worker를 만들지 않으며 feed 모듈도 import하지 않는다.
`--observer-run-id <진단 run ID>`는 collector와 맞추는 식별자다. 생략하면 별도의
진단 UUID를 만들며 실제 source/session UUID를 대신하지 않는다. 폴더당 PC feed 하나만 쓴다.

파일은 개발 도구용 `pc-live-source-1`이다. 기동 중에는 `CONNECTING_JETSON`,
`WAITING_BOOTSTRAP`, `WAITING_FEEDBACK`와0프레임을 표시하고, 실제 attach 뒤에는
`CAPTURING`을 표시한다. 원 bootstrap 확인 전 `binding`, `session_id`는 null이다.
binding은 원 공개 metadata이며 자격정보가 아니다. `FAILED`/`ENDED`와 실제 원 session
종료 사유를 내보낸다. 운행 owner가 실제 정리된 CLI 종료에서만 기본0.3초 동안
최종 진단 전달을 기다리며, 디스크가 막히면 포기한다. 전달 완료는 운행 수락 조건이 아니다.

daemon 하나가 최대0.5초 간격으로 최신 자료만 변환·원자 교체한다. 호출자는 try-lock으로
작은 상태나 원 immutable frame 참조만 넘기며 운행 중 JSON/base64/hash/파일·join을 수행하지 않는다.
운행 owner가 진단 파일 저장을 기다리지 않으며 관측 실패는 운행 실패나 모델 재시작을
일으키지 않는다. 같은 PC에서 진단 변환이 소비하는 CPU와 실제 스케줄링 영향은 별도
실행에서 측정해야 한다. 이 선택을 실시간성 수락으로 해석하지 않는다.

`sequence`는 관측 heartbeat이고 `source_sequence`는 원 렌더 frame 순번이다. 같은 frame의
재게시로 원 PC capture interval, 완료 시각, expiry를 바꾸지 않는다. RGB640×360 NV12와
깊이640×480 RAW16_LE의 원 bytes는 worker에서 base64로 변환하며 전체 파일은2MiB 이하다.
`media.original_stamp`는 원 frame scope/descriptor/시각을 보존한다. PC clock, collector
수신 clock, Jetson clock을 epoch 대조 없이 빼지 않는다.

`counters`는 renderer attempted/rendered/expired/replaced/taken 등이며 전송 완료 수가 아니다.
`producer`에는 원 작은 전체 snapshot을 둔다. `peer.received`는 clock을 포함한 전체 packet 수,
`command_hwm`은 가상 장치가 수락한 명령 순번, `applied`는 target 적용 횟수다.
`plant`의 실제 적분 pose/속도·원 wheel feedback은 이동 관측이며 명령 수신·target 적용과 별개다.
이 값과 영상은 모두 HIL SYNTHETIC이며 GT pose/scene을 Jetson 입력으로 보내지 않는다.
이 feed만으로 YOLOP 결과·Jetson 지도·Pi 계획 경로가 생성됐다고 표시하지 않는다.
