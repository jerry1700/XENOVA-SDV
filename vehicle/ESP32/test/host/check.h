/* 호스트 시험이 같이 쓰는 검사 매크로와 hex 도우미 */
#ifndef XENOVA_HOST_CHECK_H
#define XENOVA_HOST_CHECK_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks;
static int failures;
static const char *current = "";

#define CHECK(cond) \
	do { \
		checks++; \
		if (!(cond)) { \
			failures++; \
			fprintf(stderr, "FAIL %s:%d [%s] %s\n", __FILE__, __LINE__, current, #cond); \
		} \
	} while (0)

#define COUNT(array) (sizeof(array) / sizeof((array)[0]))

static inline int hex_digit(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

/* 시험 데이터가 잘못됐으면 바로 끝낸다 */
static inline size_t from_hex(const char *hex, uint8_t *out, size_t cap)
{
	size_t len = strlen(hex);
	if (len % 2 != 0 || len / 2 > cap) {
		fprintf(stderr, "bad hex [%s]\n", current);
		exit(2);
	}
	for (size_t i = 0; i < len / 2; i++) {
		int hi = hex_digit(hex[2 * i]);
		int lo = hex_digit(hex[2 * i + 1]);
		if (hi < 0 || lo < 0) {
			fprintf(stderr, "bad hex [%s]\n", current);
			exit(2);
		}
		out[i] = (uint8_t)(hi << 4 | lo);
	}
	return len / 2;
}

static inline bool same(const uint8_t *field, size_t size, const char *hex)
{
	uint8_t expected[64];
	return from_hex(hex, expected, sizeof expected) == size && memcmp(field, expected, size) == 0;
}

#endif
