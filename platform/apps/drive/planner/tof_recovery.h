/* SPDX-License-Identifier: Apache-2.0 */
/* AUTO 근거리 제동 후 후진·차로 변경 계획. 정지 증거와 출력 권한은 호출자가 소유한다. */
#ifndef XENOVA_TOF_RECOVERY_H
#define XENOVA_TOF_RECOVERY_H
#include "lattice.h"

struct lt_limits {
 float max_retreat_m, reverse_speed_mps, forward_speed_mps, front_release_m, stop_m, sensor_half_angle_rad;
};

struct lt_recovery {
 struct lp_config reverse_config, forward_config;
 struct lp_path reverse, forward;
 struct lp_tracking_geometry reverse_geometry, forward_geometry;
 struct lp_metrics metrics;
 dw_point front_hit;
 float retreat_m,stop_m,sensor_half_angle_rad,forward_speed_mps;
 bool forward_ready;
};

/* 원 제동 이후 받은 지도는 전방 장애물과 관측된 자유 공간을 포함해야 한다.
 * front_hit은 제동 관측의 원 월드 좌표다. rear_range_m은 현재 뒤 센서의 검증된 거리 하한이다.
 * 해당 범위의 자유 공간은 지도에서 별도로 확인한다. 계획은 추진을 허가하지 않는다. */
enum lp_result lt_plan(struct lt_recovery *,struct lp_search *,const dw_world *,dw_pose,
 const dw_vehicle *,dw_point front_hit,bool stopped,bool rear_clear,float rear_range_m,const struct lp_config *,
 const struct lt_limits *,struct lp_budget);

/* 완료 후 전진 경로를 현재 자세와 최신 지도에서 다시 계산한다.
 * 기존 후진 경로를 추종하는 호출과 동시에 실행하지 않는다. */
enum lp_result lt_resume(struct lt_recovery *,struct lp_search *,const dw_world *,dw_pose,
 const dw_vehicle *,bool stopped,bool front_clear,const struct lp_config *,struct lp_budget);

/* direction_clear is a fresh permit for the requested direction. On loss,
 * return a zero command even if the current map still accepts the path. */
enum lp_result lt_track(const struct lt_recovery *,struct lp_tracker *,const dw_world *,dw_pose,
 const dw_vehicle *,bool forward,bool direction_clear,float dt,struct lp_command *,struct lp_metrics *,const struct lp_budget *);

#endif
