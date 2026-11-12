# 실제 RPi와 가상 ESP를 잇는 개발 transport

이 도구는 **RPi의 제품 C 앱·Gateway**를 PC POSIX/WSL의 기존 `VirtualFeedbackPeer`와
`VirtualPlant`에 연결한다. Jetson은 카메라·인지·World를 담당한다. C/Gateway를 Jetson에
대체 실행하지 않으며 새 서비스·TCP sink·프로토콜·런타임 의존성도 추가하지 않는다.

기존 simulator TCP7600의 write-only DRIVE/STOP decoder와 함께 사용하지 않는다.

## 실제 배치

```text
OAK / Jetson 인지 → RPi C 앱 → RPi Gateway
                                      ↕ 기존 Feedback3 serial sink
                              /dev/pts/N (raw slave)
                                      ↕ Pi hil_serial_bridge.py
                              localhost TCP + SSH tunnel
                                      ↕ PC/WSL accepted socket FD
                              기존 VirtualFeedbackPeer
                                      ↕ 유일한 VirtualPlant
                         renderer는 같은 plant.snapshot()만 읽음
```

Pi 쪽 도구는 `platform/services/gateway/tools/hil_serial_bridge.py`다. 통합 dev에서
검토된 도구를 Pi의 명시 개발 경로로 전달하고 원 SHA256을 대조해야 한다. 기존 서비스가
이미 켜진 상태에서는 이 README만 보고 중복 Gateway를 실행하지 않는다. 실제 시연용
서비스 중지/환경 선택/기동은 운영자가 기존 운영 절차에 따라 한 번만 수행한다.

PC는 Linux 또는 WSL이며 native Windows의 POSIX FD 지원을 주장하지 않는다.
WSL에서 listener와 SSH를 함께 실행하면 Windows↔WSL 포트 노출을 가정할 필요가 없다.
양단은 IPv4 loopback만 허용한다. 예를 들어 PC/WSL에서 다음 터널을 연다.

```sh
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=1 \
  -R 17600:127.0.0.1:17600 root@192.168.30.88
```

이는 Pi의 `127.0.0.1:17600`을 같은 PC/WSL의 서버에 전달한다. SSH 권한·forward 정책과
실제 IP는 운영 환경에서 확인한다. 이미 사용 중인 포트나 기존 TCP7600 서버에 연결하지 않는다.

## PC 실행 또는 renderer 접합

하나의 coherent 통합 dev에서 `xenova_vehicle`, `xenova_gateway`, `xenova_drive_world`를
선택한다. 소스 경로 실행 예시는 다음과 같다. `.venv`와 실제 SHA는 해당 실험에서 선택한다.

```sh
export PYTHONPATH="$REPO/vehicle/Jetson/src:$REPO/platform/services/gateway:$REPO/platform/apps/drive/world/python"
python3 "$REPO/vehicle/Jetson/tools/hil_transport/feedback_peer_server.py" \
  --port 17600 --accept-timeout-s 120 --duration-s 600 \
  --poll-interval-s .002 --brake-drain-s .5 \
  --development-profile "$RUN/experiment-profile.json" --development-sha256 "$DEVELOPMENT_FILE_SHA" \
  --motion-arguments "$RUN/motion-arguments.json" --motion-sha256 "$MOTION_ARGUMENTS_FILE_SHA" \
  --feedback-profile "$RUN/feedback3-profile.json" --feedback-sha256 "$FEEDBACK_FILE_SHA" \
  --device-session "$NEW_DEVICE_SESSION_UUID" --plant-epoch "$NEW_PLANT_EPOCH_UUID" \
  --initial-pose "$INITIAL_X" "$INITIAL_Y" "$INITIAL_YAW"
```

세 파일은 실제 선택한 실험의 원 파일이다. `motion-arguments.json`은 기존
`DevelopmentMotionProfile` 생성자의 명시 인자 객체다. 첫 `development_profile` 인자는
앞 파일에서 만든다. 선택 `steering_sensor_profile`은 기존
`VirtualSteeringSensorProfile` 생성자의 5개 인자 객체 또는 `null`이다.
`feedback3-profile.json`은 Pi 설치에 선택한 `Feedback3Profile`의 정확 필드와
`StationaryPolicy` 객체다. 파일 원 SHA를 검사하며 config/firmware hash·시각 오차·J값·
lease·속도·조향 오차를 임의로 만들지 않는다. 생성자 validation도 그대로 적용한다.
이 가상 설정의 measured는 명시 가상 servo 센서 관측이며 실물 센서 수락이 아니다.
ToF source는 이 peer에 없으므로 cap0x200을 광고하지 않고 Pi 센서 목록은 `[]`로 둔다.

**renderer를 연결하는 시연에서는 위 독립 CLI와 두 번째 plant를 함께 실행하면 안 된다.**
기존 실험 owner가 아래 API를 사용해 유일한 plant를 주입한다.

```python
connection = accept_once(host="127.0.0.1", port=17600, timeout_s=120, ready=report_ready)
plant = make_selected_virtual_plant()  # accept 뒤 실제 monotonic 시작
run_peer_session(connection, plant, selected_feedback_profile,
    device_session=explicit_new_device_session, duration_s=600,
    poll_interval_s=.002, brake_drain_s=.5,
    on_session=attach_renderer_reader, cancelled=experiment_cancelled)
```

