# 관측 도로·출차·시작점 복귀 공통 계약

이 장은 B1~B3의 bounded wire·원문 hash·형식 검사를 정의한다. 현재 clock/권한,
M/D 수락, C 경로 계산, Gateway ledger 또는 실제 출력을 공통 codec가 발급하지 않는다.
실행 owner는 현재 원관측·geometry·occupancy·권한·설치 profile을 별도로 재검증한다.
기존 Runtime1, operator.v2, legacy State/World와 기존 fingerprint domain은 유지한다.

## 1. 부모와 지원 협상

정확 필드와 필드별 배열/bytes 상한은 [world.proto](world.proto), [world.options](world.options)가 원본이다.
신규 nested 34형 + 관리 envelope 2형이며 새 service/endpoint를 추가하지 않는다.

| 기존 부모 | 추가 필드 | 의미 |
| --- | --- | --- |
| WorldSnapshot | 42 route_view | 현재 관측 local window 참조. required_features bit16 |
| DriveState | 35 route | 완전 RouteSnapshot. state_features bit64 |
| MissionExecution | 27 route_binding | kind3 원 route/core/plan/goal 결박 |
| MissionGrant | 33 route_binding | kind3 현재 권한과 원 route/core/plan/goal 결박 |
| DriveLink | 12 road_data / 13 route_exchange | 기존 bounded transport의 관리 메시지 |
| ClockReply | 15 route_status | 현재 RAM registry/실제 fullState·apply HWM 요약 |

World의 지원 조합은 기존 0/1/11/15와 신규 27/31이다. 신규 조합은 bit16과
route_view의 실제 presence가 일치해야 한다. State는 기존 bit7/8/16/32를 유지하고
bit64와 route presence를 대조한다(known mask127). 지원 광고와 실제 state presence는
서로 다른 개념이다. ClockProbe.supported_state_features의 bit64는 해당 설치 profile의
관리 wire·status·RouteSnapshot 소비까지 지원한다는 광고이며 혼자 권한을 만들지 않는다.
MapperClockReply.supported_mapper_features는 기존 bit1 operation에 bit2 route를 추가한다.
부재/손실은 새 출차 지원의 증거가 아니다. 한번 활성화된 신규 profile의 cap 손실은
sticky STOP/명시 재검증 대상이며 자동 재출발하지 않는다. 정상 MANUAL은 AUTO cap으로 막지 않는다.
legacy warm의 일반 FOLLOW/Task8에 새 NONE/route 등록을 소급 요구하지 않는다.

## 2. 자료와 소유·수명

| 형 | 책임과 수명 |
| --- | --- |
| RoadGeometryScope | dataset/map/frame·차량/설정/calibration/primitive·원 정렬/저장 문맥. 정적 기하의 불변 식별자 |
| RoadObservationOrigin / RoadDurableRef | 원 capture 구간/관측/정렬/clock과 원 COMMITTED D receipt. restore 시 age0/fresh로 바꾸지 않음 |
| StartGoal | 실제 STOP/APPLIED·stationary 원 표본에서 등록한 MAP rear-axle pose/yaw/section/lane. GT 목표 아님 |
| RoadSection / RoadConnection / RoadTopology | 관측된 공통 station·lane·연결만. 임의 닫힌 loop/GT edge를 만들지 않음 |
| RoadWindow / WorldRouteView | 정적 원 window와 현재 source/World 채택을 분리. window 최대2 section/32 station |
| ExitSpace | 정확 parking attempt/goal/slot·관측 polygon/aisle 접속. 도로 Task33를 가짜 생성하지 않음 |
| RoutePlan | Guest C가 계산하는 global occurrence/관측 edge 계획. local path SHA와 별개 |
| RouteCore | 원 route/phase 시작·E·meter epoch/scale/baseline·예산·불변 local path commitment |
| RouteProgress | 실제 Vehicle와 계산에 따른 단조 cursor/이동량/진전·STOP 대기 누계. 매 pose 등록 ACK 금지 |
| RouteAcceptanceRef / RouteSnapshot | 원 candidate SHA→Pi accepted core SHA/exact ref와 완전 progress/current window의 사본 |
| RouteOwnerContext / RouteCurrentWitness | 현재 peer/execution/gen/operation·mission(있을 때)/cancel/Scene와 원 World/Vehicle/P4·E |
| RouteEligibility / Ref | 출발 전 현재 space/target/source를 검산한 비권위 준비 보고. 미래 grant/path가 필수 아님 |
| MapperRouteWitness / MapperWorldWitness | 원 M clock/source binding/actual published World bytes. 정적 기하·현재 채택 구분 |
| RouteQuery / Candidate / Receipt | 원 query→C candidate→Pi 수락. 자신 accepted ref/current candidate SHA를 Query에 넣지 않음 |
| WindowTransitionRequest / Receipt | PREPARE/ACTIVATE/ABORT/STATUS/COMMIT_NOTICE의 원 query/source/창·Pi 예산 CAS |
| RoadDataRequest / RegistrationRequest | 원 record 조회와 실제 원 STOP/Vehicle 기반 start goal 등록 요청 |
| RoadData / RouteExchange | 위 자료를 기존 DriveLink의 유한 payload에 운반. 권한 자체가 아님 |

