# 늦은 결과와 AUTO 기능 상실의 소비 경계

2026-11-10, `feat/vehicle/jetson-world-contract` 작업트리 기준 로컬 검토다. 보드 배포·Xen 실행·차량 이동 시험은 수행하지 않았다. 기존 미커밋 platform 변경은 보존했다. 수정은 Gateway `warm.py`·`verified.py`, C `lattice_app.c`의 planner 관측 경계와 `adapter.c`의 거절 사유 보존, 해당 회귀·기록이다. 기존 engine/lattice와 Zephyr 빌드 설정의 WIP는 수정하지 않았다.

## 확인한 원인

- Zephyr worker는 원 release/deadline을 유지하고 늦은 결과를 즉시 제한한다. `main.c:warm_report`는 실제 완료 작업·계산 기한을 이용해 `NOT_READY/CALCULATION_FAILED`를 보고한다. 이 동작은 유지했다. 다만 외곽 tracker callback의 progress가 정상이어도 별도 planner owner는 정체할 수 있어 그 소유자의 원 작업 기한을 구분했다.
- Gateway는 원래 단일 `NOT_READY`를 즉시 B 승계/SAFE_STOP으로 소비했다. `WarmDriveMachine.on_lifecycle`는 이유와 무관하게 적용된 state 증거까지 지웠다. 결과 하나 거절과 기능 상실 확인, 재사용 가능한 상태 보존이 분리되지 않았다.
- `1da02fe3`이 제거한 AUTO 반복 지연→VM 전체 `vm.trust=UNTRUSTED` 발행을 되살리지 않았다. Gateway의 출력 보호/승계는 VM 재기동 작업과 별개다.

## 변경한 판단

| 상황 | 결과·출력 | 복구/모드 판단 |
| --- | --- | --- |
| 단일 `CALCULATION_FAILED`/`DEADLINE` | 현재 부적합 결과 전달 금지. 기존 출력 명령의 원 수명만 유지하고 만료 뒤 같은 세대 STOP | 기존 readiness 300ms 창 안에서 실제 계산 회복 관찰. 정상 회복하면 같은 AUTO/owner/generation으로 계속 |
| 계산 READY/progress는 신선하지만 명령 전달 공백 | 마지막 유효 ForwardDrive의 원 100ms 수신 수명 종료 뒤 STOP | 현재 연속 READY 구간의 최초 실제 work 시각(`p.since`)과 마지막 유효 출력의 원 수신 시각 중 늦은 시각부터 300ms에 기능 상실 확인. 반복 READY는 고정 `p.since`를, 거절 명령은 출력 시각을 갱신하지 못함 |
| 계산 실패 지속 | 거절된 새 명령도 기존 출력 기한을 연장하지 못함 | 마지막 실제 READY 작업과 최초 거절 중 이른 시각부터 300ms 내 회복하지 못하면 현재 준비된 B 승계, 불가하면 STOP→MANUAL 준비. 반복 보고는 관찰 창 연장 불가 |
| 정상 planner 대기 | 즉시 같은 세대 STOP, 새 DRIVE 없음 | 실제 callback 완료·현재 세대 progress가 계속 진전할 때 AUTO 계획 대기. 감독만 반복되고 작업이 멎으면 기존 기능 상실 경계 적용 |
| 실제 binding/app 신원 손실, STOPPING/RESIDUAL 등 | 원 결과·출력 허가 철회 | 기존 즉시 승계/정지 경로 유지. 종료 phase는 보고된 대기/입력 reason보다 우선 |
| A의 인지/pose/clock만 불가, B 준비됨 | A 결과 전달 금지 | 현재 준비된 B 승계 가능 |
| 두 AUTO 경로가 입력을 제공하지 못함 | AUTO 결과 전달 금지 | 정상 로컬 MANUAL로 전환. 복구만으로 AUTO 출발하지 않음 |

