/* feedback-3 세션·시각 설치·주기 표본·명령과 cutoff 타이머 호스트 시험. 시계·엔코더·alarm·출력은 가짜 하드웨어다 */
#include "check.h"
#include "feedback3/link.h"

#include "link_vectors.inc"

/* 가짜 하드웨어. 카운터가 alarm 값과 같아지는 순간에만 인터럽트가 걸리고,
 * lock 을 쥔 동안 걸린 인터럽트는 unlock 뒤에 처리된다 */
struct fake {
	struct fb3_cutoff *cutoff;
	uint64_t now;
	uint64_t step; /* 시각을 읽을 때마다 흐르는 시간 */
	bool alarm_set;
	uint64_t alarm_at;
	bool irq; /* 걸렸지만 아직 처리하지 않은 인터럽트 */
	int locked;
	bool timer_dead;    /* alarm 이 울리지 않는다 */
	bool alarm_refused; /* alarm 을 맞출 수 없다 */
	unsigned alarm_writes;
	unsigned interrupts;
	bool sensor_dead;
	bool output_on; /* 모터 출력이 걸려 있다 */
	int16_t velocity;
	int16_t steering;
	unsigned drives;      /* 출력을 건 횟수 */
	unsigned cuts;        /* 출력을 끊은 횟수 */
	bool drive_fails;     /* 출력을 걸 수 없다 */
	bool drive_unlocked;  /* lock 없이 출력을 건 적이 있다 */
	bool estop;           /* 비상정지가 눌려 있다 */
	struct fb3_sample sample; /* latch 가 내줄 값. device_us 가 0 이면 그때 시각 */
};

static void fake_dispatch(struct fake *f)
{
	while (f->irq && f->locked == 0) {
		f->irq = false;
		f->interrupts++;
		fb3_cutoff_on_alarm(f->cutoff);
	}
}

static void fake_count(struct fake *f, uint64_t to)
{
	if (f->alarm_set && f->now < f->alarm_at && f->alarm_at <= to) {
		f->alarm_set = false;
		f->irq = !f->timer_dead;
	}
	f->now = to;
	fake_dispatch(f);
}

/* 시험이 시간을 보낸다. 그사이 alarm 은 맞춘 시각에 처리된다 */
static void fake_run(struct fake *f, uint64_t to)
{
	while (f->alarm_set && f->now < f->alarm_at && f->alarm_at <= to) {
		fake_count(f, f->alarm_at);
	}
	fake_count(f, to);
}

static uint64_t fake_now(void *ctx)
{
	struct fake *f = ctx;
	uint64_t now = f->now;

	if (f->step != 0) {
		fake_count(f, f->now + f->step);
	}
	return now;
}

static bool fake_set_alarm(void *ctx, uint64_t at_us)
{
	struct fake *f = ctx;

	f->alarm_writes++;
	if (f->alarm_refused) {
		return false;
	}
	f->alarm_set = true;
	f->alarm_at = at_us;
	return true;
}

static void fake_clear_alarm(void *ctx)
{
	struct fake *f = ctx;

	f->alarm_set = false;
}

static void fake_lock(void *ctx)
{
	struct fake *f = ctx;

	f->locked++;
}

static void fake_unlock(void *ctx)
{
	struct fake *f = ctx;

	f->locked--;
	fake_dispatch(f);
}

static void fake_cut(void *ctx)
{
	struct fake *f = ctx;

	f->output_on = false;
	f->cuts++;
}

static bool fake_drive(void *ctx, int16_t velocity_mmps, int16_t steering_mrad)
{
	struct fake *f = ctx;

	if (f->locked == 0) {
		f->drive_unlocked = true;
	}
	if (f->drive_fails) {
		return false;
	}
	f->output_on = true;
	f->velocity = velocity_mmps;
	f->steering = steering_mrad;
	f->drives++;
	return true;
}

static bool fake_estop(void *ctx)
{
	struct fake *f = ctx;

	return f->estop;
}

static bool fake_latch(void *ctx, struct fb3_sample *out)
{
	struct fake *f = ctx;

	/* 못 읽었을 때도 그럴듯한 값이 남아 있을 수 있다. 실패를 알리는 것은 반환값뿐이다 */
	*out = f->sample;
	if (out->device_us == 0) {
		out->device_us = fake_now(f);
	}
	return !f->sensor_dead;
}

static uint8_t PI_A[16], PI_B[16], BOOT[16], STRANGER[16], EPOCH[16], EPOCH_B[16];
static uint8_t CONFIG[32], OTHER_CONFIG[32], FIRMWARE[32];

struct bench {
	struct fake hw;
	struct fb3_cutoff cutoff;
	struct fb3_link link;
	uint8_t response[FB3_MAX_WIRE]; /* 마지막 응답의 wire */
	size_t response_len;
};

static void start_with(struct bench *b, const struct fb3_link_config *config)
{
	struct fb3_cutoff_port cutoff_port = {&b->hw, fake_now, fake_set_alarm, fake_clear_alarm, fake_lock,
					      fake_unlock, fake_cut};
	struct fb3_link_port link_port = {&b->hw, fake_now, fake_latch, fake_drive, fake_estop};

	memset(b, 0, sizeof *b);
	b->hw.cutoff = &b->cutoff;
	b->hw.now = 5000000;
	b->hw.sample.left_ticks = 1200;
	b->hw.sample.right_ticks = -340;
	b->hw.sample.quality = FB3_QUALITY_ENCODER;
	fb3_cutoff_init(&b->cutoff, &cutoff_port);
	fb3_link_init(&b->link, config, &link_port, &b->cutoff);
}

static void start(struct bench *b)
{
	struct fb3_link_config config;

	memcpy(config.device_boot_session, BOOT, 16);
	config.capabilities = FB3_CAP_CLOCK4_LATCH;
	memcpy(config.vehicle_config_sha256, CONFIG, 32);
	memcpy(config.firmware_sha256, FIRMWARE, 32);
	config.encoder_bits = 32;
	config.um_per_tick = 164;
	config.j_apply_us = 200;
	config.max_velocity_mmps = 1000;
	config.max_steering_mrad = 500;
	start_with(b, &config);
}

static void pass(struct bench *b, uint64_t us)
{
	fake_run(&b->hw, b->hw.now + us);
}

/* 프레임 하나를 넣는다. 응답이 있으면 b->response 에 남기고 풀어서 out 에 준다 */
static bool feed(struct bench *b, const struct fb3_frame *frame, uint64_t received_us, struct fb3_frame *out)
{
	b->response_len = fb3_link_receive(&b->link, frame, received_us, b->response, sizeof b->response);
	if (b->response_len == 0) {
		return false;
	}
	CHECK(fb3_decode_frame(b->response, b->response_len, out) == FB3_FRAME_OK);
	return true;
}

static bool post(struct bench *b, uint16_t id, const uint8_t *payload, size_t len, struct fb3_frame *out)
{
	uint8_t wire[FB3_MAX_WIRE];
	struct fb3_frame frame;
	size_t wire_len = fb3_encode_frame(id, payload, len, wire, sizeof wire);

	CHECK(wire_len > 0 && fb3_decode_frame(wire, wire_len, &frame) == FB3_FRAME_OK);
	return feed(b, &frame, fake_now(&b->hw), out);
}

/* 메인 루프를 한 바퀴 돌린다. 보낸 프레임이 있으면 b->response 에 남기고 풀어서 out 에 준다 */
static bool spin(struct bench *b, struct fb3_frame *out)
{
	b->response_len = fb3_link_poll(&b->link, b->response, sizeof b->response);
	if (b->response_len == 0) {
		return false;
	}
	CHECK(fb3_decode_frame(b->response, b->response_len, out) == FB3_FRAME_OK);
	return true;
}

/* 메인 루프를 한 바퀴 돌려 SENSOR 가 나왔으면 풀어 준다 */
static bool sensor(struct bench *b, struct fb3_sensor *out)
{
	struct fb3_frame frame;

	memset(out, 0, sizeof *out);
	if (!spin(b, &frame)) {
		return false;
	}
	CHECK(frame.id == FB3_SENSOR_ID && fb3_unpack_sensor(frame.payload, frame.length, out));
	return true;
}

/* 그 시각이 될 때까지 SENSOR 가 나오지 않는다 */
static bool quiet_until(struct bench *b, uint64_t to)
{
	struct fb3_frame frame;

	if (spin(b, &frame)) {
		return false;
	}
	fake_run(&b->hw, to);
	return !spin(b, &frame);
}

static bool hello(struct bench *b, const uint8_t *pi, const uint8_t *config, struct fb3_hello_ack *ack)
{
	struct fb3_hello in;
	uint8_t payload[FB3_HELLO_SIZE];
	struct fb3_frame out;

	memset(ack, 0, sizeof *ack);
	in.version = FB3_VERSION;
	memcpy(in.pi_session, pi, 16);
	memcpy(in.vehicle_config_sha256, config, 32);
	CHECK(fb3_pack_hello(&in, payload, sizeof payload) == sizeof payload);
	if (!post(b, FB3_HELLO_ID, payload, sizeof payload, &out)) {
		return false;
	}
	CHECK(out.id == FB3_HELLO_ACK_ID && fb3_unpack_hello_ack(out.payload, out.length, ack));
	return true;
}

static bool probe(struct bench *b, const uint8_t *pi, const uint8_t *device, uint64_t sequence, uint64_t p1,
		  struct fb3_clock_reply *reply)
{
	struct fb3_clock_probe in;
	uint8_t payload[FB3_CLOCK_PROBE_SIZE];
	struct fb3_frame out;

	memset(reply, 0, sizeof *reply);
	memcpy(in.pi_session, pi, 16);
	memcpy(in.device_session, device, 16);
	in.probe_sequence = sequence;
	in.p1_pi_send_us = p1;
	CHECK(fb3_pack_clock_probe(&in, payload, sizeof payload) == sizeof payload);
	if (!post(b, FB3_CLOCK_PROBE_ID, payload, sizeof payload, &out)) {
		/* 앞 표본에 붙어 온 probe 의 응답은 간격이 찬 뒤 메인 루프에서 나온다 */
		if (!b->link.probe_held) {
			return false;
		}
		if (b->hw.now < b->link.sample_us + FB3_SENSOR_PERIOD_US) {
			fake_run(&b->hw, b->link.sample_us + FB3_SENSOR_PERIOD_US);
		}
		if (!spin(b, &out)) {
			return false;
		}
	}
	CHECK(out.id == FB3_CLOCK_REPLY_ID && fb3_unpack_clock_reply(out.payload, out.length, reply));
	return true;
}

static struct fb3_clock_install lease(const uint8_t *pi, uint64_t clock_sequence, uint64_t probe_sequence,
				      uint64_t cutoff)
{
	struct fb3_clock_install in;

	memcpy(in.pi_session, pi, 16);
	memcpy(in.device_session, BOOT, 16);
	memcpy(in.clock_epoch, EPOCH, 16);
	in.clock_sequence = clock_sequence;
	in.probe_sequence = probe_sequence;
	in.reference_pi_us = 1000220;
	in.lower_offset_us = -4000100;
	in.upper_offset_us = -3999900;
	in.valid_until_pi_us = 2000000;
	in.cutoff_device_us = cutoff;
	return in;
}

static bool install(struct bench *b, const struct fb3_clock_install *in, struct fb3_clock_ack *ack)
{
	uint8_t payload[FB3_CLOCK_INSTALL_SIZE];
	struct fb3_frame out;

	memset(ack, 0, sizeof *ack);
	CHECK(fb3_pack_clock_install(in, payload, sizeof payload) == sizeof payload);
	if (!post(b, FB3_CLOCK_INSTALL_ID, payload, sizeof payload, &out)) {
		return false;
	}
	CHECK(out.id == FB3_CLOCK_ACK_ID && fb3_unpack_clock_ack(out.payload, out.length, ack));
	return true;
}

static bool rejected(const struct fb3_clock_ack *ack, enum fb3_reason reason)
{
	return ack->status == FB3_CLOCK_REJECTED && ack->reason == (uint16_t)reason;
}

/* HELLO 로 session 을 맺고 probe 하나에 답하게 한다. 그 reply 의 d3 를 돌려준다 */
static uint64_t open_and_probe(struct bench *b, uint64_t probe_sequence)
{
	struct fb3_hello_ack ack;
	struct fb3_clock_reply reply;

	CHECK(hello(b, PI_A, CONFIG, &ack));
	pass(b, 1000);
	CHECK(probe(b, PI_A, BOOT, probe_sequence, 900000, &reply));
	pass(b, 300);
	return reply.d3_device_send_us;
}

static void test_cutoff_expires_at_deadline(void)
{
	struct bench b;
	uint64_t at;

	current = "cutoff: 기한에 만료";
	start(&b);
	at = b.hw.now + 1000;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, at) == FB3_CUTOFF_LIVE);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == at);
	fake_run(&b.hw, at - 1);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_LIVE && b.cutoff.expired == 0);
	fake_run(&b.hw, at);
	CHECK(b.hw.interrupts == 1 && b.cutoff.expired == 1 && !b.hw.alarm_set);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_IDLE && !b.cutoff.fault);
}

static void test_cutoff_earliest_first(void)
{
	struct bench b;
	uint64_t t;

	current = "cutoff: 이른 기한부터, 인터럽트 안에서 다음 기한으로";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 3000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 1000) == FB3_CUTOFF_LIVE);
	CHECK(b.hw.alarm_at == t + 1000);
	fake_run(&b.hw, t + 1000);
	CHECK(fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_EXPIRED && fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_LIVE);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == t + 3000);
	fake_run(&b.hw, t + 2999);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_LIVE);
	fake_run(&b.hw, t + 3000);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_EXPIRED);
	CHECK(b.cutoff.expired == 2 && b.hw.interrupts == 2 && !b.hw.alarm_set);
}

static void test_cutoff_cancel_and_replace(void)
{
	struct bench b;
	uint64_t t;

	current = "cutoff: 취소와 바꿔 걸기";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 1000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 2000) == FB3_CUTOFF_LIVE);
	fb3_cutoff_cancel(&b.cutoff, 0);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_IDLE && b.hw.alarm_at == t + 2000);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 5000) == FB3_CUTOFF_LIVE && b.hw.alarm_at == t + 5000);
	fake_run(&b.hw, t + 2000);
	CHECK(fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_LIVE && b.cutoff.expired == 0 && b.hw.interrupts == 0);
	fb3_cutoff_cancel(&b.cutoff, 1);
	CHECK(!b.hw.alarm_set);
	fake_run(&b.hw, t + 6000);
	CHECK(b.hw.interrupts == 0 && b.cutoff.expired == 0);
}

static void test_cutoff_past_deadline(void)
{
	struct bench b;
	uint64_t t;

	current = "cutoff: 이미 지난 기한";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 1000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t - 1) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_IDLE);
	/* 걸려 있던 기한은 그대로다 */
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_LIVE && b.hw.alarm_at == t + 1000);
	CHECK(b.cutoff.expired == 0 && !b.cutoff.fault);

	current = "cutoff: 거는 사이에 지난 기한";
	start(&b);
	t = b.hw.now;
	b.hw.step = 400;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 500) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_IDLE && !b.hw.alarm_set && !b.cutoff.fault);
}