정적 D receipt/birth geometry가 현재 Scene·위치·occupancy의 사용권을 대신하지 않는다.
UNKNOWN은 registry 없는 상태이며 NIL epoch/revision0을 쓴다. 새 Pi UUID/rev1은 실제 등록 때
처음 발급한다. NONE/UNKNOWN snapshot이나 미래 grant를 거절 응답의 장식으로 합성하지 않는다.
RouteReceipt.snapshot(6), initial_target_context(10)은 optional이며 ACCEPTED에서만 둘 다
필수이다. 거절의 target은 금지한다. 이미 실제로 알려진 snapshot을 거절의 evidence로
운반해도 출력권한이 되지 않는다. 없는 snapshot/ref를 만드는 것보다 필드 부재가 정확하다.

## 3. 준비→권한→출력 순서

1. 기존 generic grantless input preflight는 유지한다. PARKED/CANCELLED/RETURN은 별도의
   route eligibility가 purpose/current space/target/current source/extent를 검산한다.
   기존 도로 footprint를 느슨하게 하거나 가짜 ROAD Task33/NONE으로 준비시키지 않는다.
2. Query는 현재 owner/operation/Scene/source를 고정한다. Query 원 E는 BEGIN admission까지의
   수명이다. 등록 이후 UPDATE/FINISH/window 관리에서는 불변 query id/SHA provenance로
   조회하고 현재 peer/grant/clock/witness·원 route E를 다시 검사한다. ACK로 Query/route E를 환불하지 않는다.
3. Candidate는 CandidateCore/Progress와 원 QuerySHA·current witness·원 P4/Guest 시작 시각을 낸다.
   아직 없는 acceptance를 candidate에 넣지 않는다. BEGIN의 Pi birth/meter 필드는 zero sentinel이며
   Pi가 원 clock 하한·every-accepted-encoder meter로 확정한다.
   WAIT의 phase0 PREPARE_CANDIDATE_ONLY는 원 Query E/작업 상한 안에서 기하를 먼저 준비한다.
   준비 중 실제 STOP·원 누적 meter anchor를 보존하고, 완료 뒤 현재 경로/body/Vehicle와 같은 원 Reply의
   fresh STOP 사실을 다시 검사한 다음 별도 BEGIN 사건을 만든다. 이미 생성한 birth를 새 clock에 재매핑하지 않는다.
4. Pi가 RAM/durable 정책과 원 registry CAS를 검사하고 새 grant 결박을 commit한 뒤 exact Receipt를 낸다.
   receipt 도착만으로 DRIVE하지 않는다. 실제 새 완전 State 수락·현재 grant·target APPLIED·새 계산이
   같은 route/core/owner/window/cursor/HWM에 연결되어야 출력 gate가 열린다.
