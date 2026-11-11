/* 엔코더 4체배 해석 호스트 시험 */
#include "check.h"
#include "quadrature.h"

/* 조립 안내서 단계 K 에서 실차로 확인한 bench 스케치의 표. (직전 << 2 | 지금) 으로 찾는다 */
static const int8_t BENCH[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};
/* 앞으로 굴릴 때 A·B 가 지나가는 순서 */
static const uint8_t FORWARD[4] = {0, 2, 3, 1};

static void edge(volatile struct quadrature *q, uint8_t state)
{
	quadrature_edge(q, state >> 1, state & 1);
}

static void test_delta(void)
{
	current = "delta: bench 표와 같음";
	for (unsigned previous = 0; previous < 4; previous++) {
		for (unsigned now = 0; now < 4; now++) {
			CHECK(quadrature_delta((uint8_t)previous, (uint8_t)now) == BENCH[previous << 2 | now]);
		}
	}
}

static void test_first_move(void)
{
	current = "어느 A·B 에서 켜져도 첫 움직임의 방향이 맞음";
	for (unsigned i = 0; i < 4; i++) {
		uint8_t here = FORWARD[i];
		volatile struct quadrature q;

		quadrature_start(&q, here >> 1, here & 1);
		CHECK(q.state == here && q.count == 0);
		edge(&q, FORWARD[(i + 1) % 4]);
		CHECK(q.count == 1);

		quadrature_start(&q, here >> 1, here & 1);
		edge(&q, FORWARD[(i + 3) % 4]);
		CHECK(q.count == UINT32_MAX);
	}

	current = "A·B 가 둘 다 High 일 때 켜진 뒤 B 만 남는 움직임은 앞으로";
	{
		volatile struct quadrature q;

		quadrature_start(&q, 1, 1);
		quadrature_edge(&q, 0, 1);
		CHECK(q.count == 1);
		/* 실제 선 상태가 아닌 0 에서 시작했다면 같은 움직임을 뒤로 센다 */
		CHECK(quadrature_delta(0, quadrature_state(0, 1)) == -1);
	}
}

static void test_counting(void)
{
	volatile struct quadrature q;

	current = "한 바퀴에 네 칸, 되돌아오면 0";
	for (unsigned i = 0; i < 4; i++) {
		quadrature_start(&q, FORWARD[i] >> 1, FORWARD[i] & 1);
		for (unsigned step = 1; step <= 8; step++) {
			edge(&q, FORWARD[(i + step) % 4]);
		}
		CHECK(q.count == 8 && q.state == FORWARD[i]);
		for (unsigned step = 1; step <= 8; step++) {
			edge(&q, FORWARD[(i + 8 - step) % 4]);
		}
		CHECK(q.count == 0 && q.state == FORWARD[i]);
	}

	current = "그대로거나 두 선이 한꺼번에 바뀌면 세지 않음";
	quadrature_start(&q, 0, 0);
	edge(&q, 0);
	CHECK(q.count == 0);
	edge(&q, 3);
	CHECK(q.count == 0 && q.state == 3);
	edge(&q, 1);
	CHECK(q.count == 1);

	current = "누계는 32비트에서 돌아감";
	quadrature_start(&q, 0, 0);
	q.count = UINT32_MAX;
	edge(&q, 2);
	CHECK(q.count == 0);
	edge(&q, 0);
	CHECK(q.count == UINT32_MAX);
}

int main(void)
{
	test_delta();
	test_first_move();
	test_counting();
	printf("quadrature: %d checks, %d failures\n", checks, failures);
	return failures == 0 ? 0 : 1;
}
