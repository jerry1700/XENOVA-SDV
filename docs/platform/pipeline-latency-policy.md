# 카메라–Jetson–RPi–ESP32 지연 처리

단일 결과의 폐기, 현재 사용할 자료의 부재, 지속적인 AUTO 기능 상실, 실제 실행·장치 고장은
서로 다른 사건이다. 늦은 결과에 새 시각을 붙여 살리지 않으며, 마지막 정상 결과와 그 결과의
원래 수명이 아직 유효한지는 별도로 판단한다. 이 문서는 실제 warm lattice 주행 경로의 정책이다.

## 파이프라인 경계

| 경계 | 일시적 지연·거절 | 유지하는 것 / 실제 종료 조건 |
|---|---|---|
| OAK RGB·native depth·NN → A | 오래된 frame/pair를 버리고 다음 입력 선택 | 같은 Device·모델. 실제 stream 정체·Device 오류는 별도 health 복구 |
| A → RAW → M | 예약·후보 만료는 해당 요청 폐기, 최신 RAW/NN 후보 유지 | 원 ticket·lease·RELEASE와 메모리 소유권. 소유 모순을 정상 자료 폐기로 숨기지 않음 |
| M 계산 | frame/job 만료만으로 마지막 수락 지도·모델 초기화 안 함 | 원 세대/채택 lock, bounded phase queue와 STOP/reap. 실제 취소 불응은 별도 감시 |
| Jetson JIO → MQTT | 만료 자료 폐기, 전송 상태 불명 시 MQTT client 재연결 | 카메라·모델·지도. 부분 전송된 옛 wire를 새 결과로 재사용하지 않음 |
| MQTT → RPi World | 새 후보 decode/출판 실패 시 이미 출판한 현재 World 유지 | 원 관측 시각·source·World/pose E. 실제 만료 후 이동 근거로 사용하지 않음 |
| RPi planner | search/apply E와 owner 응답 관찰 창 분리 | 원 작업 E는 그대로. 건강 관찰은 원 E 뒤 기존 300ms 창까지 허용 |
| RPi AUTO 완료 → 명령 | 아래의 순수 deadline 결과 재시도 | 실제 STOP·장애물·출력 오류·방향 전환 의도는 유예하지 않음 |
| Gateway 준비·승계 | 단발성 거절과 지속적인 기능 상실 분리 | 기존 qualification hold 이력의 원 수명. 현재 거절 중인 B는 승계 대상 아님 |
| ESP32 → Pi encoder | 정상 중복·과거 표본만 버리고 최신 pose/meter 보존 | 원 표본 시각·순번·거리. ID 내용 충돌·세션 변경·실제 관측 공백은 별도 제한 |
| Pi → ESP32 출력 | 기존 원 명령의 100ms 이하 E와 독립 정지 유지 | 만료·STOP·실제 actuator 불명확성을 재시도 횟수로 연장하지 않음 |

## AUTO deadline 재시도

제품 프로필 `warm-lattice-development`/`warm-lattice-lab`처럼 WARM과 LATTICE를 함께 사용하는
실행 경로에 적용한다. 기존 legacy 프로필의 wire·STOP 동작은 유지한다.

1. `drive_completion_restrict`는 원래 정상 이동 목표였지만 실제 계산이 늦은 결과를
   `DEADLINE`으로 분류하고 그 결과의 출력을 0으로 제한한다. 결과 자체를 주행에 쓰지 않는다.
2. `drive_deadline_attempt_due`는 동일 context에서 처음 4개의 서로 다른 늦은 완료를 폐기하고,
   5번째부터 제한 STOP의 송신 시도를 허용한다. 성공한 새 완료가 오면 연속 실패를 초기화한다.
3. 중복 sequence는 실패 횟수를 늘리지 않는다. control revision·connection revision·lifetime·
   generation·mode·app instance가 바뀌면 옛 횟수를 새 context에 사용하지 않는다.
4. 의도한 STOP, cusp/방향 gate, HOLD, 장애물·입력·출력 오류는 이 유예 대상이 아니다.
   단순히 같은 작업이 늦었다는 이유로 구체적인 오류 사유를 덮어쓰지 않는다.

이 동작은 이전 이동 명령을 재전송하지 않는다. 새 명령이 없으면 기존 100ms 이하의 원 E에서
Gateway output owner와 MCU cutoff/watchdog가 정지해야 한다. 5회를 채우기 전에도 원 E가 먼저
끝나면 정지한다. 5회는 초기 운영 정책이며 실제 주기·부하·제동 거리를 측정해 검토한다.

