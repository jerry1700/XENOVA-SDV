# Raspberry Pi 4 이미지 빌드 사전 점검 — 2026-11-11

이 기록은 다음 이미지의 준비 결과다. BitBake configure/compile/package, ARM 후보 빌드,
WIC 생성·조립, 보드 접속·배포는 수행하지 않았다. 기존 빌드 캐시와 산출물은 보존했다.
현재 이미지가 아래 변경을 이미 포함한다는 의미가 아니다.

## 1. 소스 기준과 통합할 변경

| 대상 | 확인한 revision·상태 |
|---|---|
| 빌드 작업트리 | `/home/ssafy/xenova-build-20261028/source` |
| 현재 브랜치·HEAD | `feat/platform/image-build-20261105` / `cc3f48d92075379a1f6ace63f2ea80ecbbfe6f76` |
| 최신 platform | `d3fa373d1b4c53edd1d9b1fa07e0eb1e54042840` |
| 최신 develop | `89e7890b3f2c1c8ed3499b0f8eac6b58bc52b8e7` |
| MR !555 소스 | `4131abc25a53fd879012620535bb79e7b4480366`. 위 두 원격 ref에 모두 포함됨 |
| 원격 관계 | platform과 develop은 서로의 ancestor가 아니다. 한쪽만 선택하면 다른 쪽 전체를 포함한다고 보장할 수 없음 |
| 기존 미커밋 변경 | 생성 정책 및 관련 문서·시험 8개를 먼저 보관. 정책·recipe·기존 시험의 바이트 유지 확인 |

현재 브랜치는 최신 platform과 병합하지 않았다. `git merge-tree --write-tree --name-only HEAD origin/platform`
사전 검사의 충돌 파일은 `platform/scripts/package-xenova-xenstore-g1-handoff.py` 1곳이다.
양쪽 runtime 모듈 목록을 합쳐 `xenova_creation_auto`·`xenova_display_setup`·
`xenova_drive_binding`을 모두 유지해야 한다. 이 검사는 작업트리·index·HEAD를 병합하지 않는다.

최신 platform의 생성 실패 자동 수습, CPU 준비 격리, display UUID 선택·factory 자격 공급을
실제 빌드 소스에 통합한 뒤 관련 회귀를 추가 실행해야 한다. 원격 내용을 읽어 확인한 것과
현재 브랜치에 설치한 것을 구별한다.

## 2. 설정과 누락 방지 목록

