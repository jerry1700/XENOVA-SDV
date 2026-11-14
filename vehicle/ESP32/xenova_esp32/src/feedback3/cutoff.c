#include "cutoff.h"

/* 타이머를 믿을 수 없으면 남은 기한도 지킬 수 없다. 출력부터 끊는다 */
static void fail(struct fb3_cutoff *c)
{
	c->port.cut(c->port.ctx);
	c->fault = true;
	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		if (c->slot[i].state == FB3_CUTOFF_LIVE) {
			c->slot[i].state = FB3_CUTOFF_FAULT;
		}
	}
	c->port.clear_alarm(c->port.ctx);
}

/* lock 을 쥔 채 부른다. 기한을 FB3_CUTOFF_LATE_US 넘게 지나고도 살아 있는 칸이 있으면 alarm 이 울리지 않은 것이다.
 * 칸을 바꾸거나 지우기 전에 불러서 그 흔적이 사라지지 않게 한다. 읽은 시각을 돌려준다 */
static uint64_t audit(struct fb3_cutoff *c)
{
	uint64_t now = c->port.now_us(c->port.ctx);

	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		const struct fb3_cutoff_slot *slot = &c->slot[i];

		if (slot->state == FB3_CUTOFF_LIVE && slot->at_us <= now && now - slot->at_us > FB3_CUTOFF_LATE_US) {
			fail(c);
			break;
		}
	}
	return now;
}

/* lock 을 쥔 채 부른다. 지난 칸을 만료로 바꾸고, 남은 기한 가운데 가장 이른 것에 alarm 을 맞춘다.
 * port 의 alarm 은 이미 지난 값에는 울리지 않아도 된다. 맞추는 사이 기한이 지났을 수 있으므로
 * 맞춘 뒤에 시각을 다시 읽고, 지났으면 돌아가 여기서 만료 처리한다.
 * alarm 인터럽트와, alarm 을 다시 맞추는 arm·cancel 이 부른다. arm·cancel 이 기한 뒤
 * FB3_CUTOFF_LATE_US 안에 불리면 그 기한의 alarm 이 실제로 울렸는지는 가리지 못한다 */
static void service(struct fb3_cutoff *c)
{
	for (;;) {
		uint64_t now = c->port.now_us(c->port.ctx);
		uint64_t next = 0;
		bool pending = false;
		bool missed = false;

		for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
			struct fb3_cutoff_slot *slot = &c->slot[i];

			if (slot->state != FB3_CUTOFF_LIVE) {
				continue;
			}
			if (slot->at_us > now) {
				if (!pending || slot->at_us < next) {
					next = slot->at_us;
					pending = true;
				}
			} else if (now - slot->at_us <= FB3_CUTOFF_LATE_US) {
				if (i == FB3_CUTOFF_OUTPUT) {
					c->port.cut(c->port.ctx);
				}
				slot->state = FB3_CUTOFF_EXPIRED;
				c->expired++;
			} else {
				missed = true;
			}
		}
		if (missed || (pending && !c->port.set_alarm(c->port.ctx, next))) {
			fail(c);
			return;
		}
		if (!pending) {
			c->port.clear_alarm(c->port.ctx);
			return;
		}
		if (c->port.now_us(c->port.ctx) < next) {
			return;
		}
	}
}

void fb3_cutoff_init(struct fb3_cutoff *c, const struct fb3_cutoff_port *port)
{
	c->port = *port;
	for (unsigned i = 0; i < FB3_CUTOFF_SLOTS; i++) {
		c->slot[i].state = FB3_CUTOFF_IDLE;
		c->slot[i].at_us = 0;
	}
	c->fault = false;
	c->expired = 0;
	c->port.clear_alarm(c->port.ctx);
}

enum fb3_cutoff_state fb3_cutoff_arm(struct fb3_cutoff *c, unsigned slot, uint64_t at_us)
{
	enum fb3_cutoff_state state;
	uint64_t now;

	c->port.lock(c->port.ctx);
	now = audit(c);
	if (c->fault) {
		state = FB3_CUTOFF_FAULT;
	} else if (at_us <= now) {
		state = FB3_CUTOFF_EXPIRED;
	} else {
		c->slot[slot].state = FB3_CUTOFF_LIVE;
		c->slot[slot].at_us = at_us;
		service(c);
		state = c->slot[slot].state;
		if (state != FB3_CUTOFF_LIVE) {
			c->slot[slot].state = FB3_CUTOFF_IDLE;
		}
	}
	c->port.unlock(c->port.ctx);
	return state;
}

void fb3_cutoff_cancel(struct fb3_cutoff *c, unsigned slot)
{
	c->port.lock(c->port.ctx);
	audit(c);
	c->slot[slot].state = FB3_CUTOFF_IDLE;
	service(c);
	c->port.unlock(c->port.ctx);
}

enum fb3_cutoff_state fb3_cutoff_query(struct fb3_cutoff *c, unsigned slot)
{
	enum fb3_cutoff_state state;
	uint64_t now;

	c->port.lock(c->port.ctx);
	now = audit(c);
	state = c->slot[slot].state;
	/* 기한은 지났는데 alarm 인터럽트가 아직 처리하지 않았다. 답은 만료로 하고 처리는 인터럽트에 맡긴다 */
	if (state == FB3_CUTOFF_LIVE && c->slot[slot].at_us <= now) {
		state = FB3_CUTOFF_EXPIRED;
	}
	c->port.unlock(c->port.ctx);
	return state;
}

enum fb3_cutoff_state fb3_cutoff_engage(struct fb3_cutoff *c, uint64_t at_us, uint64_t margin_us,
					bool (*apply)(void *arg), void *arg)
{
	struct fb3_cutoff_slot *slot = &c->slot[FB3_CUTOFF_OUTPUT];
	enum fb3_cutoff_state state;
	uint64_t now;

	c->port.lock(c->port.ctx);
	now = audit(c);
	if (c->fault) {
		state = FB3_CUTOFF_FAULT;
	} else if (at_us <= now || at_us - now <= margin_us) {
		state = FB3_CUTOFF_EXPIRED;
	} else {
		slot->state = FB3_CUTOFF_LIVE;
		slot->at_us = at_us;
		service(c);
		state = slot->state;
		if (state == FB3_CUTOFF_LIVE && !apply(arg)) {
			c->port.cut(c->port.ctx);
			state = FB3_CUTOFF_FAULT;
		}
		if (state != FB3_CUTOFF_LIVE) {
			slot->state = FB3_CUTOFF_IDLE;
			service(c);
		}
	}
	c->port.unlock(c->port.ctx);
	return state;
}

void fb3_cutoff_cut(struct fb3_cutoff *c)
{
	c->port.lock(c->port.ctx);
	c->port.cut(c->port.ctx);
	audit(c);
	c->slot[FB3_CUTOFF_OUTPUT].state = FB3_CUTOFF_IDLE;
	service(c);
	c->port.unlock(c->port.ctx);
}

bool fb3_cutoff_faulted(struct fb3_cutoff *c)
{
	bool fault;

	c->port.lock(c->port.ctx);
	audit(c);
	fault = c->fault;
	c->port.unlock(c->port.ctx);
	return fault;
}

void fb3_cutoff_on_alarm(struct fb3_cutoff *c)
{
	c->port.lock(c->port.ctx);
	service(c);
	c->port.unlock(c->port.ctx);
}
