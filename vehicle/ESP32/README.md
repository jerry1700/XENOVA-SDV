# ESP32 — 차량 장치 펌웨어

RC카의 모터·서보·엔코더를 맡는 ESP32 DevKitC V4 펌웨어다. 게이트웨이와는 USB 시리얼로 feedback-3 계약(develop 의 `docs/contracts/gateway-feedback-3.md`)을 따른다.

## 지금 상태

- feedback-3 프레임 층(CRC-16/CCITT-FALSE, COBS, 프레임 묶기·풀기, 수신 스트림 분리)과 payload 9종(HELLO, HELLO_ACK, CLOCK_PROBE, CLOCK_REPLY, CLOCK_INSTALL, CLOCK_ACK, SENSOR, COMMAND, REPORT)의 pack/unpack 이 있다.
- 세션과 시각 설치에 답한다. HELLO → HELLO_ACK, CLOCK_PROBE → CLOCK_REPLY, CLOCK_INSTALL → CLOCK_ACK 이고, 설치한 lease 의 기한(cutoff)은 하드웨어 타이머가 지킨다.
- 설치한 lease 가 살아 있는 동안 엔코더 표본을 SENSOR 로 20 ms 마다 보낸다.
- COMMAND 의 DRIVE·STOP·HOLD_STEER 를 모터와 서보에 걸고 REPORT 로 알린다. DRIVE 의 기한이 지나거나 통신이 끊기면 타이머가 모터 출력을 끊는다. HOLD_STEER 는 모터를 돌리지 않고 조향만 건다.
- 알리는 capability 는 Pi 가 요구하는 `0x01f7` 에 정지 조향(`0x0400`), 단일 존 ToF(`0x0800`), 근거리 제동(`0x1000`)을 더한 `0x1df7` 이다. develop 의 게이트웨이 코드는 capability 를 채워 넣지 않아도 이 펌웨어의 HELLO_ACK 를 받아들이고 STOP·DRIVE·HOLD_STEER 를 주고받는다(시뮬레이터에서 확인). 실제 Pi 의 USB 로는 아직 연결해 보지 않았다.
- 시리얼 속도는 460800 bps 다. 개발용 기본값이고, 운영값은 실제 Pi 의 USB 에서 SENSOR 와 REPORT 가 함께 흐를 때 확인해서 정한다.
- 실보드에서 바퀴를 띄운 채 전진·후진·정지와, 명령이 끊겼을 때 출력이 스스로 끊기는 것을 엔코더로 확인했다. 조향은 서보 전원을 넣고 눈으로 봐서 + 가 왼쪽, − 가 오른쪽인 것까지 확인했다. **조향 각도, 바닥 주행, 실제 Pi 의 USB 연결은 아직 확인하지 않았다.** 속도를 PWM 으로, 조향을 서보 펄스로 바꾸는 값은 맞추기 전이다.
- 앞·뒤 ToF 거리 센서(VL53L0X)를 읽어 lease 가 살아 있는 동안 TOF_SAMPLE 로 보낸다. 그 관측으로 가까운 물체를 판단해 막힌 방향의 출력을 끊고 그 방향의 DRIVE 를 받지 않는다(근거리 제동). 근거리 제동은 호스트 시험과 시뮬레이터로 확인했고 **실보드에서는 아직 확인하지 않았다.** Pi 쪽은 그것을 받는 게이트웨이 코드(S15P31E110-465)가 있어야 한다.
- 실보드(ESP32 DevKitC, USB 시리얼)에서 세션·시각 설치, 기한 만료, SENSOR 흐름, 손으로 굴린 바퀴의 엔코더 누계, 전원을 다시 넣은 뒤의 새 `device_boot_session` 을 확인했다(아래 "확인 기록").

## 구조

```text
xenova_esp32/                 Arduino 스케치 폴더 (폴더 이름과 .ino 이름이 같아야 한다)
  xenova_esp32.ino            시리얼·시계·엔코더·타이머를 통신 처리에 잇는다
  src/device_config.h         HELLO_ACK 로 알리는 설치 설정 (capability, encoder_bits, config digest)
  src/quadrature.h            엔코더 A·B 4체배 해석
  src/feedback3/wire.*        프레임 층
  src/feedback3/payloads.*    payload 9종과 단일 존 ToF 관측의 배치
  src/feedback3/codes.h       계약이 정한 값 (capability bit, reason 등)
  src/feedback3/link.*        세션, 시각 설치, 주기 표본, 명령, ToF 관측 전송
  src/feedback3/cutoff.*      기한을 타이머 alarm 하나로 지키고 출력을 끊는 논리
test/host/                    PC 호스트 시험 (ESP32 없이)
  CMakeLists.txt
  test_feedback3.c            프레임·payload 를 고정 vector 와 대조
  test_link.c                 세션·시각 설치·주기 표본·명령·cutoff (가짜 시계·엔코더·alarm·출력)
  test_quadrature.c           엔코더 해석
  sim_link.c                  통신 처리를 PC 에서 돌리는 대역 (점검 도구용)
  test_tools.py               점검 도구의 판정을 가짜 장치로 시험
  analyze_unpack.c            unpack 정적 분석 (gcc -fanalyzer, 컴파일만)
  check.h                     시험이 같이 쓰는 검사 매크로
  gen_vectors.py              고정 vector JSON 을 C 시험 데이터로 바꾼다
  gateway-feedback-3.json     계약 고정 vector 의 복사본
  gateway-tof-single-1.json   단일 존 ToF vector 의 복사본
tools/                        PC 쪽 점검 도구 (Python)
  fb3.py                      프레임·payload 와 시리얼·시뮬레이터 통로
  link_bench.py               PC 가 Pi 역할을 하며 세션·시각 설치·주기 표본·명령을 점검
  cutoff_stress.py            cutoff alarm 을 처리 순간에 붙인 기한으로 되풀이해 거는 시험
  pi_link_check.py            실제 Pi Gateway 코드와 맞물려 보는 시험
```

`src/feedback3` 는 C 로 짜서 Arduino 헤더 없이 PC 에서도 그대로 컴파일된다. 시계·센서·타이머는 함수 포인터(port)로 받고, 스케치(C++)가 실제 하드웨어를 잇는다.

## ESP32 빌드

