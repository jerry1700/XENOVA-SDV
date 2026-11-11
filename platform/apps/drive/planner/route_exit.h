/* SPDX-License-Identifier: Apache-2.0 */
/* Prepare-only selected slot + observed connector + aisle. Permission, actual
 * State/apply and output are separate; records cannot manufacture FREE cells. */
#ifndef XENOVA_ROUTE_EXIT_H
#define XENOVA_ROUTE_EXIT_H
#include "route_reference.h"
struct dre_records {const dw_world *world;const xenova_drive_ExitSpace *space;const xenova_drive_RoadSection *section;
 const xenova_drive_RoadConnection *connections[2];unsigned connection_count;
 const uint8_t *parking_attempt_id,*parking_goal_sha256;};
struct dre_input {struct dre_records records;const xenova_drive_RouteQuery *query;
 const xenova_drive_RouteCurrentWitness *witness;const dw_vehicle *vehicle;dw_pose current_map_pose;
 const uint8_t *world_raw_sha256,*vehicle_raw_sha256;uint64_t now_pi_us;};
/* Static geometry/current World binding only. Admission is not implied. */
enum drp_result dre_space(const struct dre_records *,const struct lp_config *,struct lp_space *);
/* BEGIN preparation additionally uses the authenticated caller's original
 * Query/current source/Scene. No future kind3 grant/path/APPLIED is required. */
enum drp_result dre_prepare(const struct dre_input *,const struct lp_config *,struct lp_space *);
bool dre_inactive(const xenova_drive_EpisodeSnapshot *,const xenova_drive_EpisodeStatus *,uint64_t now_pi_us);
bool dre_contains(const struct tr_view *,const struct lp_space *,dw_pose,float extra,const struct lp_config *);
#endif
