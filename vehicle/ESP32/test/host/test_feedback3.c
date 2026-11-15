/* feedback-3 통신 처리 호스트 시험. gen_vectors.py 가 만든 vectors.inc 의 고정 바이트와 대조한다 */
#include "check.h"
#include "feedback3/codes.h"
#include "feedback3/payloads.h"
#include "feedback3/wire.h"

struct frame_case {
	const char *name;
	uint16_t id;
	const char *payload_hex;
	const char *raw_hex;
	const char *wire_hex;
	void (*fields)(const uint8_t *p, size_t n);
};

struct malformed_case {
	const char *name;
	const char *wire_hex;
	enum fb3_frame_error error;
};

struct cobs_case {
	const char *raw_hex;
	const char *encoded_hex;
};

struct tof_case {
	const char *name;
	uint8_t sensor_id;
	uint64_t source_sample;
	uint64_t source_us;
	uint16_t distance_mm;
	uint8_t status;
	uint8_t quality;
	uint32_t crc32;
	const char *payload_hex;
	const char *wire_hex;
};

#include "vectors.inc"
#include "tof_vectors.inc"

static void test_crc(void)
{
	current = "crc16_check";
	CHECK(fb3_crc16((const uint8_t *)CRC_ASCII, strlen(CRC_ASCII)) == CRC_EXPECTED);
}

static void test_cobs(void)
{
	for (size_t i = 0; i < COUNT(COBS); i++) {
		uint8_t raw[512], encoded[512], out[512];
		current = COBS[i].encoded_hex;
		size_t raw_len = from_hex(COBS[i].raw_hex, raw, sizeof raw);
		size_t encoded_len = from_hex(COBS[i].encoded_hex, encoded, sizeof encoded);
		size_t n = fb3_cobs_encode(raw, raw_len, out, sizeof out);
		CHECK(n == encoded_len && memcmp(out, encoded, n) == 0);
		size_t decoded_len = 0;
		CHECK(fb3_cobs_decode(encoded, encoded_len, out, sizeof out, &decoded_len));
		CHECK(decoded_len == raw_len && memcmp(out, raw, raw_len) == 0);
		CHECK(fb3_cobs_encode(raw, raw_len, out, encoded_len - 1) == 0);
	}
}

static void test_frames(void)
{
	for (size_t i = 0; i < COUNT(FRAMES); i++) {
		const struct frame_case *f = &FRAMES[i];
		uint8_t payload[FB3_MAX_WIRE], raw[FB3_MAX_WIRE], wire[FB3_MAX_WIRE], out[FB3_MAX_WIRE];
		struct fb3_frame frame;
		current = f->name;
		size_t payload_len = from_hex(f->payload_hex, payload, sizeof payload);
		size_t raw_len = from_hex(f->raw_hex, raw, sizeof raw);
		size_t wire_len = from_hex(f->wire_hex, wire, sizeof wire);

		size_t n = fb3_encode_frame(f->id, payload, payload_len, out, sizeof out);
		CHECK(n == wire_len && memcmp(out, wire, n) == 0);
		CHECK(fb3_encode_frame(f->id, payload, payload_len, out, wire_len - 1) == 0);

		CHECK(fb3_decode_frame(wire, wire_len, &frame) == FB3_FRAME_OK);
		CHECK(frame.id == f->id && frame.length == payload_len);
		CHECK(memcmp(frame.payload, payload, payload_len) == 0);

		uint16_t crc = (uint16_t)(raw[raw_len - 2] | raw[raw_len - 1] << 8);
		CHECK(fb3_crc16(raw, raw_len - 2) == crc);
		size_t decoded_len = 0;
		CHECK(fb3_cobs_decode(wire, wire_len - 1, out, sizeof out, &decoded_len));
		CHECK(decoded_len == raw_len && memcmp(out, raw, raw_len) == 0);

		/* 한 바이트씩 넣으면 마지막 0x00 에서만 프레임이 나온다 */
		struct fb3_framer framer;
		fb3_framer_init(&framer);
		size_t completed = 0;
		for (size_t b = 0; b < wire_len; b++) {
			if (fb3_framer_push(&framer, wire[b], &frame)) {
				completed++;
				CHECK(b == wire_len - 1);
			}
		}
		CHECK(completed == 1 && framer.invalid == 0 && framer.oversize == 0);
		CHECK(frame.id == f->id && frame.length == payload_len && memcmp(frame.payload, payload, payload_len) == 0);

		f->fields(payload, payload_len);
	}
}

