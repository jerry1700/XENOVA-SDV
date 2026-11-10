/* SPDX-License-Identifier: Apache-2.0 */
/* route records and preparation are not current motor authority. */
#ifndef XENOVA_DRIVE_ROUTE_H
#define XENOVA_DRIVE_ROUTE_H
#include "world.h"
#define DW_ROUTE_VIEW 16u
#define DW_ROUTE_STATE 64u
#define DW_MAPPER_ROUTE 2u
#define DW_ROUTE_NO_EDGE UINT32_MAX
enum dw_route_connection {DW_ROUTE_NEXT=1,DW_ROUTE_LANE_CHANGE=2,DW_ROUTE_SLOT_AISLE=3};
enum dw_route_purpose { DW_ROUTE_EXIT_FOLLOW=1,DW_ROUTE_RETURN_START=2 };
enum dw_route_space { DW_ROUTE_ROAD_SPACE=1,DW_ROUTE_EXIT_SPACE=2 };
enum dw_route_phase { DW_ROUTE_EXIT=1,DW_ROUTE_MERGE_STOP=2,DW_ROUTE_FOLLOW=3,DW_ROUTE_FINAL_STOP=4,DW_ROUTE_COMPLETE=5,DW_ROUTE_HOLD=6 };
enum dw_route_action { DW_ROUTE_BEGIN=1,DW_ROUTE_UPDATE=2,DW_ROUTE_FINISH=3 };
/* Structure and exact source/body fingerprint only; no clock/registry lookup or permission. */
enum dw_result dw_route_validate(const void *,const pb_msgdesc_t *);
enum dw_result dw_route_decode(const uint8_t *,size_t,const pb_msgdesc_t *,void *);
enum dw_result dw_route_state_validate(const dw_state *);
enum dw_result dw_route_world_validate(const dw_world *);
enum dw_result dw_route_clock_validate(const xenova_drive_ClockReply *);
enum dw_result dw_route_mission_validate(const xenova_drive_MissionExecution *);
enum dw_result dw_route_grant_validate(const xenova_drive_MissionGrant *);
/* Caller owns bounded decoded workspace. Original wire extraction never reencodes. */
bool dw_route_body(const uint8_t *,size_t,unsigned field,const uint8_t **,size_t *);
bool dw_route_record_hash(uint32_t,const void *,uint8_t out[32]);
bool dw_route_source_key_hash(const xenova_drive_EpisodeSourceContext *,uint8_t out[32]);
struct dw_route_path_ref {
 uint8_t global_plan_sha256[32],geometry_scope_sha256[32],basis_window_sha256[32],exit_space_sha256[32];
 uint32_t task_space_kind;uint64_t basis_revision;const dw_point *basis_prefix;size_t basis_prefix_count;
};
bool dw_route_local_path_hash(const dw_state *,const struct dw_route_path_ref *,uint8_t out[32]);
bool dw_route_query_hash(const xenova_drive_RouteQuery *,uint8_t out[32]);
bool dw_route_candidate_hash(const xenova_drive_RouteCandidate *,uint8_t out[32]);
bool dw_route_core_hash(const xenova_drive_RouteCore *,uint8_t out[32]);
bool dw_route_owner_hash(const xenova_drive_RouteOwnerContext *,uint8_t out[32]);
bool dw_route_witness_hash(const xenova_drive_RouteCurrentWitness *,uint8_t out[32]);
bool dw_route_receipt_hash(const xenova_drive_RouteReceipt *,uint8_t out[32]);
bool dw_route_eligibility_hash(const xenova_drive_RouteEligibility *,uint8_t out[32]);
bool dw_route_permission_hash(const xenova_drive_MissionGrant *,uint8_t out[32]);
bool dw_route_stop_facts_hash(const xenova_drive_DriveControlFacts *,uint8_t out[32]);
bool dw_route_transition_request_hash(const xenova_drive_WindowTransitionRequest *,uint8_t out[32]);
bool dw_route_transition_receipt_hash(const xenova_drive_WindowTransitionReceipt *,uint8_t out[32]);
bool dw_route_data_request_hash(const xenova_drive_RoadDataRequest *,uint8_t out[32]);
bool dw_route_registration_request_hash(const xenova_drive_RoadRegistrationRequest *,uint8_t out[32]);
bool dw_route_world_witness_hash(const xenova_drive_MapperWorldWitness *,uint8_t out[32]);
#endif
