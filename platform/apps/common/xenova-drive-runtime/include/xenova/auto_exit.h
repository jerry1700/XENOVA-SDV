/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XENOVA_AUTO_EXIT_H
#define XENOVA_AUTO_EXIT_H

#include <stdbool.h>

struct k_sem;

#if defined(CONFIG_XENOVA_AUTO_EXIT_INJECTION)
void drive_auto_exit_bind(struct k_sem *wake);
void drive_auto_exit_unbind(void);
bool drive_auto_exit_take(void);
void drive_auto_exit_confirm(void);
int drive_auto_exit_start(void);
/* -EINPROGRESS until the current worker's kernel thread has exited. */
int drive_auto_exit_status(void);
#else
static inline void drive_auto_exit_bind(struct k_sem *wake) { (void)wake; }
static inline void drive_auto_exit_unbind(void) {}
static inline bool drive_auto_exit_take(void) { return false; }
static inline void drive_auto_exit_confirm(void) {}
#endif

#endif
