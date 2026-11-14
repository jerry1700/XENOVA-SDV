/* 이 펌웨어가 HELLO_ACK 로 알리는 설치 설정. 스케치와 호스트 시뮬레이터가 같은 값을 쓴다 */
#ifndef XENOVA_DEVICE_CONFIG_H
#define XENOVA_DEVICE_CONFIG_H

#include "feedback3/codes.h"

/* 구현한 기능만 알린다. Pi 가 요구하는 0x01f7 에 정지 조향(STATIONARY_STEER), 단일 존 ToF(TOF_SINGLE),
 * 근거리 제동(PROXIMITY_GUARD)을 더한 0x1df7 이다. 조향 측정(STEERING)은 없다 */
#define XENOVA_CAPABILITIES \
	(FB3_CAP_ACCEPT | FB3_CAP_APPLY | FB3_CAP_VELOCITY | FB3_CAP_STATIONARY | FB3_CAP_ODOMETRY \
	 | FB3_CAP_ABS_EXPIRY | FB3_CAP_CLOCK4_LATCH | FB3_CAP_DIRECTION_GATE | FB3_CAP_STATIONARY_STEER \
	 | FB3_CAP_TOF_SINGLE | FB3_CAP_PROXIMITY_GUARD)

/* 근거리 제동. 센서가 이보다 가까운 물체를 보면 그 방향을 막고(STOP), 이보다 멀어져야 푼다(CLEAR).
 * 판단에 쓸 관측이 STALE 동안 없으면 막는다. 센서는 100 ms 마다 재므로 측정 하나를 거르는 것까지 봐준다.
 * 플랫폼 파트와 15 cm 로 정했다. 시연 트랙의 차선 폭이 28.5 cm 라, 차량 제어 명세 §4.3 의 30 cm 로는 장애물을
 * 피하거나 주차할 때 차가 먼저 선다. 실제 제동 거리를 재서 정한 값이 아니다 */
#define XENOVA_PROXIMITY_STOP_MM 150
#define XENOVA_PROXIMITY_CLEAR_MM 180
#define XENOVA_PROXIMITY_STALE_US 250000
#if XENOVA_PROXIMITY_STOP_MM >= XENOVA_PROXIMITY_CLEAR_MM
#error "근거리 제동을 푸는 거리는 막는 거리보다 커야 한다"
#endif

/* ToF 의 등록 sensor profile. Pi 의 설치 profile 에 같은 revision 과 digest 를 넣어야 관측을 받는다.
 * 무엇을 해시할지 정해지기 전까지 쓰는 임시값으로, ASCII 문자열 "xenova-esp32-tof-front-dev-1",
 * "xenova-esp32-tof-rear-dev-1" 의 SHA-256 이다. 센서를 보정한 값이 아니다 */
#define XENOVA_TOF_FRONT_REVISION 1
#define XENOVA_TOF_FRONT_SHA256 \
	{ \
		0x45, 0xa1, 0x72, 0x91, 0x71, 0x8e, 0x3f, 0xb9, \
		0xdc, 0x4d, 0xa0, 0x71, 0xd5, 0x5b, 0x08, 0xb5, \
		0x7a, 0x3f, 0x38, 0x40, 0x7c, 0x43, 0x3a, 0x14, \
		0x24, 0xbe, 0xf4, 0x84, 0xaf, 0xee, 0x40, 0xf5, \
	}
#define XENOVA_TOF_REAR_REVISION 1
#define XENOVA_TOF_REAR_SHA256 \
	{ \
		0x00, 0xb4, 0x0f, 0x11, 0x72, 0x96, 0x62, 0x2d, \
		0xdd, 0xdc, 0xa3, 0x42, 0xaf, 0x98, 0xe6, 0xca, \
		0xe0, 0x7b, 0x9a, 0x61, 0xdb, 0x53, 0x79, 0x01, \
		0x40, 0x33, 0x01, 0x17, 0xb1, 0x79, 0x63, 0x77, \
	}

/* 시리얼 속도. 개발용 기본값이고, 게이트웨이와 PC 점검 도구도 같은 값을 쓴다 */
#define XENOVA_SERIAL_BAUD 460800

/* 엔코더 한 칸에 바퀴가 가는 거리(um). 바퀴 지름 69 mm 와 한 바퀴 1320칸에서 구했고 보정한 값이 아니다 */
#define XENOVA_UM_PER_TICK 164

/* 출력을 거는 데 걸리는 시간의 상한(us). 재기 전의 값이다 */
#define XENOVA_J_APPLY_US 2000

/* 받아들이는 속도(mm/s)와 조향(mrad)의 크기. 속도는 책상 시험용으로 낮게 잡은 값이다.
 * 조향 700 mrad(약 40도)는 서보 펄스 1054 us 에서 1946 us 로, 바퀴가 끝에 닿지 않는 범위(900 us 에서 2100 us) 안이다 */
#define XENOVA_MAX_VELOCITY_MMPS 600
#define XENOVA_MAX_STEERING_MRAD 700

/* 엔코더 누계 폭. Pi 의 OdometryConfig.encoder_bits 와 같아야 한다 */
#define XENOVA_ENCODER_BITS 32

/* 등록 config digest. 무엇을 해시할지 정해지기 전까지 쓰는 임시값으로,
 * ASCII 문자열 "xenova-esp32-config-dev-1" 의 SHA-256 이다 */
#define XENOVA_VEHICLE_CONFIG_SHA256 \
	{ \
		0x27, 0xb4, 0x73, 0x1f, 0x88, 0x37, 0x09, 0x9c, \
		0x0f, 0xa1, 0x8d, 0xa9, 0x27, 0x57, 0xb7, 0x3e, \
		0xd6, 0x40, 0x7f, 0xfc, 0x22, 0x4a, 0xfe, 0x6b, \
		0xbe, 0xb7, 0x64, 0x4d, 0x8b, 0xba, 0x3a, 0x60, \
	}

#endif
