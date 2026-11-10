# Jetson 인지 결과방향 장애 제어 v1

이 문서는 **신규 F02 계약**이다. 기존 VM `agent.command`/`auto_delay`의 지원 범위를 바꾸지 않는다. Backend가 인증된 기존 broker를 통해 Jetson의 World 발행에만 DELAY/BLOCK을 요청한다. Front/Spring 구현·broker ACL 적용·실장치 왕복 수락은 별도 작업이다.

`world_fault_control.py`는 순수 controller이며 네트워크를 열지 않는다. Main이 실제 S-jio owner/lock과 publisher에 연결한다. UI/HTTP 접수, MQTT 전달, 필터 설정 적용, 실제 World 폐기, Pi 입력 만료/정지, 차량 APPLIED는 각각 다른 사실이다.

## 1. Topic·권한·봉투

`{device}`는 실제 설치된 Mapper device와 같은 `[a-z0-9][a-z0-9_-]{0,31}`이다.

| 방향 | 신규 topic | type | QoS / retain |
| --- | --- | --- | --- |
| Backend → Jetson | `xenova/v1/mapper/{device}/fault/command.json` | `perception.fault.command` | 1 / false |
| Backend → Jetson | `xenova/v1/mapper/{device}/fault/query.json` | `perception.fault.query` | 1 / false |
| Jetson → Backend | `xenova/v1/mapper/{device}/fault/result.json` | `perception.fault.result` | 0 / false |
| Jetson → Backend | `xenova/v1/mapper/{device}/fault/status.json` | `perception.fault.status` | 0 / false |

기존 공통 봉투의 `schema_version/message_id/source_id/boot_id/agent_instance_id/type/seq/sent_at/payload` 9필드를 사용한다. schema_version은 `1.0`, 요청 source_id는 `dom0-server`, 응답은 `jetson`이다. boot_id는 **발행 측 실제 OS boot**, agent_instance_id는 발행 프로세스 수명이다. 대상 Jetson S 서비스 UUID는 payload의 service_instance다. sender/target 식별자를 바꾸어 쓰지 않는다. UUID는 canonical lowercase·nonzero, seq는 정수1..2^63−1이다. 선택 필드나 알 수 없는 키를 허용하지 않는다.

sent_at은 비어 있지 않은 RFC3339 UTC `Z` 문자열이다. 입력은 소수0..9자리(Java Instant 포함), 출력은 UTC 밀리초3자리다. 시각 형식만 검사하며 이 값을 접수/World 권한의 기한으로 쓰지 않는다. receiver는 별도 단조 window로 접수하므로 UTC 역행·오차가 window나 적용 기간을 늘리지 않는다. 원 UTC bytes는 지문에 포함되며 재직렬화/재타임스탬프하지 않는다.

일반 `envelope.validate`와 봉투가 호환된다는 사실은 이 신규 topic의 ACL이나 payload 지원을 뜻하지 않는다. F02는 재전달을 원 요청 키/raw bytes로 판정하므로 명령 앞에서 공통 SeqTracker로 중복을 먼저 버리면 안 된다. Backend의 현재 상태 projection은 같은 boot/service의 원 status seq/revision을 사용해 옛 보고가 현재를 덮지 않게 한다.

실제 인증·ACL은 기존 broker owner가 별도로 적용한다. source_id 문자열이나 window는 인증 자격증명이 아니다. Backend만 command/query 쓰기, Jetson만 result/status 쓰기를 허용해야 한다. 브라우저 직접 MQTT·retained 명령·VM topic 위장·장치 전역 wildcard 제어는 지원하지 않는다. 결과 유실은 UNKNOWN이며 Q0를 차량 적용 ACK로 취급하지 않는다.

## 2. Receiver가 발급하는 원 2초 window

Backend가 STATUS를 조회하면 receiver가 `window_id` UUID와 진단용 window_remaining_ms를 보낸다. 이 UUID는 현재 S 서비스와 내부 source_key(lowerhex64 또는 None)에 결박된다. 원 monotonic deadline은 receiver만 보관한다. Backend는 J 시각이나 expires_at을 제조하지 않고 UUID를 그대로 echo한다.