`attach_renderer_reader(plant, peer)`는 한 번 호출한다. `run_peer_session`이 같은 thread에서
기존 `peer.poll_once()`와 물리를 진행한다. renderer/카메라는 잠긴 `plant.snapshot()`의
reader여야 하며 별도 `plant.step`, `apply_target`, `peer.start`를 호출하면 안 된다.
GT pose는 renderer/evaluation용이다. 인식·지도·FREE·권한 입력으로 넘기지 않는다.
기존 실험이 이미 peer를 소유하면 그 실험의 FD 선택부를 이 accepted socket으로 바꾸고
두 peer owner를 동시에 만들지 않아야 한다.

## Pi 실행과 기존 Gateway 설정

서버와 터널이 준비된 뒤 Pi에서 bridge를 한 번 실행한다.

```sh
python3 "$RUN/hil_serial_bridge.py" --host 127.0.0.1 --port 17600 \
  --connect-timeout-s 5 --startup-timeout-s 120 --duration-s 600 --pending-age-s .1
```

출력 `hil-pty-ready-1.serial_port`의 실제 `/dev/pts/N`을 기존 Gateway 환경의
`XENOVA_GATEWAY_SERIAL_PORT`로 선택한다. 이 출력은 transport 준비이며 AUTO READY가 아니다.
기존 `XENOVA_GATEWAY_SINK=serial`, `XENOVA_GATEWAY_PROFILE=warm`,
`XENOVA_GATEWAY_SERIAL_PROFILE=feedback-3`와 설치 `XENOVA_GATEWAY_FEEDBACK3_PROFILE`·
`XENOVA_GATEWAY_ODOMETRY_CONFIG`·`XENOVA_GATEWAY_VEHICLE_LIMITS`·
`XENOVA_GATEWAY_OPERATOR_PROFILE`·`XENOVA_GATEWAY_MAPPER_UPLINK_PROFILE`을 같은 실험과 맞춘다.
PTY 접근은 실제 Gateway UID로 확인한다. 이 도구는 권한·서비스 환경을 자동 수정하지 않는다.
Gateway의 실제 HELLO→4시각→INSTALL/ACK→STOP/APPLIED→운영 준비와 새 명시 출발을
기존 actor가 수행해야 한다. write 성공·bridge 연결을 장치 적용으로 취급하지 않는다.

## 상한·종료·검증 범위

- 방향당 8KiB/32개의 원 read chunk, 회당 read4096B다. partial write는 원 offset을 유지한다.
- 원 pending 수신부터 설정 나이(`0 < pending-age-s ≤ .2`)를 넘으면 연결을 닫는다.
  새 입력/partial write는 오래된 chunk의 수명을 갱신하지 않는다. queue 초과도 종료한다.
- TCP kernel/SSH 내부 buffering은 사용자 queue 나이 검사 대상이 아니다. 원 COMMAND cutoff,
  ACK lease와 Gateway 현재성 검산은 그대로이며 네트워크 지연 때문에 READY가 거절될 수 있다.
  이를 timing 한도 완화로 숨기지 않는다. TCP는 Nagle을 끄고 socket buffer를 제한 요청한다.
- 연결은 한 번이다. 최초 PTY slave 대기는 최대120초, 세션은 최대3600초다. EOF/프로세스
  종료/시간 초과 뒤 FD를 닫고 queue를 버린다. 이전 packet·Pi session·encoder를 재시작하지 않는다.
- slave 최초 열기 전에는 보관 FD 하나만 유지하고 첫 Gateway 입력 직후 닫는다. 이후
  Gateway close는 EIO/EOF로 TCP까지 닫힌다. 원 first-write가 없으면 유한 startup timeout이다.
- peer는 원 cutoff/EOF 때 기존 target0·실제 dt 제동을 진행한다. 종료 때 simulation invalid는
  실제 속도0이나 stationary window의 수락을 뜻하지 않는다. 재연결은 새 명시 실험이다.

최소 transport 회귀 8개는 원 binary 왕복·partial-write 순서/나이·상한·PTY/TCP EOF·
최초 slave 전 EOF·단일 listener·원 profile 파일 SHA/크기를 검사한다. 실행 예시:

```sh
XENOVA_HIL_SERIAL_BRIDGE="$REPO/platform/services/gateway/tools/hil_serial_bridge.py" \
  python3 -m unittest discover -s "$REPO/vehicle/Jetson/tools/hil_transport" -p test_hil_transport.py -v
```

RPi Python3.14의 위 stdlib 시험은 8PASS/0FAIL/0ERROR/0SKIP이며 제품 module import0,
source3 변경0, 시험 자식/group 회수와 기존 Gateway·Manager·MQTT PID 불변을 확인했다.
다음 실제 RPi/PC 통합 검증에서 원 HELLO/INSTALL/ACK, 실제 COMMAND/APPLIED·움직이는
encoder/Vehicle·다음 renderer 영상, 절단/만료를 함께 확인한다.
RPi 이미지의 실제 C/Gateway 계약 설치와 Main renderer 접합이 선행 조건이다. 기존 미실행
pyserial5/실물 MCU·460800선로·물리 FREE 수락을 이 transport 시험 PASS로 대체하지 않는다.
