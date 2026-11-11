#include "link.h"

#include <string.h>

/* 응답 없이 버린다. session 을 확인하지 못한 프레임에는 ACK 를 만들지 않고 수만 센다 */
static size_t refuse(struct fb3_link *link, enum fb3_reason reason)
{
	link->refused[reason]++;
	return 0;
}

static bool in_range(uint64_t value)
{
	return value >= 1 && value <= FB3_U63_MAX;
}

static bool is_nil(const uint8_t id[16])
{
	for (int i = 0; i < 16; i++) {
		if (id[i] != 0) {
			return false;
		}
	}
	return true;
}

static bool same_session(const struct fb3_link *link, const uint8_t *pi, const uint8_t *device)
{
	return link->open && memcmp(pi, link->pi_session, 16) == 0
		&& memcmp(device, link->config.device_boot_session, 16) == 0;
}

/* Pi session 이 끝나면 그 session 의 probe 기록과 lease 를 모두 버린다 */
static void close_session(struct fb3_link *link)
{
	link->open = false;
	link->probe_fence = 0;
	link->probe_held = false;
	memset(link->replies, 0, sizeof link->replies);
	link->reply_next = 0;
	link->has_epoch = false;
	link->clock_fence = 0;
	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		if (link->leases[i].installed) {
			fb3_cutoff_cancel(link->cutoff, i);
			link->leases[i].installed = false;
		}
	}
}

static size_t on_hello(struct fb3_link *link, const struct fb3_frame *frame, uint8_t *wire, size_t cap)
{
	struct fb3_hello hello;
	struct fb3_hello_ack ack;
	uint8_t payload[FB3_HELLO_ACK_SIZE];

	if (!fb3_unpack_hello(frame->payload, frame->length, &hello)) {
		return refuse(link, FB3_REASON_BAD_LENGTH);
	}
	if (hello.version != FB3_VERSION) {
		return refuse(link, FB3_REASON_UNSUPPORTED_PROFILE);
	}
	if (memcmp(hello.vehicle_config_sha256, link->config.vehicle_config_sha256, 32) != 0) {
		/* 등록 config 가 다른 Pi 와는 session 을 맺지 않는다. 이쪽 digest 는 그대로 알려 준다 */
		close_session(link);
		link->refused[FB3_REASON_CONFIG_MISMATCH]++;
	} else if (!link->open || memcmp(hello.pi_session, link->pi_session, 16) != 0) {
		close_session(link);
		memcpy(link->pi_session, hello.pi_session, 16);
		link->open = true;
	}

	ack.version = FB3_VERSION;
	memcpy(ack.pi_session, hello.pi_session, 16);
	memcpy(ack.device_boot_session, link->config.device_boot_session, 16);
	ack.capabilities = link->config.capabilities;
	memcpy(ack.vehicle_config_sha256, link->config.vehicle_config_sha256, 32);
	memcpy(ack.firmware_sha256, link->config.firmware_sha256, 32);
	ack.encoder_bits = link->config.encoder_bits;
	return fb3_encode_frame(FB3_HELLO_ACK_ID, payload, fb3_pack_hello_ack(&ack, payload, sizeof payload),
				wire, cap);
}

/* 계약 §3 CLOCK_REPLY 의 quality·조향·누계 범위 */
static bool sample_valid(const struct fb3_link *link, const struct fb3_sample *sample)
{
	uint8_t steering = sample->quality & (FB3_QUALITY_STEERING_MEASURED | FB3_QUALITY_STEERING_ESTIMATED);

	if (!(sample->quality & FB3_QUALITY_ENCODER) || (sample->quality & ~0x07u)
			|| steering == (FB3_QUALITY_STEERING_MEASURED | FB3_QUALITY_STEERING_ESTIMATED)) {
		return false;
	}
	if (steering) {
		if (sample->steering_error_mrad == 0 || sample->steering_mrad > FB3_STEERING_LIMIT_MRAD
				|| sample->steering_mrad < -FB3_STEERING_LIMIT_MRAD) {
			return false;
		}
	} else if (sample->steering_mrad != 0 || sample->steering_error_mrad != 0) {
		return false;
	}
	if (link->config.encoder_bits == 32) {
		return sample->left_ticks >= INT32_MIN && sample->left_ticks <= INT32_MAX
			&& sample->right_ticks >= INT32_MIN && sample->right_ticks <= INT32_MAX;
	}
	return true;
}

