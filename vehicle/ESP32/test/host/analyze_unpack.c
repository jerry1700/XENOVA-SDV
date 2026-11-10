/* GCC -fanalyzer 로 컴파일만 한다. 출력 구조체를 초기화하지 않고 unpack 하는 보통의 사용을 적어 두어,
 * unpack 이 출력 구조체의 기존 값을 읽으면 분석 경고로 빌드가 멈춘다.
 * test_feedback3 은 출력 구조체를 0xa5 로 채운 뒤 unpack 하므로 이런 읽기를 드러내지 못한다 */
#include "payloads.c"

#define FB3_ANALYZE(name, type, field) \
	int analyze_##name(const uint8_t *payload, size_t len) \
	{ \
		type out; \
		if (!fb3_unpack_##name(payload, len, &out)) { \
			return -1; \
		} \
		return (int)out.field; \
	}

FB3_ANALYZE(hello, struct fb3_hello, version)
FB3_ANALYZE(hello_ack, struct fb3_hello_ack, capabilities)
FB3_ANALYZE(clock_probe, struct fb3_clock_probe, probe_sequence)
FB3_ANALYZE(clock_reply, struct fb3_clock_reply, cumulative_left_ticks)
FB3_ANALYZE(clock_install, struct fb3_clock_install, lower_offset_us)
FB3_ANALYZE(clock_ack, struct fb3_clock_ack, reason)
FB3_ANALYZE(sensor, struct fb3_sensor, steering_mrad)
FB3_ANALYZE(command, struct fb3_command, velocity_mmps)
FB3_ANALYZE(report, struct fb3_report, generation)
