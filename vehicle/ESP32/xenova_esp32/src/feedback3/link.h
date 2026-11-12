/* feedback-3 세션, 시각 설치, 주기 표본, 명령.
 * HELLO → HELLO_ACK, CLOCK_PROBE → CLOCK_REPLY, CLOCK_INSTALL → CLOCK_ACK 를 처리하고,
 * 살아 있는 lease 가 있는 동안 엔코더 표본을 SENSOR 로 보낸다.
 * COMMAND 의 DRIVE·STOP·HOLD_STEER 를 검사해 출력에 걸고 REPORT 로 알린다.
 * ToF 센서의 새 관측이 있으면 다른 프레임이 나갈 때가 아닐 때 TOF_SAMPLE 로 보낸다.
 * 계약 docs/contracts/gateway-feedback-3.md §3·§4·§5·§6·§7·§8·§9.1.
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

/* 표본을 뜨는 간격. 계약 §5 의 50 Hz. SENSOR 와 CLOCK_REPLY 를 통틀어 이웃한 표본은 이보다 가깝지 않다.
 * 앞 표본에서 이만큼 지나기 전에 온 CLOCK_PROBE 는 그때까지 응답을 미루고, 그 자리의 표본을 CLOCK_REPLY 로 보낸다 */
#define FB3_SENSOR_PERIOD_US 20000u
/* 기한이 이 안으로 남은 DRIVE·HOLD_STEER 는 표본마다의 REPORT 로 알리지 않는다. Pi 의 기한 뒤에 닿을 수 있다 */
#define FB3_REPORT_MARGIN_US 5000u

/* 다시 온 명령인지 가려내려고 기억해 두는 최근 명령 수. 마지막 STOP 은 여기서 밀려도 따로 남는다 */
#define FB3_LINK_RESULTS 8u
/* 양쪽 엔코더 누계가 이만큼 그대로면 멈춘 것으로 본다 */
#define FB3_STILL_US 100000u

/* ToF 센서 자리. 0 은 앞 가운데, 1 은 뒤 가운데다 */
#define FB3_TOF_SENSORS 2u

/* 실제로 뜬 표본 하나. 양쪽 누계와 시각을 같은 순간에 읽은 값이어야 한다 */
struct fb3_sample {
	uint64_t device_us;
	int64_t left_ticks;
	int64_t right_ticks;
	uint8_t quality;
	int32_t steering_mrad;
	uint16_t steering_error_mrad;
};

/* ToF 센서가 낸 새 관측 하나 */
struct fb3_tof_reading {
	uint64_t device_us;     /* 측정 구간을 대표하는 장치 시각. 엔코더 표본의 시각을 빌려 쓰지 않는다 */
	uint32_t time_error_us; /* 그 시각의 오차. 측정 구간과 센서에서 읽어 오는 데 걸린 시간을 모두 감싼다 */
	uint8_t instance[16];   /* 센서를 초기화할 때마다 새로 만드는 UUID. NIL 이 아니다 */
	uint32_t generation;    /* 센서를 초기화할 때마다 1 씩 는다. 1 부터 */
	uint16_t distance_mm;   /* status 가 FB3_TOF_VALID 일 때의 거리 */
	uint8_t status;         /* FB3_TOF_* */
};

struct fb3_link_port {
	void *ctx;
	uint64_t (*now_us)(void *ctx);
	/* 지금 표본을 새로 뜬다. 센서를 읽지 못하면 false */
	bool (*latch)(void *ctx, struct fb3_sample *out);
	/* 속도와 조향을 모터·서보 출력에 건다. true 를 돌려줄 때는 두 출력이 모두 그 값으로 바뀌어 있어야 한다.
	 * 이 반환값으로 APPLIED 를 알리므로, 나중에 쓰려고 값만 적어 두고 true 를 돌려주면 안 된다.
	 * 걸지 못하면 false. cutoff 의 lock 을 쥔 채 불리므로 오래 걸리면 그동안 기한의 alarm 이 밀린다 */
	bool (*drive)(void *ctx, int16_t velocity_mmps, int16_t steering_mrad);
	/* 비상정지 입력이 눌려 있으면 true */
	bool (*estop)(void *ctx);
	/* sensor 번 ToF 에 아직 넘기지 않은 관측이 있으면 out 에 채우고 true, 없으면 false. 센서를 기다리지 않는다.
	 * 메인 루프가 돌 때마다 불린다. ToF 가 없는 빌드는 NULL 로 둔다 */
	bool (*tof)(void *ctx, unsigned sensor, struct fb3_tof_reading *out);
};

/* 등록 sensor profile. Pi 의 설치 profile 과 같아야 한다 */
struct fb3_tof_profile {
	uint32_t config_revision; /* 0 이면 그 자리에 센서가 없다 */
	uint8_t calibration_sha256[32];
};

struct fb3_link_config {
	uint8_t device_boot_session[16];
	uint16_t capabilities;      /* HELLO_ACK 로 알린다. STATIONARY_STEER 가 있어야 HOLD_STEER 를 받는다 */
	uint8_t vehicle_config_sha256[32];
	uint8_t firmware_sha256[32];
	uint8_t encoder_bits;
	uint32_t um_per_tick;       /* 엔코더 한 칸에 바퀴가 가는 거리 */
	uint64_t j_apply_us;        /* 출력을 거는 데 걸리는 시간의 상한. 기한까지 이보다 많이 남아야 건다 */
	int16_t max_velocity_mmps;  /* 받아들이는 속도와 조향의 크기 */
	int16_t max_steering_mrad;
	struct fb3_tof_profile tof[FB3_TOF_SENSORS]; /* capabilities 에 TOF_SINGLE 이 있을 때 쓴다 */
};

