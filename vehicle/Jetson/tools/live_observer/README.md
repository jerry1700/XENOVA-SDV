# PC 폐루프 관측 수집기

현재 PC HIL 실행의 RGB/native depth·연결 단계와 기존 World/Vehicle/VM 경로·Gateway
운영 보고를 Streamlit 파일로 옮긴다. PC 실행기, 수집기, Streamlit은 별도 프로세스다.
Streamlit과 수집기는 출발·정지·주행 명령을 보내지 않는다. 제품 런타임 의존성이 아니다.

## 실행 연결

PC용 기존 환경에서 실행한다. 새 패키지 설치는 필요 없다. 공용 protobuf codec을 읽는
수집기는 기존 `protobuf`가 있는 PC 환경과 아래 `PYTHONPATH`를 사용한다.

```powershell
$env:PYTHONPATH = "$PWD/vehicle/Jetson;$PWD/platform/apps/drive/world/python"
$observer = "$PWD/vehicle/Jetson/build/live-observer-20261110"
python -B -m tools.live_observer.collector --input-dir "$observer/input" `
  --remote-input-dir "$observer/remote" --output-dir "$observer/current"
```

다른 PC 터미널에서 기존 `tools.hil_observation.pc` 명령의 나머지 인자는 그대로 두고
`--observer-dir <input>`를 추가한다. 선택적으로 `--observer-run-id <이번 진단 run>`을
지정한다. 생략한 run UUID는 표시용이며 실제 source/session/clock ID를 대체하지 않는다.
WSL 실행이면 동일 폴더를 `/mnt/c/.../input`으로 지정한다. 영상·원 bootstrap binding을
현재 실행기에서 직접 받으며, 예전 실행 폴더나 `final-current.json`을 LIVE로 가져오지 않는다.

```powershell
$env:XENOVA_VIEWER_OBSERVATION_DIR = "$observer/current"
python -m streamlit run vehicle/Jetson/tools/streamlit_viewer/app.py `
  --server.address 127.0.0.1 --server.port 18502 --server.headless true
```

시작 시 `WAITING_FOR_RUN`, 입력0건이 표시된다. 이후 실제 실행기의
`CONNECTING_JETSON → WAITING_BOOTSTRAP → WAITING_FEEDBACK → CAPTURING` 또는
`FAILED/ENDED` 보고를 따른다. 다음 run은 영상·지도·경로·입력 카운터를 분리한다.
수집기를 재시작하면 기존 파일의 새로운 교체를 기다려 완료된 기록을 현재 실행으로
표시하지 않는다. 운영 보고는 실제 Gateway 응답이 있을 때 출발 전에도 표시한다.

## 기존 Pi 읽기 전용 경로

PC의 새 브로커 계정·ACL·포트가 필요하지 않다. `remote.py`를 기존 SSH 관리자 연결에서
실행하고 stdout만 PC `relay`에 연결할 수 있다. helper는 Pi의 기존 owner-only MQTT
profile을 현장에서 읽고 **subscribe만** 한다. 인증 정보는 PC에 복사하거나 출력하지 않는다.
`xenova-drive status`와 `operation status`에 해당하는 기존 UNIX socket 조회도 수행한다.
기본 warm/mock 기동에서는 HIL용 operator socket이 생성되지 않는다. 이때는 기존
`GatewayClient.context()`의 G1 관리 채널로 기본 모드·선택 VM·출력 허가를 읽는다.
기본 관리 채널까지 응답하지 않을 때 `UNAVAILABLE` 진단을 보낸다. SSH 인증은 기존 터미널 방식이다.

```text
기존 SSH root@Pi: python3 -u -c '<remote.py의 원 코드>'
  | PC python -u -m tools.live_observer.relay --output-dir <remote 폴더>
```

`remote.py`는 PC 도구 패키지를 설치하지 않고 독립 실행할 수도 있다. 기본 profile은
`/etc/xenova/hil01/uplink.json`, socket은 기존 `/run/xenova-gateway/operator.sock`이다.
G1 기본 관리 socket은 `/run/xenova-gateway/control.sock`이고 `--provider-socket`으로
선택한다. 응답은 `gateway_context` 진단 레코드에 원문으로 보존한다. 별도 helper의
조회 순번·응답 수신 시각은 `observation`이며 Gateway의 원 발행 시각/순번을 대신하지 않는다.
G1에는 주차/READY/차량 대기 상태가 없으므로 해당 카드는 UNKNOWN이다. SAFE_STOP에서
선택된 VM을 실제 주행 담당으로 표시하지 않는다. operator 소켓 ABSENT/UNREACHABLE은
Gateway 기본 상태 응답과 별개이며 HIL 설정을 자동 활성화하거나 조작을 허용하지 않는다.
실제 설치 경로가 다르면 helper 인자로 지정한다. 코드/경로만 SSH 명령에 넣으며 비밀번호를
스크립트·명령 인자·관측 파일에 넣지 않는다. helper/SSH가 종료돼도 주행 서비스를 건드리지 않는다.