| 항목 | 확인·보완한 상태 | 다음 빌드/조립에서 확인할 것 |
|---|---|---|
| 대상·배포판 | `MACHINE=raspberrypi4-64`, `DISTRO=xenova`. RPi ARM64이며 Jetson 이미지가 아님 | os-release·배너와 실제 ARM64 패키지 |
| 외부 레이어 | layers.lock의 8개 저장소가 실제 checkout 커밋과 일치하고 tracked tree도 깨끗함 | 버전·lock 유지, 새 레이어 추가 없음 |
| 캐시 | 기존 DL_DIR·SSTATE_DIR·cache 유지 | clean/cleansstate 없이 재사용. 현재 빌드 작업 없음 |
| dom0 | 3072MiB, max vCPU 2, `force_turbo=1` 설정 유지 | Xen/dom0 인자 구별, WIC boot.scr·config.txt 검사 |
| HDMI | `vc4-kms-v3d-pi4,cma-64`와 Pi4 DTBO 배포 설정 유지 | FAT의 실제 overlay·config 일치, 기존 로그인/SSH 보존 |
| ESP32 | usbserial·cp210x 이미지 패키지 포함 | 실제 kernel module·의존성·manifest 검사 |
| 휴대폰 USB | rndis_host·cdc_ether·cdc_ncm, 주소 전용 DHCP 규칙 유지 | 기본 경로·DNS·NTP를 휴대폰 것으로 바꾸지 않음 |
| 생성 정책 | `call_ms=1200000`, `execution_ms=1140000`, `cleanup_ms=30000` 및 root 0600 recipe 유지 | 조립 검사에 실제 설정 파일과 소스의 바이트 일치 추가 |
| Linux Agent | 실제 private 공급 경로가 `XENOVA_AGENT_DEMO_COMMANDS=1` 기록 | 일반 생성·factory 모두 실제 Agent parser로 확인. 중립 template에는 private env 없음 |
| CPU 배치 | preset 공유 {0,3}, A→1·B→2 유지 | factory UUID plan과 결합, 현재 boot의 준비 뒤 start. UUID·기존 vCPU 수 보존 |
| 공통 Zephyr | 현재·최신 platform의 drive/runtime/guest/Gateway/Agent 경로에 diff 없음. Diagnostics·v2 Publisher·netfront·단일 firmware의 두 모드 유지 | 동일 최종 firmware를 A/B에 선택하고 UUID·MAC·자격·CPU 정책은 독립 유지 |
| 통신·종료 | warm lattice 프로필에 `CONFIG_XENOVA_MQTT_TCP_NODELAY=y`, 실제 TCP setsockopt 경로 확인 | F01·계산/통신 분리·협조 종료 회귀 및 최종 ARM receipt 확인 |
| Broker | backend read에 `xenova/v2/vms/+/telemetry/+` 포함 | VM별 auth/ACL·v1/v2 계정 분리, common image에 비밀 없음 |
| API·Worker·로그 | 실제 API/MQTT/Gateway source gate와 runtime-log collector 설치·enable 검사 존재 | collector·manager drop-in·로그 정책·실제 설치 소스 검사. 제품 연동과 패키징을 구별 |
| display/relay | 이미지의 feed·target·guest relay 활성화 설정 존재. 최신 platform은 factory에서 UUID·토큰·relay 입력을 공급 | 최신 `xenova_display_setup` 포함 필수. 실제 guest IP·선택 UUID·서로 다른 토큰 사용, 예제/공통 비밀 복사 금지 |
| 그래픽 화면 | Xorg·Chromium·FFmpeg·한국어 폰트·잠긴 display 계정·setpriv 등 기존 의존성 유지 | 실제 출력/EDID 설정이 있어야 화면 실행. 하드웨어 신원 추측 금지 |
| Gateway 출력 | runtime/pyserial은 포함하되 기본 unit은 비활성인 기존 정책 유지 | 장치·Authority·실행 바인딩·재무장 설정 없이 차량 출력 자동 허용 금지 |
| Tailscale | CLI/daemon·TUN/IPv6 패키지·영구 저장 설정 유지 | /srv 마운트 실패 차단, per-board state. VM 부팅의 필수 조건으로 만들지 않음 |
| 웹 자산 | 현 local.conf는 이전 `xenova-build-20261105/display-web-input`을 가리킴 | 최종 소스 기준 새 자산을 만들고 경로/receipt 갱신. 이전 빌드 자산 재사용으로 최신 UI를 주장하지 않음 |

Agent 설정은 새 Linux 준비 및 UUID 확정 뒤 자격 공급에 적용된다. 이미 완료된 설치의
재부팅만으로 guest env를 다시 쓰거나 자격을 회전하지 않는다. 프로그램 자체 기본값은
꺼짐이며 이번 개발 이미지에서 기존 MQTT-04 stress·agent_exit를 명시적으로 허용한다.

## 3. 템플릿 선택에서 남은 준비

현재 브랜치 lock은 Ubuntu 1.0.5·Debian 1.0.6·lab Zephyr 1.0.8을 선택한다.
확인한 platform lock은 Ubuntu 1.0.4·Debian 1.0.7·lab Zephyr 1.0.8이다.
Ubuntu를 단순히 이전 버전으로 되돌리거나, 같은 Zephyr 1.0.8의 다른 manifest를
덮어쓰면 안 된다.

- Debian은 최신 1.0.7 recipe의 guest display 제공물과 Agent receipt를 포함해 준비한다.
  검사한 이전 release root에는 Debian 1.0.5만 있었고, Downloads에서도 1.0.7 번들을
  찾지 못했다. 이는 모든 보관 위치의 부재를 증명한 것은 아니다.
- lab Zephyr 1.0.8은 현재 lock과 platform lock의 manifest hash가 다르다.
  최종 소스·프로필·BUILD_RECEIPT·파일 hash가 맞는 불변 번들을 선택하고,
  필요한 경우 새 버전으로 게시한다. lock·defaults·조립 입력을 한 번에 맞춘다.
- 별도 HIL development 1.0.2 후보
  `/home/ssafy/xenova-rpi-hil-development-template-20261111-01`가 존재한다.
  firmware.json SHA256은 `2ab0d5cef135f156e380b76bfcc8a71e69ca7492009b59b1aafcd3eb8c0e8dea`이며,
  앱 파일 34개는 현재 `platform/apps/drive/zephyr` 소스와 일치한다.
  전체 공통 소스/receipt·프로필 호환 검사와 기본 template 전환은 아직 수행하지 않았다.
  development와 lab 프로필을 같은 번들처럼 취급하지 않는다.
