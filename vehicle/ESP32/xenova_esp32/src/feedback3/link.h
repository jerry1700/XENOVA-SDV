/* feedback-3 세션과 시각 설치.
 * HELLO → HELLO_ACK, CLOCK_PROBE → CLOCK_REPLY, CLOCK_INSTALL → CLOCK_ACK 를 처리한다.
 * 계약 docs/contracts/gateway-feedback-3.md §4·§5·§7·§8. COMMAND·REPORT·SENSOR 는 아직 다루지 않는다.
 * 시계·센서·타이머는 port 로 받아 PC 호스트 시험에서도 그대로 돈다. */
#ifndef XENOVA_FB3_LINK_H
#define XENOVA_FB3_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "codes.h"
#include "cutoff.h"
#include "payloads.h"
#include "wire.h"

#ifdef __cplusplus
extern "C" {
#endif

/* INSTALL 이 가리킬 수 있게 기억해 두는 최근 CLOCK_REPLY 수.
 * Pi 는 200 ms 마다 probe 를 보내고 lease 는 길어야 1 s 다 */
#define FB3_LINK_REPLIES 8u

/* 실제로 뜬 표본 하나. 양쪽 누계와 시각을 같은 순간에 읽은 값이어야 한다 */
struct fb3_sample {
	uint64_t device_us;
	int64_t left_ticks;
	int64_t right_ticks;
	uint8_t quality;
	int32_t steering_mrad;
	uint16_t steering_error_mrad;
};

struct fb3_link_port {
	void *ctx;
	uint64_t (*now_us)(void *ctx);
	/* 지금 표본을 새로 뜬다. 센서를 읽지 못하면 false */
	bool (*latch)(void *ctx, struct fb3_sample *out);
};

struct fb3_link_config {
	uint8_t device_boot_session[16];
	uint16_t capabilities;
	uint8_t vehicle_config_sha256[32];
	uint8_t firmware_sha256[32];
	uint8_t encoder_bits;
};

struct fb3_reply_mark {
	uint64_t probe_sequence; /* 0 이면 빈 칸 */
	uint64_t d3_us;
};

/* 설치한 lease. 기한은 cutoff 의 같은 번호 칸에 걸려 있다 */
struct fb3_lease {
	bool installed;
	struct fb3_clock_install install;
	uint8_t payload[FB3_CLOCK_INSTALL_SIZE]; /* 같은 바이트의 재전송인지 본다 */
	uint64_t ack_device_us;                  /* INSTALLED 로 답한 시각 */
};

struct fb3_link {
	struct fb3_link_config config;
	struct fb3_link_port port;
	struct fb3_cutoff *cutoff;
	bool open; /* HELLO 로 맺은 Pi session 이 있다 */
	uint8_t pi_session[16];
	uint64_t probe_fence; /* 이 session 에서 답한 가장 큰 probe_sequence */
	struct fb3_reply_mark replies[FB3_LINK_REPLIES];
	unsigned reply_next;
	bool has_epoch;
	uint8_t clock_epoch[16];
	uint64_t clock_fence; /* 이 session 에서 설치한 가장 큰 clock_sequence */
	struct fb3_lease leases[FB3_CUTOFF_SLOTS];
	uint64_t sample_sequence; /* 부팅 뒤 내보낸 표본 순번. session 이 바뀌어도 이어진다 */
	uint64_t sample_us;
	uint32_t refused[FB3_REASON_COUNT]; /* 받아들이지 않은 프레임 수, 이유별 */
};

void fb3_link_init(struct fb3_link *link, const struct fb3_link_config *config,
		   const struct fb3_link_port *port, struct fb3_cutoff *cutoff);

/* 받은 프레임 하나를 처리한다. received_us 는 그 프레임을 다 받은 장치 시각이다.
 * 보낼 응답이 있으면 wire 에 구분자까지 쓰고 그 길이를, 없으면 0 을 돌려준다 */
size_t fb3_link_receive(struct fb3_link *link, const struct fb3_frame *frame, uint64_t received_us,
			uint8_t *wire, size_t cap);

/* 그 lease 가 설치돼 있고 아직 기한 전인지 */
bool fb3_link_lease_live(struct fb3_link *link, const uint8_t clock_epoch[16], uint64_t clock_sequence);

#ifdef __cplusplus
}
#endif

#endif