/* 센서 하나가 지금까지 보낸 관측 */
struct fb3_tof_source {
	uint8_t instance[16];
	uint32_t generation; /* 0 이면 아직 받은 관측이 없다 */
	uint64_t sample;     /* 이 instance 에서 보낸 관측 순번 */
	uint64_t device_us;  /* 마지막으로 받은 관측의 시각 */
};

struct fb3_reply_mark {
	uint64_t probe_sequence; /* 0 이면 빈 칸 */
	uint64_t d3_us;
};

/* 받아들인 명령 하나와 그때 보낸 REPORT */
struct fb3_result {
	uint64_t sequence; /* 0 이면 빈 칸 */
	uint8_t command[FB3_COMMAND_SIZE];
	uint8_t report[FB3_REPORT_SIZE];
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
	bool probe_held;      /* 받았지만 다음 표본을 뜰 때가 아니라서 아직 답하지 않은 CLOCK_PROBE 가 있다 */
	struct fb3_clock_probe probe;
	uint64_t probe_received_us;
	struct fb3_reply_mark replies[FB3_LINK_REPLIES];
	unsigned reply_next;
	bool has_epoch;
	uint8_t clock_epoch[16];
	uint64_t clock_fence; /* 이 session 에서 설치한 가장 큰 clock_sequence */
	struct fb3_lease leases[FB3_CUTOFF_LEASES];
	uint64_t sample_sequence; /* 부팅 뒤 내보낸 표본 순번. CLOCK_REPLY 와 SENSOR 가 같이 쓰고 session 이 바뀌어도 이어진다 */
	uint64_t sample_us;       /* 마지막으로 내보낸 표본의 시각 */
	uint64_t sensor_due_us;   /* 다음 SENSOR 를 보낼 시각 */
	/* 보낸 표본에서 구한 움직임 */
	int64_t sample_left;
	int64_t sample_right;
	bool has_velocity;        /* 표본 둘 이상에서 구한 속도가 있다 */
	int32_t velocity_mmps;
	uint64_t still_since_us;  /* 누계가 마지막으로 바뀐 표본의 시각 */
	/* 명령 */
	uint64_t command_fence;   /* 이 session 에서 받아들인 가장 큰 command_sequence */
	uint32_t generation;      /* 이 session 에서 받아들인 가장 큰 generation */
	struct fb3_result results[FB3_LINK_RESULTS];
	unsigned result_next;
	struct fb3_result stop;   /* 마지막으로 적용한 STOP */
	struct fb3_command active; /* 표본마다 보고하는 명령. 출력에 걸려 있는 DRIVE·HOLD_STEER 이거나 마지막 STOP 이다 */
	bool reporting;           /* active 가 있다 */
	bool report_due;          /* 방금 보낸 표본에 대한 REPORT 를 아직 보내지 않았다 */
	int8_t direction;         /* 마지막 STOP 뒤로 간 방향. 아직 없으면 0 */
	bool stop_needed;         /* 비상정지가 눌렸었다. 새 STOP 을 받기 전에는 DRIVE 를 받지 않는다 */
	bool fault_reported;      /* 타이머 고장을 REPORT 로 알렸다 */
	uint64_t report_sequence; /* 부팅 뒤 보낸 REPORT 순번 */
	/* ToF */
	struct fb3_tof_source tof[FB3_TOF_SENSORS];
	unsigned tof_next;        /* 다음에 먼저 볼 센서. 한 센서가 다른 센서를 굶기지 않게 한다 */
	uint32_t tof_dropped[FB3_TOF_SENSORS]; /* 받았지만 보내지 않은 관측 수 */
	uint32_t refused[FB3_REASON_COUNT]; /* 받아들이지 않은 프레임 수, 이유별 */
};

void fb3_link_init(struct fb3_link *link, const struct fb3_link_config *config,
		   const struct fb3_link_port *port, struct fb3_cutoff *cutoff);

/* 받은 프레임 하나를 처리한다. received_us 는 그 프레임을 다 받은 장치 시각이다.
 * 보낼 응답이 있으면 wire 에 구분자까지 쓰고 그 길이를, 없으면 0 을 돌려준다 */
size_t fb3_link_receive(struct fb3_link *link, const struct fb3_frame *frame, uint64_t received_us,
			uint8_t *wire, size_t cap);

/* 메인 루프가 돌 때마다 부른다. 표본에 따라붙는 REPORT, 미뤄 둔 CLOCK_REPLY, 주기 표본 SENSOR 가운데
 * 보낼 때가 된 것이 있으면 wire 에 구분자까지 쓰고 그 길이를, 아니면 0 을 돌려준다. 한 번에 프레임 하나다.
 * 그 셋 가운데 보낼 것이 없을 때에만 ToF 의 새 관측을 TOF_SAMPLE 로 보낸다 */
size_t fb3_link_poll(struct fb3_link *link, uint8_t *wire, size_t cap);

/* 그 lease 가 설치돼 있고 아직 기한 전인지 */
bool fb3_link_lease_live(struct fb3_link *link, const uint8_t clock_epoch[16], uint64_t clock_sequence);

#ifdef __cplusplus
}
#endif

#endif
