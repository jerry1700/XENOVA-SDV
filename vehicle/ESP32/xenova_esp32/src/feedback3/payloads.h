/* feedback-3 payload 9종과 단일 존 ToF 관측의 고정 배치. little endian, padding 없음.
 * 계약 docs/contracts/gateway-feedback-3.md §3 의 표와 Pi struct 형식 문자열을 따른다.
 * 크기와 배치만 다루고 session·기한 같은 의미 검사는 하지 않는다. */
#ifndef XENOVA_FB3_PAYLOADS_H
#define XENOVA_FB3_PAYLOADS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* HELLO, Pi → MCU, <B16s32s */
#define FB3_HELLO_ID 0x0f00u
#define FB3_HELLO_SIZE 49u
struct fb3_hello {
	uint8_t version;
	uint8_t pi_session[16];
	uint8_t vehicle_config_sha256[32];
};

/* HELLO_ACK, MCU → Pi, <B16s16sH32s32sB */
#define FB3_HELLO_ACK_ID 0x0f01u
#define FB3_HELLO_ACK_SIZE 100u
struct fb3_hello_ack {
	uint8_t version;
	uint8_t pi_session[16];
	uint8_t device_boot_session[16];
	uint16_t capabilities;
	uint8_t vehicle_config_sha256[32];
	uint8_t firmware_sha256[32];
	uint8_t encoder_bits;
};

/* CLOCK_PROBE, Pi → MCU, <16s16sQQ */
#define FB3_CLOCK_PROBE_ID 0x0f10u
#define FB3_CLOCK_PROBE_SIZE 48u
struct fb3_clock_probe {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t probe_sequence;
	uint64_t p1_pi_send_us;
};

/* CLOCK_REPLY, MCU → Pi, <16s16sQQQQQQqqBiH */
#define FB3_CLOCK_REPLY_ID 0x0f11u
#define FB3_CLOCK_REPLY_SIZE 103u
struct fb3_clock_reply {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t probe_sequence;
	uint64_t p1_pi_send_us;
	uint64_t d2_device_receive_us;
	uint64_t latch_device_us;
	uint64_t d3_device_send_us;
	uint64_t source_sample;
	int64_t cumulative_left_ticks;
	int64_t cumulative_right_ticks;
	uint8_t quality;
	int32_t steering_mrad;
	uint16_t steering_error_mrad;
};

/* CLOCK_INSTALL, Pi → MCU, <16s16s16sQQQqqQQ */
#define FB3_CLOCK_INSTALL_ID 0x0f12u
#define FB3_CLOCK_INSTALL_SIZE 104u
struct fb3_clock_install {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
	uint64_t probe_sequence;
	uint64_t reference_pi_us;
	int64_t lower_offset_us;
	int64_t upper_offset_us;
	uint64_t valid_until_pi_us;
	uint64_t cutoff_device_us;
};

/* CLOCK_ACK, MCU → Pi, <16s16s16sQQBHQQ */
#define FB3_CLOCK_ACK_ID 0x0f13u
#define FB3_CLOCK_ACK_SIZE 83u
struct fb3_clock_ack {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
	uint64_t probe_sequence;
	uint8_t status;
	uint16_t reason;
	uint64_t device_now_us;
	uint64_t cutoff_device_us;
};

/* SENSOR, MCU → Pi, <16s16sQQqqBiH16sQ */
#define FB3_SENSOR_ID 0x0f15u
#define FB3_SENSOR_SIZE 95u
struct fb3_sensor {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t source_sample;
	uint64_t source_device_us;
	int64_t cumulative_left_ticks;
	int64_t cumulative_right_ticks;
	uint8_t quality;
	int32_t steering_mrad;
	uint16_t steering_error_mrad;
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
};

/* COMMAND, Pi → MCU, <16s16sQ16sQQQQIBBQhhBB */
#define FB3_COMMAND_ID 0x0111u
#define FB3_COMMAND_SIZE 108u
struct fb3_command {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t command_sequence;
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
	uint64_t issued_pi_us;
	uint64_t expires_pi_us;
	uint64_t cutoff_device_us;
	uint32_t generation;
	uint8_t counter;
	uint8_t command_kind;
	uint64_t stop_command_sequence;
	int16_t velocity_mmps;
	int16_t steering_mrad;
	uint8_t reason;
	uint8_t reserved;
};