/* 앞 표본에서 FB3_SAMPLE_GAP_US 가 지났는지 */
static bool gap_passed(const struct fb3_link *link, uint64_t now)
{
	return link->sample_sequence == 0 || now >= link->sample_us + FB3_SAMPLE_GAP_US;
}

/* 받아 둔 CLOCK_PROBE 에 답한다. 앞 표본과 아직 가까우면 답하지 않고 0 을 돌려준다.
 * 그때는 fb3_link_poll 이 간격이 찬 뒤에 답한다 */
static size_t answer_probe(struct fb3_link *link, uint8_t *wire, size_t cap)
{
	const struct fb3_clock_probe *probe = &link->probe;
	struct fb3_clock_reply reply;
	struct fb3_sample sample;
	uint8_t payload[FB3_CLOCK_REPLY_SIZE];
	uint64_t d2 = link->probe_received_us;
	uint64_t d3;
	size_t len;

	if (!gap_passed(link, link->port.now_us(link->port.ctx))) {
		return 0;
	}
	link->probe_held = false;
	if (link->sample_sequence >= FB3_U63_MAX) {
		return refuse(link, FB3_REASON_COUNTER_EXHAUSTED);
	}
	/* 받은 뒤에 새로 뜬 표본만 싣는다. 미리 떠 둔 표본을 쓰지 않는다 */
	if (!link->port.latch(link->port.ctx, &sample) || !sample_valid(link, &sample)) {
		return refuse(link, FB3_REASON_SENSOR_UNAVAILABLE);
	}
	d3 = link->port.now_us(link->port.ctx);
	if (d2 == 0 || sample.device_us < d2 || d3 < sample.device_us || d3 > FB3_U63_MAX
			|| sample.device_us <= link->sample_us) {
		return refuse(link, FB3_REASON_TIMESTAMP_ORDER);
	}

	memcpy(reply.pi_session, probe->pi_session, 16);
	memcpy(reply.device_session, probe->device_session, 16);
	reply.probe_sequence = probe->probe_sequence;
	reply.p1_pi_send_us = probe->p1_pi_send_us;
	reply.d2_device_receive_us = d2;
	reply.latch_device_us = sample.device_us;
	reply.d3_device_send_us = d3;
	reply.source_sample = link->sample_sequence + 1;
	reply.cumulative_left_ticks = sample.left_ticks;
	reply.cumulative_right_ticks = sample.right_ticks;
	reply.quality = sample.quality;
	reply.steering_mrad = sample.steering_mrad;
	reply.steering_error_mrad = sample.steering_error_mrad;
	len = fb3_encode_frame(FB3_CLOCK_REPLY_ID, payload, fb3_pack_clock_reply(&reply, payload, sizeof payload),
			       wire, cap);
	if (len == 0) {
		return 0;
	}

	link->sample_sequence = reply.source_sample;
	link->sample_us = sample.device_us;
	/* 다음 probe 는 200 ms 뒤에 온다. 그 앞뒤로 SENSOR 가 반 주기씩 떨어지게 한다 */
	link->sensor_due_us = sample.device_us + FB3_SENSOR_PERIOD_US / 2;
	link->probe_fence = probe->probe_sequence;
	link->replies[link->reply_next].probe_sequence = probe->probe_sequence;
	link->replies[link->reply_next].d3_us = d3;
	link->reply_next = (link->reply_next + 1) % FB3_LINK_REPLIES;
	return len;
}

static size_t on_clock_probe(struct fb3_link *link, const struct fb3_frame *frame, uint64_t received_us,
			     uint8_t *wire, size_t cap)
{
	struct fb3_clock_probe probe;

	if (!fb3_unpack_clock_probe(frame->payload, frame->length, &probe)) {
		return refuse(link, FB3_REASON_BAD_LENGTH);
	}
	if (!same_session(link, probe.pi_session, probe.device_session)) {
		return refuse(link, FB3_REASON_SESSION_MISMATCH);
	}
	if (!in_range(probe.probe_sequence) || !in_range(probe.p1_pi_send_us)) {
		return refuse(link, FB3_REASON_RANGE);
	}
	if (probe.probe_sequence <= link->probe_fence
			|| (link->probe_held && probe.probe_sequence <= link->probe.probe_sequence)) {
		return refuse(link, FB3_REASON_SEQUENCE_REPLAY);
	}
	/* 답을 미뤄 둔 probe 가 있었다면 새 것이 그 자리를 차지한다. Pi 는 한 번에 하나만 기다린다 */
	link->probe_held = true;
	link->probe = probe;
	link->probe_received_us = received_us;
	return answer_probe(link, wire, cap);
}

