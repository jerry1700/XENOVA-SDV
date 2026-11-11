# ESP32 — 차량 장치 펌웨어

RC카의 모터·서보·엔코더를 맡는 ESP32 DevKitC V4 펌웨어다. 게이트웨이와는 USB 시리얼로 feedback-3 계약(develop 의 `docs/contracts/gateway-feedback-3.md`)을 따른다.

## 지금 상태

- feedback-3 프레임 층(CRC-16/CCITT-FALSE, COBS, 프레임 묶기·풀기, 수신 스트림 분리)과 payload 9종(HELLO, HELLO_ACK, CLOCK_PROBE, CLOCK_REPLY, CLOCK_INSTALL, CLOCK_ACK, SENSOR, COMMAND, REPORT)의 pack/unpack 이 있다.
- 세션과 시각 설치에 답한다. HELLO → HELLO_ACK, CLOCK_PROBE → CLOCK_REPLY, CLOCK_INSTALL → CLOCK_ACK 이고, 설치한 lease 의 기한(cutoff)은 하드웨어 타이머가 지킨다.
- COMMAND·REPORT·SENSOR 주기 송신과 모터·서보 구동은 아직 없다. 그래서 기한이 지나도 끊을 출력이 없고 PWM1·PWM2 는 늘 Low 다.
- 알리는 capability 는 `0x0080`(CLOCK4_LATCH) 하나다. Pi 는 `0x01f7` 을 모두 요구하므로 지금 펌웨어의 HELLO_ACK 를 받아들이지 않는다. 명령 처리가 들어가야 Pi 와 끝까지 연결된다.
- ToF(`TOF_FRAGMENT3`)는 계약에서 설계 전용이라 넣지 않았다.
- 실보드(ESP32 DevKitC, USB 시리얼)에서 세션·시각 설치, 기한 만료, 손으로 굴린 바퀴의 엔코더 누계, 전원을 다시 넣은 뒤의 새 `device_boot_session` 을 확인했다(아래 "확인 기록").

## 구조

```text
xenova_esp32/                 Arduino 스케치 폴더 (폴더 이름과 .ino 이름이 같아야 한다)
  xenova_esp32.ino            시리얼·시계·엔코더·타이머를 통신 처리에 잇는다
  src/device_config.h         HELLO_ACK 로 알리는 설치 설정 (capability, encoder_bits, config digest)
  src/quadrature.h            엔코더 A·B 4체배 해석
  src/feedback3/wire.*        프레임 층
  src/feedback3/payloads.*    payload 9종 배치
  src/feedback3/codes.h       계약이 정한 값 (capability bit, reason 등)
  src/feedback3/link.*        세션과 시각 설치
  src/feedback3/cutoff.*      기한을 타이머 alarm 하나로 지키는 논리
test/host/                    PC 호스트 시험 (ESP32 없이)
  CMakeLists.txt
  test_feedback3.c            프레임·payload 를 고정 vector 와 대조
  test_link.c                 세션·시각 설치·cutoff (가짜 시계·엔코더·alarm)
  test_quadrature.c           엔코더 해석
  sim_link.c                  통신 처리를 PC 에서 돌리는 대역 (점검 도구용)
  test_tools.py               점검 도구의 판정을 가짜 장치로 시험
  analyze_unpack.c            unpack 정적 분석 (gcc -fanalyzer, 컴파일만)
  check.h                     시험이 같이 쓰는 검사 매크로
  gen_vectors.py              고정 vector JSON 을 C 시험 데이터로 바꾼다
  gateway-feedback-3.json     계약 고정 vector 의 복사본
tools/                        PC 쪽 점검 도구 (Python)
  fb3.py                      프레임·payload 와 시리얼·시뮬레이터 통로
  link_bench.py               PC 가 Pi 역할을 하며 세션·시각 설치를 점검
  cutoff_stress.py            cutoff alarm 을 처리 순간에 붙인 기한으로 되풀이해 거는 시험
  pi_link_check.py            실제 Pi Gateway 코드와 맞물려 보는 시험
```