Arduino IDE 2 에서 `xenova_esp32/xenova_esp32.ino` 를 열고 보드 `ESP32 Dev Module`(esp32 by Espressif Systems 3.x)로 컴파일·업로드한다. 명령줄로는 다음과 같다.

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 vehicle/ESP32/xenova_esp32
```

ToF 를 읽는 데 **VL53L0X by Pololu** 라이브러리(1.3.1 로 확인)가 필요하다. Arduino IDE 의 Library Manager 에서 설치한다(조립 안내서 단계 C-1 의 5). 이름이 비슷한 Adafruit_VL53L0X 는 다른 라이브러리다.

## 세션과 시각 설치

### HELLO_ACK 로 알리는 것

| 항목 | 값 |
|---|---|
| `device_boot_session` | 켤 때마다 `esp_fill_random` 16 B 로 만든 RFC 4122 version 4 UUID. version·variant bit 때문에 NIL 이 될 수 없다. 플래시에 저장하지 않는다. Wi-Fi·BT 를 켜지 않으므로 부트로더가 넣어 둔 entropy 에서 이어지는 난수다 |
| `capabilities` | `0x1df7`. 구현한 기능만 알린다. Pi 필수 `0x01f7` 과 정지 조향(`0x0400`), 단일 존 ToF(`0x0800`), 근거리 제동(`0x1000`)이다. 조향 측정(`0x0008`)은 없다 |
| `encoder_bits` | 32. 누계는 부호 있는 32비트이고 넘치면 돌아간다. Pi 의 `OdometryConfig.encoder_bits` 도 32 여야 한다 |
| `vehicle_config_sha256`, `firmware_sha256` | 아래 "digest 제안" |

### 프레임별 동작

- **HELLO**: 버전 3 만 받는다. 등록 config digest 가 같아야 session 을 맺는다. 다르면 이쪽 digest 를 담은 HELLO_ACK 는 보내지만 session 은 맺지 않아서, 뒤따르는 프레임에 답하지 않는다. 같은 `pi_session` 의 HELLO 가 다시 오면 상태를 그대로 두고 같은 HELLO_ACK 를 보낸다. 새 `pi_session` 이면 앞 session 의 probe 기록과 lease 를 버리고 타이머도 푼다.
- **CLOCK_PROBE**: 현재 session 의 것만 받는다. `probe_sequence` 는 앞서 답한 것보다 커야 한다. 프레임을 받은 뒤에 양쪽 엔코더 누계와 시각을 인터럽트를 막고 한 번에 읽어 그 표본으로 답한다. `d2` 는 프레임의 끝을 읽은 시각, `d3` 는 응답을 묶기 직전 시각이다. `source_sample` 은 부팅 뒤 1 부터 늘고 session 이 바뀌어도 이어진다. 엔코더 누계는 켜질 때 0 이고, 해석은 그때 읽은 A·B 상태에서 시작한다. 조향 센서가 없어 quality 는 1, 조향 값과 오차는 0 이다. 앞 표본을 뜬 지 20 ms 가 안 됐으면 20 ms 가 될 때까지 기다렸다가 표본을 떠서 답한다(아래 "표본 사이 간격"). 그때에도 `d2` 는 프레임의 끝을 읽은 시각 그대로다.
- **CLOCK_INSTALL**: 아래 검사를 통과하고 기한을 타이머에 건 뒤에만 INSTALLED 로 답한다. 이미 설치한 lease 와 같은 바이트가 다시 오면 처음 보낸 CLOCK_ACK 를 그대로 다시 보내고 타이머는 건드리지 않는다.
- lease 는 두 칸이다. 세 번째가 오면 `clock_sequence` 가 가장 작은 것을 밀어낸다. 다만 그 lease 를 가리키는 DRIVE 가 출력에 걸려 있으면 밀어내지 않고 새 CLOCK_INSTALL 을 거절한다(6 CLOCK_UNAVAILABLE).
- COMMAND 는 아래 "명령"대로 처리한다. 그 밖의 종류에는 답하지 않는다.
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
- 타이머의 세 번째 칸은 출력의 기한이다. DRIVE 의 `cutoff_device_us` 를 여기에 걸고, 기한이 되면 alarm 인터럽트 안에서 모터 PWM 을 0 으로 내린다. 타이머가 고장으로 굳을 때도 그 자리에서 출력을 끊는다.
- 출력은 alarm 인터럽트를 막은 채로 건다. 그래서 기한의 alarm 은 출력을 다 건 뒤에야 처리되고, 기한 뒤에 출력이 켜진 채 남지 않는다.
- `J_cutoff`(기한에서 PWM 이 실제로 내려갈 때까지)와 `J_apply`(출력을 거는 데 걸리는 시간)는 아직 재지 않았다. `J_apply` 는 2 ms 로 잡아 두었다.

### digest 제안

계약 §4 가 MCU 쪽에 맡긴 항목이다. 플랫폼 파트와 합의해 등록하기 전의 제안이다.

- `firmware_sha256`: 링크된 ELF 파일의 SHA-256. Arduino 빌드가 이미지를 만들 때(esptool `elf2image --elf-sha256-offset 0xb0`) app description 에 적어 넣고, 펌웨어는 그 값을 읽어 보낸다. ELF 안의 그 자리는 비어 있어서 자기 해시를 자기 안에 넣는 순환이 없다. 등록값은 빌드한 사람이 `sha256sum <빌드 폴더>/xenova_esp32.ino.elf` 로 구한다. Arduino 코어가 컴파일 시각을 이미지에 넣기 때문에 같은 소스를 다시 빌드해도 값이 달라진다. 보드에 올린 바로 그 빌드에서 구해야 하고, 다시 빌드해 올리면 등록값도 바꿔야 한다. 펌웨어가 플래시를 다시 읽어 검증하는 것은 아니므로 자기보고 값이다.
- `vehicle_config_sha256`: Pi 설치 profile 과 MCU 가 함께 쓰는 설정 문서(엔코더 폭, `J_apply`·`J_cutoff`, 시각 오차 같은 실측값)를 정해진 바이트 표현으로 고정하고 그 SHA-256 을 쓰자는 제안이다. MCU 는 같은 문서에서 만든 헤더로 값을 빌드에 넣어 digest 와 실제 값이 어긋나지 않게 한다. 아직 MCU 에 넣을 실측값이 없어서, 정해질 때까지는 `src/device_config.h` 의 임시값(문자열 `xenova-esp32-config-dev-1` 의 SHA-256)을 쓴다.

## 주기 표본 (SENSOR)

설치한 lease 가 살아 있는 동안 엔코더 표본을 SENSOR(`0x0f15`)로 보낸다. Pi 는 이 표본으로 주행 거리와 속도를 만들고, STOP 을 적용했다는 보고가 가리키는 표본을 이 흐름에서 찾아 정지를 확인한다.

- **보내는 때**: 현재 session 에 설치돼 있고 기한 전인 lease 가 있을 때만 보낸다. lease 가 모두 만료됐거나, 타이머가 고장으로 굳었거나, 새 `pi_session` 이 와서 lease 를 버렸으면 멈춘다. 새 lease 가 설치되면 20 ms 안에 다시 보낸다.
- **가리키는 lease**: 살아 있는 lease 가운데 `clock_sequence` 가 가장 큰 것의 `clock_epoch`·`clock_sequence` 를 싣는다. Pi 는 가장 새 lease 둘만 들고 있어서, 장치가 아는 가장 새 lease 를 가리켜야 Pi 가 이미 버린 lease 에 걸리지 않는다. 표본 시각은 그 lease 의 cutoff 보다 앞이다. 표본을 먼저 뜨고 나서 lease 를 순번이 큰 것부터 조회해 고르며, 마지막으로 조회한 lease 만 쓴다. 조회는 걸려 있는 기한을 모두 검사하므로, 그 사이에 타이머 고장이 드러나면 앞서 살아 있다고 본 lease 로도 보내지 않는다.
- **표본**: CLOCK_REPLY 와 같은 방법으로 그때 새로 뜬다. `source_sample` 은 CLOCK_REPLY 와 SENSOR 가 함께 쓰는 순번 하나이고, 보낸 표본마다 1 씩 는다. 같은 표본을 다른 순번이나 시각으로 다시 보내지 않는다.
- **주기**: 앞 표본(SENSOR 든 CLOCK_REPLY 든)을 뜬 시각에서 20 ms 뒤에 다음 표본을 뜬다(장치 시계). 메인 루프가 늦게 돌면 그만큼 늦게 뜨고, 밀린 표본을 몰아서 보내지 않는다.
- **건너뛰는 경우**: 센서를 읽지 못했거나 표본이 계약 범위 밖이면 그 주기는 보내지 않고 순번도 쓰지 않는다. 한 주기 뒤에 다시 본다.

### 표본 사이 간격

엔코더 표본은 SENSOR 와 CLOCK_REPLY 두 프레임에 실린다. 어느 쪽이든 앞 표본에서 20 ms 뒤에 뜬다. 이웃한 두 표본은 종류와 상관없이 20 ms 보다 가깝지 않다.

- SENSOR 가 흐르는 동안 CLOCK_PROBE 가 오면 다음 표본 자리까지 답을 미루고, 그 자리의 표본을 CLOCK_REPLY 로 보낸다. 그 주기의 SENSOR 는 보내지 않는다. 응답은 길어야 20 ms 늦는다.
- 앞 표본에서 20 ms 가 지난 뒤에 온 CLOCK_PROBE 에는 곧바로 답한다(SENSOR 가 흐르지 않을 때가 그렇다).
- 답을 미뤄도 `d2` 는 받은 시각 그대로이고 표본과 `d3` 가 함께 늦는다. Pi 는 시각 구간의 아래를 `p1 − d2`, 위를 `도착 시각 − d3` 로 잡으므로 미룬 만큼 구간이 넓어지지 않는다.
- 미뤄 둔 probe 가 있는 동안에도 앞 표본의 REPORT 는 그 표본 바로 뒤에 가고, 표본 자리마다 하는 비상정지·타이머 고장 검사도 그대로 한다.

간격을 두는 까닭은 Pi 가 이웃한 두 표본을 견주기 때문이다.

- Pi 의 odometry 는 이웃한 두 표본의 누계 차이를 표본 시각 차이로 나눠 속도 상한과 견준다. 또 lease 마다 따로 잡은 시각 구간의 가운데 값으로 표본의 Pi 시각을 정하고, 그 값이 표본 순서대로 늘어야 한다. CLOCK_REPLY 의 표본이 SENSOR 의 표본에 바짝 붙으면 엔코더 한 칸이 큰 속도로 보이거나 Pi 시각의 순서가 뒤집힌다. 그러면 Pi 는 그 표본을 버리면서 그때까지의 최신 표본과 누적 거리도 무효로 한다(`ODOM_ENCODER_DISCONTINUITY`, `ODOM_SAMPLE_ORDER`). 간격을 두지 않은 시험용 빌드로는 이 두 오류가 실제로 나왔다(확인 기록).
- Pi 는 정지를 확인할 때 이웃한 두 표본마다 `(누계 차이 + 1) × 칸당 거리 ÷ (표본 간격 − 두 표본의 시각 오차)` 를 정지 한도와 견준다. STOP 뒤 첫 DRIVE 와 모든 HOLD_STEER 를 내보낼 때 이 확인을 거친다. CLOCK_REPLY 의 표본을 앞 표본에서 5 ms 만 띄우던 때에는 그 쌍이 확인 창에 든 동안 이 값이 한도를 넘어, Pi 가 HOLD_STEER 24개쯤 가운데 3개에서 5개를 내보내지 않았다(확인 기록). 20 ms 간격이면 누계가 그대로일 때 시각 오차가 8 ms 쯤 될 때까지 0.05 m/s 한도 아래다.

### 시리얼 부하

- SENSOR 한 프레임은 선 위에서 최대 102 B 이고 115200 bps 에서 8.9 ms 가 걸린다. 50 Hz 면 선의 44 % 를 쓴다.
- REPORT 는 130 B 이고 표본마다 하나씩 뒤따르므로 SENSOR 와 합쳐 초당 11.6 kB 다. 115200 bps 로는 실을 수 없어 460800 bps 로 올렸다(선의 25 % 쯤). 속도를 올린 것만으로 지연이 보장되지는 않는다. Pi 의 정지 확인은 이웃한 두 표본의 간격에서 두 표본의 시각 오차를 뺀 시간으로 속도를 구하므로, 시각 오차(왕복 시간의 절반쯤)가 표본 간격 20 ms 에 견줘 충분히 작아야 한다. 실제 Pi 의 USB 에서 재서 정한다.
- 스케치는 송신 버퍼를 1024 B 둔다. 송신 버퍼가 없으면 앞 프레임이 다 나가기 전에 쓴 프레임이 메인 루프를 세운다(확인 기록).

## ToF 관측 (TOF_SAMPLE)

앞·뒤 가운데의 VL53L0X 두 개를 읽어 관측 하나를 TOF_SAMPLE(`0x0f17`, 145 B) 하나로 보낸다(계약 §3·§9.1). Jetson 은 이 관측을 지도에 근거리 장애물로 덧씌운다. 같은 관측으로 근거리 제동도 한다(아래 "근거리 제동").

- **보내는 때**: SENSOR 와 같다. 현재 session 에 설치돼 있고 기한 전인 lease 가 있을 때만 보내고, 가장 새 lease 의 `clock_epoch`·`clock_sequence` 를 싣는다. lease 가 없을 때 센서가 낸 관측은 버린다. 관측 시각이 그 lease 의 cutoff 와 같거나 뒤여도 버린다.
- **우선순위**: 표본 뒤의 REPORT, 미뤄 둔 CLOCK_REPLY, SENSOR 가 나갈 때가 아닐 때에만 보낸다. 메인 루프 한 바퀴에 프레임 하나이고, 두 센서에 새 관측이 다 있으면 번갈아 보낸다.
- **센서 읽기**: I2C 는 센서가 답하지 않으면 수십 ms 를 기다리므로 메인 루프에서 읽지 않는다. 다른 코어에서 도는 task 가 5 ms 마다 두 센서를 보고, 센서마다 가장 새 관측 하나만 남긴다. 메인 루프는 그것을 집어 갈 뿐이라 센서 선이 빠져도 서지 않는다(실보드에서 앞 센서의 SDA 선을 뺀 동안 표본 간격이 그대로였다). 메인 루프는 돌 때마다 새 관측을 받아 근거리 판단에 쓰고, 보낼 것은 센서마다 가장 새 것 하나만 들고 있다가 보낸다. 보내기 전에 더 새 관측이 오면 앞의 것은 버린다.
- **측정**: 센서마다 100 ms 에 한 번, 측정 하나에 33 ms(timing budget)를 쓴다. 센서당 초에 10개쯤이다.
- **관측 시각**: 측정은 task 가 그 센서를 앞서 본 때와 끝난 것을 본 때 사이에 끝났고, 끝나기 전 33 ms 동안 이뤄졌다. `device_sample_us` 는 그 구간의 가운데이고, `sample_time_error_us` 는 구간의 절반에 읽는 시간 여유 1 ms 를 더한 값이다(평소 20 ms 쯤). 다른 센서를 다시 초기화하느라 100 ms 넘게 못 봤으면 어느 측정의 값인지 알 수 없어 그 관측은 내지 않는다. 엔코더 표본의 시각을 빌려 쓰지 않는다.
- **순번과 instance**: `source_sample` 은 센서의 instance 마다 1 부터 1 씩 는다. 센서를 초기화할 때마다(켜질 때, 답하지 않다가 다시 붙었을 때) 새 `sensor_instance`(난수 UUID)와 1 큰 `sensor_generation` 을 쓴다. 답하지 않는 센서는 끄고 1초마다 다시 초기화해 본다. 새 `pi_session` 이 와도 센서의 instance 와 순번은 이어진다.
- **주소**: 두 센서는 켜질 때 주소가 같아서(0x29) XSHUT 으로 하나씩 켜며 앞 0x30, 뒤 0x31 로 옮긴다. EN 리셋은 센서 전원을 끊지 않으므로 켜질 때 둘 다 끄고 시작한다.
- **등록 profile**: `sensor_config_revision` 은 앞·뒤 모두 1, `sensor_calibration_sha256` 은 임시값이다(`device_config.h`. ASCII 문자열 `xenova-esp32-tof-front-dev-1`, `xenova-esp32-tof-rear-dev-1` 의 SHA-256). Pi 의 ToF 선택에 같은 값을 넣어야 Pi 가 관측을 받는다. 센서를 보정한 값이 아니다.

거리와 상태는 다음과 같이 싣는다. `rows`·`cols` 는 1 이다.

| 센서의 native 상태 (`RESULT_RANGE_STATUS` 의 bit 3..6) | `status` | `distance_mm` | `quality` | `frame_status` |
|---|---|---|---|---|
| 11 측정 완료, 거리 1 mm 에서 8189 mm | 1 VALID | 읽은 거리 | `0x03` TARGET_VALID·NATIVE | `0x03` SENSOR_OK·TIMING_VALID |
| 11 측정 완료인데 거리가 8190 mm 이상 | 3 OUT_OF_RANGE | 0 | `0x02` | `0x03` |
| 11 측정 완료인데 거리가 0 | 7 BAD_RETURN | 0 | `0x02` | `0x03` |
| 4 돌아온 신호가 약함 | 2 NO_TARGET | 0 | `0x02` | `0x03` |
| 6, 9 위상이 범위 밖 | 3 OUT_OF_RANGE | 0 | `0x02` | `0x03` |
| 8, 10 최소 거리 검사 실패 | 7 BAD_RETURN | 0 | `0x02` | `0x03` |
| 1, 2, 3 센서 안의 고장 | 5 SENSOR_ERROR | 0 | `0x02` | `0x02` TIMING_VALID 만 |
| 그 밖 | 0 UNKNOWN | 0 | `0x02` | `0x02` |

native 상태의 구분은 ST 의 VL53L0X API 를 따랐다. 실보드에서는 VALID 와 NO_TARGET 만 봤고 나머지 줄은 센서로 확인하지 않았다. 거리 보정(오프셋)은 넣지 않았다. 측정 완료인데 거리가 0 인 것은 잴 물체가 없다는 뜻인지 알 수 없어 OUT_OF_RANGE 로 치지 않는다. 근거리 제동이 OUT_OF_RANGE 를 "물체 없음"으로 쓰기 때문이다.

## 근거리 제동

앞·뒤 ToF 의 관측으로 가까운 물체를 판단해, 그 방향으로 가는 출력을 스스로 끊고 그 방향의 DRIVE 를 받지 않는다. 앞 센서는 앞으로 가는 것을, 뒤 센서는 뒤로 가는 것을 막는다. capability `0x1000` 으로 알린다. 알리는 값은 ToF 요청서(`docs/requests/2026-11-10-tof-vl53l0x-gateway-jetson.md`) 2.1 에서 차량 쪽이 정했고, Pi 쪽은 그것을 받는 게이트웨이 코드(S15P31E110-465)가 있어야 한다.

- **판단**: 메인 루프가 돌 때마다 센서의 새 관측을 받아 본다. status 가 VALID 이고 150 mm 보다 가까우면 막는다. VALID 이고 180 mm 보다 멀거나, NO_TARGET·OUT_OF_RANGE(잴 물체가 없음)면 푼다. 그 사이의 거리는 앞선 판단을 그대로 둔다. 그 밖의 status 와 계약 밖 값, 시각이 지금보다 뒤이거나 앞선 판단보다 새롭지 않은 관측은 판단에 쓰지 않는다.
- **관측이 없을 때**: 판단에 쓴 마지막 관측의 시각에서 250 ms 가 지나면 그 방향을 막는다. 켜진 뒤 첫 관측이 오기 전에도 막혀 있다. 관측이 다시 오면 그 값으로 판단한다. 장치를 다시 시작할 때까지 막아 두지 않는다. 센서가 답하지 않으면 펌웨어가 1초마다 다시 초기화하므로, 그동안 그 방향은 막혀 있다.
- **가던 중**: 막힌 방향으로 걸려 있는 출력은 다음 표본을 기다리지 않고 끊는다. 기한이 지난 명령과 같이 다뤄서 그 명령의 REPORT 를 더 보내지 않고, 물체가 치워져도 스스로 다시 걸지 않는다. STOP, HOLD_STEER, 속도 0 인 DRIVE, 반대 방향으로 걸려 있는 출력은 끊지 않는다.
- **거절**: 막힌 방향의 DRIVE 는 REPORT 의 REJECTED 와 reason 21 `PROXIMITY_BLOCKED` 로 거절한다. 다른 검사를 모두 지나 그 밖에는 걸렸을 명령만 이 이유로 거절한다. 타이머 고장이면 FAULT, 기한이 모자라면 COMMAND_EXPIRED 가 먼저다. 이 이유의 거절만 Pi 가 고장으로 보지 않기 때문이다. STOP 과 HOLD_STEER, 속도 0 인 DRIVE 는 막힌 중에도 받는다.
- **알림**: 막혀 있는 동안 SENSOR 와 CLOCK_REPLY 의 표본 `quality` 에 bit 3(`0x08`, 앞이 막힘)·bit 4(`0x10`, 뒤가 막힘)를 켠다. 풀리면 끈다. 물체 때문인지 관측이 없어서인지는 TOF_SAMPLE 로 가린다.
- **방향 전환**: 근거리 정지는 STOP 이 아니다. 뒤로 빠지려면 Pi 가 STOP 을 보내 APPLIED 를 받은 뒤 반대 방향 DRIVE 를 보낸다. 물체가 치워지면 같은 방향의 DRIVE 는 STOP 없이 다시 받는다.
- **등록하지 않은 센서**: `sensor_config_revision` 이 0 인 자리의 방향은 막지 않는다. 근거리 제동을 알리지 않은 빌드는 판단하지 않는다.
- **기준 거리**: `device_config.h` 의 `XENOVA_PROXIMITY_STOP_MM`(150), `XENOVA_PROXIMITY_CLEAR_MM`(180), `XENOVA_PROXIMITY_STALE_US`(250 ms)다. 플랫폼 파트와 15 cm 로 정했다(2026-11-12). 시연 트랙의 차선 폭이 28.5 cm 라, 차량 제어 명세 §4.3 의 30 cm 로는 장애물을 피하거나 주차할 때 차가 먼저 서기 때문이다. 제동 거리를 재서 정한 값이 아니다. **경로가 센서 정면 15 cm 안으로 들어가면 차가 선다.**
- **감지 지연**: 센서가 100 ms 마다 재고 측정 하나에 33 ms 가 걸리므로, 갑자기 들어온 물체는 길게는 0.13초쯤 뒤에 판단에 닿는다(계산한 값이고 실보드에서 재지 않았다). 그 뒤 출력을 끊는 것은 메인 루프 한 바퀴 안이다.

## 명령 (COMMAND·REPORT)

DRIVE(1), STOP(2), HOLD_STEER(3)를 받는다. 받아들인 명령은 출력에 적용한 뒤에 APPLIED 로 알린다. 받아들이는 것과 적용이 한 번에 일어나므로 ACCEPTED 는 따로 보내지 않는다.

- **STOP**: 현재 session 의 STOP 이면 검사에 앞서 모터 출력부터 끊는다. 시각 lease 가 없어도 받는다. 기한·속도·조향 칸이 0 이 아니면 거절하지만 출력은 이미 끊겨 있다. 낮은 `generation` 의 STOP 도 적용하고 `generation` 은 내리지 않는다. 마지막으로 적용한 STOP 은 따로 기억한다.
- **DRIVE**: 마지막으로 적용한 STOP 을 `stop_command_sequence` 로 가리켜야 한다. 그 STOP 뒤로 가던 방향과 반대면 거절한다(새 STOP 이 있어야 한다). 속도 0 인 DRIVE 는 방향으로 치지 않는다. 가리키는 lease 가 설치돼 있어야 하고, 명령의 `expires_pi_us`·`cutoff_device_us` 가 그 lease 의 것을 넘지 못한다. 기한까지 `J_apply` 넘게 남아 있을 때만 출력에 걸고 그 기한을 타이머에 건다. 거절된 DRIVE 는 앞서 걸려 있던 출력과 기한을 건드리지 않는다.
- **HOLD_STEER**: 멈춘 채 조향만 바꾸는 명령이다. 속도 칸이 0 이어야 하고(아니면 RANGE), 그 밖의 검사는 DRIVE 와 같다. 마지막으로 적용한 STOP 을 가리켜야 하고 lease 와 기한을 본다. 모터 PWM 은 0 으로 두고 서보 펄스만 바꾸며, 명령의 기한을 출력의 타이머에 건다. 가던 방향으로 치지 않으므로 STOP 뒤의 HOLD_STEER 다음에는 어느 방향의 DRIVE 든 받는다. 걸려 있던 DRIVE 뒤에 오면 모터 출력을 내리고 기한을 제 것으로 바꾼다. 정지 조향을 알리지 않게 설정한 빌드(`capabilities` 에 `0x0400` 이 없음)는 UNSUPPORTED_PROFILE 로 거절한다.
- **다시 온 명령**: 받아들인 명령 여덟 개와 마지막 STOP 을 기억한다. 같은 순번에 같은 바이트면 처음 보낸 REPORT 만 그대로 다시 보내고, 출력에 다시 걸거나 기한을 늘리지 않는다. 같은 순번에 다른 바이트면 거절한다. 거절한 명령은 기억하지 않고 순번도 쓰지 않는다.
- **표본마다 REPORT**: 지금 명령(출력에 걸려 있는 DRIVE·HOLD_STEER, 없으면 마지막 STOP)의 APPLIED 를 SENSOR 나 CLOCK_REPLY 를 보낼 때마다 그 표본을 가리켜 다시 보낸다. Pi 는 STOP 의 APPLIED 보고가 가리키는 표본이 자기가 받은 가장 새 표본일 때 정지를 확인한다. DRIVE 와 HOLD_STEER 는 기한까지만 알리고, 기한이 5 ms 안으로 남았으면 그 REPORT 가 Pi 의 기한 뒤에 닿을 수 있어 알리지 않는다. 기한이 지난 뒤에는 새 명령이 올 때까지 REPORT 를 보내지 않는다.
- **측정**: REPORT 가 가리키는 표본은 마지막으로 보낸 표본이다. 속도는 그 표본과 앞 표본의 누계 차이에서 구하고(칸당 0.164 mm, 보정 전), 양쪽 누계가 100 ms 동안 그대로면 `stationary` 를 1 로 알린다. 표본이 100 ms 넘게 끊겼다 이어지면 다시 잰다. 조향 센서가 없어 조향은 싣지 않는다(flags 5). 거절과 고장 보고에는 측정을 싣지 않는다.
- **비상정지 입력**(13번): 눌려 있으면 출력을 끊고 DRIVE 를 거절하며, 풀린 뒤에도 새 STOP 을 받기 전에는 DRIVE 를 받지 않는다. 눌린 것을 순번 0·종류 0 의 FAULT(14 E_STOP)로 한 번 알린다. 지금 차에는 이 입력이 GND 에 묶여 있고 비상정지는 릴레이로 모터 전원을 끊는다.
- **타이머 고장**: 순번 0·종류 0 의 FAULT(11 DEVICE_FAULT)로 한 번 알린다. 그 뒤로는 DRIVE 를 받지 않는다.
- 새 `pi_session` 이 오면 출력을 끊고 명령 기록을 버린다.

거절할 때의 REPORT 는 status 3 이고 reason 은 다음과 같다. 출력을 걸지 못했거나 타이머가 고장이면 status 4 다. Pi 는 지금 REJECTED 나 FAULT 를 하나라도 받으면 그 연결을 고장으로 굳히므로(게이트웨이를 다시 띄워야 풀린다), 이 배정을 플랫폼 파트와 맞춰야 한다.

| 경우 | reason |
|---|---|
| 순번·generation 이 0 이거나 범위 밖, reserved 가 0 이 아님, 계약 밖 reason, STOP 의 0 이어야 할 칸, 속도·조향·기한이 범위 밖, 속도가 실린 HOLD_STEER | 9 RANGE |
| DRIVE·STOP·HOLD_STEER 가 아닌 종류 | 16 UNSUPPORTED_PROFILE |
| 받아들인 것보다 크지 않은, 기억에 없는 순번 | 4 SEQUENCE_REPLAY |
| 기억하고 있는 순번인데 바이트가 다름 | 5 IDEMPOTENCY_CONFLICT |
| 받아들인 것보다 낮은 generation 의 DRIVE | 15 GENERATION_REPLAY |
| 적용한 STOP 이 없음, 마지막 STOP 이 아닌 것을 가리킴, STOP 없이 방향을 바꿈, 비상정지 뒤 새 STOP 이 없음 | 10 DIRECTION_STOP_REQUIRED |
| 비상정지 입력이 눌려 있음 | 14 E_STOP |
| 설치하지 않은 lease 를 가리킴 | 6 CLOCK_UNAVAILABLE |
| 명령의 기한이 그 lease 의 기한을 넘음 | 19 DEADLINE_MISMATCH |
| 기한이 지났거나 `J_apply` 안으로 남음 | 8 COMMAND_EXPIRED |
| 출력을 걸지 못함, 타이머 고장 (status 4) | 11 DEVICE_FAULT |

### 출력

- 모터(MDD10A): PWM 25·26(20 kHz, 10비트), 방향 32·33. 속도를 듀티로 바꾸는 것은 엔코더로 되먹이지 않는 개루프이고, 1150 mm/s 를 듀티 100 % 로 어림한 값이다. 받아들이는 속도는 600 mm/s 까지로 낮게 잡았다. 바퀴를 띄운 시험에서 재서 바꿔야 한다.
- 서보: 27번(50 Hz, MCPWM 1 us 단위). 조향 0 이 1500 us 이고 1 rad 에 637 us 로 어림했다. 조향은 왼쪽이 + 이고(MQTT 계약 v1 의 `left` 는 `steer_rad` +), 이 차의 서보는 펄스가 짧을수록 왼쪽이라(조립 안내서 9.2 의 왼쪽 한계 900 us) + 일수록 펄스를 짧게 낸다. 한계는 900 us 에서 2100 us 이고 받아들이는 조향은 ±700 mrad(약 40도, 1054 us 에서 1946 us)까지다. 방향은 실보드에서 눈으로 확인했다(확인 기록). 기울기는 어림값이고 바퀴 각도를 재서 맞추기 전이다.
- 서보 펄스는 MCPWM 으로 낸다. 명령을 출력에 걸 때 모터 PWM 과 같은 자리에서(타이머 인터럽트를 막은 채) 펄스 폭을 쓰고, 그 쓰기가 성공한 뒤에 APPLIED 를 알린다. 새 폭은 다음 주기의 처음부터 나가므로 펄스가 중간에 잘리지 않고, 길어야 20 ms 뒤의 펄스부터 바뀐다. 거절됐거나 기한이 모자란 명령의 조향은 쓰지 않는다. 쓰는 데 걸리는 시간은 실보드에서 2 us 에서 19 us 였다(확인 기록).
- 서보에 LEDC 를 쓰지 않는 까닭: LEDC 는 앞서 쓴 값이 자리 잡기 전에 다시 쓰면 PWM 두 주기(50 Hz 에서 40 ms)를 멈춰 기다린다(확인 기록). 타이머 인터럽트를 막은 채로는 쓸 수 없고, 메인 루프로 미뤄 쓰면 서보에 쓰기 전에 APPLIED 가 나가며 그사이 기한이 지난 명령의 조향이 뒤늦게 나간다. 모터 채널(20 kHz)은 LEDC 그대로다. 그 주파수에서는 쓰기가 0.1 ms 를 넘지 않는다.
- 켜지면 모터 PWM 은 0 이고 서보에는 펄스를 내지 않는다. 첫 DRIVE 나 HOLD_STEER 가 올 때 서보가 움직인다. 출력을 끊을 때는 모터 PWM 만 0 으로 내리고 서보는 마지막 자리에 둔다.

## 호스트 시험

Jenkins 의 Vehicle C Host Test 와 같은 명령이다(gcc, ASan·UBSan).

```sh
cmake -S vehicle/ESP32/test/host -B target/c-host/esp32
cmake --build target/c-host/esp32
ctest --test-dir target/c-host/esp32 --output-on-failure
```

ASan 을 쓸 수 없는 환경(Windows 등)에서는 configure 에 `-DFB3_SANITIZE=OFF` 를 준다.

시험하는 것:

- `esp32_feedback3`: 단일 존 ToF vector 의 프레임 2개(payload·wire 바이트, 관측 CRC32)와 고정 vector 의 제품 프레임 20개(payload·raw·wire 바이트, 묶기·풀기, 한 바이트씩 넣은 스트림 분리, 필드 값), 손상 프레임 6개의 거절 이유와 그 뒤 회복, CRC 기준값, COBS 경계, payload 240 B 상한
- `esp32_link`: 세션·시각 설치의 정상 응답과 위 거절 경우, 같은 바이트 재전송, 새 `pi_session`, lease 두 칸, 기한에 맞춘 만료, 타이머 고장. SENSOR 는 lease 가 있을 때만 나가는지, 주기와 표본 사이 간격, CLOCK_REPLY 와 이어지는 순번, 가리키는 lease, 만료·고장·새 session 에서 멈추는지, lease 를 고르는 사이에 드러난 타이머 고장으로도 멈추는지, 쓸 수 없는 표본을 건너뛰는지 본다. ToF 는 lease 가 있을 때만 나가는지, 관측 순번·instance·generation 규칙, 계약 밖 관측을 버리는지, 표본과 REPORT 뒤에 나가는지 본다. 근거리 제동은 켜진 직후와 관측이 끊겼을 때 막혀 있는지, 기준 거리의 경계, 판단에 쓰지 않는 관측, 가던 중의 출력 차단, 막힌 방향 DRIVE 의 거절과 다른 거절 이유의 우선, STOP·HOLD_STEER 를 건드리지 않는지, 표본의 막힘 bit 를 본다. 명령은 STOP·DRIVE·HOLD_STEER 의 검사와 거절, 다시 온 명령, 방향, 기한에 맞춘 출력 차단, 표본마다의 REPORT, 정지 판단, 비상정지, 타이머 고장을 본다. 고정 vector 의 HELLO → CLOCK_PROBE → CLOCK_INSTALL 을 넣으면 vector 의 HELLO_ACK·CLOCK_REPLY·CLOCK_ACK 와 같은 바이트가 나오고, 이어서 vector 의 표본을 뜨게 하면 vector 의 SENSOR 네 개와 같은 바이트가 나오는지도 본다
- `esp32_quadrature`: 엔코더 해석이 조립 안내서 단계 K 의 bench 스케치와 같은지, 네 가지 A·B 상태 어디서 켜져도 첫 움직임의 방향이 맞는지
- `esp32_link_bench`: 아래 `link_bench.py` 를 `sim_link` 에 돌린다. 실보드에서 쓰는 것과 같은 도구다
- `esp32_steer_check`: 아래 `steer_check.py` 를 `sim_link` 에 돌린다. 속도 0 인 DRIVE 가 APPLIED 로 돌아오고, 자리마다 그 뒤에 받은 표본의 엔코더 누계가 그대로인지까지 본다
- `esp32_steer_check_stationary`: 같은 도구를 `--stationary` 로 돌린다. HOLD_STEER 가 APPLIED 로 돌아오고 엔코더 누계가 그대로인지 본다
- `esp32_tools`: 점검 도구의 판정을 가짜 장치로 본다. `steer_check.py` 는 DRIVE 가 거절되거나 답이 없을 때, 누계가 바뀌었다가 제자리로 돌아왔을 때, CLOCK_REPLY 에 실린 누계만 바뀌었을 때, DRIVE 뒤로 표본이 끊겼을 때 통과로 끝나지 않는지 본다. `link_bench.py` 의 DRIVE REPORT 판정은 후속 REPORT 가 빠지거나 순서가 다르거나 기한 뒤의 표본에 붙었을 때 문제로 보는지 본다. `cutoff_stress.py` 와 `pi_link_check.py` 는 답이 끊기거나, 타이머에 걸린 기한이 하나도 없거나, 초반 lease 뒤로 새 lease 가 없을 때 통과로 끝나지 않는지 본다. `link_bench.py` 는 SENSOR 가 없거나 도중에 끊기거나, 순번·간격·가리키는 lease 가 어긋난 흐름을 알아채는지 본다
- gcc 12 이상이면 `analyze_unpack.c` 를 `-fanalyzer` 로 컴파일해, 출력 구조체를 초기화하지 않고 unpack 해도 그 값을 읽지 않는지 본다

호스트 시험의 시계·엔코더·alarm 은 가짜다. 실제 GPTimer 와 인터럽트는 실보드에서만 확인된다.

`sim_link` 는 스케치의 loop 처럼 입력이 없을 때에도 1 ms 마다 돌면서 SENSOR 를 낸다. Windows 에서 ctest 로 돌리면 이 간격이 15 ms 단위로 늘어날 때가 있다(SENSOR 가 30 ms 마다 나감). 그래서 `link_bench.py` 는 DRIVE 의 REPORT 수를 정해 두지 않고 장치 시각으로 본다. 장치는 표본을 보낸 다음 loop 에서(다음 표본을 뜨기 전에) 그 표본의 REPORT 를 만들므로, 기한까지 5 ms 넘게 남았고 다음 표본도 기한 전인 표본의 REPORT 는 PC 나 시뮬레이터가 얼마나 늦든 꼭 와야 한다. 기한 전의 마지막 표본은 와도 되고 안 와도 된다. 앞·뒤 두 DRIVE 가운데 꼭 와야 하는 후속 REPORT 가 하나도 없었으면 확인한 것이 없으므로 실패로 끝낸다. DRIVE 로 출력이 걸려 있는 동안에는 엔코더가 명령한 속도대로 돌고, 출력이 끊기면 그 자리에 선다. 모터와 차체의 움직임을 흉내 낸 것은 아니다. 인자로 초당 칸 수를 주면(`sim_link 3000`) 명령과 상관없이 그 속도로 더 돈다. 점검 도구에는 `--ticks-per-second` 로 준다.

## 점검 도구

### 실보드 점검 (`tools/link_bench.py`)

PC 가 Pi 역할을 하며 HELLO, CLOCK_PROBE, CLOCK_INSTALL 을 보내고 응답을 계약과 대조한다. lease 를 설치한 뒤로는 SENSOR 도 본다. 모터·서보는 움직이지 않으니 XT60 은 연결하지 않아도 된다.

```sh
pip install pyserial
python vehicle/ESP32/tools/link_bench.py --port COM8
```

- 펌웨어를 올린 뒤 실행한다. PuTTY 같은 다른 시리얼 창은 닫는다.
- 끝줄이 `link_bench: … checks, 0 failed` 이면 통과다.
- `기한 뒤 재전송은 REJECTED CLOCK_EXPIRED` 가 `ok` 이면 타이머 인터럽트가 기한에 만료를 처리한 것이다. DEVICE_FAULT 로 나오면 alarm 이 울리지 않은 것이다.
- SENSOR 는 lease 를 설치하기 전과 기한이 지난 뒤, 새 `pi_session` 이 온 뒤에는 오지 않아야 하고, lease 가 살아 있는 동안에는 그 lease 를 가리키며 끊기지 않고 와야 한다. 실보드에서는 한 주기보다 3 ms 넘게 늦은 표본을 끊긴 것으로 본다. 시뮬레이터는 PC 가 멈칫하는 만큼 늦으므로 80 ms 까지 봐 준다.
- lease 가 살아 있는 동안 CLOCK_PROBE 24개를 SENSOR 주기의 여러 자리에 닿게 보낸다. 모두 답이 와야 하고, 받고 나서 표본을 뜰 때까지 기다린 시간이 한 주기를 넘지 않으며 반 주기 넘게 기다린 것이 하나 이상 있어야 한다. 그동안의 표본은 순번이 1 씩 늘고 위 "표본 사이 간격"을 지켜야 한다.
- STOP 을 보내 APPLIED 와 그 뒤 표본마다의 REPORT, 멈춘 바퀴를 정지로 알리는지를 본다. 여기까지는 모터와 서보가 움직이지 않는다.
- `--drive` 를 주면 DRIVE 를 보낸다. **바퀴가 0.2 m/s 로 80 ms 씩 앞으로 한 번, 뒤로 한 번 돈다. 바퀴를 띄우고 모터 전원을 넣었을 때만 준다.** 엔코더 누계로 출력이 걸린 것과 기한 뒤에 끊긴 것, 다시 온 DRIVE 가 출력을 켜지 않는 것, STOP 없이 방향을 바꾸는 DRIVE 의 거절을 본다. 장치가 정지 조향을 알리면 그 사이에 HOLD_STEER 를 세 번(+100, −100, 0 mrad) 보내 **조향이 움직인다.** 적용과 표본마다의 REPORT, 그동안 엔코더 누계가 그대로인 것, 속도가 실린 HOLD_STEER 의 거절을 본다. 알리지 않는 장치에는 HOLD_STEER 가 UNSUPPORTED_PROFILE 로 거절되는지 본다. 시뮬레이터에는 늘 여기까지 한다.
- 장치가 단일 존 ToF 를 알리면 그동안 받은 TOF_SAMPLE 의 배치·CRC·순번·instance 규칙을 본다. lease 를 설치하기 전과 기한 뒤, 새 `pi_session` 뒤에 오지 않는지도 본다. `--tof` 를 주면 앞·뒤 두 센서의 관측이 모두 와야 통과한다(시뮬레이터에는 늘 그렇게 본다). 센서별 유효 관측 수와 거리 범위를 보여 준다.
- 장치가 근거리 제동을 알리면 표본 quality 의 막힘 bit 를 받아들이고 지금 막힌 방향을 보여 준다. `--drive` 는 앞뒤로 다 가 보므로 두 방향이 모두 열려 있을 때만 DRIVE 를 보낸다. 막혀 있으면 실패로 적고 DRIVE 는 보내지 않는다. 실보드에서는 센서 앞 18 cm 안을 비우고 한다.
- 시뮬레이터에 `--wall-mm 230` 을 주면 앞 센서 230 mm 앞에 벽을 두고, 위의 것을 다 한 뒤 벽으로 다가가 본다(호스트 시험의 `esp32_link_bench_wall`). DRIVE 를 40 ms 마다 이어 보내 막힌 뒤의 DRIVE 가 PROXIMITY_BLOCKED 로 거절되는지, 표본에 앞 막힘 bit 만 켜지는지, 벽 앞 150 mm 를 지난 지 40 mm 안에서 서는지, 새 STOP 뒤에 뒤로 물러나면 풀리고 앞으로 가는 DRIVE 가 다시 적용되는지 본다. 실보드에는 쓰지 않는다.
- 속도는 460800 bps 가 기본이다. 앞선 펌웨어(115200 bps)에는 `--baud 115200` 을 준다.
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

### 조향 확인 (`tools/steer_check.py`)

속도 0 인 DRIVE 로 조향만 차례로 보낸다(기본 0, +200, 0, −200, 0 mrad 를 3초씩). 모터 PWM 은 0 이라 뒷바퀴는 돌지 않고, 기한이 지나도 서보는 마지막 자리에 머문다. 조향 값은 왼쪽이 + 다.

- 도구가 확인하는 것은 그 DRIVE 가 APPLIED 로 돌아오고, 머무는 동안 받은 표본(SENSOR 와, lease 를 설치하며 받은 CLOCK_REPLY)마다 엔코더 누계가 시작값 그대로라는 것까지다. 머무는 동안 lease 를 0.5초마다 이어 설치해 SENSOR 가 끊기지 않게 하고, DRIVE 뒤로 표본이 오지 않은 자리는 확인하지 못한 것이므로 실패로 친다. **서보가 실제로 움직인 방향과 바퀴 각도는 사람이 보고 잰다.**
- 실보드에는 `--drive` 를 줄 때만 보낸다. 서보 전원(UBEC)이 들어와 있어야 움직인다. 앞바퀴가 바닥에 닿지 않게 차를 올려 두고, 조립 안내서 단계 K 의 서보 확인과 같은 자세로 본다.
- 볼 것: + 에 바퀴가 왼쪽으로 꺾이는지, 0 에서 곧은지, 같은 크기의 + 와 − 가 좌우로 같은 만큼 꺾이는지. 바퀴 각도를 재면 `SERVO_US_PER_RAD` 를 고칠 수 있다(각도 = 보낸 mrad 가 되도록).
- ±700 mrad(약 40도)를 넘는 값은 펌웨어가 `RANGE` 로 거절한다.
- `--stationary` 를 주면 속도 0 인 DRIVE 대신 HOLD_STEER 로 보낸다. 장치가 정지 조향(`0x0400`)을 알리지 않으면 보내지 않고 실패로 끝난다. 서보가 움직이는 것은 같다.

```sh
python vehicle/ESP32/tools/steer_check.py --port COM8 --drive
python vehicle/ESP32/tools/steer_check.py --port COM8 --drive --mrad 100 300 500 700 --hold 5
python vehicle/ESP32/tools/steer_check.py --port COM8 --drive --stationary
```

### Pi Gateway 코드와 맞물려 보기 (`tools/pi_link_check.py`)

실제 `xenova_gateway.feedback3` 을 PC 에서 띄워 `socket://` 로 이 도구의 중계에 붙이고, 중계가 시뮬레이터나 실보드와 바이트를 주고받는다. PC 에서 게이트웨이 코드를 돌리는 것이라 실제 Pi 의 USB 와 연결됐다는 뜻은 아니다.