/* 설치돼 있고 아직 기한 전인 lease 가운데 clock_sequence 가 가장 큰 것. 없으면 NULL.
 * 순번이 큰 것부터 조회하고, 마지막으로 조회한 것만 돌려준다. 조회는 걸려 있는 기한을 모두 검사하므로,
 * 뒤의 조회에서 타이머 고장이 드러나면 앞서 받은 답은 더는 맞지 않는다 */
static const struct fb3_lease *newest_live_lease(struct fb3_link *link)
{
	uint64_t below = UINT64_MAX;

	for (;;) {
		unsigned pick = FB3_CUTOFF_SLOTS;

		for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
			const struct fb3_lease *lease = &link->leases[i];

			if (lease->installed && lease->install.clock_sequence < below
					&& (pick == FB3_CUTOFF_SLOTS
					    || lease->install.clock_sequence > link->leases[pick].install.clock_sequence)) {
				pick = i;
			}
		}
		if (pick == FB3_CUTOFF_SLOTS) {
			return NULL;
		}
		if (fb3_cutoff_query(link->cutoff, pick) == FB3_CUTOFF_LIVE) {
			return &link->leases[pick];
		}
		below = link->leases[pick].install.clock_sequence;
	}
}

/* 지금 표본을 떠서 SENSOR 로 묶는다. 살아 있는 lease 가 없거나 표본을 쓸 수 없으면 한 주기 뒤에 다시 본다 */
static size_t send_sensor(struct fb3_link *link, uint64_t now, uint8_t *wire, size_t cap)
{
	const struct fb3_lease *lease;
	struct fb3_sensor sensor;
	struct fb3_sample sample;
	uint8_t payload[FB3_SENSOR_SIZE];
	size_t len;

	link->sensor_due_us = now + FB3_SENSOR_PERIOD_US;
	if (link->sample_sequence >= FB3_U63_MAX || !link->port.latch(link->port.ctx, &sample)
			|| !sample_valid(link, &sample) || sample.device_us <= link->sample_us) {
		return 0;
	}
	/* 표본을 뜬 뒤에 lease 를 고른다. 그래야 표본을 뜨는 사이에 기한이 지났거나 타이머 고장이 드러난 것을 놓치지 않는다 */
	lease = newest_live_lease(link);
	if (lease == NULL || sample.device_us >= lease->install.cutoff_device_us) {
		return 0;
	}

	memcpy(sensor.pi_session, link->pi_session, 16);
	memcpy(sensor.device_session, link->config.device_boot_session, 16);
	sensor.source_sample = link->sample_sequence + 1;
	sensor.source_device_us = sample.device_us;
	sensor.cumulative_left_ticks = sample.left_ticks;
	sensor.cumulative_right_ticks = sample.right_ticks;
	sensor.quality = sample.quality;
	sensor.steering_mrad = sample.steering_mrad;
	sensor.steering_error_mrad = sample.steering_error_mrad;
	memcpy(sensor.clock_epoch, lease->install.clock_epoch, 16);
	sensor.clock_sequence = lease->install.clock_sequence;
	len = fb3_encode_frame(FB3_SENSOR_ID, payload, fb3_pack_sensor(&sensor, payload, sizeof payload), wire, cap);
	if (len == 0) {
		return 0;
	}

	link->sample_sequence = sensor.source_sample;
	link->sample_us = sample.device_us;
	link->sensor_due_us = sample.device_us + FB3_SENSOR_PERIOD_US;
	return len;
}

static size_t clock_ack(struct fb3_link *link, const struct fb3_clock_install *install, uint8_t status,
			enum fb3_reason reason, uint64_t device_us, uint8_t *wire, size_t cap)
{
	struct fb3_clock_ack ack;
	uint8_t payload[FB3_CLOCK_ACK_SIZE];

