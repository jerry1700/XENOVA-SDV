/* SPDX-License-Identifier: Apache-2.0 */
/* Exact open local windows and current occupancy remain separate from the
 * global occurrence list. These helpers grant no transition/output authority. */
#ifndef XENOVA_ROUTE_REFERENCE_H
#define XENOVA_ROUTE_REFERENCE_H
#include "route_plan.h"
struct drr_view {const dw_world *world;const xenova_drive_RoadWindow *window;struct tr_view road;struct tr_intent intent;
 const struct lp_config *config;};
enum drp_result drr_open(struct drr_view *,const dw_world *,const xenova_drive_RoadWindow *,
 const xenova_drive_RoadTopology *,const xenova_drive_RoutePlan *,unsigned occurrence,const struct lp_config *);
/* Same identity is append-only. A different window uses exact suffix/prefix
 * overlap and a distinct transition; neither operation changes route budgets. */
bool drr_append(const xenova_drive_RoadWindow *,const xenova_drive_RoadWindow *);
bool drr_seam(const xenova_drive_RoadWindow *,const xenova_drive_RoadWindow *);
/* The cursor is computed from current MAP pose on the original section's
 * paired lane/reference stations, not an externally supplied nearest index. */
enum drp_result drr_project(const struct drr_view *,const xenova_drive_RoadSection *,
 const xenova_drive_RoadTopology *,unsigned section,dw_pose,float position_pad,struct drp_location *);
/* Caller supplies persistent scratch; this is hash geometry, not a publishable
 * DriveState/NONE or an accepted EXIT State. Future grants are not required. */
bool drr_path_hash(const struct lp_path *,const dw_world *,const xenova_drive_RoadWindow *,
 const xenova_drive_RoutePlan *,dw_state *scratch,uint8_t out[32]);
/* This is the actual full local-path validation, not a point-count claim.
 * Existing UNKNOWN/age/work/deadline and body-sweep checks remain in lattice. */
enum drp_result drr_extent(const struct drr_view *,const struct lp_path *,dw_pose,const dw_vehicle *,
 const struct lp_config *,struct lp_metrics *,const struct lp_budget *,xenova_drive_RouteExtentProof *);
#endif