static void test_cutoff_masked_interrupt(void)
{
	struct bench b;
	uint64_t at;

	current = "cutoff: lock 중에 지난 기한은 unlock 뒤에 처리";
	start(&b);
	at = b.hw.now + 1000;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, at) == FB3_CUTOFF_LIVE);
	fake_lock(&b.hw);
	fake_count(&b.hw, at);
	CHECK(b.hw.interrupts == 0 && b.hw.irq);
	fake_unlock(&b.hw);
	CHECK(b.hw.interrupts == 1 && fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_EXPIRED);

	current = "cutoff: 인터럽트가 허용 범위 안에서 늦으면 만료";
	start(&b);
	at = b.hw.now + 1000;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, at) == FB3_CUTOFF_LIVE);
	fake_lock(&b.hw);
	fake_count(&b.hw, at + FB3_CUTOFF_LATE_US);
	fake_unlock(&b.hw);
	CHECK(b.hw.interrupts == 1 && b.cutoff.expired == 1 && !b.cutoff.fault);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_EXPIRED);

	current = "cutoff: 인터럽트가 그보다 늦으면 고장";
	start(&b);
	at = b.hw.now + 1000;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, at) == FB3_CUTOFF_LIVE);
	fake_lock(&b.hw);
	fake_count(&b.hw, at + FB3_CUTOFF_LATE_US + 1);
	fake_unlock(&b.hw);
	CHECK(b.hw.interrupts == 1 && b.cutoff.expired == 0 && b.cutoff.fault);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_FAULT);
}

static void test_cutoff_timer_fault(void)
{
	struct bench b;
	uint64_t t;

	current = "cutoff: 기한이 지나면 만료로 답하고, 처리는 alarm 에 맡김";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 1000) == FB3_CUTOFF_LIVE);
	b.hw.timer_dead = true;
	fake_run(&b.hw, t + 1000 + FB3_CUTOFF_LATE_US);
	CHECK(b.hw.interrupts == 0);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_EXPIRED && !b.cutoff.fault && b.cutoff.expired == 0);
	fake_run(&b.hw, t + 1000 + FB3_CUTOFF_LATE_US + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_FAULT && b.cutoff.fault);

	current = "cutoff: alarm 이 울리지 않으면 고장";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 1000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 900000) == FB3_CUTOFF_LIVE);
	b.hw.timer_dead = true;
	fake_run(&b.hw, t + 1000 + FB3_CUTOFF_LATE_US + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_FAULT && b.cutoff.fault);
	CHECK(fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_FAULT && !b.hw.alarm_set);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, b.hw.now + 5000) == FB3_CUTOFF_FAULT);
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, b.hw.now) == FB3_CUTOFF_FAULT);
	CHECK(b.cutoff.expired == 0);

	current = "cutoff: alarm 을 맞출 수 없으면 고장";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 1000) == FB3_CUTOFF_LIVE);
	b.hw.alarm_refused = true;
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 2000) == FB3_CUTOFF_FAULT);
	CHECK(b.cutoff.fault && !b.hw.alarm_set);
	CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_FAULT && fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_IDLE);
}

/* 기한 둘을 걸고, 이른 쪽(early 칸)의 alarm 이 울리지 않은 채 1 ms 넘게 지나게 한다 */
static void missed_alarm(struct bench *b, unsigned early)
{
	uint64_t t;

	start(b);
	t = b->hw.now;
	CHECK(fb3_cutoff_arm(&b->cutoff, early, t + 1000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_arm(&b->cutoff, 1 - early, t + 900000) == FB3_CUTOFF_LIVE);
	b->hw.timer_dead = true;
	fake_run(&b->hw, t + 1000 + FB3_CUTOFF_LATE_US + 1);
}

static void test_cutoff_missed_alarm_is_kept(void)
{
	struct bench b;

	for (unsigned early = 0; early < FB3_CUTOFF_LEASES; early++) {
		unsigned other = 1 - early;

		current = "cutoff: 다른 칸을 조회해도 울리지 않은 alarm 을 알아챔";
		missed_alarm(&b, early);
		CHECK(fb3_cutoff_query(&b.cutoff, other) == FB3_CUTOFF_FAULT && b.cutoff.fault);
		CHECK(fb3_cutoff_query(&b.cutoff, early) == FB3_CUTOFF_FAULT && !b.hw.alarm_set);

		current = "cutoff: 그 칸에 새 기한을 걸어도 흔적이 덮이지 않음";
		missed_alarm(&b, early);
		CHECK(fb3_cutoff_arm(&b.cutoff, early, b.hw.now + 5000) == FB3_CUTOFF_FAULT && b.cutoff.fault);
		CHECK(fb3_cutoff_query(&b.cutoff, early) == FB3_CUTOFF_FAULT);
		CHECK(fb3_cutoff_query(&b.cutoff, other) == FB3_CUTOFF_FAULT && !b.hw.alarm_set);

		current = "cutoff: 다른 칸에 새 기한을 걸어도 알아챔";
		missed_alarm(&b, early);
		CHECK(fb3_cutoff_arm(&b.cutoff, other, b.hw.now + 5000) == FB3_CUTOFF_FAULT && b.cutoff.fault);
		CHECK(fb3_cutoff_query(&b.cutoff, early) == FB3_CUTOFF_FAULT);

		current = "cutoff: 취소해도 흔적이 남음";
		missed_alarm(&b, early);
		fb3_cutoff_cancel(&b.cutoff, early);
		CHECK(b.cutoff.fault && fb3_cutoff_query(&b.cutoff, other) == FB3_CUTOFF_FAULT);
		CHECK(fb3_cutoff_arm(&b.cutoff, early, b.hw.now + 5000) == FB3_CUTOFF_FAULT);
		missed_alarm(&b, early);
		fb3_cutoff_cancel(&b.cutoff, other);
		fb3_cutoff_cancel(&b.cutoff, early);
		CHECK(b.cutoff.fault && fb3_cutoff_arm(&b.cutoff, early, b.hw.now + 5000) == FB3_CUTOFF_FAULT);
	}
}

static void test_hello(void)
{
	struct bench b;
	struct fb3_hello_ack ack;
	struct fb3_clock_reply reply;
	struct fb3_frame out;
	uint8_t first[FB3_MAX_WIRE], payload[FB3_HELLO_SIZE];
	size_t first_len;

	current = "hello: ACK 내용";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	CHECK(ack.version == FB3_VERSION && memcmp(ack.pi_session, PI_A, 16) == 0);
	CHECK(memcmp(ack.device_boot_session, BOOT, 16) == 0 && ack.capabilities == FB3_CAP_CLOCK4_LATCH);
	CHECK(memcmp(ack.vehicle_config_sha256, CONFIG, 32) == 0 && memcmp(ack.firmware_sha256, FIRMWARE, 32) == 0);
	CHECK(ack.encoder_bits == 32 && b.link.open);

	current = "hello: 같은 session 재전송에는 같은 ACK";
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	pass(&b, 50000);
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	CHECK(b.response_len == first_len && memcmp(b.response, first, first_len) == 0 && b.link.open);

	current = "hello: 버전·크기가 다르면 답하지 않음";
	memset(payload, 0, sizeof payload);
	payload[0] = 2;
	CHECK(!post(&b, FB3_HELLO_ID, payload, sizeof payload, &out));
	CHECK(b.link.refused[FB3_REASON_UNSUPPORTED_PROFILE] == 1);
	payload[0] = FB3_VERSION;
	CHECK(!post(&b, FB3_HELLO_ID, payload, sizeof payload - 1, &out));
	CHECK(b.link.refused[FB3_REASON_BAD_LENGTH] == 1 && b.link.open);

	current = "hello: config 가 다르면 이쪽 digest 로 답하고 session 은 맺지 않음";
	CHECK(hello(&b, PI_B, OTHER_CONFIG, &ack));
	CHECK(memcmp(ack.pi_session, PI_B, 16) == 0 && memcmp(ack.vehicle_config_sha256, CONFIG, 32) == 0);
	CHECK(!b.link.open && b.link.refused[FB3_REASON_CONFIG_MISMATCH] == 1);
	pass(&b, 1000);
	CHECK(!probe(&b, PI_B, BOOT, 1, 900000, &reply));
	CHECK(!probe(&b, PI_A, BOOT, 1, 900000, &reply));
	CHECK(b.link.refused[FB3_REASON_SESSION_MISMATCH] == 2);
}

static void test_probe(void)
{
	struct bench b;
	struct fb3_hello_ack ack;
	struct fb3_clock_reply reply, second;
	struct fb3_frame out;
	uint8_t payload[FB3_CLOCK_PROBE_SIZE];
	uint64_t sent;

	current = "probe: session 전에는 답하지 않음";
	start(&b);
	CHECK(!probe(&b, PI_A, BOOT, 1, 900000, &reply));
	CHECK(b.link.refused[FB3_REASON_SESSION_MISMATCH] == 1);

	current = "probe: 받은 뒤에 뜬 표본으로 답함";
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	b.hw.step = 7;
	sent = b.hw.now;
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	CHECK(memcmp(reply.pi_session, PI_A, 16) == 0 && memcmp(reply.device_session, BOOT, 16) == 0);
	CHECK(reply.probe_sequence == 1 && reply.p1_pi_send_us == 900000);
	CHECK(reply.d2_device_receive_us == sent && sent > 0);
	CHECK(reply.d2_device_receive_us < reply.latch_device_us && reply.latch_device_us < reply.d3_device_send_us);
	CHECK(reply.source_sample == 1 && reply.cumulative_left_ticks == 1200 && reply.cumulative_right_ticks == -340);
	CHECK(reply.quality == FB3_QUALITY_ENCODER && reply.steering_mrad == 0 && reply.steering_error_mrad == 0);

	current = "probe: 표본 순번과 시각이 늘어남";
	b.hw.sample.left_ticks = 1260;
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &second));
	CHECK(second.source_sample == 2 && second.latch_device_us > reply.latch_device_us);
	CHECK(second.cumulative_left_ticks == 1260 && second.d2_device_receive_us > reply.d3_device_send_us);

	current = "probe: 지난 순번·범위 밖 값";
	CHECK(!probe(&b, PI_A, BOOT, 2, 1300000, &reply));
	CHECK(!probe(&b, PI_A, BOOT, 1, 1300000, &reply));
	CHECK(b.link.refused[FB3_REASON_SEQUENCE_REPLAY] == 2);
	CHECK(!probe(&b, PI_A, BOOT, 0, 1300000, &reply));
	CHECK(!probe(&b, PI_A, BOOT, FB3_U63_MAX + 1, 1300000, &reply));
	CHECK(!probe(&b, PI_A, BOOT, 3, 0, &reply));
	CHECK(!probe(&b, PI_A, BOOT, 3, FB3_U63_MAX + 1, &reply));
	CHECK(b.link.refused[FB3_REASON_RANGE] == 4);

	current = "probe: 다른 session·크기";
	CHECK(!probe(&b, PI_B, BOOT, 3, 1300000, &reply));
	CHECK(!probe(&b, PI_A, STRANGER, 3, 1300000, &reply));
	CHECK(b.link.refused[FB3_REASON_SESSION_MISMATCH] == 3);
	memset(payload, 0, sizeof payload);
	CHECK(!post(&b, FB3_CLOCK_PROBE_ID, payload, sizeof payload - 1, &out));
	CHECK(b.link.refused[FB3_REASON_BAD_LENGTH] == 1);

	current = "probe: 센서를 못 읽거나 표본이 계약 밖이면 답하지 않음";
	b.hw.sensor_dead = true;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	b.hw.sensor_dead = false;
	b.hw.sample.quality = 0;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	b.hw.sample.quality = FB3_QUALITY_ENCODER | FB3_QUALITY_STEERING_MEASURED | FB3_QUALITY_STEERING_ESTIMATED;
	b.hw.sample.steering_error_mrad = 5;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	b.hw.sample.quality = FB3_QUALITY_ENCODER | FB3_QUALITY_STEERING_MEASURED;
	b.hw.sample.steering_error_mrad = 0;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	b.hw.sample.quality = FB3_QUALITY_ENCODER;
	b.hw.sample.steering_mrad = 10;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	b.hw.sample.steering_mrad = 0;
	b.hw.sample.right_ticks = (int64_t)INT32_MAX + 1;
	CHECK(!probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	CHECK(b.link.refused[FB3_REASON_SENSOR_UNAVAILABLE] == 6);
	b.hw.sample.right_ticks = INT32_MIN;
	CHECK(probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	CHECK(reply.source_sample == 3 && reply.cumulative_right_ticks == INT32_MIN);

	current = "probe: 측정 조향은 오차와 함께";
	b.hw.sample.quality = FB3_QUALITY_ENCODER | FB3_QUALITY_STEERING_MEASURED;
	b.hw.sample.steering_mrad = -1400;
	b.hw.sample.steering_error_mrad = 7;
	CHECK(probe(&b, PI_A, BOOT, 4, 1500000, &reply));
	CHECK(reply.quality == 3 && reply.steering_mrad == -1400 && reply.steering_error_mrad == 7);
	b.hw.sample.steering_mrad = 1401;
	CHECK(!probe(&b, PI_A, BOOT, 5, 1700000, &reply));

	current = "probe: 표본 시각이 늘지 않으면 답하지 않음";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	CHECK(reply.d2_device_receive_us == reply.d3_device_send_us);
	pass(&b, 50000);
	b.hw.sample.device_us = reply.latch_device_us;
	CHECK(!probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	b.hw.sample.device_us = b.hw.now - 1;
	CHECK(!probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	CHECK(b.link.refused[FB3_REASON_TIMESTAMP_ORDER] == 2 && b.link.sample_sequence == 1);
	b.hw.sample.device_us = 0;
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply) && reply.source_sample == 2);
}

static void test_install(void)
{
	struct bench b;
	struct fb3_clock_install in, changed;
	struct fb3_clock_ack ack;
	uint8_t first[FB3_MAX_WIRE];
	size_t first_len;
	unsigned writes;
	uint64_t d3, cutoff;

	current = "install: 타이머에 건 뒤 INSTALLED";
	start(&b);
	d3 = open_and_probe(&b, 1);
	cutoff = d3 + 500000;
	in = lease(PI_A, 1, 1, cutoff);
	CHECK(install(&b, &in, &ack));
	CHECK(ack.status == FB3_CLOCK_INSTALLED && ack.reason == FB3_REASON_OK);
	CHECK(memcmp(ack.pi_session, PI_A, 16) == 0 && memcmp(ack.device_session, BOOT, 16) == 0);
	CHECK(memcmp(ack.clock_epoch, EPOCH, 16) == 0 && ack.clock_sequence == 1 && ack.probe_sequence == 1);
	CHECK(ack.cutoff_device_us == cutoff && ack.device_now_us == b.hw.now && ack.device_now_us < cutoff);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == cutoff);
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && !fb3_link_lease_live(&b.link, EPOCH_B, 1));
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 2));

	current = "install: 같은 바이트 재전송에는 같은 ACK, 타이머는 그대로";
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	writes = b.hw.alarm_writes;
	pass(&b, 20000);
	CHECK(install(&b, &in, &ack));
	CHECK(b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(b.hw.alarm_writes == writes && b.hw.alarm_at == cutoff);

	current = "install: 같은 순번의 다른 바이트는 충돌";
	changed = in;
	changed.upper_offset_us++;
	CHECK(install(&b, &changed, &ack) && rejected(&ack, FB3_REASON_IDEMPOTENCY_CONFLICT));
	CHECK(ack.device_now_us == b.hw.now && ack.cutoff_device_us == cutoff && ack.clock_sequence == 1);
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && b.hw.alarm_writes == writes);

	current = "install: 시계가 흐르는 동안에도 설치·재전송·만료가 같음";
	start(&b);
	d3 = open_and_probe(&b, 1);
	b.hw.step = 3;
	cutoff = b.hw.now + 500000;
	in = lease(PI_A, 1, 1, cutoff);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(ack.device_now_us > d3 && ack.device_now_us < b.hw.now && b.hw.now < cutoff);
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	CHECK(install(&b, &in, &ack));
	CHECK(b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && b.hw.alarm_at == cutoff);
	b.hw.step = 0;
	fake_run(&b.hw, cutoff);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && b.cutoff.expired == 1 && !b.cutoff.fault);
}

