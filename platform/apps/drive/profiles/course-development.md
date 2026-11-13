# RPi 개발 차량 설정

`course-development-1/`은 실제 개발용 motion metadata와 초기 GridSpec에서 생성한
RPi 주행 앱 입력이다. `kind=test`이며 실차 측정·장치 승인·주행 권한을 뜻하지 않는다.
기존 `lattice-lab-1.json`과 `planner/generated/`는 별도로 유지한다.

| 항목 | 선택 근거 |
|---|---|
| configuration / vehicle | 원 motion identity |
| calibration | 원 `motion.canonical.json` bytes의 SHA256 |
| 차체·축간거리·조향·가감속 상한 | 원 development 모델, bound calibration의 같은 차체 |
| 격자 | 원 `grid.json`; 초기 32×32, 5cm, origin `[-0.2,-0.8]` |
| 그 밖의 제어·충돌·clock·작업 예산 | `sources/lattice-lab-policy.json`의 임시 정책 |

`source-receipt.json`은 원본별 길이·SHA와 직접 유도한 필드, 유지한 임시 정책을
구분한다. local calibration SHA와 calibration 식별자인 full motion SHA는 다르다.
장치↔Pi clock의 오차를 Pi↔guest clock 오차로 전용하지 않는다.

## 코스와 여유 설정

코스 생성기는 `platform/apps/simulator/tools/jetson-course-geometry.json`에서 한 차로의
폭, 차체 길이·폭, 축간거리, 카메라 높이를 m 단위로 읽는다. 차로 폭을 바꾸면 양쪽 차로를 포함한
도로 폭과 분리선·주차장 연결·콘 좌표를 함께 계산한다. 차체 길이로 뒤 차축 기준
출발점을 다시 계산한다. 축간거리 20cm와 앞뒤 돌출 대칭 조건에서는 길이 38cm가 뒤 차축에서
앞 29cm·뒤 9cm와 일치한다.

차체 폭 21cm·길이 38cm, 카메라 높이 14cm, 좌우 최대 조향각 ±40°, 흰 테이프 폭
12mm는 `jetson-course-measured.json`에 기록했다. 합성 자전거 모델은 그 각도와
축간거리 20cm에서 뒤 차축 최소 회전반경 약 23.8cm를 계산한다. 실차의 좌우
조향 보정에는 각 명령에서 실제 뒤 차축 회전반경을 쓴다. 테이프 폭은 코스의
물리 치수이며, 차체 겹침 허용량은 Pi 차로 검사에서 별도로 다룬다.
기존 코스 파일을 기준으로 만든 Scene은 원 SHA에 결합되므로, 측정값 시험은 별도 출력으로 만든다.

```bash
python3 -B platform/apps/simulator/tools/generate_jetson_tracks.py \
  --geometry platform/apps/simulator/tools/jetson-course-measured.json \
  --output "$NEW_TRACKS_JSON"
```

공유 코스를 갱신할 때는 기본 설정 파일을 수정하고 생성기를 기본 출력으로 실행한 뒤
`platform/apps/simulator/tools/export_tracks.py`를 실행한다. 프런트엔드의 공유 코스와
Python 시뮬레이터의 `tracks.json`에 같은 geometry가 들어간다.

Pi는 World의 차로 경계를 받아 계획과 충돌 검사에 쓴다. Jetson은 카메라 높이를 지면
투영에 쓰고, `lane_width_m ± lane_width_tolerance_m`를 차로 인식의 폭 범위로 쓴다.
양쪽 경계가 관측됐을 때는 관측한 위치가 유지된다. Pi의 `front`·`rear`·`half_width`·
`wheelbase`는 같은 차체에서 생성한 설정을 읽는다.

흰선 폭과 추가 돌출 허용치는 차체 경계 검사에 사용한다. 좁은 차로 추종은 도착 차로의
폭과 차체 여유로 선택한다. `narrow_lane_speed_mps`는 선택적으로 지정하는 추종 속도 상한이다.