1. 연결: Pi 가 HELLO_ACK 를 받아들이고 lease 를 이어 간다. 첫 lease 뒤로 `--seconds`(기본 3초, 최소 2초) 동안 INSTALLED 응답이 0.8초 넘게, SENSOR 가 0.2초 넘게 끊기지 않는지, Pi 가 쓸 수 있는 lease 가 끊기지 않고 끝날 때에도 있는지, Pi 의 odometry 가 오류 없이 표본을 받는지 본다.
2. STOP: Pi 가 STOP 을 보내고, 장치의 APPLIED 보고와 그 뒤의 표본으로 정지를 확인한다. Pi 가 DRIVE 를 내보내기 전에 하는 바로 그 검사를 통과해야 한다.
3. DRIVE: Pi 가 1초 동안 20 ms 마다 DRIVE 를 보내고 APPLIED 를 받는다. 보내기를 멈추면 장치가 스스로 출력을 끊어 속도가 0 이 되는지, 이어서 보낸 STOP 으로 다시 정지를 확인하는지 본다. 실보드에서는 `--drive` 를 줄 때만 한다. **바퀴가 0.2 m/s 로 1초 돈다. 바퀴를 띄우고 모터 전원을 넣었을 때만 준다.**
- ToF: 장치가 단일 존 ToF 를 알리고 TOF_SAMPLE 을 보내면, 그 관측에 실린 sensor profile 로 Pi 의 ToF 선택을 만들어 1초 동안 Pi 가 관측을 받아 넘기는지 본다(1 다음에 한다). Pi 가 버리거나 거절한 관측이 있으면 실패다. 실보드에서 관측이 하나도 오지 않으면 확인하지 못했다고 적고 넘어가며, `--tof` 를 주면 실패로 친다.
4. HOLD_STEER: 장치가 정지 조향을 알리면, Pi 가 방향을 바꾸는 자리의 STOP 뒤에 0.5초 동안 20 ms 마다 HOLD_STEER 를 보내고 APPLIED 를 받는다. 조향이 왼쪽 0.1 rad 로 움직이고 바퀴는 돌지 않는다. 3 과 같은 때에만 한다. Pi 가 내보내지 않은 HOLD_STEER 의 수와 까닭, 그동안 Pi 의 방향 전환 gate state 를 보여 준다(아래 "HOLD_STEER 를 Pi 와 맞물려 알게 된 것").