static void test_install_rejects(void)
{
	struct bench b;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_frame out;
	uint8_t payload[FB3_CLOCK_INSTALL_SIZE];
	uint64_t d3, cutoff;

	start(&b);
	d3 = open_and_probe(&b, 1);
	cutoff = d3 + 500000;

	current = "install: 보낸 적 없는 probe";
	in = lease(PI_A, 1, 99, cutoff);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));
	CHECK(ack.probe_sequence == 99 && ack.cutoff_device_us == cutoff);

	current = "install: 범위 밖 값";
	in = lease(PI_A, 1, 1, cutoff);
	in.lower_offset_us = in.upper_offset_us + 1;
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_RANGE));
	in = lease(PI_A, 0, 1, cutoff);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_RANGE));
	in = lease(PI_A, FB3_U63_MAX + 1, 1, cutoff);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_RANGE));
	in = lease(PI_A, 1, 1, 0);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_RANGE));
	in = lease(PI_A, 1, 1, cutoff);
	memset(in.clock_epoch, 0, 16);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_RANGE));
	CHECK(b.link.refused[FB3_REASON_RANGE] == 5);

	current = "install: cutoff 가 d3 보다 뒤가 아님";
	in = lease(PI_A, 1, 1, d3);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_TIMESTAMP_ORDER));
	in = lease(PI_A, 1, 1, d3 - 1);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_TIMESTAMP_ORDER));

	current = "install: 이미 지난 cutoff";
	in = lease(PI_A, 1, 1, b.hw.now);
	CHECK(in.cutoff_device_us > d3);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_EXPIRED));
	CHECK(ack.device_now_us >= ack.cutoff_device_us);

	current = "install: 거절은 아무것도 설치하지 않음";
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && !b.hw.alarm_set && b.cutoff.expired == 0);
	CHECK(b.link.clock_fence == 0 && !b.link.has_epoch);

	current = "install: 다른 session·크기에는 ACK 없음";
	in = lease(PI_B, 1, 1, cutoff);
	CHECK(!install(&b, &in, &ack));
	in = lease(PI_A, 1, 1, cutoff);
	memcpy(in.device_session, STRANGER, 16);
	CHECK(!install(&b, &in, &ack));
	CHECK(b.link.refused[FB3_REASON_SESSION_MISMATCH] == 2);
	memset(payload, 0, sizeof payload);
	CHECK(!post(&b, FB3_CLOCK_INSTALL_ID, payload, sizeof payload - 1, &out));
	CHECK(b.link.refused[FB3_REASON_BAD_LENGTH] == 1);

	current = "install: 지난 순번";
	in = lease(PI_A, 3, 1, cutoff);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 1, cutoff);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_SEQUENCE_REPLAY));

	current = "install: 한 session 에 다른 clock epoch";
	in = lease(PI_A, 4, 1, cutoff);
	memcpy(in.clock_epoch, EPOCH_B, 16);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_SESSION_MISMATCH));
	CHECK(memcmp(ack.clock_epoch, EPOCH_B, 16) == 0 && !fb3_link_lease_live(&b.link, EPOCH_B, 4));

	current = "install: 타이머에 걸지 못하면 INSTALLED 가 아님";
	b.hw.alarm_refused = true;
	in = lease(PI_A, 4, 1, cutoff);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 4) && !fb3_link_lease_live(&b.link, EPOCH, 3));
}

static void test_expiry(void)
{
	struct bench b;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_clock_reply reply;
	uint64_t d3, cutoff;

	current = "expiry: 정해진 시각에 lease 만료";
	start(&b);
	d3 = open_and_probe(&b, 1);
	cutoff = d3 + 400000;
	in = lease(PI_A, 1, 1, cutoff);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, cutoff - 1);
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && b.hw.interrupts == 0);
	fake_run(&b.hw, cutoff);
	CHECK(b.hw.interrupts == 1 && b.cutoff.expired == 1);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1));

	current = "expiry: 만료 뒤 재전송은 CLOCK_EXPIRED";
	pass(&b, 5000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_EXPIRED));
	CHECK(ack.device_now_us >= cutoff && ack.cutoff_device_us == cutoff);

	current = "expiry: alarm 이 울리지 않았으면 DEVICE_FAULT";
	start(&b);
	d3 = open_and_probe(&b, 1);
	cutoff = d3 + 400000;
	in = lease(PI_A, 1, 1, cutoff);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	b.hw.timer_dead = true;
	fake_run(&b.hw, cutoff + FB3_CUTOFF_LATE_US + 1);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && b.hw.interrupts == 0);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));
	pass(&b, 1000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	in = lease(PI_A, 2, 2, reply.d3_device_send_us + 400000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 2));
}

/* lease 둘을 설치하고, 먼저 끝나는 쪽의 alarm 이 울리지 않은 채 1 ms 넘게 지나게 한다.
 * later 는 아직 기한 전인 lease, 돌려주는 값은 설치에 쓰지 않은 probe 3 의 d3 다 */
static uint64_t leases_with_missed_alarm(struct bench *b, struct fb3_clock_install *later)
{
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install soon;
	struct fb3_clock_ack ack;
	uint64_t c1;

	start(b);
	CHECK(hello(b, PI_A, CONFIG, &hello_ack));
	pass(b, 1000);
	CHECK(probe(b, PI_A, BOOT, 1, 900000, &reply));
	pass(b, 1000);
	CHECK(probe(b, PI_A, BOOT, 2, 1100000, &reply));
	pass(b, 1000);
	CHECK(probe(b, PI_A, BOOT, 3, 1300000, &reply));
	c1 = reply.d3_device_send_us + 100000;
	soon = lease(PI_A, 1, 1, c1);
	*later = lease(PI_A, 2, 2, c1 + 800000);
	CHECK(install(b, &soon, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(install(b, later, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	b->hw.timer_dead = true;
	fake_run(&b->hw, c1 + FB3_CUTOFF_LATE_US + 1);
	return reply.d3_device_send_us;
}

static void test_missed_alarm_blocks_leases(void)
{
	struct bench b;
	struct fb3_clock_install later, in;
	struct fb3_clock_ack ack;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	uint64_t d3;

	current = "고장: 아직 기한 전인 lease 도 더는 살아 있지 않음";
	leases_with_missed_alarm(&b, &later);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 2) && b.cutoff.fault);

	current = "고장: 기한 전인 lease 의 재전송에 INSTALLED 로 답하지 않음";
	leases_with_missed_alarm(&b, &later);
	CHECK(install(&b, &later, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));

	current = "고장: 울리지 않은 칸을 밀어내는 새 lease 를 받지 않음";
	d3 = leases_with_missed_alarm(&b, &later);
	in = lease(PI_A, 3, 3, b.hw.now + 400000);
	CHECK(in.cutoff_device_us > d3);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 3) && !fb3_link_lease_live(&b.link, EPOCH, 2));

	current = "고장: 새 session 으로 바뀌어도 lease 를 받지 않음";
	leases_with_missed_alarm(&b, &later);
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack));
	pass(&b, 1000);
	CHECK(probe(&b, PI_B, BOOT, 1, 900000, &reply));
	in = lease(PI_B, 1, 1, reply.d3_device_send_us + 400000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_DEVICE_FAULT));
	CHECK(b.cutoff.fault && !fb3_link_lease_live(&b.link, EPOCH, 1));
}

static void test_two_leases(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install first, in;
	struct fb3_clock_ack ack;
	uint64_t c1, c2, c3;

	current = "lease: 두 칸, 새 lease 는 가장 오래된 것을 밀어냄";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack));
	pass(&b, 1000);
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	pass(&b, 200000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	pass(&b, 200000);
	CHECK(probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	c1 = reply.d3_device_send_us + 100000;
	c2 = reply.d3_device_send_us + 300000;
	c3 = reply.d3_device_send_us + 500000;
	first = lease(PI_A, 1, 1, c1);
	CHECK(install(&b, &first, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 2, c2);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && fb3_link_lease_live(&b.link, EPOCH, 2));
	CHECK(b.hw.alarm_at == c1);
	in = lease(PI_A, 3, 3, c3);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1));
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 2) && fb3_link_lease_live(&b.link, EPOCH, 3));
	CHECK(b.hw.alarm_set && b.hw.alarm_at == c2);
	CHECK(install(&b, &first, &ack) && rejected(&ack, FB3_REASON_SEQUENCE_REPLAY));

	current = "lease: 저마다 자기 기한에 만료";
	fake_run(&b.hw, c2);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 2) && fb3_link_lease_live(&b.link, EPOCH, 3));
	CHECK(b.hw.alarm_set && b.hw.alarm_at == c3);
	fake_run(&b.hw, c3);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 3) && b.cutoff.expired == 2 && b.hw.interrupts == 2);
}

static void test_new_session(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	uint64_t d3;

	current = "session: 새 pi_session 이면 probe 기록과 lease 를 버림";
	start(&b);
	d3 = open_and_probe(&b, 1);
	in = lease(PI_A, 1, 1, d3 + 400000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack) && memcmp(hello_ack.pi_session, PI_B, 16) == 0);
	CHECK(memcmp(hello_ack.device_boot_session, BOOT, 16) == 0);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && !b.hw.alarm_set && b.cutoff.expired == 0);
	CHECK(!probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	in = lease(PI_B, 1, 1, d3 + 400000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));

	current = "session: 순번은 새로 시작하고 표본 순번은 이어짐";
	pass(&b, 1000);
	CHECK(probe(&b, PI_B, BOOT, 1, 900000, &reply));
	CHECK(reply.source_sample == 2 && memcmp(reply.pi_session, PI_B, 16) == 0);
	in = lease(PI_B, 1, 1, reply.d3_device_send_us + 400000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);

	current = "session: 같은 pi_session 의 HELLO 는 상태를 지우지 않음";
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack));
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && b.hw.alarm_set);
}

static void test_unsupported(void)
{
	struct bench b;
	struct fb3_hello_ack ack;
	struct fb3_frame out;
	uint8_t payload[FB3_REPORT_SIZE];

	current = "다루지 않는 종류에는 답하지 않음";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	memset(payload, 0, sizeof payload);
	CHECK(!post(&b, FB3_REPORT_ID, payload, FB3_REPORT_SIZE, &out));
	CHECK(!post(&b, FB3_HELLO_ACK_ID, payload, FB3_HELLO_ACK_SIZE, &out));
	CHECK(b.link.refused[FB3_REASON_UNSUPPORTED_PROFILE] == 2 && b.link.open);
}

/* session 을 맺고 lease 하나를 설치한다. 기한은 그 probe 응답의 표본에서 life_us 뒤다. 그 표본의 시각을 돌려준다 */
static uint64_t open_with_lease(struct bench *b, uint64_t life_us)
{
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	uint64_t at = open_and_probe(b, 1);

	CHECK(b->link.sample_us == at);
	in = lease(PI_A, 1, 1, at + life_us);
	CHECK(install(b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	return at;
}

/* CLOCK_PROBE 를 넣기만 한다. 곧바로 나온 응답이 있으면 true */
static bool push_probe(struct bench *b, const uint8_t *pi, uint64_t sequence, struct fb3_frame *out)
{
	struct fb3_clock_probe in;
	uint8_t payload[FB3_CLOCK_PROBE_SIZE];

	memcpy(in.pi_session, pi, 16);
	memcpy(in.device_session, BOOT, 16);
	in.probe_sequence = sequence;
	in.p1_pi_send_us = 900000 + sequence;
	CHECK(fb3_pack_clock_probe(&in, payload, sizeof payload) == sizeof payload);
	return post(b, FB3_CLOCK_PROBE_ID, payload, sizeof payload, out);
}

static void test_sensor_needs_lease(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_sensor s;
	uint64_t at;

	current = "sensor: session 이 없으면 보내지 않음";
	start(&b);
	CHECK(quiet_until(&b, b.hw.now + 100000));

	current = "sensor: lease 가 없으면 보내지 않음";
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack));
	CHECK(quiet_until(&b, b.hw.now + 100000));
	at = open_and_probe(&b, 1);
	CHECK(quiet_until(&b, at + 100000) && b.link.sample_sequence == 1);

	current = "sensor: 거절된 설치는 lease 가 아님";
	in = lease(PI_A, 1, 99, at + 900000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));
	CHECK(quiet_until(&b, b.hw.now + 100000) && b.link.sample_sequence == 1);

	current = "sensor: lease 를 설치하면 그 lease 를 가리켜 보냄";
	in = lease(PI_A, 1, 1, at + 900000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s));
	CHECK(memcmp(s.pi_session, PI_A, 16) == 0 && memcmp(s.device_session, BOOT, 16) == 0);
	CHECK(s.source_sample == 2 && s.source_device_us == b.hw.now);
	CHECK(s.cumulative_left_ticks == 1200 && s.cumulative_right_ticks == -340);
	CHECK(s.quality == FB3_QUALITY_ENCODER && s.steering_mrad == 0 && s.steering_error_mrad == 0);
	CHECK(memcmp(s.clock_epoch, EPOCH, 16) == 0 && s.clock_sequence == 1);
	CHECK(b.link.sample_sequence == 2 && b.link.sample_us == s.source_device_us);
}