- 살아 있는 window를 다시 조회해도 UUID/deadline을 갱신하지 않는다. 정확히 원 E에 도달하면 접수가 만료되고, 이후 조회에만 새 window를 발급한다.
- 실제 source 교체는 이전 window를 무효화한다. **이미 적용된 장애 설정은 같은 S의 관리 상태**이므로 mode/원 duration/deadline/revision을 유지한다. 이는 옛 명령을 새 source에 재수락하는 것이 아니다.
- Main이 source 교체 시 World 두 칸을 별도 discard한다. controller.set_source에서 PASS callback을 호출하지 않는다.
- source None에서도 window/조회/현재 원 주입 CLEAR는 가능하다. 새 SET은 SOURCE_UNAVAILABLE이다.
- 새 S 프로세스는 새 service_instance와 새 window·빈 이력을 갖는다. 옛 서비스의 신규 요청은 SERVICE_MISMATCH이며 원 설정을 복원하거나 자동 재주입하지 않는다.

## 3. 요청 payload의 정확한 모양

| action | payload 필드 — 정확히 이 키만 |
| --- | --- |
| SET | command_id, service_instance, window_id, action, mode, delay_ms, duration_s |
| CLEAR | command_id, service_instance, window_id, action, active_command_id, expected_revision |
| STATUS(query topic) | action |
| RESULT(query topic) | action, command_id |

SET mode는 DELAY/BLOCK만 가능하다. DELAY delay_ms는 정수1..2000, BLOCK은 정확히0이다. duration_s는 정수1..120이다. bool/float는 정수가 아니다. PASS SET은 거절하고, PASS 복귀는 원 주입에 대한 CLEAR/기간 종료만 수행한다. SET이 이미 active이면 새 SET은 BUSY다. 설정을 바꾸려면 원 주입을 명시 CLEAR한 뒤 새 요청 키로 SET한다.

CLEAR는 현재 active_command_id와 expected_revision(0..2^63−1의 canonical decimal string)이 정확히 맞아야 한다. 늦은 옛 CLEAR가 다음 주입을 해제하지 않는다. 모든 command_id는 원 논리 요청의 UUID다.

아래 UUID/UTC/seq는 **형식이 유효한 예시 값**이며 현재 장치 값이 아니다. Backend는 발행 전 sender의 실제 boot/process, 현재 status의 service/window/revision·active key로 치환한다. JSON은 compact ASCII로 직렬화하고 원 bytes를 보관한다. 같은 논리 요청을 재전송할 때 message_id·sent_at·공백까지 새로 만들지 않는다.

### STATUS 조회 — query.json, QoS1, retain=false

```json
{"schema_version":"1.0","message_id":"11111111-1111-4111-8111-111111111111","source_id":"dom0-server","boot_id":"22222222-2222-4222-8222-222222222222","agent_instance_id":"33333333-3333-4333-8333-333333333333","type":"perception.fault.query","seq":1,"sent_at":"2026-11-09T09:00:00.000Z","payload":{"action":"STATUS"}}
```

응답 status payload 예시(바깥 봉투는 같은9필드, source_id=jetson, 실제 Jetson boot/S service):

```json
{"service_instance":"66666666-6666-4666-8666-666666666666","source_bound":true,"revision":"0","active_command_id":null,"mode":"PASS","applied_known":true,"delay_ms":0,"remaining_ms":0,"window_id":"77777777-7777-4777-8777-777777777777","window_remaining_ms":2000,"observed_us":"1000000","cache_size":0,"cache_limit":128,"recovery_size":0,"recovery_limit":8}
```

### DELAY 설정 — command.json, QoS1, retain=false

```json
{"schema_version":"1.0","message_id":"44444444-4444-4444-8444-444444444444","source_id":"dom0-server","boot_id":"22222222-2222-4222-8222-222222222222","agent_instance_id":"33333333-3333-4333-8333-333333333333","type":"perception.fault.command","seq":2,"sent_at":"2026-11-09T09:00:00.100Z","payload":{"command_id":"55555555-5555-4555-8555-555555555555","service_instance":"66666666-6666-4666-8666-666666666666","window_id":"77777777-7777-4777-8777-777777777777","action":"SET","mode":"DELAY","delay_ms":100,"duration_s":5}}
```

BLOCK은 새 요청 키·message_id로 mode=BLOCK/delay_ms=0을 사용한다. 필터 적용 callback이 실제로 성공한 뒤의 result payload 예시:

```json
{"command_id":"55555555-5555-4555-8555-555555555555","request_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"APPLIED","reason":null,"query_id":null,"service_instance":"66666666-6666-4666-8666-666666666666","revision":"1","active_command_id":"55555555-5555-4555-8555-555555555555","mode":"DELAY","applied_known":true,"delay_ms":100,"remaining_ms":5000,"observed_us":"1100000"}
```

예시 SHA는 형식용이다. 실제 값은 receiver가 수신한 원 요청 bytes의 SHA256이다. APPLIED는 **필터 설정 반영만** 뜻하며 World의 실제 지연량·Pi 만료·STOP·차량 APPLIED 증거가 아니다. 콜백 전의 접수는 APPLIED로 발행하지 않는다.