5. true phase 전환만 원 Guest stamp로 phase 시작을 확정한다. samephase replan/ACK/재시작/
   window 교체·STOP 대기는 원 route/phase E·absolute meter·cursor HWM을 환불하지 않는다.
   FINISH는 terminal fact로 phase5 COMPLETED 또는 phase6 CANCELLED/FAILED이며 원 phase_revision,
   phase Pi start/E, meter/birth와 local commitment를 유지한다. 완료가 새 움직임 허가는 아니다.

Route kind3의 EXIT_SPACE 예외는 준비/State/출력/복사 모두 설치 profile+현재 active peer cap64에서만
사용한다. 공통 형식은 EXIT에 Task33가 없음을 허용하지만 current known episode/route·실제 coverage와
완전한 원 증거 검산은 runtime owner 책임이다. 지원되지 않은 active S19를 가짜 NONE으로 감추지 않는다.

등록된 EXIT_SPACE에서는 새 kind3 mission이라는 이유로 EP NONE를 발급하지 않는다. 이전 inactive
EP/accepted source는 원 provenance로 보존하며 새 mission의 현재 NONE로 재해석하지 않는다.
실제 ROAD_ROUTE 등록과 현재 grant ACK 뒤, 그 새 mission의 미초기화 EP genesis만 기존 규칙으로 한 번 만든다.
원 inactive admission 근거가 필요하며 first fullState/apply를 genesis 선행조건으로 넣지 않는다.
같은 mission의 ACTIVE/UNKNOWN·phase/window/replan/A-B 및 startup key는 이 경로로 초기화하지 않는다.

## 4. 관측 연결과 two-slot window

RoadConnection.kind1 NEXT, kind2 LANE_CHANGE만 Topology/GlobalRoute의 순방향 edge로 쓴다.
kind3 SLOT_AISLE_CONNECTOR의 from/to는 같은 실제 road section/lane/point anchor이다.
slot endpoint는 이 connector를 참조하는 ExitSpace의 정확 slot/goal/evidence roots에만 있다.
독립 kind3 record로 slot 위치/통로를 추정하거나 graph 이동을 허가하지 않는다. 소비 cache는
동일 scope, 원 section hash, lane/station 접속, 실제 관측 polygon/coverage와 부모 ExitSpace를 검산한다.

RouteOccurrence.edge_index는 다음 occurrence로 나가는 실제 outgoing edge index이다.
마지막 occurrence만 UINT32_MAX(4294967295)이고 그 외에는 실제 index0..95이다.
단일 section+edge0개인 앞쪽 목표도 마지막 sentinel 하나로 표현한다. sentinel은 실제 index0과 다르다.
마지막 실제 edge 강제·중간 sentinel·GT 미관측 접속은 거절한다.

PREPARE는 geometry data를 준비하고 ACTIVATE는 현재 fresh World와 exact pair commit한다.
두 유한 slot을 원자 교체하고 compatible rolling current grid도 갱신한다. 실패 시 ABORT와 현재 source
재검증을 사용한다. 옛 window를 새 자료/age0로 자동 복원하지 않는다. 원 window가 여전히 유효하면
명시 재검증 후에만 다시 ACTIVATE하며 원 E/예산은 유지한다.
32 station은 점 개수 상한이다. body+cusp+S19 전체 change/pass/rejoin sweep+braking extent를
담기에 부족하면 명시 WINDOW_INSUFFICIENT/unsupported/fallback/WAIT를 낸다. 무기한 seam HOLD가
해결책이 아니다. active S19 ACTIVATE 거절과 남은 extent를 함께 검사해야 한다.

## 5. 원문 hash와 API

