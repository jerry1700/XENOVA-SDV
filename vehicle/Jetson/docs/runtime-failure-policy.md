# Jetson 결과 수명과 고장 처리

이 문서는 결과를 사용할 수 있는 시간과 장치·프로세스·자원 소유의 고장을 구분한다. Jetson은 최신 관측을 받아 인지·지도를 갱신하고 계속 전달한다. RPi의 현재 입력 검사, 출력 권한, 원 명령 만료와 독립 정지는 유지한다. 상세설계 정본은 `C:\Users\SSAFY\Documents\Jetson-drive-docs\result`의 03·09·10·11·13·14·21장과 `runtime-lifecycle-policy.json`이다.

## 사건별 책임

| 사건 | 처리 | 유지하는 것 | worker 복구 횟수 |
|---|---|---|---|
| 올바른 frame·계산 결과의 사용 기한 만료 | 그 작업·결과만 폐기하고 다음 최신 입력 처리 | 모델·worker·지도 | 소비하지 않음 |
| 일시적인 RGB/depth/NN 입력 부재·준비 근거 부족 | 필요한 결과 발행을 보류하고 WAIT | 같은 모델·worker·기존 지도 | 소비하지 않음 |
| 단일 clock 표본·설정 전달 거래의 지연 | 표본 거절·새 nonce 측정 또는 새 로컬 전달 거래 | 원 관측 E·모델·설치 사실 | 소비하지 않음 |
| 실제 PC 가상 관측 연결 단절 | PC source/World 사용을 제한하고 DEGRADED | 모델·worker·지도/DB | 자료 단절만으로 소비하지 않음 |
| Device 오류·A의 실제 stream 장기 정체 | 실제 owner 정리 후 A/M 범위 복구 | S·D·Pi 독립 보호 | 실제 복구를 시작할 때 소비 |
| 실제 worker 종료·제어 무응답·계산 취소 불응 | 해당 worker와 실제 자원 소유 범위 수습 | 관계없는 모델/worker·DB | 실제 복구를 시작할 때 소비 |
| 명시 설정 불일치·native post 결과 불명·자원 소유 모순 | 원인과 영향을 구분해 보고하고 불확실한 자원 사용 금지 | 확인된 다른 자원과 보호 경로 | 정상 frame 폐기로 숨기지 않음 |

`FrameExpired`와 `ReservationExpired`는 정상 자료/예약 폐기다. `ConfigurationError`는 명시 설정 오류다. 예외가 `ValueError`라는 이유나 현재 worker 상태만으로 고장 종류를 추정하지 않는다. 실제 PC 단절 뒤 자동 hot reattach나 PC worker 부분복구를 지원한다는 뜻은 아니다.

## 네 종류의 시간

1. **관측·주행 결과의 수명:** 원 capture/입력 E를 지킨다. 초기 live frame 창은 500ms이며 지연된 결과에 새 시각을 붙이지 않는다. RPi 명령 E를 worker 고장 감시값으로 연장하지 않는다.
2. **로컬 거래·설치/완료 보고의 전달 수명:** 새 거래는 새 request/nonce를 쓴다. 이미 설치한 consumer, CONFIG 적용, 동일 DB/checkpoint 검증의 사실은 현재 peer·설정·digest와 원 완료 시각을 대조한다. 전달 지연과 주행 자격은 별개다.
3. **실제 local startup:** cold startup은 단계별 중복 cap 없이 총 90초다. M/D의 local setup→HELLO도 총 90초이며 외부 입력 대기와 구분한다. 별도 벤치 단계 수치는 진단용이다.
4. **실제 고장 감시:** S control 5초, A가 직접 관측하는 실제 RGB/depth stream 정체 15초, S의 compute 취소 불응 15초가 초기값이다. S의 capture-pair·NN·parser 중복 고장 타이머는 두지 않는다.

고장 감시값은 운영 벤치 초깃값이며 Nano 부하 실측으로 조정한다. `XENOVA_JETSON_CONTROL_STALL_S`, `XENOVA_JETSON_CAPTURE_STALL_S`, `XENOVA_JETSON_COMPUTE_STALL_S`는 process 기동 때 한 번 읽고 유한한 양수·signed64 microseconds 범위를 검사한다. 이 값은 처리율 보증이나 RPi 출력 유예가 아니다.

capture 30쌍과 REQUIRED NN의 fresh 정상 parser 결과 5개 확인은 유지한다. 최초 15초 관찰 창에 근거가 부족하면 같은 모델에서 WAIT하며 나중의 fresh 자료로 검사를 계속한다. 빈 detection은 정상 결과이고, 오래된 결과는 준비 개수에 세지 않는다. 최초 형식·대응관계 검사가 현재 World·위치·점유의 신선도 검사를 대신하지 않는다.

## native 예약과 실제 메모리 반환

RAW write/read의 기본 5초는 native 취득을 시작할 수 있는 별도 허용시간이다. 명시한 HIL 벤치의 150ms 설정도 안전한 예약 취소·실제 반환 규칙을 적용한다.