- 근거리 제동: 시뮬레이터에 `--wall-mm 250` 을 주면 3·4 대신 한다. Pi 가 2초 동안 앞 벽으로 DRIVE 를 이어 보내, 막히기 전의 것은 APPLIED 로, 막힌 뒤의 것은 PROXIMITY_BLOCKED 로 알려지는지, Pi 가 앞이 막힌 것을 보면서도 장치를 고장으로 보지 않는지, 차가 벽 앞 150 mm 를 지난 지 40 mm 안에 서 있는지 본다. 이어서 STOP 뒤에 뒤로 물러나 막힘이 풀리고 앞으로 가는 DRIVE 가 다시 적용되는지 본다. 근거리 제동 수용(S15P31E110-465)이 들어 있는 게이트웨이 코드가 필요하고, 없으면 실패로 적는다.

그동안 Pi 가 장치를 고장으로 보거나 프레임을 버리면 실패다. 펌웨어의 capability 에 Pi 필수 bit 가 모자라면 Pi 가 HELLO_ACK 를 받지 않는 것을 먼저 확인하고, 중계가 모자란 bit 를 채워 넣은 채 1 만 본다. 그것은 부분 호환성 시험이다.

```sh
python vehicle/ESP32/tools/pi_link_check.py --platform <platform 폴더> --sim <빌드 폴더>/sim_link
```

