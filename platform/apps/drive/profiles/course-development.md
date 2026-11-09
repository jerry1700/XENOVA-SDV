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

## 새 입력 생성

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
