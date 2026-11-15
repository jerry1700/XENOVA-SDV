/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XENOVA_AUTO_TX_BLOCK_H
#define XENOVA_AUTO_TX_BLOCK_H

#include <stdbool.h>
#include <stdint.h>

/* All calls run on the MQTT I/O owner. Use its monotonic time in ms. */
int drive_auto_tx_block_start(uint32_t duration_s, int64_t now);
bool drive_auto_tx_block_active(int64_t now);
bool drive_auto_tx_block_drop(int64_t now);
uint64_t drive_auto_tx_block_dropped(void);

#endif
