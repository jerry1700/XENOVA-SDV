/* 이 펌웨어가 HELLO_ACK 로 알리는 설치 설정. 스케치와 호스트 시뮬레이터가 같은 값을 쓴다 */
#ifndef XENOVA_DEVICE_CONFIG_H
#define XENOVA_DEVICE_CONFIG_H

#include "feedback3/codes.h"

/* 구현한 기능만 알린다. Pi 가 요구하는 0x01f7 에 정지 조향(STATIONARY_STEER)을 더한 0x05f7 이다.
 * 조향 측정(STEERING)과 ToF 는 없다 */
#define XENOVA_CAPABILITIES \
	(FB3_CAP_ACCEPT | FB3_CAP_APPLY | FB3_CAP_VELOCITY | FB3_CAP_STATIONARY | FB3_CAP_ODOMETRY \
	 | FB3_CAP_ABS_EXPIRY | FB3_CAP_CLOCK4_LATCH | FB3_CAP_DIRECTION_GATE | FB3_CAP_STATIONARY_STEER)

/* 시리얼 속도. 개발용 기본값이고, 게이트웨이와 PC 점검 도구도 같은 값을 쓴다 */
#define XENOVA_SERIAL_BAUD 460800

/* 엔코더 한 칸에 바퀴가 가는 거리(um). 바퀴 지름 69 mm 와 한 바퀴 1320칸에서 구했고 보정한 값이 아니다 */
#define XENOVA_UM_PER_TICK 164

/* 출력을 거는 데 걸리는 시간의 상한(us). 재기 전의 값이다 */
#define XENOVA_J_APPLY_US 2000

/* 받아들이는 속도(mm/s)와 조향(mrad)의 크기. 책상 시험용으로 낮게 잡은 값이다 */
#define XENOVA_MAX_VELOCITY_MMPS 600
#define XENOVA_MAX_STEERING_MRAD 500

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
