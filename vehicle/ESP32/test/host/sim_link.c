/* 펌웨어의 통신 처리(link·cutoff)를 PC 에서 돌리는 대역. stdin 으로 받은 바이트에 stdout 으로 답하고,
 * 스케치의 loop 처럼 입력이 없을 때에도 1 ms 마다 돌면서 주기 표본을 보낸다.
 * 시계는 PC 의 단조 시계다. 엔코더는 DRIVE 로 출력이 걸려 있는 동안 명령한 속도대로 돌고, 출력이 끊기면 그 자리에 선다.
 * 인자로 초당 칸 수를 주면 명령과 상관없이 양쪽 바퀴가 그 속도로 더 돈다. 모터와 차체의 움직임을 흉내 낸 것이 아니다.
 * 가짜 ToF 는 앞 600 mm, 뒤 800 mm 를 그대로 본다. 둘째 인자로 앞 벽까지의 거리(mm)를 주면, 출력으로 굴러간 만큼
 * 앞 거리는 줄고 뒤 거리는 는다. 근거리 제동을 맞물려 보는 데 쓴다.
 * alarm 은 맞춘 시각에 울린 것으로 순서대로 처리한다. 실제 ESP32·USB·타이머 시험이 아니다.
 * Windows 에서는 stdin 이 pipe 여야 한다.
 * tools/link_bench.py, tools/cutoff_stress.py, tools/pi_link_check.py 가 쓴다
 *
 *   sim_link [초당 칸 수 [앞 벽까지 mm]] */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#include <mmsystem.h>
#else
#include <poll.h>
#include <time.h>
#include <unistd.h>
#endif

#include "device_config.h"
#include "feedback3/link.h"

/* 장치가 켜진 지 이만큼 지난 것으로 시작한다. 장치 시계와 Pi 시계는 서로 다른 값이다 */
#define SIM_BOOT_AGE_US UINT64_C(9000000)

struct sim {
	uint64_t origin; /* 시작할 때의 PC 시각 */
	bool alarm_set;
	uint64_t alarm_at;
	bool in_alarm; /* alarm 을 처리하는 동안 시계는 울린 시각에 멈춘다 */
	uint64_t frozen_us;
	int64_t ticks_per_second; /* 명령과 상관없이 양쪽 엔코더가 도는 속도 */
	int32_t drive_mmps;       /* 출력에 걸려 있는 속도. 끊겨 있으면 0 */
	int64_t driven_um;        /* 출력으로 굴러간 거리 */
	uint64_t driven_us;       /* 그 거리를 셈한 시각 */
	uint64_t tof_due_us[FB3_TOF_SENSORS]; /* 가짜 ToF 가 다음 관측을 낼 시각 */
	uint8_t tof_instance[FB3_TOF_SENSORS][16];
	int64_t wall_mm; /* 시작할 때 앞 센서에서 벽까지의 거리. 0 이면 벽이 없다 */
	struct fb3_cutoff cutoff;
};

/* 가짜 ToF. 측정 하나에 걸리는 시간과 관측을 내는 간격 */
#define SIM_TOF_MEASURE_US 33000u
#define SIM_TOF_PERIOD_US 100000u

static uint64_t host_us(void)
{
#ifdef _WIN32
	LARGE_INTEGER frequency, counter;

	QueryPerformanceFrequency(&frequency);
	QueryPerformanceCounter(&counter);
	return (uint64_t)(counter.QuadPart / frequency.QuadPart) * 1000000u
		+ (uint64_t)(counter.QuadPart % frequency.QuadPart) * 1000000u / (uint64_t)frequency.QuadPart;
#else
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
#endif
}

static uint64_t sim_now(void *ctx)
{
	struct sim *s = ctx;

	return s->in_alarm ? s->frozen_us : host_us() - s->origin + SIM_BOOT_AGE_US;
}

static bool sim_set_alarm(void *ctx, uint64_t at_us)
{
	struct sim *s = ctx;

	/* port 가 허락하는 가장 불리한 경우로, 이미 지난 값에는 울리지 않는다 */
	s->alarm_set = at_us > sim_now(s);
	s->alarm_at = at_us;
	return true;
}

static void sim_clear_alarm(void *ctx)
{
	struct sim *s = ctx;

	s->alarm_set = false;
}

static void sim_lock(void *ctx)
{
	(void)ctx;
}

static void sim_unlock(void *ctx)
{
	(void)ctx;
}

/* 출력이 걸려 있던 동안 굴러간 거리를 지금까지 셈한다 */
static void sim_roll(struct sim *s, uint64_t now)
{
	s->driven_um += (int64_t)s->drive_mmps * (int64_t)(now - s->driven_us) / 1000;
	s->driven_us = now;
}