정상 planner 대기 이유는 `PATH_UNAVAILABLE`, `PATH_INVALIDATED`, `SEARCH_NOT_FOUND`, `NODE_BUDGET`, `WORK_BUDGET`, `BUSY`, `COLLISION`, `PATH_LIMIT`, `CANCELLED`, `STATE_PREPARING`, `CONTEXT_CHANGED`와 아래 네 state 거절이다. `NUMERIC_ERROR`, `RESOURCE_LIMIT`, 실제 실행/자원 소유 손실 등을 정상 대기로 일괄 완화하지 않았다.

적용 증거는 `STATE_REJECTED`, `STATE_FRAME_CHANGED`, `STATE_CONFIGURATION_CHANGED`, `STATE_REFERENCE_CHANGED` 또는 실제 앱/실행 신원 교체에서 철회한다. 계산 지연·일시 입력 만료·탐색 거절만으로는 삭제하지 않는다. 보존된 증거는 현재 binding과 앱의 현재 입력 재검증을 통과해야 사용할 수 있고, 그것만으로 READY·새 명령·새 권한을 생성하지 않는다.

## Planner owner와 외곽 callback 구분

`adapter.c`의 `progress`는 callback이 정상 반환할 때 증가한다. 내부 planner가 멎었어도 `de_track`이 `STATE_PREPARING`/`BUSY`를 즉시 반환하면 외곽 progress는 계속 늘 수 있다. 따라서 이 값만으로 별도 planner가 건강하다고 판정하지 않는다.

`planner_health.h`의 단일 atomic 시각 snapshot은 planner owner만 기록하고 tracker/qualifier는 읽기만 한다. 검색·상태 적용 중에는 **기존 원 `budget.deadline_us`**를 고정한다. 현재 프로필의 검색 예산은 2초이며 이를 300ms로 줄이거나 2.3초로 늘리지 않았다. 실제 search admission에서 원 예산을 기록한 후 진행 중 slice와 반복 BUSY가 그 시각을 연장하지 않는다. 아직 admission하지 못한 BUSY 재시도도 첫 unresolved attempt의 원 기한을 유지한다. 실제 정상 완료·no-path 완료 뒤에는 기존 300ms 완료 관측 창을 기록한다.

원 pending 기한 또는 완료 관측이 끝났는데 planner 작업이 회복하지 못하면 실제 callback/qualification은 기존 `CALCULATION_FAILED`로 보고한다. Gateway는 그 결과를 즉시 제한하며 기존 기능 상실 관찰을 적용한다. 별도 wire/schema 필드는 추가하지 않았다. `adapter.c`는 포괄 `STATE_PREPARING`/planner 관측 사유가 callback에서 확인한 구체적인 인지 입력 장애·정상 no-path·계산 실패를 덮지 않도록 하되 결과 제한과 영속 권한 경계는 유지한다.

이 관측은 취소 완료나 자원 회수를 대신하지 않는다. planner stop·기존 원 기한의 join·소유 참조 해제·새 instance 재기동 절차는 그대로이며, 관측 만료만으로 기존 thread의 메모리를 재사용하지 않는다.

## 검증

기존 WSL Python 3.12 가상환경에 의존성을 설치하지 않고, 작업 공간에 보존된 pytest 8.3.5 도구와 paho 2.1.0 wheel을 `PYTHONPATH`로 읽었다. Gateway·공용 MQTT·world 패키지 경로도 자식 프로세스에 전달했다.

```sh
python -B -m pytest -c platform/services/gateway/pyproject.toml -o addopts= \
  platform/services/gateway/tests/test_function_failure_scope.py \
  platform/services/gateway/tests/test_warm.py \
  platform/services/gateway/tests/test_verified.py \
  platform/services/gateway/tests/test_state_transfer.py \
  platform/services/gateway/tests/test_planning.py -q -p no:cacheprovider
```

