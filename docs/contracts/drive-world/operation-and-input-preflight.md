# A1 운영 진술과 AUTO 입력 preflight

B09 공통 구현 후보다. canonical 원본은 [world.proto](world.proto)다. operator/Gateway actor·C producer·Jetson 운영 API 연결이나 실차 수락 완료를 뜻하지 않는다. 기존 operator.v2/hash/StatusView2/mapper-runtime-1, kind1/2, Episode 원 등록·예산은 재해석하지 않는다.

## 위치와 지원

| 위치 | 필드 | 의미 |
|---|---|---|
|MissionExecution26 / feature32|operation_binding|사본이 실제 소비한 운영·설정 결박|
|MissionGrant32|operation_binding|현재 Pi grant 결박|
|ClockReply14|operation_status|현재 진술. 출력 권위/device APPLIED 아님|
|ClockProbe8|auto_input_preflight|현재 입력 검사. mission/path/apply/DRIVE와 독립|
|MapperClockProbe14|operation_gate|명시 Jetson STANDBY를 제한하는 Pi 사실|
|MapperClockReply20|supported_mapper_features|bit1 해석 지원, absence는 legacy|

known State mask는63(base7/Task8/Episode16/Operation32)이다. bit32는 실제 mission.operation_binding presence와 정확히 일치한다. support 광고와 State presence는 다르다. 인증 peer·설치 승인·생산/소비 지원이 함께 있어야 신규 운영을 연다. 자기 광고는 권위가 아니다. legacy warm에 새 binding/NONE/preflight를 소급 요구하지 않는다. MANUAL SID·원 input/E·keyup·CONTINUE_SESSION은 독립이다.

## 정확한 필드

다음 번호는 모두 required이며 bytes UUID/SHA는 정확16/32B다. positive counters/times는 u63, control generation은 양수 u32다.

- **OperationBinding:** 1 service_epoch bytes16(기존 Gateway instance, non-NIL), 2 operation_revision u64, 3 departure_nonce bytes16, 4 permission_revision u64, 5 parking_enabled bool. departure NIL은 현재 비활성이다. ON/OFF는 출발·새 budget이 아니다.
- **OperationStatus:** 1 major=1, 2 binding, 3 vehicle_phase u32, 4 ready_revision u64, 5 auto_ready bool, 6 blocked_reason u32, 7 valid_until_pi_us u64, 8 active_request_nonce bytes16, 9 countdown_until_pi_us u64, 10 desired_mode u32. phase1 STOPPING/2 WAIT/3 COUNTDOWN/4 APPLYING/5 ACTIVE, mode0 NONE/1 AUTO/2 MANUAL. reason0은 차단 없음,1..31은 actor 등록 공간이다. READY이면 reason0이다. COUNTDOWN은 원 request nonce·양수 end·AUTO를 요구하고 다른 phase의 countdown end는0이다. phase3 이상은 departure non-NIL이다.
- **AutoInputPreflight:** 1 major=1, 2 source(EpisodeSourceContext), 3 vehicle_sequence u64, 4 control_generation u32, 5 checked_guest_us u64, 6 valid_until_pi_us u64, 7 clock_probe_sequence u64, 8 clock_reply_sha256 bytes32. source의 역사적 형식 이름만 재사용하며 episode/등록·권한 의미는 없다. 원 source/World/Vehicle/관측 seq를 보존한다. ref는 이미 실제 P4로 확인한 원 Reply seq/SHA이며 parent Probe보다 작다. checked는 parent send보다 늦지 않는다.
- **PiOperationGate:** 1 gateway_instance bytes16, 2 control_generation u32, 3 operation_revision u64, 4 mode u32(0 SAFE_STOP/1 AUTO/2 MANUAL), 5 wait_confirmed bool, 6 manual_local_admitted bool, 7 vehicle_sequence u64, 8 stop_command_sequence u64, 9 valid_until_pi_us u64. WAIT는 원 STOP seq 양수, local MANUAL은 mode2만 가능하다. exact Vehicle lookup이 device/Pi session·calibration·sample/time을 제공한다. E는 parent Probe send 뒤여야 한다.

auto_ready는 Enter 전 현재 입력/geometry 준비다. 정상 MANUAL all-up/neutral·실STOP 뒤에는 추가 WAIT 명령 없이 새 Enter를 받을 수 있다. 미정지/미준비 요청은 즉시 거절하며 WAIT_READY 예약을 남기지 않는다. 미래 operation/grant/fullState를 preflight 조건으로 만들지 않는다. 수락 뒤에는 새 grant·actual fullState/apply·fresh calculation과 현재 권위/E를 따로 검산한다. ready revision은 source/Scene/owner/permission/phase·필요 predicate의 의미 문맥 상실에서 전진하고 정상 Vehicle/World seq마다 전진하지 않는다.

READY status는 같은 원 Probe에 preflight가 있고 그 원 E가 Pi send 뒤일 때만 가능하다. status E도 그 report E를 넘지 않는다. preflight 없음/만료 때 actor는 READY를 철회하되 numeric clock/Probe bootstrap은 계속한다.

status는 원 numeric clock E 안이며 parent Gateway와 service epoch가 일치한다. 입력 owner는 Probe를 실제 보낸 generation을 따로 기억해 제어 전환 전 Probe의 늦은 운영 status를 현재 것으로 채택하지 않는다. 숫자 clock 자체는 기존 source 수명을 유지한다. admitted current grant의 binding은 exact 일치해야 한다. fenced/terminal grant는 원 provenance을 보존하므로 현재 WAIT/설정에 맞춰 다시 쓰지 않는다. 새 status가 옛 출력 허가를 부활시키지 않는다. Pi gate의 부재/expired/허용 facts 없음은 STANDBY 허용이 아니다. 실제 matched STOP·stationary/적법한 MANUAL·현재 source/clock 검사는 owner 책임이며 gate가 운동 허가가 되지 않는다.

