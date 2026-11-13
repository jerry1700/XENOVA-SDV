# R45 방향 바이어스 재생 시험

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

실차에서 사용할 바이어스 범위와 흰선 관측 오차는 별도로 보정해야 한다.
