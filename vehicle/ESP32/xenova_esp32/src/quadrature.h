/* 엔코더 A·B 두 선의 4체배 해석. 방향은 조립 안내서 단계 K 에서 확인한 것과 같다(앞으로 굴리면 늘어난다).
 * 인터럽트 안에서 쓰므로 표 없이 계산하고 헤더에 둔다 */
#ifndef XENOVA_QUADRATURE_H
#define XENOVA_QUADRATURE_H

#include <stdint.h>

#define QUADRATURE_INLINE static inline __attribute__((always_inline))

struct quadrature {
	uint8_t state;  /* 직전 A·B. bit1 이 A, bit0 이 B */
	uint32_t count; /* 누계. 넘치면 돌아간다 */
};

QUADRATURE_INLINE uint8_t quadrature_state(int a, int b)
{
	return (uint8_t)((a ? 2u : 0u) | (b ? 1u : 0u));
}

/* 한 칸 움직였으면 +1 이나 -1. 그대로거나 두 선이 한꺼번에 바뀌었으면 0 */
QUADRATURE_INLINE int quadrature_delta(uint8_t previous, uint8_t current)
{
	return (int)((previous ^ (current >> 1)) & 1u) - (int)(((previous >> 1) ^ current) & 1u);
}

/* 지금 A·B 에서 시작한다. 인터럽트를 걸기 전에 부른다.
 * 실제 선 상태와 다른 값에서 시작하면 첫 움직임을 거꾸로 셀 수 있다 */
QUADRATURE_INLINE void quadrature_start(volatile struct quadrature *q, int a, int b)
{
	q->state = quadrature_state(a, b);
	q->count = 0;
}

/* A 나 B 가 바뀔 때마다 지금 값으로 부른다 */
QUADRATURE_INLINE void quadrature_edge(volatile struct quadrature *q, int a, int b)
{
	uint8_t now = quadrature_state(a, b);

	q->count = q->count + (uint32_t)quadrature_delta(q->state, now);
	q->state = now;
}

#endif
