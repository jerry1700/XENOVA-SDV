# Gateway–ESP32 feedback-3 계약

상태: **명시 선택 신규 연결 목표 / Pi 구현·고정 vector 교차 확인 / MCU·실물 수락 전**. 구형 write-only/feedback-1/feedback-2는 명시 호환 선택일 때만 사용하며 원문·vector를 보존한다. 자동 승격·downgrade는 없다.

기본 제어 설계의 원천은 Jetson-drive-docs/result/appendix-b-control-protocol.md §8–10이다. ToF는 2026-11-10 사용자 요구에 따라 **앞0·뒤1 두 센서의 단일존1×1, 관측당 한 패킷, 근거리 HIT-only**로 개정했다. 이 문서의 현재 ToF 계약이 구 §13.2의8×8 조각 설계를 대체한다. 기본 제어와 선택형 ToF의 software 소비·J 전달 경계는 MCU producer·실측 보정·물리 수락과 별개다. 이 문서·vector는 firmware 또는 물리 수락을 대신하지 않는다.

장치 담당 제공물과 수락 순서는 [ESP32 요청서](../requests/20261029-request-esp32.md), 기존 명시 호환은 [feedback-1/2 계약](gateway-provider-v1.md#g3-명시-선택-serial-feedback-1), 고정 bytes는 [feedback-3 vector](vectors/gateway-feedback-3.json), 실제 codec 대조 코드는 [시험](../../platform/services/gateway/tests/test_feedback3_vectors.py)을 따른다.

## 1. 책임과 해석

Pi Gateway의 단일 `OutputOwner`가 TX/RX·프레이밍·명령 원 기한·ACK를 소유한다. ESP32는 승인된 명령을 모터/서보에 적용하고, 독립 하드웨어 timer/watchdog로 기한을 지키며 실제 센서 표본을 생산한다. VM 선택, AUTO/수동 권한, 경로·회피·주차 알고리즘, Mission/Episode/Operation/Scene/State/ControlFacts protobuf는 Pi 내부 책임이다. 그 필드를 ESP serial packet에 추가하지 않는다.

표의 검산은 현 Pi consumer/producer에서 확인한 범위다. MCU 요구사항과 아직 정의되지 않은 동작은 별도로 적었다. enum·hash domain·물리 기본값을 새로 발명하지 않는다. wire의 `generation/counter`는 Pi가 부여한 원 값을 echo하며 MCU가 VM 역할을 판단하는 키가 아니다.

## 2. 공통 프레임

`COBS([id:u16LE][payload_length:u8][payload][CRC:u16LE]) + 0x00`.

- CRC-16/CCITT-FALSE: poly `0x1021`, init `0xffff`, refin/refout false, xorout `0`; 대상은 id+length+payload. 독립 stdlib 생성기에서 `123456789 → 0x29b1` 기준을 확인했다. 실제 제품 codec과의 golden 교차시험은 별도 실행한다.
- 모든 정수는 `<` little endian, alignment/padding 없음. UUID는 `UUID.bytes`의 RFC UUID 16B 순서이며 `bytes_le`가 아니다. SHA는 hex 문자열이 아닌 원 digest 32B다.
- decoded raw는 payload+5B, payload 최대240B. `packet()`은 240 초과를 거절하고 `unpack()`은 delimiter 포함 frame256B 초과·불일치 길이·CRC·잘못된 COBS를 거절한다. 기본9개 제품 payload 중 최장 REPORT123B/wire상한130B다. 선택형 단일존 ToF는145B/wire상한152B이며 같은 serial adapter에서 소비한다. 별도 thread/serial handle을 만들지 않는다.
- header raw offset: id0..1, length2, payload3부터, CRC는 `3+payload_length`부터2B. 다음 표의 offset은 **payload 시작 기준**이다. raw offset은 +3.
- RX는 최대4096B/read, 부분 frame을 유지한다. 중간 손상·oversize는 다음0 delimiter까지 버린다. 빈 delimiter는 frame이 아니다. invalid/oversize 계수는 진단이며 성공 ACK가 아니다.
- 현 SerialSink 코드 기본 baud는115200이다. baud는 설치 시 명시하고 실제 USB bridge/MCU 지원·전체 트래픽·STOP 지연을 계측한다. 구8×8 설계의460800/8N1 목표를 새 단일존 구성의 필수값으로 계승하지 않는다. USB line 설정 자체를 물리 적용 보증으로 해석하지 않는다. `vehicle/ESP32` 펌웨어의 개발용 기본값은460800이며, 운영값은 실제 USB에서 SENSOR와 REPORT가 함께 흐를 때의 계측으로 확정한다.

## 3. 정확 payload layout

각 필드는 필수 고정폭이며 optional 필드가 없다. absent 표본은 quality/flags로 구분하고 slot의0은 미지원 sentinel일 때만 쓴다. 설치 profile의 실측값에는 runtime 기본값이 없다. wire 정수형 범위보다 좁은 의미 제한은 아래와 후속 검산 절을 함께 적용한다.

### HELLO `0x0f00` · Pi → MCU

원 symbol `HELLO3`, format `<B16s32s`, payload **49B**, raw **54B**, COBS+delimiter 최대 **56B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `version` | `B` / 1 | version | 정확히 3 |
| 1 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | Pi serial adapter 기동마다 새 UUID; echo 필수 |
| 17 | `vehicle_config_sha256` | `32s` / 32 | 등록 config digest 원 32B | ACK와 설치 profile에 exact; canonical hash domain 미정 |

### HELLO_ACK `0x0f01` · MCU → Pi

원 symbol `ACK3`, format `<B16s16sH32s32sB`, payload **100B**, raw **105B**, COBS+delimiter 최대 **107B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `version` | `B` / 1 | version | 정확히 3 |
| 1 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 HELLO echo |
| 17 | `device_boot_session` | `16s` / 16 | UUID | NIL 금지; 부팅마다 새 identity; 같은 Pi 연결 중 변경은 fault |
| 33 | `capabilities` | `H` / 2 | bit mask | (caps & 0x01f7)==0x01f7; HOLD_STEER 추가 0x0400; 근거리 제동 추가 0x1000 |
| 35 | `vehicle_config_sha256` | `32s` / 32 | digest 원 32B | 원 HELLO 및 Pi 등록값 exact |
| 67 | `firmware_sha256` | `32s` / 32 | 등록 firmware digest 원 32B | Pi 등록값 exact; flash attestation으로 해석 금지 |
| 99 | `encoder_bits` | `B` / 1 | signed cumulative counter 폭 | 32 또는 64; Pi OdometryConfig와 exact |

### CLOCK_PROBE `0x0f10` · Pi → MCU

원 symbol `PROBE3`, format `<16s16sQQ`, payload **48B**, raw **53B**, COBS+delimiter 최대 **55B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 session |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 boot session |
| 32 | `probe_sequence` | `Q` / 8 | 원 probe ID | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 40 | `p1_pi_send_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | 원 probe 작성/송신 기준; 재송신/회신 도착으로 변경 금지 |

### CLOCK_REPLY `0x0f11` · MCU → Pi

원 symbol `CLOCK3`, format `<16s16sQQQQQQqqBiH`, payload **103B**, raw **108B**, COBS+delimiter 최대 **110B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 probe exact |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 probe exact |
| 32 | `probe_sequence` | `Q` / 8 | 원 probe ID | 현재 outstanding와 exact; <2^63 |
| 40 | `p1_pi_send_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | 원 p1 echo; 현재 outstanding와 exact; <2^63 |
| 48 | `d2_device_receive_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 0<d2<=latch<=d3; <2^63 |
| 56 | `latch_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 실제 양 encoder/조향 표본 latch; <2^63 |
| 64 | `d3_device_send_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 원 reply 실제 송신 기준; <2^63 |
| 72 | `source_sample` | `Q` / 8 | 실제 센서 표본 순번 | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 80 | `cumulative_left_ticks` | `q` / 8 | signed ticks | encoder_bits signed 범위; 명령 적분값 금지 |
| 88 | `cumulative_right_ticks` | `q` / 8 | signed ticks | encoder_bits signed 범위; 누계·wrap 규칙 동일 |
| 96 | `quality` | `B` / 1 | encoder/steering quality bits | 1 필수, unknown bit 금지; measured2/estimated4 동시 금지; cap0x1000 장치만 앞 막힘8/뒤 막힘16 |
| 97 | `steering_mrad` | `i` / 4 | signed mrad | quality2/4이면 abs(value)<=1400; 없으면 정확히 0 |
| 101 | `steering_error_mrad` | `H` / 2 | 보수 오차 mrad | quality2/4이면 1..65535; 없으면 정확히 0 |

### CLOCK_INSTALL `0x0f12` · Pi → MCU

원 symbol `INSTALL3`, format `<16s16s16sQQQqqQQ`, payload **104B**, raw **109B**, COBS+delimiter 최대 **111B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 session |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 boot session |
| 32 | `clock_epoch` | `16s` / 16 | Pi serial clock UUID | 현재 adapter epoch; 새 service epoch와 혼합 금지 |
| 48 | `clock_sequence` | `Q` / 8 | lease revision | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 56 | `probe_sequence` | `Q` / 8 | 원 probe ID | 해당 실제 CLOCK_REPLY와 exact |
| 64 | `reference_pi_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | Pi의 원 p4 receive 시각 |
| 72 | `lower_offset_us` | `q` / 8 | Pi−device offset 하한 us | signed; lower<=upper, profile 폭 한도 |
| 80 | `upper_offset_us` | `q` / 8 | Pi−device offset 상한 us | signed; MCU 동작 허가시간 자체가 아님 |
| 88 | `valid_until_pi_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | 원 p1+lease_us; ACK 도착으로 연장 금지 |
| 96 | `cutoff_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 원 계산 absolute cutoff; d3보다 커야 함 |

### CLOCK_ACK `0x0f13` · MCU → Pi

원 symbol `CLOCK_ACK3`, format `<16s16s16sQQBHQQ`, payload **83B**, raw **88B**, COBS+delimiter 최대 **90B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 INSTALL exact |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 INSTALL exact |
| 32 | `clock_epoch` | `16s` / 16 | 원 epoch | 현재 epoch exact |
| 48 | `clock_sequence` | `Q` / 8 | 원 lease revision | Pi 저장 lease 존재 |
| 56 | `probe_sequence` | `Q` / 8 | 원 probe ID | 해당 lease exact |
| 64 | `status` | `B` / 1 | 1 INSTALLED / 3 REJECTED | Pi 성공 수락은1만; 설계의 실패status3은usable로 승격0 |
| 65 | `reason` | `H` / 2 | 공통 reason enum | 성공 0; 설계 부록B 공통0..20 참조 |
| 67 | `device_now_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 원 ACK 시각; Pi는 device_now<원 cutoff 검산 |
| 75 | `cutoff_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 원 INSTALL cutoff exact; Pi receive<원 Pi E 필수 |

### SENSOR `0x0f15` · MCU → Pi

원 symbol `SENSOR3`, format `<16s16sQQqqBiH16sQ`, payload **95B**, raw **100B**, COBS+delimiter 최대 **102B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 session |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 boot session |
| 32 | `source_sample` | `Q` / 8 | 실제 센서 표본 순번 | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 40 | `source_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 실제 latch 시각; 1..2^63-1 |
| 48 | `cumulative_left_ticks` | `q` / 8 | signed ticks | encoder_bits signed 범위 |
| 56 | `cumulative_right_ticks` | `q` / 8 | signed ticks | encoder_bits signed 범위 |
| 64 | `quality` | `B` / 1 | sensor quality bits | CLOCK_REPLY와 같은 1/2/4 규칙과 막힘8/16 |
| 65 | `steering_mrad` | `i` / 4 | signed mrad | quality2/4이면 abs(value)<=1400; 없으면0 |
| 69 | `steering_error_mrad` | `H` / 2 | 보수 오차 mrad | quality2/4이면 양수; 없으면0 |
| 71 | `clock_epoch` | `16s` / 16 | 원 installed epoch | 현재 epoch exact |
| 87 | `clock_sequence` | `Q` / 8 | 원 installed lease | Pi에 ACK된 lease 존재; 원 E 유효 |

### COMMAND `0x0111` · Pi → MCU

원 symbol `COMMAND3`, format `<16s16sQ16sQQQQIBBQhhBB`, payload **108B**, raw **113B**, COBS+delimiter 최대 **115B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 session |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 boot session |
| 32 | `command_sequence` | `Q` / 8 | 비순환 명령 ID | 1..2^63-1; u8 counter와 다른 exact key |
| 40 | `clock_epoch` | `16s` / 16 | 원 lease epoch | DRIVE/HOLD exact installed; STOP NIL |
| 56 | `clock_sequence` | `Q` / 8 | 원 lease revision | DRIVE/HOLD exact installed; STOP0 |
| 64 | `issued_pi_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | DRIVE/HOLD 원 issued; STOP0 |
| 72 | `expires_pi_us` | `Q` / 8 | Pi CLOCK_MONOTONIC 계열 us; wall-clock/guest 시각 아님 | issued<E<=issued+100000us; 원 lease E 이하; STOP0 |
| 80 | `cutoff_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | DRIVE/HOLD absolute deadline; STOP0 |
| 88 | `generation` | `I` / 4 | Pi 제어 generation | u32; 보고 exact echo; VM ID 아님 |
| 92 | `counter` | `B` / 1 | 모든 출력 공통 u8 counter | 0..255 순환; 중복/현재성의 유일 key로 사용 금지 |
| 93 | `command_kind` | `B` / 1 | 1 DRIVE / 2 STOP / 3 HOLD_STEER | 그 외 미정·수락 금지 |
| 94 | `stop_command_sequence` | `Q` / 8 | 원 STOP pin ID | DRIVE/HOLD는 실제 APPLIED된 원 STOP; STOP0 |
| 102 | `velocity_mmps` | `h` / 2 | signed mm/s | i16 wire; HOLD/STOP0; 차량 승인한도 별도 적용 |
| 104 | `steering_mrad` | `h` / 2 | signed mrad | i16 wire; STOP0; HOLD는 정지 조향 목표 |
| 106 | `reason` | `B` / 1 | COMMAND reason0..10 | 설계 enum 아래 참조; 현 producer는 모든 kind에서0 |
| 107 | `reserved` | `B` / 1 | reserved | 계약0만; 현 producer0/STOP evidence0 강제 |

### REPORT `0x0f14` · MCU → Pi

원 symbol `FEEDBACK3`, format `<16s16sQIBBBQQQBiiB16sQQQH`, payload **123B**, raw **128B**, COBS+delimiter 최대 **130B**.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 COMMAND exact |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 원 COMMAND exact |
| 32 | `command_sequence` | `Q` / 8 | 원 명령 ID | pending 또는 원 STOP pin exact; unsolicited FAULT만0 |
| 40 | `generation` | `I` / 4 | 원 generation | 원 COMMAND exact |
| 44 | `counter` | `B` / 1 | 원 u8 counter | 원 COMMAND exact |
| 45 | `command_kind` | `B` / 1 | 원 kind | 원 COMMAND exact; unsolicited FAULT만0 |
| 46 | `status` | `B` / 1 | 1 ACCEPTED /2 APPLIED /3 REJECTED /4 FAULT | 다른 status 거절; ACCEPTED와 APPLIED 구분 |
| 47 | `report_sequence` | `Q` / 8 | device-session 보고 순번 | Pi HWM보다 커야 함; <=HWM은 duplicate ignore |
| 55 | `source_sample` | `Q` / 8 | 해당 사실과 연결된 실제 표본 | 정지 증거는 양수·원 Vehicle 마지막 표본 exact |
| 63 | `source_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 정지 증거는 양수·원 Vehicle 마지막 latch exact |
| 71 | `flags` | `B` / 1 | measurement presence bits | velocity1/steering2/stationary4; unknown bit 거절 |
| 72 | `velocity_mmps` | `i` / 4 | 보고 m/s ×1000 | flag1 없으면0; setpoint를 실측으로 위장 금지 |
| 76 | `steering_mrad` | `i` / 4 | 보고 rad ×1000 | flag2 없으면0; SENSOR quality와 별도 |
| 80 | `stationary` | `B` / 1 | 0/1 | APPLIED STOP+flag4에서만 physical_stop 후보; 단독 정지증명 아님 |
| 81 | `clock_epoch` | `16s` / 16 | 원 command epoch | 원 COMMAND exact; clockless STOP NIL |
| 97 | `clock_sequence` | `Q` / 8 | 원 command lease | 원 COMMAND exact; STOP0 |
| 105 | `cutoff_device_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | 원 COMMAND exact; STOP0 |
| 113 | `stop_command_sequence` | `Q` / 8 | 원 STOP reference | 원 COMMAND exact; STOP0 |
| 121 | `reason` | `H` / 2 | 공통 reason0..21 | 설계 enum 아래 참조; 성공0, 실패 원 이유 보존 |

### TOF_SAMPLE3 `0x0f17` · MCU → Pi (선택형 단일존 / MCU·물리 미수락)

제품 symbol `tof_feedback3.TOF_SAMPLE3`, opcode `TOF_SAMPLE=0x0f17`, format `<16s16sQQI16sQB16sII32sBBHHBBI`, payload **145B**, raw **150B**, COBS+delimiter 최대 **152B**다. 한 관측 전체가 이 패킷 하나이며 조각 필드는 없다. 구 `TOF_FRAGMENT3_DESIGN`/`0x0f16`/209B vector는 역사 provenance로만 보존하고 현재 decoder·MCU 구현 대상에서 제외한다. 구 패킷을 단일존으로 재해석하지 않는다.

| offset | field | type / bytes | 단위·의미 | 현재 제한 / sentinel |
|---:|---|---|---|---|
| 0 | `pi_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 session |
| 16 | `device_session` | `16s` / 16 | RFC UUID 16B; 현재 Pi/device와 exact | 현재 motor device boot session |
| 32 | `source_sample` | `Q` / 8 | ToF instance별 원 관측 순번 | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 40 | `device_sample_us` | `Q` / 8 | 같은 device_boot_session의 단조 us; 재부팅 시 새 session | ToF 적분/취득구간 대표 시각; encoder latch 재사용 금지 |
| 48 | `sample_time_error_us` | `I` / 4 | 원 시각 보수 오차 us | 전체 적분+IRQ/I2C취득 불확실성 구간 포함 |
| 52 | `clock_epoch` | `16s` / 16 | 원 clock epoch | 현재 mapped clock 참조 |
| 68 | `clock_sequence` | `Q` / 8 | 원 lease revision | wire u64; 정상 source는 1..2^63-1, wrap 재사용 금지 |
| 76 | `sensor_id` | `B` / 1 | 등록 센서 index | 0=앞 중앙, 1=뒤 중앙 |
| 77 | `sensor_instance` | `16s` / 16 | ToF 재초기화 UUID | NIL 금지; sensor reset마다 새 UUID |
| 93 | `sensor_generation` | `I` / 4 | ToF reset generation | 1..2^32-1; wrap 금지; 같은gen/다른instance conflict |
| 97 | `sensor_config_revision` | `I` / 4 | 등록 sensor profile revision | 원 calibration/source profile exact |
| 101 | `sensor_calibration_sha256` | `32s` / 32 | 등록 digest 원32B | 원 sensor profile exact |
| 133 | `rows` | `B` / 1 | 관측 rows | 1만 |
| 134 | `cols` | `B` / 1 | 관측 cols | 1만 |
| 135 | `frame_status` | `H` / 2 | bitmask | SENSOR_OK1/TIMING_VALID2/TARGET_REDUCTION_APPLIED4; 그 외0 |
| 137 | `distance_mm` | `H` / 2 | 단일 거리 mm | 1..65534 유효 후보; 0/65535 invalid |
| 139 | `status` | `B` / 1 | 단일 wire 상태 | 0..7; 아래 의미 |
| 140 | `quality` | `B` / 1 | 단일 quality bitmask | 0x1f 이외 거절; 어느 bit도 FREE 허가가 아님 |
| 141 | `sample_crc32` | `I` / 4 | 원 전체 관측 CRC32 | 직전141B canonical body CRC32/IEEE |

## 4. session·capability·firmware 등록

Pi HELLO는 version3+새 Pi session+등록 config digest다. MCU는 부팅 session과 실제 지원 cap, 등록 config/firmware digest, 실제 누계 encoder 폭을 ACK한다. ACK의 원 Pi echo/NIL 아닌 device session/등록 SHA/encoder ABI/필수 mask가 모두 맞아야 Pi `ready`가 된다. 이는 출력 허가나 정지 확인이 아니다. ACK만으로 DRIVE를 시작하지 않는다.

Pi는 `ready` 전에는 같은 Pi session의 HELLO를50ms 간격으로 다시 보낸다. handshake 기한은 첫 HELLO부터 세며 재전송으로 늘지 않고, 기한 뒤의 고장은 늦은 ACK로 풀리지 않는다. MCU는 열려 있는 session과 같은 Pi session의 HELLO에 session을 닫지 않고 같은 ACK로 답한다.

현 필수 mask는 **0x01f7**. 기본 u16 의미는 ACCEPT0x1, APPLY0x2, VELOCITY0x4, STEERING0x8, STATIONARY0x10, ODOMETRY0x20, **ABS_EXPIRY0x40, CLOCK4_LATCH0x80, DIRECTION_GATE0x100, STATIONARY_STEER0x400**이다. 선택형 단일존 ToF는 신규 `TOF_CAPABILITY=0x0800`을 검사한다. 선택형 근거리 제동은 `PROXIMITY_CAPABILITY=0x1000`으로 알린다. 구 조각형0x0200은 역사 예약값으로 두며 새 ToF 지원으로 인정하지 않는다. ToF를 쓸 profile에는0x0800, 정지 조향 profile에는0x0400도 필요하다. `tof_profiles: []`는0x0800 없이 정상이며, 선택한 ToF의 cap 부족은 ToF만 unavailable로 남긴다. clock/encoder/STOP의 기존 준비조건을 ToF로 막지 않는다. 0x8은 필수 mask 밖이며 AUTO HOLD의 measured steering sensor quality와 동일 개념이 아니다. 없는 기능을 cap로 광고하지 않는다.

같은 Pi 연결에서 MCU boot session이 바뀌면 DEVICE_RESTARTED fault·ready 철회다. 새 ACK/새 표본이 기존 fault를 자동 해제하지 않는다. clock/session/cumulative counter를 조용히 초기화해 같은 장치로 계속 달리지 않는다. Pi 재기동은 새 pi_session/clock_epoch로 다시 설치한다. boot session 생성·영구 저장 방식은 MCU 제공물에 명시한다.

`firmware_sha256`은 **등록된 기대값과 ACK 자기보고 digest를 비교하는 코드**다. 바이너리 전체 자체 내장 hash/flash contents 검증/보안 attestation이라고 확정할 수 없다. 현 확인 범위에는 bin/ELF/partition 중 hash 대상, hash-domain/canonical manifest, NVS 저장·업데이트·읽기 검증 절차가 없다. config SHA의 canonical bytes도 없다. 자기 해시를 자신을 포함한 binary에 넣는 순환 정의를 만들지 말고 빌드 manifest·분리 저장값·읽기 검증 범위를 MCU 담당이 제안해 공동 등록한다. Pi의 private profile 소유·권한 검사는 firmware contents 검증을 대신하지 않는다.

## 5. 원 4시각과 실제 latch → 설치 ACK

원 시각은 p1=Pi probe send 기준, d2=MCU receive, latch=실제 센서 표본, d3=MCU reply send 기준, p4=Pi의 **완성 read 시각**이다. `0<d2<=latch<=d3`이며 원 outstanding(probe,p1) exact, `p1<=p4<p1+probe_timeout_us`를 검사한다. batch의 모든 완성 frame에는 같은 실제 read 완료 시각을 쓴다. 부분 frame에는 완성한 read의 시각이 붙는다. 캐시 센서/가짜 d2=d3/도착시각 재라벨로 RTT를 줄이지 않는다. latch와 양쪽 ticks/조향의 동시성 오차를 timestamp_error 측정에 포함한다.

`D(dt)=ceil(drift_ppm*dt/(1_000_000-drift_ppm))`.

1. `pad=2*timestamp_error_us+D(p4-p1)`; `lower=p1-d2-pad`, `upper=p4-d3+pad` (Pi−device). lower<=upper 및 폭 상한을 검사한다.
2. 이전 구간을 경과 drift로 확장했을 때 교집합이 있어야 하며, 연속 compatible reply2개 뒤에만 lease를 만든다. 불연속이면 clock fault/lease 폐기다.
3. 원 `E=p1+lease_us`; `deviceCutoff=E-(upper+D(E-p4))-j_cutoff_us`. cutoff>d3이어야 한다. p4/ACK/retry 도착부터 새 lease를 시작하지 않는다.
4. INSTALL은 원 probe·시각·구간·E·cutoff를 전달한다. MCU는 원 epoch/seq를 실제 cutoff timer에 설치한 뒤 exact ACK를 보낸다. 성공status1/reason0, device_now<원cutoff, Pi receive<원E여야 ACK lease가 usable이 된다.
5. SENSOR는 ACK된 원 lease ref를 쓴다. Pi는 device source 시각을 drift 확장한 원 offset 구간으로 map한다. `lo=source+lower(now)`, `hi=min(source+upper(now),now)`, `0<=lo<=hi<=now`, 원 age/uncertainty 상한을 검사한다. 원 sensor 시각을 현재 arrival로 바꾸지 않는다.

Pi probe 주기는200ms, outstanding1; probe timeout은 설치 profile≤200ms. INSTALL retry는50ms 간격으로 **동일 원 bytes**를 사용하며 E를 바꾸지 않는다. lease 슬롯은2개이며 살아 있는 pending command가 원 슬롯을 참조하면 조용히 evict하지 않고 거절한다. encoder 최초 유효 표본은 frame anchor만 만들고 속도0 표본으로 발행하지 않는다. 표본시각/순번이 증가하고 유효한 후속 표본이 있어야 Pi Vehicle이 생긴다. CLOCK_REPLY와 SENSOR는 같은 device sample 순서를 공유하며 SENSOR50Hz는 software 목표다. 장치가 reply에 이미 낸 latch를 새 sample/time으로 재송신해 관측 주파수를 부풀리지 않는다.

## 6. DRIVE·STOP·HOLD_STEER와 원 기한

DRIVE/HOLD는 handshake+ACK된 현재 clock+원issued<E≤issued+100ms+now<E≤leaseE를 요구한다. Pi는 `commandCutoff=min(leaseCutoff, E-upperBoundAt(E)-j_cutoff_us)`로 생성한다. 합의된 MCU 책임은 **apply 직전 `device_now+J_apply<device_cutoff` 검산**, cutoff에서 안전 출력 진입 시작·J_cutoff 안 완료다. 원 Pi E 이전 보장은 안전 출력 완료이며 차량 속도0 보장은 아니다. 실제 제동거리/정지 관측은 별도다. 독립 timer/watchdog를 사용하고 INSTALL 재전송, clock refresh, ACK/REPORT 수신, 같은 명령 retry가 원 cutoff/E를 늘려서는 안 된다. Pi owner 지연/USB EOF 중에도 MCU에서 안전 출력이 필요하다.

속도·조향은 Pi의 승인된 차량 한도에서 생성하며 wire에는 round(value×1000)를 signed i16으로 pack한다. feedback3 pack은 i16 범위 밖 값을 조용히 clamp하지 않는다. +속도 전진/-속도 후진; 조향 +축의 실제 좌우 부호는 MCU 장착·차량 보정과 맞추어 공동 시험에서 확정한다. 이 문서만으로 왼쪽/오른쪽을 새로 정하지 않는다.

STOP은 handshake된 session 아래 clockless로 보낼 수 있으며 clock epoch NIL, clockseq/issued/E/cutoff/STOP ref/속도/조향은0이다. clock 설치 실패가 STOP을 막지 않는다. MCU는 유효 current-session 새 STOP을 우선 처리하고 pending DRIVE를 버린다. old-generation STOP도 안전 출력에는 적용하지만 generation을 후퇴시키지 않는다. 현 Pi producer는 frame.reason을 COMMAND3에 전달하지 않고 reason slot0을 보낸다. STOP의0 기한은 무기한 DRIVE가 아니라 **정지 명령의 sentinel**이다. 원 STOP APPLIED와 실측 정지창 없이는 후속 추진 허가가 생기지 않는다.

합의된 COMMAND reason:u8은0 NONE/1 OPERATOR/2 FAULT/3 CUSP/4 MISSION_CANCEL/5 MISSION_COMPLETE/6 SHUTDOWN/7 EXPIRY/8 EVAL_STOP/9 INPUT_LOST/10 MODE_CHANGE다. 현 producer의0과 목표 reason enum을 구분한다. reserved는 계약상0만이다.

같은 episode/generation/phase/cusp/driver 문맥에서 이미 멈춘 상태의 반복 STOP은 원 pin sequence/command bytes/제출시각을 재사용하고 wire 재전송0일 수 있다. u8 counter가 달라도 원 STOP pin이 바뀌지 않는다. 새 STOP을 보내 정지창·phase budget을 계속 초기화하면 안 된다. 첫 원 STOP의 phase budget≤10s는 각 DRIVE/HOLD≤100ms E와 별개이며 상위 phase E가 더 짧으면 줄인다. 같은 부호 연속 구동은 원 gate를 유지할 수 있으나 새mode/driver/주차cusp·방향반전에는 새STOP 경계가 필요하다. 낮은 generation DRIVE는 MCU가 거절한다. 더 높은 generation 같은방향 A/B 연속은 Pi의 현재 권한 검산이 선행해야 하며 MCU가 자체승계를 결정하지 않는다.

DRIVE/HOLD는 실제 APPLIED 원 STOP ref를 요구한다. 이동 방향을 바꾸려면 새 STOP이 필요하다. HOLD_STEER는 kind3/velocity0/cap0x0400이며 원 VM/plan/phase/segment/cusp/mission 또는 driver 문맥 일치를 Pi에서 검사한다. 이 내부 문맥을 MCU wire에 추가하지 않는다. MCU는 받은 원 STOP sequence·generation·kind와 실제 적용을 보존한다.

## 7. 보고·실제 정지·조향 준비

ACCEPTED는 파싱·수락, APPLIED는 실제 actuator 적용, REJECTED는 거부, FAULT는 장치 실패다. write 성공/PUBACK/ACCEPTED/APPLIED 하나를 물리 정지라고 부르지 않는다. Pi는 report session·원 sequence/gen/counter/kind·clock/cutoff/STOP echo를 검사한다. nonSTOP 보고가 원 Pi E 이후 도착하면 거절한다. 장치 전체 fault는 seq0/kind0/status4 특별형이다. 이 형태에 성공·정지 권한을 부여하지 않는다.

report_sequence는 장치 session 안에서 증가한다. <=Pi HWM은 duplicate ignore이며 현재성을 갱신하지 않는다. 현 코드의 report slot을 같은 명령에서 ACCEPTED 뒤 APPLIED 순으로 생산하고, APPLIED 뒤 뒤늦은 ACCEPTED로 회귀시키지 않는다. 부록B §10.1의 **MCU 계약은 최근 원 command 결과8개+최고 sequence fence+별도 active STOP pin1개**다. 같은 seq/같은 bytes는 원 ACK/report sequence·time replay만 하며 재적용/watchdog/E 연장0, 같은 seq/다른 bytes는 conflict다. 원 STOP은 HOLD50Hz에 ACK cache에서 밀려도 pin으로 유지해야 한다. 이후 실제 새로운 stationary 관측 보고만 새 report/sample/time을 가질 수 있다. INSTALL 동일 retry도 timer를 새로 시작하지 않는다. 이 설계가 현재 MCU에서 구현됐다고 주장하지 않는다.

공통 CLOCK_ACK/REPORT reason:u16은0 OK/1 BAD_LENGTH/2 BAD_CRC/3 SESSION_MISMATCH/4 SEQUENCE_REPLAY/5 IDEMPOTENCY_CONFLICT/6 CLOCK_UNAVAILABLE/7 CLOCK_EXPIRED/8 COMMAND_EXPIRED/9 RANGE/10 DIRECTION_STOP_REQUIRED/11 DEVICE_FAULT/12 CONFIG_MISMATCH/13 COUNTER_EXHAUSTED/14 E_STOP/15 GENERATION_REPLAY/16 UNSUPPORTED_PROFILE/17 TIMESTAMP_ORDER/18 SENSOR_UNAVAILABLE/19 DEADLINE_MISMATCH/20 CLOCK_DRIFT/21 PROXIMITY_BLOCKED다. 21은 REPORT에서만 쓴다. CLOCK_ACK status1 INSTALLED/3 REJECTED이며 Pi는 성공1/reason0만 usable로 받는다. CRC/session을 확인 못 하는 frame에는 ACK를 발명하지 않고 bounded error counter만 남긴다.

**근거리 제동(선택형, cap0x1000).** MCU는 앞·뒤 ToF로 가까운 물체를 보면 그 방향의 출력을 스스로 끊고, SENSOR/CLOCK_REPLY 표본 quality의 앞 막힘8/뒤 막힘16으로 알린다. 막혀 있는 동안 모든 표본에 켜지고 풀리면 꺼진다. 센서가 판단할 값을 주지 않아 막을 때도 같은 bit다. 막힌 방향의 DRIVE는 REJECTED/reason21로 거절하고 STOP·HOLD_STEER·반대 방향 DRIVE는 그 이유로 거절하지 않는다. 끊긴 DRIVE의 REPORT는 더 보내지 않는다. Pi는 cap0x1000 장치의 두 bit를 odometry에 넘기지 않고 view의 `proximity_front_blocked`/`proximity_rear_blocked`로 낸다. 이 값은 odometry가 받아들인 가장 새 표본의 것이며, 표본이 끊기거나 거절되는 동안에는 갱신되지 않는다. DRIVE의 REJECTED/reason21은 `DEVICE_REJECTED`로 묶지 않는다. 그 명령만 거절된 것으로 적고(`rejection=PROXIMITY_BLOCKED`, `proximity_rejected` 증가) 적용 대기에서 빼 `DEVICE_APPLY_TIMEOUT`으로도 넘기지 않는다. cap0x1000이 없는 장치의 두 bit와 reason21, STOP·HOLD_STEER의 reason21, status4는 기존처럼 거절/고장이다. 근거리 정지는 STOP APPLIED가 아니며 방향을 바꾸려면 기존 규칙대로 새 STOP의 APPLIED가 필요하다. Pi는 막힌 방향의 DRIVE를 미리 막지 않고 MCU의 거절을 받는다. 막힘을 받은 뒤 AUTO·MANUAL의 동작은 아직 정하지 않았다. 기준 거리와 판단 규칙은 MCU 쪽 설정이며 [ToF 요청서](../requests/2026-11-10-tof-vl53l0x-gateway-jetson.md) 2.1에 있다. MCU 구현과 실차 확인은 아직 없다.

flags는 reported velocity1/steering2/stationary4 presence다. flag가 없는 velocity/steering slot은0이어야 하고 Pi view는 null로 해석한다. `stationary`는0/1만 허용하며 APPLIED STOP+flag4의 후보만 physical_stop에 반영된다. REPORT steering에는 measured/estimated quality가 없으므로 실제 조향 준비 검사는 **SENSOR/CLOCK_REPLY의 원 quality+positive error**를 사용한다. 현 feedback3 REPORT parser는 feedback1과 달리 presence를 ACK capability의 각 sensor bit와 별도 재대조하지 않는다. 이를 firmware의 허위 cap/센서 허용이라고 해석하지 않으며 공식 수락 시 cross-check 범위를 결정한다.

Pi `MatchedStop`은 원 STOP COMMAND와 APPLIED REPORT와2..256개 원 Vehicle bytes를 exact 연결한다. 마지막 원 sample/time이 보고와 같고 현재 최신 sample이어야 한다. 각 표본의 Pi/device/host/odom/calibration/session·순번·gap·age를 검산한다. **양 wheel 각각** `(abs(deltaTicks)+1)*metres_per_tick/보수dt +미관측속도상한`과 yaw+양끝heading error+미관측 yaw 상한을 검사하고 유한 관측창 길이를 요구한다. 평균 속도0이나 stationary bit1만으로 대체하지 않는다. 첫 APPLIED의 원 report·map된 실제 시각을 보존하며 늦은 보고로 첫 적용시각을 현재로 바꾸지 않는다.

AUTO cusp HOLD_READY는 여기서 더 나아가 실제 HOLD APPLIED 이후 연속 **measured** 조향 표본에 대해 `abs(measured-target)+원error<=steering_tolerance_rad`, 원 gap/age/보수 관측기간≥steering_settle_us를 만족해야 한다. encoder quality1은 필수, measured2 또는 estimated4는 하나만, 제공된 조향 error는 반드시 양수다. **센서 없는 estimated-only 하드웨어는 현재 AUTO HOLD_READY를 만족하지 않는다.** 목표 조향/서보 PWM/추정치를 measured로 위장하지 않는다. estimated로 진행하는 별도 정책은 아직 수락되지 않았다. MANUAL의 neutral READY는 별도 원 SID/실제 STOP 경계이며 AUTO 조향 settle 성공과 같지 않다.

## 8. encoder·wrap·reset·fault·부분 전송

MCU는 실제 양쪽 누계 ticks와 실제 latch/sample을 보낸다. 순번은 송신횟수가 아니라 실제 sensor sample ID다. 같은 표본을 다른 시각/ID로 다시 만들지 않는다. signed32/64 범위의 정상 wrap은 Pi가 모듈러 차분한다. source 시각/순번 역전·0·동일 sample 재사용은 sensor 검산에서 거절한다. ticks의 reset을 정상 wrap으로 위장하지 말고 새 boot session/명시 fault와 재설치로 구분한다. **time/command/report/sample/clock은1..2^63-1, generation은1..2^32-1이며 wrap 전 STOP·새 disarmed session 절차가 필요하다.** MCU32bit micros를 atomic64 확장 없이 u64로 포장하지 않는다. encoder modulo와 debug counteru8만 wrap 예외다. Pi의 일부 parser는 이 전체 의미범위를 모든 필드에 똑같이 강제하지 않으므로 최종 codec 회귀에서 계약과 실제 거절 경계를 분리한다.

Pi odometry는 원 Δticks와 source dt·승인 최대속도·Ackermann 일관성을 검사한다. 좌우가 한 tick씩 정반대로 움직인 표본은 encoder 양자화 폭으로 보아 거절하지 않고 측정대로 제자리 yaw로 적분하며, 두 tick 이상의 정반대 변화는 `ODOM_ACKERMANN_INCONSISTENT`로 거절한다. 긴 gap은 새 odom epoch/anchor가 필요하고 잃어버린 이동을0으로 복구하지 않는다. Pi의 별도 absolute meter는 연속 source_sample+1일 때 wrap-aware `max(abs(ΔL),abs(ΔR))`를 누적한다. MCU가 mean/abs(mean)으로 원 누계 wheel 정보를 줄이면 안 된다. 이 meter와 임무 거리예산은 Pi 내부이며 ESP packet을 새로 만들지 않는다.

시계 불연속, device restart, malformed/과기한/다른 session, partial TX, RX EOF, command ACK timeout은 현재성/출력에 영향을 준다. Pi read는 partial frame을 다음 read까지 유지하지만 incomplete bytes를 ACK로 쓰지 않는다. nonblocking TX가 partial이면 전체 적용 여부 미확정으로 실패하고 serial frame을 성공 처리하지 않는다. readable 이후 빈 read가 EOF이며 idle의 빈 read와 구분한다. 물리 USB 단절 때 Pi가 STOP을 전송할 수 있다고 가정하지 않는다. 독립 MCU cutoff/watchdog/E-stop·brake가 최종 보호다. MCU의 brake/stop 출력 방식·무전원 상태·고장 latch 해제 절차는 실차 공동 수락 항목이다.

## 9. 설치 수치·미정 항목

`Feedback3Profile` exact 필드13개: vehicle_config_sha256, firmware_sha256, timestamp_error_us, drift_ppm, max_offset_width_us, lease_us, probe_timeout_us, j_cutoff_us, j_apply_us, stationary, phase_budget_us, steering_tolerance_rad, steering_settle_us. private canonical 절대경로·소유uid(기본root)·일반 single-link 파일·group/other 권한0·최대8192B를 요구한다. Odometry/vehicle limits/feedback timeout도 별도 설치값이다.

- timestamp_error/drift/j_cutoff/j_apply는 exact int0..999999. lease는0<≤1s, probe timeout0<≤200ms, phase budget0<≤10s, steering tolerance finite≥0, settle0<≤phase budget이다. **max_offset_width는 현 validator에서 truthy만 검사하며 양수 정수까지 강제하지 않는다.** intended positive integer와 구현 차이를 숨기지 않는다. 수치 타입을 조용히 강화하는 제품 수정은 이 문서의 작성 범위 밖이다.
- StationaryPolicy exact8필드: window_us/max_gap_us/max_age_us(1..u63), metres_per_tick(유한>0), max_wheel_mps/max_yaw_rps/unobserved_wheel_mps/unobserved_yaw_rps(유한≥0). 현 wrap 경계에서 stationary checker는 raw ticks 차이를 쓰므로 wrap 인접창은 보수 실패할 수 있다. 정상 주행 odometry wrap 지원과 정지 인증창 통과를 혼동하지 않는다.
- `j_cutoff_us`는 Pi cutoff 계산에서 소비된다. `j_apply_us`는 Pi에서 저장/범위 검사만 하며 **MCU apply 직전 검산의 입력**이라는 합의가 있다. Pi가 이를 cutoff 식에 다시 빼지 않는 것 자체는 버그로 단정하지 않는다. config hash가 같은 실측 rho/e/J_cutoff 등 설치 설정을 Pi/MCU에 결박해야 하지만, MCU에 실제 같은 J_apply/J_cutoff를 넣는 manifest/NVS/배포 절차와 계측 증거는 아직 확인되지 않았다.
- clock error/drift, serial/ISR/timer jitter j_cutoff/j_apply, 양 encoder scale와 상한, 조향 실측오차·settle·허용오차, wheel/yaw stationary bound, brake 지연/감속/정지거리/최대속도는 장치에서 실측·승인해야 한다. test의 a/b 반복 SHA, zero jitter, 가상1mrad 오차, µm/tick은 하드웨어 기본값이 아니다.
- ToF source 빈 목록은 허용된다. 장착 시에는 현재 단일존 **0x0f17/145B·cap0x0800·CRC141B**와 원 source lifetime을 MCU/Pi/Jetson에서 함께 사용한다. 구0x0f16/8×8 조각은 거절하며 자동 호환·downgrade하지 않는다. 설치 센서 모델·native status 매핑·위치/방향·관측 범위·측정 오차와 firmware 구현·물리 수락은 별도 확인한다.
- 미확인 핵심은 hash domain/manifest/NVS·실측 MCU 설정 주입/검증과 실제 firmware 구현·물리 성능이다. cap/reason/cache 규칙의 설계 존재와 구현 미확인을 구분한다.

## 9.1 optional ToF의 합의 wire와 선택형 Pi 연결

앞 중앙0·뒤 중앙1의 두 센서는 각각 단일존1×1이다. 패킷 한 개에 거리:u16LE·status:u8·quality:u8를 각각 하나 넣는다. 실제 센서 모델·ROI/FOV·거리 오차·AXIAL_Z/RADIAL 의미·취득 주기는 아직 확정하지 않는다. 장착/driver 보정 manifest에 명시한 값만 사용하고 구8×8 센서의 native 상태·free whitelist·2target 설정을 새 하드웨어의 성질로 계승하지 않는다.

status는0 UNKNOWN/1 VALID/2 NO_TARGET/3 OUT_OF_RANGE/4 SATURATED/5 SENSOR_ERROR/6 STALE/7 BAD_RETURN이다. quality bit는 TARGET_VALID1/NATIVE_QUALITY_AVAILABLE2/SECOND_TARGET_OBSERVED4/REDUCED_TO_CLOSEST8/FREE_BLOCKED16이며 상위 bit는0이다. TARGET_VALID는 status1과 유효 거리1..65534를 요구한다. frame_status는 SENSOR_OK1/TIMING_VALID2/TARGET_REDUCTION_APPLIED4만 허용한다. native driver 상태와 wire enum은 구분하며 실제 변환표를 담당자가 제공한다.

**ToF는 근거리 장애물 HIT-only 입력이다.** 유효 거리의 장애물 후보를 제공하며 측정 앞쪽 구간·반환 없음·out-of-range·오류·넓은 관측 구역을 FREE로 carve하지 않는다. 어떤 quality bit, 단일존 정상 수신 또는 거리값도 FREE 증거를 발급하지 않는다. 센서가 보지 못한 뒤·옆 공간은 UNKNOWN으로 남는다. 현재 driver/보정이 지지하지 않는 정밀 위치나 장애물 형상을 추정해 확정하지 않는다.

Jetson M은 관측 원 시각의 odom을 보간하고 등록된 map 변환·오차를 적용해 발행용 World에만 HIT를 덧씌운다. 원 저장 지도/정적 FREE·주차칸은 수정하지 않는다. HIT 만료·센서 reset 뒤 해당 영역은 더 새로운 카메라 관측이 확인될 때까지 UNKNOWN이며, 원 시각·수명을 재발행으로 늘리지 않는다. 현재 발행 창 밖의 영역을 ToF가 관측했다고 주장하지 않는다. ToF는 정적 조사 완료의 필수 센서가 아니며, 미장착·무효·보간 불가 때도 카메라 파이프라인은 계속 처리한다.

sample_crc32는 CRC32/IEEE(reflected poly0xedb88320, init/final xor0xffffffff)다. `TOF_COMMON3`의137B, 즉 `pi_session,device_session,source_sample,device_sample_us,sample_time_error_us,clock_epoch,clock_seq,sensor_id,sensor_instance,sensor_generation,sensor_config_revision,sensor_calibration_sha256,rows=1,cols=1,frame_status` 뒤 단일 `distance/status/quality`4B를 붙인 **141B**를 계산한다. 이는 payload0..140이며 CRC 필드 자체는 제외한다. outer CRC16도 별도로 검사한다. 원 관측 전체가 한 packet이므로 fragment index/count·zone range·partial buffer·조립 timer·재조립 절차는 없다.

sensor reset은 generation증가+새instance이며 motor device session/encoder/odom을 reset하지 않는다. same-generation 다른instance conflict·구generation거절·generation소진 unavailable이다. 시각오차는 해당 센서의 전체 적분/취득+IRQ/I2C 지연 구간을 감싸고 encoder latch/UART send 시각으로 대체하지 않는다. TIMING_VALID는 실제 시간근거가 있을 때만 켠다.

Pi 단일 serial owner는 outer framing/session·선택 sensor·신규 cap0x0800·원 clock epoch/seq와 실제 INSTALL ACK lease를 확인하고, 각145B와 원 완성 read 시각·변경 불가 lease 사본을 queue8에 nonblocking으로 넘긴다. overflow는 원 queued batch 전체를 폐기하고 영향 source를 fence한다. Gateway actor는 센서별 최신 완전 관측141B 한 개만 보유한다. 두 센서의 원 관측 bytes 합계282B와 입력 queue 최대1160B는 Python object overhead를 제외한 크기다. CRC32·protobuf/J 작업은 serial owner에서 하지 않는다. 중복 관측은 재발행하지 않고 같은 순번의 다른 내용·역전·CRC 오류를 거절한다.

원 ACK lease의 Pi−device 구간을 capture까지 보수 drift 확장한 뒤 TIMING_VALID와 원 sensor time error를 함께 검사한다. `pi_sample_us`는 원 capture를 map한 midpoint이며 `clock_error_us`는 보수 half-width다. 원 만료는 `pi_sample_us+tof_max_age_us-clock_error_us-sensor_time_error_us`이고 actor poll/J 전달/재접속으로 늘지 않는다. 도착시각이나 encoder latch로 ToF source time을 바꾸지 않는다. reset은 센서별 더 높은 generation+새 instance와 실제 증가한 motor-boot 시각을 요구하며, old generation·같은 generation의 다른 instance·같은 instance의 generation 재라벨을 거절한다. device boot/session·clock epoch 변경은 기존 전역 fault/session 규칙을 따르고 ToF가 odom/encoder를 초기화하지 않는다.

actor는 원 `TofObservation`을 기존 `submit_mapper_tof→submit_tof→offer_tof`로 넘긴다. 공용 message 필드는 유지하되 `rows=cols=1`, `distance_mm_le`2B·`status`1B·`quality`1B, `complete=true`만 현재 운영 입력으로 허용한다. 선택 profile의 leaf/config, Pi/device/host/Gateway 원천과 source HWM을 다시 검사하고 J가 original expiry로 보낸다. source 손실/reset은 아직 보내지 않은 parent/seqpacket 사본만 폐기할 수 있다. 이미 child/MQTT에 제출된 관측을 즉시 철회하는 wire는 없으므로 그런 보장을 하지 않는다. 다음 실제 higher-generation 완전 관측 또는 원 expiry로 종결한다. empty source/센서 오류/ToF timeout은 encoder·clock·STOP을 바꾸지 않으며 ToF는 출력 권한을 만들지 않는다.

STOP/fault→COMMAND/FEEDBACK→encoder/clock→ToF 순위이며 ToF polling/reset이 control/encoder를 막아서는 안 된다. 센서당10Hz는 조정 가능한 software bench 가정이며 실제 센서 율을 확정하지 않는다. 그 가정의 ToF wire 최대량은 `152B×10Hz×2=3040B/s`다. 8N1에서 wire 상한152B를 연속 송신하는 계산값은115200에서 약13.20ms,460800에서 약3.30ms다. 이는 실제 지연 상한이 아니며 ACK·encoder·기타 traffic과 USB/driver queue·jitter를 포함한 STOP 지연은 별도 측정한다. 모든 센서 표본을 대량 선입력해 STOP을 뒤에 세우거나 control E를 연장하지 않는다.

## 10. 고정 vector·검증 범위

[gateway-feedback-3.json](vectors/gateway-feedback-3.json)의 기본 제어9종은 기존 비교 기준이다. 그 안의 `TOF_FRAGMENT3_DESIGN`/209B/8×8 원 bytes는 역사 provenance로만 보존하며 현재 제품 decoder가 수락해야 할 vector가 아니다. 현재 단일존0x0f17의 [앞/뒤 고정 vector](vectors/gateway-tof-single-1.json)와 codec 교차시험은145B layout·141B sample CRC 및 구 opcode 거절을 검사한다. UUID/hash/time/jitter/encoder/오차는 codec 전용 가상값이며 실물 설정 기본값이 아니다. firmware/config hash도 명시한 fixture-only label의 digest이지 배포 바이너리 digest가 아니다.

[test_feedback3_vectors.py](../../platform/services/gateway/tests/test_feedback3_vectors.py)는 기본 제어 struct·packet/unpack·Framer·frame CRC/COBS와 고정 bytes를 교차한다. STOP/전진/후진/HOLD, 상태보고, measured/estimated/미지원 slot, CRC/COBS/길이/부분/oversize를 확인한다. [ToF 회귀](../../platform/services/gateway/tests/test_tof_feedback3.py)는 현재 단일패킷 decoder·시각·세대·CRC·압력 반례, 실제 development FD/원 ACK 경로→actor→기존 J local port와 구 조각형 거절을 별도로 검사한다. software leaf·local port 수락과 실제 MCU·MQTT/Mapper 처리·HIT 의미·물리 수락의 범위를 구분한다.

고정 vector는 독립 stdlib CRC/COBS/struct와 Pi codec의 byte offset/size·원 bytes를 비교하는 기준이다. 실행별 source·결과·미수락 경계는 Jetson-drive-docs/making-reports에서 관리하며 개별 시험 경과를 계약 수락으로 대신하지 않는다. codec와 ToF consumer의 소프트웨어 선택자는 다음과 같다.

```text
pytest -o addopts= -q platform/services/gateway/tests/test_feedback3_vectors.py platform/services/gateway/tests/test_tof_feedback3.py platform/services/gateway/tests/test_feedback3_proximity.py
```

원 feedback3 PTY2개와 ControlFacts 실제 PTY3개는 pyserial 준비/승인 대기 상태이며 이 codec 선택과 별개다. mock/development-fd 또는 단순 codec 성공을 그5개의 PASS·실제 USB/MCU·타이머/실측 센서·정지 수락으로 바꾸지 않는다.

## 11. 구현 원천

| 책임 | 현재 파일·기호 |
|---|---|
| 고정 payload/session/clock/원 command/HOLD | [feedback3.py](../../platform/services/gateway/xenova_gateway/feedback3.py) `Feedback3Profile`, `Feedback3SerialSink` |
| COBS/CRC 외부 framing·bounded RX | [feedback.py](../../platform/services/gateway/xenova_gateway/feedback.py) `packet/unpack/Framer`; [frame.py](../../platform/services/gateway/xenova_gateway/frame.py) `crc16/cobs_encode/cobs_decode` |
| COMMAND3/FEEDBACK3·원 STOP·실측 window | [scene_authority.py](../../platform/services/gateway/xenova_gateway/scene_authority.py) `COMMAND3/FEEDBACK3/MatchedStop/check_stationary_window` |
| 원 누계/품질·steering error·Vehicle | [odometry.py](../../platform/services/gateway/xenova_gateway/odometry.py) `observe_mapped`; [absolute_meter.py](../../platform/services/gateway/xenova_gateway/absolute_meter.py) |
| 실제 단일 owner·원 E·link failure | [output_owner.py](../../platform/services/gateway/xenova_gateway/output_owner.py) |
| private 설치 profile | [operator_profile.py](../../platform/services/gateway/xenova_gateway/operator_profile.py) `load_feedback3/private_profile` |
| 선택 ToF 단일패킷 raw queue·원 capture mapping | [tof_feedback3.py](../../platform/services/gateway/xenova_gateway/tof_feedback3.py) `TofReceipt/TofInbox/TofReceiver`; [verified_runner.py](../../platform/services/gateway/xenova_gateway/verified_runner.py) `configure_mapper_tof/_mapper_tof_once` |
| 근거리 막힘 표시·reason21 거절 수용 | [feedback3.py](../../platform/services/gateway/xenova_gateway/feedback3.py) `_sample`, REPORT 처리, `proximity_view` |
| 원 ToF 최신 사본·J 전달/HWM | [mapper_uplink.py](../../platform/services/gateway/xenova_gateway/mapper_uplink.py) `submit_tof/offer_tof/revoke_tof` |

가상 [virtual_feedback3.py](../../platform/services/gateway/examples/virtual_feedback3.py)는 시험 대역이며 firmware 구현 기준 전체나 물리 센서 대용이 아니다. 기본 zero steering error 등 일부 옛 가상 설정은 현 quality 계약에서 유효하지 않을 수 있으므로 실물 설정으로 복사하지 않는다.
