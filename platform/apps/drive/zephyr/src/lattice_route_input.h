/* SPDX-License-Identifier: Apache-2.0 */
/* Internal bounded route transport. Geometry/provenance is not permission.
 * The existing input owner retains raw bytes; planner callers release leases.
 * No packet/tag/clock profile is introduced by these local C interfaces. */
#ifndef XENOVA_LATTICE_ROUTE_INPUT_H
#define XENOVA_LATTICE_ROUTE_INPUT_H
#include "lattice_input.h"
#define DLR_RECORD_SLOTS 16u
#define DLR_RECORD_WIRE 12288u
struct dlr_lease {unsigned slot;uint32_t generation,token;};
struct dlr_clock_proof {struct dl_clock clock;xenova_drive_DriveControlFacts facts;dw_vehicle vehicle;uint64_t now_pi_us;};
/* Installed code capability only. This is not input READY or output permission. */
bool drive_lattice_route_supported(void);
bool dl_route_installed(void);
int dl_route_record_acquire(uint32_t kind,const uint8_t hash[32],struct dlr_lease *,
 const xenova_drive_RoadData **,const uint8_t **original,size_t *);
/* Bounded bootstrap discovery of observed records. The caller still checks
 * current scope/body/provenance; cache insertion order is not freshness. */
int dl_route_record_next(uint32_t kind,uint32_t after_generation,struct dlr_lease *,
 const xenova_drive_RoadData **,const uint8_t **original,size_t *);
int dl_route_record_release(struct dlr_lease);
int dl_route_query_get(xenova_drive_RouteQuery *,uint8_t original_sha256[32]);
int dl_route_status(xenova_drive_RouteStatus *,uint64_t now_pi_us);
/* Same original Reply's facts are promoted only by the real P4 receipt ACK.
 * Current STOP/session/E still requires a separate live recheck. */
int dl_route_clock_proof(struct dlr_clock_proof *);
/* Join exact accepted wire hashes to an actual de_input_current() result. */
int dl_route_input_hashes(const struct de_input_check *,uint8_t world_raw[32],uint8_t vehicle_raw[32]);
int dl_route_exchange_submit(const xenova_drive_RouteExchange *);
int dl_route_plan_submit(const xenova_drive_RoadData *);
int dl_route_receipt_take(xenova_drive_RouteReceipt *,uint8_t original_sha256[32]);
int dl_route_transition_take(xenova_drive_WindowTransitionReceipt *,uint8_t original_sha256[32]);
#endif