	if (reason != FB3_REASON_OK) {
		link->refused[reason]++;
	}
	memcpy(ack.pi_session, install->pi_session, 16);
	memcpy(ack.device_session, install->device_session, 16);
	memcpy(ack.clock_epoch, install->clock_epoch, 16);
	ack.clock_sequence = install->clock_sequence;
	ack.probe_sequence = install->probe_sequence;
	ack.status = status;
	ack.reason = (uint16_t)reason;
	ack.device_now_us = device_us;
	ack.cutoff_device_us = install->cutoff_device_us;
	return fb3_encode_frame(FB3_CLOCK_ACK_ID, payload, fb3_pack_clock_ack(&ack, payload, sizeof payload),
				wire, cap);
}

static size_t reject(struct fb3_link *link, const struct fb3_clock_install *install, enum fb3_reason reason,
		     uint8_t *wire, size_t cap)
{
	return clock_ack(link, install, FB3_CLOCK_REJECTED, reason, link->port.now_us(link->port.ctx), wire, cap);
}

static const struct fb3_reply_mark *find_reply(const struct fb3_link *link, uint64_t probe_sequence)
{
	for (unsigned i = 0; i < FB3_LINK_REPLIES; i++) {
		if (link->replies[i].probe_sequence == probe_sequence) {
			return &link->replies[i];
		}
	}
	return NULL;
}

/* 빈 칸이 없으면 clock_sequence 가 가장 작은 lease 의 칸을 내준다 */
static unsigned pick_slot(const struct fb3_link *link)
{
	unsigned oldest = 0;

	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		if (!link->leases[i].installed) {
			return i;
		}
		if (link->leases[i].install.clock_sequence < link->leases[oldest].install.clock_sequence) {
			oldest = i;
		}
	}
	return oldest;
}

static size_t on_clock_install(struct fb3_link *link, const struct fb3_frame *frame, uint8_t *wire, size_t cap)
{
	struct fb3_clock_install install;
	const struct fb3_reply_mark *reply;
	struct fb3_lease *lease;
	enum fb3_cutoff_state armed;
	unsigned slot;
	uint64_t now;

	if (!fb3_unpack_clock_install(frame->payload, frame->length, &install)) {
		return refuse(link, FB3_REASON_BAD_LENGTH);
	}
	if (!same_session(link, install.pi_session, install.device_session)) {
		return refuse(link, FB3_REASON_SESSION_MISMATCH);
	}
	if (is_nil(install.clock_epoch) || !in_range(install.clock_sequence) || !in_range(install.probe_sequence)
			|| !in_range(install.reference_pi_us) || !in_range(install.valid_until_pi_us)
			|| !in_range(install.cutoff_device_us) || install.lower_offset_us > install.upper_offset_us) {
		return reject(link, &install, FB3_REASON_RANGE, wire, cap);
	}
	/* 한 Pi session 의 clock epoch 는 하나다 */
	if (link->has_epoch && memcmp(install.clock_epoch, link->clock_epoch, 16) != 0) {
		return reject(link, &install, FB3_REASON_SESSION_MISMATCH, wire, cap);
	}

	/* 이미 설치한 lease 의 재전송에는 처음 보낸 ACK 를 그대로 다시 보낸다. 타이머는 건드리지 않는다 */
	for (slot = 0; slot < FB3_CUTOFF_SLOTS; slot++) {
		lease = &link->leases[slot];
		if (!lease->installed || lease->install.clock_sequence != install.clock_sequence) {
			continue;
		}
		if (memcmp(lease->payload, frame->payload, FB3_CLOCK_INSTALL_SIZE) != 0) {
			return reject(link, &install, FB3_REASON_IDEMPOTENCY_CONFLICT, wire, cap);
		}
		switch (fb3_cutoff_query(link->cutoff, slot)) {
		case FB3_CUTOFF_LIVE:
			return clock_ack(link, &install, FB3_CLOCK_INSTALLED, FB3_REASON_OK, lease->ack_device_us,
					 wire, cap);
		case FB3_CUTOFF_EXPIRED:
			return reject(link, &install, FB3_REASON_CLOCK_EXPIRED, wire, cap);
		default:
			return reject(link, &install, FB3_REASON_DEVICE_FAULT, wire, cap);
		}
	}

	if (install.clock_sequence <= link->clock_fence) {
		return reject(link, &install, FB3_REASON_SEQUENCE_REPLAY, wire, cap);
	}
	/* 이 장치가 실제로 보낸 CLOCK_REPLY 를 가리켜야 한다 */
	reply = find_reply(link, install.probe_sequence);
	if (reply == NULL) {
		return reject(link, &install, FB3_REASON_CLOCK_UNAVAILABLE, wire, cap);
	}
	if (install.cutoff_device_us <= reply->d3_us) {
		return reject(link, &install, FB3_REASON_TIMESTAMP_ORDER, wire, cap);
	}

	/* 기한을 실제 타이머에 건 뒤에만 INSTALLED 로 답한다 */
	slot = pick_slot(link);
	lease = &link->leases[slot];
	armed = fb3_cutoff_arm(link->cutoff, slot, install.cutoff_device_us);
	now = link->port.now_us(link->port.ctx);
	if (armed == FB3_CUTOFF_LIVE && now >= install.cutoff_device_us) {
		fb3_cutoff_cancel(link->cutoff, slot);
		armed = FB3_CUTOFF_EXPIRED;
	}
	if (armed != FB3_CUTOFF_LIVE) {
		/* 걸다가 그 칸의 이전 기한이 지워졌으면 이전 lease 도 더는 없다 */
		if (fb3_cutoff_query(link->cutoff, slot) == FB3_CUTOFF_IDLE) {
			lease->installed = false;
		}
		return reject(link, &install,
			      armed == FB3_CUTOFF_EXPIRED ? FB3_REASON_CLOCK_EXPIRED : FB3_REASON_DEVICE_FAULT,
			      wire, cap);
	}

	lease->installed = true;
	lease->install = install;
	memcpy(lease->payload, frame->payload, FB3_CLOCK_INSTALL_SIZE);
	lease->ack_device_us = now;
	link->clock_fence = install.clock_sequence;
	memcpy(link->clock_epoch, install.clock_epoch, 16);
	link->has_epoch = true;
	return clock_ack(link, &install, FB3_CLOCK_INSTALLED, FB3_REASON_OK, now, wire, cap);
}

