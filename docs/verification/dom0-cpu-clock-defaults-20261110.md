# Pi 4 dom0 2 vCPU·정규 클럭 기본값 — 2026-11-10

최신 확인 platform `b0bf33b857d6a7d184404f034cbb5c60d15c5aac`에서 분리한
`feat/platform/dom0-cpu-clock-defaults`의 미커밋 후보와 실제 보드를 검증했다.
기존 작업 브랜치·작업트리는 보존했다. 아래 실행 검증 시점에는 commit/push를 하지 않았으며,
이후 제출 상태는 이 브랜치의 커밋과 MR을 따른다.

## 현재 적용과 다음 이미지 원천

| 항목 | 다음 이미지 공급·검사 | 실보드 결과 |
|---|---|---|
| dom0 수량 | `boot.cmd.xen.in`의 Xen 인자 `dom0_max_vcpus=2` | 정상 재부팅 후 Xen 두 vCPU, Linux online/possible/present 모두 `0-1` |
| 정규 클럭 | `recipes-bsp/bootfiles/rpi-config_%.bbappend`의 Pi 4 `RPI_EXTRA_CONFIG`에 `[all]`·`force_turbo=1` | 실제 mailbox 측정 약 1.5GHz, 설정 유지 |
| CPU 배치 | 기존 `/srv/xenova/cpu-policy.json` | dom0·Debian→{0,3}, Zephyr A→1/B→2 유지. `dom0_vcpus_pin` 추가 없음 |
| 최적화 포함 | 기존 Manager/Gateway recipe의 전체 Python 설치 | 앞서 적용한 세 패치의 설치 bytes 유지·재부팅 후 서비스 기동 |
| 재누락 검출 | `check-xenova-wic.py`의 실제 FAT 프로필 검사, `deployment_payloads.py`의 Gateway/world 소스 대조 | 누락·충돌·오래된 payload 거절을 격리 fixture에서 검증 |

최대 주파수·전압·온도/저전압 보호와 guest 메모리/vCPU·펌웨어·자격을 변경하지 않았다.
lifecycle의 100ms 발행 주기와 300ms 보호 기한, E4 정책값, 원 journal/Authority를 변경·삭제하지 않았다.

기존 보드의 세 패치는 ec857ceb(동일 재합류 행 저장 생략), c68b7e7c(정확한 numeric subtree의
중복 의미 순회 생략), 60acb995(검증한 state subtree의 재검산 없는 직렬화)다.
다음 이미지 후보에는 이 변경과 이후 platform의 호환 후속이 이미 포함돼 있어 제품 코드를
중복 이식하지 않았다. 보드의 최소 패치 파일과 다음 이미지의 최신 모듈은 서로 다른 설치 후보다.
이번에 최신 RPM 전체를 보드에 덮어쓰지는 않았다.

## 정규 부분 포장

고정 BSP·기존 캐시와 전용 Linux 빌드를 사용했다. 공유 빌드 설정·외부 layer revision은 바꾸지 않았다.

- 빌드: `/home/ssafy/xenova-dom0-cpu-clock-20261110-6a52/build`
- 소스: 같은 작업 폴더의 `source`; 입력 base+diff/새 파일은 `source-input.json`으로 식별한다.
- `bitbake -c deploy rpi-config xen-rpi-u-boot-scr`: 성공.
- `bitbake -c package_qa xenova-dom0-manager xenova-gateway`: 성공.
- Manager/Gateway/Gateway-client/MQTT-client 정규 RPM 생성과 실제 추출·147개 설치 파일의 소스 일치 확인.
- rejoin/drive_link/codec가 RPM에 포함되고 모드가 0644임을 확인했다. 두 부팅 기본값은 실제 deploy 파일로 검사했다.
- 정규 recipe의 boot.scr 본문이 보드 후보와 동일하다. header 시각은 SOURCE_DATE_EPOCH 차이가 있으며 각 CRC가 유효하다.

첫 일반 `bitbake rpi-config xen-rpi-u-boot-scr xenova-dom0-manager xenova-gateway`는 요청 대상의
RPM/QA를 완료한 뒤 부수 runtime 의존성인 QEMU 전체 컴파일까지 이어졌다. 이 전용 빌드의
client만 명시적으로 중단하고 위 deploy/QA task로 마감했다. 최초 중단은 패키지 결함으로
집계하지 않으며 로그를 보존했다. 호스트 `CONFIG_TASK_IO_ACCOUNTING` 부재 경고도 보존한다.

native RPM 추출 도구는 고정 recipe sysroot의 라이브러리가 필요했고 실제 출력은 gzip tar였다.
이를 확인한 뒤 해당 형식으로 경로·크기를 제한해 추출했다. 도구 의존성·archive 형식을
잘못 가정한 최초 검사 실패는 제품 RPM/보드 결함으로 집계하지 않는다.

## 로컬 검증