### 결과 유실 시 원 키 조회 — query.json

```json
{"schema_version":"1.0","message_id":"88888888-8888-4888-8888-888888888888","source_id":"dom0-server","boot_id":"22222222-2222-4222-8222-222222222222","agent_instance_id":"33333333-3333-4333-8333-333333333333","type":"perception.fault.query","seq":3,"sent_at":"2026-11-09T09:00:00.200Z","payload":{"action":"RESULT","command_id":"55555555-5555-4555-8555-555555555555"}}
```

있으면 저장한 result 원 bytes를 재발행한다. 없으면 UNKNOWN/NOT_FOUND다. **캐시된 APPLIED는 과거 이력**이며 현재 active 여부·남은 기간은 별도 STATUS로 확인한다. Q0 결과 유실을 새 키 SET의 자동 재주입으로 복구하지 않는다.

### 현재 원 주입 CLEAR — command.json

```json
{"schema_version":"1.0","message_id":"99999999-9999-4999-8999-999999999999","source_id":"dom0-server","boot_id":"22222222-2222-4222-8222-222222222222","agent_instance_id":"33333333-3333-4333-8333-333333333333","type":"perception.fault.command","seq":4,"sent_at":"2026-11-09T09:00:00.300Z","payload":{"command_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","service_instance":"66666666-6666-4666-8666-666666666666","window_id":"77777777-7777-4777-8777-777777777777","action":"CLEAR","active_command_id":"55555555-5555-4555-8555-555555555555","expected_revision":"1"}}
```

실제 CLEAR 직전에 STATUS로 살아 있는 window/current revision을 읽는다. clear 성공은 mode=PASS, active_command_id=null, remaining_ms=0, revision+1의 APPLIED다. callback은 두 World 칸을 폐기하며 stale flush·출력권한 갱신·AUTO 출발을 하지 않는다.

## 4. 이력·거절·실패·유한 메모리

- 원 key와 **전체 raw bytes**가 같으면 저장한 result 객체/bytes를 그대로 반환한다. 같은 key의 다른 bytes는 IDEMPOTENCY_CONFLICT다. 원 기간은 최초 SET callback 시각+duration_s에 한 번 고정한다. query·QoS 재전달·재연결은 configure나 deadline을 다시 실행하지 않는다.
- 정상 형식의 거절도 일반128행에 여유가 있으면 terminal 기록한다. 형식 파괴·중복 JSON 키·비ASCII/비유한 값·잘못된 topic/QoS·retained·식별 불가 입력은 응답 없이 폐기한다. 식별 가능한 잘못된 action/parameter는 REJECTED다.
- 일반 요청 기록128행은 service 동안 eviction하지 않는다. 초과 SET은 CACHE_FULL이며 기존 원 주입은 그대로다. STATUS/RESULT query는 이128행을 소모하지 않는다.
- 성공 CLEAR는 해당 SET당 최대1개이므로 별도128행까지 보관한다. 따라서 일반128행 포화가 정상 현재 CLEAR를 막지 않는다.
- callback 실패 CLEAR는 별도의 **8개 복구 예약**에 원 UNKNOWN 결과를 보관한다. 같은 raw 재전송은 다시 적용하지 않고, **새 명시 CLEAR key**만 재시도한다. 작은 고정 예약은 일시 실패 복구를 허용하면서 지속 실패가 무한 이력/재시도를 만들지 않게 한다.
- 복구8행까지 소진되면 현재 CLEAR는 UNKNOWN/RECOVERY_CACHE_FULL, 새 SET은 거절한다. 사용수/상한을 status에 공개하며 명시 owner 복구가 필요하다. 이 경우도 무한 메모리·자동 반복을 만들지 않는다.
- 총 기록 상한은128+128+8=264다. 요청/결과 raw 각각1024B이므로 저장 raw 합계는 최대540672B이며 Python 객체 overhead/실측 RSS와 구분한다. 별도 thread·sleep·daemon·무한 queue는 없다.

주요 reason은 SERVICE_MISMATCH, WINDOW_EXPIRED_OR_MISMATCH, SOURCE_UNAVAILABLE, BUSY, ACTIVE_MISMATCH, CACHE_FULL, RECOVERY_CACHE_FULL, IDEMPOTENCY_CONFLICT, INVALID_PARAMETERS, INVALID_ACTION이다. callback 실패는 UNKNOWN/APPLY_FAILED이고 applied_known=false다.

