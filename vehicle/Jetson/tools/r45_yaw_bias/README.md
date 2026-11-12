# R45 방향 바이어스 재생 시험

## 사전 지도를 사용하는 오프라인 대조

R45의 기록된 20ms 차량 궤적에서 RGB와 깊이를 만들고, 주행 중 가상 엔코더에 방향 바이어스를 주입한다. 차로 폭 28.5cm, 카메라 높이 14cm, 차체 폭 21cm와 길이 38cm를 적용한다. 스칼라 칼만 필터는 흰선의 방향으로 보정량을 추정한다. 흰선 관측으로 제한한 바이어스 범위와 실제로 적용한 보정량을 사용해, 정합 기준점 이후 남을 수 있는 방향·위치 오차를 기존 5cm 지도 판정에 반영한다. 이 코드는 정적 R45 지도와 시작 방향이 맞춰진 오프라인 비교용이다.

저장소 루트의 Linux 또는 WSL 환경에서 기존 Jetson·Gateway 개발 의존성을 사용한다. 입력은 이전 R45 기록의 JSON(`trace`에 연속 `step`과 `pose`, `final`에 마지막 자세), R45 장면 JSON, OAK RGB EEPROM JSON, 그리고 `development-motion.json`, `mapper-profile.json`, `perception-calibration.json`이 들어 있는 프로필 디렉터리다.

이 시험에서 사용한 원본 경로는 `vehicle/Jetson/build` 아래에 있다.

- 29.38초 궤적은 `pi-course-tracking-20261111/r45-allowance-check/12mm/front/run.json`이다.
- 32.52초 궤적은 `offline-algorithms-20261111/r45-lap-and-p2-first-lap-drive.json`이다.
- 장면과 RGB EEPROM은 `offline-algorithms-20261111`의 `r45-marked-cones-shared.json`과 `oak-eeprom-r55.json`이다.
- 프로필 디렉터리는 `hil-live-20261111/jetson-run02/artifacts`다.

```bash
export PYTHONPATH=platform/apps/drive/world/python:platform/services/gateway:vehicle/Jetson/src

python vehicle/Jetson/tools/r45_yaw_bias/r45_yaw_bias_trial.py \
  --trace path/to/run.json --scene path/to/r45-marked-cones-shared.json \
  --profiles path/to/artifacts --rgb-eeprom path/to/oak-eeprom-r55.json \
  --rate 0.001 --filter --residual-bounds \
  --output path/to/results/r45-yaw-residual-quantized-positive-metrics.json

python vehicle/Jetson/tools/r45_yaw_bias/check_r45_residual_math.py
python vehicle/Jetson/tools/r45_yaw_bias/audit_r45_residual_replays.py path/to/results
```

검산은 29.38초 기록에서 각각 `+0.001`, `-0.001`, `0` rad/s를 주입한 `positive`, `negative`, `zero` 결과와 32.52초 기록에서 `+0.0006` rad/s를 주입한 `heldout-p2` 결과를 읽는다. 출력 파일명은 위 예의 `positive` 부분을 바꿔 사용한다. 32.52초 입력은 주차 시험 기록의 1,626번째 20ms 표본에서 자른 첫 바퀴다. 비교에서 지도 위치 출력은 각각 148/148, 148/148, 148/148, 146/164였다. 콘 B는 네 경우 모두 17.8초에 지도에 처음 반영됐다. 32.52초 기록의 28.2~31.6초에는 위치 상한이 5cm를 넘어 18프레임이 격리됐다.

## 사전 지도 없이 런타임 경로 재생

위의 사전 지도 대조와 같은 입력에 `--filter`와 `--residual-bounds` 대신
`--online-map`을 사용한다. `+0.001`, `-0.001`, `0 rad/s`를 각각 실행한다. 각 조건에서
`--no-online-filter`를 추가하면 기존 위치 전파를 쓰는 대조군을 얻는다.
이 경로는 원점 `(0, 0, 0)`의 엔코더와 빈 Jetson 지도에서 출발한다. 장면과 궤적은
가상 센서 생성 및 결과 채점에 쓰인다. 코스의 흰선 지도는 런타임에 전달하지 않는다.

같은 지도 흰선의 과거 관측과 새 RGB에서 겹치는 구간을 비교한다. 추정한
바이어스와 잔여 오차는 원 휠 이동 기록을 사용해 지도 자세에 전파한다.
출력의 `yaw_bias`는 추정 범위와 해당 프레임의 보정 전파 채택 여부를 기록한다.
관측이 부족하거나 보정 전파가 위치·방향 허용치에 불리하면 기존 전파를 사용한다.
이 시험의 각도 오차 여유와 바이어스 모델은 실제 OAK·엔코더 데이터로 검증해야 한다.

29.38초 기록의 빈 지도 재생에서 보정을 켜면 위치 출력은 양수 121/148, 음수 125/148,
무바이어스 123/148이었다. 보정을 끈 대조군은 각각 88/148, 91/148, 89/148이었다.
세 조건에서 콘 B가 처음 지도에 반영된 시각은 17.8초였다. 21~26초에는 위치 오차
상한이 5cm를 넘은 프레임이 남았다. 이 결과는 기본 점선 길이 25cm인 R45 렌더러로 얻었다.

초기 엔코더 양자화를 반영하기 전의 4~5cm 점선 R45 재생에서는 개발용 mapper 프로필의
`pipeline.registration.max_position_error_m`에 `0.06`을 적용해 29.38초 동안 위치를 출력했다.
이 값은 정합 결과와 오도메트리 전파 위치의 허용 오차 상한이다. 새 시각 정합의 유효기간이
지나면 출력 자세의 상태는 `ODOMETRY_CONTINUED`가 된다.
이 재생의 정규 형식 프로필과 입력 파일은 `build/r45-tape-v185-20261112/r45-continuous-60`에 저장했다.
같은 디렉터리를 `audit_threshold_registration.py`의 `--profiles-dir`에 지정하면 짧은 점선을 쓰는
재생에서 다시 확인할 수 있다. 기본 5cm와 5.5cm에서는 각각 32프레임, 3프레임의 위치 단절이 있었다.

첫 영상 정합 전부터 엔코더 양자화와 경과 시간에 따른 드리프트를 포함한 6cm 재생에서는
양수 145/148, 음수 147/148, 무바이어스 146/148프레임에서 위치를 출력했다.
세 조건의 모든 후보 자세에서 기준 위치·방향 오차가 각 상한 안에 들었다. 21.4~21.8초에는
흰선 대응이 끊기고 위치 상한이 최대 6.16cm까지 올라갔다. 14.8초의 대응점 4개 정합과
17.8초의 콘 B 지도 반영은 세 조건에서 유지됐다.

상한을 `0.065`m로 조정한 재생에서 세 조건 모두 148/148프레임 위치를 출력했다.
14.8초 정합과 17.8초 콘 B 지도 반영도 유지됐다. 실측 코스 입력 생성기는 이 값을
개발용 mapper 프로필에 기록한다.