- **취득 전:** `CANCEL_REQUEST`의 원 request ID·packet SHA256·raw binding SHA256을 현재 예약과 대조한다. 같은 ordered channel에서 취소의 full-send를 다음 요청보다 먼저 마친다. 아직 취득하지 않은 WRITING 예약은 FREE, 아직 읽지 않은 IN_USE 예약은 READY로 되돌릴 수 있다. semaphore를 추가 취득하거나 post하지 않는다.
- **취득 후:** 유효 grant E 안에 취득을 시작했으면 정상 copy/header/CRC/post를 완결한다. 늦은 `FILLED`의 header는 별도 cleanup 전달 TTL을 쓸 수 있으나 원 frame/payload E는 유지한다. 읽기는 실제 view를 모두 닫고 writable을 정확히 한 번 반환한 뒤 exact `RELEASE`를 보낸다.
- **S ledger:** TTL만으로 실제 WRITING/IN_USE를 회수하지 않는다. exact full key의 늦은 완료/반환 또는 확인된 owner 실종료로 정리한다. cleanup 수락이 오래된 payload를 새 관측으로 만들지는 않는다. 실제 native 소유·post 결과가 불명하면 실제 고장으로 남긴다.

## 역사 사실과 현재 주행 근거

동일 DB UUID·selected checkpoint·mapper incarnation에 대한 검증 완료는 역사 사실이다. DB·checkpoint·mapper가 바뀌면 호환성을 다시 확인한다. `OPERATION_RESULT`의 body는 원 job/request ID·issued/E·completed·digest를 보존하고 header의 로컬 전달 TTL과 구분한다. 원 작업이 제때 실제 완료했는지 확인하며 새 전달 거래가 실패한 원 작업을 성공으로 바꾸지 않는다.

physical M의 bootstrap LOAD에는 옛 cold/local setup 90초를 다시 적용하지 않는다. 외부 capture/clock/odom 대기가 길어진 뒤에도 새 유효 LOAD를 진행한다. 각 실제 DB job 5초, 선택 checkpoint의 유한 page 수·길이·digest·내용 검증과 실제 control/compute 고장 감시는 유지한다.

STATIC `PC_NN_START`/`PC_SOURCE_STATE`도 실제 모델 start의 cold 90초 근거와 현재 binding을 보존해 전달한다. 전달이 늦었다는 이유만으로 모델을 다시 올리지 않는다. 이러한 설치·복원 사실은 현재 FIELD/WORLD/Scene의 E, 위치·장애물·점유, 출력 권한을 대체하지 않는다.

## RPi 복구와의 경계

단일 늦은 결과는 그 결과의 사용을 막는다. 확인된 반복 AUTO 기능 상실과 실제 process/binding 손실은 별도 복구 판단이다. 기존 readiness lease 300ms 안에서 마지막 실제 READY 계산/최초 거절 중 이른 시각을 기준으로 회복을 관찰하며, 새 거절·heartbeat가 이 창을 연장하지 않는다. 원 출력 수명은 마지막 실제 ForwardDrive의 원 수신시각/원 E로 계산하며 거절된 새 명령 수신이 연장하지 않는다. 임의의 연속 실패 횟수를 새로 만들지 않는다.

READY와 실제 계산은 정상인데 command 전달만 비어 있는 경우에도 원 100ms 출력 만료와 300ms 기능 확인을 구분한다. 최초 command가 없는 경우와 정상 WAIT 뒤 복귀를 포함해 현재 연속 READY 구간의 고정 활성화 근거와 마지막 유효 ForwardDrive 중 늦은 시각을 사용한다. 반복 READY/progress가 이 창을 매번 다시 시작하지 않는다. 확인을 기다리는 동안 원 출력이 만료되면 STOP한다.

정상적인 no-path·검색 미발견·계획 예산 소진, `STATE_PREPARING`/`CONTEXT_CHANGED`와 네 `STATE_*`는 실제 작업 진전이 유지되는 동안 정지·AUTO 계획 대기이며 VM 전체 고장이 아니다. 상태 불일치의 적용 증거 철회는 즉시 수행한다. supervision만 새롭고 실제 callback이 기존 lease를 넘게 정체하면 별도 기능 상실로 처리한다.

별도 planner owner의 검색/apply 건강은 외곽 tracker callback의 progress로 갱신하지 않는다. 진행 중 snapshot은 현재 profile의 원 검색 예산 2초를 그대로 쓰며 추가 300ms를 더하지 않는다. 이미 실제 완료한 owner의 근거를 관측하는 기존 300ms 창과 구분한다. 반복 BUSY·slice·감독 표본이 원 budget을 연장하지 않고, 원 job E 뒤에도 owner가 완료/종결하지 못하면 기존 `CALCULATION_FAILED`로 알린다. Gateway의 300ms 기능 회복 관찰은 별도로 적용한다. 정상 no-path 완료는 살아 있는 작업 결과다. 이 관찰은 원 검색 결과 E나 명령 권한을 늘리지 않는다.

현재 binding/app 손실과 명시 `STOPPING`/`RESIDUAL` phase는 위 계산/정상 WAIT 유예보다 우선한다. reason이 정상 WAIT나 입력 장애에 속하더라도 실제 실행·소유 상실을 숨기지 않는다. 이 보완은 새 wire field를 도입하지 않는다.

기능 상실 때는 준비된 B가 현재 입력과 유효 path/state를 재검증해 자기 명령을 계산한다. 불가능하면 독립 안전정지 후 기존 MANUAL 경로로 전환한다. 과거 command의 권한·E를 새 세대로 재포장하지 않는다. Jetson 공통 입력 장애로 A/B를 반복 교체하지 않는다. 적용된 상태 증거는 실제 상태/좌표계/설정/참조 불일치 때 철회하며 단순 계산 지연만으로 삭제하지 않는다.

기존 [복구 시나리오](../../../docs/platform/recovery-scenarios.md)와 [주행 설계](../../../docs/platform/drive-pipeline-architecture.md)의 warm 승계·현재 재검증·A 복구 후 Standby 합류를 따른다. 문서 참조 모델의 PASS와 제품 코드 회귀·실보드 연속 주행 수락은 각각 구분해 기록한다.
