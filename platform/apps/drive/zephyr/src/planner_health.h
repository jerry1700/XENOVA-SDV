/* SPDX-License-Identifier: Apache-2.0 */
/* Planner-owner work evidence. Tracker callbacks and supervision readers never
 * renew it. In-flight work uses its original search/apply deadline; completed
 * work has the existing bounded supervision window. Neither is command E. */
#ifndef XENOVA_DRIVE_PLANNER_HEALTH_H
#define XENOVA_DRIVE_PLANNER_HEALTH_H
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

/* Existing completed-work supervision window in main.c, not a search budget. */
#define DRIVE_PLANNER_OBSERVE_US UINT64_C(300000)
struct drive_planner_health {
 atomic_ullong valid_until_us;
 bool pending; /* sole planner owner; consumers read only the atomic snapshot */
};
static inline uint64_t drive_planner_observe_until(uint64_t original){
 return original>UINT64_MAX-DRIVE_PLANNER_OBSERVE_US?UINT64_MAX:original+DRIVE_PLANNER_OBSERVE_US;
}
static inline void drive_planner_health_complete(struct drive_planner_health *h,uint64_t now){
 h->pending=false;
 atomic_store(&h->valid_until_us,drive_planner_observe_until(now));
}
static inline void drive_planner_health_begin(struct drive_planner_health *h,uint64_t original_deadline){
 if(h->pending)return; /* repeated BUSY is still the first unresolved attempt */
 h->pending=true;
 atomic_store(&h->valid_until_us,original_deadline);
}
static inline void drive_planner_health_admitted(struct drive_planner_health *h,uint64_t original_deadline){
 /* Called once on actual admission, never at a later search slice. */
 h->pending=true;
 atomic_store(&h->valid_until_us,original_deadline);
}
static inline bool drive_planner_health_current(const struct drive_planner_health *h,uint64_t now){
 uint64_t until=atomic_load(&h->valid_until_us);
 return until&&now<until;
}
#endif
