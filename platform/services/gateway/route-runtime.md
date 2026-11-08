# 출차·시작점 복귀 Gateway owner

이 문서는 B10 신규 모듈과 아직 연결하지 않은 경계를 구분한다. 공통 wire는
[route-and-return.md](../../../docs/contracts/drive-world/route-and-return.md)를 따른다.
B09 A1 Gateway/C의 정상 병합 전에는 기존 actor·CLI·transport를 수정하지 않았다.
따라서 신규 모듈이 존재한다는 사실은 `home.return` 또는 실제 차량 출차 성공을 뜻하지 않는다.

## 구현된 유한 owner

| 모듈 | 입력과 책임 | 출력 또는 보관 |
| --- | --- | --- |
| `route_policy.py` | 승인된 설치 profile의 명시 시간·거리·phase·gap·작업량·hash 검사. 물리 기본값 없음 | 변경 불가 `RouteBudgetPolicy` |
| `route_data.py` | 원 RoadData/record bytes·현재 M 또는 Guest 출처·D membership. 실제 발행 World 원문과 sidecar의 SHA/출처 결박 | bounded record pool, witness8, lookup1, registration1 |
| `route_owner.py` | 원 출발 원장 key/CAS→Query1→Candidate1→실제 kind3 grant ACK→receipt→fullState→실제 대상 apply→새 계산 순서 | registry1, last exact reply1, terminal1, eligibility2, source별 HWM4 |
| `route_goal.py` | 실제 WAIT/STOP의 원 Vehicle·facts로 home.set 요청, M 검증과 원 D COMMITTED 뒤 목표 채택 | goal1, pending1, last result1, 정확한 home.status 8필드 |
| `route_transition.py` | 원 suffix/prefix F32 seam, source binding CAS, M transition generation, 실제 새 World/route_view | transition1, last reply1, terminal1, receipt 대기1 |
| `route_episode.py` | 같은 서비스의 실제 inactive episode registry+accepted source/주차 원장 이력 | 원 State/accepted wrapper bytes. 없으면 UNKNOWN, wrapper 재포장 없음 |

record pool은 목표1·topology1·section2·connection2·window3·ExitSpace1·GlobalPlan1·scope1이다.
현재 route와 home goal의 pin은 별도 소유한다. 활성 원본은 압력 때문에 버리지 않고 BUSY를
반환한다. route가 명시 종료/철회되면 해당 pin을 해제하되 원 Candidate/Query/GlobalPlan/receipt와
terminal은 유한 RAM 이력으로 보존한다. 새 thread·daemon·DB·동기 D I/O는 없다.

## 등록과 기한

1. `begin_query(raw, acknowledged_operator_row)`는 기존 durable 출발 수락 뒤 호출한다.
   현재 COUNTDOWN의 service/op/permission/원 departure nonce와 이전 READY revision을 검산한다.
   설정 변경·READY·Query 자체는 출발 명령이 아니다.
2. `candidate(raw, authenticated_peer)`는 원 raw SHA와 nonce/sequence/CAS, current owner·confirmed
   P4·Vehicle·World·meter·scope·Scene·extent를 검산한다. 원 retry는 같은 bytes의 원 결과만 돌려준다.
   같은 nonce의 다른 bytes는 거절한다. pending은 아직 성공 receipt가 아니다.
3. `registration()`의 원 body/snapshot/binding으로 기존 async Authority worker가 실제 kind3 grant를
   발급해야 한다. `commit_grant()`는 실제 현재 grant bytes와 원 witness E까지 확인한 뒤에만 receipt를 만든다.
   grant 실패/원 E 만료 뒤에는 actor가 STOP을 유지하고 원 요청을 실패로 종결해야 한다.
4. Candidate가 예약한 첫 State ID·새 actual completion·원 core/ref/localPath와 current grant를 검산한
   `prepare_state()` 결과를 기존 StateCopies 수락과 같은 actor 회차에서 `commit_state()`한다.
   State 수락은 적용이 아니다. `applied()`는 기존 대상의 실제 StateCopies 증거와 보존 accepted wire를 검사한다.
5. `command_allowed()`는 현재 source/feature64, 실제 grant/mission/plan/phase, fullState 이후의 source별
   계산 번호와 실제 apply ACK 뒤의 completed lower, 원 P4·World·Vehicle·hard cap을 검사한다.
   이는 기존 Authority/command parser/Scene/physical 검사에 추가하는 조건이다. `output_deadline()`을
   최종 출력 min E에 함께 넣는다. 순수 STOP과 기존 인증 MANUAL은 이 조건으로 추진 권한을 새로 얻지 않는다.

Query의 10s E는 새 BEGIN admission 기한이다. 등록 뒤 UPDATE/FINISH/A-B는 그 원 id/SHA/raw를
불변 provenance로 조회한다. 현재 peer/grant/P4/witness와 원 route/phase E는 별도로 검사한다.
조회는 Query E 연장·새 nonce·이전 owner 부활·예산 환불이 아니다. 같은 Query로 두 번째 BEGIN은 없다.

BEGIN의 Pi start는 원 Guest birth의 confirmed interval lower다. 그 lower 이전에 수신한 같은
연속 meter epoch의 실제 encoder anchor를 pin한다. 도착 표본으로 baseline을 늦춰 이동을 환불하지 않는다.
매 accepted encoder의 wrap-aware `max(abs(ΔL),abs(ΔR))` 정수 누적을 사용한다. gap/scale 변경은 원 경로를
이어가지 못한다. 실제 phase 변경만 새 phase cap을 만들고, same-phase/window/local replan과 A/B는 원 수치를 보존한다.

