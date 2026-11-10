#include "wire.h"

#include <string.h>

uint16_t fb3_crc16(const uint8_t *data, size_t len)
{
	uint16_t crc = 0xffff;
	for (size_t i = 0; i < len; i++) {
		crc ^= (uint16_t)(data[i] << 8);
		for (int bit = 0; bit < 8; bit++) {
			crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
		}
	}
	return crc;
}

/* Pi frame.cobs_encode 와 같다. 254 B 블록이 데이터 끝에 오면 빈 블록 코드 0x01 을 하나 더 쓴다 */
size_t fb3_cobs_encode(const uint8_t *data, size_t len, uint8_t *out, size_t cap)
{
	if (cap == 0) {
		return 0;
	}
	size_t n = 1;
	size_t code_at = 0;
	uint8_t code = 1;
	for (size_t i = 0; i < len; i++) {
		if (data[i] != 0) {
			if (n >= cap) {
				return 0;
			}
			out[n++] = data[i];
			code++;
		}
		if (data[i] == 0 || code == 0xff) {
			if (n >= cap) {
				return 0;
			}
			out[code_at] = code;
			code_at = n++;
			code = 1;
		}
	}
	out[code_at] = code;
	return n;
}

bool fb3_cobs_decode(const uint8_t *data, size_t len, uint8_t *out, size_t cap, size_t *out_len)
{
	size_t index = 0;
	size_t n = 0;
	while (index < len) {
		uint8_t code = data[index];
		size_t end = index + code;
		if (code == 0 || end > len) {
			return false;
		}
		for (size_t i = index + 1; i < end; i++) {
			if (data[i] == 0 || n >= cap) {
				return false;
			}
			out[n++] = data[i];
		}
		index = end;
		if (code < 0xff && index < len) {
			if (n >= cap) {
				return false;
			}
			out[n++] = 0;
		}
	}
	*out_len = n;
	return true;
}

size_t fb3_encode_frame(uint16_t id, const uint8_t *payload, size_t len, uint8_t *wire, size_t cap)
{
	uint8_t raw[FB3_MAX_PAYLOAD + FB3_RAW_OVERHEAD];
	if (len > FB3_MAX_PAYLOAD) {
		return 0;
	}
	raw[0] = (uint8_t)id;
	raw[1] = (uint8_t)(id >> 8);
	raw[2] = (uint8_t)len;
	if (len > 0) {
		memcpy(raw + 3, payload, len);
	}
	uint16_t crc = fb3_crc16(raw, len + 3);
	raw[len + 3] = (uint8_t)crc;
	raw[len + 4] = (uint8_t)(crc >> 8);
	size_t n = fb3_cobs_encode(raw, len + FB3_RAW_OVERHEAD, wire, cap);
	if (n == 0 || n >= cap) {
		return 0;
	}
	wire[n] = 0;
	return n + 1;
}

/* 구분자를 뺀 COBS 바이트를 풀어 검사한다. fb3_decode_frame 과 Framer 가 같이 쓴다 */
static enum fb3_frame_error decode_encoded(const uint8_t *encoded, size_t len, struct fb3_frame *out)
{
	uint8_t raw[FB3_MAX_WIRE];
	size_t raw_len = 0;
	if (!fb3_cobs_decode(encoded, len, raw, sizeof raw, &raw_len)) {
		return FB3_FRAME_COBS;
	}
	if (raw_len < FB3_RAW_OVERHEAD) {
		return FB3_FRAME_SHORT;
	}
	uint8_t length = raw[2];
	uint16_t crc = (uint16_t)(raw[raw_len - 2] | raw[raw_len - 1] << 8);
	if (raw_len != (size_t)length + FB3_RAW_OVERHEAD || fb3_crc16(raw, raw_len - 2) != crc) {
		return FB3_FRAME_LENGTH_CRC;
	}
	out->id = (uint16_t)(raw[0] | raw[1] << 8);
	out->length = length;
	memcpy(out->payload, raw + 3, length);
	return FB3_FRAME_OK;
}

enum fb3_frame_error fb3_decode_frame(const uint8_t *wire, size_t len, struct fb3_frame *out)
{
	if (len == 0 || wire[len - 1] != 0 || len > FB3_MAX_WIRE) {
		return FB3_FRAME_BOUNDARY_SIZE;
	}
	return decode_encoded(wire, len - 1, out);
}

void fb3_framer_init(struct fb3_framer *framer)
{
	memset(framer, 0, sizeof *framer);
}

bool fb3_framer_push(struct fb3_framer *framer, uint8_t byte, struct fb3_frame *out)
{
	if (byte == 0) {
		bool done = false;
		if (framer->discard) {
			framer->discard = false;
		} else if (framer->length > 0) {
			if (decode_encoded(framer->buffer, framer->length, out) == FB3_FRAME_OK) {
				done = true;
			} else {
				framer->invalid++;
			}
		}
		framer->length = 0;
		return done;
	}
	if (framer->discard) {
		return false;
	}
	if (framer->length >= sizeof framer->buffer) {
		framer->length = 0;
		framer->discard = true;
		framer->oversize++;
		return false;
	}
	framer->buffer[framer->length++] = byte;
	return false;
}
