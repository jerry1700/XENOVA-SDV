#include "payloads.h"

#include <string.h>

/* 종류마다 필드 순서를 *_fields 한 곳에만 적는다. unpack 은 읽고 pack 은 같은 함수로 쓴다 */
struct cursor {
	uint8_t *at;
	bool write;
};

static void io_uint(struct cursor *c, uint64_t *value, int bytes)
{
	if (c->write) {
		for (int i = 0; i < bytes; i++) {
			c->at[i] = (uint8_t)(*value >> (8 * i));
		}
	} else {
		uint64_t v = 0;
		for (int i = bytes - 1; i >= 0; i--) {
			v = v << 8 | c->at[i];
		}
		*value = v;
	}
	c->at += bytes;
}

/* unpack 의 출력 구조체는 초기화되지 않았을 수 있어 필드 값은 pack 일 때만 읽는다 */
static void io_u8(struct cursor *c, uint8_t *v)
{
	uint64_t x = c->write ? *v : 0;
	io_uint(c, &x, 1);
	*v = (uint8_t)x;
}

static void io_u16(struct cursor *c, uint16_t *v)
{
	uint64_t x = c->write ? *v : 0;
	io_uint(c, &x, 2);
	*v = (uint16_t)x;
}

static void io_u32(struct cursor *c, uint32_t *v)
{
	uint64_t x = c->write ? *v : 0;
	io_uint(c, &x, 4);
	*v = (uint32_t)x;
}

static void io_u64(struct cursor *c, uint64_t *v)
{
	io_uint(c, v, 8);
}

static void io_i16(struct cursor *c, int16_t *v)
{
	uint64_t x = c->write ? (uint16_t)*v : 0;
	io_uint(c, &x, 2);
	*v = (int16_t)(uint16_t)x;
}

static void io_i32(struct cursor *c, int32_t *v)
{
	uint64_t x = c->write ? (uint32_t)*v : 0;
	io_uint(c, &x, 4);
	*v = (int32_t)(uint32_t)x;
}

static void io_i64(struct cursor *c, int64_t *v)
{
	uint64_t x = c->write ? (uint64_t)*v : 0;
	io_uint(c, &x, 8);
	*v = (int64_t)x;
}

static void io_bytes(struct cursor *c, uint8_t *v, size_t n)
{
	if (c->write) {
		memcpy(c->at, v, n);
	} else {
		memcpy(v, c->at, n);
	}
	c->at += n;
}

static void hello_fields(struct cursor *c, struct fb3_hello *v)
{
	io_u8(c, &v->version);
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->vehicle_config_sha256, sizeof v->vehicle_config_sha256);
}

static void hello_ack_fields(struct cursor *c, struct fb3_hello_ack *v)
{
	io_u8(c, &v->version);
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_boot_session, sizeof v->device_boot_session);
	io_u16(c, &v->capabilities);
	io_bytes(c, v->vehicle_config_sha256, sizeof v->vehicle_config_sha256);
	io_bytes(c, v->firmware_sha256, sizeof v->firmware_sha256);
	io_u8(c, &v->encoder_bits);
}

static void clock_probe_fields(struct cursor *c, struct fb3_clock_probe *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->probe_sequence);
	io_u64(c, &v->p1_pi_send_us);
}

static void clock_reply_fields(struct cursor *c, struct fb3_clock_reply *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->probe_sequence);
	io_u64(c, &v->p1_pi_send_us);
	io_u64(c, &v->d2_device_receive_us);
	io_u64(c, &v->latch_device_us);
	io_u64(c, &v->d3_device_send_us);
	io_u64(c, &v->source_sample);
	io_i64(c, &v->cumulative_left_ticks);
	io_i64(c, &v->cumulative_right_ticks);
	io_u8(c, &v->quality);
	io_i32(c, &v->steering_mrad);
	io_u16(c, &v->steering_error_mrad);
}

static void clock_install_fields(struct cursor *c, struct fb3_clock_install *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
	io_u64(c, &v->probe_sequence);
	io_u64(c, &v->reference_pi_us);
	io_i64(c, &v->lower_offset_us);
	io_i64(c, &v->upper_offset_us);
	io_u64(c, &v->valid_until_pi_us);
	io_u64(c, &v->cutoff_device_us);
}

static void clock_ack_fields(struct cursor *c, struct fb3_clock_ack *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
	io_u64(c, &v->probe_sequence);
	io_u8(c, &v->status);
	io_u16(c, &v->reason);
	io_u64(c, &v->device_now_us);
	io_u64(c, &v->cutoff_device_us);
}

static void sensor_fields(struct cursor *c, struct fb3_sensor *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->source_sample);
	io_u64(c, &v->source_device_us);
	io_i64(c, &v->cumulative_left_ticks);
	io_i64(c, &v->cumulative_right_ticks);
	io_u8(c, &v->quality);
	io_i32(c, &v->steering_mrad);
	io_u16(c, &v->steering_error_mrad);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
}