| 명령·범위 | 결과 |
|---|---|
| `python3 platform/tests/check_xenova_bringup_regression.py` | boot/init/network 20개 시나리오 통과 |
| `python3 -m unittest discover -s platform/tests -p test_xenova_dom0_boot_profile.py -v` | 3개 통과. turbo 누락/비활성/조건부·중복과 vCPU 누락/충돌·직접 pin 거절 |
| 같은 discover의 `test_xenova_deployment_payloads.py` | 14개 통과. 실제 recipe install·Gateway/world stale payload·비밀/enable/원 디스크 보존 포함 |
| 세 최적화 회귀 파일의 최신 소스 pytest | 46개 통과. rejoin 기록, numeric schema fallback, private decoded state와 mutable 공개 호출 보호 |

배포 fixture의 중복 임시 경로를 분리했고 기존 ARM systemd 259 검사기를
`XENOVA_DOM0_SYSTEMD_ANALYZE`로 공급한 뒤 재검증했다. 이는 QEMU userspace unit loader 검증이며
QEMU로 새 dom0 이미지를 부팅한 결과가 아니다. protobuf의 기존 label deprecation 경고를 보존했다.

## 실보드 재부팅 수락

기존 boot.scr/config를 root 전용 폴더에 백업하고 native mkimage가 만든 후보의 SHA256·header/data CRC를
검사했다. 실행문은 dom0_max_vcpus 추가 외 기존과 같음을 확인한 뒤 boot.scr만 원자 교체했다.
원 config의 force_turbo=1은 그대로 유지했다. 이후 정상 host reboot 1회를 수행했다.

새 host boot ID는 `83d4201f-d455-4ed4-82fe-2c7f53f865e0`다. Xen command line에
`dom0_max_vcpus=2`가 있으며 vCPU 목록에는 dom0의 0·1만 존재한다. 지난 offline 비교와 달리
이번에는 possible/present도 두 개다. dom0 메모리는 3072MiB를 유지했다.

| VM | UUID | 재부팅 후 API/실제 Xen |
|---|---|---|
| Debian Interaction | fcebe11e-abf6-4c85-a61f-19464f3647c7 | running, domid 1, 512MiB/1vCPU |
| Zephyr A | de113a23-d80e-4333-94b8-788babe0e41e | running, domid 2, 16MiB/1vCPU |
| Zephyr B | 5002a22d-63d6-4f7a-9106-193b14499898 | running, domid 3, 16MiB/1vCPU |

CPU prepare·VM autostart가 성공했고 Manager·Worker·Gateway·display-feed가 active다.
CPU/creation 정책 SHA256과 기존 세 최적화 파일 SHA256도 유지됐다.

API는 기존 Bearer 자격을 메모리에서만 사용했다. `GET /api/v1/vms` 20회는 전부 200이며
중앙값 15.0ms, P90 30.2ms, 최대 56.5ms였다. 이어 목록 한 번과 VM별 GET 세 번도 200이며
세 VM 모두 running을 반환했다. 단건 GET은 약 259–587ms였으므로 목록의 15ms를 모든 route의
지연이라고 읽지 않는다. 이전 4-vCPU 표본과 다른 boot의 짧은 서비스 부하 결과다.

표본은 boot monotonic 약 123초 이후다. 실제 ARM은 약 1500.35–1500.40MHz,
온도는 35.1–37.5°C, get_throttled는 모든 표본에서 0x0이었다. 전력(W)·어댑터 전압/전류와
장시간 최고 부하의 열 여유를 측정한 것은 아니다.

Gateway는 mock, SAFE_STOP/FAILED, output_permitted=false이며 실제 tty/serial/PTY FD가 없다.
기존 실패 이력을 지우거나 재무장하지 않았다. 실제 차량 출력·실차 운행은 하지 않았다.

## 기록과 미수행

보드 원본 백업·결과: `/root/xenova-updates/dom0-defaults-20261110-8d1c`.
보드 증거 archive SHA256: `36d256f3db6f6d852623eb1389cb22d22cb78c0793906c5841de6eb35424966e`.
Windows 원자료와 결과는 `C:/Users/SSAFY/Documents/xenova-test-docs/2026-11-10-dom0-cpu-clock-defaults`에 보존한다.
RPM별 SHA와 147개 source 대조 결과는 `build-evidence/packages.json`이다.

전체 rootfs/WIC·최종 SD 이미지 재조립/기록, 새 RPM 전체의 ARM import·보드 배포, 장기 부하·물리 출력은
미수행이다. 현재 보드의 기존 제품과 실제 고정 2-vCPU 재부팅 수락, 다음 이미지용 RPM/deploy 검증을 구분한다.
다음 이미지 빌드는 이 worktree의 변경까지 포함한 소스를 사용하고, 기존 전체 이미지 산출물을
재사용할 경우 새 FAT 프로필 검사와 전체 Python source gate를 다시 통과해야 한다.