원 적용 기간 종료는 PASS callback 성공 뒤 ENDED/DURATION_ELAPSED다. 종료 callback이 실패하면 **한 번만** UNKNOWN/EXPIRY_APPLY_FAILED를 만들고 원 active key/revision·deadline을 유지한다. 이후 tick/status의2ms loop에서 재시도하거나 가짜 PASS를 발행하지 않는다. 새 현재 window·원 active key/revision으로 보내는 명시 CLEAR만 복구할 수 있다. SystemExit/KeyboardInterrupt는 일반 Exception으로 삼키지 않는다.

## 5. Main 연결 API와 경계

```python
controller = WorldFaultController(device, service_instance, apply_callback,
                                 boot_id=actual_jetson_boot_id)
# 선택형 utc_clock()은 aware UTC datetime을 반환한다. 진단·시험 주입용이며 권한 시계가 아니다.
controller.set_source(actual_lowerhex64_source_key_or_none, now_us=locked_now)
publication = controller.handle(topic, original_raw, qos=qos, retained=retained,
                                now_us=locked_now)
publication = controller.tick(now_us=locked_now)
publication = controller.status(now_us=locked_now)
```

Publication은 frozen `topic/raw`뿐이다. handle/tick/set_source는 publication 또는 None, status는 publication을 반환한다. owner가 같은 lock 안에서 원 monotonic 시각을 샘플하고 전달한다. callback은 같은 lock의 `_set_world_fault_locked(mode, delay_us=..., now_us=...)`로 바로 연결하며 locking wrapper를 재호출하지 않는다. 초기 controller는 새 owner의 PASS core에 연결한다.

source 교체 때 Main의 별도 discard hook, 최종 `_claim_world`, 운영 gate AND·generation/HWM, 원 World bytes/capture/seq/E·source/Scene/clock·권한 검사는 그대로 유지한다. callback 실패를 독립 clock/Vehicle owner의 종료로 확대하지 않는다. status/query는 원 입력·운영 모드·MANUAL SID를 바꾸지 않는다.

Main은 결과/상태를 기존 Q0 단일 write slot에 유한하게 두고 clock 우선순위를 유지한다. 이 controller에 broker I/O·blocking wait·새 publisher thread를 넣지 않는다. slot 교체/유실은 Backend에서 UNKNOWN→query/동일 raw 재발행으로 관측한다. UI refresh가 새 SET을 만들지 않게 한다.

## 6. 크기·검증·인계 상태

모든 수신/출력 raw는1024B 이하이며 알 수 없는 필드가 없는 닫힌 형식이다. device32자·UUID36자·u63 최대 seq/revision·최대 파라미터·최대 문자열/NULL 조합을 집중시험으로 대조한다. 최장 command topic은68B다. payload1024B를 끝까지 채운 QoS1 PUBLISH의 remaining length도 `2+68+2+1024=1096B`로 기존1280B socket cap 안이다. pretty-print 공백도1024B에 포함하며 수신 제한을 늘리지 않는다.

| 닫힌 필드 조합의 보수 최대 벡터 | compact ASCII bytes |
| --- | ---: |
| SET / CLEAR / RESULT query 요청(UTC 소수9자리·seq u63) | 549 / 604 / 393 |
| result(최대 reason·query ID·원 SHA·미확정 적용·남은120초 조합) | 818 |
| status(source 없음·미확정 적용·128행·복구8·u63·남은120초/window2초) | 745 |

이는 필드별 상한을 함께 채운 직렬화 벡터이며 실장치 상태의 수락 증거가 아니다. 실제 raw1024B까지의 공백도 별도로 허용/검사한다. 공통 봉투의 실제 topics.py/envelope.py는 시험 전용 namespace loader로 읽어 Paho나 package init을 실행하지 않고 request/result/status와 문서의 네 요청 예제를 검증한다.

검증 명령(저장소 루트, 기존 Python 환경):

```powershell
$env:PYTHONPATH = (Resolve-Path vehicle/Jetson/src).Path
python -m unittest discover -s vehicle/Jetson/tests/unit -p test_world_fault_control.py -v
python -m unittest discover -s vehicle/Jetson/tests/unit -p test_world_egress_fault.py -v
```

이 단위는 순수 controller/core와 공통 봉투 호환을 검사한다. 실제 broker 인증/ACL, Backend/API·화면 연동, JIO 실 publisher·World 결과방향 필터, Pi 현재성 만료/STOP·MANUAL/clock/Vehicle 생존, 해제 후 명시 재출발의 통합 수락을 대신하지 않는다. BE/FE 담당은 공개 contract 확정·실제 Platform 왕복 근거 뒤 기존 부하 UI에 연결한다.