결과: **89 passed in 21.83s**. 전용 18건은 단일 지연 후 같은 세대 회복, 거절 반복과 원 명령 수명, 부적합 새 명령의 기한 연장 금지, 지속 지연 B 승계, B 불가 시 정지·재무장 필요 MANUAL, 정상 planner 대기, 작업 정체와 감독 분리, 공통/개별 입력 장애 범위, 실제 binding 손실, 상태 증거 보존/철회를 검사한다. 명령 공백 회귀는 120ms 뒤 STOP·같은 세대 회복, 반복 READY의 300ms 관찰 창 연장 금지, 최초 출력 전·정상 대기 후 회복의 고정 READY anchor, 새 유효 명령만 새 공백의 기준이 됨, 공백 중 실제 binding 손실 및 STOPPING/RESIDUAL은 즉시 승계함을 확인했다. 기존 시험은 원 pending/control 기한, 실제 프로세스 종료 뒤 상태 인계, 상태ful 준비와 연속 승계의 원 명령 예산 등 기존 계약을 확인했다.

추가 반례 전 기준 실행은 82건, 명령 공백 추가 뒤는 86건이었다. 최종 실행은 위 89건이며 수정 대상의 `git diff --check`도 통과했다.

기존 CMake 실행 파일은 없어 설치하지 않았다. 기존 `platform/apps/drive/tests/CMakeLists.txt`의 source 목록을 기존 `cc -std=c11 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined`로 직접 빌드했다. `planner_health.c`, 실제 adapter/controller의 `controller.c`, 기존 `preparation.c` **3개 native C target이 ASan/UBSan으로 통과**했다. 새 target도 CMake에 등록했다. planner snapshot 시험은 정상 2초 search, BUSY 재시도의 원 기한 고정, 정상 no-path 완료, 외곽 tracker가 계속 도는 동안 planner snapshot 갱신만 중단한 반례를 대조한다. adapter 시험은 정상 no-path와 공통 입력 장애 사유가 포괄 상태 대기에 가려지지 않는 것을 검사했다. 기존 수동 세션·기한·상태 인계와 공유 manual vector 12건도 통과했다.

실행 명령·binary는 현재 작업 공간의 ignored `vehicle/Jetson/build/supervision-audit/rpi-recovery-scope-20261110/run-c-review.py`와 `c-native/`에 남겼다. 실행은 `python3 -B <run-c-review.py> <repository-root>`다. 이는 snapshot 갱신 중단을 주입한 native 단위 반례이며 실제 Zephyr thread를 강제 중단한 시험은 아니다.

초기 실행의 기존 프로세스 시험 두 건은 자식 `PYTHONPATH` 누락으로 import에 실패했다. 경로를 전달한 최종 실행에서 통과했다. 최초 원 명령 반례는 부적합 새 명령이 계속 오면 STOP이 늦어지는 것을 드러냈고, 마지막 ForwardDrive의 원 수신 시각을 따로 유지해 수정했다.

## 한계와 후속 확인

- 300ms는 새로 늘린 주행/명령 기한이 아니라 기존 readiness 근거 창이다. 차체 제동·ESP32 만료·보드 부하 조건에서 적절한지 물리 검증은 남는다. 기존 20ms 작업 기한·100ms 명령 신선도·명시적 재출발 조건은 변경하지 않았다.
- 실제 Xen A/B 승계, MQTT 지연, 수동 조종 유지와 물리 정지는 이번 로컬 actor 회귀가 입증하지 않는다. Worker의 일반 앱 재초기화 선택을 새로 구현한 것도 아니다.
- 전체 Zephyr 펌웨어 빌드와 `lattice_app.c`의 실제 Zephyr 실행은 수행하지 못했다. 현재 WSL의 CMake/SDK 경로가 확인되지 않아 새로 설치하지 않았으며, 보드·펌웨어 배포는 이번 범위에서 제외했다. C snapshot과 adapter native 시험을 전체 펌웨어 빌드나 실제 thread 정지 시험의 증거로 확대하지 않는다.
- 새 World 입력의 C `dw_publish`는 같은 source/epoch에서 단조 증가 snapshot sequence를 허용하며 `+1` 연속을 요구하지 않는다. binary state application sequence도 단조 증가를 허용한다. 이 경로에서 전달 gap을 VM 전체 고장으로 전환하는 처리는 확인되지 않았다.