`protobuf==6.33.6`, `paho-mqtt`, `pyserial` 이 필요하다. vehicle 브랜치에는 아직 feedback-3 게이트웨이 코드가 없어서 develop 을 따로 꺼낸 폴더의 `platform` 을 `--platform` 으로 준다. `--sim` 대신 `--port` 를 주면 실보드와 맞물린다. 오래 보려면 `--seconds 30` 처럼 준다.

Pi odometry 에는 조립 안내서에서 잰 차체 치수 언저리의 값(칸당 0.164 mm, 속도 상한 1 m/s)을 준다. 보정한 값이 아니다. 시뮬레이터에 `--ticks-per-second 3000` 을 주면 바퀴가 도는 표본을 Pi 가 받는 것까지 보고, Pi 가 구한 속도가 그 속도와 10 % 안에서 같은지 본다. 실보드에서는 그동안 바퀴를 손으로 굴리면 Pi 가 본 속도와 누계가 끝줄 앞에 찍힌다.

### HOLD_STEER 를 Pi 와 맞물려 알게 된 것

develop `841b7bf3` 의 게이트웨이 코드를 시뮬레이터에 물려 본 것이다. 둘 다 플랫폼 파트와 맞춰야 한다.

- **조향 측정이 없으면 자동 주행의 방향 전환이 열리지 않는다.** Pi 는 HOLD_STEER 가 APPLIED 된 뒤 `measured` 로 표시된 조향 표본이 목표에 머무는 것을 보고 방향 전환을 허락한다(gate state 4, 계약 §7). 이 차에는 조향 센서가 없어 표본에 조향을 싣지 않으므로 state 는 2 에 머문다. 이 펌웨어가 HOLD_STEER 를 받게 된 것만으로는 자동 주행의 전진 → 후진이 되지 않는다. 수동 주행의 방향 전환은 조향 표본을 보지 않는다(게이트웨이 코드를 읽은 것이고 돌려 보지는 않았다).
- **가까이 붙은 표본 둘이 있으면 Pi 가 그 순간의 HOLD_STEER 를 내보내지 않는다.** Pi 는 HOLD_STEER 를 내보낼 때마다 최근 표본으로 정지를 다시 확인한다(위 "표본 사이 간격"의 계산). CLOCK_REPLY 의 표본을 앞 SENSOR 에서 5 ms 만 띄우던 빌드에서는, 이 도구가 준 값(칸당 0.164 mm, 한도 0.05 m/s, 시각 오차 1.7 ms)으로 간격 7 ms 인 쌍이 0.051 m/s 로 계산돼 0.5초 동안 낸 HOLD_STEER 24개쯤 가운데 3개에서 5개를 Pi 가 내보내지 않았다. 표본 간격을 20 ms 로 고르게 한 뒤로는 23개를 모두 내보낸다. `pi_link_check.py` 는 내보내지 않은 것이 하나라도 있으면 실패로 끝난다. 한도와 시각 오차는 설치값이라 실제 Pi 에서 다시 봐야 한다.
- **시각 오차가 5 ms 에서 6 ms 쯤이면 Pi 가 HOLD_STEER 일부를 내보내지 않는다.** 표본 간격을 고르게 한 빌드를 실보드에 올리고 PC 에서 게이트웨이 코드를 돌렸을 때 23개 가운데 3개, 60개 가운데 2개가 그랬다(확인 기록). 그 순간 표본은 20.0 ms 에서 21.5 ms 간격이었고 누계는 그대로였다. 통과하지 못한 것은 정지 확인 창의 길이다. Pi 는 창의 맨 앞 표본을 버려도 `남은 길이 ≥ 30 ms + 둘째 표본 시각 오차의 두 배` 이면 버리는데, 검사는 `길이 − 첫 표본의 오차 − 마지막 표본의 오차 ≥ 30 ms` 를 요구한다(`feedback3.py` 의 창을 자르는 두 곳과 `scene_authority.py` 의 `check_stationary_window`). 마지막 표본의 오차가 더 크면 잘라 낸 창이 검사에 모자란다. 한 번은 41777 − 5883 − 6196 = 29698 us 였다. 시각 오차가 표본마다 같은 시뮬레이터에서는 오차를 키워도 나오지 않았다. 게이트웨이 쪽을 고친 브랜치(S15P31E110-460)의 코드로는 이 까닭으로 빠지는 것이 없었다(확인 기록). 실제 Pi 의 시각 오차가 이 범위인지는 아직 모른다.
- **시각 오차가 8 ms 를 넘으면 표본 간격 20 ms 로도 정지 확인이 통과하지 못한다.** 누계가 그대로여도 `칸당 거리 ÷ (표본 간격 − 두 표본의 시각 오차)` 가 한도를 넘기 때문이다. 칸당 0.164 mm, 한도 0.05 m/s 에서는 두 표본의 오차 합이 16.7 ms 를 넘을 때다. PC 에서 게이트웨이 코드를 돌린 경로에서 오차가 8 ms 에서 10 ms 로 뛴 0.12초 동안 HOLD_STEER 6개가 잇달아 빠졌다. 펌웨어로 줄일 수 있는 것이 아니고, 한도와 시각 오차 값에 달려 있다.