`src/feedback3` 는 C 로 짜서 Arduino 헤더 없이 PC 에서도 그대로 컴파일된다. 시계·센서·타이머는 함수 포인터(port)로 받고, 스케치(C++)가 실제 하드웨어를 잇는다.

## ESP32 빌드

Arduino IDE 2 에서 `xenova_esp32/xenova_esp32.ino` 를 열고 보드 `ESP32 Dev Module`(esp32 by Espressif Systems 3.x)로 컴파일·업로드한다. 명령줄로는 다음과 같다.

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 vehicle/ESP32/xenova_esp32
```

## 세션과 시각 설치

### HELLO_ACK 로 알리는 것

| 항목 | 값 |
|---|---|
| `device_boot_session` | 켤 때마다 `esp_fill_random` 16 B 로 만든 RFC 4122 version 4 UUID. version·variant bit 때문에 NIL 이 될 수 없다. 플래시에 저장하지 않는다. Wi-Fi·BT 를 켜지 않으므로 부트로더가 넣어 둔 entropy 에서 이어지는 난수다 |
| `capabilities` | `0x0080`. 구현한 기능만 알린다 |
| `encoder_bits` | 32. 누계는 부호 있는 32비트이고 넘치면 돌아간다. Pi 의 `OdometryConfig.encoder_bits` 도 32 여야 한다 |
| `vehicle_config_sha256`, `firmware_sha256` | 아래 "digest 제안" |

### 프레임별 동작

- **HELLO**: 버전 3 만 받는다. 등록 config digest 가 같아야 session 을 맺는다. 다르면 이쪽 digest 를 담은 HELLO_ACK 는 보내지만 session 은 맺지 않아서, 뒤따르는 프레임에 답하지 않는다. 같은 `pi_session` 의 HELLO 가 다시 오면 상태를 그대로 두고 같은 HELLO_ACK 를 보낸다. 새 `pi_session` 이면 앞 session 의 probe 기록과 lease 를 버리고 타이머도 푼다.
- **CLOCK_PROBE**: 현재 session 의 것만 받는다. `probe_sequence` 는 앞서 답한 것보다 커야 한다. 프레임을 받은 뒤에 양쪽 엔코더 누계와 시각을 인터럽트를 막고 한 번에 읽어 그 표본으로 답한다. `d2` 는 프레임의 끝을 읽은 시각, `d3` 는 응답을 묶기 직전 시각이다. `source_sample` 은 부팅 뒤 1 부터 늘고 session 이 바뀌어도 이어진다. 엔코더 누계는 켜질 때 0 이고, 해석은 그때 읽은 A·B 상태에서 시작한다. 조향 센서가 없어 quality 는 1, 조향 값과 오차는 0 이다.
- **CLOCK_INSTALL**: 아래 검사를 통과하고 기한을 타이머에 건 뒤에만 INSTALLED 로 답한다. 이미 설치한 lease 와 같은 바이트가 다시 오면 처음 보낸 CLOCK_ACK 를 그대로 다시 보내고 타이머는 건드리지 않는다.
- lease 는 두 칸이다. 세 번째가 오면 `clock_sequence` 가 가장 작은 것을 밀어낸다. 명령이 쓰고 있는 lease 를 밀어내지 않는 규칙은 명령 처리와 함께 넣는다.
- 다른 종류의 프레임에는 답하지 않는다.
- 구분자(0x00)가 오기 전의 조각은 다음 구분자까지 들고 있다. 보내는 쪽이 프레임 중간에 멈췄다가 다시 시작하면 새 프레임이 그 조각 뒤에 붙어 함께 버려진다. 보내는 쪽이 시작할 때 0x00 하나를 먼저 보내면 막을 수 있다(빈 조각은 프레임이 아니다).

CLOCK_INSTALL 을 거절할 때(status 3)의 reason 이다. session 이 다르거나 크기가 틀린 프레임에는 ACK 를 보내지 않고 수만 센다.

| 경우 | reason |
|---|---|
| 순번·시각이 0 이거나 2^63 이상, NIL epoch, lower > upper | 9 RANGE |
| 이 session 에서 쓰던 것과 다른 `clock_epoch` | 3 SESSION_MISMATCH |
| 설치한 lease 와 `clock_sequence` 는 같은데 바이트가 다름 | 5 IDEMPOTENCY_CONFLICT |
| 이미 설치한 것보다 크지 않은 `clock_sequence` | 4 SEQUENCE_REPLAY |
| 이 장치가 답한 적 없는 `probe_sequence` | 6 CLOCK_UNAVAILABLE |
| cutoff 가 그 응답의 `d3` 보다 뒤가 아님 | 17 TIMESTAMP_ORDER |
| cutoff 가 이미 지남, 또는 만료된 lease 의 재전송 | 7 CLOCK_EXPIRED |
| 타이머에 걸지 못함, 기한에 alarm 이 울리지 않음 | 11 DEVICE_FAULT |

계약은 reason 의 이름만 정하고 어느 경우에 무엇을 쓸지는 적지 않았다. 위 배정은 이 펌웨어가 고른 것이라 플랫폼 파트와 맞춰야 한다. Pi 는 지금 status 1·reason 0 만 성공으로 받고 나머지 값은 구분하지 않는다.

### cutoff 타이머

- 장치 시계와 타이머가 같은 카운터다. GPTimer 하나를 1 µs 단위 64비트로 돌리고, `cutoff_device_us` 를 변환 없이 alarm 값으로 쓴다.
- lease 를 설치할 때 가장 이른 기한에 alarm 을 맞춘다. 맞춘 뒤 시각을 다시 읽어 아직 기한 전일 때만 INSTALLED 로 답한다.
- 이 보드의 GPTimer 는 이미 지난 값에 맞춘 alarm 도 곧바로 울린다(확인 기록). `cutoff.c` 는 그것에 기대지 않고, 맞추는 사이 기한이 지났으면 그 자리에서 만료 처리한다.
- alarm 인터럽트가 기한이 지난 lease 를 만료로 바꾸고 다음 기한에 alarm 을 다시 맞춘다. 메인 루프가 멈춰 있어도 된다.
- alarm 인터럽트는 플래시에 쓰는 동안에는 미뤄진다(만료 처리 코드가 IRAM 에 있지 않다). 지금 펌웨어는 켜진 뒤 플래시에 쓰지 않는다. 쓰게 되면 그동안 만료 처리가 늦어지고, 1 ms 를 넘기면 아래의 타이머 고장으로 굳는다. 이 경우는 시험하지 않았다.
- 기한을 1 ms 넘기도록 만료 처리가 안 된 lease 가 있으면 alarm 이 울리지 않은 것이므로 타이머 고장으로 본다. lease 를 조회하거나 바꾸거나 지울 때마다 걸려 있는 기한을 모두 검사한다. 한 번 고장으로 굳으면 남은 lease 도 쓸 수 없고, session 이 바뀌어도 다시 켤 때까지 새 lease 를 받지 않는다(DEVICE_FAULT).
- 지금은 만료 때 끊을 출력이 없다. 출력 차단과 `J_cutoff`·`J_apply` 실측은 명령 처리 작업에서 한다.

### digest 제안

계약 §4 가 MCU 쪽에 맡긴 항목이다. 플랫폼 파트와 합의해 등록하기 전의 제안이다.

- `firmware_sha256`: 링크된 ELF 파일의 SHA-256. Arduino 빌드가 이미지를 만들 때(esptool `elf2image --elf-sha256-offset 0xb0`) app description 에 적어 넣고, 펌웨어는 그 값을 읽어 보낸다. ELF 안의 그 자리는 비어 있어서 자기 해시를 자기 안에 넣는 순환이 없다. 등록값은 빌드한 사람이 `sha256sum <빌드 폴더>/xenova_esp32.ino.elf` 로 구한다. Arduino 코어가 컴파일 시각을 이미지에 넣기 때문에 같은 소스를 다시 빌드해도 값이 달라진다. 보드에 올린 바로 그 빌드에서 구해야 하고, 다시 빌드해 올리면 등록값도 바꿔야 한다. 펌웨어가 플래시를 다시 읽어 검증하는 것은 아니므로 자기보고 값이다.
- `vehicle_config_sha256`: Pi 설치 profile 과 MCU 가 함께 쓰는 설정 문서(엔코더 폭, `J_apply`·`J_cutoff`, 시각 오차 같은 실측값)를 정해진 바이트 표현으로 고정하고 그 SHA-256 을 쓰자는 제안이다. MCU 는 같은 문서에서 만든 헤더로 값을 빌드에 넣어 digest 와 실제 값이 어긋나지 않게 한다. 아직 MCU 에 넣을 실측값이 없어서, 정해질 때까지는 `src/device_config.h` 의 임시값(문자열 `xenova-esp32-config-dev-1` 의 SHA-256)을 쓴다.

## 호스트 시험

Jenkins 의 Vehicle C Host Test 와 같은 명령이다(gcc, ASan·UBSan).

```sh
cmake -S vehicle/ESP32/test/host -B target/c-host/esp32
cmake --build target/c-host/esp32
ctest --test-dir target/c-host/esp32 --output-on-failure
```

ASan 을 쓸 수 없는 환경(Windows 등)에서는 configure 에 `-DFB3_SANITIZE=OFF` 를 준다.

시험하는 것:

- `esp32_feedback3`: 고정 vector 의 제품 프레임 20개(payload·raw·wire 바이트, 묶기·풀기, 한 바이트씩 넣은 스트림 분리, 필드 값), 손상 프레임 6개의 거절 이유와 그 뒤 회복, CRC 기준값, COBS 경계, payload 240 B 상한
- `esp32_link`: 세션·시각 설치의 정상 응답과 위 거절 경우, 같은 바이트 재전송, 새 `pi_session`, lease 두 칸, 기한에 맞춘 만료, 타이머 고장. 고정 vector 의 HELLO → CLOCK_PROBE → CLOCK_INSTALL 을 넣으면 vector 의 HELLO_ACK·CLOCK_REPLY·CLOCK_ACK 와 같은 바이트가 나오는지도 본다
- `esp32_quadrature`: 엔코더 해석이 조립 안내서 단계 K 의 bench 스케치와 같은지, 네 가지 A·B 상태 어디서 켜져도 첫 움직임의 방향이 맞는지
- `esp32_link_bench`: 아래 `link_bench.py` 를 `sim_link` 에 돌린다. 실보드에서 쓰는 것과 같은 도구다
- `esp32_tools`: `cutoff_stress.py` 와 `pi_link_check.py` 의 판정을 가짜 장치로 본다. 답이 끊기거나, 타이머에 걸린 기한이 하나도 없거나, 초반 lease 뒤로 새 lease 가 없을 때 통과로 끝나지 않는지 본다
- gcc 12 이상이면 `analyze_unpack.c` 를 `-fanalyzer` 로 컴파일해, 출력 구조체를 초기화하지 않고 unpack 해도 그 값을 읽지 않는지 본다

호스트 시험의 시계·엔코더·alarm 은 가짜다. 실제 GPTimer 와 인터럽트는 실보드에서만 확인된다.

## 점검 도구

### 실보드 점검 (`tools/link_bench.py`)

PC 가 Pi 역할을 하며 HELLO, CLOCK_PROBE, CLOCK_INSTALL 을 보내고 응답을 계약과 대조한다. 모터·서보는 움직이지 않으니 XT60 은 연결하지 않아도 된다.

```sh
pip install pyserial
python vehicle/ESP32/tools/link_bench.py --port COM8
```

- 펌웨어를 올린 뒤 실행한다. PuTTY 같은 다른 시리얼 창은 닫는다.
- 끝줄이 `link_bench: … checks, 0 failed` 이면 통과다.
- `기한 뒤 재전송은 REJECTED CLOCK_EXPIRED` 가 `ok` 이면 타이머 인터럽트가 기한에 만료를 처리한 것이다. DEVICE_FAULT 로 나오면 alarm 이 울리지 않은 것이다.
- EN 버튼으로 리셋하고 한 번 더 돌려 `device_boot_session` 이 달라지는지 본다.
- `firmware_sha256` 이 `sha256sum <빌드 폴더>/xenova_esp32.ino.elf` 와 같은지 본다.
- 도구는 포트를 열면 0x00 하나를 먼저 보낸다. 앞서 쓰던 프로그램이 프레임 중간에 끊겨 장치에 조각이 남아 있어도 첫 프레임이 버려지지 않는다.
- `깨진 프레임` 이 0개가 아니면 응답이 CRC·COBS 가 맞지 않게 도착한 것이다. 그 응답을 기다리던 검사도 함께 실패하며, 도구가 그 조각을 16진수로 보여 준다.

이 PC(Windows 11, 보드의 CP2102, Silicon Labs 드라이버 11.6.0)에서는 장치가 보낸 응답 가운데 32 B 가 0xFF 64 B 로 바뀌어 도착하는 일이 있었다. 2026-11-11 에는 수천 번에 몇 번꼴이었고, 포트를 연 직후의 첫 응답에 몰려서 난 때도 있었다. 버린 조각을 알리게 한 시험용 빌드로 `cutoff_stress.py` 를 3000번 돌렸을 때 장치가 버린 요청은 없었고, 답을 받지 못한 다섯 번 동안 PC 에는 깨진 조각이 넷 도착했다(하나는 응답 둘이 붙은 길이였다). 원인은 찾지 못했고 Pi 에서도 그런지는 확인하지 못했다. 이런 조각이 보이면 다시 돌린다.

### 기한 만료 되풀이 시험 (`tools/cutoff_stress.py`)

CLOCK_INSTALL 의 cutoff 를 장치가 그 프레임을 처리하는 순간의 앞뒤 1 ms 안에 오게 잡아 되풀이해 건다. 네 가지 시나리오(기한 하나, 먼 기한을 먼저 건 뒤 하나 더, 기한 둘을 차례로, 수십 us 차이인 기한 둘)를 2:1:1:1 로 나눈 목표 횟수만큼 검증한다.

- 검증 한 번은 건 기한이 모두 타이머에 걸렸고(INSTALLED), 기한 뒤 재전송에 CLOCK_EXPIRED 가 온 것까지 본 것이다. 기한 둘을 건 시나리오는 앞 기한이 끝난 뒤에 뒤 기한이 아직 살아 있는 것도 봐야 한다(수십 us 차이인 둘은 사이에 볼 틈이 없어 뺀다).
- 처리 순간에 붙인 기한이 이미 지나 있어서 그 자리에서 CLOCK_EXPIRED 로 거절된 것은 타이머에 걸린 적이 없다. 따로 세고 검증 횟수에 넣지 않는다.
- 끝줄은 셋 가운데 하나다. `통과` 는 목표를 모두 채웠고 틀린 답이 없었다는 뜻이다. 정한 시도 안에 목표를 못 채웠거나 답이 잇달아 10번 오지 않으면 `미완료`(종료 코드 2), DEVICE_FAULT 처럼 틀린 답이 오면 `실패`(종료 코드 1)다.
- `INSTALLED 로 답할 때 cutoff 까지 남아 있던 시간` 은 장치가 CLOCK_ACK 를 만든 시각으로 잰 것이다. alarm 인터럽트의 지연을 잰 값이 아니다.

```sh
python vehicle/ESP32/tools/cutoff_stress.py --port COM8 --trials 1000
```

1000번에 3분쯤 걸린다. `--sim` 으로 시뮬레이터에도 돌릴 수 있지만, 시뮬레이터의 alarm 은 실제 타이머가 아니다.

### Pi Gateway 코드와 맞물려 보기 (`tools/pi_link_check.py`)

실제 `xenova_gateway.feedback3` 을 PC 에서 띄워 `socket://` 로 이 도구의 중계에 붙이고, 중계가 시뮬레이터나 실보드와 바이트를 주고받는다. 두 가지를 본다.

