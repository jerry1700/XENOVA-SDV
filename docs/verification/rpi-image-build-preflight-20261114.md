# Raspberry Pi 4 최신 이미지 빌드 체크리스트 · 2026-11-14

Raspberry Pi 4 ARM64 개발용 최초 설치 이미지의 입력 준비부터 압축본 검증까지 확인한다. 실행 명령의 기본 순서는 [빌드 가이드](../platform/build-guide.md)를 따른다. [11월 11일 점검 기록](rpi-image-build-preflight-20261111.md)은 당시 기준을 보존한다.

## 1. 빌드 기준과 보존 대상

| 항목 | 사전 확인 결과 |
| --- | --- |
| 작업트리 | WSL `/home/ssafy/xenova-build-20261028/source`, `feat/platform/image-build-20261114`, 소스 HEAD `4b3cf7aa`. 점검 시작 시 clean |
| 원격 | `platform=588e18ef`, `develop=6904e50b`. 두 ref의 전체 트리가 일치하고 현재 HEAD에는 해당 platform과 MR !555 입력 `4131abc2`가 포함됨 |
| Yocto | `/home/ssafy/yocto-workspace/build-rpi4-develop-20261028`, `MACHINE=raspberrypi4-64`, `DISTRO=xenova`. 8개 레이어와 BitBake checkout이 `layers.lock`의 고정 값과 일치 |
| 재사용 | `/home/ssafy/yocto-workspace/build-rpi4-xen`의 `downloads`, `sstate-cache`, `cache`. Chromium·Node.js 캐시, 레이어·SDK, 게시본, 검증 증거 및 Jetson 작업 경로 보존 |
| 도구 | Linux Node.js `22.23.4`, npm `10.9.9`와 npm 캐시 확인. Zephyr `684c9e8f`(4.4.0), SDK 1.0.0 고정 도구를 사용 |
| 저장공간 | 점검 당시 WSL 내부 518.71 GiB, Windows C: 116.50 GiB 가용. 실제 조립 직전에는 새 plan의 scratch 상한과 호스트 사용량으로 재판정 |

- [ ] 착수 직전에 원격 ref와 작업트리를 다시 확인한다. 소스, 레이어, 실제 BitBake 설정, npm lockfile, 도구와 템플릿 입력을 이번 `RUN`에 기록한다. 진행 중인 다른 빌드와 conf 또는 작업 디렉터리를 공유하지 않는다.
- [ ] 캐시를 연결한 상태로 필요한 task를 실행하고 sstate 적중과 재실행 항목을 로그로 확인한다. Chromium과 Node.js의 완료 여부는 실제 task 결과 및 rootfs 설치 결과로 확인한다. 전체 캐시 또는 기존 게시본을 정리 대상으로 삼지 않는다.
- [ ] 이 작업트리의 Yocto `local.conf`에 남은 이전 `XENOVA_DISPLAY_WEB_ASSETS` 경로를 이번 dist의 상위 디렉터리로 교체한다. 활성 할당값과 실제 레이어 경로는 BitBake의 해석 결과에서 확인한다.
- [ ] 메타데이터 검사 전 `platform/tests/check_xenova_metadata.py`의 서비스 기대값에 `xenova-display-check.service`를 반영한다. 현재 recipe에는 포함되어 있고 테스트 목록에만 빠져 있다. 검사 자체는 유지한다.

## 2. 웹 자산·최신 템플릿과 공장 선택

| 입력 | 사전 확인 결과 | 사용할 입력의 완료 조건 |
| --- | --- | --- |
| vm-screens | 이전 dist 경로가 Yocto conf에 남아 있음 | 현재 frontend와 npm lockfile로 새 dist를 생성한다. dom0와 Debian Interaction에 같은 트리 해시를 사용한다. index, 지연 로드 JS/CSS, 발표용 MP3까지 포함한다. |
| Ubuntu ARM64 | `ubuntu-24.04-20260926` 1.0.5 기존 번들의 manifest는 lock과 일치. 현재 Agent·MQTT 소스 7개가 번들 영수증과 다름 | 검증한 고정 베이스와 최신 Agent로 새 Linux release를 만든다. |
| Debian ARM64 | `debian-13-20261015` 1.0.8의 원래 배포 해시는 발행 기록에 있으며, 이번 WSL/Windows 검색 범위에서 그 압축본을 찾지 못함. 구버전 로컬 번들은 최신 Agent 근거로 사용할 수 없음 | 고정 베이스와 최신 Agent·Interaction relay·이번 vm-screens dist로 새 release를 만든다. 원본이 발견되어도 해시와 현재 소스 및 자산 영수증을 함께 대조한다. |
| Zephyr A/B | `zephyr-drive-lattice-lab-arm64` 1.0.8 로컬 번들의 manifest는 lock과 일치. 이전 ARM 빌드 영수증의 386개 경로 중 현재와 불일치하거나 빠진 경로가 65개 | `drive` / `warm-lattice-lab`을 고정 도구로 빌드·검증한 새 번들 하나를 A/B에 적용한다. 별도 Network Starter와 다른 작업트리의 HIL 펌웨어는 이 선택과 분리한다. |

