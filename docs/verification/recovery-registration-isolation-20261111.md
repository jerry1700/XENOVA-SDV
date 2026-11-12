# 등록 미완료 VM의 복구 판정 격리와 등록 경합 보완

## 확인한 보드 상태

2026-11-11 `root@100.108.226.65`를 읽기 전용으로 확인했다.
대상은 `web-test-1111`, VM UUID `6f430da2-85bf-4703-a4da-429d04befeb8`,
원 create `27e22bd5-d3ac-479b-a96d-f4d93c729654`다.

- 원 작업은 `RECOVERY_REQUIRED / published`이며 최초 오류는
  `OPERATION_IN_PROGRESS / short lock is busy / details.lock=admission`이었다.
  게시 instance는 있었고 tombstone과 intent는 없었다.
- 일반 `intents` 전체 조회는 `TARGET_CHANGED`로 실패했다. 정상 A/B의 단건 의도 조회는 성공했다.
  Worker는 전체 의도를 모른다고 판단해 `INTENTS_UNAVAILABLE`로 모든 자동 판정을 보류했다.
- 이 작업에서 보관을 실행하기 전 다른 작업으로 활성 경로가 사라졌다. 조회한 보관 위치는
  `/srv/xenova/manual-backup-1111`다. 이 작업에서는 VM 정지·디스크 이동·원장 삭제를 실행하지 않았다.
- 보관된 원장 SHA-256은
  `0fc267a0bfd6a7aba5bc06ca5ec8c28492283820ea93099822e78f2b59fc3b27`로 이전과 같았다.
  보관 `root.raw`는 8,589,934,592바이트, inode 786440으로 이동 전과 같았다.
  전체 디스크 해시는 실행하지 않았다.
- 기존 Worker PID 78165는 재시작 없이 `OPERABLE`, `intents_known=true`,
  `hold_reasons=[]`로 회복했다. A/B의 domid 1·4도 유지됐다.
  이 상태 확인은 장애 주입을 통한 A/B 종단 복구 수락과 구분한다.

## 원인과 구조적 공백

`Manager._register_created`는 게시 뒤 먼저 `_save_tomb`으로 수명을 기록하고,
DHCP 반영 뒤 `_finish`에서 초기 intent와 원 결과를 확정한다.
실제 기록의 `published`·tombstone 부재·admission 오류는 첫 수명 기록 단계의 경합과 일치한다.
당시 admission 잠금 점유자는 남은 로그로 확인하지 못했다.

기존 `PendingRegistration / _complete_registration` 대기는 마지막 intent 초기화에만 연결돼
있었다. 첫 수명 기록은 같은 잠금 경합을 즉시 실패로 돌려줘 게시 자산과 미완료 원 작업이
남을 수 있었다. 별도로 전체 의도 조회는 그 한 항목을 전체 snapshot 실패로 처리했고,
Worker가 정상 VM까지 보류했다.

## 보완한 경계

| 위치 | 현재 동작 |
|---|---|
| `xenova_management.ReadContext / Manager._query` | 내부 `recovery-intents`에서 원 VM·create ID·원 명세·템플릿 신원이 일치하는 등록 미완료 항목만 표시 |
| `dom0_manager.recovery.observe.PlatformObserver` | 해당 항목만 `intent_known=False`로 유지하고 다른 VM은 현재 의도로 판정 |
| `recovery.__main__ / drivers.platform` | Worker의 실제 진입점과 읽기 fork·호출 예산에 내부 조회를 연결 |
| `Manager._register_created / _defer_registration / _complete_registration` | 첫 수명 기록과 마지막 intent 초기화의 admission 경합을 같은 제한된 대기 경로로 처리 |
| `Manager._prepare_dhcp` | 기존 대상 lease가 있는 재개 경로는 공통 잠금 밖에서 I/O하고 복귀 뒤 원 문맥을 대조 |

`registration_pending=true` 항목은 목록에 남는다. 의도는 `unknown`,
revision은 null, applicable은 false다. Worker는 이를 등록 해제나 사용자 stop으로
해석하지 않으며 건강 기록과 열린 복구를 유지한다. 정상 VM의 판정은 계속한다.
원장 누락·손상·신원 불일치나 불안정한 등록 집합은 기존 조회 실패 보호를 유지한다.

등록 대기는 원 호출 예산 안에서만 진행한다. 미반영 Linux DHCP는 원 생성 실행 기한과
정리 여유도 적용한다. 공통 잠금을 놓고 기다리며 대상 lease·원 요청·원 기한을 유지한다.
host·원장·instance·tombstone 변경은 재진입 시 거절한다.
대기 중 같은 원장을 반복 저장하거나 자산을 재복사하지 않는다.