`margin`은 선택한 시험 profile에서 고정하는 차체 여유다. `tracking_guard`는 실제 추종 오차에
대해 별도로 조절하는 절대 거리(m)다. 둘 다 `course-clearance-narrow-test.json`에서
정하고 아래 절차로 새 개발 번들을 생성한다. 각 빌드에서는 기본 여유를 고정한다.

작은 `tracking_guard`는 primitive의 충돌 검사 간격도 줄인다. 현재 C의 구간당 64회 상한에서
0.4m/s·0.8초 primitive에 5mm 또는 3mm를 그대로 넣으면 작업 예산을 넘긴다. 두 값을 비교할
때는 같은 0.22초 primitive를 사용했다. ±40° 개발 프로필은 기존 5rad/s 조향률에서
좌우 최대각 사이 전환에 약 0.28초가 필요해 0.30초 primitive를 사용한다.
합성 시험에서는 기본 여유 2mm를 고정했고, 위치·방향
오차와 실제 차체의 swept 검사 여유를 따로 적용했다.

R55의 장애물 없는 좁은 차로 시험값은 추종 여유 2.5mm, primitive 속도 0.25m/s,
같은 차로 추종 상한 0.22m/s다. `tape_width_m`은 테이프 전체 폭이며,
`tape_overhang_m`은 테이프 끝에서 차체가 더 나갈 수 있는 거리다. 좁은 코스 시험
프로필은 흰선 폭 12mm와 추가 허용 6mm를 써서 흰선 중심으로부터 12mm까지 차체를
허용한다. 추가 허용을 생략한 입력은 테이프 끝까지 허용한다. 앞뒤 차축 중심은
명목 차로 안에 둔다. 관측된 경계, 셀 권한과 점유 상태, 차체의 연속 이동 및
정지 가능성을 함께 검사한다. 이 수치는 합성 시험의 잠정값이다.

이 좁은 코스의 콘 회피는 `avoid_policy=2`로 시험한다. 장애물을 통과한 뒤
변경한 차로를 유지한다. 설정을 생략한 기존 개발 입력은 원래 회피 정책을 사용한다.

좁은 코스에서는 바깥쪽과 안쪽으로 차로를 변경할 때 모두 경로의 곡률을 따라 저속으로
추종한다. 주차 진입도 경로의 곡률을 반영하며 속도 명령 상한은 0.16m/s다.
최종 주차 지점까지 남은 거리가 위치 오차보다 크면 최저 제어 속도를 후보로 검사한다.
각 명령은 현재 지도에서 차체의 이동과 정지 가능성을 검사한 뒤 채택한다.

R45 A·B 콘 회피 후 두 번째 바퀴의 P2 전면주차는 호스트 시뮬레이션에서 확인했다.
이 시험은 정답 경계와 미리 생성한 주차 경로를 사용했다. 상세 결과는
`making-reports/r45-lap-and-p2-20261112.md`에 있다. 실시간 지도 기반 주차 경로 생성과
실차 주행은 후속 검증 대상이다.

## 새 입력 생성

실측 치수를 넣은 **합성 개발 시험**은 다음 순서로 준비한다. `BASE_METADATA`는 기존
`course-development-1/sources`, `BASE_MAPPER`는 그 모델과 짝지어진 기존 Mapper 설정이다.
Jetson의 기존 Python·계약 모듈 환경에서 실행한다. `PYTHONPATH`에는
`vehicle/Jetson/src`, `vehicle/Jetson`과 기존 계약 모듈 경로가 필요하다.

```bash
python3 -B platform/apps/simulator/tools/prepare_jetson_course_inputs.py \
  --geometry platform/apps/simulator/tools/jetson-course-measured.json \
  --metadata-root "$BASE_METADATA" --metadata-receipt-sha256 "$BASE_METADATA_RECEIPT_SHA256" \
  --mapper-profile "$BASE_MAPPER" --output "$NEW_COURSE_INPUTS"

python3 -B platform/apps/drive/tools/prepare_course_profile.py \
  --metadata-root "$NEW_COURSE_INPUTS" --metadata-receipt-sha256 "$NEW_METADATA_RECEIPT_SHA256" \
  --grid "$BASE_METADATA/grid.json" \
  --clearance platform/apps/drive/profiles/course-clearance-narrow-test.json \
  --output "$NEW_PROFILE_DIRECTORY"
```