이미 승인된 PC subscriber profile이 있는 환경에서는 `--mqtt-profile <기존 private JSON>`
으로 PC에서 직접 구독할 수도 있다. 기존 Paho가 필요하며 자동 설치하지 않는다. `host`,
`port`, `username`, `password`는 기존 private 설정에서만 읽는다. 공개 설정에 복사하지 않는다.
기존 읽기 전용 CLI의 비대화형 호출이 가능하면 `--gateway-status-command <JSON>`도 지원한다.
JSON은 `{"argv":["ssh","root@Pi","xenova-drive","--json","status"]}` 형태다.
주차/운영 상태까지 볼 때는 두 응답을 주는 SSH helper를 사용한다.

수신하는 topic은 기존 `xenova/v1/world/snapshot.pb`, `vehicle/state.pb`,
`vms/+/drive/link/out`, `mapper/+/runtime`뿐이다. retained는 현재 관측으로 받지 않는다.

공용 codec은 이 프로젝트에서 검증한 protobuf6.33.6을 사용한다. 현재 PC 뷰어 환경의
protobuf7에서는 `FieldDescriptor.label`이 없어 codec import가 실패한다. 기존6.33.6
캐시의 순수 Python 파일을 별도 폴더에 두고, **수집기만** `python -S`와
`PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`으로 실행할 수 있다. 이때 `PYTHONPATH`에
해당 폴더·Jetson 도구·공용 codec을 넣는다. `-S`는 다른 site-packages의7.x upb와
섞이지 않게 한다. 뷰어 환경의 패키지를 교체하거나 제품 codec에 호환 shim을 넣지 않는다.

## 표시 의미와 영향 범위

- 수신 수는 수집기가 채택한 서로 다른 원 순번의 수다. 원 프레임 순번과 PC render/taken
  카운터도 따로 보존한다. 2Hz 화면 갱신률을 주행 FPS로 표시하지 않는다.
  `mqtt`는 PC relay의 최신 packet 사본 채택 수이며 clock packet도 포함한다. `world/path`는
  이번 PC run과 식별자가 맞는 사본을 채택한 수이므로 두 수치는 다르다.
- RGB·깊이는 원 NV12/RAW16의 사본이다. 원 촬영 구간·시각·순번·만료를 유지한다.
  화면용 PNG/NV12 변환은 원본이나 모델 전처리를 바꾸지 않는다.
- World에는 실제 인지 지도·도로 경계와 occupancy grid가 들어온다. grid의0=FREE,
  100=OCCUPIED, 255=UNKNOWN을 각각 녹색·적색·짙은색으로 표시한다.
- 경로는 실제 VM snapshot이다. Gateway가 보고한 담당 VM만 선택하며 World의
  map/frame/odom/configuration/calibration/primitive가 맞는 사본을 표시한다.
  명령이 장치에 적용됐다는 뜻은 아니다.
- 최신 Vehicle 위치는 원 ODOMETRY frame이다. World의 reference 차량 위치는
  해당 `reference_odom_seq` 시점의 MAP 관측점이다. 정확한 anchor history 없이 둘을
  합치지 않는다. plant 실제 적분 위치는 별도 PLANT frame/GT 층이며 제품에 전달하지 않는다.
- 명령 도착, 가상 장치 target 적용, 실제 가상 차량 적분 속도·위치는 독립 관측이다.
  clock packet 수를 명령 수로 세지 않고, target 속도를 실제 이동으로 표시하지 않는다.
  실물 ESP32나 실제 차체 이동 검증을 대신하지 않는다.
- 기본3초는 **표시 STALE 기준**이다. RPi 명령·입력 deadline을 바꾸지 않는다.
  collector heartbeat가 영상·경로·운영 보고의 원 관측 나이를 연장하지 않는다.
  collector 자체가 멈추면 Streamlit도 파일 STALE을 표시한다.
- PC feed의 주행 측 호출은 latest immutable 참조를 try-lock으로 교환한다. JSON/base64/
  해시/파일은 관측 daemon에서 처리한다. MQTT 큐32개, relay 최대8개 최신 파일,
  수집기 media 두 슬롯·최신 입력만 보관한다. 저장 지연/오류와 뷰어 종료는 주행 루프의
  대기·락·재시작·종료 조건이 아니다. 관측이 따라가지 못하면 표시 사본을 버린다.
  실행기가 실제 운행 owner의 종료를 확인한 뒤에만 마지막 실패 보고를 위해 최대0.3초
  전달 기회를 준다. 관측 디스크가 막히면 포기한다. 운행 중 `close()`는 대기하지 않는다.
  일시 파일 점유·쓰기 오류는 한 사본을 버리고 다음 주기에 다시 쓴다. 누적 쓰기 오류와
  최근 I/O 오류를 표시 자료에 남기며, 같은 과거 오류를 매 heartbeat 새 장애로 만들지 않는다.

`tests/test_collector.py`는 기동 실패·수신0·원 수명·run 분리·세 단계·relay를,
`tests/test_sources.py`는 기존 wire 투영을 확인한다. feed 회귀는
`tests/unit/test_live_observer_feed.py`, 화면 회귀는 `tools/streamlit_viewer/tests`다.
실제 영상·경로·움직임의 수락 여부는 실제 폐루프 실행으로 별도 확인한다.