/* 손상 프레임 뒤에 정상 프레임이 오면 Framer 가 다시 받는다 */
static void test_malformed(void)
{
	uint8_t good[FB3_MAX_WIRE];
	current = FRAMES[0].name;
	size_t good_len = from_hex(FRAMES[0].wire_hex, good, sizeof good);
	for (size_t i = 0; i < COUNT(MALFORMED); i++) {
		const struct malformed_case *m = &MALFORMED[i];
		uint8_t wire[512];
		struct fb3_frame frame;
		current = m->name;
		size_t wire_len = from_hex(m->wire_hex, wire, sizeof wire);
		CHECK(fb3_decode_frame(wire, wire_len, &frame) == m->error);

		struct fb3_framer framer;
		fb3_framer_init(&framer);
		size_t completed = 0;
		for (size_t b = 0; b < wire_len; b++) {
			completed += fb3_framer_push(&framer, wire[b], &frame);
		}
		CHECK(completed == 0);
		if (m->error == FB3_FRAME_BOUNDARY_SIZE && wire[wire_len - 1] == 0) {
			CHECK(framer.oversize == 1 && framer.invalid == 0);
		} else if (m->error != FB3_FRAME_BOUNDARY_SIZE) {
			CHECK(framer.invalid == 1 && framer.oversize == 0);
		} else {
			/* 구분자가 없으면 아직 끝나지 않은 조각이다. 0x00 이 오면 그때 Pi 와 같은 검사를 한다 */
			uint8_t closed[513];
			memcpy(closed, wire, wire_len);
			closed[wire_len] = 0;
			bool valid = fb3_decode_frame(closed, wire_len + 1, &frame) == FB3_FRAME_OK;
			CHECK(framer.invalid == 0 && framer.oversize == 0);
			CHECK(fb3_framer_push(&framer, 0, &frame) == valid);
			CHECK(framer.invalid == (valid ? 0u : 1u));
		}
		for (size_t b = 0; b < good_len; b++) {
			completed += fb3_framer_push(&framer, good[b], &frame);
		}
		CHECK(completed == 1 && frame.id == FRAMES[0].id);
	}
}

static void test_boundaries(void)
{
	uint8_t payload[FB3_MAX_PAYLOAD + 1], wire[FB3_MAX_WIRE + 8];
	struct fb3_frame frame;
	current = "payload 240 B";
	for (size_t i = 0; i < sizeof payload; i++) {
		payload[i] = (uint8_t)i;
	}
	size_t n = fb3_encode_frame(0x0f15, payload, FB3_MAX_PAYLOAD, wire, sizeof wire);
	CHECK(n > 0 && n <= FB3_MAX_WIRE);
	CHECK(fb3_decode_frame(wire, n, &frame) == FB3_FRAME_OK);
	CHECK(frame.length == FB3_MAX_PAYLOAD && memcmp(frame.payload, payload, FB3_MAX_PAYLOAD) == 0);

	current = "payload 241 B";
	CHECK(fb3_encode_frame(0x0f15, payload, FB3_MAX_PAYLOAD + 1, wire, sizeof wire) == 0);

	current = "empty delimiter";
	struct fb3_framer framer;
	fb3_framer_init(&framer);
	CHECK(!fb3_framer_push(&framer, 0, &frame) && framer.invalid == 0 && framer.oversize == 0);

	current = "wrong payload size";
	struct fb3_hello hello;
	uint8_t short_buffer[FB3_HELLO_SIZE - 1];
	memset(&hello, 0, sizeof hello);
	CHECK(!fb3_unpack_hello(payload, FB3_HELLO_SIZE + 1, &hello));
	CHECK(fb3_pack_hello(&hello, short_buffer, sizeof short_buffer) == 0);
}

