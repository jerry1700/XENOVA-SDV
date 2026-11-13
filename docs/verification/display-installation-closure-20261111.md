# 표시 VM 선택·첫 설치 공급 검증 — 2026-11-11

## 후보와 변경

기준은 `origin/platform`의 `db5a0e04`다. 작업 브랜치는
`feat/platform/interaction-display-bootstrap`이며 아래 소스와 시험을 함께 커밋한다.
이 문서는 실행 결과이며 작업 지시문을 완료 증거로 사용하지 않는다.

| 문제 | 반영한 제품 경로 | 확인한 결과 |
| --- | --- | --- |
| 같은 Debian 템플릿을 추가하면 화면 역할/주소가 바뀜 | `xenova_display_sync.sync_after_registration`, `select_interaction` | 기존 binding UUID 유지. 명시 교체만 전체 입력 검증 후 게시 |
| 새 보드의 표시 토큰·relay 설정 공급이 수동 | `FirstBoot.register` → `prepare_token` → 기존 `apply_stopped_linux`/`apply_guest_display` → `configure_factory` | 실제 표시 제공물인 factory만 초기 공급. 중단 뒤 원 토큰 재사용. 완료된 설치는 초기화하지 않음 |
| 이미지 포함과 설치 후 연결의 증거가 섞임 | `deployment_payloads.platform_root_ok`와 `xenova-display-check.service` | 모듈/unit/enable·비밀 부재 검사와 실제 HTTP 관측을 분리 |

제품 소스는 `platform/yocto/meta-xenova/recipes-core/xenova-platform/files/` 아래다.
relay 제품은 `platform/services/display/router/xenova_display/`를 그대로 사용한다.
새 VM 생성/시작·주행 Authority·계정 암호 회전·EC2 DB 변경은 추가하지 않았다.

## 실제 실행과 결과

WSL Ubuntu-24.04의 root 전용 임시 fixture에서 Python 표준 `unittest`를 실행했다.
기존 고정 환경의 Python 의존성과 ARM systemd 259.5 verifier를 재사용했고 새 의존성을 설치하지 않았다.

| 실행 | 결과 | 범위/대체 경계 |
| --- | --- | --- |
| `frozen-r2`: 아래 22개 모듈 | **491/491 통과**, 오류·skip 0, 190.169초 | 등록·생성·CPU·복구·동시 조회·기존 표시·첫 설치 회귀. Xen/블록 장치 등은 기존 fixture 경계 |
| `final-focused`: display_sync/setup/guest_template | **36/36 통과**, 오류·skip 0, 4.424초 | 최종 시험의 모듈 격리 보완 후 재실행. 제품 소스는 위 전체 시험과 동일 |
| 실제 relay + guest HTTP 서버 | 인증 200·무인증/잘못된 토큰 401·상위 503·정상 회복 통과 | 제품 FastAPI relay·LocalFeed·guest_app·DataSource와 실제 로컬 socket 사용. 상위 presentation과 웹 빌드만 작은 시험 제공물 |
| 실제 first-boot 재진입 | 공급 전 EIO → 원 토큰 유지 → 정상 등록/guest 공급 → 완료 후 재등록 통과 | mount/디스크와 broker 설치 환경은 fixture. 토큰/IP 공급·observer export/ACL 검사는 제품 함수 |
| 설정 실패/경쟁 | 오래된 UUID·중복 대상·잘못된 후속 설정·설정 잠금 busy·게시 EIO·재시작 실패와 재실행 통과 | 설정 전용 nonblocking 잠금. VM admission/global 잠금 추가 없음 |
| 조립 누락 검출 | enable 링크·relay 모듈 누락, 공용 이미지의 활성 토큰 거절 | 작고 격리된 시험 rootfs이며 실제 전체 SD 아님 |
| ARM/QEMU `systemd-analyze verify` | 정상 그래프 종료 0, 의도한 순환 그래프 종료 1·cycle 검출 | ARM verifier 실행이며 Xen/guest 부팅 시험 아님 |
| recipe의 신규 install 명령 3개 | module/unit 0644·wrapper 0755, 원 소스와 바이트 일치 | 임시 설치 prefix. Python `-I -B`로 설치 모듈 import/help 성공, thread 수 1. 정규 RPM QA/target Python 아님 |

최종 19개 변경 소스/시험/recipe 파일의 SHA-256을 실행 후보와 다시 대조했다.
전체 시험 뒤 변경은 시험 모듈 cache 격리뿐이며 최종 36개 시험에 포함했다.
현재 `xenova_display_setup.py`의 설치 검증 hash는
`a39215063e9abd0bae083a6af62679de03e32ae0ecd888af1337af9c304d1f1d`다.
토큰/계정 비밀은 보고서·로그·Git에 넣지 않는다.