1. 거절 확인: 펌웨어가 알리는 capability 그대로면 Pi 가 HELLO_ACK 를 받아들이지 않는다.
2. 부분 호환성 시험: 중계가 HELLO_ACK 의 capability 에 Pi 필수 bit 를 채워 넣으면, Pi 가 CLOCK_PROBE 를 보내고 CLOCK_REPLY 로 lease 를 만들어 CLOCK_INSTALL 하고 CLOCK_ACK 를 받아들인다. 첫 lease 뒤로 `--seconds`(기본 3초, 최소 2초) 동안 계속 주고받게 두고, 그동안 INSTALLED 응답이 0.8초 넘게 끊기지 않는지, Pi 가 쓸 수 있는 lease 가 끊기지 않는지, 끝날 때에도 쓸 수 있는 lease 가 있는지 본다. 초반에만 성공하고 그 뒤 답이 끊기면 실패다. capability 를 고쳐 넣은 시험이므로 실제 펌웨어와 Pi 가 끝까지 연결됐다는 뜻이 아니다.

```sh
python vehicle/ESP32/tools/pi_link_check.py --platform <platform 폴더> --sim <빌드 폴더>/sim_link
```

`protobuf==6.33.6`, `paho-mqtt`, `pyserial` 이 필요하다. vehicle 브랜치에는 아직 feedback-3 게이트웨이 코드가 없어서 develop 을 따로 꺼낸 폴더의 `platform` 을 `--platform` 으로 준다. `--sim` 대신 `--port` 를 주면 실보드와 맞물린다. 오래 보려면 `--seconds 30` 처럼 준다.

