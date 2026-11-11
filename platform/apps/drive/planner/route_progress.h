/* SPDX-License-Identifier: Apache-2.0 */
/* Original Pi budgets and measured occurrence progress. Neither a restored
 * owner nor a local endpoint creates a new budget, terminal or permission. */
#ifndef XENOVA_ROUTE_PROGRESS_H
#define XENOVA_ROUTE_PROGRESS_H
#include "route_exit.h"
struct drg_limits {uint64_t progress_us;float progress_m,stationary_speed,yaw_rate;};
struct drg_owner {bool bound;xenova_drive_RouteCore original;uint64_t phase_revision,phase_started,phase_end,
 anchor,wait_baseline;uint32_t occurrence_anchor;float station_anchor,remaining_anchor;bool path_metric_pending;};
struct drg_sample {uint64_t now_pi_us,calculation_sequence;float remaining_upper_m;dw_pose map_pose;
 const struct drr_view *view;const xenova_drive_RoadSection *section;unsigned section_index;
 const dw_vehicle *vehicle;const xenova_drive_DriveControlFacts *facts;const xenova_drive_RoadConnection *crossing;
 const struct lp_path *path;const struct lp_tracker *tracker;};
/* Accepted receipt sync only. A/B reconstruction initializes wait credit to0
 * at the original anchor; it never subtracts historical cumulative wait twice. */
enum drp_result drg_sync(struct drg_owner *,const xenova_drive_RouteSnapshot *);
enum drp_result drg_meter(const xenova_drive_RouteCore *,const xenova_drive_DriveControlFacts *,
 const dw_vehicle *,uint64_t now,uint64_t *used_ticks,double *travel_upper_m);
enum drp_result drg_update(struct drg_owner *,const xenova_drive_RouteSnapshot *,const xenova_drive_RoutePlan *,
 const xenova_drive_RoadTopology *,const struct drg_sample *,const struct drg_limits *,xenova_drive_RouteProgress *);
/* A local STOP/cusp/endpoint cannot complete the global StartGoal. */
enum drp_result drg_terminal(const xenova_drive_RouteSnapshot *,const xenova_drive_RoutePlan *,
 const xenova_drive_StartGoal *,const xenova_drive_MissionGrant *,const struct drr_view *,dw_pose,
 const struct drg_sample *,const struct lp_command *,const struct lp_config *,const struct drg_limits *,
 struct lp_metrics *,const struct lp_budget *);
#endif
