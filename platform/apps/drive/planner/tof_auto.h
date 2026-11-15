/* SPDX-License-Identifier: Apache-2.0 */
/* AUTO-only recovery session. Caller serializes the tracker and planner owners. */
#ifndef XENOVA_TOF_AUTO_H
#define XENOVA_TOF_AUTO_H
#include "tof_recovery.h"

/* drive-command-3 segment/cusp indices reserved for temporary ToF recovery. */
enum lt_auto_segment { LT_STOP_REVERSE=16, LT_DRIVE_REVERSE=17, LT_DRIVE_FORWARD=18 };
enum lt_auto_stage { LT_AUTO_IDLE, LT_AUTO_BRAKE, LT_AUTO_REVERSE, LT_AUTO_ALIGN,
 LT_AUTO_FORWARD, LT_AUTO_HANDOFF, LT_AUTO_RETURN, LT_AUTO_HELD };
struct lt_auto {
 struct lt_recovery plan;
 struct lp_tracker tracker;
 enum lt_auto_stage stage;
 uint64_t plan_revision,phase_revision,stop_sequence,gate_revision,began_pi_us,expires_pi_us;
 uint64_t attempted_stop_sequence;
 uint32_t attempted_map_generation;
 uint64_t grant_revision,mission_revision;
 uint32_t generation;
 uint8_t mission[16];
 char device_session[37],pi_session[37],odom_epoch[37];
 float front_range_m;
 dw_pose hit_odom;
 bool hit_valid;
};

bool lt_auto_same(const struct lt_auto *,const xenova_drive_MissionGrant *);
void lt_auto_begin(struct lt_auto *,const xenova_drive_MissionGrant *,const struct lp_path *,uint64_t now,uint64_t expires_pi_us);
/* Use only an authenticated, fresh ClockReply with the original Vehicle. */
void lt_auto_observe(struct lt_auto *,const xenova_drive_DriveControlFacts *,const dw_vehicle *hit_sample);
bool lt_auto_stop(const struct lt_auto *,const xenova_drive_DriveControlFacts *,
 const xenova_drive_DirectionGateWitness *,uint64_t now,unsigned segment,int direction);
bool lt_auto_ready(const struct lt_auto *,const xenova_drive_DirectionGateWitness *,uint64_t now,unsigned segment,int direction);
enum lp_result lt_auto_plan(struct lt_auto *,struct lp_search *,const dw_world *,dw_pose,const dw_vehicle *,
 const xenova_drive_DriveControlFacts *,const xenova_drive_DirectionGateWitness *,uint64_t now,
 const struct lp_config *,const struct lt_limits *,struct lp_budget);
enum lp_result lt_auto_track(struct lt_auto *,const dw_world *,dw_pose,const dw_vehicle *,
 const xenova_drive_DriveControlFacts *,uint64_t now,struct lp_command *,const struct lp_budget *);
#endif