### 근거리 제동을 Pi 와 맞물려 알게 된 것

- 근거리 제동 수용(S15P31E110-465)이 없는 게이트웨이 코드는 막힘 bit 가 켜진 표본을 odometry 에서 버린다(`ODOM_SENSOR_QUALITY`). 차량 표본이 없어져 주행 명령을 낼 수 없게 된다. 가까운 물체가 없는 동안에는 드러나지 않는다. 그래서 게이트웨이가 먼저 바뀌어야 한다.
- 게이트웨이는 막힌 방향의 DRIVE 를 미리 막지 않는다. 위쪽이 보내는 대로 20 ms 마다 내보내고, 장치가 그때마다 PROXIMITY_BLOCKED 로 거절한다. Pi 는 그것을 고장으로 보지 않고 세기만 한다. 막혔다는 것을 받은 뒤 AUTO·MANUAL 이 무엇을 할지는 아직 없다.
- **Pi 의 정지 확인은 달린 거리가 늘수록 통과하기 어려워진다.** 정지 확인의 yaw 조건은 이웃한 두 표본의 방향 오차를 더해 표본 간격으로 나눈 값을 한도와 견주는데, odometry 가 그 방향 오차를 달린 거리에 비례해 키운다. `pi_link_check.py` 의 설정(`heading_error_per_m` 0.02, `max_yaw_rps` 1.0. 실측값이 아니다)으로는 0.4 m 쯤 달린 뒤부터 STOP 을 확인하지 못했다(`stationary yaw bound failed`). 그 뒤로는 방향을 바꾸는 DRIVE 를 낼 수 없다. 실제 설치 설정에서도 그런지는 확인하지 않았다. 플랫폼 파트에 알릴 일이다.
- 시뮬레이터에서 DRIVE 를 앞 DRIVE 의 기한이 끝나는 때에 맞춰 이어 보냈을 때, 스물네 번 가운데 한 번 장치가 DEVICE_FAULT 로 답했다. 실제 Pi 처럼 기한 전에 이어 보내게 바꾼 뒤로는 서른 번 가운데 없었다. 까닭은 확인하지 않았다. 시뮬레이터는 alarm 을 메인 루프에서 처리하므로 PC 가 멈칫한 사이 기한이 지난 것으로 짐작한다.

## 확인 기록