첫 명령의 JSON에 PC 관측 생산자가 읽을 Scene·개발 모델·motion SHA와 초기 자세가 나온다.
Jetson PC HIL resident에서 이 family를 선택할 때는 installation JSON의 각 파일·SHA와
calibration SHA를 바꾸고 `--installation-sha256`에 그 JSON 원문의 SHA를 준다. JIO의
차량 bundle·local calibration·primitive SHA도 같은 산출물로 맞춘다. 실제 OAK 설치는
별도 실측 calibration과 그에 결합된 Mapper 설정을 사용한다.

### 기존 LAB 여유를 쓰는 개발 입력

기존 디렉터리를 덮어쓰지 않는다. 원 metadata receipt의 SHA를 별도로 확인한 뒤
새 출력 경로를 지정한다. 생성기는 제품 모듈이나 시뮬레이터를 실행하지 않는다.

```bash
python3 -B platform/apps/drive/tools/prepare_development_profile.py \
  --metadata-root "$MOTION_METADATA" \
  --metadata-receipt-sha256 "$MOTION_METADATA_RECEIPT_SHA256" \
  --grid "$DEVELOPMENT_GRID" --output "$NEW_PROFILE_DIRECTORY"
```

원 receipt·motion·development·bound calibration·grid를 함께 보존한다. 모델이 바뀌면
새 primitive asset SHA가 필요하며, 이미 받은 World/Vehicle의 식별자나 시각을 바꾸지
않는다. 새 실행의 생산자·Gateway·C 설정을 같은 원 설정과 새 asset에 결합한다.

## ARM 빌드와 포장

단일 통합 소스에서 고정 Zephyr/SDK를 사용한다. 출력은 저장소·SDK·Zephyr 바깥의
존재하지 않는 경로여야 한다. A/B에는 같은 최종 firmware 번들을 사용한다.

```bash
python3 -B platform/scripts/build-xenova-drive-app.py \
  --app drive --profile warm-lattice-development \
  --development-bundle "$NEW_PROFILE_DIRECTORY" \
  --zephyr-source "$ZEPHYR_SOURCE" --tools "$ZEPHYR_TOOLS" \
  --output "$NEW_ARM_OUTPUT" --execute

python3 -B platform/scripts/package-xenova-drive-template.py \
  --build-root "$NEW_ARM_OUTPUT" --output "$NEW_TEMPLATE_OUTPUT" \
  --version "$NEW_DEVELOPMENT_RELEASE_VERSION"
```

빌더는 원 metadata·선택 profile·generator·생성 C 지문, 실제 compile DB의 단 하나의
선택 primitive TU를 검사한다. 포장에는 실제 ARM `BUILD_RECEIPT.json`과 payload가
필요하다. development template은 `xenova-zephyr-drive-development-1.0.0` /
`zephyr-drive-development-arm64`이며 새 release version을 명시해야 한다.
`--development-bundle`을 생략하면 기존 저장소 입력이 선택된다.

이 선택은 기본 LAB release `1.0.8`, factory/seed/VM 설정을 자동 변경하지 않는다.
실제 포장 결과의 manifest/firmware SHA로 A/B asset·seed·release 선택을 갱신하고
기존 first-install 이미지 조립·검증 절차를 따른다. rootfs 재빌드만으로 새 firmware가
선택되지 않는다. route64·Mapper2 설치를 요구하거나 활성화하지 않는다.

## 확인 범위

순수 입력 도구 회귀 명령은 다음과 같다.

```bash
python3 -B -m unittest discover -s platform/tests -p test_xenova_development_profile.py
```

이 명령은 원 입력·재생성·손상 거절·실제 compiler TU 선택 조건을 확인하며
ARM 빌드·RPi 부팅·폐루프 시험을 대신하지 않는다.