/* 단일 존 ToF 관측. 계약의 따로 둔 vector(gateway-tof-single-1.json)와 대조한다 */
static void test_tof(void)
{
	current = "tof layout";
	CHECK(FB3_TOF_SAMPLE_ID == TOF_ID && FB3_TOF_SAMPLE_SIZE == TOF_PAYLOAD_BYTES);
	CHECK(FB3_TOF_SAMPLE_CRC_SIZE == TOF_CRC_BYTES && FB3_CAP_TOF_SINGLE == TOF_CAPABILITY);
	CHECK(TOF_WIRE_BYTES <= FB3_MAX_WIRE);

	current = "crc32 check";
	CHECK(fb3_crc32((const uint8_t *)"123456789", 9) == 0xcbf43926u && fb3_crc32(NULL, 0) == 0);

	for (size_t i = 0; i < COUNT(TOF_FRAMES); i++) {
		const struct tof_case *t = &TOF_FRAMES[i];
		uint8_t payload[FB3_MAX_WIRE], wire[FB3_MAX_WIRE], out[FB3_MAX_WIRE];
		struct fb3_tof_sample v;
		struct fb3_frame frame;

		current = t->name;
		size_t payload_len = from_hex(t->payload_hex, payload, sizeof payload);
		size_t wire_len = from_hex(t->wire_hex, wire, sizeof wire);
		memset(&v, 0xa5, sizeof v);
		CHECK(payload_len == FB3_TOF_SAMPLE_SIZE && fb3_unpack_tof_sample(payload, payload_len, &v));
		CHECK(v.sensor_id == t->sensor_id && v.source_sample == t->source_sample && v.device_sample_us == t->source_us);
		CHECK(v.rows == 1 && v.cols == 1 && v.distance_mm == t->distance_mm && v.status == t->status
		      && v.quality == t->quality && v.sample_crc32 == t->crc32);
		CHECK(fb3_crc32(payload, FB3_TOF_SAMPLE_CRC_SIZE) == t->crc32);

		/* 같은 값으로 다시 묶으면 같은 바이트가 나온다. CRC 는 seal 이 채운다 */
		v.sample_crc32 = 0;
		memset(out, 0xa5, sizeof out);
		CHECK(fb3_pack_tof_sample(&v, out, sizeof out) == FB3_TOF_SAMPLE_SIZE);
		CHECK(memcmp(out, payload, FB3_TOF_SAMPLE_CRC_SIZE) == 0 && memcmp(out, payload, payload_len) != 0);
		fb3_seal_tof_sample(out);
		CHECK(memcmp(out, payload, payload_len) == 0);
		CHECK(fb3_pack_tof_sample(&v, out, FB3_TOF_SAMPLE_SIZE - 1) == 0);
		CHECK(!fb3_unpack_tof_sample(payload, payload_len - 1, &v));

		size_t n = fb3_encode_frame(FB3_TOF_SAMPLE_ID, payload, payload_len, out, sizeof out);
		CHECK(n == wire_len && n <= TOF_WIRE_BYTES && memcmp(out, wire, n) == 0);
		CHECK(fb3_decode_frame(wire, wire_len, &frame) == FB3_FRAME_OK);
		CHECK(frame.id == FB3_TOF_SAMPLE_ID && frame.length == payload_len
		      && memcmp(frame.payload, payload, payload_len) == 0);
	}
}

int main(void)
{
	test_crc();
	test_cobs();
	current = "layouts";
	check_layouts();
	test_frames();
	test_malformed();
	test_boundaries();
	test_tof();
	printf("feedback-3: %zu frames, %zu malformed, %zu cobs, %zu tof, %d checks, %d failures\n",
		   COUNT(FRAMES), COUNT(MALFORMED), COUNT(COBS), COUNT(TOF_FRAMES), checks, failures);
	return failures == 0 ? 0 : 1;
}