/* REPORT, MCU → Pi, <16s16sQIBBBQQQBiiB16sQQQH */
#define FB3_REPORT_ID 0x0f14u
#define FB3_REPORT_SIZE 123u
struct fb3_report {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t command_sequence;
	uint32_t generation;
	uint8_t counter;
	uint8_t command_kind;
	uint8_t status;
	uint64_t report_sequence;
	uint64_t source_sample;
	uint64_t source_device_us;
	uint8_t flags;
	int32_t velocity_mmps;
	int32_t steering_mrad;
	uint8_t stationary;
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
	uint64_t cutoff_device_us;
	uint64_t stop_command_sequence;
	uint16_t reason;
};

/* TOF_SAMPLE, MCU → Pi, <16s16sQQI16sQB16sII32sBBHHBBI. 단일 존 관측 하나가 프레임 하나다 (계약 §3·§9.1) */
#define FB3_TOF_SAMPLE_ID 0x0f17u
#define FB3_TOF_SAMPLE_SIZE 145u
/* sample_crc32 가 덮는 앞부분. sample_crc32 자리 바로 앞까지다 */
#define FB3_TOF_SAMPLE_CRC_SIZE 141u
struct fb3_tof_sample {
	uint8_t pi_session[16];
	uint8_t device_session[16];
	uint64_t source_sample;
	uint64_t device_sample_us;
	uint32_t sample_time_error_us;
	uint8_t clock_epoch[16];
	uint64_t clock_sequence;
	uint8_t sensor_id;
	uint8_t sensor_instance[16];
	uint32_t sensor_generation;
	uint32_t sensor_config_revision;
	uint8_t sensor_calibration_sha256[32];
	uint8_t rows;
	uint8_t cols;
	uint16_t frame_status;
	uint16_t distance_mm;
	uint8_t status;
	uint8_t quality;
	uint32_t sample_crc32;
};

/* unpack: len 이 그 종류의 크기와 다르면 false.
 * pack: 쓴 바이트 수(그 종류의 크기). cap 이 모자라면 0 */
bool fb3_unpack_hello(const uint8_t *payload, size_t len, struct fb3_hello *out);
size_t fb3_pack_hello(const struct fb3_hello *in, uint8_t *out, size_t cap);
bool fb3_unpack_hello_ack(const uint8_t *payload, size_t len, struct fb3_hello_ack *out);
size_t fb3_pack_hello_ack(const struct fb3_hello_ack *in, uint8_t *out, size_t cap);
bool fb3_unpack_clock_probe(const uint8_t *payload, size_t len, struct fb3_clock_probe *out);
size_t fb3_pack_clock_probe(const struct fb3_clock_probe *in, uint8_t *out, size_t cap);
bool fb3_unpack_clock_reply(const uint8_t *payload, size_t len, struct fb3_clock_reply *out);
size_t fb3_pack_clock_reply(const struct fb3_clock_reply *in, uint8_t *out, size_t cap);
bool fb3_unpack_clock_install(const uint8_t *payload, size_t len, struct fb3_clock_install *out);
size_t fb3_pack_clock_install(const struct fb3_clock_install *in, uint8_t *out, size_t cap);
bool fb3_unpack_clock_ack(const uint8_t *payload, size_t len, struct fb3_clock_ack *out);
size_t fb3_pack_clock_ack(const struct fb3_clock_ack *in, uint8_t *out, size_t cap);
bool fb3_unpack_sensor(const uint8_t *payload, size_t len, struct fb3_sensor *out);
size_t fb3_pack_sensor(const struct fb3_sensor *in, uint8_t *out, size_t cap);
bool fb3_unpack_command(const uint8_t *payload, size_t len, struct fb3_command *out);
size_t fb3_pack_command(const struct fb3_command *in, uint8_t *out, size_t cap);
bool fb3_unpack_report(const uint8_t *payload, size_t len, struct fb3_report *out);
size_t fb3_pack_report(const struct fb3_report *in, uint8_t *out, size_t cap);
bool fb3_unpack_tof_sample(const uint8_t *payload, size_t len, struct fb3_tof_sample *out);
/* sample_crc32 는 in 의 값을 그대로 쓴다. 관측의 CRC 를 넣으려면 fb3_seal_tof_sample 을 부른다 */
size_t fb3_pack_tof_sample(const struct fb3_tof_sample *in, uint8_t *out, size_t cap);

/* CRC32/IEEE (reflected 0xedb88320, 처음과 끝에 0xffffffff) */
uint32_t fb3_crc32(const uint8_t *data, size_t len);
/* 묶어 둔 TOF_SAMPLE payload 의 앞 141 B 로 sample_crc32 를 구해 그 자리에 쓴다 */
void fb3_seal_tof_sample(uint8_t payload[FB3_TOF_SAMPLE_SIZE]);

#ifdef __cplusplus
}
#endif

#endif