static void test_sensor_period(void)
{
	struct bench b;
	struct fb3_sensor s, extra;
	uint64_t at, last;

	current = "sensor: CLOCK_REPLY 의 표본에서 한 주기 뒤에 첫 표본";
	start(&b);
	at = open_with_lease(&b, 900000);
	CHECK(quiet_until(&b, at + FB3_SENSOR_PERIOD_US - 1));
	fake_run(&b.hw, at + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 2 && s.source_device_us == at + FB3_SENSOR_PERIOD_US);

	current = "sensor: 그 뒤로 20 ms 마다, 순번은 하나씩";
	for (unsigned i = 0; i < 5; i++) {
		last = s.source_device_us;
		b.hw.sample.left_ticks += 3;
		b.hw.sample.right_ticks -= 2;
		CHECK(quiet_until(&b, last + FB3_SENSOR_PERIOD_US - 1));
		fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
		CHECK(sensor(&b, &s) && s.source_sample == 3 + i && s.source_device_us == last + FB3_SENSOR_PERIOD_US);
		CHECK(s.cumulative_left_ticks == b.hw.sample.left_ticks
		      && s.cumulative_right_ticks == b.hw.sample.right_ticks);
		CHECK(!sensor(&b, &extra));
	}

	current = "sensor: 늦게 돌면 그 표본에서 다시 20 ms";
	last = s.source_device_us + FB3_SENSOR_PERIOD_US + 3000;
	fake_run(&b.hw, last);
	CHECK(sensor(&b, &s) && s.source_sample == 8 && s.source_device_us == last);
	CHECK(quiet_until(&b, last + FB3_SENSOR_PERIOD_US - 1));
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 9);

	current = "sensor: 주기는 표본을 뜬 시각에서 잰다";
	last = s.source_device_us + FB3_SENSOR_PERIOD_US;
	fake_run(&b.hw, last);
	b.hw.step = 7;
	CHECK(sensor(&b, &s) && s.source_sample == 10 && s.source_device_us == last + 14);
	b.hw.step = 0;
	CHECK(quiet_until(&b, last + 14 + FB3_SENSOR_PERIOD_US - 1));
	fake_run(&b.hw, last + 14 + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 11);

	current = "sensor: 오래 멈췄다 돌아도 밀린 표본을 몰아 보내지 않음";
	last = s.source_device_us + 200000;
	fake_run(&b.hw, last);
	CHECK(sensor(&b, &s) && s.source_sample == 12 && s.source_device_us == last);
	CHECK(!sensor(&b, &extra));
	CHECK(quiet_until(&b, last + FB3_SENSOR_PERIOD_US - 1) && b.link.sample_sequence == 12);
}

static void test_sensor_shares_samples_with_reply(void)
{
	struct bench b;
	struct fb3_sensor s;
	struct fb3_clock_reply reply;
	uint64_t at;

	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	fake_run(&b.hw, at + 40000);
	CHECK(sensor(&b, &s) && s.source_sample == 3);

	current = "sensor: CLOCK_REPLY 가 같은 순번을 이어 씀";
	fake_run(&b.hw, at + 62000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	CHECK(reply.source_sample == 4 && reply.d2_device_receive_us == at + 62000
	      && reply.latch_device_us == at + 62000);

	current = "sensor: CLOCK_REPLY 뒤에도 그 표본에서 한 주기 뒤에";
	CHECK(quiet_until(&b, at + 62000 + FB3_SENSOR_PERIOD_US - 1));
	fake_run(&b.hw, at + 62000 + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 5 && s.source_device_us == at + 82000 && s.clock_sequence == 1);
	CHECK(quiet_until(&b, at + 82000 + FB3_SENSOR_PERIOD_US - 1));
}

static void test_probe_close_to_sample(void)
{
	struct bench b;
	struct fb3_frame out;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_sensor s;
	uint64_t at, last;

	current = "간격: 앞 표본에서 한 주기가 지나기 전에 온 probe 에는 다음 표본 자리에서 답함";
	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	last = s.source_device_us;
	pass(&b, 1000);
	CHECK(!push_probe(&b, PI_A, 2, &out) && b.link.probe_held);
	CHECK(b.link.sample_sequence == 2 && b.link.probe_fence == 1);
	CHECK(quiet_until(&b, last + FB3_SENSOR_PERIOD_US - 1) && b.link.probe_held);
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	memset(&reply, 0, sizeof reply);
	CHECK(spin(&b, &out) && out.id == FB3_CLOCK_REPLY_ID
	      && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.probe_sequence == 2 && reply.d2_device_receive_us == last + 1000);
	CHECK(reply.latch_device_us == last + FB3_SENSOR_PERIOD_US && reply.d3_device_send_us == reply.latch_device_us);
	CHECK(reply.source_sample == 3 && !b.link.probe_held && b.link.probe_fence == 2);

	current = "간격: 그 자리에 SENSOR 를 더 보내지 않고, 응답의 표본에서 한 주기 뒤에 SENSOR";
	last = reply.latch_device_us;
	CHECK(quiet_until(&b, last + FB3_SENSOR_PERIOD_US - 1) && b.link.sample_sequence == 3);
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 4 && s.source_device_us == last + FB3_SENSOR_PERIOD_US);

	current = "간격: 한 주기가 지난 뒤에 온 probe 에는 곧바로 답함";
	last = s.source_device_us + FB3_SENSOR_PERIOD_US;
	fake_run(&b.hw, last);
	memset(&reply, 0, sizeof reply);
	CHECK(push_probe(&b, PI_A, 3, &out) && out.id == FB3_CLOCK_REPLY_ID
	      && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.source_sample == 5 && reply.latch_device_us == last && !b.link.probe_held);

	current = "간격: 미뤄 둔 probe 는 새 probe 가 오면 바뀜";
	pass(&b, 1000);
	CHECK(!push_probe(&b, PI_A, 4, &out) && b.link.probe_held);
	pass(&b, 1000);
	CHECK(!push_probe(&b, PI_A, 5, &out) && b.link.probe_held);
	CHECK(!push_probe(&b, PI_A, 4, &out) && !push_probe(&b, PI_A, 5, &out));
	CHECK(b.link.refused[FB3_REASON_SEQUENCE_REPLAY] == 2 && b.link.probe_held);
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	memset(&reply, 0, sizeof reply);
	CHECK(spin(&b, &out) && out.id == FB3_CLOCK_REPLY_ID
	      && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.probe_sequence == 5 && reply.d2_device_receive_us == last + 2000 && reply.source_sample == 6);
	CHECK(b.link.probe_fence == 5 && !spin(&b, &out));
	in = lease(PI_A, 2, 4, b.hw.now + 500000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));

	current = "간격: 미뤄 둔 사이 센서를 못 읽게 되면 답하지 않고, 같은 순번을 다시 받음";
	last = b.link.sample_us;
	pass(&b, 1000);
	b.hw.sensor_dead = true;
	CHECK(!push_probe(&b, PI_A, 6, &out) && b.link.probe_held);
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	CHECK(!spin(&b, &out) && !b.link.probe_held);
	CHECK(b.link.refused[FB3_REASON_SENSOR_UNAVAILABLE] == 1 && b.link.probe_fence == 5 && b.link.sample_sequence == 6);
	b.hw.sensor_dead = false;
	memset(&reply, 0, sizeof reply);
	CHECK(push_probe(&b, PI_A, 6, &out) && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.probe_sequence == 6 && reply.source_sample == 7);

	current = "간격: 새 pi_session 이 오면 미뤄 둔 probe 를 버림";
	last = b.link.sample_us;
	pass(&b, 1000);
	CHECK(!push_probe(&b, PI_A, 7, &out) && b.link.probe_held);
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack) && !b.link.probe_held);
	fake_run(&b.hw, last + FB3_SENSOR_PERIOD_US);
	CHECK(!spin(&b, &out) && b.link.sample_sequence == 7);
}

static void test_sensor_lease_choice(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_sensor s;
	uint64_t at, far, near;

	current = "lease: 살아 있는 것 가운데 가장 새 lease 를 가리킴";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack));
	pass(&b, 1000);
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	pass(&b, 6000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	at = reply.latch_device_us;
	far = at + 800000;
	near = at + 100000;
	in = lease(PI_A, 1, 1, far);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 2, near);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 3 && s.clock_sequence == 2);
	fake_run(&b.hw, near - 1);
	CHECK(sensor(&b, &s) && s.clock_sequence == 2 && s.source_device_us == near - 1);

	current = "lease: 그 lease 가 먼저 끝나면 아직 살아 있는 lease 를 가리킴";
	fake_run(&b.hw, near - 1 + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.clock_sequence == 1 && s.source_sample == 5);
	fake_run(&b.hw, far - 1);
	CHECK(sensor(&b, &s) && s.clock_sequence == 1 && s.source_device_us == far - 1);

	current = "lease: 모두 끝나면 멈춤";
	CHECK(quiet_until(&b, far + 200000));
	CHECK(b.cutoff.expired == 2 && !b.cutoff.fault && b.link.sample_sequence == 6);

	current = "lease: 새 lease 를 설치하면 다음 표본부터 그 lease 를 가리킴";
	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.clock_sequence == 1);
	fake_run(&b.hw, at + 22000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	in = lease(PI_A, 2, 2, at + 1200000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(reply.latch_device_us == at + 40000);
	fake_run(&b.hw, at + 60000);
	CHECK(sensor(&b, &s) && s.clock_sequence == 2 && s.source_sample == 4);

	current = "lease: 표본을 뜨는 사이 기한이 되면 보내지 않음";
	start(&b);
	at = open_and_probe(&b, 1);
	in = lease(PI_A, 1, 1, at + 20014);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, at + 20000);
	b.hw.step = 7;
	CHECK(!sensor(&b, &s) && b.link.sample_sequence == 1);
	b.hw.step = 0;
	CHECK(b.cutoff.expired == 1 && !b.cutoff.fault);

	current = "lease: 표본 시각이 기한과 같으면 보내지 않음";
	start(&b);
	at = open_and_probe(&b, 1);
	in = lease(PI_A, 1, 1, at + 20001);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, at + 20000);
	b.hw.sample.device_us = at + 20001;
	CHECK(!sensor(&b, &s) && b.link.sample_sequence == 1 && fb3_link_lease_live(&b.link, EPOCH, 1));

	current = "lease: 기한 바로 앞의 표본은 보냄";
	start(&b);
	at = open_and_probe(&b, 1);
	in = lease(PI_A, 1, 1, at + 20022);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, at + 20000);
	b.hw.step = 7;
	CHECK(sensor(&b, &s) && s.source_device_us == at + 20014 && s.source_sample == 2);
	b.hw.step = 0;
}

/* lease 둘을 설치한다. 순번이 큰 쪽이 먼저 끝나면 newest_ends_first 다. 먼저 끝나는 쪽의 기한을 돌려준다 */
static uint64_t two_leases(struct bench *b, bool newest_ends_first)
{
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	uint64_t at = open_and_probe(b, 1);

	in = lease(PI_A, 1, 1, newest_ends_first ? at + 1000000 : at + 100000);
	CHECK(install(b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 1, newest_ends_first ? at + 100000 : at + 1000000);
	CHECK(install(b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	return at + 100000;
}

static void test_sensor_sees_fault_while_choosing(void)
{
	struct bench b;
	struct fb3_sensor s;
	struct fb3_frame frame;
	uint64_t soon;

	for (unsigned newest_ends_first = 0; newest_ends_first < 2; newest_ends_first++) {
		current = "고장: lease 를 고르는 조회에서 드러난 타이머 고장으로 SENSOR 를 보내지 않음";
		start(&b);
		soon = two_leases(&b, newest_ends_first);
		b.hw.timer_dead = true;
		fake_run(&b.hw, soon + FB3_CUTOFF_LATE_US - 17);
		b.link.sensor_due_us = b.hw.now;
		b.hw.step = 7;
		CHECK(!sensor(&b, &s) && b.cutoff.fault && b.link.sample_sequence == 1);
		b.hw.step = 0;
		CHECK(fb3_cutoff_query(&b.cutoff, 0) == FB3_CUTOFF_FAULT && fb3_cutoff_query(&b.cutoff, 1) == FB3_CUTOFF_FAULT);
		/* 다음 주기에 고장을 REPORT 로 한 번 알리고, 그 뒤로는 아무것도 보내지 않는다 */
		fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
		CHECK(spin(&b, &frame) && frame.id == FB3_REPORT_ID);
		CHECK(quiet_until(&b, b.hw.now + 100000) && b.link.sample_sequence == 1);

		current = "고장: 끝난 lease 다음 lease 를 조회하다 드러난 고장도 놓치지 않음";
		start(&b);
		soon = two_leases(&b, newest_ends_first);
		b.hw.timer_dead = true;
		fake_run(&b.hw, soon + FB3_CUTOFF_LATE_US - 21);
		b.link.sensor_due_us = b.hw.now;
		b.hw.step = 7;
		if (newest_ends_first) {
			CHECK(!sensor(&b, &s) && b.cutoff.fault && b.link.sample_sequence == 1);
		} else {
			/* 순번이 큰 lease 가 살아 있어 한 번만 조회한다. 그때는 아직 고장으로 볼 만큼 늦지 않았다 */
			CHECK(sensor(&b, &s) && !b.cutoff.fault && s.clock_sequence == 2);
		}
		b.hw.step = 0;

		current = "고장: 아직 고장으로 볼 만큼 늦지 않았으면 살아 있는 lease 로 보냄";
		start(&b);
		soon = two_leases(&b, newest_ends_first);
		b.hw.timer_dead = true;
		fake_run(&b.hw, soon + FB3_CUTOFF_LATE_US - 28);
		b.link.sensor_due_us = b.hw.now;
		b.hw.step = 7;
		CHECK(sensor(&b, &s) && !b.cutoff.fault && s.source_sample == 2);
		CHECK(s.clock_sequence == (newest_ends_first ? 1u : 2u));
		b.hw.step = 0;
	}
}

static void test_sensor_stops(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in, later;
	struct fb3_clock_ack ack;
	struct fb3_sensor s;
	struct fb3_frame frame;
	uint64_t at;

	current = "멈춤: lease 가 만료되면";
	start(&b);
	at = open_with_lease(&b, 100000);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	fake_run(&b.hw, at + 100000);
	CHECK(quiet_until(&b, at + 400000) && b.link.sample_sequence == 2 && b.cutoff.expired == 1);

	current = "멈춤: 타이머가 고장 나면 기한 전인 lease 로도 보내지 않음";
	leases_with_missed_alarm(&b, &later);
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(spin(&b, &frame) && frame.id == FB3_REPORT_ID && b.cutoff.fault);
	CHECK(quiet_until(&b, b.hw.now + 100000) && b.link.sample_sequence == 3);

	current = "멈춤: 새 pi_session 이 오면 그 session 의 lease 가 생길 때까지";
	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack));
	CHECK(quiet_until(&b, b.hw.now + 100000));
	CHECK(probe(&b, PI_B, BOOT, 1, 900000, &reply) && reply.source_sample == 3);
	CHECK(quiet_until(&b, b.hw.now + 100000));
	in = lease(PI_B, 1, 1, b.hw.now + 900000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && memcmp(s.pi_session, PI_B, 16) == 0 && s.source_sample == 4 && s.clock_sequence == 1);

	current = "멈춤: 같은 pi_session 의 HELLO 는 흐름을 끊지 않음";
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack));
	fake_run(&b.hw, s.source_device_us + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 5);

	current = "멈춤: 등록 config 가 다른 HELLO 가 오면";
	CHECK(hello(&b, PI_A, OTHER_CONFIG, &hello_ack));
	CHECK(quiet_until(&b, b.hw.now + 100000) && !b.link.open && b.link.sample_sequence == 5);
}

