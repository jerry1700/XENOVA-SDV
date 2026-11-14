# 실차 프로필 xenova-rc-1

`xenova-rc-1.json`은 2026-11-15에 잰 실차 치수를 기존 `warm-lattice-lab` 정책에 넣은
**시험용(`kind=test`) 차량 프로필**이다. 생성 산출물은 `xenova-rc-1/`에 둔다.
기존 시험 차량 `lattice-lab-1.json`(rc-test-1 / encoder-lab-1)과 `planner/generated/`는
그대로 두며, 선택하지 않으면 지금까지와 같은 펌웨어가 나온다.

## 왜 별도 프로필인가

보드의 주행 VM 1.0.11은 `warm-lattice-lab` 빌드다. 이 빌드는 원래 `profiles/lattice-lab-1.json` →
`planner/generated/primitives.c`로 고정돼 있다. 그 안의 `.vehicle="rc-test-1"`,
`.calibration="encoder-lab-1"`이 게이트웨이 Vehicle의 이름과 다르면
`lattice_app.c`의 `current_vehicle_role()`이 Vehicle을 거절한다(-EXDEV).
그러면 MANUAL 앱은 `POSE_UNAVAILABLE`이 된다.
`lattice-lab-1.json`은 `course-development-1`의 정책 원본과 C 시험 기준값으로도 쓰인다.
그래서 이 파일은 고치지 않고 차량만 바꾸는 선택지를 추가했다.

## 값

| 항목 | 값 | 출처 |
|---|---|---|
| vehicle / calibration | `xenova-rc-1` / `encoder-measured-1` | 차량 이름은 게이트웨이 이름을 따름. 보정 이름은 실측 반영을 드러내도록 새로 지음 |
| `id`(World 프로필) | `lattice-lab-1` | VM provisioning의 `world_profile`과 맞추려고 유지 |
| `wheelbase` | 0.167 m | 실측 |
| `front` / `rear` | 0.30 / 0.10 m | 길이 0.40 m 실측. 카메라가 뒤 차축에서 0.294 m 앞에 있어 앞을 0.30 m로 추정함(범퍼~차축은 미측정) |
| `half_width` | 0.115 m | 폭 0.23 m 실측 |
| `max_steer` | 0.35 rad (≈20.05°) | 실측 조향 한계 |
| `max_velocity` / `max_reverse_velocity` | 0.3 / 0.2 m/s | 운용 상한 0.3 m/s 이하 |
| `primitive_speed` | 0.25 m/s | 생성기 조건 `primitive_speed ≤ max_velocity`. primitive 길이 0.2 m |
| 윤거 0.174 m, 엔코더 0.164 mm/틱 | — | 펌웨어 설정에는 없음. 게이트웨이 odometry 설정에 들어감 |
| 그 밖의 값 | `lattice-lab-1`과 같음 | 조향률 1.5 rad/s, 가감속, clock·계획 예산 모두 시험 정책값이며 실측 아님 |

최소 회전 반경(뒤 차축)은 0.167 / tan 0.35 ≈ 0.46 m다. 0.3 m/s로 돌 때 횡가속은 약 0.20 m/s²라서
`max_lateral_accel` 0.4보다 작다.

## 재생성과 빌드

```bash
python3 -B platform/apps/drive/tools/generate_primitives.py \
  --profile platform/apps/drive/profiles/xenova-rc-1.json \
  --output platform/apps/drive/profiles/xenova-rc-1

python3 -B platform/scripts/build-xenova-drive-app.py --app drive --profile warm-lattice-lab \
  --vehicle-profile xenova-rc-1 \
  --zephyr-source "$ZEPHYR_SOURCE" --tools "$ZEPHYR_TOOLS" --output "$NEW_ARM_OUTPUT" --execute

python3 -B platform/scripts/package-xenova-drive-template.py \
  --build-root "$NEW_ARM_OUTPUT" --output "$NEW_TEMPLATE_OUTPUT" --version "$NEW_RELEASE_VERSION"
```

- `--vehicle-profile`은 `drive/warm-lattice-lab`에서만 받는다.
- 빌더는 CMake에 `-DXENOVA_DRIVE_VEHICLE_PROFILE=<이름>`을 넘긴다.
- 빌드 뒤 compile DB에 실제로 컴파일된 primitive TU가 `profiles/<이름>/primitives.c` 하나뿐인지 검사한다.
- source digest에는 차량 이름이 더해진다. 차량을 고르지 않은 기존 빌드의 digest는 바뀌지 않는다.
- BUILD_RECEIPT의 `vehicle_profile`과 `drive_contract.vehicle_profile`에 이름, vehicle, calibration을 남긴다.
- template ID는 기존 LAB(`zephyr-drive-lattice-lab-arm64`)를 그대로 쓴다. 그래서 보드에서는 같은 프로필의 firmware 교체로 반영된다.

## 게이트웨이 odometry 설정

초안은 [`platform/services/gateway/examples/odometry-xenova-rc-1.json`](../../../services/gateway/examples/odometry-xenova-rc-1.json)이다.
vehicle·calibration·`metres_per_tick`·`track_width_m`·`wheelbase_m`은 실측값을 넣었다.
나머지 값(clock·나이·주기·오차, `maximum_speed_mps` 0.5)은 개발 motion 모델의 값이다.
보드의 기존 `gateway-odometry.json`과 비교한 뒤 기존 값을 유지할지 정한다.
`maximum_speed_mps`는 엔코더 불연속 판정에도 쓰이므로 운용 상한 0.3보다 여유를 둔다.
`OdometryConfig.load()`는 소유자가 게이트웨이 실행 uid와 같고 group/other 권한이 없는(0600) 일반 파일만 받는다.

## 확인 범위

- 생성기 재실행 결과는 바이트 단위로 같다. 기존 LAB 재생성 결과도 커밋된 `planner/generated`와 같다.
- `platform/tests/test_drive_primitive_selection.py`(6개)에 CMake 선택·거절 경우를 추가했다.
- planner C 시험(`platform/apps/drive/tests/`)은 LAB 기하와 primitive SHA를 고정해 둔 fixture라서 새 기하로는 돌리지 않았다.
- 실보드 부팅, 수동 주행, 자동 주행은 아직 확인하지 않았다.
