# ESP32 — 차량 장치 펌웨어

RC카의 모터·서보·엔코더를 맡는 ESP32 DevKitC V4 펌웨어다. 게이트웨이와는 USB 시리얼로 feedback-3 계약(develop 의 `docs/contracts/gateway-feedback-3.md`)을 따른다.

## 지금 상태

- feedback-3 프레임 층(CRC-16/CCITT-FALSE, COBS, 프레임 묶기·풀기, 수신 스트림 분리)과 payload 9종(HELLO, HELLO_ACK, CLOCK_PROBE, CLOCK_REPLY, CLOCK_INSTALL, CLOCK_ACK, SENSOR, COMMAND, REPORT)의 pack/unpack 이 있다.
- 스케치는 받은 프레임을 검사만 한다. HELLO_ACK 같은 응답, 시각 설치, 명령 적용, 모터·서보 구동은 아직 없다.
- ToF(`TOF_FRAGMENT3`)는 계약에서 설계 전용이라 넣지 않았다.

## 구조

```text
xenova_esp32/                 Arduino 스케치 폴더 (폴더 이름과 .ino 이름이 같아야 한다)
  xenova_esp32.ino
  src/feedback3/wire.*        프레임 층
  src/feedback3/payloads.*    payload 9종 배치
test/host/                    PC 호스트 시험 (ESP32 없이)
  CMakeLists.txt
  test_feedback3.c
  analyze_unpack.c            unpack 정적 분석 (gcc -fanalyzer, 컴파일만)
  gen_vectors.py              고정 vector JSON 을 C 시험 데이터(vectors.inc)로 바꾼다
  gateway-feedback-3.json     계약 고정 vector 의 복사본
```

`src/feedback3` 는 C 로 짜서 Arduino 헤더 없이 PC 에서도 그대로 컴파일된다. 스케치(C++)는 헤더를 그대로 include 한다.

## ESP32 빌드

Arduino IDE 2 에서 `xenova_esp32/xenova_esp32.ino` 를 열고 보드 `ESP32 Dev Module`(esp32 by Espressif Systems 3.x)로 컴파일·업로드한다. 명령줄로는 다음과 같다.

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 vehicle/ESP32/xenova_esp32
```

## 호스트 시험

Jenkins 의 Vehicle C Host Test 와 같은 명령이다(gcc, ASan·UBSan).

```sh
cmake -S vehicle/ESP32/test/host -B target/c-host/esp32
cmake --build target/c-host/esp32
ctest --test-dir target/c-host/esp32 --output-on-failure
```

ASan 을 쓸 수 없는 환경(Windows 등)에서는 configure 에 `-DFB3_SANITIZE=OFF` 를 준다.

시험하는 것:

- 고정 vector 의 제품 프레임 20개: payload·raw·wire 바이트, 묶기·풀기, 한 바이트씩 넣은 스트림 분리, 필드 값, 다시 묶은 바이트
- 손상 프레임 6개가 Pi 와 같은 이유(boundary/size, COBS, short, length/CRC)로 거절되고, 그 뒤 정상 프레임을 다시 받는지
- CRC 기준값(`123456789` → `0x29b1`), COBS 경계(254 B, 256 B), payload 240 B 상한
- gcc 12 이상이면 `analyze_unpack.c` 를 `-fanalyzer` 로 컴파일해, 출력 구조체를 초기화하지 않고 unpack 해도 그 값을 읽지 않는지 본다. 분석 경고가 나면 빌드가 멈춘다

## 고정 vector 복사본

`test/host/gateway-feedback-3.json` 은 develop 의 `docs/contracts/vectors/gateway-feedback-3.json`(SHA-256 `0a0d35c2edf5da2b…`)과 같은 파일이다. vehicle 브랜치에 원본이 없어서 시험 폴더에 둔다. 원본이 저장소에 있으면 cmake 가 두 파일이 같은지 검사하고, 다르면 configure 를 멈춘다. 원본이 바뀌면 복사본을 다시 복사한다.

## 핀

조립 안내서(`docs/vehicle/assembly-guide.md`) 6장의 연결표를 따른다. 지금 스케치는 PWM1(25)·PWM2(26)을 Low 로 두기만 한다.
