/* SPDX-License-Identifier: Apache-2.0 */
/* Observed directed topology search. A plan is geometry, never permission.
 * Caller owns this workspace and all immutable records; no heap or global map. */
#ifndef XENOVA_ROUTE_PLAN_H
#define XENOVA_ROUTE_PLAN_H
#include "lattice.h"
#include <stdint.h>
#define DRP_STATES 193u
#define DRP_NO_EDGE UINT32_MAX
enum drp_result { DRP_OK,DRP_INPUT,DRP_UNREACHABLE,DRP_WORK,DRP_DEADLINE,
 DRP_CANCELLED,DRP_WINDOW,DRP_CONTEXT,DRP_PROGRESS,DRP_EXPIRED,DRP_STOP_REQUIRED };
struct drp_location {uint32_t section,lane;float lower,upper;};
struct drp_request {
 const xenova_drive_RoadTopology *topology;
 const xenova_drive_StartGoal *goal; /* NULL only for EXIT_FOLLOW. */
 const xenova_drive_ExitSpace *exit_space;
 const xenova_drive_RouteCurrentWitness *witness;
 struct drp_location start;
 uint8_t route_id[16];uint64_t revision;uint32_t purpose;
 dw_pose merge_pose;uint8_t terminal_profile_sha256[32];
};
struct drp_node {double cost;int16_t parent;uint8_t closed,laps;uint32_t edge;};
struct drp_workspace {struct drp_node nodes[DRP_STATES];uint16_t chain[96];uint64_t work;};
/* edge_index is outgoing; only the last occurrence uses DRP_NO_EDGE. Costs
 * rank observed routes, not absolute travel/remaining budgets. Those use Pi's
 * accepted-encoder meter. A goal behind is reached only by observed edges. */
enum drp_result drp_build(struct drp_workspace *,const struct drp_request *,
 const struct lp_budget *,uint64_t work_limit,xenova_drive_RoutePlan *);
enum drp_result drp_plan_validate(const xenova_drive_RoutePlan *,const xenova_drive_RoadTopology *);
const char *drp_reason(enum drp_result);
#endif
