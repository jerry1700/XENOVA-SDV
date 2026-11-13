# ToF(VL53L0X) 근거리 제동·지도 연결 요청

대상: 플랫폼 파트(Gateway, [feedback-3 계약](../contracts/gateway-feedback-3.md)), Jetson(지도)
보낸 곳: 차량 파트(ESP32·조립)
상태: 요청 작성(2026-11-10). 회신·구현 전. 차량 쪽 책상 시험은 [조립 안내서](../vehicle/assembly-guide.md) 단계 K 에서 한다.

## 1. 바뀐 것

설계와 계약은 ToF 로 VL53L5CX(8×8 멀티존) 2개를 전제로 한다. 실물은 VL53L0X(단일 존, 기판 인쇄 `VL53LXX-V2`) 4개다. 2026-11-10 차량 파트가 다음을 정했다.

1. 2개를 쓴다. 차 앞 가운데 1개, 뒤 가운데 1개, 범퍼 높이.
2. 근거리 제동은 ESP32 가 한다.
3. 지도에도 쓴다.

VL53L0X 는 센서마다 거리 하나만 준다. 시야는 약 25° 원뿔이고 그 안의 물체 거리 하나가 나온다. 기본 설정에서 약 1.2 m 까지 재고(장거리 설정은 약 2 m), 한 번 재는 데 약 33 ms 걸린다([데이터시트](https://www.st.com/resource/en/datasheet/vl53l0x.pdf)).
앞 센서가 30 cm 앞에서 보는 폭은 약 13 cm 로 윤거(174 mm)보다 좁다. 차 정면에서 벗어난 물체는 놓칠 수 있다.
배선은 [조립 안내서 6장](../vehicle/assembly-guide.md#6-신호-연결-esp32)의 S15\~S17 이다. I2C(SDA 21, SCL 17)에 두 센서를 같이 물리고, XSHUT(16, 4)으로 하나씩 켜며 주소를 0x30·0x31 로 바꾼다.

## 2. 요청 1 — 근거리 정지를 계약에 넣기 (플랫폼·Gateway)

지금 계약으로는 Pi 에 알릴 수 없다. REPORT(`0x0f14`)의 reason(공통 0\~20)과 flags(velocity 1, steering 2, stationary 4)에 근거리 정지가 없다. ESP32 가 혼자 멈추면 Pi 는 DRIVE 가 적용 중이라고 보고, 엔코더만 멈춘 것으로 보인다.

차량 쪽이 정한 동작이다. [부록 A](../vehicle/assembly-guide.md#부록-a-책상-시험용-esp32-스케치)의 책상 시험용 스케치(bench-4)로 먼저 시험한다.

| 상황 | ESP32 동작 |
| --- | --- |
| 앞 30 cm 안에 물체 | 앞으로 가는 명령을 받지 않는다. 앞으로 가던 중이면 바로 PWM 0(드라이버 제동). 뒤로 가는 명령은 받는다 |
| 뒤 30 cm 안에 물체 | 위와 방향만 반대 |
| 33 cm 보다 멀어짐 | 다시 그 방향 명령을 받는다. 멈춘 차를 스스로 다시 움직이지는 않는다 |
| 찾았던 센서가 100 ms 동안 새 값을 주지 않음 | 그 센서 쪽 방향을 막는다. 장치를 다시 시작하기 전에는 풀지 않는다 |

회신이 필요한 것:

1. 막힌 방향의 DRIVE 를 거절할 때 REPORT 의 status·reason. 새 reason 을 둘지, 있는 값을 쓸지.
2. 적용 중이던 DRIVE 를 ESP32 가 끊었을 때 Pi 에 알리는 방법. REPORT 로 알릴지, SENSOR(`0x0f15`) 표본에 표시를 둘지.
3. 센서 고장 때 동작과 알림. 있는 reason 18 `SENSOR_UNAVAILABLE` 을 쓸지. 부팅 때 센서를 찾지 못하면 그 방향을 막을지(bench-4 는 막지 않는다. ToF 를 달기 전 시험에도 같은 스케치를 쓰려는 것이다).
4. 기준 거리(앞·뒤 30 cm)를 펌웨어 고정값으로 둘지, 설치 설정(config digest)에 넣을지. 출차·주차 경로가 장애물에 30 cm 보다 가까이 가야 하는지.
5. 근거리 제동을 하는 장치임을 HELLO_ACK capability 로 알릴지.
6. 근거리 정지 뒤 Gateway·AUTO·MANUAL 의 행동. 지금 계약은 방향을 바꾸려면 새 STOP 이 APPLIED 되어야 한다. 차량 쪽은 근거리 정지를 STOP APPLIED 로 치지 않고, 뒤로 빠지려면 Pi 가 STOP 을 먼저 보내는 것으로 이해했다. 맞는지.

## 3. 요청 2 — 지도용 단일 존 ToF 전송 (플랫폼·Gateway, Jetson)

지금 계약으로는 보낼 수 없다. `TOF_FRAGMENT3`(`0x0f16`)는 rows·cols 8, 조각 4개, 조각마다 16존으로 고정이다. ToF 빈 목록은 계약상 유효하므로, 정해질 때까지 ESP32 는 ToF capability(`0x0200`)를 광고하지 않는다.

차량 쪽이 측정마다 낼 수 있는 값:

- 센서 id(앞·뒤)와 재초기화 instance·generation. XSHUT 으로 다시 켤 때마다 바뀐다
- 장치 단조 시각(측정 구간), 거리(mm), 상태(유효·범위 밖·오류), 신호 세기(선택)
- 측정 주기는 센서마다 약 30 Hz 까지 된다. 보내는 주기는 정해 주는 대로 한다
- 설치 높이·기울기와 30 cm 에서의 시야 폭은 안내서 단계 K 에서 실측해 9.2 에 적는다

회신이 필요한 것:

1. 새 frame 을 둘지, `TOF_FRAGMENT3` 에 1×1 을 허용할지. wire 필드와 크기.
2. 센서 설치 설정(위치·자세·시야·보정)을 등록하는 방법. 지금의 `sensor_config_revision`·`sensor_calibration_sha256` 과 같은 방식인지.
3. (Jetson) 단일 존 관측을 지도에 넣는 방법. 원뿔 안의 거리 하나라 물체 모양은 알 수 없다. 범위 밖이 빈 공간의 증거가 아니라는 규칙([주행 설계](../platform/drive-pipeline-architecture.md) MAP-04)은 그대로라고 본다.

우선순위는 지금 계약을 따른다. STOP·명령·엔코더가 ToF 보다 먼저고, ToF 읽기가 제어를 막지 않는다.

## 4. 함께 고칠 문서

| 문서 | 고칠 곳 |
| --- | --- |
| [차량 제어 상세 명세](../design/vehicle-control-spec.md) | §2.1 ESP32 역할, §4.3 근거리 장애물 행(VL53L5CX → VL53L0X 앞·뒤, 뒤쪽 규칙 추가), §7 배치 |
| [RC카 시나리오](../scenario-rc-car.md) | §7 "ToF(VL53L5CX)" |
| [주행 설계](../platform/drive-pipeline-architecture.md) | 2.2 장비 표의 ToF 와 VL53L5CX 설명, DEC-01 |
| [feedback-3 계약](../contracts/gateway-feedback-3.md) | REPORT reason·flags, `TOF_FRAGMENT3`, §9.1 ToF |
| [ESP32 요청서](20261029-request-esp32.md) | ToF 단락 |
| [조립 안내서](../vehicle/assembly-guide.md) | 6장 S15\~S17, 단계 K 의 ToF, 부록 A bench-4. 이 요청과 함께 반영했다 |

## 5. 차량 쪽이 줄 것

- bench-4 책상·바퀴 띄움 시험 결과(안내서 9.2)
- 설치 높이·기울기, 30 cm 에서의 시야 폭 실측(안내서 9.2)
- 실제 펌웨어의 근거리 제동 알림과 ToF 전송. 이 요청의 회신을 받은 뒤 구현한다

## 6. 회신

2·3 의 번호마다 [담당 회신 양식](README.md#담당-회신-양식)에 맞춰 적는다.

## 진행 기록

- 2026-11-10: 차량 파트 결정(VL53L0X 앞·뒤, ESP32 근거리 제동, 지도 사용)과 지금 계약의 차이를 정리해 요청을 작성했다. 회신·구현은 없다.
