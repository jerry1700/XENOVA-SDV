/* 기한(cutoff)을 하드웨어 alarm 하나로 지킨다.
 * 칸마다 기한을 걸어 두면 가장 이른 기한에 alarm 을 맞추고, alarm 이 울리면 지난 칸을 만료로 바꾼 뒤
 * 다음 기한에 다시 맞춘다. 메인 루프가 멈춰 있어도 alarm 인터럽트가 만료를 처리한다.
 * 계약 docs/contracts/gateway-feedback-3.md §5·§6 의 독립 timer 다. 앞의 칸들은 lease 의 기한이고,
 * 마지막 칸은 출력의 기한이다. 출력의 기한이 지나거나 타이머가 고장 나면 그 자리에서 출력을 끊는다.
 * 하드웨어는 port 로 받아 PC 호스트 시험에서도 그대로 돈다. */
#ifndef XENOVA_FB3_CUTOFF_H
#define XENOVA_FB3_CUTOFF_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FB3_CUTOFF_SLOTS 3u
#define FB3_CUTOFF_LEASES 2u /* 앞의 두 칸은 lease 의 기한 */
#define FB3_CUTOFF_OUTPUT 2u /* 이 칸은 출력의 기한 */
/* 기한을 이만큼 넘기도록 만료 처리가 안 됐으면 타이머 고장으로 본다 */
#define FB3_CUTOFF_LATE_US 1000u

enum fb3_cutoff_state {
	FB3_CUTOFF_IDLE = 0, /* 걸린 기한이 없다 */
	FB3_CUTOFF_LIVE,     /* 기한 전이다 */
	FB3_CUTOFF_EXPIRED,  /* 기한에 만료 처리했다 */
	FB3_CUTOFF_FAULT,    /* 타이머 고장으로 기한을 지키지 못했다 */
};

/* lock 은 alarm 인터럽트를 막는다. 모든 함수는 인터럽트 안에서도 부를 수 있어야 한다 */
struct fb3_cutoff_port {
	void *ctx;
	uint64_t (*now_us)(void *ctx);
	/* 카운터가 at_us 와 같아지는 순간 울리게 한다. 이미 지난 값이면 울리지 않아도 된다 */
	bool (*set_alarm)(void *ctx, uint64_t at_us);
	void (*clear_alarm)(void *ctx);
	void (*lock)(void *ctx);
	void (*unlock)(void *ctx);
	/* 출력을 안전한 상태로 끊는다. 이미 끊겨 있어도 된다 */
	void (*cut)(void *ctx);
};

struct fb3_cutoff_slot {
	enum fb3_cutoff_state state;
	uint64_t at_us;
};

struct fb3_cutoff {
	struct fb3_cutoff_port port;
	struct fb3_cutoff_slot slot[FB3_CUTOFF_SLOTS];
	bool fault;       /* 한 번 고장 나면 다시 켤 때까지 새 기한을 받지 않는다 */
	uint32_t expired; /* 기한에 만료 처리한 횟수 */
};

void fb3_cutoff_init(struct fb3_cutoff *cutoff, const struct fb3_cutoff_port *port);

/* 아래 세 함수는 먼저 걸려 있는 기한을 모두 검사한다. 기한을 FB3_CUTOFF_LATE_US 넘게 지나고도
 * 살아 있는 칸이 있으면 alarm 이 울리지 않은 것이므로 타이머 고장으로 굳히고, 그 뒤로는 풀리지 않는다 */

/* 칸에 기한을 건다. 그 칸에 걸려 있던 기한은 새 기한으로 바뀐다.
 * LIVE 면 alarm 을 맞췄고 아직 기한 전이다. 이미 지난 기한이면 EXPIRED, 타이머 고장이면 FAULT 이고
 * 그 기한은 걸리지 않는다 */
enum fb3_cutoff_state fb3_cutoff_arm(struct fb3_cutoff *cutoff, unsigned slot, uint64_t at_us);
void fb3_cutoff_cancel(struct fb3_cutoff *cutoff, unsigned slot);
/* 기한이 지났으면 alarm 인터럽트가 처리하기 전이라도 EXPIRED 로 답한다 */
enum fb3_cutoff_state fb3_cutoff_query(struct fb3_cutoff *cutoff, unsigned slot);

/* 출력 칸에 기한을 걸고, 걸렸으면 lock 을 쥔 채 apply 로 출력을 건다. 그래서 기한의 alarm 은 apply 가 끝난 뒤에야
 * 처리되고, 기한 뒤에 출력이 켜진 채 남지 않는다. 기한까지 margin_us 넘게 남아 있어야 건다.
 * LIVE 면 출력을 걸었다. 기한이 모자라면 EXPIRED 이고 앞서 걸려 있던 출력과 기한은 그대로다.
 * 거는 사이에 기한이 지났으면 EXPIRED, 타이머 고장이거나 apply 가 false 면 FAULT 이고 두 경우 모두 출력을 끊는다 */
enum fb3_cutoff_state fb3_cutoff_engage(struct fb3_cutoff *cutoff, uint64_t at_us, uint64_t margin_us,
					bool (*apply)(void *arg), void *arg);
/* 출력을 끊고 출력의 기한을 지운다 */
void fb3_cutoff_cut(struct fb3_cutoff *cutoff);
/* 걸려 있는 기한을 모두 검사하고, 타이머가 고장으로 굳어 있는지 답한다 */
bool fb3_cutoff_faulted(struct fb3_cutoff *cutoff);

/* alarm 인터럽트에서 부른다 */
void fb3_cutoff_on_alarm(struct fb3_cutoff *cutoff);

#ifdef __cplusplus
}
#endif

#endif
