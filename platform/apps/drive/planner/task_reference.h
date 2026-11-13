/* SPDX-License-Identifier: Apache-2.0 */
/* 원 World를 바꾸지 않는 Pi 내부 차로 목적과 열린 station 기준이다.
 * ID/관측 revision은 권위가 아니다. 원 source/시간/permission은 caller가 검사한다. */
#ifndef XENOVA_TASK_REFERENCE_H
#define XENOVA_TASK_REFERENCE_H
#include "world.h"
#define TR_BASIS_POINTS 64
#define TR_LANE_POINTS 32
struct lp_config;
struct lp_collision_index;
struct tr_intent { bool active,changing;uint32_t selected_lane_id,from_lane_id,home_lane_id;float vehicle_pad; };
struct tr_view { const dw_world *world;const xenova_drive_LaneGeometry *selected,*from;
 const dw_point *points;unsigned count;const struct lp_collision_index *index; };
struct tr_lane_edges {
 dw_point outer[TR_LANE_POINTS],outer_tape[TR_LANE_POINTS],divider_tape[TR_LANE_POINTS];
 float min_half_width;
};
struct tr_basis { bool bound;unsigned count;uint64_t revision;dw_point points[TR_BASIS_POINTS];
 char id[65],map[37],frame[37],odom[37],configuration[65],calibration[65],primitive[65],vehicle[65],
 mapper_boot[37],mapper_instance[37],clock_epoch[37]; };
bool tr_open(const dw_world *,const struct tr_intent *,struct tr_view *);
bool tr_open_indexed(const dw_world *,const struct tr_intent *,const struct lp_collision_index *,struct tr_view *);
void tr_index_lanes(struct lp_collision_index *,const dw_world *,const struct lp_config *);
/* Geometry-only reference. Crossings still require tr_open_indexed/footprint checks. */
bool tr_centerline(const dw_world *,const struct tr_intent *,const dw_point **,unsigned *);
/* 전체 차체 tube와 경계의 교차를 검사한다. CHANGE만 실제 허용 divider 구간을 넘는다. */
bool tr_contains(const struct tr_view *,dw_pose,float extra,const struct lp_config *);
/* 명목 최근접/배열 순서는 쓰지 않는다. 전체 tube가 정확히 한 차로에 있을 때만 ID다. */
uint32_t tr_unique_lane(const dw_world *,dw_pose,float extra,const struct lp_config *,const struct lp_collision_index *);
bool tr_same_intent(const struct tr_intent *,const struct tr_intent *);
bool tr_basis_accept(struct tr_basis *,const dw_world *);
bool tr_basis_compatible(const struct tr_basis *,const dw_world *);
/* 명시 선택 slot의 approach lane/관측 기하만 검사한다. slot 선택·추진·빈칸 승인은 없다. */
bool tr_parking_approach(const struct tr_view *,uint32_t slot_id);
/* Aisle, permitted adjacent-lane crossings and one selected slot. */
bool tr_parking_contains(const struct tr_view *,const dw_point slot[4],float slot_pad,
 dw_pose,float extra,const struct lp_config *);
/* Exterior edge samples depend only on the observed corridor and slot polygon.
 * A dense geometry falls back to evaluating the original edges. */
#define TR_PARKING_EDGE_SAMPLES 80
struct tr_parking_edges {dw_point points[TR_PARKING_EDGE_SAMPLES];unsigned count;bool ready;};
struct tr_parking_geometry {struct tr_view aisle,both;struct tr_parking_edges aisle_edges,both_edges;bool has_both;};
void tr_prepare_parking(struct tr_parking_geometry *,const struct tr_view *,const dw_point slot[4]);
bool tr_prepared_parking_contains(const struct tr_parking_geometry *,const dw_point slot[4],float slot_pad,
 dw_pose,float extra,const struct lp_config *);
#endif