모든 새 typed/raw hash는 `SHA256(ASCII domain + NUL + u32LE(raw length) + raw)`이다.
새 관리 subtree는 canonical known protobuf bytes만 수락한다. unknown/중복/비정규 varint·순서를
해시 원문으로 받아들인 뒤 재encode한 bytes를 원문이라고 부르지 않는다. legacy optional-unknown
호환 규칙은 새 subtree 밖에서 유지한다.
`RoadSection.point_error_m`(10), `RoadConnection.overlap_error_m`(13),
`RoadWindow.point_error_m`(12)는 명시한 packed float 배열이 canonical이다. Python과 nanopb의
원문·record SHA를 일치시키며, 해석상 동등한 unpacked 배열도 새 canonical 원문으로 수락하지 않는다.

| domain prefix `xenova.drive.route.` + suffix | 원문 |
| --- | --- |
| record.v1/1..8 | StartGoal/Topology/Section/Connection/Window/ExitSpace/Plan/GeometryScope |
| query.v1 / candidate.v1 / core.v1 / owner.v1 | 해당 canonical 메시지 |
| current-witness.v1 / receipt.v1 / eligibility.v1 | RouteCurrentWitness/RouteReceipt/RouteEligibility |
| current-permission.v1 / stop-facts.v1 | 실제 MissionGrant / DriveControlFacts canonical 원문 |
| transition-request.v1 / transition-receipt.v1 | 원 WindowTransition request/receipt |
| data-request.v1 / registration-request.v1 / world-witness.v1 | 원 관리 요청/MapperWorldWitness |
| source-key.v1 | EpisodeSourceContext 1..10의 실제 string/primitive32, 각 u32LE 길이+bytes |
| local-path.v1 | 아래의 기하 scalar stream |

`local-path.v1`은 major1, globalPlan/scope/basisWindow SHA, taskSpace/exitSHA,
referenceID(u32LE length), immutable basisRevision/prefix count+F32xy,
path count+F32(x,y,yaw,steer,v), segment count+u32first/u32last/i32direction,
mergeGoal F32pose, i32direction/u32maneuverPhase/u64maneuverRevision,
configuration/calibration(u32LE length), primitive32, map/frame/odom UUID16 순서이다.
count는 u32LE, revision은 u64LE이며 현재 World reference_revision/권한/ref/progress/calc를
포함하지 않는다. fullState validation을 hash 선행조건으로 삼아 등록 순환을 만들지 않는다.
실제 현재 reference/footprint/occupancy/차체 검사는 따로 유지한다. C/Python은 같은 stream을 쓴다.

Python 공개 함수는 `xenova_drive_world`의 `route_*_sha256`, `route_record_sha256`,
`route_source_key_sha256`, `route_local_path_sha256`이다. C는 `world/route.h`의
`dw_route_*_hash`, `dw_route_record_hash`, `dw_route_local_path_hash` 및
`dw_route_validate/decode`를 쓴다. typed 구조/hash 성공은 현재 사용권 수락이 아니다.
`road_data_body`, `route_exchange_body`, `route_candidate_body`, `road_record_body`는
엄격히 검사한 envelope의 원 nested bytes를 그대로 반환한다. C `dw_route_body`는 caller가
먼저 bounded decode/audit한 원문에서 span만 추출한다.

### 관측 Origin과 현재 승인의 분리

`RoadObservationOrigin.mapper_clock_reply_sha256`는 frozen Probe R2에 대해 JIO가 생성한 원 Reply R2 bytes의
출처 trace다. 같은 peer/source의 frame snapshot을 만들 때 함께 고정하며 뒤늦은 latest Reply로 바꾸지 않는다.
이 hash는 Pi P4/승인이나 capture 매핑 승인 원문이 아니다. capture에는 기존 FRAME/REGISTRATION의 returned
trust R1과 원 proof를 보존하고, RoadData의 현재 사용 가능성은 Gateway가 실제 승인한 현재 MapperRouteWitness로
따로 검사한다. D는 RoadRecord body와 참조한 원 reply/provenance도 실제 저장·완전 LOAD membership으로 증명해야 한다.
hash 일치·현재 World ACK나 역산한 P4로 없는 D/승인 근거를 대신하지 않는다. public tag 추가는 없다.