## Gateway의 실패 관찰과 qualification

`warm-drive-1`은 `DEADLINE`/`CALCULATION_FAILED`와 일시적인 입력·World·pose·clock 부재를
최대 5개의 서로 다른 실패 완료, 또는 원 300ms 관찰 창 중 먼저 도달하는 조건으로 확인한다.
실패 완료는 EXECUTING의 새 `progress_seq`로 센다. 같은 진행 순번의 반복 `check_seq`·heartbeat는
실패 횟수나 관찰 창을 늘리지 않는다. 보고 간격 때문에 펌웨어의 완료 개수와 Gateway가 관측한
실패 개수는 같다고 가정하지 않는다.

단발성 실패 동안 마지막 qualification의 원 timestamp와 hold 시작 이력을 보존할 수 있다.
현재 `NOT_READY`는 그대로 기록하며 새 READY·명령·관측 TTL을 만들지 않는다. 현재 거절 중인
B를 승계하거나 거절된 계산의 명령을 ForwardDrive로 보내는 근거로 사용하지 않는다.
새 실제 완료의 READY 또는 새 유효 QUALIFIED로 복귀하면 원 lease 안에서는 hold를 처음부터
다시 쌓지 않는다. 같은 progress의 READY 재전달로 실패를 지우지는 않는다.

AUTO_READY와 resume countdown도 단발성 입력 공백만으로 즉시 취소하지 않는다. 다만 countdown이
끝나더라도 현재 qualification이 없으면 AUTO 전환을 실행하지 않는다. 재개 요청의 원 세대·
입력 세션·권한·실제 적용 검사는 유지한다. binding/실행 identity 손실, STOPPING/RESIDUAL,
구조적 입력 오류와 명시적인 state/configuration/source 불일치는 이 유예로 숨기지 않는다.

두 AUTO의 지속적인 공통 입력 상실은 기존 수동 준비·명시 재개 정책으로 처리한다. 한 번의
거절 때문에 A/B를 왕복시키거나 VM 전체를 UNTRUSTED로 바꾸지 않는다. 구 FW의 누적 5회
AUTO deadline 보고도 AUTO 기능 범위로만 해석한다.

## planner와 완료 관찰

tracker의 직전 정상 완료는 다음 작업이 실행 중이라는 이유나 다음 작업의 deadline 위반만으로
폐기하지 않는다. 직전 완료의 원 300ms 창, context와 현재 명령·입력 E를 사용한다.

planner의 search/apply E는 계산 취소·결과 수락에 쓰는 원 기한이다. planner owner가 응답하지
않는다는 판정에는 그 원 E 뒤의 300ms 관찰 창을 둔다. 반복 BUSY나 tracker callback으로 이
창을 재발급하지 않는다. planner health가 유지돼도 실제 tracker는 원 경로·현재 World·pose·
collision·mission·자기 deadline 검사를 통과해야 한다. 이 창은 새 계획이나 경로 사용권이 아니다.

## encoder 표본과 신뢰 상실

같은 Pi/device session의 정상 표본이 이전에 받은 sensor payload와 완전히 같거나, 표본 번호와
원 장치 시각이 모두 최신 표본보다 오래되면 해당 packet만 거절한다. 최신 pose·odom epoch·
absolute meter·history는 보존하며 원 Pi 관측 시각을 새 수신 시각으로 바꾸지 않는다.

최신 표본과 같은 ID에 다른 내용, 번호와 시각의 모순, 세션·센서 품질·encoder 연속성 문제는 기존
신뢰 제한 경로를 유지한다. 실제로 빠진 encoder 이동을 0으로 채우지 않으며, absolute meter의
연속 source_sample 조건과 주차 정지·조향의 실제 관측 요구도 그대로다.

## 적용·검증 한계

소스 수준 정책이며 보드에 배포됐다는 뜻은 아니다. `vehicle/ESP32`에는 현재 `.gitkeep`만 있어
MCU 구현의 watchdog/cutoff·ACK cache·실제 brake 동작을 이 저장소에서 검증할 수 없다.
Pi의 encoder/ACK 처리와 계약·virtual peer 시험은 MCU 실물 수락과 구분한다.
설치 프로필의 20ms 제어 주기, World 300ms, pose 200ms, 원 명령 100ms 한도는 변경하지 않았다.