| 날짜 | 범위 | 결과 |
|---|---|---|
| 2026-11-11 | 호스트 시험(Windows, zig cc, UBSan) | `esp32_feedback3` 600 검사, `esp32_link` 1392 검사, `esp32_quadrature` 43 검사, `esp32_link_bench` 48 검사, `esp32_tools` 78 검사 통과 |
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
| 2026-11-11 | 실보드 `link_bench.py --port`(SENSOR 를 넣은 빌드, lease 500 ms 와 3초) | 48 검사 통과. lease 가 살아 있는 동안 SENSOR 간격이 20003 us 에서 20013 us 였고, lease 3초 동안 146개가 23 ms 넘게 끊기지 않음. 설치 전과 기한 뒤, 새 `pi_session` 뒤에는 SENSOR 가 오지 않음. SENSOR 주기의 여러 자리에 닿게 보낸 CLOCK_PROBE 24개에 모두 답했고, 표본 뒤 5 ms 안에 닿은 5개는 간격을 채운 뒤에 답함 |
| 2026-11-11 | 실보드 `pi_link_check.py --port --seconds 30`(develop `a9642deb` 의 게이트웨이 코드, 바퀴는 멈춘 채) | 통과. SENSOR 1495개를 Pi 가 odometry 오류 없이 받음(CLOCK_REPLY 의 표본까지 1644개), 새 lease 150개, Pi 가 버린 프레임 0, 끝날 때 쓸 수 있는 차량 표본이 있음. 종단 연결 완료가 아니다 |
| 2026-11-11 | 실보드, 같은 구성으로 40초 동안 바퀴를 손으로 굴림 | SENSOR 1993개 가운데 1234개에서 누계가 바뀌었고(왼쪽 3081칸, 오른쪽 6848칸, 이웃한 표본 사이 최대 19칸으로 0.16 m/s 쯤), Pi 가 버린 프레임과 odometry 오류가 없음 |
| 2026-11-11 | `pi_link_check.py --sim --ticks-per-second 3000` 과 `5500`(같은 게이트웨이 코드, 30초씩) | 통과. Pi 가 구한 속도 0.494 m/s 와 0.901 m/s 가 넣은 속도와 같고 odometry 오류 없음 |
| 2026-11-11 | 같은 시험을 표본 사이 간격을 두지 않은 시험용 빌드로 | 0.49 m/s 에서 `ODOM_SAMPLE_ORDER` 3개와 `ODOM_ENCODER_DISCONTINUITY` 1개, 0.90 m/s 에서 6개와 4개. Pi 가 그 프레임을 버림 |
| 2026-11-11 | 실보드 `cutoff_stress.py --trials 1000`(SENSOR 가 나가는 중) | 통과. 기한 하나 400번, 먼 기한 먼저·차례로·수십 us 차이로 각 200번(시도 1563번). 답을 받지 못한 프레임과 DEVICE_FAULT 없음. 그 자리에서 거절된 563번은 넣지 않았다 |
| 2026-11-11 | 실보드, 100 B 를 네 번 이어 쓰는 시험용 스케치 | 송신 버퍼가 없으면 두 번째부터 write 가 11 ms 씩 걸림. 1024 B 를 두면 0.1 ms 미만 |
| 2026-11-11 | develop 의 Pi odometry 에 좌 +1·우 −1 인 표본(게이트웨이 코드만 따로 돌림) | `ODOM_ACKERMANN_INCONSISTENT` 로 거절함. 누계가 그대로면 표본 나이 한도(0.4초로 둠)까지 쓸 수 있는 차량 표본이 없다가, 그 뒤 새 odom epoch 으로 위치가 0 으로 돌아감. 펌웨어가 아니라 Pi 쪽 동작이다. 멈춘 차의 바퀴가 실제로 그렇게 떨리는지는 확인하지 못했다 |
| 2026-11-11 | 호스트 시험, 명령과 출력 차단을 넣은 뒤(Windows, zig cc, UBSan) | `esp32_link` 2357 검사, `esp32_link_bench` 66 검사, `esp32_tools` 91 검사 통과. 스케치 컴파일 경고 없음 |
| 2026-11-11 | `link_bench.py --sim`(DRIVE 포함) | 66 검사 통과. DRIVE 80 ms 동안 엔코더 누계가 늘고 기한 뒤에 멈춤. 다시 온 DRIVE 는 출력을 켜지 않음 |
| 2026-11-11 | `pi_link_check.py --sim`(develop `62a3dfb2` 의 게이트웨이 코드, capability 를 채우지 않음) | 통과. Pi 가 HELLO_ACK 를 받아들이고, STOP 뒤 정지를 확인하고, 1초 동안 DRIVE 47개를 보내 모두 APPLIED 를 받음. 보내기를 멈추자 속도가 0 이 됐고 이어진 STOP 으로 다시 정지를 확인. Pi 가 버린 프레임과 고장 없음. 실제 Pi 의 USB 가 아니다 |
| 2026-11-11 | 실보드 `link_bench.py --port`(460800 bps, DRIVE 는 보내지 않음) | 54 검사 통과. CLOCK_PROBE 왕복 5.4 ms(115200 bps 에서는 16 ms). STOP 에 APPLIED 로 답하고 그 뒤 0.3초에 REPORT 15개가 표본마다 뒤따랐으며 멈춘 바퀴를 정지로 알림. SENSOR 간격 20008 us 에서 20014 us |
| 2026-11-11 | 실보드 `pi_link_check.py --port --seconds 30`(develop `62a3dfb2` 의 게이트웨이 코드, capability 를 채우지 않음, DRIVE 는 보내지 않음) | 통과. Pi 가 HELLO_ACK 를 받아들였고 SENSOR 1494개를 odometry 오류 없이 받음. STOP 을 보내고 0.07초 뒤에 Pi 의 정지 검사를 통과. Pi 가 본 시각 오차 3.4 ms(115200 bps 에서는 8.4 ms). Pi 가 버린 프레임과 고장 없음. 실제 Pi 의 USB 가 아니다 |
| 2026-11-11 | 실보드 `cutoff_stress.py --trials 500`(460800 bps) | 통과. 기한 하나 200번, 기한 둘 세 가지 각 100번. 답을 받지 못한 프레임과 DEVICE_FAULT 없음 |
| 2026-11-11 | 실보드, 바퀴를 띄우고 모터 전원을 넣음. `link_bench.py --port --drive` | 66 검사 통과. 0.2 m/s 의 DRIVE 80 ms 에 양쪽 누계가 96칸 늘었다가 멈췄고, 새 STOP 뒤의 반대 방향 DRIVE 에 96칸 줄었다가 멈춤. 다시 온 DRIVE 는 바퀴를 돌리지 않았고, STOP 없이 방향을 바꾸는 DRIVE 와 HOLD_STEER 는 거절됨 |
| 2026-11-11 | 같은 상태에서 `pi_link_check.py --port --drive`, 서보에 쓰는 자리를 고치기 전 | 실패. DRIVE 를 20 ms 마다 받자 0.3초쯤에 장치가 타이머 고장으로 굳어 출력을 끊고 순번 0 의 FAULT 를 알림. 인터럽트를 막은 채 서보 채널에 쓰는 동안 기한의 alarm 이 1 ms 넘게 밀린 것이었다. 고장으로 굳어 출력을 끊는 경로가 실보드에서 동작함 |
| 2026-11-11 | 실보드 `ledcWrite` 가 걸리는 시간(시험용 스케치) | 서보 채널(50 Hz)에 30 ms 간격으로 쓰면 9 ms 에서 40 ms 까지 멈춰 있고, 45 ms 이상 간격이면 5 us. 20 kHz 채널에 잇달아 쓰면 0.1 ms |
| 2026-11-11 | 같은 상태에서 `pi_link_check.py --port --drive`, 고친 뒤 | 통과. Pi 가 1초 동안 DRIVE 48개를 보내 APPLIED 를 받았고 바퀴 속도를 0.229 m/s 까지 봄(보낸 속도 0.2 m/s). 보내기를 멈추자 바퀴가 멈췄고 이어진 STOP 으로 0.08초 뒤에 다시 정지 검사를 통과. Pi 가 버린 프레임과 고장 없음 |
| 2026-11-11 | platform `1dd1ba43` 의 게이트웨이 코드(쓰던 lease 를 이어 쓰는 판)로 `pi_link_check.py --sim` | 통과. capability 를 채우지 않고 STOP 정지 확인, DRIVE 48개 APPLIED, 출력 차단, 다시 정지 확인까지 같음. HELLO 를 다시 보내는 게이트웨이 판(S15P31E110-457 브랜치)으로도 같고, capability 가 모자란 앞선 펌웨어에는 HELLO_ACK 를 20번 거절한 뒤 handshake 실패로 끝남 |
| 2026-11-11 | `steer_check.py --sim` 과 호스트 시험 6개 | 통과. 속도 0 인 DRIVE 다섯 개(0, +200, 0, −200, 0 mrad)가 APPLIED 로 돌아오고, 자리마다 그 뒤 표본의 엔코더 누계가 그대로임. 바퀴가 도는 시뮬레이터(`--ticks-per-second 100`)에는 실패로 끝남. ±500 mrad 를 넘는 값은 `RANGE` 로 거절됨. 시뮬레이터에는 서보가 없어서 방향과 각도는 확인한 것이 아니다. 조향 부호를 바꾼 스케치는 컴파일까지만 봄 |
| 2026-11-12 | 실보드, 조향 부호를 고친 빌드. `link_bench.py --port`, `pi_link_check.py --port --seconds 30`(develop `4ac49509` 의 게이트웨이 코드), `cutoff_stress.py --trials 500`. DRIVE 는 보내지 않음 | 통과. 54 검사. Pi 쪽 코드가 SENSOR 1495개를 odometry 오류 없이 받고 STOP 뒤 0.06초에 정지를 확인(시각 오차 3.1 ms). 기한 만료 500번에 DEVICE_FAULT 와 잃은 프레임 없음 |
| 2026-11-12 | 실보드 `steer_check.py --port --drive` 두 번(자리마다 3초, 5초) | 장치 쪽은 통과. 다섯 DRIVE 가 APPLIED 이고 자리마다 표본 160개, 268개의 엔코더 누계가 그대로임. **서보는 움직이지 않았다.** 배터리가 과방전돼(셀 합 3.5 V) 서보 전원이 없었다. 조향이 실제로 움직이는 방향과 각도는 여전히 확인하지 못했다 |
| 2026-11-12 | 실보드, 서보 핀(27)의 수준을 되읽는 시험용 빌드. 핀 인터럽트로 펄스 폭을 재고, 조향 0·±20 mrad 를 13번씩 두 차례 보냄 | 펄스 412개, 417개가 20.01 ms 에서 20.05 ms 간격으로 빠짐없이 나옴. 폭은 조향 0 에 1500 us, +20 mrad 에 1489 us, −20 mrad 에 1513 us. 조향을 바꾸는 때에도 1482 us 에서 1516 us 를 벗어난 펄스가 없음. ±200 mrad 에는 1372 us, 1626 us(이 값은 loop 에서 읽은 것) |
| 2026-11-12 | 같은 시험용 빌드로 속도 0 인 DRIVE 140개의 시각을 장치 시계로 잼(모터 PWM 은 0 인 채) | 프레임의 끝을 읽은 때부터 출력에 걸기 시작할 때까지 74 us 에서 89 us, 방향과 PWM 두 채널을 쓰는 데 13 us 에서 14 us. 기한부터 alarm 인터럽트에 들어갈 때까지 3 us 에서 8 us, PWM 두 채널에 0 을 다 쓸 때까지 20 us 에서 26 us. 핀에서 PWM 이 실제로 내려가는 때와, 엔코더가 도는 동안의 값은 재지 않았다 |
| 2026-11-12 | 호스트 시험, HOLD_STEER 를 넣은 뒤(Windows, zig cc, UBSan) | 일곱 개 통과. `esp32_link` 2691 검사, `esp32_link_bench` 77 검사, `esp32_steer_check` 27 검사, `esp32_steer_check_stationary` 28 검사, `esp32_tools` 121 검사. HOLD_STEER 처리를 일곱 군데 고친 변형을 모두 알아챔. 스케치 컴파일 경고 없음 |
| 2026-11-12 | `pi_link_check.py --sim`(develop `841b7bf3` 의 게이트웨이 코드) | 통과. Pi 가 capability `0x05f7` 을 채우지 않고 받아들임. HOLD_STEER 는 0.5초에 18개에서 19개를 내보내 모두 APPLIED 를 받았고, 4개에서 5개는 Pi 가 정지 확인을 통과하지 못해 내보내지 않음. 그동안 Pi 의 방향 전환 gate state 는 2 였고 4 에 이르지 않음. Pi 가 장치를 고장으로 보거나 프레임을 버리지 않음 |
| 2026-11-12 | 실보드, HOLD_STEER 를 넣은 빌드. `link_bench.py --port`, `pi_link_check.py --port --seconds 30`(develop `841b7bf3`). DRIVE 와 HOLD_STEER 는 보내지 않음 | 통과. 장치가 `0x05f7` 을 알리고 Pi 쪽 코드가 그대로 받아들임. 54 검사. SENSOR 1495개, Pi 가 본 시각 오차 3.4 ms. **실보드에 HOLD_STEER 를 보내 보지는 않았다** |
| 2026-11-12 | 실보드, 서보를 MCPWM 으로 내는 빌드에 서보 핀(27)과 시각을 되읽는 코드를 더한 시험용 빌드. 속도 0 인 DRIVE 와 HOLD_STEER 를 번갈아 12개(0, ±20, ±150, 300 mrad). 서보 전원은 없음 | 첫 명령 전에는 펄스가 없음. 펄스 208개가 19998 us 에서 20002 us 간격으로 빠짐없이 나오고, 모든 펄스의 폭이 그 펄스가 오르기 전에 마지막으로 쓴 값과 4 us 안에서 같음(1500, 1488, 1512, 1405, 1595, 1309 us). 쓴 뒤 그 폭의 첫 펄스까지 1.4 ms 에서 19.3 ms. 출력을 거는 데 14 us 에서 41 us(첫 명령이 41 us), 그 가운데 서보 쓰기까지 2 us 에서 19 us |
| 2026-11-12 | 같은 빌드에 20 ms 마다 조향을 바꾼 DRIVE 를 100개, 300개 이어 보냄(LEDC 때 쓰기가 9 ms 에서 40 ms 멈추던 경우) | 모두 걸리고 APPLIED, FAULT 없음. 출력을 거는 데 14 us 에서 15 us. 기한부터 PWM 두 채널에 0 을 다 쓸 때까지는 18 us 에서 23 us 로 앞 기록과 같음 |
| 2026-11-12 | 실보드, 서보를 MCPWM 으로 내는 정식 빌드. `link_bench.py --port`, `pi_link_check.py --port --seconds 30`, `cutoff_stress.py --trials 500`. DRIVE 와 HOLD_STEER 는 보내지 않음 | 통과. 54 검사, SENSOR 1494개, STOP 뒤 0.07초에 Pi 가 정지를 확인, 기한 만료 500번에 DEVICE_FAULT 없음 |
| 2026-11-12 | 호스트 시험, 표본 간격을 20 ms 로 고르게 한 뒤(Windows, zig cc, UBSan) | 일곱 개 통과. `esp32_link` 3567 검사, `esp32_link_bench` 77 검사, `esp32_tools` 122 검사. 표본 간격 처리를 여섯 군데 고친 변형과 HOLD_STEER 변형 일곱 개를 모두 알아챔. 스케치 컴파일 경고 없음 |
| 2026-11-12 | `pi_link_check.py --sim`(develop `841b7bf3`) 네 번, 엔코더가 도는 시뮬레이터(0.49 m/s, 0.90 m/s)로 10초씩 | 통과. Pi 가 HOLD_STEER 23개를 빠짐없이 내보내고 모두 APPLIED 를 받음. 그동안 방향 전환 gate state 는 내내 2. 엔코더가 도는 동안 odometry 오류 없이 표본 477개를 받고 속도가 일치. 같은 도구를 간격을 고치기 전의 시뮬레이터에 돌리면 HOLD_STEER 3개를 내보내지 않아 실패로 끝남 |
| 2026-11-12 | 실보드, 표본 간격을 고친 빌드. `link_bench.py --port` 네 번, `pi_link_check.py --port --seconds 30`, `cutoff_stress.py --trials 500`. DRIVE 와 HOLD_STEER 는 보내지 않음 | 통과. 54 검사. CLOCK_PROBE 24개가 받고 나서 0.4 ms 에서 19.1 ms 뒤의 표본 자리에서 답을 받음. Pi 쪽 코드가 30초에 표본 1498개(SENSOR 1350개와 CLOCK_REPLY)를 오류 없이 받고 STOP 뒤 0.06초에 정지를 확인. 시각 오차 3.0 ms |
| 2026-11-12 | 같은 빌드에서 본 SENSOR 간격 | PC 가 프레임을 보내지 않는 동안에는 20006 us 에서 20014 us(200개). `link_bench.py` 가 CLOCK_INSTALL 을 잇달아 보내는 구간에서는 네 번 가운데 세 번에 20.2 ms 에서 21.7 ms 인 간격이 하나씩 있었다. 표본이 늦게 떠진 것이고 20 ms 보다 가까워진 적은 없다. 까닭은 확인하지 못했다 |
| 2026-11-12 | 실보드, 서보 전원을 넣고(배터리 셀 3.82 V 셋, UBEC 6.0 V) 앞바퀴를 띄움. `steer_check.py --port --drive` 와 `--stationary`(0, +200, 0, −200, 0 mrad 를 3초씩) | 장치 쪽 27 검사, 28 검사 통과. 엔코더 누계는 그대로. 눈으로 본 것: 속도 0 인 DRIVE 와 HOLD_STEER 둘 다 서보를 움직이고 움직임이 비슷함. 가운데에서 바퀴가 곧음. 뒷바퀴는 돌지 않음. UBEC 을 켜지 않은 채 돌린 첫 번은 서보가 움직이지 않았다 |
| 2026-11-12 | 같은 상태에서 한쪽씩 크게: 가운데 4초, +300 mrad 8초, 가운데 4초. 이어서 −300 mrad 로 같은 순서 | 장치 쪽 23 검사씩 통과. 눈으로 본 것: **+ 에 왼쪽, − 에 오른쪽으로 꺾임.** 왼쪽으로 꺾었다가 가운데로 돌아왔을 때는 바퀴가 정면이 아니었고, 오른쪽으로 꺾었다가 돌아왔을 때는 정면이었다. 두 경우 모두 서보에 나가는 펄스는 1500 us 다. 어긋난 크기와 까닭(조향 링크나 서보의 유격 등)은 재지 않았다. 바퀴 각도도 재지 않았다 |
| 2026-11-12 | 실보드, 바퀴를 띄우고 모터·서보 전원을 넣음(배터리 셀 3.82 V 셋). 서보 MCPWM 과 표본 간격을 고친 빌드로 `link_bench.py --port --drive` | 77 검사 통과. 0.2 m/s 의 DRIVE 80 ms 에 양쪽 누계가 81칸, 79칸 늘었다가 멈췄고, 새 STOP 뒤의 반대 방향 DRIVE 에 80칸씩 줄었다가 멈춤. HOLD_STEER 세 번은 적용되고 그동안 누계가 그대로임. 다시 온 DRIVE 는 바퀴를 돌리지 않았고, STOP 없이 방향을 바꾸는 DRIVE 와 속도가 실린 HOLD_STEER 는 거절됨 |
| 2026-11-12 | 같은 상태에서 `pi_link_check.py --port --drive`(platform `8b6d2c1b` 의 게이트웨이 코드) | DRIVE 는 통과: 48개 모두 APPLIED, Pi 가 본 속도 0.190 m/s, 보내기를 멈추자 출력이 끊기고 STOP 으로 정지 확인. **HOLD_STEER 에서 실패 1건**: Pi 가 20개를 내보내 모두 APPLIED 를 받았지만 3개는 내보내지 않음(`fresh original HOLD stationary evidence required`). Pi 가 장치를 고장으로 보거나 프레임을 버리지는 않음 |
| 2026-11-12 | 같은 상태에서 HOLD_STEER 60개를 보내며 Pi 가 내보내지 않은 순간의 정지 확인을 다시 계산 | 2개를 내보내지 않음. 두 번 모두 표본 간격은 20.0 ms 에서 21.5 ms, 누계 변화 없음, 시각 오차 5.1 ms 에서 6.2 ms 였고, 걸린 조건은 정지 확인 창의 길이였다(41777 − 5883 − 6196 = 29698 us, 40771 − 5071 − 5825 = 29875 us, 필요한 값 30000 us). 위 "HOLD_STEER 를 Pi 와 맞물려 알게 된 것"의 셋째 항목 |
| 2026-11-12 | 실보드, 게이트웨이의 창 자르기를 고친 브랜치(S15P31E110-460, `9365344b`)의 코드로 HOLD_STEER 60개와 300개. 순간마다 옛 식으로도 다시 계산함 | 60개: 빠진 것 없음(옛 식으로도 떨어질 순간이 없었음, 시각 오차 3.0 ms 에서 5.3 ms). 300개: 옛 식이면 13번 떨어졌을 순간 가운데 창 길이 때문인 7번은 모두 통과. 나머지 6개는 시각 오차가 8 ms 에서 10 ms 로 뛴 구간에서 잇달아 빠졌고 걸린 조건은 바퀴 속도 한도였다. 장치를 고장으로 보거나 odometry 오류가 난 적은 없음 |
| 2026-11-12 | 이 PC 에서 최신 platform 의 게이트웨이 코드를 `pi_link_check.py` 의 중계에 물림 | 절반쯤이 `SerialTimeoutException: Write timeout` 으로 중간에 끝남. 게이트웨이가 시리얼을 5 ms 쓰기 기한으로 여는데, pyserial 의 `socket://` 은 보낸 뒤에 기한을 검사해서 Windows 에서 5 ms 넘게 밀리면 보낸 것도 실패로 친다. 도구의 중계 경로에서만 나는 일이다. 쓰기 기한을 1초로 늘려 돌린 실행에서는 나지 않았다 |
| 2026-11-12 | 호스트 시험, ToF 관측 전송을 넣은 뒤(Windows, zig cc, UBSan) | 일곱 개 통과. `esp32_feedback3` 628 검사(ToF vector 2개가 platform 의 `gateway-tof-single-1.json` 과 같은 바이트), `esp32_link` 3808 검사, `esp32_link_bench` 79 검사, `esp32_tools` 151 검사. ToF 처리를 스물세 군데 고친 변형을 모두 알아챔. 스케치 컴파일 경고 없음(VL53L0X by Pololu 1.3.1) |
| 2026-11-12 | `pi_link_check.py --sim`(platform `8b6d2c1b` 의 게이트웨이 코드) 여섯 번 | 통과. Pi 가 `0x0df7` 을 채우지 않고 받아들이고, 장치의 sensor profile 로 ToF 선택을 만들자 1초에 센서마다 9개에서 10개의 관측을 받아 넘김. 버리거나 거절한 관측 없음. STOP·DRIVE·HOLD_STEER 는 그대로 통과 |
| 2026-11-12 | 실보드(USB 전원만, 두 센서는 조립 안내서 단계 K 의 배선 그대로), ToF 를 넣은 빌드. `link_bench.py --port` 와 `--tof`, `pi_link_check.py --port --seconds 30 --tof`, `cutoff_stress.py --trials 500` | 통과. 55 검사, 56 검사. 두 센서가 켜질 때 붙었고 TOF_SAMPLE 51개(앞 25개, 뒤 26개)가 모두 VALID, 규칙에 어긋난 것 없음. 그때 앞 센서는 452 mm 에서 506 mm, 뒤 센서는 284 mm 에서 294 mm 를 봄(책상 위에 놓인 그대로이고 줄자로 견주지 않았다). Pi 쪽 코드가 1초에 센서마다 10개를 받아 넘김. SENSOR 간격은 20008 us 에서 21755 us, 30초에 표본 1499개, 기한 만료 500번에 DEVICE_FAULT 없음 |
| 2026-11-12 | 실보드, 같은 빌드. 초마다 ToF 관측과 표본 간격을 찍어 봄(25초, 리셋 직후 8초, 다시 올린 직후 6초) | 센서마다 초에 10개에서 11개가 끊기지 않고 옴. 표본 간격은 20020 us 에서 20921 us. TOF_SAMPLE 804개 가운데 규칙에 어긋난 것 없음. 한 번은 켜진 뒤 몇 분 사이에 두 센서가 함께 다시 초기화돼 generation 2 와 새 instance 로 붙어 있었다(그 뒤 25초 동안은 그대로). 언제 왜 떨어졌는지는 보지 못했고, 리셋하거나 다시 올린 뒤에는 generation 1 인 채였다 |
| 2026-11-12 | 실보드, 같은 빌드. 초마다 찍는 동안(60초) 두 센서 앞을 손으로 두 번 가림(2초, 6초) | 가린 동안 두 센서가 21 mm 에서 41 mm 를 VALID 로 냈고 치우자 앞 390 mm 에서 497 mm, 뒤 276 mm 에서 292 mm 로 돌아옴. 센서마다 초에 10개에서 11개가 끊기지 않고 옴. 표본 3010개의 간격은 가장 긴 것이 21157 us. TOF_SAMPLE 1237개 가운데 규칙에 어긋난 것 없음. 시작할 때 두 센서는 이미 generation 3 이었다(다시 올린 뒤 몇 분 사이에 두 번 함께 다시 초기화된 것이고, 까닭은 보지 못했다) |
| 2026-11-12 | 실보드, 같은 빌드. 초마다 찍는 동안(60초) 앞 센서의 SDA 선(조립 안내서의 T7, `a50`)을 두 번 뺐다 꽂음 | 뺀 동안(4초, 2초) 앞 센서의 관측이 오지 않았고, 꽂자 새 instance 와 1 큰 generation 으로 순번 1 부터 다시 옴(3 → 4 → 5, 두 번째에는 5 → 6. 첫 번째에는 다시 붙은 지 1초 안에 한 번 더 초기화됨). 뒤 센서는 그동안 generation 3 인 채 초에 10개에서 11개가 끊기지 않고 옴. 표본 3004개의 간격은 가장 긴 것이 21351 us 로 선을 뺀 동안에도 늘지 않았고 lease 를 놓친 적 없음. TOF_SAMPLE 1160개 가운데 규칙에 어긋난 것 없음, 깨진 프레임 없음. 시작한 뒤 3초쯤 앞 센서가 NO_TARGET 을 냄(첫 1초는 10개 모두, 다음 2초는 501 mm 에서 545 mm 의 VALID 와 섞임. 그때 센서 앞이 어땠는지는 확인하지 않았다) |
| 2026-11-12 | 호스트 시험, 근거리 제동을 넣은 뒤(Windows, zig cc, UBSan) | 여덟 개 통과. `esp32_link` 4502 검사, `esp32_link_bench` 80 검사, `esp32_link_bench_wall` 93 검사, `esp32_tools` 164 검사. 근거리 제동을 마흔한 군데 고친 변형을 `esp32_link` 가 모두 알아챔. 스케치 컴파일 경고 없음 |
| 2026-11-12 | 시뮬레이터의 앞 벽(기준 거리가 300 mm 이던 때). `link_bench.py --sim --wall-mm 380` 서른 번, `pi_link_check.py --sim --wall-mm 400`(S15P31E110-465 의 게이트웨이 코드) 여섯 번 | 통과. 벽 앞 300 mm 를 지나 5 mm 에서 15 mm 사이에 섰고(따로 돌린 열여덟 번에서 본 값), 막힌 뒤의 DRIVE 는 PROXIMITY_BLOCKED 로 거절됨. Pi 는 APPLIED 26개쯤 뒤의 거절 70개쯤을 고장으로 보지 않았고 물러나자 막힘이 풀림. 펌웨어를 여덟 군데 고친 변형 가운데 일곱을 두 도구가 알아챔. "막혀도 걸려 있는 출력을 끊지 않음"은 이어 오는 DRIVE 가 거절돼 80 ms 안에 서므로 도구로는 가리지 못하고 `esp32_link` 가 본다 |
| 2026-11-12 | 근거리 제동을 넣은 시뮬레이터를 그 수용이 없는 게이트웨이 코드(platform `44b95e50`)에 물림 | 가까운 물체가 없으면 통과(34 검사, 두 번). 벽을 두면 `pi_link_check.py` 가 게이트웨이 코드가 근거리 제동을 모른다고 실패로 적음 |
| 2026-11-12 | 기준 거리를 150 mm 로 줄인 뒤. 호스트 시험, `link_bench.py --sim --wall-mm 230` 여섯 번, `pi_link_check.py --sim --wall-mm 250`(S15P31E110-465 의 게이트웨이 코드) 네 번 | 통과. 여덟 개 시험 통과, 벽 앞 150 mm 를 지나 6 mm 에서 21 mm 사이에 섬. 스케치 컴파일 경고 없음 |
| 2026-11-14 | 실보드, 근거리 제동과 조향 ±700 mrad 를 넣은 빌드(`firmware_sha256` `278f465a…`, capability `0x1df7`). 배터리 전원, 200 mm/s 의 DRIVE 를 40 ms 마다 이어 보냄 | `link_bench.py --port` 55 검사 통과. `steer_check.py --drive --mrad 0 400 700 0 -400 -700 0` 35 검사 통과(바퀴 각도는 재지 않음). 바퀴를 띄우고: 앞 60 mm 에서 73 mm 에 물체를 두면 전진은 첫 DRIVE 부터 `PROXIMITY_BLOCKED` 로 거절되고 엔코더가 그대로, 후진은 적용. 뒤 59 mm 에서 66 mm 에 두면 후진은 거절, 전진은 적용. 표본에는 막힌 쪽 bit 만 켜짐. 전진 중에 물체를 밀어 넣으면 거리가 267 mm 에서 112 mm 로 줄다가 1.12초에 거절되고 약 60칸 더 돌고 멈춤. 바닥에서: 앞 416 mm 의 물체로 다가가 134 mm 에서 139 mm 앞에 서고, 뒤 405 mm 의 물체로는 137 mm 에서 142 mm 앞에 섬(평균 속도 약 0.11 m/s). 도는 동안 두 센서의 관측은 끊기지 않음 |