[CPU 준비 차단 범위 보완](cpu-preparation-asset-isolation-20261110.md)과
[게시 전 자동 수습](../platform/cli-manager-spec.md#cmd-operation-reconcile)의 범위를 유지한다.
게시 자산은 private 자동 삭제에 넘기지 않는다. E4 예산·출력 보호·일반 공개
`intents`의 완료 검증은 기존 동작을 유지한다.

## 반복 비용 확인

VM 세 대의 격리 fixture에서 기존 `intents`와 `recovery-intents`를 실제 호출하고
읽기·쓰기·등록 스캔을 계수했다.

| 한 호출의 계수 | intents | recovery-intents |
|---|---|---|
| Manager metadata 읽기 | 9 | 9 |
| 등록부 스캔 | 2 | 2 |
| Path.read_text 전체 계수 | 27 | 27 |
| Manager 파일 쓰기 | 0 | 0 |

같은 호출에서 `Platform.execution`과 `Manager._operations`를 호출하면 즉시 실패하도록
하여 Xen 관측·전체 원장 스캔 의존이 없는 것도 확인했다. 새 필드의 작은 메모리 값과
등록 미완료 항목의 신원 대조만 더한다. 정상 Worker 주기는 기존과 같은 한 번의 의도 조회다.
등록 경합 때는 기존 short_lock의 유한 sleep 대기를 사용한다. 기다리는 매 회차에
registry를 스캔하거나 로그·원장을 저장하지 않는다. 공통 잠금을 놓고 대상 lease를 유지한다.
이는 fixture 호출 계수이며 보드 CPU 사용률 절감의 보장은 아니다.

## 검증

WSL Ubuntu-24.04, Python 3.12.3, 기존 시험 venv에서 다음 회귀를 실행했다.
결과는 **295 passed, 1 warning, 79.90초**다.
경고는 기존 FastAPI/Starlette test client의 httpx 사용 중단 예정 안내다.

```bash
export PYTHONPATH="$PWD/platform/first-install:$PWD/platform/tests:$PWD/platform/services/vm-agent/mqtt-client:$PWD/platform/services/gateway:$PWD/platform/services/dom0-manager:$PWD/platform/yocto/meta-xenova/recipes-core/xenova-platform/files"
/home/ssafy/xenova-platform-refresh-20261110-l0m7b1cj/test-venv/bin/python -B -m pytest -p no:cacheprovider -ra \
  platform/tests/test_xenova_management.py \
  platform/tests/test_xenova_creation_reconcile.py \
  platform/tests/test_xenova_creation_auto.py \
  platform/tests/test_xenova_management_budget.py \
  platform/services/dom0-manager/tests/test_recovery_worker.py \
  platform/services/dom0-manager/tests/test_recovery_status.py \
  platform/services/dom0-manager/tests/test_platform_driver.py
```

- 등록 첫 수명 기록의 실제 admission 경합에서 원 호출을 재개해 완료한다.
  기다리는 동안 공통 잠금을 다른 요청이 획득하고, 다른 VM의 의도 조회와 stop도 완료된다.
- 기한 만료 시 게시 디스크·원 작업을 남기고 초기 intent를 만들어내지 않는다.
- 첫 수명 기록과 마지막 intent 단계 각각에서 host·operation·tombstone·instance 변경을 거절한다.
- 실제 Zephyr firmware 준비 경로에서도 두 admission 대기를 통과하고,
  대기 중 내부 조회가 해당 Zephyr만 등록 미완료로 표시하는 것을 확인한다.
- storage 실패로 남은 게시 VM을 격리하고, 정상 완료·원 key 재전달 후 표식을 제거한다.
  공개 `intents`의 완료 검사와 원장 불일치 거절도 유지한다.
- Worker에서 등록 미완료 항목을 건강 보고에 유지하면서 다른 VM의
  `DOMAIN_CRASHED` 판정을 계속한다. 잘못된 표식은 전체 조회 실패로 보류하고 회복 후 재개한다.
- 옛 Manager의 기존 `intents` 호환 경로와 자동 수습·상태 보고·호출 예산 회귀도 통과했다.

`git diff --check`도 통과했다.

제품 코드는 실제 Manager·Worker·소유 lease·파일 잠금을 사용한다.
로컬 Xen 명령·외부 서비스·storage 오류는 기존 격리 fixture에서 대체한다.
첫 확인에서는 기존 Worker의 정상화와 보관 상태를 읽기 전용으로 확인했다.
이후 사용자 승인으로 아래 서비스 교체를 수행했다.
기존 recipe가 변경 Python 파일을 포함하는 설치 경로도 확인했다.
이번 후보의 target package 재빌드는 수행하지 않았다.

## 보관 주체 재확인

보관 디렉터리의 UID는 0이었다. 보관을 실행한 개인이나 대화방을 식별할 기록은
확인하지 못했다. 이 대화방의 실행은 보관 직전의 원장 확인 단계에서 중단돼 자산 이동을
수행하지 않았다. 이후 같은 host boot에서 다시 확인했을 때
`/srv/xenova/manual-backup-1111` 경로가 사라졌고 `/root/.bash_history`에도 해당 경로의
명령은 없었다. 앞서 확인한 동일 원장 해시·디스크 inode는 당시 보관 상태의 근거로 남긴다.
최종 조회에서는 같은 VM UUID가 다시 REGISTERED이며 domid 6으로 실행됐다.
현재 인스턴스의 디스크 inode는 786440이고 생성 작업은
`74c02051-6c61-42cd-bb9c-d673180e6d7e`를 가리킨다. 이전 생성 작업 파일과 보관 경로는
찾지 못했다. 해당 UUID의 API 삭제 시도는 404였으며 보관이나 후속 등록을 한 사람은
특정할 수 없다. 이번 서비스 교체는 이 VM의 생성·시작·보관을 수행하지 않았다.

## 보드 서비스 적용

사용자 승인 뒤 구현과 회귀·계약 문서를 `93006e9c`로 커밋했다.
보드의 기존 네 모듈이 커밋 부모의 파일과 일치하는지 확인하고 root 전용 코드 백업을
만든 뒤 해당 네 파일만 원 소유권·모드로 교체했다. Manager와 Worker를 정상 재시작했다.
설치 파일의 SHA-256과 커밋 소스를 바이트 대조했다.

| 설치 파일 | SHA-256 |
|---|---|
| `/usr/lib/xenova/xenova_management.py` | `0a435e15d09a32f6dc78d085dc807d9e4f6c363e4624d56e8eb73916bd725520` |
| `/usr/lib/python3.14/site-packages/dom0_manager/drivers/platform.py` | `28a8f71f76dc22cca175c92ff210c02139be5d0bfe34c1adf1440c510cc59911` |
| `/usr/lib/python3.14/site-packages/dom0_manager/recovery/observe.py` | `ddff41aa2be163bb35b95873ff1b7a702e8ee6cb6aa8c46f8141fa012f5c1e46` |
| `/usr/lib/python3.14/site-packages/dom0_manager/recovery/__main__.py` | `839dccc8c706e899a65bce20227ea183395ceca8ab0ba2691aebbb58e75d4639` |

첫 적용에서는 API 기동 직후 VM 목록 503을 받아 이전 코드로 복구했다.
PID 188532의 로그는 `shared VM observation unavailable`이었다.
기존 `ObservationHub.start`는 먼저 비어 있는 관측을 게시하고 수집 thread를 시작한다.
재적용에서는 API 준비를 유한하게 확인한 뒤 Worker를 시작했다.
동일한 일시적 503 한 번 뒤 200으로 회복했고, Worker 시작 후의 API 확인도 200이었다.
제품의 관측 만료·운영 예산·발행 주기는 변경하지 않았다.
이 실패와 재적용 근거를 각각 보존했다.

- 적용 후 Manager PID는 191215, Worker PID는 191251이다.
- Worker는 `OPERABLE / intents_known=true / hold_reasons=[] / gateway_available=true`다.
- 주행 A domid 1, 주행 B domid 4, Debian domid 5가 유지됐다.
  현재 의도·intent revision·lifecycle generation도 적용 전과 같았다.
- 기존 API 토큰과 E4 누적 예산을 적용 전후 대조해 동일함을 확인했다.
- Gateway PID 137601은 유지됐다. 서비스 교체는 해당 두 Python 서비스에 한정했다.
- 이전 코드 백업은 `/root/xenova-registration-rollout-20261111-4msy8t9k`다.
  이 백업은 서비스 코드와 배포 검증 정보다.

로컬 원 실행 기록은
`C:/Users/SSAFY/Documents/Codex/2026-11-11/gateway-idle-ledger-evidence/registration-rollout-20261111.jsonl`과
`registration-rollout-20261111-second.jsonl`에 남겼다.

## 적용 뒤 재확인과 비용 표본

추가 Tailscale SSH 연결이 시간 초과되어 기존 보드 호스트 키를 확인한 LAN SSH로
다시 점검했다. 설치 네 파일의 해시와 적용 직후 세 서비스 PID가 유지됐다.
인증한 VM 목록은 연속 3회 200, Worker 상태는 `OPERABLE`이고 관측 갱신은
4.64초 전이었다. Gateway context의 generation은 1, 모드는 `SAFE_STOP`,
`output_permitted=false`였다. 기존 A/B·Debian의 domid 1·4·5가 유지됐고
동시에 별도 등록된 같은 UUID의 `web-test-1111`이 domid 6으로 관측됐다.

현재 등록 VM 네 대에서 두 의도 조회를 각 5회 측정했다. 두 조회 모두 호출당
Manager 파일 읽기 8회, 파일 쓰기 0회, 외부 명령 0회였다. 지연 중앙값은
`intents` 106ms, `recovery-intents` 88ms였다. 적용 전 3대와 적용 후 4대의
파일 계수는 서로 다른 등록 집합에서 얻은 값이다.

| 프로세스 CPU, 한 코어 기준 20초 표본 | 교체 전 | 교체 후 |
|---|---|---|
| Manager | 13.80% | 15.10% |
| Worker | 25.55% | 25.35% |
| Gateway | 23.70% | 23.70% |

운영 부하와 등록 집합이 다른 짧은 표본이므로 CPU 절감률이나 지연 상한으로 해석하지 않는다.
후속 원 기록은 같은 evidence 디렉터리의 `registration-steady-check-20261111.jsonl`과
`registration-active-target-20261111.jsonl`에 남겼다.