실제 progress가 확인된 때만 watchdog anchor를 전진한다. `confirmed_stop_wait_us`는 route 전체 누적이며
Pi RAM의 `wait_baseline`을 실제 progress/phase anchor 때 pin해 그 이후 차분만 공제한다. 원 command/session과
APPLIED·양 wheel/yaw stationary window를 대조하며 heartbeat/ACK/속도 목표0은 정지가 아니다. hard E는 멈추지 않는다.

## 필수 내부 포트와 미연결 경계

아래 callback은 wire API가 아니다. 기본 성공값이나 임시 READY로 대체하면 안 된다.

| 포트 | 정상 병합 뒤 연결할 실제 owner |
| --- | --- |
| `current_owner()` | 현재 인증 binding/execution/AUTO app·실제 generation·OperationBinding·mission/grant/cancel·Scene·source key/scope |
| `installed()` / `mapper_support()` | 승인 설치 profile와 현재 원 MapperReply.raw/Reply20+source+lease E의 지원 교집합 |
| `mapper_current()` | 실제 MapperClockOwner source/원 P4/lease, admission revision 및 원 report E |
| `guest_current()` | 인증된 현재 Guest source와 실제 confirmed clock/witness. Guest는 GlobalPlan만 생산 |
| `durable_membership()` | 실제 원 D receipt의 job/digest/COMMITTED/revision/origin/record membership |
| `world_current()` | 이미 정상 발행·수신한 실제 World 원 bytes. bool/관리 ACK 반환 금지 |
| `terminal_check()` | 실제 STOP facts와 현재 정합 pose/차체·오차·merge interval 또는 StartGoal terminal profile 검산 |
| `progress_check()` | 실제 tracker/Vehicle·occurrence/edge/seam 진행 검산. pose heartbeat로 성공 처리 금지 |
| `stop_auto()` | 같은 actor의 현재 route/AUTO RAM 철회를 먼저 처리. 정당한 독립 MANUAL을 취소하지 않음 |

통합 시 남은 작업은 다음과 같다.

- B09 정상 병합 뒤 current_operation/Authority 기존 async worker·kind3 mission issuer·미션 원 parking
  attempt 보존과 연결한다. callback 실패/expired grant ACK를 현재 권한으로 재포장하지 않는다.
- 실제 public `home.set/status/return`, 출차 departure mode, 기존 CLI/parser/원장 성공·거절 응답을
  공통 계약의 exact args로 연결한다. 현재 신규 파일만으로 public 명령이 열린 것은 아니다.
- DriveLink12/13의 원 nested bytes를 실제 인증 dispatch/유한 관리 transport로 연결한다. M의 실제
  RoadData/D receipt/World sidecar와 C Query/Candidate/receipt/transition 흐름을 왕복 시험한다.
- first fullState 수락 전 계산 자체는 허용해 snapshot 생산 순환을 피하되, DRIVE는 보류한다.
  StateCopies 실제 copy/raw/application 이후에만 현재 대상의 새 계산을 출력한다.
- EpisodeOwner의 known feature 검사는 공통 상수와 합치고, EXIT에서 ACTIVE/UNKNOWN/absence를
  신규 NONE로 초기화하지 않는다. 최초 grantless WAIT/offroad는 유한 NOT_READY다. 실제 완료/중단
  parking의 HOLD 원장과 원 accepted source가 있는 경우에만 historical adapter를 제공한다.
- 현재 인증 peer/M capability 변경 직후 `support_changed()`를 호출한다. 선택한 새 profile의 손실은
  sticky STOP이며 capability 복귀만으로 재출발하지 않는다. 기존 legacy warm 전체에는 소급하지 않는다.
- A1 bit1과 route bit2는 실제 수락된 **원 MapperReply.raw의 Reply20·source·원 lease E**에 결박한다.
  현재 지원 부재/다른 source/재시작/만료면 새AUTO readiness·active AUTO/route를 철회한다. 숫자 P4,
  history/map/NN와 MANUAL은 독립이다. M의 자기 광고나 오래된 cache만으로 설치 승인을 만들지 않는다.
- OwnedPreparedClock는 stale RouteStatus15만 omit하고 원 numeric/P4/probe/receipt는 보존한다.
- PREPARE/ACTIVATE receipt는 실제 World HWM 변경이나 DRIVE가 아니다. ACTIVATE는 같은 원 seam/
  transition과 실제 새 route_view를 확인하고 새 phase/window State 수락 뒤 COMMIT_NOTICE한다.
- 정상 A/B는 원 Query/receipt/core/parking budget을 보존하고 current grant로만 재결합한다. 원 A 계산
  번호와 B 계산 번호를 크기 비교하지 않는다. B fullState/apply/fresh calc·실제 출력 적용은 후속 통합 범위다.

## 검증 범위

`test_route_data_runtime.py`, `test_route_owner_runtime.py`, `test_route_history_runtime.py`는 원 codec/cache,
실제 GuestClockOwner P4와 AbsoluteWheelMeter/route registry를 사용한다. A1 내구 원장 ACK·M/D publication·
application·geometry tracker는 명시 typed leaf다. C planner/MCU/물리 장치 적용 증거로 세지 않는다.

정적 AST 3.11·schema constructor 필드 대조와 diff 검사를 수행한다. host import/pytest/collect/native/SSH는
실행하지 않았다. 실제 Nano에서 공통15+C helper+Gateway 의미시험을 한 묶음으로 실행하고, 정상 A1 병합 뒤
실제 actor/C/M 왕복으로 위 미연결 경계를 이어 검증해야 한다. pyserial/실물/Xen/MCU 수락은 별도 미실행 항목이다.
