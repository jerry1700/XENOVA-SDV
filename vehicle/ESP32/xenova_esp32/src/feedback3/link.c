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
	/* 앞 session 의 명령으로 걸린 출력을 끊고 명령 기록을 버린다 */
	fb3_cutoff_cut(link->cutoff);
	link->command_fence = 0;
	link->generation = 0;
	memset(link->results, 0, sizeof link->results);
	link->result_next = 0;
	link->stop.sequence = 0;
	link->reporting = false;
	link->report_due = false;
	link->direction = 0;
	link->stop_needed = false;
	/* 앞 session 에 보내려고 받아 둔 ToF 관측도 버린다. 근거리 판단은 session 과 상관없이 이어진다 */
	memset(link->tof_has, 0, sizeof link->tof_has);
	memset(link->replies, 0, sizeof link->replies);
	link->reply_next = 0;
	link->has_epoch = false;
	link->clock_fence = 0;
	for (unsigned i = 0; i < FB3_CUTOFF_LEASES; i++) {
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

/* 표본을 내보낼 때 부른다. 앞 표본과 견줘 속도를 구하고, 누계가 바뀌었으면 그때부터 다시 멈춘 시간을 잰다 */
static void note_sample(struct fb3_link *link, const struct fb3_sample *sample)
{
	if (link->sample_sequence == 0) {
		link->still_since_us = sample->device_us;
	} else {
		/* 32비트 누계는 넘치면 돌아간다. 차이도 그 폭으로 구한다 */
		int64_t left = link->config.encoder_bits == 32
			? (int32_t)((uint32_t)sample->left_ticks - (uint32_t)link->sample_left)
			: sample->left_ticks - link->sample_left;
		int64_t right = link->config.encoder_bits == 32
			? (int32_t)((uint32_t)sample->right_ticks - (uint32_t)link->sample_right)
			: sample->right_ticks - link->sample_right;
		int64_t elapsed = (int64_t)(sample->device_us - link->sample_us);
		int64_t velocity = (left + right) * (int64_t)link->config.um_per_tick * 500 / elapsed;

		link->velocity_mmps = velocity > INT32_MAX ? INT32_MAX : velocity < INT32_MIN ? INT32_MIN : (int32_t)velocity;
		link->has_velocity = true;
		/* 표본이 오래 끊겼으면 그사이를 보지 못했으므로 여기서부터 다시 잰다 */
		if (left != 0 || right != 0 || elapsed > (int64_t)FB3_STILL_US) {
			link->still_since_us = sample->device_us;
		}
	}
	link->sample_left = sample->left_ticks;
	link->sample_right = sample->right_ticks;
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

/* 그 센서로 근거리 제동을 하는 설정인지 */
static bool guard_on(const struct fb3_link *link, unsigned sensor)
{
	return (link->config.capabilities & FB3_CAP_PROXIMITY_GUARD) && link->config.tof[sensor].config_revision != 0;
}

/* 그 센서 쪽 방향이 막혀 있는지. 가까운 물체 없음으로 판단한 적이 없거나, 그 판단이 오래됐으면 막혀 있다 */
static bool way_blocked(const struct fb3_link *link, unsigned sensor, uint64_t now)
{
	const struct fb3_guard *guard = &link->guard[sensor];

	return guard_on(link, sensor)
		&& (!guard->clear || now < guard->judged_us || now - guard->judged_us > link->config.proximity_stale_us);
}

/* 표본의 quality 에 더할 막힘 bit */
static uint8_t blocked_bits(const struct fb3_link *link, uint64_t now)
{
	return (uint8_t)((way_blocked(link, 0, now) ? FB3_QUALITY_FRONT_BLOCKED : 0u)
			 | (way_blocked(link, 1, now) ? FB3_QUALITY_REAR_BLOCKED : 0u));
}

/* ToF 관측 하나로 그 방향의 근거리 판단을 고친다. 가까우면 막고 충분히 멀거나 잴 물체가 없으면 푼다.
 * 두 기준 사이의 거리는 앞선 판단을 그대로 두고 판단한 시각만 새로 적는다. 그 밖의 관측은 쓰지 않는다 */
static void judge(struct fb3_link *link, unsigned sensor, const struct fb3_tof_reading *reading, uint64_t now)
{
	struct fb3_guard *guard = &link->guard[sensor];

	/* 지금보다 뒤이거나 앞선 판단보다 새롭지 않은 관측은 쓰지 않는다 */
	if (reading->device_us > now || reading->device_us <= guard->judged_us) {
		return;
	}
	if (reading->status == FB3_TOF_VALID) {
		if (reading->distance_mm == 0 || reading->distance_mm == UINT16_MAX) {
			return;
		}
		if (reading->distance_mm < link->config.proximity_stop_mm) {
			guard->clear = false;
		} else if (reading->distance_mm > link->config.proximity_clear_mm) {
			guard->clear = true;
		}
	} else if (reading->status == FB3_TOF_NO_TARGET || reading->status == FB3_TOF_OUT_OF_RANGE) {
		guard->clear = true;
	} else {
		return;
	}
	guard->judged_us = reading->device_us;
}

/* 센서의 새 관측을 받아 근거리 판단에 쓰고, 보낼 수 있는 때면 보낼 것으로 남겨 둔다.
 * 그 뒤에 막힌 방향으로 걸려 있는 출력이 있으면 끊는다 */
static void watch_tof(struct fb3_link *link)
{
	bool send = link->open && (link->config.capabilities & FB3_CAP_TOF_SINGLE);
	uint64_t now;

	if (!(link->config.capabilities & (FB3_CAP_TOF_SINGLE | FB3_CAP_PROXIMITY_GUARD))) {
		return;
	}
	now = link->port.now_us(link->port.ctx);
	for (unsigned sensor = 0; link->port.tof != NULL && sensor < FB3_TOF_SENSORS; sensor++) {
		struct fb3_tof_reading reading;

		if (link->config.tof[sensor].config_revision == 0 || (!send && !guard_on(link, sensor))
				|| !link->port.tof(link->port.ctx, sensor, &reading)) {
			continue;
		}
		if (guard_on(link, sensor)) {
			judge(link, sensor, &reading, now);
		}
		if (send) {
			/* 앞서 받아 둔 것을 보내기 전에 새 관측이 왔으면 새 것만 남긴다 */
			if (link->tof_has[sensor]) {
				link->tof_dropped[sensor]++;
			}
			link->tof_held[sensor] = reading;
			link->tof_has[sensor] = true;
		}
	}
	if (link->output_direction != 0 && way_blocked(link, link->output_direction > 0 ? 0 : 1, now)) {
		/* 기한이 지난 명령과 같이 다룬다. 이 명령은 더 보고하지 않고, 물체가 치워져도 스스로 다시 걸지 않는다.
		 * 기한이 먼저 지나 이미 끊긴 출력은 건드리지 않는다 */
		if (fb3_cutoff_query(link->cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_LIVE) {
			fb3_cutoff_cut(link->cutoff);
			link->reporting = false;
			link->proximity_cuts++;
		}
		link->output_direction = 0;
	}
}

/* 다음 표본을 뜰 때가 됐는지. 앞 표본에서 한 주기가 지나야 한다 */
static bool gap_passed(const struct fb3_link *link, uint64_t now)
{
	return link->sample_sequence == 0 || now >= link->sample_us + FB3_SENSOR_PERIOD_US;
}

/* 받아 둔 CLOCK_PROBE 에 답한다. 아직 다음 표본을 뜰 때가 아니면 답하지 않고 0 을 돌려준다.
 * 그때는 fb3_link_poll 이 그 자리에서 SENSOR 대신 답한다 */
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
	reply.quality = sample.quality | blocked_bits(link, sample.device_us);
	reply.steering_mrad = sample.steering_mrad;
	reply.steering_error_mrad = sample.steering_error_mrad;
	len = fb3_encode_frame(FB3_CLOCK_REPLY_ID, payload, fb3_pack_clock_reply(&reply, payload, sizeof payload),
			       wire, cap);
	if (len == 0) {
		return 0;
	}

	note_sample(link, &sample);
	link->sample_sequence = reply.source_sample;
	link->sample_us = sample.device_us;
	link->report_due = link->reporting;
	link->sensor_due_us = sample.device_us + FB3_SENSOR_PERIOD_US;
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
		unsigned pick = FB3_CUTOFF_LEASES;

		for (unsigned i = 0; i < FB3_CUTOFF_LEASES; i++) {
			const struct fb3_lease *lease = &link->leases[i];

			if (lease->installed && lease->install.clock_sequence < below
					&& (pick == FB3_CUTOFF_LEASES
					    || lease->install.clock_sequence > link->leases[pick].install.clock_sequence)) {
				pick = i;
			}
		}
		if (pick == FB3_CUTOFF_LEASES) {
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
	sensor.quality = sample.quality | blocked_bits(link, sample.device_us);
	sensor.steering_mrad = sample.steering_mrad;
	sensor.steering_error_mrad = sample.steering_error_mrad;
	memcpy(sensor.clock_epoch, lease->install.clock_epoch, 16);
	sensor.clock_sequence = lease->install.clock_sequence;
	len = fb3_encode_frame(FB3_SENSOR_ID, payload, fb3_pack_sensor(&sensor, payload, sizeof payload), wire, cap);
	if (len == 0) {
		return 0;
	}

	note_sample(link, &sample);
	link->sample_sequence = sensor.source_sample;
	link->sample_us = sample.device_us;
	link->report_due = link->reporting;
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

	for (unsigned i = 0; i < FB3_CUTOFF_LEASES; i++) {
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
	for (slot = 0; slot < FB3_CUTOFF_LEASES; slot++) {
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
	/* 출력에 걸려 있는 DRIVE·HOLD_STEER 가 쓰는 lease 는 밀어내지 않는다 */
	if (lease->installed && link->reporting && link->active.command_kind != FB3_COMMAND_STOP
			&& link->active.clock_sequence == lease->install.clock_sequence
			&& fb3_cutoff_query(link->cutoff, FB3_CUTOFF_OUTPUT) == FB3_CUTOFF_LIVE) {
		return reject(link, &install, FB3_REASON_CLOCK_UNAVAILABLE, wire, cap);
	}
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

/* REPORT 하나를 묶는다. 마지막으로 내보낸 표본을 가리키고, 적용을 알릴 때는 그 표본에서 구한 측정을 싣는다.
 * payload 에는 묶기 전의 REPORT 를 남긴다 */
static size_t send_report(struct fb3_link *link, const struct fb3_command *command, uint8_t status,
			  enum fb3_reason reason, uint8_t payload[FB3_REPORT_SIZE], uint8_t *wire, size_t cap)
{
	struct fb3_report report;
	size_t len;

	memset(payload, 0, FB3_REPORT_SIZE);
	if (link->report_sequence >= FB3_U63_MAX) {
		return 0;
	}
	memcpy(report.pi_session, link->pi_session, 16);
	memcpy(report.device_session, link->config.device_boot_session, 16);
	report.command_sequence = command->command_sequence;
	report.generation = command->generation;
	report.counter = command->counter;
	report.command_kind = command->command_kind;
	report.status = status;
	report.report_sequence = link->report_sequence + 1;
	report.source_sample = link->sample_sequence;
	report.source_device_us = link->sample_sequence != 0 ? link->sample_us : 0;
	report.flags = 0;
	report.velocity_mmps = 0;
	report.steering_mrad = 0; /* 조향 센서가 없다 */
	report.stationary = 0;
	if (status == FB3_REPORT_APPLIED && link->sample_sequence != 0) {
		report.flags |= FB3_REPORT_STATIONARY;
		report.stationary = link->sample_us - link->still_since_us >= FB3_STILL_US;
		if (link->has_velocity) {
			report.flags |= FB3_REPORT_VELOCITY;
			report.velocity_mmps = link->velocity_mmps;
		}
	}
	memcpy(report.clock_epoch, command->clock_epoch, 16);
	report.clock_sequence = command->clock_sequence;
	report.cutoff_device_us = command->cutoff_device_us;
	report.stop_command_sequence = command->stop_command_sequence;
	report.reason = (uint16_t)reason;
	len = fb3_encode_frame(FB3_REPORT_ID, payload, fb3_pack_report(&report, payload, FB3_REPORT_SIZE), wire, cap);
	if (len != 0) {
		link->report_sequence = report.report_sequence;
	}
	return len;
}

/* 이 session 에서 받아들인 명령 가운데 그 순번의 것. 없으면 NULL */
static const struct fb3_result *find_result(const struct fb3_link *link, uint64_t sequence)
{
	if (sequence == 0) {
		return NULL;
	}
	if (link->stop.sequence == sequence) {
		return &link->stop;
	}
	for (unsigned i = 0; i < FB3_LINK_RESULTS; i++) {
		if (link->results[i].sequence == sequence) {
			return &link->results[i];
		}
	}
	return NULL;
}

struct drive_request {
	struct fb3_link *link;
	const struct fb3_command *command;
};

static bool apply_drive(void *arg)
{
	const struct drive_request *request = arg;

	return request->link->port.drive(request->link->port.ctx, request->command->velocity_mmps,
					 request->command->steering_mrad);
}

/* 그 방향으로 가는 DRIVE 를 근거리 제동이 막고 있으면 거절할 이유, 아니면 FB3_REASON_OK.
 * PROXIMITY_BLOCKED 로 거절하는 것은 그 밖에는 걸렸을 명령뿐이다. 이 이유의 거절만 Pi 가 고장으로 보지 않으므로,
 * 타이머 고장과 모자란 기한을 이 이유로 가리지 않는다. 기한은 fb3_cutoff_engage 와 같은 식으로 본다 */
static enum fb3_reason proximity_refusal(struct fb3_link *link, const struct fb3_command *command, int8_t direction)
{
	unsigned sensor = direction > 0 ? 0 : 1;
	uint64_t now;

	if (direction == 0 || !guard_on(link, sensor)) {
		return FB3_REASON_OK;
	}
	now = link->port.now_us(link->port.ctx);
	if (!way_blocked(link, sensor, now)) {
		return FB3_REASON_OK;
	}
	if (fb3_cutoff_faulted(link->cutoff)) {
		return FB3_REASON_DEVICE_FAULT;
	}
	if (command->cutoff_device_us <= now || command->cutoff_device_us - now <= link->config.j_apply_us) {
		return FB3_REASON_COMMAND_EXPIRED;
	}
	return FB3_REASON_PROXIMITY_BLOCKED;
}

/* DRIVE·HOLD_STEER 를 검사하고 출력에 건다. 걸었으면 FB3_REASON_OK, 아니면 받아들이지 않는 이유.
 * HOLD_STEER 는 속도 칸이 0 이어야 하는 것 말고는 DRIVE 와 같은 검사를 거친다 */
static enum fb3_reason accept_drive(struct fb3_link *link, const struct fb3_command *command)
{
	struct drive_request request = {link, command};
	const struct fb3_lease *lease = NULL;
	int8_t direction = command->velocity_mmps > 0 ? 1 : command->velocity_mmps < 0 ? -1 : 0;
	enum fb3_reason blocked;

	if (command->generation < link->generation) {
		return FB3_REASON_GENERATION_REPLAY;
	}
	if (link->port.estop(link->port.ctx)) {
		fb3_cutoff_cut(link->cutoff);
		link->stop_needed = true;
		return FB3_REASON_E_STOP;
	}
	/* 실제로 적용한 마지막 STOP 을 가리켜야 하고, 그 뒤로 가던 방향을 바꾸려면 새 STOP 이 있어야 한다 */
	if (link->stop_needed || link->stop.sequence == 0 || command->stop_command_sequence != link->stop.sequence
			|| (direction != 0 && link->direction != 0 && direction != link->direction)) {
		return FB3_REASON_DIRECTION_STOP_REQUIRED;
	}
	if ((command->command_kind == FB3_COMMAND_HOLD_STEER && command->velocity_mmps != 0)
			|| command->velocity_mmps > link->config.max_velocity_mmps
			|| command->velocity_mmps < -link->config.max_velocity_mmps
			|| command->steering_mrad > link->config.max_steering_mrad
			|| command->steering_mrad < -link->config.max_steering_mrad
			|| !in_range(command->issued_pi_us) || !in_range(command->expires_pi_us)
			|| !in_range(command->cutoff_device_us) || command->expires_pi_us <= command->issued_pi_us
			|| command->expires_pi_us - command->issued_pi_us > FB3_COMMAND_LIFE_US) {
		return FB3_REASON_RANGE;
	}
	if (link->has_epoch && memcmp(command->clock_epoch, link->clock_epoch, 16) == 0) {
		for (unsigned i = 0; i < FB3_CUTOFF_LEASES; i++) {
			if (link->leases[i].installed
					&& link->leases[i].install.clock_sequence == command->clock_sequence) {
				lease = &link->leases[i];
			}
		}
	}
	if (lease == NULL) {
		return FB3_REASON_CLOCK_UNAVAILABLE;
	}
	/* 명령의 기한은 그 lease 의 기한을 넘지 못한다. 그래서 lease 가 끝난 뒤에 출력이 남지 않는다 */
	if (command->expires_pi_us > lease->install.valid_until_pi_us
			|| command->cutoff_device_us > lease->install.cutoff_device_us) {
		return FB3_REASON_DEADLINE_MISMATCH;
	}
	blocked = proximity_refusal(link, command, direction);
	if (blocked != FB3_REASON_OK) {
		return blocked;
	}
	switch (fb3_cutoff_engage(link->cutoff, command->cutoff_device_us, link->config.j_apply_us, apply_drive,
				  &request)) {
	case FB3_CUTOFF_LIVE:
		break;
	case FB3_CUTOFF_EXPIRED:
		return FB3_REASON_COMMAND_EXPIRED;
	default:
		return FB3_REASON_DEVICE_FAULT;
	}
	if (direction != 0) {
		link->direction = direction;
	}
	link->output_direction = direction;
	return FB3_REASON_OK;
}

/* 명령을 검사하고 출력에 적용한다. 적용했으면 FB3_REASON_OK, 아니면 받아들이지 않는 이유 */
static enum fb3_reason accept(struct fb3_link *link, const struct fb3_command *command)
{
	/* 이 session 의 STOP 이면 검사에 앞서 출력부터 끊는다 */
	if (command->command_kind == FB3_COMMAND_STOP) {
		fb3_cutoff_cut(link->cutoff);
	}
	if (!in_range(command->command_sequence) || command->generation == 0 || command->reserved != 0
			|| command->reason > FB3_COMMAND_REASON_MAX) {
		return FB3_REASON_RANGE;
	}
	/* HOLD_STEER 는 정지 조향을 한다고 알린 설정에서만 받는다 */
	if (command->command_kind != FB3_COMMAND_DRIVE && command->command_kind != FB3_COMMAND_STOP
			&& (command->command_kind != FB3_COMMAND_HOLD_STEER
			    || !(link->config.capabilities & FB3_CAP_STATIONARY_STEER))) {
		return FB3_REASON_UNSUPPORTED_PROFILE;
	}
	if (command->command_sequence <= link->command_fence) {
		return FB3_REASON_SEQUENCE_REPLAY;
	}
	if (command->command_kind != FB3_COMMAND_STOP) {
		return accept_drive(link, command);
	}
	/* STOP 은 시각 lease 없이 온다. 기한·속도·조향 칸은 모두 0 이다 */
	if (!is_nil(command->clock_epoch) || command->clock_sequence != 0 || command->issued_pi_us != 0
			|| command->expires_pi_us != 0 || command->cutoff_device_us != 0
			|| command->stop_command_sequence != 0 || command->velocity_mmps != 0
			|| command->steering_mrad != 0) {
		return FB3_REASON_RANGE;
	}
	link->direction = 0;
	link->stop_needed = false;
	return FB3_REASON_OK;
}

static size_t on_command(struct fb3_link *link, const struct fb3_frame *frame, uint8_t *wire, size_t cap)
{
	struct fb3_command command;
	const struct fb3_result *known;
	struct fb3_result *result;
	uint8_t report[FB3_REPORT_SIZE];
	enum fb3_reason reason;
	size_t len;

	if (!fb3_unpack_command(frame->payload, frame->length, &command)) {
		return refuse(link, FB3_REASON_BAD_LENGTH);
	}
	if (!same_session(link, command.pi_session, command.device_session)) {
		return refuse(link, FB3_REASON_SESSION_MISMATCH);
	}
	known = find_result(link, command.command_sequence);
	if (known != NULL) {
		if (memcmp(known->command, frame->payload, FB3_COMMAND_SIZE) != 0) {
			link->refused[FB3_REASON_IDEMPOTENCY_CONFLICT]++;
			return send_report(link, &command, FB3_REPORT_REJECTED, FB3_REASON_IDEMPOTENCY_CONFLICT, report,
					   wire, cap);
		}
		/* 받아들인 명령이 다시 왔다. 처음 보낸 REPORT 만 다시 보내고 출력과 기한은 건드리지 않는다 */
		return fb3_encode_frame(FB3_REPORT_ID, known->report, FB3_REPORT_SIZE, wire, cap);
	}

	reason = accept(link, &command);
	if (reason != FB3_REASON_OK) {
		link->refused[reason]++;
		return send_report(link, &command,
				   reason == FB3_REASON_DEVICE_FAULT ? FB3_REPORT_FAULT : FB3_REPORT_REJECTED, reason,
				   report, wire, cap);
	}

	/* 출력에 적용한 뒤다. 이제부터 이 명령을 표본마다 보고한다 */
	link->command_fence = command.command_sequence;
	if (command.generation > link->generation) {
		link->generation = command.generation;
	}
	link->active = command;
	link->reporting = true;
	link->report_due = false;
	len = send_report(link, &command, FB3_REPORT_APPLIED, FB3_REASON_OK, report, wire, cap);
	result = &link->results[link->result_next];
	link->result_next = (link->result_next + 1) % FB3_LINK_RESULTS;
	result->sequence = command.command_sequence;
	memcpy(result->command, frame->payload, FB3_COMMAND_SIZE);
	memcpy(result->report, report, FB3_REPORT_SIZE);
	if (command.command_kind == FB3_COMMAND_STOP) {
		link->stop = *result;
	}
	return len;
}

/* 방금 내보낸 표본을 가리켜 지금 명령의 상태를 다시 알린다 */
static size_t report_sample(struct fb3_link *link, uint8_t *wire, size_t cap)
{
	uint8_t report[FB3_REPORT_SIZE];

	if (!link->reporting) {
		return 0;
	}
	/* 기한이 지나 출력이 끊긴 DRIVE·HOLD_STEER 는 더 알리지 않는다. 기한이 코앞인 것도 그 REPORT 가 Pi 의
	 * 기한 뒤에 닿을 수 있어 알리지 않는다 */
	if (link->active.command_kind != FB3_COMMAND_STOP
			&& (link->active.cutoff_device_us <= link->sample_us + FB3_REPORT_MARGIN_US
			    || fb3_cutoff_query(link->cutoff, FB3_CUTOFF_OUTPUT) != FB3_CUTOFF_LIVE)) {
		link->reporting = false;
		return 0;
	}
	return send_report(link, &link->active, FB3_REPORT_APPLIED, FB3_REASON_OK, report, wire, cap);
}

/* 센서가 낸 관측 하나를 TOF_SAMPLE 로 묶는다. 보낼 수 없는 관측은 버리고 0 을 돌려준다 */
static size_t tof_frame(struct fb3_link *link, unsigned sensor, const struct fb3_tof_reading *reading,
			uint8_t *wire, size_t cap)
{
	struct fb3_tof_source *source = &link->tof[sensor];
	const struct fb3_lease *lease;
	struct fb3_tof_sample out;
	uint8_t payload[FB3_TOF_SAMPLE_SIZE];
	bool same = memcmp(reading->instance, source->instance, 16) == 0;
	size_t len;

	if (is_nil(reading->instance) || reading->generation == 0 || !in_range(reading->device_us)
			|| reading->time_error_us == 0 || reading->status > FB3_TOF_BAD_RETURN
			|| (reading->status == FB3_TOF_VALID
			    && (reading->distance_mm == 0 || reading->distance_mm == UINT16_MAX))) {
		return 0;
	}
	/* 센서를 다시 초기화했으면 새 instance 와 더 큰 generation 이어야 한다. 한쪽만 바뀐 것은 받지 않는다 */
	if (source->generation != 0
			&& (same ? reading->generation != source->generation : reading->generation <= source->generation)) {
		return 0;
	}
	if (reading->device_us <= source->device_us || (same && source->sample >= FB3_U63_MAX)) {
		return 0;
	}
	if (!same) {
		memcpy(source->instance, reading->instance, 16);
		source->generation = reading->generation;
		source->sample = 0;
	}
	source->device_us = reading->device_us;
	/* 관측 시각을 Pi 시각으로 옮길 lease 가 있어야 한다. 없으면 이 관측은 보내지 않는다 */
	lease = newest_live_lease(link);
	if (lease == NULL || reading->device_us >= lease->install.cutoff_device_us) {
		return 0;
	}

	memcpy(out.pi_session, link->pi_session, 16);
	memcpy(out.device_session, link->config.device_boot_session, 16);
	out.source_sample = source->sample + 1;
	out.device_sample_us = reading->device_us;
	out.sample_time_error_us = reading->time_error_us;
	memcpy(out.clock_epoch, lease->install.clock_epoch, 16);
	out.clock_sequence = lease->install.clock_sequence;
	out.sensor_id = (uint8_t)sensor;
	memcpy(out.sensor_instance, source->instance, 16);
	out.sensor_generation = source->generation;
	out.sensor_config_revision = link->config.tof[sensor].config_revision;
	memcpy(out.sensor_calibration_sha256, link->config.tof[sensor].calibration_sha256, 32);
	out.rows = 1;
	out.cols = 1;
	out.frame_status = FB3_TOF_FRAME_TIMING_VALID;
	if (reading->status != FB3_TOF_SENSOR_ERROR && reading->status != FB3_TOF_UNKNOWN) {
		out.frame_status |= FB3_TOF_FRAME_SENSOR_OK;
	}
	/* 유효한 거리가 아니면 거리 칸은 0 이고 TARGET_VALID 를 켜지 않는다 */
	out.distance_mm = reading->status == FB3_TOF_VALID ? reading->distance_mm : 0;
	out.status = reading->status;
	out.quality = FB3_TOF_QUALITY_NATIVE | (reading->status == FB3_TOF_VALID ? FB3_TOF_QUALITY_TARGET_VALID : 0u);
	out.sample_crc32 = 0;
	if (fb3_pack_tof_sample(&out, payload, sizeof payload) != sizeof payload) {
		return 0;
	}
	fb3_seal_tof_sample(payload);
	len = fb3_encode_frame(FB3_TOF_SAMPLE_ID, payload, sizeof payload, wire, cap);
	if (len != 0) {
		source->sample = out.source_sample;
	}
	return len;
}

/* 받아 둔 관측이 있는 ToF 센서 하나의 것을 보낸다. 센서를 번갈아 먼저 본다 */
static size_t send_tof(struct fb3_link *link, uint8_t *wire, size_t cap)
{
	for (unsigned turn = 0; turn < FB3_TOF_SENSORS; turn++) {
		unsigned sensor = (link->tof_next + turn) % FB3_TOF_SENSORS;
		size_t len;

		if (!link->tof_has[sensor]) {
			continue;
		}
		link->tof_has[sensor] = false;
		link->tof_next = (sensor + 1) % FB3_TOF_SENSORS;
		len = tof_frame(link, sensor, &link->tof_held[sensor], wire, cap);
		if (len == 0) {
			link->tof_dropped[sensor]++;
		}
		return len;
	}
	return 0;
}

/* 어느 명령의 것도 아닌 장치 고장을 알린다 (계약 §7 의 순번 0·종류 0·FAULT) */
static size_t report_fault(struct fb3_link *link, enum fb3_reason reason, uint8_t *wire, size_t cap)
{
	struct fb3_command none;
	uint8_t report[FB3_REPORT_SIZE];

	memset(&none, 0, sizeof none);
	none.generation = link->generation;
	return send_report(link, &none, FB3_REPORT_FAULT, reason, report, wire, cap);
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
	case FB3_COMMAND_ID:
		return on_command(link, frame, wire, cap);
	default:
		return refuse(link, FB3_REASON_UNSUPPORTED_PROFILE);
	}
}

size_t fb3_link_poll(struct fb3_link *link, uint8_t *wire, size_t cap)
{
	uint64_t now;

	/* 보낼 것을 고르기 전에 근거리 판단부터 고친다. 막힌 방향의 출력은 다음 표본을 기다리지 않고 끊는다 */
	watch_tof(link);
	/* 방금 보낸 표본에 대한 REPORT 가 그 표본 바로 뒤에 간다 */
	if (link->report_due) {
		link->report_due = false;
		return report_sample(link, wire, cap);
	}
	/* 다음 표본을 뜰 때가 아니면 그사이에 받아 둔 ToF 관측을 보낸다 */
	now = link->port.now_us(link->port.ctx);
	if (now < link->sensor_due_us) {
		return send_tof(link, wire, cap);
	}
	/* 비상정지가 눌리면 출력을 끊고, 새 STOP 을 받을 때까지 DRIVE 를 받지 않는다 */
	if (!link->stop_needed && link->port.estop(link->port.ctx)) {
		fb3_cutoff_cut(link->cutoff);
		link->stop_needed = true;
		if (link->open) {
			return report_fault(link, FB3_REASON_E_STOP, wire, cap);
		}
	}
	if (link->open && !link->fault_reported && fb3_cutoff_faulted(link->cutoff)) {
		link->fault_reported = true;
		link->reporting = false;
		return report_fault(link, FB3_REASON_DEVICE_FAULT, wire, cap);
	}
	/* 답을 미뤄 둔 CLOCK_PROBE 가 있으면 이 자리의 표본은 그 응답에 싣는다 */
	if (link->probe_held) {
		return answer_probe(link, wire, cap);
	}
	return send_sensor(link, now, wire, cap);
}

bool fb3_link_lease_live(struct fb3_link *link, const uint8_t clock_epoch[16], uint64_t clock_sequence)
{
	if (!link->has_epoch || memcmp(clock_epoch, link->clock_epoch, 16) != 0) {
		return false;
	}
	for (unsigned i = 0; i < FB3_CUTOFF_LEASES; i++) {
		if (link->leases[i].installed && link->leases[i].install.clock_sequence == clock_sequence) {
			return fb3_cutoff_query(link->cutoff, i) == FB3_CUTOFF_LIVE;
		}
	}
	return false;
}