## 실제 C 포트

`lattice_input.h`의 다음 포트만 추가했다. public CLI/Gateway actor/lattice_app producer는 후속 담당이 연결한다.

- `dl_auto_preflight_set(const xenova_drive_AutoInputPreflight *)`: 현재 실제 검사 결과1개 보관.
- `dl_auto_preflight_clear(void)`: atomic 철회. lock/fsync/새 worker를 기다리지 않음.
- `dl_operation_status(xenova_drive_OperationStatus *, uint64_t now_pi_us)`: 현재 context/원E의 진술만 복사.

set은 실제 confirmed P4 seq/SHA와 그 실제 confirmation Guest 시각 이후의 checked, 현재 generation/Gateway, published World와 source exact 일치, 정확한 anchor의 현재 pose/Vehicle seq, 조향 측정 또는 추정+오차 presence를 검사한다. `dl_clock.confirmed_guest_us`는 원 P4 확인 분기에서만 채운 관찰 시각이며 numeric lease E를 바꾸지 않는다. 조향 provenance의 물리 수락과 도로/coverage geometry 판정은 실제 C producer 책임이다. set이 경로 탐색·mission·STATE_APPLIED·READY를 합성하지 않는다.

report E는 원 World live/source clock E, Scene E(적용 시), 원 Vehicle sample+pose_age-clock_error, 원 confirmed numeric clock Pi E, **checked Guest를 같은 원 offset lower로 옮긴 Pi 시각+100ms**의 min 이하다. 수신/재전송/새 Probe·ACK는 checked/E를 갱신하지 않는다. 같은 원 upper로 변환한 Guest expiry 뒤에는 field8을 생략하고 기존 Probe/receipt/P4를 계속한다. omission은 NONE/READY가 아니다.

동일 checked의 동일 canonical body만 idempotent다. 다른 body/E나 과거 checked는 ESTALE이다. clear 뒤 같은 checked 재사용은 거절하고 새 실제 검사가 필요하다. 실제 입력 검사가 실패하면 producer가 clear를 호출해야 한다. Gateway/generation/source identity 교체는 철회하며 정상 clock 갱신도 원 E를 연장하지 않는다. 조회·commit은 bounded/nonblocking이고 K_NO_WAIT 실패는 EBUSY/EAGAIN이다. status 부재는 ENODATA, 만료/현재 admitted grant 불일치는 ESTALE이다.

## 공유 helper와 hash

Python `operations.py`와 C `operations.inc`는 같은 구조·부모 결박을 검사한다. `operation_binding_equal`/`dw_operation_binding_equal`은 stamp 비교이며 권위가 아니다. `preflight_source_matches`/`dw_preflight_source_matches`는 검사에 쓴 World를 exact 식별한다. C mapper extension validators는 원 mapper source/clock admission 전체를 대신하지 않는다.

plan/selected-goal/core hash의 geometry 범위는 바꾸지 않았다. operation stamp는 full State 원문 SHA·mission metadata의 일부지만 같은 geometry의 plan SHA를 바꾸지 않는다. old command/E에 새 설정/generation을 붙여서는 안 된다. current actor 대조는 후속 연결에서 유지한다.

## 생성 최대와 상한

고정 protoc3.20.3/nanopb0.4.9.1의 실제 생성 header 값이다. 정책 cap·host sizeof·대상 stack은 다른 수치다.

|메시지|생성 최대 B|정책 cap B|
|---|---:|---:|
|OperationBinding|60|128|
|OperationStatus|139|256|
|AutoInputPreflight|652|768|
|PiOperationGate|78|96|
|ClockProbe|855|기존 전체 Link32768 안|
|ClockReply|4639|32768|
|MissionExecution|352|State metadata에 포함|
|MissionGrant|629|1024|
|MissionBudgetReply|702|1536|
|MapperClockProbe|993|1024|
|MapperClockReply|921|1024|
|DriveState|18208|32768|
|DriveLink|18298|32768|
|State metadata 합계|2159|4096|

기존 ClockProbe200B는 생성 최대였고 별도 정책 cap이 아니었다. 상한을 늘려 통과시키지 않았다. preflight가 원 Probe echo에 들어가 ClockReply는3779→4639B다. Pi clock owner의 peer4×원Reply32개 raw 최대 증가는128×860=110080B이며 Python object overhead/대상 ABI 측정이 아니다. 기존 input4/clock-completion8/fullcopy4·map3/odom128 개수는 유지한다. 새 C 저장은 report1/status1·원 report 비교768B×2·atomic/HWM 한 개다. 대형 Reply/fullState를 자동 지역변수로 복제하지 않는다. 실제 host sizeof·정적 명령/소스 SHA는 `build/a1-common-static`에 기록한다.

## 회귀와 남은 연결

`world/tests/test_operations_contract.py`에는 actual shared C/Python 최대 필드·원 mapper cap·feature/presence·현재 admitted grant·원 clock 순서/SHA·countdown/STOP source·legacy bootstrap absence·geometry hash/원 raw SHA 구분 회귀를 작성했다. PC 실행은 하지 않았다.

Nano 최대 벡터 실행, actual set/omit/clear, 현재 actor/C producer의 Enter→grant/fullState/apply/freshcalc, 실제 조향/RF/pose·STANDBY 수락은 남아 있다. 구현·정적 구문·소프트웨어 실행·실물 수락을 구분한다.
