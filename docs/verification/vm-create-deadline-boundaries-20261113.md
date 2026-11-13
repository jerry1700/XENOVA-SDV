# VM 생성 기한 경계 로컬 검증

기준 후보는 `eada062e`다. 신규 생성은 자산 준비·게시, 등록 마감, 별도 Xen 시작 순서로 진행된다. 아래 시험은 각 지점의 기한을 독립적으로 넘기고, 같은 UUID·명세의 재전달을 확인했다.

| 시간 초과를 넣은 지점 | 자산·자격·예약 | 같은 요청을 다시 보내면 |
|---|---|---|
| Debian·Ubuntu, 자격 공급 직후 게시 전 | loop·mount 정리 receipt 확인. 실패 응답 직후 private workspace와 예약이 남고, 기존 수습으로 workspace를 폐기하면 예약을 반환한다. 작업 디렉터리 밖의 UUID별 `provisioning` 사본과 브로커 계정은 그대로 남는다. | 수습 뒤 원 ID와 기한으로 `DEADLINE_EXCEEDED`를 회수한다. 디스크 복사나 비밀번호 발급을 반복하지 않는다. |
| Debian·Ubuntu, 게시 후 DHCP 적용 전 | 게시 디스크와 자격, 원 예약이 남는다. 게시된 자산은 private 폐기 대상에서 제외된다. | 하위 Manager의 같은 요청은 원 기한 만료를 반환한다. DHCP 적용이나 복사를 반복하지 않는다. |
| Debian·Ubuntu, DHCP 적용 기록 후 등록 마감 전 | 게시 디스크와 자격이 남는다. 등록 의도가 마감될 때까지 원 작업은 미확정이다. | 하위 Manager에 같은 생성 명세를 보내면 복사·자격 발급·DHCP 없이 등록만 마감한다. HTTP는 선행 `get`이 `409 TARGET_CHANGED`를 반환해 이 재개에 닿지 못한다. Manager에서 원 등록을 마감한 뒤 같은 HTTP 요청으로 시작한다. |
| HTTP 호출 예산이 등록 완료 직후 만료 | 등록 원장은 `SUCCEEDED`다. Xen 시작 원장과 명령은 아직 없다. | 현재 HTTP는 등록 후 확인용 `get`에서 `504`를 반환하므로 문맥이 `create/registration/unknown`이다. 같은 명세로 예산을 다시 주면 기존 디스크로 Xen을 한 번 시작한다. |
| Xen 시작 명령 뒤 확인 기한 만료, 도메인은 등장 | 등록과 자격은 보존된다. 시작 원장만 `uncertain`이다. | 같은 HTTP 생성은 살아 있는 도메인을 보고 `409 VMExists`를 반환한다. Xen 시작 명령은 한 번이고 원 시작 원장은 미확정으로 유지된다. 원 시작 ID 조회는 `uncertain`을 반환한다. |
| Zephyr, 브로커 계정 저장 후 사본 작성 전 호출 기한 만료 | private firmware와 예약을 기존 수습으로 정리할 수 있다. 브로커 drive·telemetry 계정 및 비어 있는 UUID별 공급 디렉터리는 남는다. | 수습 뒤 같은 작업 ID와 `RECOVERY_REQUIRED`를 돌려준다. firmware 재복사와 계정 재발급은 없다. Zephyr의 이 시험에는 Linux 생성 실행 기한 정책이 적용되지 않는다. |

게시 전 폐기가 반환하는 것은 private 작업 자산과 예약이다. 브로커 계정·공급 사본의 정리 여부는 별도 수명 문제로 남는다. HTTP 등록 마감 재진입과 시작 전 오류 문맥도 위 재현 상태 그대로 기록했다.

시작 명령 뒤 도메인이 생기지 않은 경우는 기존 [만료 시작 시험](../../platform/tests/test_xenova_expired_start_reconciliation.py)으로 확인했다. 원 기한 만료, 도메인 부재와 원 scope 종료가 확인되면 원 시작을 실패로 닫는다. 새 명시적 시작은 그 뒤 진행할 수 있다.

## 재현 명령과 시험 경계

로컬 WSL에서 dom0-manager 관련 회귀 226개, 생성·수습·실행 경계 회귀 96개, 만료된 시작 수습 21개가 통과했다. 자격을 연결한 시험 2개는 Debian·Ubuntu 각 4개 지점과 Zephyr 1개 지점을 확인했다.

Linux에서 저장소 루트 기준으로 실행한다. dom0-manager에는 기존 FastAPI·pytest 시험 의존성이 필요하다. 자격 fixture는 root 소유권을 실제 검사하므로 격리 Linux의 root에서 실행한다. 아래 명령에서 설치된 시험 의존성 경로는 환경에 맞춘다.

```bash
(
  cd platform/services/dom0-manager
  python3 -B -m pytest -q tests/test_platform_integration.py \
    -k 'dhcp_deadline or start_timeout or budget_can_expire'
)

PYTHONPATH=platform/tests:platform/yocto/meta-xenova/recipes-core/xenova-platform/files:platform/guests/common:platform/first-install:platform/services/vm-agent/mqtt-client \
  python3 -B -m pytest -q platform/tests/test_xenova_provisioning.py \
    -k 'linux_creation_deadlines or zephyr_supply_timeout'
```

임시 디스크 파일, 게시 rename, 원장, 복사 검증, 계정 세대와 private 공급 파일은 제품 코드를 사용했다. Xen·loop·mount·게스트 외부 명령과 Mosquitto 해시 실행 파일·reload는 기존 로컬 대역을 사용했다. 시간은 단조 시계 fixture에서 주입했다. 자격 값은 출력하지 않고 변경 여부만 비교했다. 운영 보드·게스트 기동·브로커 인증은 이 시험 범위 밖이다.