### 재실행

Linux에서 repo root를 현재 디렉터리로 두고 기존 FastAPI/Uvicorn 시험 환경을 사용한다.
root 소유·모드 시험이 있으므로 root로 실행한다. 환경 의존성을 감추는 성공 fixture를 추가하지 않는다.

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/platform/tests:$PWD/platform/yocto/meta-xenova/recipes-core/xenova-platform/files:$PWD/platform/first-install:$PWD/platform/guests/common:$PWD/platform/deploy/mosquitto/scripts:$PWD/platform/services/display/router:$PWD/platform/services/dom0-manager:$PWD/platform/services/vm-agent/mqtt-client${PYTHONPATH:+:$PYTHONPATH}"
python3 -B -m unittest -v test_xenova_display_sync test_xenova_display_setup test_xenova_display_guest_template
```

전체 회귀의 나머지 모듈은 `test_xenova_creation_auto`, `test_xenova_creation_reconcile`,
`test_xenova_preparation_concurrency`, `test_xenova_lifecycle_ownership`, `test_xenova_read_progress`,
`test_xenova_cpu_asset_isolation`, `test_xenova_cpu`, `test_xenova_cpu_cross`, `test_xenova_cpu_pinning`,
`test_xenova_recovery_foundation`, `test_xenova_g1_handoff`, `test_xenova_default_deployment`,
`test_xenova_management`, `test_xenova_spring_followup`, `test_xenova_network_zephyr`,
`test_xenova_creation_failure`, `test_xenova_first_install`,
`test_xenova_provisioning.DisplayProvisioningTest`, `test_xenova_registration_boot`다.
unit 검사는 기존 고정 `XENOVA_SYSTEMD_ANALYZE` 환경으로
`python3 platform/tests/check_xenova_first_install_units.py`를 실행한다.

원 명령·case 목록·전체 로그·소스 hash·설치 대조·정상/실패 unit 출력은 로컬
`C:/Users/SSAFY/Documents/Codex/2026-11-11/interaction-display-bootstrap/`의
`*.command.json`, `*.result.json`, `*.source.json`, `*.log`, `installed-payload.json`, `units.json`에 보존했다.

## 실패 이력과 해석

Windows mount 경로에서 처음 실행한 `regression-r1`은 491개 중 490개 통과했다.
기존 `test_S4_T08_owned_helper_timeout_after_pipe_eof_reaps_child`에서 100ms 안에 자식이
PID 파일을 남겼다는 assertion만 실패했고, 예상한 `DEADLINE_EXCEEDED`는 반환됐다.
같은 제품 소스를 WSL 내부 디스크에 고정한 대조에서는 해당 시험을 포함해 전부 통과했다.
경로/파일 접근 비용의 영향으로 추정하며 정확한 시간 분해를 확인한 것은 아니다.
제품 기한이나 시험 예산을 늘리지 않았고 최초 실패 로그를 남겼다.

작업 중 기존 오류 문구 assertion, CLI 시험의 subprocess 주입, HTTP 시험의 모듈 cache 격리도
보완했다. 최종 후보에서는 위 정상/실패/회복 시험이 통과했다.

## 배포·수락 경계

`xenova-platform`의 setup/sync/first_boot/wrapper/check unit과 `xenova-display`의 기존 relay/feed
모듈·unit을 함께 설치해야 한다. 이미지에는 활성 토큰/env/binding을 넣지 않는다.
새 factory가 첫 부팅에 보드별 자격을 공급한다. 기존 설치 갱신은 현재 자격·운영자 선택을 보존한다.

`configured`, 이번 `connected`, 앱 READY·현재 MQTT 신호, 실제 HDMI 렌더링은 서로 다르다.
자동 check는 autostart 이후 한 번 실행하므로 guest 부팅 중이면 unconfirmed일 수 있다.
이후 `xenova-display-setup check --live --record`로 명시 재확인한다. 무한 재시도나 다른 VM 기동 차단은 없다.
일반 쓰기 오류는 자기 변경을 rollback하지만 전원 단절의 다중 파일 원자성은 보장하지 않는다.
중단 후 origin/binding 불일치는 check에서 거절하고 명시 선택 재실행으로 정합화한다.

이번에는 정규 Yocto 부분 빌드/RPM QA·전체 이미지·SD·실보드 갱신/재부팅·방화벽·HDMI 물리 화면을
시험하지 않았다. EDID/장치와 운영 방화벽은 실제 공급 후 별도 수락한다.
실제 사용·교체 명령은 [제공물 README](../../platform/services/display/router/deploy/guest-display/README.md#표시-담당-선택과-설치-확인),
배포 단위는 [배포 준비 원본](../platform/deployment-readiness.md#display-first-install)에 둔다.
