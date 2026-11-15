# lane_drive

젯슨이 OAK 영상의 차선 표시와 RPi4 가 돌려주는 엔코더 자세로 속도와 조향각을 정하고, RPi4 의 중계 도구가 ESP32 에 DRIVE 로 넘긴다. 주행 VM 과 World 는 쓰지 않는다. 트랙은 반시계 방향으로 돈다고 본다.

## 구성

| 파일 | 하는 일 |
| --- | --- |
| `ground.py` | 영상을 바닥 격자로 펴서 흰색 표시를 점선 조각과 실선으로 나눈다 |
| `lane.py` | 직선 구간의 차로 방향과 가운데를 구하고 가운데 선을 따라가는 조향각을 정한다 |
| `path.py` | 엔코더 좌표에 모서리 경로와 차로 변경 경로를 그리고 따라가는 조향각을 정한다 |
| `driver.py` | 위 셋을 묶은 주행 판단. 카메라와 통신에서 떨어져 있다 |
| `obstacle.py` | 깊이 영상에서 바닥보다 높은 점을 찾아 차로마다 가장 가까운 물체까지의 거리를 구한다 |
| `drive.py` | 젯슨에서 실행. 카메라를 열고 `driver.py` 의 값을 RPi4 로 보낸다 |
| `pi_bridge.py` | RPi4 에서 실행. 받은 값을 ESP32 에 보내고 엔코더 자세를 돌려준다 |
| `pi_bridge_run.sh` | RPi4 에서 게이트웨이를 멈추고 `pi_bridge.py` 를 돌린 뒤 게이트웨이를 다시 켠다 |
| `arc_send.py` | RPi4 에서 정한 속도·조향을 중계 도구에 보내 회전 반경을 잰다 |
| `sim.py` | 트랙과 차를 흉내 낸 모형으로 `driver.py` 를 돌린다 |
| `look.py`, `probe.py` | 지금 보이는 표시를 한 장 찍거나 녹화한 장으로 관측을 확인한다 |

## 실행

RPi4 (ESP32 통신 도구 `vehicle/ESP32/tools/fb3.py`, `link_bench.py` 와 `pi_bridge.py`, `pi_bridge_run.sh` 를 한 폴더에 둔다). 게이트웨이가 시리얼을 쥐고 있으면 중계 도구가 열지 못하므로, `pi_bridge_run.sh` 가 게이트웨이를 멈추고 중계 도구를 돌린 뒤 다시 켠다:

```
sh pi_bridge_run.sh run.out --seconds 30 --max-mmps 130 --max-m 1.2            # 찍기만
sh pi_bridge_run.sh run.out --seconds 118 --max-mmps 130 --max-m 9.5 --drive   # 바퀴가 돈다
```

RPi4 의 Wi-Fi 가 절전 상태면 젯슨이 보낸 값이 중계 도구에 닿지 않는다. 달리는 동안 RPi4 에 SSH 창을 하나 열어 `tail -f run.out` 을 띄워 둔다.

젯슨 (`vehicle/Jetson` 에서, `PYTHONPATH` 에 `src` 를 넣고):

```
python -m tools.lane_drive.drive <보정 json> <mapper json> --pi <RPi4 주소> --seconds 8 --mmps 0 --obstacles   # 보기만
python -m tools.lane_drive.drive <보정 json> <mapper json> --pi <RPi4 주소> --seconds 14 --mmps 120            # 직선만
python -m tools.lane_drive.drive <보정 json> <mapper json> --pi <RPi4 주소> --seconds 110 --mmps 120 --corner-mmps 100     --steer-bias-mrad 30 --corners --long 1.51 --short 0.46 --obstacles --stay-inner --keep-right        # 한 바퀴 넘게, 물체를 피하며
```

마지막 줄이 2026-11-15 에 실차에서 9.5 m 를 달린 설정이다. 차는 긴 직선 시작 쪽의 바깥 차로에 놓는다. `--steer-bias-mrad 30` 은 이 차의 조향 가운데가 치우친 만큼이다.

