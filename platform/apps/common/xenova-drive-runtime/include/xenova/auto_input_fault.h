/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XENOVA_AUTO_INPUT_FAULT_H_
#define XENOVA_AUTO_INPUT_FAULT_H_
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#if defined(CONFIG_XENOVA_AUTO_INPUT_FAULT_INJECTION)
struct drive_auto_input_fault_receipt { int64_t started_ms,expires_at_ms; };
int drive_auto_input_fault_start(uint32_t duration_s,struct drive_auto_input_fault_receipt *out);
void drive_auto_input_fault_stop(void);
bool drive_auto_input_fault_active(void);
#else
struct drive_auto_input_fault_receipt { int64_t started_ms,expires_at_ms; };
static inline int drive_auto_input_fault_start(uint32_t duration_s,struct drive_auto_input_fault_receipt *out)
{ (void)duration_s;(void)out;return -ENOTSUP; }
static inline void drive_auto_input_fault_stop(void) {}
static inline bool drive_auto_input_fault_active(void) { return false; }
#endif
#endif
