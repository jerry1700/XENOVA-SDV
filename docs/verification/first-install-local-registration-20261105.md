# 최초 설치의 로컬 등록과 외부 통지 분리 — 2026-11-05

## 범위와 후보

- 작업 브랜치: `feat/platform/image-build-20261105`.
- 기준 HEAD: `5d05101efd39c457cb78a05b473952bc38e7a317`. 이 보고서는 미커밋 diff를 포함한 후보를 다룬다.
- 기존 USB 네트워크·display 의존성·Xenova 배포판 변경은 보존했다. 이번 변경과 구분한 원 diff는 아래 증거 디렉터리의 `before-source.diff`에 있다.
- 현재 계약은 [VM 공급·등록 계약](../contracts/vm-provisioning-v1.md), 사용법은 [등록 조회·수동 통지](../platform/cli-manager-spec.md#cmd-registrations), 부팅 책임은 [최초 등록과 매 부팅 준비](../platform/management-boot-boundaries.md#최초-등록과-매-부팅-준비)를 따른다.

## 변경과 유지 조건

| 대상 | 결과 |
| --- | --- |
| `xenova_first_boot.FirstBoot.register` | 로컬 등록 완료 후 새 `registration_notices` 기회를 만들지 않고 빈 목록을 반환한다. 기존 반환형은 유지한다. |
| `xenova_first_boot.main` | factory 완료 뒤 `automatic(..., wait_ready=True)`를 호출하지 않는다. 통지 job이나 외부 요청을 새로 만들지 않는다. |
| 로컬 등록 | 기존 identity plan·UUID/MAC·자산·실행 의도·완료 상태·autostart 인계를 유지한다. 재호출에서 등록 JSON과 자산 바이트가 유지됨을 확인했다. |
| 기존 기록 | 과거 `registration_notices`와 통지 결과는 삭제·초기화하지 않는다. 완료 first-boot나 API 재시작이 과거 대상을 다시 송신하지 않는다. |
| 일반 VM 생성 | Manager의 기존 create 후 자동 기회 소비·잠금 반환·선택적 통지를 유지한다. 같은 create 재전달·API 재시작에서 기회를 보충하지 않는다. |
| 조회·수동 통지 | 기존 등록 GET/CLI 조회와 인증된 UUID별 notify POST를 유지한다. 설정 부재·실패·미확정·confirmed의 기존 응답 의미도 유지한다. |
| 구형 후속 실행 | 이미 예약된 defer/after-api 진입점은 호환을 위해 남긴다. 기존 설치본의 실행 중인 구형 job 취소·정리는 이번 로컬 변경에 포함하지 않는다. |

기능 변경은 first-boot 모듈에 한정한다. 공유 `xenova_registration_hook.py`는 설명만 고쳤으며 module docstring을 제외한 AST가 HEAD와 같다.
Manager 및 `dom0_manager` 제품 Python 56개 파일은 작업 전 SHA-256과 동일하다.
새 의존성·API·필드·재시도 큐·서비스 의존성을 추가하지 않았다.

## 수정 전후 assertion

- 수정 전 제품 코드에 새 first-boot 진입점 시험을 실행하면 `AssertionError: factory must not notify`로 실패했다. 이는 통지 helper 진입의 재현이며 실제 외부 송신 증거로 확대하지 않는다.
- 수정 후에는 실제 `boot.main`과 기존 관리 잠금으로 로컬 등록을 완료하며 통지 helper 호출은 0회다.
- 완료 후 재호출에서도 UUID 계획·등록 JSON·자산 해시·과거 통지 결과가 유지되고 새 통지 디렉터리는 생기지 않는다.
- 실제 별도 Uvicorn/HTTP 프로세스와 독립 Spring 응답 대역을 사용했다. API 부재에서 factory 등록, 설정 없는 API의 등록 GET, notify `503/not-configured`, 설정 후 명시 notify 1회, 완료 boot/API 재시작 뒤 추가 송신 0회를 확인했다.
- 일반 create의 기존 자동 통지·원 응답 비대기·재전달 중복 방지와 수동 notify의 인증·실패 의미는 같은 HTTP suite에서 유지했다.
- first-boot unit에 API·`network-online.target`·`tailscaled.service`의 ordering/성공/중지 전파 의존성이 추가되지 않았고 기존 autostart → host admission → API 순서를 확인했다.

## 로컬 회귀

공통 명령은 저장소 루트에서 다음과 같다. 기존 격리 시험 도구를 `PYTHONPATH`로 사용했으며 제품 의존성이나 시스템 Python을 바꾸지 않았다.

```sh
PYTHONDONTWRITEBYTECODE=1 XENOVA_TEST_SYSTEMD_BOOT=0   python3 -B -m unittest discover -s platform/tests -p '시험파일.py' -v
```

| 시험 파일 | 통과 | 건너뜀 |
| --- | ---: | ---: |
| `test_xenova_registration_boot.py` | 4 | 0 |
| `test_xenova_registration.py` | 9 | 0 |
| `test_registration_api_process.py` | 10 | 1 |
| `test_xenova_first_install.py` | 51 | 0 |
| `test_xenova_provisioning.py` | 16 | 1 |
| `test_xenova_lifecycle.py` | 53 | 0 |
| `test_xenova_boot_boundaries.py` | 22 | 0 |
| 합계 | 165 | 2 |

- HTTP suite의 건너뜀은 opt-in 구형 transient systemd job 시험이다. 현재 factory는 이 경로를 새로 사용하지 않는다.
- provisioning은 root 소유 임시 fixture에서 다시 실행한 최종 결과다. 실제 ARM 주행 번들을 지정해야 하는 1건만 건너뛰었다.
- root fixture 최초 실행은 호스트 시험 경로의 Paho 부재로 2개 오류가 났다. 기존 빌드 rootfs의 순수 Python Paho를 격리 `PYTHONPATH`에만 연결한 뒤 16개가 통과했다. 초기 실패 로그도 보존했다.
- provisioning의 두 broker는 전용 임시 경로·loopback 포트만 사용했다. 실제 mount·운영 broker·systemd reload·VM 제어 경계는 대역이다.
- 마지막 부팅 의존성 assertion 보강 뒤 4개 boot 시험을 다시 실행해 통과했다. 위 합계에 중복 가산하지 않았다.

## Yocto 설치 단계와 증거

- `bitbake xenova-platform -c install`: 성공. 685개 task 중 677개는 다시 실행할 필요가 없었고, 실제 재실행 task도 모두 성공했다.
- 기존 recipe의 SRC_URI와 install 규칙이 두 변경 모듈을 포함한다. 설치된 first-boot·hook·Manager 3개 모듈의 바이트와 후보 소스가 같다.
- 설치 staging 모듈을 기존 ARM64 Python으로 QEMU에서 import해 성공했다. main·서비스·VM 동작은 실행하지 않았다.
- RPM 포장 dry-run은 앞선 배포판 변경 때문에 Xen tools 재컴파일까지 계획했다. 실제 RPM 포장은 실행하지 않고 플랫폼 설치 단계로 범위를 제한했다.
- 설치 계획 guard가 `xenova-platform`을 `xen` 접두사로 오인한 것은 controller 검사 오류였다. 정확한 recipe basename 비교로 정정한 뒤, 플랫폼만 compile하는 계획을 확인하고 실행했다.
- `git diff --check`와 변경 문서의 로컬 링크·anchor 검사 결과는 최종 증거에 보관한다.

증거 디렉터리: `/home/ssafy/xenova-firstboot-local-20261105-2s7ceqhz`.
시험별 로그·실행 JSON·수정 전 실패·작업 전후 diff·후보 파일 SHA-256·후보 소스 묶음을 보존한다.
`installed-source-match.json`은 설치 바이트 대응, `final-preservation-proof.json`은 보호 파일 대응을 기록한다.

## 남은 수락

실제 Spring/운영 DB 반영, 실제 보드·구형 systemd job, 최종 이미지/WIC/SD의 첫 부팅은 이번 수락에 포함하지 않는다.
전체 이미지 빌드·보드 설치·서비스 변경·commit/push는 실행하지 않았다.
새 이미지 사용자는 기본 네트워크와 필요한 Tailnet 접속·Spring 자격을 준비한 뒤 기존 등록 조회와 수동 notify를 별도로 수행한다.