## 확인 기록

| 날짜 | 범위 | 결과 |
|---|---|---|
| 2026-11-11 | 호스트 시험(Windows, zig cc, UBSan) | `esp32_feedback3` 600 검사, `esp32_link` 720 검사, `esp32_quadrature` 43 검사, `esp32_link_bench` 35 검사, `esp32_tools` 41 검사 통과 |
| 2026-11-11 | 스케치 컴파일(Arduino-ESP32 3.3.12, 경고 모두 켬) | 경고 없음. 빌드한 bin 의 0xb0 값이 ELF 의 SHA-256 과 같음 |
| 2026-11-10 | Pi 거절 확인(`sim_link`, develop `25c79b21` 의 게이트웨이 코드) | 펌웨어 capability `0x0080` 그대로면 Pi 가 ready 가 되지 않고 handshake 시간 초과로 끝남. CLOCK_PROBE 는 나가지 않음 |
| 2026-11-10 | Pi 부분 호환성 시험(같은 구성, 중계가 capability 에 `0x0177` 을 채움) | CLOCK_PROBE 4·CLOCK_REPLY 4·CLOCK_INSTALL 6·CLOCK_ACK 6(모두 INSTALLED), Pi 가 lease 를 받아들임. 종단 연결 완료가 아니다 |
| 2026-11-11 | 실보드 `link_bench.py --port`(ESP32-D0WD-V3, USB 시리얼) | 35 검사 통과. `firmware_sha256` 이 올린 ELF 의 SHA-256 과 같음. lease 500 ms 와 3초 모두 기한 뒤 재전송에 CLOCK_EXPIRED |
| 2026-11-11 | 실보드를 EN 으로 8번 리셋 | `device_boot_session` 8개가 모두 다름. 리셋 뒤 0.1초쯤에 보낸 HELLO 부터 답하고 그 전 것은 사라짐. 부팅 문구 뒤의 첫 응답이 온전히 풀림 |
| 2026-11-11 | 실보드 `cutoff_stress.py --trials 3000` | 통과. 타이머에 걸렸다가 제때 만료된 것까지 확인한 것이 기한 하나 1200번, 먼 기한 먼저·차례로·수십 us 차이로 각 600번(시도 4386번). DEVICE_FAULT 없음. 처리 순간에 붙인 기한이 이미 지나 있어 그 자리에서 거절된 1384번은 타이머에 걸리지 않았으므로 여기에 넣지 않았다. 장치가 INSTALLED 로 답할 때 cutoff 까지 남아 있던 시간이 20 us 미만이던 것이 31번(최소 2 us)인데, 이것은 alarm 인터럽트의 지연을 잰 값이 아니다 |
| 2026-11-11 | 실보드, 버린 조각을 알리게 한 시험용 빌드로 기한을 걸고 만료를 확인하기를 3000번 | alarm 이 잇달아 울리는 동안에도 장치가 버린 요청은 없음 |
| 2026-11-11 | 실보드, 장치 시계의 아래 32비트가 넘어가는 순간(켜진 지 12초 뒤에 넘어가게 한 시험용 빌드, 6번) | 넘어가기 전, 그 순간, 그 뒤에 둔 기한이 모두 제때 만료. 넘어가기 전에 맞춘 alarm 이 넘어간 뒤에 울림 |
| 2026-11-11 | 실보드 GPTimer 의 alarm(시험용 스케치) | 1 us 전부터 1초 전까지 이미 지난 값에 맞춘 alarm 이 모두 곧바로 울림. 앞선 값은 그 시각에 울림 |
| 2026-11-11 | 실보드에 깨진 입력 | 구분자 없는 3000 B, CRC 나 길이가 틀린 HELLO, 빈 구분자 50개 뒤에도 HELLO 에 답함. 잇달아 보낸 HELLO 20개에 순서대로 답함 |
| 2026-11-11 | 실보드 `pi_link_check.py --port --seconds 30`(develop `25c79b21` 의 게이트웨이 코드) | 거절 확인 통과. 부분 호환성 시험은 첫 lease 뒤 30초 동안 새 lease 148개가 이어졌고 INSTALLED 응답의 가장 긴 공백이 0.59초, Pi 에 쓸 수 있는 lease 가 끊긴 적이 없고 끝날 때에도 있음. CLOCK_PROBE 151·CLOCK_REPLY 150(하나는 PC 에 깨져 도착), CLOCK_INSTALL 296·CLOCK_ACK 296(모두 INSTALLED), Pi 가 버린 프레임 0. 종단 연결 완료가 아니다 |
| 2026-11-11 | 실보드, 바퀴를 손으로 한 바퀴씩 | 오른쪽을 앞으로 굴리면 +1334, 왼쪽을 뒤로 굴리면 −1333. 그 전에 왼쪽만 한 바퀴 굴렸을 때는 왼쪽 +1334, 오른쪽 0. 조립 안내서 단계 K 기록의 1회전 1320 과 1 % 차이 |
| 2026-11-11 | 실보드, USB 를 뺐다 꽂기 2번 | `device_boot_session` 이 그때마다 달랐고, 전원을 새로 넣은 두 번끼리도 달랐음. 누계는 0, 표본 순번은 1 부터 다시 시작 |

아직 확인하지 않은 것: 타이머 고장으로 굳는 경로(실보드에서는 일으키지 못해 호스트 시험만 있다), 모터로 빠르게 돌 때의 엔코더 누계, 켜진 직후 첫 움직임 한 칸의 방향(손으로 굴려서는 가릴 수 없어 호스트 시험만 있다), Pi(Linux)의 USB 시리얼, Linux gcc 와 ASan(Jenkins 에서 돈다).

## 고정 vector 복사본

`test/host/gateway-feedback-3.json` 은 develop 의 `docs/contracts/vectors/gateway-feedback-3.json`(SHA-256 `0a0d35c2edf5da2b…`)과 같은 파일이다. vehicle 브랜치에 원본이 없어서 시험 폴더에 둔다. 원본이 저장소에 있으면 cmake 가 두 파일이 같은지 검사하고, 다르면 configure 를 멈춘다. 원본이 바뀌면 복사본을 다시 복사한다.

## 핀

조립 안내서(`docs/vehicle/assembly-guide.md`) 6장의 연결표를 따른다. 지금 스케치는 PWM1(25)·PWM2(26)을 Low 로 두고, 엔코더(왼쪽 18·19, 오른쪽 22·23)를 읽는다.