static void sim_cut(void *ctx)
{
	struct sim *s = ctx;

	sim_roll(s, sim_now(s));
	s->drive_mmps = 0;
}

static bool sim_drive(void *ctx, int16_t velocity_mmps, int16_t steering_mrad)
{
	struct sim *s = ctx;

	(void)steering_mrad;
	sim_roll(s, sim_now(s));
	s->drive_mmps = velocity_mmps;
	return true;
}

static bool sim_estop(void *ctx)
{
	(void)ctx;
	return false;
}

/* 앞은 600 mm, 뒤는 800 mm 의 물체를 100 ms 마다 본다. 측정 구간의 가운데를 관측 시각으로 낸다.
 * 벽이 있으면 굴러간 거리만큼 앞은 가까워지고 뒤는 멀어진다 */
static bool sim_tof(void *ctx, unsigned sensor, struct fb3_tof_reading *out)
{
	struct sim *s = ctx;
	uint64_t now = sim_now(s);

	if (now < s->tof_due_us[sensor]) {
		return false;
	}
	s->tof_due_us[sensor] = now + SIM_TOF_PERIOD_US;
	out->device_us = now - SIM_TOF_MEASURE_US / 2;
	out->time_error_us = SIM_TOF_MEASURE_US / 2 + 1000;
	memcpy(out->instance, s->tof_instance[sensor], 16);
	out->generation = 1;
	out->distance_mm = sensor == 0 ? 600 : 800;
	out->status = FB3_TOF_VALID;
	if (s->wall_mm != 0) {
		int64_t rolled_mm, distance;

		sim_roll(s, now);
		rolled_mm = s->driven_um / 1000;
		distance = sensor == 0 ? s->wall_mm - rolled_mm : 800 + rolled_mm;
		out->distance_mm = (uint16_t)(distance < 1 ? 1 : distance > 2000 ? 2000 : distance);
	}
	return true;
}

static bool sim_latch(void *ctx, struct fb3_sample *out)
{
	struct sim *s = ctx;
	int64_t ticks;

	out->device_us = sim_now(s);
	sim_roll(s, out->device_us);
	ticks = (int64_t)(out->device_us - SIM_BOOT_AGE_US) * s->ticks_per_second / 1000000
		+ s->driven_um / XENOVA_UM_PER_TICK;
	/* 펌웨어처럼 32비트 누계이고 넘치면 돌아간다 */
	out->left_ticks = (int32_t)(uint32_t)ticks;
	out->right_ticks = (int32_t)(uint32_t)ticks;
	out->quality = FB3_QUALITY_ENCODER;
	out->steering_mrad = 0;
	out->steering_error_mrad = 0;
	return true;
}

static void sim_run_alarms(struct sim *s)
{
	while (s->alarm_set && s->alarm_at <= sim_now(s)) {
		s->alarm_set = false;
		s->frozen_us = s->alarm_at;
		s->in_alarm = true;
		fb3_cutoff_on_alarm(&s->cutoff);
		s->in_alarm = false;
	}
}

/* 입력을 1 ms 까지 기다려 읽는다. 읽은 바이트 수, 그동안 온 것이 없으면 0, 입력이 닫혔으면 -1 */
static int host_read(uint8_t *buffer, int cap)
{
#ifdef _WIN32
	HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
	DWORD waiting = 0, got = 0;

	if (!PeekNamedPipe(input, NULL, 0, NULL, &waiting, NULL)) {
		return -1;
	}
	if (waiting == 0) {
		Sleep(1);
		return 0;
	}
	if (!ReadFile(input, buffer, waiting < (DWORD)cap ? waiting : (DWORD)cap, &got, NULL) || got == 0) {
		return -1;
	}
	return (int)got;
#else
	struct pollfd input = {0, POLLIN, 0};
	ssize_t got;

	if (poll(&input, 1, 1) <= 0) {
		return 0;
	}
	got = read(0, buffer, (size_t)cap);
	return got > 0 ? (int)got : -1;
#endif
}

static void sim_send(const uint8_t *wire, size_t len)
{
	if (len > 0) {
		fwrite(wire, 1, len, stdout);
		fflush(stdout);
	}
}

/* 실행할 때마다 다른 부팅 session (RFC 4122 version 4 모양) */
static void make_session(uint8_t out[16])
{
	uint64_t seed = host_us() ^ (uint64_t)(uintptr_t)out;

	for (int i = 0; i < 16; i++) {
		uint64_t z;

		seed += UINT64_C(0x9e3779b97f4a7c15);
		z = seed;
		z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
		z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
		out[i] = (uint8_t)(z ^ (z >> 31));
	}
	out[6] = (uint8_t)((out[6] & 0x0f) | 0x40);
	out[8] = (uint8_t)((out[8] & 0x3f) | 0x80);
}

