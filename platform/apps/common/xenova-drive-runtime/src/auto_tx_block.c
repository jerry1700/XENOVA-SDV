/* SPDX-License-Identifier: Apache-2.0 */
/* Bounded send-side fault on the MQTT owner, shared by dispatch and result polling. */
#include <errno.h>
#include <xenova/auto_tx_block.h>

static int64_t expires_at_ms;
static uint64_t dropped;

int drive_auto_tx_block_start(uint32_t duration_s, int64_t now)
{
	if (duration_s < 1 || duration_s > 120) {
		return -ERANGE;
	}
	if (drive_auto_tx_block_active(now)) {
		return -EBUSY;
	}
	expires_at_ms = now + (int64_t)duration_s * 1000;
	dropped = 0;
	return 0;
}

bool drive_auto_tx_block_active(int64_t now)
{
	return now < expires_at_ms;
}

bool drive_auto_tx_block_drop(int64_t now)
{
	if (!drive_auto_tx_block_active(now)) {
		return false;
	}
	dropped++;
	return true;
}

uint64_t drive_auto_tx_block_dropped(void)
{
	return dropped;
}