회전 반경 재기 (RPi4, 중계 도구를 `--drive` 로 띄운 뒤. 조향이 자리 잡은 뒤의 엔코더로 구간마다 반경을 적는다):

```
python3 arc_send.py 100:490:6 100:-490:6     # 속도 mm/s : 조향 mrad : 초
```

모형:

```
python -m tools.lane_drive.sim --know-short --laps 2
python -m tools.lane_drive.sim --know-short --laps 2.2 --obstacle 1.2:right --stay-inner --keep-right
python -m tools.lane_drive.sim --know-short --laps 0.62 --quiet --trace 0.15
```

## 멈추는 조건

- 젯슨: 경로에서 0.2 m 넘게 벗어나거나, 엔코더 자세가 차가 돌 수 없는 만큼 급하게 돌거나, 속도를 보내는데 1.5초 동안 엔코더 자세가 안 움직이면 서고 다시 달리지 않는다.
- 젯슨: 직선 차로가 0.5초 넘게 안 보이거나, 경로를 따라가는 중에 RPi4 의 답이 0.3초 넘게 끊기면 속도 0 을 보낸다.
- RPi4: 젯슨의 값이 0.3초 넘게 끊기면 STOP 을 보낸다. `--seconds`, `--max-m` 을 넘으면 STOP 하고 끝낸다.
- ESP32: DRIVE 하나의 기한은 80 ms 다. 앞 ToF 가 가까우면 스스로 멈춘다.

`--obstacles` 를 주면 깊이 영상에서 두 차로 안의 물체만 본다. 직선에서 내 차로의 물체가 0.95\~1.3 m 앞이고 옆 차로가 비어 있으면 차로를 옮기고, 못 옮기면 0.85 m 앞에서 선다. 모서리를 돌거나 차로를 옮기는 동안에는 깊이 영상으로 서지 않는다.

## 트랙에 맞춘 값

차로 폭 0.285 m, 긴 변 직선 1.51 m, 짧은 변 직선 0.46 m 는 2026-11-15 에 이 트랙에서 잰 값이고, 가운데 점선의 모서리 반경 0.47 m 는 그날 주행 기록의 점선 조각을 원에 맞춰 얻은 값이다(`path.py`, `lane.py`, `sim.py`).

차는 폭 0.23 m, 뒤 차축에서 앞 범퍼까지 0.30 m 다. 바깥 차로(가운데 반경 0.61 m)를 돌 때 차가 쓸고 가는 폭이 0.29 m 라 차로 폭과 거의 같다. 그래서 모서리에서는 차로 가운데보다 4 cm 안쪽을 돈다. 안쪽 차로(가운데 반경 0.33 m)는 조향 0.49 rad 로 돌 수는 있지만 앞 범퍼의 바깥 모서리가 가운데 점선을 6 cm 넘게 넘는다. 기본 동작은 안쪽 차로에서 모서리를 만나면 바깥 차로로 나오는 것이고, `--stay-inner` 를 주면 안쪽 차로를 그대로 돈다. 네 바퀴만 보면 뒤 차축이 반경 0.30\~0.32 m 로 돌 때 안쪽 차로의 두 선 안에 들어간다. `--keep-right` 를 같이 주면 안쪽 차로로 모서리를 돌고 나온 직선에서 바깥 차로가 비어 있을 때 바깥 차로로 돌아간다.

모서리를 도는 동안에는 내 차로가 안 보여 엔코더 좌표에 그린 경로를 따라간다. 그동안 앞에 보이는 휘어진 점선과 실선이 트랙 모서리의 중심에서 정해진 반경에 오도록 경로를 조금씩 옮긴다(`--no-anchor` 로 끈다).

조향은 ±0.49 rad 까지 보낸다. ESP32 펌웨어가 그만큼을 받아야 한다(350 mrad 까지만 받는 펌웨어는 넘는 값을 RANGE 로 거절한다).
