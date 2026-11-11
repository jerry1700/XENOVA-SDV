/* 이 펌웨어가 HELLO_ACK 로 알리는 설치 설정. 스케치와 호스트 시뮬레이터가 같은 값을 쓴다 */
#ifndef XENOVA_DEVICE_CONFIG_H
#define XENOVA_DEVICE_CONFIG_H

#include "feedback3/codes.h"

/* 구현한 기능만 알린다. 지금은 4시각 시계 응답과 실제 표본 latch, 엔코더 표본 주기 송신이다 */
#define XENOVA_CAPABILITIES (FB3_CAP_CLOCK4_LATCH | FB3_CAP_ODOMETRY)

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
