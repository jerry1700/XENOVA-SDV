/* feedback-3 프레임 층: CRC-16/CCITT-FALSE, COBS, 프레임 묶기·풀기, 수신 스트림 분리.
 * 계약 docs/contracts/gateway-feedback-3.md §2 와 Pi 의 xenova_gateway/frame.py·feedback.py 동작을 따른다.
 * Arduino 헤더를 쓰지 않아 PC 호스트 시험에서도 그대로 컴파일된다. */
#ifndef XENOVA_FB3_WIRE_H
#define XENOVA_FB3_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FB3_MAX_PAYLOAD 240u  /* 묶을 수 있는 payload 상한 (Pi packet()) */
#define FB3_RAW_OVERHEAD 5u   /* id 2 + length 1 + CRC 2 */
#define FB3_MAX_WIRE 256u     /* 구분자 0x00 포함한 프레임 상한 */

/* Pi unpack() 과 같은 순서로 검사하고 같은 이유로 거절한다 */
enum fb3_frame_error {
	FB3_FRAME_OK = 0,
	FB3_FRAME_BOUNDARY_SIZE, /* 'serial boundary/size': 0x00 으로 끝나지 않거나 256 B 초과 */
	FB3_FRAME_COBS,          /* 'serial COBS' */
	FB3_FRAME_SHORT,         /* 'short serial frame': 풀린 raw 가 5 B 미만 */
	FB3_FRAME_LENGTH_CRC,    /* 'serial length/CRC' */
};

/* 프레임 층은 240 B 를 넘는 payload 도 Pi 처럼 내준다. 종류별 크기는 payload 층이 검사한다 */
struct fb3_frame {
	uint16_t id;
	uint8_t length;
	uint8_t payload[FB3_MAX_WIRE];
};

uint16_t fb3_crc16(const uint8_t *data, size_t len);

/* out 에 쓴 바이트 수. cap 이 모자라면 0 */
size_t fb3_cobs_encode(const uint8_t *data, size_t len, uint8_t *out, size_t cap);
/* 잘못된 COBS 거나 cap 이 모자라면 false */
bool fb3_cobs_decode(const uint8_t *data, size_t len, uint8_t *out, size_t cap, size_t *out_len);

/* 구분자까지 포함한 wire 길이. payload 가 240 B 를 넘거나 cap 이 모자라면 0 */
size_t fb3_encode_frame(uint16_t id, const uint8_t *payload, size_t len, uint8_t *wire, size_t cap);
/* wire 는 구분자 0x00 까지 포함한 프레임 하나 */
enum fb3_frame_error fb3_decode_frame(const uint8_t *wire, size_t len, struct fb3_frame *out);

/* 바이트 스트림을 0x00 으로 나눠 검사를 통과한 프레임만 내준다.
 * 256 B 를 넘는 조각은 다음 0x00 까지 버린다. 경계와 계수는 Pi Framer 와 같다 */
struct fb3_framer {
	uint8_t buffer[FB3_MAX_WIRE - 1];
	size_t length;
	bool discard;
	uint32_t invalid;  /* 검사에 실패한 프레임 수 */
	uint32_t oversize; /* 너무 길어 버린 조각 수 */
};

void fb3_framer_init(struct fb3_framer *framer);
/* 프레임 하나가 끝나 검사를 통과하면 out 을 채우고 true */
bool fb3_framer_push(struct fb3_framer *framer, uint8_t byte, struct fb3_frame *out);

#ifdef __cplusplus
}
#endif

#endif
