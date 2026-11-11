/* SPDX-License-Identifier: Apache-2.0 */
#ifndef XENOVA_DRIVE_AUTO_DELAY_H
#define XENOVA_DRIVE_AUTO_DELAY_H
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#define DRIVE_AUTO_DELAY_MAX_MS 100U
#define DRIVE_AUTO_DELAY_MAX_DURATION_S 120U
#define DRIVE_AUTO_DEADLINE_MISS_LIMIT 5U
struct drive_auto_delay_status {
 uint32_t delay_ms,consecutive_deadline_misses;int64_t expires_at_ms;bool active,unhealthy;
};
/* 주입이 실제 채택된 한 시각과 원 종료값이다. 실패 시 out은 바꾸지 않는다. */
struct drive_auto_delay_receipt {
 int64_t started_ms,expires_at_ms;
};
#if defined(CONFIG_XENOVA_AUTO_DELAY_INJECTION)
int drive_auto_delay_start(uint32_t,uint32_t);
int drive_auto_delay_start_with_receipt(uint32_t,uint32_t,struct drive_auto_delay_receipt *);
void drive_auto_delay_stop(void);
bool drive_auto_delay_active(void);
void drive_auto_delay_apply(void);
bool drive_auto_delay_observe_deadline(bool);
void drive_auto_delay_status_get(struct drive_auto_delay_status *);
#else
static inline int drive_auto_delay_start(uint32_t a,uint32_t b){(void)a;(void)b;return -ENOTSUP;}
static inline int drive_auto_delay_start_with_receipt(uint32_t a,uint32_t b,struct drive_auto_delay_receipt *out){(void)a;(void)b;(void)out;return -ENOTSUP;}
static inline void drive_auto_delay_stop(void){}
static inline bool drive_auto_delay_active(void){return false;}
static inline void drive_auto_delay_apply(void){}
static inline bool drive_auto_delay_observe_deadline(bool a){(void)a;return false;}
static inline void drive_auto_delay_status_get(struct drive_auto_delay_status *o){if(o)*o=(struct drive_auto_delay_status){0};}
#endif
#endif