static void test_sensor_skips_bad_sample(void)
{
	struct bench b;
	struct fb3_sample good;
	struct fb3_frame frame;
	struct fb3_clock_probe probe_in;
	struct fb3_sensor s;
	uint8_t payload[FB3_CLOCK_PROBE_SIZE], wire[FB3_MAX_WIRE];
	uint64_t at;
	size_t len;

	current = "건너뜀: 센서를 못 읽으면 순번을 쓰지 않고 한 주기 뒤에 다시 봄";
	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + 20000);
	b.hw.sensor_dead = true;
	CHECK(!sensor(&b, &s) && b.link.sample_sequence == 1);
	b.hw.sensor_dead = false;
	CHECK(quiet_until(&b, at + 20000 + FB3_SENSOR_PERIOD_US - 1));
	fake_run(&b.hw, at + 20000 + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 2);

	current = "건너뜀: 계약 밖 표본";
	good = b.hw.sample;
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	b.hw.sample.quality = 0;
	CHECK(!sensor(&b, &s));
	b.hw.sample = good;
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	b.hw.sample.quality = FB3_QUALITY_ENCODER | FB3_QUALITY_STEERING_MEASURED | FB3_QUALITY_STEERING_ESTIMATED;
	b.hw.sample.steering_error_mrad = 5;
	CHECK(!sensor(&b, &s));
	b.hw.sample = good;
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	b.hw.sample.steering_mrad = 10;
	CHECK(!sensor(&b, &s));
	b.hw.sample = good;
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	b.hw.sample.right_ticks = (int64_t)INT32_MAX + 1;
	CHECK(!sensor(&b, &s));
	b.hw.sample = good;

	current = "건너뜀: 표본 시각이 앞 표본보다 뒤가 아니면";
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	b.hw.sample.device_us = b.link.sample_us;
	CHECK(!sensor(&b, &s) && b.link.sample_sequence == 2);
	b.hw.sample = good;
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 3 && s.source_device_us == b.hw.now);

	current = "건너뜀: 프레임을 담을 자리가 모자라면 순번을 쓰지 않음";
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(fb3_link_poll(&b.link, b.response, FB3_SENSOR_SIZE) == 0 && b.link.sample_sequence == 3);
	memcpy(probe_in.pi_session, PI_A, 16);
	memcpy(probe_in.device_session, BOOT, 16);
	probe_in.probe_sequence = 2;
	probe_in.p1_pi_send_us = 1100000;
	CHECK(fb3_pack_clock_probe(&probe_in, payload, sizeof payload) == sizeof payload);
	len = fb3_encode_frame(FB3_CLOCK_PROBE_ID, payload, sizeof payload, wire, sizeof wire);
	CHECK(len > 0 && fb3_decode_frame(wire, len, &frame) == FB3_FRAME_OK);
	CHECK(fb3_link_receive(&b.link, &frame, b.hw.now, b.response, FB3_CLOCK_REPLY_SIZE) == 0);
	CHECK(b.link.sample_sequence == 3 && b.link.probe_fence == 1 && !b.link.probe_held);
	fake_run(&b.hw, b.hw.now + FB3_SENSOR_PERIOD_US);
	CHECK(sensor(&b, &s) && s.source_sample == 4);
}

static bool engage_ok(void *arg)
{
	struct fake *f = arg;

	if (f->locked == 0) {
		f->drive_unlocked = true;
	}
	f->output_on = true;
	f->drives++;
	return !f->drive_fails;
}

static void test_output_cutoff(void)
{
	struct bench b;
	uint64_t t;

	current = "출력: 기한까지 여유가 있어야 걸고, lock 을 쥔 채 건다";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 1000, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	CHECK(b.hw.drives == 1 && !b.hw.drive_unlocked && b.hw.output_on);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == t + 1000);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_LIVE);

	current = "출력: 여유가 모자란 기한은 걸지 않고, 걸려 있던 출력과 기한은 그대로";
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 200, 200, engage_ok, &b.hw) == FB3_CUTOFF_EXPIRED);
	CHECK(fb3_cutoff_engage(&b.cutoff, t, 0, engage_ok, &b.hw) == FB3_CUTOFF_EXPIRED);
	CHECK(b.hw.drives == 1 && b.hw.output_on && b.hw.cuts == 0 && b.hw.alarm_at == t + 1000);
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 201, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	CHECK(b.hw.drives == 2 && b.hw.alarm_at == t + 201);

	current = "출력: 기한이 되면 alarm 인터럽트가 끊음";
	fake_run(&b.hw, t + 200);
	CHECK(b.hw.output_on && b.hw.cuts == 0);
	fake_run(&b.hw, t + 201);
	CHECK(!b.hw.output_on && b.hw.cuts == 1 && b.hw.interrupts == 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_EXPIRED && !b.cutoff.fault);

	current = "출력: lease 의 기한이 지나는 것으로는 끊지 않음";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 500) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 2000, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	fake_run(&b.hw, t + 500);
	CHECK(b.hw.output_on && b.hw.cuts == 0 && b.hw.alarm_at == t + 2000);
	fake_run(&b.hw, t + 2000);
	CHECK(!b.hw.output_on && b.hw.cuts == 1 && b.cutoff.expired == 2);

	current = "출력: cut 은 곧바로 끊고 출력의 기한을 지움";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 1, t + 5000) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 1000, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	fb3_cutoff_cut(&b.cutoff);
	CHECK(!b.hw.output_on && b.hw.cuts == 1 && b.hw.alarm_at == t + 5000);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);
	fake_run(&b.hw, t + 1000);
	CHECK(b.hw.cuts == 1 && b.hw.interrupts == 0);

	current = "출력: 걸지 못하면 끊고 FAULT, 타이머는 고장이 아님";
	start(&b);
	t = b.hw.now;
	b.hw.drive_fails = true;
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 1000, 200, engage_ok, &b.hw) == FB3_CUTOFF_FAULT);
	CHECK(!b.hw.output_on && b.hw.cuts == 1 && !b.cutoff.fault && !b.hw.alarm_set);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);

	current = "출력: 출력의 alarm 이 울리지 않으면 다음 검사에서 끊고 고장으로 굳음";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 1000, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	b.hw.timer_dead = true;
	fake_run(&b.hw, t + 1000 + FB3_CUTOFF_LATE_US + 1);
	CHECK(b.hw.output_on && b.hw.cuts == 0);
	CHECK(fb3_cutoff_faulted(&b.cutoff) && !b.hw.output_on && b.hw.cuts == 1);
	CHECK(fb3_cutoff_engage(&b.cutoff, b.hw.now + 1000, 200, engage_ok, &b.hw) == FB3_CUTOFF_FAULT);
	CHECK(!b.hw.output_on && b.hw.drives == 1);

	current = "출력: lease 의 alarm 이 울리지 않아도 출력을 끊음";
	start(&b);
	t = b.hw.now;
	CHECK(fb3_cutoff_arm(&b.cutoff, 0, t + 500) == FB3_CUTOFF_LIVE);
	CHECK(fb3_cutoff_engage(&b.cutoff, t + 900000, 200, engage_ok, &b.hw) == FB3_CUTOFF_LIVE);
	b.hw.timer_dead = true;
	fake_run(&b.hw, t + 500 + FB3_CUTOFF_LATE_US + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_FAULT && !b.hw.output_on && b.hw.cuts == 1);

	current = "출력: 고장이 없으면 고장이라 하지 않음";
	start(&b);
	CHECK(!fb3_cutoff_faulted(&b.cutoff) && b.hw.cuts == 0);
}

static struct fb3_command stop_command(const uint8_t *pi, uint64_t sequence, uint32_t generation)
{
	struct fb3_command c;

	memset(&c, 0, sizeof c);
	memcpy(c.pi_session, pi, 16);
	memcpy(c.device_session, BOOT, 16);
	c.command_sequence = sequence;
	c.generation = generation;
	c.counter = (uint8_t)(sequence + 40);
	c.command_kind = FB3_COMMAND_STOP;
	return c;
}

static struct fb3_command drive_command(uint64_t sequence, uint64_t stop_sequence, uint64_t clock_sequence,
					uint64_t cutoff, int16_t velocity, int16_t steering)
{
	struct fb3_command c = stop_command(PI_A, sequence, 1);

	memcpy(c.clock_epoch, EPOCH, 16);
	c.clock_sequence = clock_sequence;
	c.issued_pi_us = 1500000;
	c.expires_pi_us = 1590000;
	c.cutoff_device_us = cutoff;
	c.command_kind = FB3_COMMAND_DRIVE;
	c.stop_command_sequence = stop_sequence;
	c.velocity_mmps = velocity;
	c.steering_mrad = steering;
	return c;
}

static struct fb3_command hold_command(uint64_t sequence, uint64_t stop_sequence, uint64_t clock_sequence,
				       uint64_t cutoff, int16_t steering)
{
	struct fb3_command c = drive_command(sequence, stop_sequence, clock_sequence, cutoff, 0, steering);

	c.command_kind = FB3_COMMAND_HOLD_STEER;
	return c;
}

/* COMMAND 를 넣는다. REPORT 가 나왔으면 풀어 준다 */
static bool command(struct bench *b, const struct fb3_command *in, struct fb3_report *out)
{
	uint8_t payload[FB3_COMMAND_SIZE];
	struct fb3_frame frame;

	memset(out, 0, sizeof *out);
	CHECK(fb3_pack_command(in, payload, sizeof payload) == sizeof payload);
	if (!post(b, FB3_COMMAND_ID, payload, sizeof payload, &frame)) {
		return false;
	}
	CHECK(frame.id == FB3_REPORT_ID && fb3_unpack_report(frame.payload, frame.length, out));
	return true;
}

/* 명령의 칸을 그대로 돌려주는 REPORT 인지 */
static bool echoes(const struct fb3_report *r, const struct fb3_command *c)
{
	return memcmp(r->pi_session, c->pi_session, 16) == 0 && memcmp(r->device_session, BOOT, 16) == 0
		&& r->command_sequence == c->command_sequence && r->generation == c->generation
		&& r->counter == c->counter && r->command_kind == c->command_kind
		&& memcmp(r->clock_epoch, c->clock_epoch, 16) == 0 && r->clock_sequence == c->clock_sequence
		&& r->cutoff_device_us == c->cutoff_device_us && r->stop_command_sequence == c->stop_command_sequence;
}

static bool applied(struct bench *b, const struct fb3_command *in)
{
	struct fb3_report r;

	return command(b, in, &r) && r.status == FB3_REPORT_APPLIED && r.reason == FB3_REASON_OK && echoes(&r, in);
}

static bool turned_down(struct bench *b, const struct fb3_command *in, enum fb3_reason reason)
{
	struct fb3_report r;

	return command(b, in, &r) && r.status == FB3_REPORT_REJECTED && r.reason == (uint16_t)reason && echoes(&r, in)
		&& r.flags == 0 && r.velocity_mmps == 0 && r.stationary == 0;
}

/* 메인 루프를 한 바퀴 돌려 REPORT 가 나왔으면 풀어 준다 */
static bool next_report(struct bench *b, struct fb3_report *out)
{
	struct fb3_frame frame;

	memset(out, 0, sizeof *out);
	if (!spin(b, &frame)) {
		return false;
	}
	CHECK(frame.id == FB3_REPORT_ID && fb3_unpack_report(frame.payload, frame.length, out));
	return true;
}

/* session 을 맺고 lease 하나(기한은 900 ms 뒤)를 설치한 뒤 STOP 1 을 적용한다. probe 응답의 표본 시각을 돌려준다 */
static uint64_t ready_to_drive(struct bench *b)
{
	struct fb3_command stop = stop_command(PI_A, 1, 1);
	uint64_t at = open_with_lease(b, 900000);

	CHECK(applied(b, &stop));
	return at;
}

static void test_command_stop(void)
{
	struct bench b;
	struct fb3_hello_ack ack;
	struct fb3_command stop, other;
	struct fb3_report r;
	uint8_t first[FB3_MAX_WIRE];
	size_t first_len;
	unsigned cuts;

	current = "stop: session 전에는 답하지 않음";
	start(&b);
	stop = stop_command(PI_A, 1, 7);
	CHECK(!command(&b, &stop, &r) && b.link.refused[FB3_REASON_SESSION_MISMATCH] == 1);

	current = "stop: lease 가 없어도 출력을 끊고 APPLIED 로 알림";
	CHECK(hello(&b, PI_A, CONFIG, &ack));
	cuts = b.hw.cuts;
	CHECK(command(&b, &stop, &r));
	CHECK(r.status == FB3_REPORT_APPLIED && r.reason == FB3_REASON_OK && echoes(&r, &stop));
	CHECK(r.report_sequence == 1 && r.source_sample == 0 && r.source_device_us == 0 && r.flags == 0);
	CHECK(b.hw.cuts == cuts + 1 && !b.hw.output_on);
	CHECK(b.link.stop.sequence == 1 && b.link.generation == 7 && b.link.command_fence == 1);

	current = "stop: 같은 바이트가 다시 오면 처음 REPORT 만 다시 보냄";
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	cuts = b.hw.cuts;
	pass(&b, 30000);
	CHECK(command(&b, &stop, &r));
	CHECK(b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(b.hw.cuts == cuts && b.link.report_sequence == 1);

	current = "stop: 같은 순번의 다른 바이트는 충돌";
	other = stop;
	other.counter++;
	CHECK(turned_down(&b, &other, FB3_REASON_IDEMPOTENCY_CONFLICT));
	CHECK(b.link.report_sequence == 2 && b.link.stop.sequence == 1);

	current = "stop: 낮은 generation 의 STOP 도 적용하되 generation 은 내리지 않음";
	other = stop_command(PI_A, 2, 3);
	CHECK(applied(&b, &other) && b.link.generation == 7 && b.link.stop.sequence == 2);

	current = "stop: 계약 밖 STOP 은 거절하지만 출력은 끊음";
	cuts = b.hw.cuts;
	other = stop_command(PI_A, 3, 7);
	other.velocity_mmps = 10;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE) && b.hw.cuts == cuts + 1);
	other = stop_command(PI_A, 3, 7);
	other.clock_sequence = 1;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 7);
	other.cutoff_device_us = 1;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 7);
	other.stop_command_sequence = 2;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 7);
	memcpy(other.clock_epoch, EPOCH, 16);
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 7);
	other.reserved = 1;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 7);
	other.reason = FB3_COMMAND_REASON_MAX + 1;
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 3, 0);
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, 0, 7);
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	other = stop_command(PI_A, FB3_U63_MAX + 1, 7);
	CHECK(turned_down(&b, &other, FB3_REASON_RANGE));
	CHECK(b.link.stop.sequence == 2 && b.link.command_fence == 2 && b.hw.cuts == cuts + 10);

	current = "stop: 설계에 있는 reason 값은 받음";
	other = stop_command(PI_A, 3, 7);
	other.reason = FB3_COMMAND_REASON_MAX;
	CHECK(applied(&b, &other));

	current = "stop: 받은 적 없는 지난 순번은 거절";
	other = stop_command(PI_A, 10, 7);
	CHECK(applied(&b, &other));
	other = stop_command(PI_A, 5, 7);
	CHECK(turned_down(&b, &other, FB3_REASON_SEQUENCE_REPLAY) && b.link.stop.sequence == 10);

	current = "stop: 결과 칸에서 밀려난 뒤에도 마지막 STOP 은 다시 온 것으로 알아봄";
	stop = stop_command(PI_A, 11, 7);
	CHECK(applied(&b, &stop));
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	memset(b.link.results, 0, sizeof b.link.results);
	CHECK(command(&b, &stop, &r) && b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
}