### 공개 home 상태의 후속 보완

`home.status`는 기존 operator.v2의 readonly `{version:2,action:"home.status"}`이며 `home-status-1`을 반환한다.
미배포 초기8필드에 `return_ready_revision`을 추가해 정확9필드로 보완했다:
supported, goal_id, goal_revision, goal_sha256, geometry_scope_sha256, registered, current_compatible,
blocked_reason, return_ready_revision. 새 필드는 nullable decimal-u63 string이다.

현재 목적2 RouteEligibilityRef의 원 revision만 사용한다. 실제 WAIT/정지·parking OFF·목표/범위 exact·현재
witness/P4/source/원 E 검사를 `return_ready()`로 통과한 경우에만 값을 노출하고, 그 외에는 null과 실제
blocked_reason을 반환한다. 상태 조회가 새 준비·Query·nonce·기한을 만들지 않는다. AUTO input-ready revision으로
대신하지 않는다. CLI `home return`은 이 값을 읽어 기존 exact5args에 넣고 실제 actor가 다시 검산한다.
준비 전 요청을 보관하지 않으며 읽기와 출발 사이 준비 상실도 거절한다. 과거8필드 source/archive는 원 자료로 보존한다.
이 계약 보완은 wire/proto/tag·기존 StatusView2·기존 fingerprint를 바꾸지 않는다.

## 6. 상한과 검증 구분

고정 protoc3.20.3/nanopb0.4.9.1의 실제 생성 `*_size`는 다음과 같다. 이는 최대로 선언된
wire 크기이며 legal semantic vector 길이나 ARM `sizeof`/pool RAM 측정값이 아니다.

| 메시지 | 실제 generated 최대 / 정책 cap(B) |
| --- | --- |
| WorldSnapshot | 31870 / 32768 |
| DriveState / AcceptedState / DriveLink | 19365 / 19445 / 19455, 각각32768 |
| MissionExecution / MissionGrant / MissionBudgetReply | 493 / 770 / 843; grant1024, reply1536 |
| ClockReply / ClockProbe | 5348 / 855; 전체 Link32768 |
| RouteSnapshot / Candidate / Receipt / Status | 1012/1024, 2210/2816, 1421/1536, 565/768 |
| RoadData / RouteExchange | 11328/16384, 2219/3072 |
| RoadWindow / RouteEligibility | 3326/4096, 1394/1536 |
| MapperClockProbe / MapperClockReply | 993/1024, 921/1024 |

State metadata의 수동 보수 합계는3316B/4096이다. 실제 최대 legal vector의 metadata 크기,
C sizeof/ARM layout·pool stack 및 실제 codec 실행은 별도 Nano 묶음에서 확인한다.
원 첫 수동안에서 nested MissionGrant 증가141B를 빠뜨린 ClockReply/Reply 합계는 생성값으로 정정했다.
반복 SHA 배열은 options에 max_size와 max_count를 모두 명시해 callback/unbounded를 제거했다.
Receipt6/10 optional 교정은 최대 wire 크기를 바꾸지 않는다.

공통 회귀 입구는 `platform/apps/drive/tests/test_route_contract.py`이며 C source는 `route_probe.c`이다.
명시 typed geometry factory는 `route_fixtures.py`이다. factory는 실제 M/D/P4/Authority를 주장하지 않으며,
제공된 실제 World/Vehicle/witness bytes를 덮어쓰지 않는다. 실제 C global planner는 별도 consumer가 계산한다.
이번 공통 단계의 host 허용 검사는 AST3.11/C-fsyntax-only와 고정 codegen이다.
codec/hash 실제 실행·권한 수락·출차 폐루프·HIL/실물 성공으로 확대하지 않는다.