- [ ] [웹 자산 빌드](../platform/build-guide.md#display-웹-자산과-그래픽-실행-환경)의 `npm ci`와 `npm run build -w @xenova/vm-screens`를 사용한다. 실제 웹 소스 revision·lockfile·dist 파일 해시를 보관한다.
- [ ] `sources.lock.json`에 따른 Ubuntu cloud image와 Debian generic ARM64 QCOW2를 재사용한다. 두 입력은 `/home/ssafy/perseus-guest-templates-20261016-150530/downloads`에서 해시가 일치했다. 두 Linux 번들의 Agent·공용 MQTT 파일, Paho, 부팅 자산, 서비스 enable과 `agent-install.json`을 확인한다.
- [ ] Debian의 `prepare-xenova-guests.py --execute template`에는 아래 두 인자를 함께 전달한다. [Debian Interaction 빌드 기록](../../platform/services/display/router/deploy/guest-display/BUILD-CHECKLIST.md)에서 기존 입력의 출처를 확인할 수 있다.

  ```bash
  --display-web-assets "$RUN/display-web-input/display-web" \
  --display-web-revision "$DISPLAY_WEB_REVISION"
  ```

- [ ] 실제 설치된 양쪽 Linux Agent unit에서 `XENOVA_AGENT_DEMO_COMMANDS=1`을 확인한다. 필요한 경우 VM별 `agent.env`에서 `0`으로 재정의할 수 있어야 한다. 최초 설치 후 VM별 자격을 발급하며, 공통 템플릿의 자산과 로그에 자격을 넣지 않는다.
- [ ] ARM 후보의 AArch64 ELF와 Xen 게스트 DTS·GICv2, 1 vCPU/16 MiB 프로필, 메모리와 스택을 확인한다. AUTO/MANUAL 공통 앱, v1 MQTT·TCP_NODELAY, 별도 v2 Publisher, Diagnostics, 최신 netfront·UTC 공급 수신·협조 종료의 실제 빌드 설정과 소스 영수증을 확인한다. fault/delay injection이 포함된 lab 프로필임을 배포 기록에 명시한다.
- [ ] 새 번들의 버전과 manifest를 고정하고 기존 게시 디렉터리는 보존한다. Linux `recipe.json`의 버전, [기본 템플릿 lock](../../platform/guests/default-templates.lock.json), factory `defaults.json`, [최초 설치 설정 예제](../../platform/first-install/example.config.json)와 이번 실제 `deployment.config.json`에서 버전·ID·경로가 일치해야 한다. 빌드 가이드의 Zephyr 예시 버전을 그대로 복사하지 않는다.
- [ ] 최종 seed에서 Zephyr A/B `zephyr.bin` SHA-256이 검증된 후보와 모두 일치하는지 확인한다. 각 VM의 UUID·MAC·MQTT 자격과 CPU 예약은 독립적으로 관리한다. Debian은 기본 seed와 원본 템플릿을, Ubuntu는 설치 가능한 원본 템플릿을 포함한다.

## 3. 실제 WIC와 최종 이미지에서 확인할 항목

아래 항목은 소스의 선언과 실제 패키지·이미지 파일을 함께 확인한다. 설치 여부, systemd enable 여부, 정상 작동 여부를 별도로 기록한다.

| 확인 | 영역 | 이미지에서 대조할 값과 파일 |
| --- | --- | --- |
| [ ] | Xen·dom0·부트 | 실제 FAT의 `config.txt`, `boot.scr`, DTB/DTBO, 커널, Xen. dom0 메모리 `3072M`, `dom0_max_vcpus=2`, `force_turbo=1`. Xen의 UART와 Linux `console=hvc0 loglevel=4`, hvc0 로그인 및 SSH 준비 경로. 기존 부팅 진행 표시 및 복구 진입점 |
| [ ] | HDMI·그래픽 환경 | `dtoverlay=vc4-kms-v3d-pi4,cma-64`와 실제 Pi4 DTBO가 함께 들어 있는지 확인. Xorg·Chromium·FFmpeg·Playwright·폰트·xset·libturbojpeg, 전용 display 계정·setpriv, Chromium 실행 파일과 공유 라이브러리 확인. 기존 getty와 화면 자원 점유 정책 대조 |
| [ ] | ESP32·휴대폰 | `kernel-module-usbserial`, `kernel-module-cp210x`와 실행 커널의 모듈 버전 일치. RNDIS/CDC ECM/NCM의 내장 또는 설치 모듈 확인. `20-xenova-usb-phone.network`에서 IPv4 DHCP 적용, 기본 경로·DNS·NTP·RA 차단 및 `RequiredForOnline=no`. ESP32 장치 코드 배포와 dom0 USB 드라이버는 별개 |
| [ ] | 로컬 네트워크·Tailscale | 내부 Xen 브리지·DHCP, Wi-Fi/Ethernet·SSH와 TUN, tailscale/tailscaled 및 `/srv/xenova/tailscale` 마운트 보호. 공통 이미지에는 인증 상태와 auth key가 없어야 한다. Tailscale·휴대폰 부재나 외부 인터넷 지연이 로컬 VM 준비의 선행조건으로 연결되지 않아야 한다. |
| [ ] | 최초 설치·시간·CPU | `/srv/xenova` → 로컬 등록·identity plan, Xen toolstack·CPU 준비 → `xenova-vms`. 최초 설치 자동 외부 통지는 제거된 상태이며 등록 조회·수동 notify·일반 생성 통지는 유지. CPU 프리셋은 dom0·Debian·일반 VM {0,3}과 Zephyr A1/B2로, 이미 설치된 명시적 정책의 갱신은 별도. `/etc/xenova/creation-policy.json`은 root:root 0600이며 호출 1,200,000 ms·실행 1,140,000 ms·정리 30,000 ms |
| [ ] | 종료·복구 | host-admission, guest shutdown, autostart 실행 scope의 실제 unit/drop-in과 역순 종료, `xendomains.service` 중복 기동 방지. Manager API·Worker·복구 설정 서비스와 기존 수락/실행/journal 결과, 로그 조회 경로의 설치 여부. factory 설정, 등록 정보, 보드별 비밀은 첫 실행에 생성되는 범위 확인 |
| [ ] | MQTT·Telemetry | 실제 broker 설정 생성·시작 주체와 listen 범위를 확인한다. Mosquitto 기본 배포 preset은 disable이며, 실제 연결은 Xenova provisioning 결과로 검증한다. backend 계정은 `xenova/v2/vms/+/telemetry/+` 구독 가능, v2 write 불가. 게스트의 교차 UUID 발행과 관리 토픽 우회 발행 거절. v1과 v2를 각각 확인하고, 서비스 active·ACL 근거·실제 구독 수신을 분리한다. |
| [ ] | Telemetry 타이머 | UTC 공급과 콘솔 증거 서비스 각각에서 `Requisite=` 및 `After=xenova-first-boot.service`, 저장소 및 해당 Xen daemon 의존성을 확인한다. 타이머의 첫 예약은 여전히 부팅 10초 후다. 기준은 타이머가 active가 된 시점이 아니라 각 서비스의 실제 수집 시작 시점이다. 등록 완료 전, 실패 또는 미완료 상태에서는 수집을 시작하지 않아야 한다. `storage_check()`를 유지하고, 수집 서비스의 실패가 autostart에 전파되지 않는지 확인한다. |
| [ ] | Gateway | 현재 Gateway와 공용 MQTT 코드, Paho·protobuf·pyserial 설치. 적합한 factory A/B는 복구 설정 서비스가 warm profile/mock sink로 시작한다. 실제 시리얼 출력은 운영자가 장치와 실행 신원을 확인하고 설정하는 별도 경로다. 최신 보호·Feedback3·정지/승계 동작을 시험하고, mock 제출과 실제 ESP32 적용을 구분한다. |
| [ ] | Display·relay | feed, display target, guest relay, `xenova-display-check.service`, Debian Interaction 서비스·선택 절차가 설치본에 있는지 확인한다. dom0·Debian의 웹 트리 해시를 대조하고, 선택된 guest 주소와 VM별 토큰으로 relay를 구성한다. 설정 부재 시 중립 화면·안전한 대기를 확인한다. HDMI 키오스크와 선택형 모바일 화면·캡처의 실행 조건은 각각 따로 검사한다. |
| [ ] | 로그·배포판 | Xenova `os-release` 및 로그인 배너, 실행 로그 collector·SQLite·브라우저 조회 경로, journald 영구 저장 bind와 flush drop-in 확인. 재부팅 뒤 `journalctl -b -1` 및 실행 로그 보존은 실제 부팅 시험에서 판정한다. |

정식 소스 확인 위치는 [MQTT 정책](../../platform/deploy/mosquitto/etc/policy.json), [생성 정책](../../platform/yocto/meta-xenova/recipes-core/xenova-platform/files/creation-policy.json), [factory 복구·Gateway 설정](../../platform/services/dom0-manager/dom0_manager/recovery_host_config.py), [display 패키지 검사](../../platform/scripts/check-mobile-display-rootfs.py)다.

## 4. 진행 순서와 완료 증거

| 확인 | 단계 | 다음 단계로 넘길 증거 |
| --- | --- | --- |
| [ ] | 소스·패키징 검사 | `platform/scripts/check-xenova-platform.sh --metadata`의 소스 회귀와 실제 파싱 결과. Management/Recovery, MQTT ACL·계약, Gateway, Display, Agent, ARM/drive의 관련 suite를 구분해 명령·결과·skip 사유를 기록한다. 병합 당시 관련 회귀 258 통과·1 ARM 입력 조건 skip과 별도 Display 16 통과는 참고용이며 새 후보 결과는 다시 남긴다. |
| [ ] | 입력 봉인 | `BUILD-INPUT.json`, source commit·diff 및 파일 해시, 실제 BitBake 설정·레이어·SDK, 웹 자산·템플릿 목록과 영수증. 작업 중에는 가이드의 입력 검사로 변경 여부를 재확인한다. |
| [ ] | dom0 빌드 | 커널 구성과 prereq를 검사하고 sstate를 연결해 `bitbake xenova-xen-bringup-image`. deploy manifest와 pkgdata로 이번 WIC에 설치된 패키지와 소유 파일을 대조한다. stale WIC를 최신 결과로 인계하지 않는다. |
| [ ] | WIC 검증·dom0 게시 | [WIC 검사](../../platform/scripts/check-xenova-wic.py)로 FAT 부트 자산을 확인한다. rootfs를 오프라인으로 열어 unit 및 preset/drop-in, 실행 파일과 패키지·설치 자산을 대조한다. 저장한 `BUILD-RESULT.json`과 실제 WIC 해시로 `publish-xenova-platform.py`를 실행한다. |
| [ ] | 조립 계획 | 새 템플릿과 게시 dom0로 실제 `CARD_BYTES`를 넣어 `assemble-xenova-first-install.py plan`을 실행한다. 최소 카드 용량, VM 증가분과 운영 여유, WSL scratch 상한, C:의 VHDX 증가 및 압축 공간을 함께 확인한다. 이전 빌드의 계획 수치나 카드 표기 용량을 새 입력의 용량 근거로 쓰지 않는다. |
| [ ] | 조립·최종 검증 | 같은 입력으로 `assemble … --execute`, 이어서 `verify --manifest …`. 실제 최종 이미지의 분할 영역과 boot/root/data 해시, 카탈로그 전체와 Debian seed, A/B 자산을 읽기 전용으로 다시 확인한다. 식별자·인증정보·host 상태가 공통 이미지에 미리 고정되지 않았는지 검사한다. |
| [ ] | 전달 | `xenova-first-install.img.zst`, 압축·비압축 SHA-256, 최종 manifest, 설치 패키지·템플릿·입력 기록, 검사 결과를 전달한다. `zstd -t`와 스트리밍 복원으로 원본 크기·SHA-256을 대조하고 PC 전달 사본도 확인한다. 새 실행의 중간 작업 파일은 증거와 사본 확인 후 별도로 정리할 수 있다. |

## 5. 이후 실보드에서 확인할 항목

- [ ] 새 카드 첫 부팅과 재부팅에서 boot/UART·SSH, 3개 기본 VM, CPU 실제 배치, 최초 등록 재실행의 신원 보존, autostart와 Telemetry 타이머의 순서를 boot ID 및 journal monotonic 기록으로 확인한다. 정상 종료에서 협조 stop과 로그 보존을 확인한다.
- [ ] Linux Agent demo 명령의 허용·결과, Zephyr A/B의 동일 firmware·실행별 v1/v2 주제, backend의 인증된 v2 수신을 확인한다. Gateway 제어와 ESP32 장치 적용, 실제 시리얼 피드백은 별도 판정한다.
- [ ] 물리 HDMI의 키오스크, 실제 선택 VM의 relay와 정적 파일 응답, 휴대폰 USB 연결 시 IPv4 할당과 기본 경로 보존을 확인한다. Tailscale 최초 인증·재접속·상태 보존도 별도로 확인한다.

이번 문서의 사전 확인은 소스·캐시·기존 영수증 범위다. 신규 빌드, WIC·최종 이미지 검증과 실제 부팅 수락은 위 체크 항목에서 각각 판정한다.