static void test_command_drive(void)
{
	struct bench b;
	struct fb3_command drive, next, stop;
	struct fb3_report r;
	uint8_t first[FB3_MAX_WIRE];
	size_t first_len;
	unsigned cuts;
	uint64_t at, cutoff;

	current = "drive: 출력에 건 뒤 APPLIED 로 알림";
	start(&b);
	at = ready_to_drive(&b);
	cutoff = b.hw.now + 80000;
	drive = drive_command(2, 1, 1, cutoff, 300, -120);
	CHECK(command(&b, &drive, &r));
	CHECK(r.status == FB3_REPORT_APPLIED && r.reason == FB3_REASON_OK && echoes(&r, &drive));
	CHECK(r.report_sequence == 2 && r.source_sample == 1 && r.source_device_us == at);
	CHECK(r.flags == FB3_REPORT_STATIONARY && r.stationary == 0 && r.velocity_mmps == 0 && r.steering_mrad == 0);
	CHECK(b.hw.output_on && b.hw.velocity == 300 && b.hw.steering == -120 && b.hw.drives == 1 && !b.hw.drive_unlocked);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == cutoff && b.link.command_fence == 2);

	current = "drive: 같은 명령이 다시 와도 다시 걸지 않고 기한을 늘리지 않음";
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	pass(&b, 30000);
	CHECK(command(&b, &drive, &r) && b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(b.hw.drives == 1 && b.hw.alarm_at == cutoff && b.link.report_sequence == 2);

	current = "drive: 같은 순번의 다른 바이트는 충돌이고 걸려 있던 출력은 그대로";
	next = drive;
	next.velocity_mmps = 500;
	CHECK(turned_down(&b, &next, FB3_REASON_IDEMPOTENCY_CONFLICT));
	CHECK(b.hw.output_on && b.hw.velocity == 300 && b.hw.drives == 1 && b.hw.alarm_at == cutoff);

	current = "drive: 기한이 되면 타이머가 출력을 끊음";
	fake_run(&b.hw, cutoff - 1);
	CHECK(b.hw.output_on);
	cuts = b.hw.cuts;
	fake_run(&b.hw, cutoff);
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_EXPIRED && !b.cutoff.fault);

	current = "drive: 기한 뒤에 같은 명령이 다시 와도 출력을 켜지 않음";
	CHECK(command(&b, &drive, &r) && b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(!b.hw.output_on && b.hw.drives == 1);

	current = "drive: 새 명령은 새 기한으로 다시 검";
	next = drive_command(3, 1, 1, b.hw.now + 80000, 320, 50);
	CHECK(applied(&b, &next));
	CHECK(b.hw.output_on && b.hw.velocity == 320 && b.hw.steering == 50 && b.hw.drives == 2);
	CHECK(b.hw.alarm_at == next.cutoff_device_us);
	pass(&b, 20000);
	next = drive_command(4, 1, 1, b.hw.now + 80000, 340, 0);
	CHECK(applied(&b, &next) && b.hw.velocity == 340 && b.hw.drives == 3 && b.hw.alarm_at == next.cutoff_device_us);

	current = "drive: STOP 이 오면 곧바로 끊고 출력의 기한을 지움";
	cuts = b.hw.cuts;
	stop = stop_command(PI_A, 5, 1);
	CHECK(applied(&b, &stop));
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == at + 900000);
	fake_run(&b.hw, next.cutoff_device_us);
	CHECK(b.hw.cuts == cuts + 1 && b.hw.interrupts == 1);

	current = "drive: 속도 0 도 걸 수 있음";
	next = drive_command(6, 5, 1, b.hw.now + 80000, 0, 200);
	CHECK(applied(&b, &next) && b.hw.output_on && b.hw.velocity == 0 && b.hw.steering == 200);
}

static void test_drive_rejects(void)
{
	struct bench b;
	struct fb3_command drive, stop;
	struct fb3_report r;
	struct fb3_frame out;
	uint8_t payload[FB3_COMMAND_SIZE];
	uint64_t at, cutoff;

	start(&b);
	at = open_with_lease(&b, 900000);
	cutoff = b.hw.now + 80000;

	current = "거절: 적용한 STOP 이 없으면";
	drive = drive_command(1, 0, 1, cutoff, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	CHECK(!b.hw.output_on && b.hw.drives == 0 && b.link.command_fence == 0);
	stop = stop_command(PI_A, 1, 1);
	CHECK(applied(&b, &stop));

	current = "거절: 마지막 STOP 이 아닌 것을 가리키면";
	drive = drive_command(2, 9, 1, cutoff, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	drive = drive_command(2, 0, 1, cutoff, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));

	current = "거절: 범위 밖 값";
	drive = drive_command(2, 1, 1, cutoff, 1001, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, -1001, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 501);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, -501);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.issued_pi_us = 0;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.issued_pi_us = 0;
	drive.expires_pi_us = 50000;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.expires_pi_us = drive.issued_pi_us;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.expires_pi_us = drive.issued_pi_us + FB3_COMMAND_LIFE_US + 1;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, 0, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, FB3_U63_MAX + 1, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.reserved = 1;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.reason = FB3_COMMAND_REASON_MAX + 1;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.generation = 0;
	CHECK(turned_down(&b, &drive, FB3_REASON_RANGE));

	current = "거절: 다루지 않는 명령 종류";
	drive = drive_command(2, 1, 1, cutoff, 0, 100);
	drive.command_kind = FB3_COMMAND_HOLD_STEER;
	CHECK(turned_down(&b, &drive, FB3_REASON_UNSUPPORTED_PROFILE));
	drive.command_kind = 0;
	CHECK(turned_down(&b, &drive, FB3_REASON_UNSUPPORTED_PROFILE));
	drive.command_kind = 9;
	CHECK(turned_down(&b, &drive, FB3_REASON_UNSUPPORTED_PROFILE));

	current = "거절: 설치하지 않은 lease";
	drive = drive_command(2, 1, 2, cutoff, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_CLOCK_UNAVAILABLE));
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	memcpy(drive.clock_epoch, EPOCH_B, 16);
	CHECK(turned_down(&b, &drive, FB3_REASON_CLOCK_UNAVAILABLE));

	current = "거절: lease 를 넘는 기한";
	drive = drive_command(2, 1, 1, cutoff, 300, 0);
	drive.issued_pi_us = 1950000;
	drive.expires_pi_us = 2000001;
	CHECK(turned_down(&b, &drive, FB3_REASON_DEADLINE_MISMATCH));
	drive = drive_command(2, 1, 1, at + 900001, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DEADLINE_MISMATCH));

	current = "거절: 걸 시간이 모자란 기한";
	drive = drive_command(2, 1, 1, b.hw.now, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_COMMAND_EXPIRED));
	drive = drive_command(2, 1, 1, b.hw.now - 1, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_COMMAND_EXPIRED));
	drive = drive_command(2, 1, 1, b.hw.now + 200, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_COMMAND_EXPIRED));

	current = "거절: 거절은 출력을 걸지 않고 순번도 쓰지 않음";
	CHECK(!b.hw.output_on && b.hw.drives == 0 && b.link.command_fence == 1 && b.hw.alarm_at == at + 900000);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);
	drive = drive_command(2, 1, 1, at + 900000, 300, 0);
	drive.issued_pi_us = 1900000;
	drive.expires_pi_us = 2000000;
	CHECK(applied(&b, &drive) && b.hw.output_on && b.hw.alarm_at == at + 900000);

	current = "거절: 가던 방향을 바꾸려면 새 STOP 이 있어야 함";
	drive = drive_command(3, 1, 1, cutoff, -300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	CHECK(b.hw.output_on && b.hw.velocity == 300 && b.hw.drives == 1);
	drive = drive_command(3, 1, 1, cutoff, 0, 0);
	CHECK(applied(&b, &drive) && b.hw.velocity == 0);
	drive = drive_command(4, 1, 1, cutoff, -300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	stop = stop_command(PI_A, 4, 1);
	CHECK(applied(&b, &stop));
	drive = drive_command(5, 1, 1, cutoff, -300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	drive = drive_command(5, 4, 1, cutoff, -300, 0);
	CHECK(applied(&b, &drive) && b.hw.velocity == -300);
	drive = drive_command(6, 4, 1, cutoff, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
	drive = drive_command(6, 4, 1, cutoff, -200, 0);
	CHECK(applied(&b, &drive) && b.hw.velocity == -200);

	current = "거절: 낮은 generation 의 DRIVE";
	stop = stop_command(PI_A, 7, 5);
	CHECK(applied(&b, &stop));
	drive = drive_command(8, 7, 1, cutoff, 300, 0);
	drive.generation = 4;
	CHECK(turned_down(&b, &drive, FB3_REASON_GENERATION_REPLAY));
	drive.generation = 5;
	CHECK(applied(&b, &drive));
	drive = drive_command(9, 7, 1, cutoff, 300, 0);
	drive.generation = 6;
	CHECK(applied(&b, &drive) && b.link.generation == 6);

	current = "거절: 다른 session·크기에는 답하지 않음";
	drive = drive_command(10, 7, 1, cutoff, 300, 0);
	memcpy(drive.pi_session, PI_B, 16);
	CHECK(!command(&b, &drive, &r));
	drive = drive_command(10, 7, 1, cutoff, 300, 0);
	memcpy(drive.device_session, STRANGER, 16);
	CHECK(!command(&b, &drive, &r));
	memset(payload, 0, sizeof payload);
	CHECK(!post(&b, FB3_COMMAND_ID, payload, sizeof payload - 1, &out));
	CHECK(b.link.refused[FB3_REASON_SESSION_MISMATCH] == 2 && b.link.refused[FB3_REASON_BAD_LENGTH] == 1);
	CHECK(b.link.command_fence == 9);
}

static void test_drive_faults(void)
{
	struct bench b;
	struct fb3_command drive, stop;
	struct fb3_report r;
	struct fb3_sensor s;
	struct fb3_frame frame;
	unsigned cuts;

	current = "고장: 출력을 걸지 못하면 FAULT 로 알리고 끊음";
	start(&b);
	ready_to_drive(&b);
	b.hw.drive_fails = true;
	cuts = b.hw.cuts;
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(command(&b, &drive, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_DEVICE_FAULT);
	CHECK(echoes(&r, &drive) && r.flags == 0);
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1 && b.link.command_fence == 1 && !b.cutoff.fault);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);
	b.hw.drive_fails = false;
	CHECK(applied(&b, &drive) && b.hw.output_on);

	current = "고장: 출력의 alarm 이 울리지 않았으면 다음 DRIVE 에서 끊고 FAULT";
	start(&b);
	ready_to_drive(&b);
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive));
	b.hw.timer_dead = true;
	fake_run(&b.hw, drive.cutoff_device_us + FB3_CUTOFF_LATE_US + 1);
	CHECK(b.hw.output_on);
	drive = drive_command(3, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(command(&b, &drive, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_DEVICE_FAULT);
	CHECK(!b.hw.output_on && b.cutoff.fault && b.hw.drives == 1);

	current = "고장: 명령이 없어도 메인 루프가 알아채 끊고, 어느 명령의 것도 아닌 FAULT 로 한 번 알림";
	start(&b);
	ready_to_drive(&b);
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive));
	b.hw.timer_dead = true;
	fake_run(&b.hw, drive.cutoff_device_us + FB3_CUTOFF_LATE_US + 1);
	CHECK(b.hw.output_on);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_DEVICE_FAULT);
	CHECK(r.command_sequence == 0 && r.command_kind == 0 && r.counter == 0 && r.generation == 1 && r.flags == 0);
	CHECK(!b.hw.output_on && b.cutoff.fault);
	CHECK(!spin(&b, &frame));
	fake_run(&b.hw, b.hw.now + 100000);
	CHECK(!spin(&b, &frame));
	stop = stop_command(PI_A, 3, 1);
	CHECK(applied(&b, &stop));
	drive = drive_command(4, 3, 1, b.hw.now + 80000, 300, 0);
	CHECK(command(&b, &drive, &r) && r.status == FB3_REPORT_FAULT && !b.hw.output_on);

	current = "비상정지: 눌리면 출력을 끊고 알림";
	start(&b);
	ready_to_drive(&b);
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive) && b.hw.output_on);
	b.hw.estop = true;
	fake_run(&b.hw, b.link.sensor_due_us);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_E_STOP);
	CHECK(r.command_sequence == 0 && r.command_kind == 0 && !b.hw.output_on);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);

	current = "비상정지: 끊긴 DRIVE 를 걸려 있는 것처럼 알리지 않음";
	CHECK(sensor(&b, &s) && s.source_device_us < drive.cutoff_device_us - FB3_REPORT_MARGIN_US);
	CHECK(!spin(&b, &frame) && !b.link.reporting);

	current = "비상정지: 눌려 있는 동안의 DRIVE 는 거절";
	drive = drive_command(3, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_E_STOP) && !b.hw.output_on);

	current = "비상정지: 풀어도 새 STOP 을 받기 전에는 DRIVE 를 받지 않음";
	b.hw.estop = false;
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED) && !b.hw.output_on);
	stop = stop_command(PI_A, 3, 1);
	CHECK(applied(&b, &stop));
	drive = drive_command(4, 3, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive) && b.hw.output_on);

	current = "비상정지: DRIVE 를 받는 순간 눌려 있어도 끊음";
	b.hw.estop = true;
	drive = drive_command(5, 3, 1, b.hw.now + 80000, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_E_STOP) && !b.hw.output_on);
	b.hw.estop = false;
	drive = drive_command(5, 3, 1, b.hw.now + 80000, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED));
}