아직 확인하지 않은 것: 근거리 제동의 실제 감지 지연과 0.11 m/s 보다 빠를 때의 제동 거리, 명령한 속도와 바닥에서의 실제 속도의 차이(200 mm/s 명령에 약 0.11 m/s), 근거리 제동이 걸렸을 때 실제 Pi 의 동작, 켜 둔 사이에 두 ToF 센서가 함께 다시 초기화된 까닭, 센서가 새 instance 로 다시 붙었을 때 Pi 쪽 코드가 이어 받는지(실보드), ToF 거리의 정확도와 VALID·NO_TARGET 이 아닌 상태들, 모터가 도는 동안의 ToF, 조향했을 때의 바퀴 각도와 왼쪽에서 가운데로 돌아올 때 어긋나는 크기, 기한에 핀에서 PWM 이 실제로 내려가는 시각(로직 분석기로 재야 한다. 장치 안에서 잰 `J_apply`·`J_cutoff` 쪽 값은 2026-11-12 기록에 있다), 바닥 주행, 실제 Pi 의 USB·게이트웨이와의 연결이 남아 있다. 모터로 빠르게 돌 때의 엔코더 누계, 모터로 도는 속도에서 SENSOR 를 Pi 가 받는 것(실보드에서는 손으로 0.16 m/s 쯤까지 굴려 봤고 그보다 빠른 경우는 시뮬레이터로만 봤다), 켜진 직후 첫 움직임 한 칸의 방향(손으로 굴려서는 가릴 수 없어 호스트 시험만 있다), Pi(Linux)의 USB 시리얼, Linux gcc 와 ASan(Jenkins 에서 돈다).

## 고정 vector 복사본

`test/host/gateway-feedback-3.json` 은 develop 의 `docs/contracts/vectors/gateway-feedback-3.json`(SHA-256 `0a0d35c2edf5da2b…`)과 같은 파일이다. vehicle 브랜치에 원본이 없어서 시험 폴더에 둔다. 원본이 저장소에 있으면 cmake 가 두 파일이 같은지 검사하고, 다르면 configure 를 멈춘다. 원본이 바뀌면 복사본을 다시 복사한다.

`test/host/gateway-tof-single-1.json` 은 platform 의 `docs/contracts/vectors/gateway-tof-single-1.json`(SHA-256 `27ea0b5019d17827…`)과 같은 파일이다. 같은 까닭으로 시험 폴더에 두고, 저장소에 원본이 있으면 configure 때 두 파일이 같은지 본다.

## 핀

조립 안내서(`docs/vehicle/assembly-guide.md`) 6장의 연결표를 따른다. 모터 PWM 25·26, 방향 32·33, 서보 27, 비상정지 입력 13(눌리면 HIGH), 엔코더 왼쪽 18·19, 오른쪽 22·23, ToF 의 SDA 21·SCL 17, XSHUT 앞 16·뒤 4 다.