- 고정 Zephyr/SDK와 HIL 캐시는 보존한다. 다른 source 경로에 결합된 WORKSPACE marker나
  CMake cache를 현재 작업트리로 강제 재지정하지 않는다.

## 4. 최초 설치와 선택적 Telemetry의 순서

현재 HEAD와 최신 platform 두 소스에 이미 `59db419f`의 수정이 있다.

```text
데이터 준비 → /srv/xenova mount → first-boot 등록 성공
                                    ├→ CPU 준비 / VM autostart
                                    ├→ UTC 기준 제공
                                    └→ 콘솔 증거 수집
```

두 수집 서비스의 `Requisite`와 `After=xenova-first-boot.service`를 함께 검사했다.
등록 실패/비활성 상태는 명령 실행을 막고, 진행 중인 등록과는 순서를 맞춘다.
autostart는 두 수집기의 완료를 기다리지 않으며 `storage_check()`도 유지한다.

이번 보완은 service를 다시 설계하지 않았다. 다음 이미지에 과거 파일이 들어가는 것을
막도록 최종 조립의 `platform_root_ok()`가 두 service·두 timer의 source-exact 바이트와
활성화 링크를 검사한다. 누락·Requisite 제거·timer 비활성은 거절하는 회귀를 추가했다.
실제 보드의 설치 파일과 당시 FileNotFoundError 대상 파일은 이번에 조사하지 않았다.

## 5. 로컬 검증과 미검증 경계

| 검증 | 결과 |
|---|---|
| 실제 root-owned 임시 fixture: provisioning·boot boundaries·creation reconcile·broker accounts·registration boot | 107건: 106 통과, 실제 후보 ARM 번들이 필요한 1건 skip, 27.118초 |
| 최종 조립 gate·first install·default deployment·boot boundaries·creation reconcile | 최종 수정 후 138건 전부 통과, 31.400초 |
| ARM systemd 259.5의 실제 unit loader | 정상 그래프 0, 순환 주입 1 및 cycle 검출. 두 Telemetry timer 포함 |
| git diff --check | 통과 |
| 첫 비특권 실행의 display sync | 98건 중 fixture 오류 1·root 필요 17 skip. 삭제한 설정 파일을 helper가 stat하는 오류이며 최신 platform에 이미 수정됨 |
| BitBake/ARM/패키징/WIC/보드 | 이번에는 실행하지 않음. 위 로컬 통과를 실제 이미지 포함·부팅 성공으로 확대하지 않음 |

주요 명령은 저장소의 제품 코드·실제 Agent parser·기존 격리 Mosquitto를 사용한다.
root 시험은 소유 임시 경로 및 격리 broker만 사용하며 시스템 서비스·실제 VM은 제어하지 않았다.

```bash
python -B -m unittest -v test_xenova_provisioning test_xenova_boot_boundaries   test_xenova_creation_reconcile test_xenova_broker_accounts test_xenova_registration_boot
python -B -m unittest -v test_xenova_deployment_payloads test_xenova_first_install   test_xenova_default_deployment test_xenova_boot_boundaries test_xenova_creation_reconcile
```

기존 승인 시험 환경의 TEST_ENV.json과 venv를 재사용했다. 새 의존성은 설치하지 않았다.
첫 실패 로그와 성공 로그를 함께 보존한다.
원자료: `/home/ssafy/xenova-build-preflight-20261111-zgop5zqf/`의
`PRESERVE.json`·`source-before.patch`·`local-regression.log`·
`root-local-regression.json/log`·`assembly-regression-final.json/log`·
`arm-systemd-result.json`·`layers-result.json`.
시험 묶음 사이 중복 사례가 있으므로 건수를 합해 고유 시험 수로 표현하지 않는다.

남은 순서는 소스 통합·충돌 해소 → Linux/Zephyr 입력 고정 → 최신 웹 자산 →
dom0·firmware 빌드 → 실제 설치 모듈·service/enable·자격 중립성 검사 →
최종 설치 이미지 조립·WIC/FAT/root/data 검증이다. 현재 준비 검사가 끝났다는 이유로
이 단계를 건너뛰거나 이전 이미지를 최신으로 전달하지 않는다.