static void command_fields(struct cursor *c, struct fb3_command *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->command_sequence);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
	io_u64(c, &v->issued_pi_us);
	io_u64(c, &v->expires_pi_us);
	io_u64(c, &v->cutoff_device_us);
	io_u32(c, &v->generation);
	io_u8(c, &v->counter);
	io_u8(c, &v->command_kind);
	io_u64(c, &v->stop_command_sequence);
	io_i16(c, &v->velocity_mmps);
	io_i16(c, &v->steering_mrad);
	io_u8(c, &v->reason);
	io_u8(c, &v->reserved);
}

static void report_fields(struct cursor *c, struct fb3_report *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->command_sequence);
	io_u32(c, &v->generation);
	io_u8(c, &v->counter);
	io_u8(c, &v->command_kind);
	io_u8(c, &v->status);
	io_u64(c, &v->report_sequence);
	io_u64(c, &v->source_sample);
	io_u64(c, &v->source_device_us);
	io_u8(c, &v->flags);
	io_i32(c, &v->velocity_mmps);
	io_i32(c, &v->steering_mrad);
	io_u8(c, &v->stationary);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
	io_u64(c, &v->cutoff_device_us);
	io_u64(c, &v->stop_command_sequence);
	io_u16(c, &v->reason);
}

static void tof_sample_fields(struct cursor *c, struct fb3_tof_sample *v)
{
	io_bytes(c, v->pi_session, sizeof v->pi_session);
	io_bytes(c, v->device_session, sizeof v->device_session);
	io_u64(c, &v->source_sample);
	io_u64(c, &v->device_sample_us);
	io_u32(c, &v->sample_time_error_us);
	io_bytes(c, v->clock_epoch, sizeof v->clock_epoch);
	io_u64(c, &v->clock_sequence);
	io_u8(c, &v->sensor_id);
	io_bytes(c, v->sensor_instance, sizeof v->sensor_instance);
	io_u32(c, &v->sensor_generation);
	io_u32(c, &v->sensor_config_revision);
	io_bytes(c, v->sensor_calibration_sha256, sizeof v->sensor_calibration_sha256);
	io_u8(c, &v->rows);
	io_u8(c, &v->cols);
	io_u16(c, &v->frame_status);
	io_u16(c, &v->distance_mm);
	io_u8(c, &v->status);
	io_u8(c, &v->quality);
	io_u32(c, &v->sample_crc32);
}

/* 필드를 다 읽거나 쓴 뒤 움직인 거리가 정해진 크기와 같은지도 본다 */
#define FB3_CODEC(name, type, size) \
	bool fb3_unpack_##name(const uint8_t *payload, size_t len, type *out) \
	{ \
		struct cursor c = {(uint8_t *)payload, false}; \
		if (len != (size)) { \
			return false; \
		} \
		name##_fields(&c, out); \
		return (size_t)(c.at - payload) == (size); \
	} \
	size_t fb3_pack_##name(const type *in, uint8_t *out, size_t cap) \
	{ \
		type copy = *in; \
		struct cursor c = {out, true}; \
		if (cap < (size)) { \
			return 0; \
		} \
		name##_fields(&c, &copy); \
		return (size_t)(c.at - out) == (size) ? (size) : 0; \
	}

FB3_CODEC(hello, struct fb3_hello, FB3_HELLO_SIZE)
FB3_CODEC(hello_ack, struct fb3_hello_ack, FB3_HELLO_ACK_SIZE)
FB3_CODEC(clock_probe, struct fb3_clock_probe, FB3_CLOCK_PROBE_SIZE)
FB3_CODEC(clock_reply, struct fb3_clock_reply, FB3_CLOCK_REPLY_SIZE)
FB3_CODEC(clock_install, struct fb3_clock_install, FB3_CLOCK_INSTALL_SIZE)
FB3_CODEC(clock_ack, struct fb3_clock_ack, FB3_CLOCK_ACK_SIZE)
FB3_CODEC(sensor, struct fb3_sensor, FB3_SENSOR_SIZE)
FB3_CODEC(command, struct fb3_command, FB3_COMMAND_SIZE)
FB3_CODEC(report, struct fb3_report, FB3_REPORT_SIZE)
FB3_CODEC(tof_sample, struct fb3_tof_sample, FB3_TOF_SAMPLE_SIZE)

uint32_t fb3_crc32(const uint8_t *data, size_t len)
{
	uint32_t crc = 0xffffffffu;

	for (size_t i = 0; i < len; i++) {
		crc ^= data[i];
		for (int bit = 0; bit < 8; bit++) {
			crc = crc & 1u ? crc >> 1 ^ 0xedb88320u : crc >> 1;
		}
	}
	return ~crc;
}

void fb3_seal_tof_sample(uint8_t payload[FB3_TOF_SAMPLE_SIZE])
{
	uint32_t crc = fb3_crc32(payload, FB3_TOF_SAMPLE_CRC_SIZE);

	for (int i = 0; i < 4; i++) {
		payload[FB3_TOF_SAMPLE_CRC_SIZE + i] = (uint8_t)(crc >> (8 * i));
	}
}