static void test_reports_follow_samples(void)
{
	struct bench b;
	struct fb3_command drive;
	struct fb3_clock_reply reply;
	struct fb3_report r;
	struct fb3_sensor s;
	struct fb3_frame frame;
	uint64_t at, last;

	current = "보고: STOP 을 표본마다 그 표본을 가리켜 다시 알림";
	start(&b);
	at = ready_to_drive(&b);
	CHECK(!spin(&b, &frame));
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_APPLIED && r.command_sequence == 1);
	CHECK(r.command_kind == FB3_COMMAND_STOP && r.report_sequence == 2);
	CHECK(r.source_sample == 2 && r.source_device_us == s.source_device_us);
	CHECK(r.flags == (FB3_REPORT_VELOCITY | FB3_REPORT_STATIONARY) && r.velocity_mmps == 0 && r.stationary == 0);
	CHECK(!spin(&b, &frame));

	current = "보고: 누계가 100 ms 동안 그대로면 멈춘 것으로 알림";
	for (unsigned i = 0; i < 3; i++) {
		fake_run(&b.hw, at + 40000 + 20000 * i);
		CHECK(sensor(&b, &s) && next_report(&b, &r) && r.source_sample == 3 + i && r.stationary == 0);
	}
	fake_run(&b.hw, at + 100000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.source_sample == 6 && r.stationary == 1);
	CHECK(r.report_sequence == 6);

	current = "보고: 누계가 바뀌면 그 속도를 싣고 멈춘 시간을 다시 잼";
	b.hw.sample.left_ticks += 100;
	b.hw.sample.right_ticks += 100;
	fake_run(&b.hw, at + 120000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.velocity_mmps == 820 && r.stationary == 0);
	b.hw.sample.left_ticks -= 50;
	fake_run(&b.hw, at + 140000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.velocity_mmps == -205 && r.stationary == 0);
	for (unsigned i = 0; i < 4; i++) {
		fake_run(&b.hw, at + 160000 + 20000 * i);
		CHECK(sensor(&b, &s) && next_report(&b, &r) && r.velocity_mmps == 0 && r.stationary == 0);
	}
	fake_run(&b.hw, at + 240000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 1);

	current = "보고: 한쪽 누계만 바뀌어도 멈춘 것이 아님";
	b.hw.sample.right_ticks += 1;
	fake_run(&b.hw, at + 260000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 0 && r.velocity_mmps == 4);
	b.hw.sample.left_ticks -= 1;
	fake_run(&b.hw, at + 280000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 0);
	for (unsigned i = 0; i < 4; i++) {
		fake_run(&b.hw, at + 300000 + 20000 * i);
		CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 0);
	}
	fake_run(&b.hw, at + 380000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 1);
	at += 140000;

	current = "보고: CLOCK_REPLY 의 표본에도 따라붙음";
	fake_run(&b.hw, at + 262000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	CHECK(next_report(&b, &r) && r.source_sample == reply.source_sample
	      && r.source_device_us == reply.latch_device_us && r.stationary == 1);
	CHECK(!spin(&b, &frame));

	current = "보고: 표본이 오래 끊겼다 이어지면 멈춘 시간을 다시 잼";
	last = at + 262000 + 150000;
	fake_run(&b.hw, last);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.stationary == 0 && r.velocity_mmps == 0);

	current = "보고: DRIVE 는 기한까지만 알림";
	drive = drive_command(2, 1, 1, last + 30000, 300, 0);
	CHECK(applied(&b, &drive));
	fake_run(&b.hw, last + 20000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.command_sequence == 2 && r.command_kind == FB3_COMMAND_DRIVE);
	CHECK(r.status == FB3_REPORT_APPLIED && echoes(&r, &drive) && r.source_sample == s.source_sample);
	fake_run(&b.hw, last + 40000);
	CHECK(!b.hw.output_on && sensor(&b, &s));
	CHECK(!spin(&b, &frame) && !b.link.reporting);
	fake_run(&b.hw, last + 60000);
	CHECK(sensor(&b, &s) && !spin(&b, &frame));

	current = "보고: 기한이 5 ms 안으로 남은 DRIVE 는 알리지 않음";
	last = s.source_device_us;
	drive = drive_command(3, 1, 1, last + 20000 + FB3_REPORT_MARGIN_US, 300, 0);
	CHECK(applied(&b, &drive));
	fake_run(&b.hw, last + 20000);
	CHECK(sensor(&b, &s) && b.hw.output_on && !spin(&b, &frame) && !b.link.reporting);
	drive = drive_command(4, 1, 1, last + 40000 + FB3_REPORT_MARGIN_US + 1, 300, 0);
	CHECK(applied(&b, &drive));
	fake_run(&b.hw, last + 40000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.command_sequence == 4);
}

/* 정지 조향을 한다고 알리는 설정으로 시작한다 */
static void start_steering(struct bench *b)
{
	start(b);
	b->link.config.capabilities |= FB3_CAP_STATIONARY_STEER;
}

static void test_hold_steer(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_command hold, drive, stop;
	struct fb3_report r;
	struct fb3_sensor s;
	struct fb3_frame frame;
	uint8_t first[FB3_MAX_WIRE];
	size_t first_len;
	unsigned cuts;
	uint64_t at, cutoff, last;

	current = "hold: 정지 조향을 알리지 않은 설정에서는 받지 않음";
	start(&b);
	ready_to_drive(&b);
	hold = hold_command(2, 1, 1, b.hw.now + 80000, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_UNSUPPORTED_PROFILE) && b.hw.drives == 0 && b.link.command_fence == 1);

	current = "hold: 조향을 건 뒤 APPLIED 로 알림";
	start_steering(&b);
	at = ready_to_drive(&b);
	cutoff = b.hw.now + 80000;
	hold = hold_command(2, 1, 1, cutoff, 150);
	CHECK(command(&b, &hold, &r));
	CHECK(r.status == FB3_REPORT_APPLIED && r.reason == FB3_REASON_OK && echoes(&r, &hold));
	CHECK(r.command_kind == FB3_COMMAND_HOLD_STEER && r.report_sequence == 2 && r.source_sample == 1
	      && r.source_device_us == at);
	CHECK(r.flags == FB3_REPORT_STATIONARY && r.stationary == 0 && r.velocity_mmps == 0 && r.steering_mrad == 0);
	CHECK(b.hw.velocity == 0 && b.hw.steering == 150 && b.hw.drives == 1 && !b.hw.drive_unlocked);
	CHECK(b.hw.alarm_set && b.hw.alarm_at == cutoff && b.link.command_fence == 2);

	current = "hold: 같은 명령이 다시 와도 다시 걸지 않고 기한을 늘리지 않음";
	first_len = b.response_len;
	memcpy(first, b.response, first_len);
	pass(&b, 30000);
	CHECK(command(&b, &hold, &r) && b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(b.hw.drives == 1 && b.hw.alarm_at == cutoff && b.link.report_sequence == 2);
	drive = hold;
	drive.steering_mrad = 160;
	CHECK(turned_down(&b, &drive, FB3_REASON_IDEMPOTENCY_CONFLICT));
	CHECK(b.hw.steering == 150 && b.hw.drives == 1 && b.hw.alarm_at == cutoff);

	current = "hold: 기한이 되면 타이머가 출력의 기한을 끝내고, 다시 온 명령이 되살리지 않음";
	fake_run(&b.hw, cutoff - 1);
	cuts = b.hw.cuts;
	CHECK(b.hw.output_on && fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_LIVE);
	fake_run(&b.hw, cutoff);
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_EXPIRED && !b.cutoff.fault);
	CHECK(command(&b, &hold, &r) && b.response_len == first_len && memcmp(b.response, first, first_len) == 0);
	CHECK(!b.hw.output_on && b.hw.drives == 1);

	current = "hold: 속도 칸이 0 이 아니면 거절";
	hold = hold_command(3, 1, 1, b.hw.now + 80000, 150);
	hold.velocity_mmps = 1;
	CHECK(turned_down(&b, &hold, FB3_REASON_RANGE));
	hold.velocity_mmps = -1;
	CHECK(turned_down(&b, &hold, FB3_REASON_RANGE));
	CHECK(b.hw.drives == 1 && b.link.command_fence == 2);

	current = "hold: STOP 참조·범위·lease·기한 검사는 DRIVE 와 같음";
	hold = hold_command(3, 9, 1, b.hw.now + 80000, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_DIRECTION_STOP_REQUIRED));
	hold = hold_command(3, 0, 1, b.hw.now + 80000, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_DIRECTION_STOP_REQUIRED));
	hold = hold_command(3, 1, 1, b.hw.now + 80000, 501);
	CHECK(turned_down(&b, &hold, FB3_REASON_RANGE));
	hold = hold_command(3, 1, 1, b.hw.now + 80000, -501);
	CHECK(turned_down(&b, &hold, FB3_REASON_RANGE));
	hold = hold_command(3, 1, 1, b.hw.now + 80000, 150);
	hold.expires_pi_us = hold.issued_pi_us + FB3_COMMAND_LIFE_US + 1;
	CHECK(turned_down(&b, &hold, FB3_REASON_RANGE));
	hold = hold_command(3, 1, 2, b.hw.now + 80000, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_CLOCK_UNAVAILABLE));
	hold = hold_command(3, 1, 1, at + 900001, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_DEADLINE_MISMATCH));
	hold = hold_command(3, 1, 1, b.hw.now + 200, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_COMMAND_EXPIRED));
	CHECK(b.hw.drives == 1 && b.link.command_fence == 2 && !b.hw.output_on);

	current = "hold: 가던 방향으로 치지 않음";
	hold = hold_command(3, 1, 1, b.hw.now + 80000, -200);
	CHECK(applied(&b, &hold) && b.hw.steering == -200 && b.hw.drives == 2 && b.link.direction == 0);
	drive = drive_command(4, 1, 1, b.hw.now + 80000, -300, -200);
	CHECK(applied(&b, &drive) && b.hw.velocity == -300 && b.link.direction == -1);

	current = "hold: 걸려 있던 DRIVE 의 모터 출력을 내리고 기한을 제 것으로 바꿈";
	hold = hold_command(5, 1, 1, b.hw.now + 60000, 100);
	CHECK(applied(&b, &hold));
	CHECK(b.hw.velocity == 0 && b.hw.steering == 100 && b.hw.drives == 4 && b.hw.alarm_at == hold.cutoff_device_us);

	current = "hold: 뒤에도 방향을 바꾸려면 새 STOP 이 있어야 함";
	drive = drive_command(6, 1, 1, b.hw.now + 50000, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_DIRECTION_STOP_REQUIRED) && b.hw.velocity == 0 && b.hw.drives == 4);
	stop = stop_command(PI_A, 6, 1);
	CHECK(applied(&b, &stop) && !b.hw.output_on);
	hold = hold_command(7, 1, 1, b.hw.now + 80000, 0);
	CHECK(turned_down(&b, &hold, FB3_REASON_DIRECTION_STOP_REQUIRED));
	hold = hold_command(7, 6, 1, b.hw.now + 80000, 0);
	CHECK(applied(&b, &hold) && b.hw.steering == 0);
	drive = drive_command(8, 6, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive) && b.hw.velocity == 300);

	current = "hold: 표본마다 그 표본을 가리켜 알리고 기한까지만 알림";
	start_steering(&b);
	at = ready_to_drive(&b);
	hold = hold_command(2, 1, 1, at + 50000, 150);
	CHECK(applied(&b, &hold));
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.command_sequence == 2 && r.command_kind == FB3_COMMAND_HOLD_STEER);
	CHECK(r.status == FB3_REPORT_APPLIED && echoes(&r, &hold) && r.source_sample == s.source_sample
	      && r.source_device_us == s.source_device_us);
	fake_run(&b.hw, at + 40000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.command_sequence == 2 && r.source_sample == s.source_sample);
	fake_run(&b.hw, at + 60000);
	CHECK(!b.hw.output_on && sensor(&b, &s));
	CHECK(!spin(&b, &frame) && !b.link.reporting);
	fake_run(&b.hw, at + 80000);
	CHECK(sensor(&b, &s) && !spin(&b, &frame));

	current = "hold: 기한이 5 ms 안으로 남았으면 알리지 않음";
	last = s.source_device_us;
	hold = hold_command(3, 1, 1, last + 20000 + FB3_REPORT_MARGIN_US, 150);
	CHECK(applied(&b, &hold));
	fake_run(&b.hw, last + 20000);
	CHECK(sensor(&b, &s) && b.hw.output_on && !spin(&b, &frame) && !b.link.reporting);
	hold = hold_command(4, 1, 1, last + 40000 + FB3_REPORT_MARGIN_US + 1, 150);
	CHECK(applied(&b, &hold));
	fake_run(&b.hw, last + 40000);
	CHECK(sensor(&b, &s) && next_report(&b, &r) && r.command_sequence == 4);

	current = "hold: 걸려 있는 HOLD_STEER 가 쓰는 lease 는 새 lease 가 밀어내지 않음";
	start_steering(&b);
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack) && (hello_ack.capabilities & FB3_CAP_STATIONARY_STEER));
	pass(&b, 1000);
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	pass(&b, 6000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	at = reply.latch_device_us;
	in = lease(PI_A, 1, 1, at + 800000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 2, at + 900000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	stop = stop_command(PI_A, 1, 1);
	CHECK(applied(&b, &stop));
	hold = hold_command(2, 1, 1, at + 80000, 150);
	CHECK(applied(&b, &hold));
	pass(&b, 6000);
	CHECK(probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	in = lease(PI_A, 3, 3, at + 1000000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && fb3_link_lease_live(&b.link, EPOCH, 2) && b.hw.output_on);
	fake_run(&b.hw, at + 80000);
	CHECK(!b.hw.output_on);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && fb3_link_lease_live(&b.link, EPOCH, 3));

	current = "hold: 비상정지가 눌려 있으면 거절하고, 풀어도 새 STOP 이 있어야 함";
	start_steering(&b);
	ready_to_drive(&b);
	b.hw.estop = true;
	hold = hold_command(2, 1, 1, b.hw.now + 80000, 150);
	CHECK(turned_down(&b, &hold, FB3_REASON_E_STOP) && b.hw.drives == 0);
	b.hw.estop = false;
	CHECK(turned_down(&b, &hold, FB3_REASON_DIRECTION_STOP_REQUIRED) && b.hw.drives == 0);
	stop = stop_command(PI_A, 2, 1);
	CHECK(applied(&b, &stop));

	current = "hold: 조향을 걸지 못하면 FAULT 로 알림";
	b.hw.drive_fails = true;
	cuts = b.hw.cuts;
	hold = hold_command(3, 2, 1, b.hw.now + 80000, 150);
	CHECK(command(&b, &hold, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_DEVICE_FAULT);
	CHECK(echoes(&r, &hold) && r.flags == 0);
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1 && b.link.command_fence == 2 && !b.cutoff.fault);
	b.hw.drive_fails = false;
	CHECK(applied(&b, &hold) && b.hw.steering == 150);
}

static void test_samples_keep_one_period_apart(void)
{
	struct bench b;
	struct fb3_frame out;
	struct fb3_clock_reply reply;
	struct fb3_sensor s;
	struct fb3_report r;
	uint64_t at, previous, received, slot;
	unsigned replies, sensors;

	/* SENSOR 가 흐르는 동안 CLOCK_PROBE 를 주기의 여러 자리에 넣는다. 1 ms 마다 메인 루프를 돌린다 */
	for (unsigned offset = 0; offset < 20; offset++) {
		current = "간격: 종류와 상관없이 이웃한 표본은 한 주기보다 가깝지 않음";
		start(&b);
		at = open_with_lease(&b, 900000);
		previous = at;
		received = 0;
		replies = sensors = 0;
		for (unsigned ms = 1; ms <= 120; ms++) {
			fake_run(&b.hw, at + 1000 * ms);
			if (ms == 40 + offset) {
				received = b.hw.now;
				slot = b.link.sample_us + FB3_SENSOR_PERIOD_US;
				if (push_probe(&b, PI_A, 2, &out)) {
					/* 표본 자리에 딱 맞춰 왔을 때만 곧바로 답한다 */
					CHECK(offset == 0 && fb3_unpack_clock_reply(out.payload, out.length, &reply));
					CHECK(reply.latch_device_us == previous + FB3_SENSOR_PERIOD_US);
					previous = reply.latch_device_us;
					replies++;
				} else {
					CHECK(b.link.probe_held && slot > received && slot - received < FB3_SENSOR_PERIOD_US);
				}
			}
			while (spin(&b, &out)) {
				if (out.id == FB3_SENSOR_ID) {
					CHECK(fb3_unpack_sensor(out.payload, out.length, &s));
					CHECK(s.source_device_us == previous + FB3_SENSOR_PERIOD_US);
					previous = s.source_device_us;
					sensors++;
				} else {
					CHECK(out.id == FB3_CLOCK_REPLY_ID && fb3_unpack_clock_reply(out.payload, out.length, &reply));
					CHECK(reply.latch_device_us == previous + FB3_SENSOR_PERIOD_US);
					CHECK(reply.d2_device_receive_us == received && reply.probe_sequence == 2);
					previous = reply.latch_device_us;
					replies++;
				}
			}
		}
		/* 120 ms 에 표본 자리는 여섯이고, 그 가운데 하나를 CLOCK_REPLY 가 차지한다 */
		CHECK(replies == 1 && sensors == 5 && b.link.sample_sequence == 7 && !b.link.probe_held);
	}

	current = "간격: 한 주기에 1 us 모자랄 때 온 probe 도 미룸";
	start(&b);
	at = open_with_lease(&b, 900000);
	fake_run(&b.hw, at + FB3_SENSOR_PERIOD_US - 1);
	CHECK(!push_probe(&b, PI_A, 2, &out) && b.link.probe_held);
	fake_run(&b.hw, at + FB3_SENSOR_PERIOD_US);
	CHECK(spin(&b, &out) && out.id == FB3_CLOCK_REPLY_ID && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.latch_device_us == at + FB3_SENSOR_PERIOD_US
	      && reply.d2_device_receive_us == at + FB3_SENSOR_PERIOD_US - 1);

	current = "간격: 미뤄 둔 probe 가 있어도 앞 표본의 REPORT 는 그 표본 바로 뒤에 감";
	start(&b);
	at = ready_to_drive(&b);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && s.source_sample == 2);
	CHECK(!push_probe(&b, PI_A, 2, &out) && b.link.probe_held);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_APPLIED && r.source_sample == 2 && b.link.probe_held);
	CHECK(quiet_until(&b, at + 40000 - 1) && b.link.probe_held);
	fake_run(&b.hw, at + 40000);
	CHECK(spin(&b, &out) && out.id == FB3_CLOCK_REPLY_ID && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.source_sample == 3 && reply.latch_device_us == at + 40000);
	CHECK(next_report(&b, &r) && r.source_sample == 3 && r.source_device_us == at + 40000);
	CHECK(!spin(&b, &out));

	current = "간격: 미뤄 둔 probe 가 있어도 그 자리에서 비상정지를 알아채 먼저 알림";
	fake_run(&b.hw, at + 41000);
	CHECK(!push_probe(&b, PI_A, 3, &out) && b.link.probe_held);
	b.hw.estop = true;
	CHECK(quiet_until(&b, at + 60000 - 1));
	fake_run(&b.hw, at + 60000);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_E_STOP && b.link.probe_held);
	CHECK(spin(&b, &out) && out.id == FB3_CLOCK_REPLY_ID && fb3_unpack_clock_reply(out.payload, out.length, &reply));
	CHECK(reply.probe_sequence == 3 && reply.source_sample == 4 && reply.latch_device_us == at + 60000);

	current = "간격: 미뤄 둔 probe 가 있어도 그 자리에서 타이머 고장을 알아채 먼저 알림";
	start(&b);
	at = ready_to_drive(&b);
	fake_run(&b.hw, at + 20000);
	CHECK(sensor(&b, &s) && next_report(&b, &r));
	CHECK(!push_probe(&b, PI_A, 2, &out) && b.link.probe_held);
	b.hw.timer_dead = true;
	fake_run(&b.hw, at + 900000 + FB3_CUTOFF_LATE_US + 1);
	CHECK(next_report(&b, &r) && r.status == FB3_REPORT_FAULT && r.reason == FB3_REASON_DEVICE_FAULT);
}