int main(int argc, char **argv)
{
	static const uint8_t config_sha256[32] = XENOVA_VEHICLE_CONFIG_SHA256;
	static const uint8_t tof_front_sha256[32] = XENOVA_TOF_FRONT_SHA256, tof_rear_sha256[32] = XENOVA_TOF_REAR_SHA256;
	/* ASCII 문자열 "xenova-esp32-host-sim" 의 SHA-256. 실제 펌웨어의 digest 가 아니다 */
	static const uint8_t firmware_sha256[32] = {
		0x99, 0xb6, 0xed, 0xce, 0xf8, 0x58, 0xec, 0xad, 0x1d, 0xdf, 0x43, 0x83, 0x85, 0x75, 0x49, 0x28,
		0xfd, 0xcf, 0x0d, 0x9d, 0xf4, 0x27, 0x75, 0x7a, 0x04, 0x7b, 0x75, 0xc9, 0x30, 0x85, 0xd3, 0x22,
	};
	static struct sim sim;
	static struct fb3_link link;
	static struct fb3_framer framer;
	static struct fb3_frame frame;
	struct fb3_cutoff_port cutoff_port = {&sim, sim_now, sim_set_alarm, sim_clear_alarm, sim_lock, sim_unlock,
					      sim_cut};
	struct fb3_link_port link_port = {&sim, sim_now, sim_latch, sim_drive, sim_estop, sim_tof};
	struct fb3_link_config config;
	uint8_t wire[FB3_MAX_WIRE], input[256];
	int got;

#ifdef _WIN32
	_setmode(_fileno(stdout), _O_BINARY);
	timeBeginPeriod(1); /* Sleep(1) 이 1 ms 언저리로 깨게 한다 */
#endif
	sim.ticks_per_second = argc > 1 ? strtoll(argv[1], NULL, 10) : 0;
	sim.wall_mm = argc > 2 ? strtoll(argv[2], NULL, 10) : 0;
	sim.origin = host_us();
	sim.driven_us = sim_now(&sim);
	fb3_cutoff_init(&sim.cutoff, &cutoff_port);
	memset(&config, 0, sizeof config);
	make_session(config.device_boot_session);
	config.capabilities = XENOVA_CAPABILITIES;
	config.tof[0].config_revision = XENOVA_TOF_FRONT_REVISION;
	memcpy(config.tof[0].calibration_sha256, tof_front_sha256, sizeof tof_front_sha256);
	config.tof[1].config_revision = XENOVA_TOF_REAR_REVISION;
	memcpy(config.tof[1].calibration_sha256, tof_rear_sha256, sizeof tof_rear_sha256);
	make_session(sim.tof_instance[0]);
	make_session(sim.tof_instance[1]);
	sim.tof_due_us[0] = sim_now(&sim) + SIM_TOF_PERIOD_US;
	sim.tof_due_us[1] = sim_now(&sim) + SIM_TOF_PERIOD_US * 3 / 2;
	memcpy(config.vehicle_config_sha256, config_sha256, sizeof config_sha256);
	memcpy(config.firmware_sha256, firmware_sha256, sizeof firmware_sha256);
	config.encoder_bits = XENOVA_ENCODER_BITS;
	config.um_per_tick = XENOVA_UM_PER_TICK;
	config.j_apply_us = XENOVA_J_APPLY_US;
	config.max_velocity_mmps = XENOVA_MAX_VELOCITY_MMPS;
	config.max_steering_mrad = XENOVA_MAX_STEERING_MRAD;
	config.proximity_stop_mm = XENOVA_PROXIMITY_STOP_MM;
	config.proximity_clear_mm = XENOVA_PROXIMITY_CLEAR_MM;
	config.proximity_stale_us = XENOVA_PROXIMITY_STALE_US;
	fb3_link_init(&link, &config, &link_port, &sim.cutoff);
	fb3_framer_init(&framer);
	/* 스케치처럼 켜지면 구분자 하나를 먼저 보낸다 */
	putchar(0);
	fflush(stdout);

	while ((got = host_read(input, (int)sizeof input)) >= 0) {
		for (int i = 0; i < got; i++) {
			sim_run_alarms(&sim);
			if (fb3_framer_push(&framer, input[i], &frame)) {
				sim_send(wire, fb3_link_receive(&link, &frame, sim_now(&sim), wire, sizeof wire));
			}
		}
		sim_run_alarms(&sim);
		sim_send(wire, fb3_link_poll(&link, wire, sizeof wire));
	}
	return 0;
}