void fb3_link_init(struct fb3_link *link, const struct fb3_link_config *config,
		   const struct fb3_link_port *port, struct fb3_cutoff *cutoff)
{
	memset(link, 0, sizeof *link);
	link->config = *config;
	link->port = *port;
	link->cutoff = cutoff;
}

size_t fb3_link_receive(struct fb3_link *link, const struct fb3_frame *frame, uint64_t received_us,
			uint8_t *wire, size_t cap)
{
	switch (frame->id) {
	case FB3_HELLO_ID:
		return on_hello(link, frame, wire, cap);
	case FB3_CLOCK_PROBE_ID:
		return on_clock_probe(link, frame, received_us, wire, cap);
	case FB3_CLOCK_INSTALL_ID:
		return on_clock_install(link, frame, wire, cap);
	default:
		return refuse(link, FB3_REASON_UNSUPPORTED_PROFILE);
	}
}

size_t fb3_link_poll(struct fb3_link *link, uint8_t *wire, size_t cap)
{
	uint64_t now;

	/* 답을 미뤄 둔 CLOCK_PROBE 가 있으면 간격이 찼는지 본다. 그 응답의 표본이 다음 표본이다 */
	if (link->probe_held) {
		return answer_probe(link, wire, cap);
	}
	now = link->port.now_us(link->port.ctx);
	if (now < link->sensor_due_us) {
		return 0;
	}
	return send_sensor(link, now, wire, cap);
}

bool fb3_link_lease_live(struct fb3_link *link, const uint8_t clock_epoch[16], uint64_t clock_sequence)
{
	if (!link->has_epoch || memcmp(clock_epoch, link->clock_epoch, 16) != 0) {
		return false;
	}
	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		if (link->leases[i].installed && link->leases[i].install.clock_sequence == clock_sequence) {
			return fb3_cutoff_query(link->cutoff, i) == FB3_CUTOFF_LIVE;
		}
	}
	return false;
}