static void test_command_session_and_lease(void)
{
	struct bench b;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_install in;
	struct fb3_clock_ack ack;
	struct fb3_command drive, stop;
	struct fb3_report r;
	unsigned cuts;
	uint64_t at;

	current = "session: 새 pi_session 이 오면 출력을 끊고 명령 기록을 버림";
	start(&b);
	ready_to_drive(&b);
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive) && b.hw.output_on);
	cuts = b.hw.cuts;
	CHECK(hello(&b, PI_B, CONFIG, &hello_ack));
	CHECK(!b.hw.output_on && b.hw.cuts == cuts + 1);
	CHECK(fb3_cutoff_query(&b.cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_IDLE);
	CHECK(b.link.command_fence == 0 && b.link.stop.sequence == 0 && !b.link.reporting && b.link.generation == 0);
	CHECK(!command(&b, &drive, &r));
	stop = stop_command(PI_B, 1, 1);
	CHECK(applied(&b, &stop) && b.link.stop.sequence == 1);

	current = "session: 같은 pi_session 의 HELLO 는 걸려 있는 출력을 끊지 않음";
	start(&b);
	ready_to_drive(&b);
	drive = drive_command(2, 1, 1, b.hw.now + 80000, 300, 0);
	CHECK(applied(&b, &drive));
	cuts = b.hw.cuts;
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack));
	CHECK(b.hw.output_on && b.hw.cuts == cuts && b.link.command_fence == 2);

	current = "lease: 출력에 걸려 있는 DRIVE 가 쓰는 lease 는 새 lease 가 밀어내지 않음";
	start(&b);
	CHECK(hello(&b, PI_A, CONFIG, &hello_ack));
	pass(&b, 1000);
	CHECK(probe(&b, PI_A, BOOT, 1, 900000, &reply));
	pass(&b, 6000);
	CHECK(probe(&b, PI_A, BOOT, 2, 1100000, &reply));
	at = reply.latch_device_us;
	in = lease(PI_A, 1, 1, at + 800000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	in = lease(PI_A, 2, 2, at + 900000);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	stop = stop_command(PI_A, 1, 1);
	CHECK(applied(&b, &stop));
	drive = drive_command(2, 1, 1, at + 80000, 300, 0);
	CHECK(applied(&b, &drive));
	pass(&b, 6000);
	CHECK(probe(&b, PI_A, BOOT, 3, 1300000, &reply));
	in = lease(PI_A, 3, 3, at + 1000000);
	CHECK(install(&b, &in, &ack) && rejected(&ack, FB3_REASON_CLOCK_UNAVAILABLE));
	CHECK(fb3_link_lease_live(&b.link, EPOCH, 1) && fb3_link_lease_live(&b.link, EPOCH, 2) && b.hw.output_on);
	fake_run(&b.hw, at + 80000);
	CHECK(!b.hw.output_on);
	CHECK(install(&b, &in, &ack) && ack.status == FB3_CLOCK_INSTALLED);
	CHECK(!fb3_link_lease_live(&b.link, EPOCH, 1) && fb3_link_lease_live(&b.link, EPOCH, 3));

	current = "lease: lease 가 끝나면 그 lease 로는 DRIVE 를 걸지 못함";
	start(&b);
	at = ready_to_drive(&b);
	fake_run(&b.hw, at + 900000);
	drive = drive_command(2, 1, 1, at + 900000, 300, 0);
	CHECK(turned_down(&b, &drive, FB3_REASON_COMMAND_EXPIRED) && !b.hw.output_on);
}

/* 고정 vector 의 HELLO → PROBE → INSTALL 을 넣으면 vector 의 ACK·REPLY·CLOCK_ACK 와 같은 바이트가 나온다 */
static void vector_session(struct bench *bench)
{
	struct fb3_link_config config;
	struct fb3_frame frame, out;
	struct fb3_hello_ack hello_ack;
	struct fb3_clock_reply reply;
	struct fb3_clock_ack ack;
	uint8_t wire[FB3_MAX_WIRE], expected[FB3_MAX_WIRE];
	size_t len, expected_len;

	current = "vector: HELLO_ACK";
	memset(&config, 0, sizeof config);
	memset(&hello_ack, 0, sizeof hello_ack);
	expected_len = from_hex(VEC_HELLO_ACK3, expected, sizeof expected);
	CHECK(fb3_decode_frame(expected, expected_len, &frame) == FB3_FRAME_OK
	      && fb3_unpack_hello_ack(frame.payload, frame.length, &hello_ack));
	memcpy(config.device_boot_session, hello_ack.device_boot_session, 16);
	config.capabilities = hello_ack.capabilities;
	memcpy(config.vehicle_config_sha256, hello_ack.vehicle_config_sha256, 32);
	memcpy(config.firmware_sha256, hello_ack.firmware_sha256, 32);
	config.encoder_bits = hello_ack.encoder_bits;
	start_with(bench, &config);
	len = from_hex(VEC_HELLO3, wire, sizeof wire);
	CHECK(fb3_decode_frame(wire, len, &frame) == FB3_FRAME_OK);
	CHECK(feed(bench, &frame, bench->hw.now, &out));
	CHECK(bench->response_len == expected_len && memcmp(bench->response, expected, expected_len) == 0);

	current = "vector: CLOCK_REPLY";
	memset(&reply, 0, sizeof reply);
	expected_len = from_hex(VEC_CLOCK_REPLY3, expected, sizeof expected);
	CHECK(fb3_decode_frame(expected, expected_len, &frame) == FB3_FRAME_OK
	      && fb3_unpack_clock_reply(frame.payload, frame.length, &reply));
	bench->link.sample_sequence = reply.source_sample - 1;
	bench->hw.sample.device_us = reply.latch_device_us;
	bench->hw.sample.left_ticks = reply.cumulative_left_ticks;
	bench->hw.sample.right_ticks = reply.cumulative_right_ticks;
	bench->hw.sample.quality = reply.quality;
	bench->hw.sample.steering_mrad = reply.steering_mrad;
	bench->hw.sample.steering_error_mrad = reply.steering_error_mrad;
	bench->hw.now = reply.d3_device_send_us;
	len = from_hex(VEC_CLOCK_PROBE3, wire, sizeof wire);
	CHECK(fb3_decode_frame(wire, len, &frame) == FB3_FRAME_OK);
	CHECK(feed(bench, &frame, reply.d2_device_receive_us, &out));
	CHECK(bench->response_len == expected_len && memcmp(bench->response, expected, expected_len) == 0);

	current = "vector: CLOCK_ACK";
	memset(&ack, 0, sizeof ack);
	expected_len = from_hex(VEC_CLOCK_ACK3, expected, sizeof expected);
	CHECK(fb3_decode_frame(expected, expected_len, &frame) == FB3_FRAME_OK
	      && fb3_unpack_clock_ack(frame.payload, frame.length, &ack));
	bench->hw.now = ack.device_now_us;
	len = from_hex(VEC_CLOCK_INSTALL3, wire, sizeof wire);
	CHECK(fb3_decode_frame(wire, len, &frame) == FB3_FRAME_OK);
	CHECK(feed(bench, &frame, bench->hw.now, &out));
	CHECK(bench->response_len == expected_len && memcmp(bench->response, expected, expected_len) == 0);
	CHECK(bench->hw.alarm_set && bench->hw.alarm_at == ack.cutoff_device_us);
	CHECK(fb3_link_lease_live(&bench->link, ack.clock_epoch, ack.clock_sequence));
}

/* vector 의 SENSOR 가 실은 표본을 그 시각에 뜨게 하면 같은 바이트가 나온다 */
static void expect_sensor_vector(struct bench *bench, const char *hex)
{
	struct fb3_frame frame, out;
	struct fb3_sensor expected_fields;
	uint8_t expected[FB3_MAX_WIRE];
	size_t expected_len = from_hex(hex, expected, sizeof expected);

	memset(&expected_fields, 0, sizeof expected_fields);
	CHECK(fb3_decode_frame(expected, expected_len, &frame) == FB3_FRAME_OK
	      && fb3_unpack_sensor(frame.payload, frame.length, &expected_fields));
	bench->hw.sample.device_us = expected_fields.source_device_us;
	bench->hw.sample.left_ticks = expected_fields.cumulative_left_ticks;
	bench->hw.sample.right_ticks = expected_fields.cumulative_right_ticks;
	bench->hw.sample.quality = expected_fields.quality;
	bench->hw.sample.steering_mrad = expected_fields.steering_mrad;
	bench->hw.sample.steering_error_mrad = expected_fields.steering_error_mrad;
	bench->hw.now = expected_fields.source_device_us;
	CHECK(spin(bench, &out));
	CHECK(bench->response_len == expected_len && memcmp(bench->response, expected, expected_len) == 0);
}

/* vector 의 SENSOR 는 CLOCK_REPLY 의 표본 다음 순번이고, 그 lease 를 가리킨다 */
static void test_vectors(void)
{
	static const char *const next_sample[] = {VEC_SENSOR_MEASURED, VEC_SENSOR_ESTIMATED, VEC_SENSOR_ENCODER_ONLY};
	struct bench b;

	for (size_t i = 0; i < COUNT(next_sample); i++) {
		vector_session(&b);
		current = "vector: SENSOR";
		expect_sensor_vector(&b, next_sample[i]);
	}
	current = "vector: SENSOR, 32비트 누계의 끝값";
	expect_sensor_vector(&b, VEC_SENSOR_SIGNED32_WRAP);
}

int main(void)
{
	memset(PI_A, 0xa1, sizeof PI_A);
	memset(PI_B, 0xb2, sizeof PI_B);
	memset(BOOT, 0xd0, sizeof BOOT);
	memset(STRANGER, 0x5e, sizeof STRANGER);
	memset(EPOCH, 0xe1, sizeof EPOCH);
	memset(EPOCH_B, 0xe2, sizeof EPOCH_B);
	memset(CONFIG, 0xc1, sizeof CONFIG);
	memset(OTHER_CONFIG, 0xc2, sizeof OTHER_CONFIG);
	memset(FIRMWARE, 0xf1, sizeof FIRMWARE);

	test_cutoff_expires_at_deadline();
	test_cutoff_earliest_first();
	test_cutoff_cancel_and_replace();
	test_cutoff_past_deadline();
	test_cutoff_masked_interrupt();
	test_cutoff_timer_fault();
	test_cutoff_missed_alarm_is_kept();
	test_hello();
	test_probe();
	test_install();
	test_install_rejects();
	test_expiry();
	test_missed_alarm_blocks_leases();
	test_two_leases();
	test_new_session();
	test_unsupported();
	test_sensor_needs_lease();
	test_sensor_period();
	test_sensor_shares_samples_with_reply();
	test_probe_close_to_sample();
	test_sensor_lease_choice();
	test_sensor_sees_fault_while_choosing();
	test_sensor_stops();
	test_sensor_skips_bad_sample();
	test_output_cutoff();
	test_command_stop();
	test_command_drive();
	test_drive_rejects();
	test_drive_faults();
	test_reports_follow_samples();
	test_hold_steer();
	test_samples_keep_one_period_apart();
	test_command_session_and_lease();
	test_vectors();
	printf("feedback-3 link: %d checks, %d failures\n", checks, failures);
	return failures == 0 ? 0 : 1;
}
