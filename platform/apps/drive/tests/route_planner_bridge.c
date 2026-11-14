/* SPDX-License-Identifier: Apache-2.0 */
/* Native semantic fixture only. Raw typed geometry is decoded, never a fake
 * transport/clock/grant/READY. Production helper/search output is returned. */
#include "route_exit.h"
#include "route_progress.h"
#include "pb_decode.h"
#include "pb_encode.h"
#include <string.h>
#include <time.h>
extern const struct lp_config lp_selected_config;
extern const struct lp_assets lp_selected_assets;
static struct {
 dw_world world;dw_vehicle vehicle;xenova_drive_StartGoal goal;xenova_drive_RoadTopology topology;
 xenova_drive_RoadSection section;xenova_drive_RoadConnection connections[2];xenova_drive_RoadWindow windows[2];
 xenova_drive_ExitSpace exit_space;xenova_drive_RouteCurrentWitness witness;xenova_drive_RouteQuery query;
 struct drp_workspace work;xenova_drive_RoutePlan plan;struct lp_search search;struct lp_path path;
 struct lp_space space;struct lp_metrics metrics;struct tr_basis basis;uint64_t clock;bool cancelled;
 xenova_drive_RouteSnapshot route;xenova_drive_MissionGrant grant;xenova_drive_DriveControlFacts facts;dw_state state;
 struct drg_owner owner;struct lp_tracker tracker;struct lp_command command;
} fixture;
static int fixed_clock(void *unused,uint64_t *out){(void)unused;*out=fixture.clock;return 0;}
static int real_clock(void *unused,uint64_t *out){
 (void)unused;struct timespec now;if(clock_gettime(CLOCK_MONOTONIC,&now))return -1;
 *out=(uint64_t)now.tv_sec*1000000+(uint64_t)now.tv_nsec/1000;return 0;
}
static bool cancelled(void *unused){(void)unused;return fixture.cancelled;}
void route_probe_reset(void){memset(&fixture,0,sizeof(fixture));fixture.clock=1000000;}
int route_probe_record(unsigned kind,unsigned index,const uint8_t *raw,size_t n){
 void *out=NULL;const pb_msgdesc_t *desc=NULL;
 switch(kind){
 case 0:out=&fixture.world;desc=xenova_drive_WorldSnapshot_fields;break;
 case 1:out=&fixture.goal;desc=xenova_drive_StartGoal_fields;break;
 case 2:out=&fixture.topology;desc=xenova_drive_RoadTopology_fields;break;
 case 3:out=&fixture.section;desc=xenova_drive_RoadSection_fields;break;
 case 4:if(index>=2)return -1;out=&fixture.connections[index];desc=xenova_drive_RoadConnection_fields;break;
 case 5:if(index>=2)return -1;out=&fixture.windows[index];desc=xenova_drive_RoadWindow_fields;break;
 case 6:out=&fixture.exit_space;desc=xenova_drive_ExitSpace_fields;break;
 case 7:out=&fixture.plan;desc=xenova_drive_RoutePlan_fields;break;
 case 9:out=&fixture.query;desc=xenova_drive_RouteQuery_fields;break;
 case 10:out=&fixture.witness;desc=xenova_drive_RouteCurrentWitness_fields;break;
 case 11:out=&fixture.vehicle;desc=xenova_drive_VehicleSample_fields;break;
 case 12:out=&fixture.route;desc=xenova_drive_RouteSnapshot_fields;break;
 case 13:out=&fixture.grant;desc=xenova_drive_MissionGrant_fields;break;
 case 14:out=&fixture.facts;desc=xenova_drive_DriveControlFacts_fields;break;
 case 15:out=&fixture.state;desc=xenova_drive_DriveState_fields;break;
 default:return -1;
 }
 /* World/Vehicle are declared mathematical leaves here. Actual input-owner
  * authentication, exact raw-source/P4 admission belongs to the app bridge. */
 pb_istream_t in=pb_istream_from_buffer(raw,n);
 return pb_decode(&in,desc,out)?0:-1;
}
int route_probe_build(unsigned purpose,unsigned section,unsigned lane,float low,float high,int use_exit,unsigned fault,uint64_t work){
 struct drp_request request={.topology=&fixture.topology,.goal=purpose==2?&fixture.goal:NULL,
  .exit_space=use_exit?&fixture.exit_space:NULL,.witness=&fixture.witness,.start={section,lane,low,high},
  .revision=1,.purpose=purpose,.merge_pose={low,2,0}};
 memcpy(request.route_id,fixture.query.query_id.bytes,16);memcpy(request.terminal_profile_sha256,fixture.goal.terminal_profile_sha256.bytes,32);
 fixture.clock=1000000;fixture.cancelled=fault==2;
 struct lp_budget budget={fault==1?1000000:2000000,fixed_clock,cancelled,NULL,NULL};
 return drp_build(&fixture.work,&request,&budget,work,&fixture.plan);
}
size_t route_probe_plan(uint8_t *out,size_t cap){pb_ostream_t stream=pb_ostream_from_buffer(out,cap);
 return pb_encode(&stream,xenova_drive_RoutePlan_fields,&fixture.plan)?stream.bytes_written:0;}
int route_probe_open(unsigned occurrence){struct drr_view view;
 return drr_open(&view,&fixture.world,&fixture.windows[0],&fixture.topology,&fixture.plan,occurrence,&lp_selected_config);}
int route_probe_window(unsigned seam){return seam?drr_seam(&fixture.windows[0],&fixture.windows[1]):drr_append(&fixture.windows[0],&fixture.windows[1]);}
static struct dre_records records(void){return (struct dre_records){&fixture.world,&fixture.exit_space,&fixture.section,
 {&fixture.connections[0],fixture.exit_space.connection_sha256_count>1?&fixture.connections[1]:NULL},fixture.exit_space.connection_sha256_count,
 fixture.exit_space.parking_attempt_id.bytes,fixture.exit_space.parking_goal_sha256.bytes};}
static struct dre_input input(uint64_t now,dw_pose pose){return (struct dre_input){.records=records(),.query=&fixture.query,.witness=&fixture.witness,
 .vehicle=&fixture.vehicle,.current_map_pose=pose,.world_raw_sha256=fixture.witness.world_raw_sha256.bytes,
 .vehicle_raw_sha256=fixture.witness.vehicle_raw_sha256.bytes,.now_pi_us=now};}
int route_probe_exit_space(void){struct dre_records r=records();return dre_space(&r,&lp_selected_config,&fixture.space);}
int route_probe_exit_prepare(uint64_t now){struct dre_input in=input(now,fixture.vehicle.odom_pose);return dre_prepare(&in,&lp_selected_config,&fixture.space);}
int route_probe_exit_contains(float x,float y,float yaw){
 if(route_probe_exit_space())return -1;
 uint32_t lane=fixture.exit_space.aisle_lane_id;struct tr_intent task={true,false,lane,lane,lane,0};struct tr_view view;
 return tr_open(&fixture.world,&task,&view)?dre_contains(&view,&fixture.space,(dw_pose){x,y,yaw},0,&lp_selected_config):-1;
}
int route_probe_exit_search(float x,float y,float yaw,float gx,float gy,float gyaw){
 uint64_t now=0;if(real_clock(NULL,&now))return LP_CLOCK;
 dw_pose pose={x,y,yaw};struct dre_input in=input(1020000,pose);fixture.cancelled=false;
 struct lp_goal_set goals={.count=1,.poses={{gx,gy,gyaw}},.distance=.12f,.heading=.22f,.final_gear=1};
 uint32_t lane=fixture.exit_space.aisle_lane_id;struct tr_intent task={true,false,lane,lane,lane,.01f};
 if(!tr_basis_accept(&fixture.basis,&fixture.world))return LP_INPUT;
 struct lp_budget budget={now+10000000,real_clock,cancelled,NULL,NULL};
 enum lp_result rc=lp_start_exit(&fixture.search,&fixture.world,pose,0,&goals,1,2,&in,&task,&fixture.basis,&lp_selected_config,&lp_selected_assets,budget);
 for(unsigned i=0;rc==LP_SEARCHING&&i<10000;i++)rc=lp_advance(&fixture.search,32,&fixture.path);
 return rc;
}
int route_probe_exit_valid(void){uint64_t now=0;if(real_clock(NULL,&now))return LP_CLOCK;
 struct lp_budget budget={now+10000000,real_clock,cancelled,NULL,NULL};
 return lp_validate_path(&fixture.path,&fixture.world,&lp_selected_config,&fixture.metrics,&budget);}
int route_probe_exit_hash(uint8_t out[32]){return drr_path_hash(&fixture.path,&fixture.world,&fixture.windows[0],&fixture.plan,&fixture.state,out);}
int route_probe_exit_legacy_export(void){return lp_state_export(&fixture.path,&fixture.state);}
unsigned route_probe_path_count(void){return (unsigned)fixture.path.count;}
int route_probe_path_point(unsigned n,float out[5]){if(n>=fixture.path.count)return -1;
 const xenova_drive_PathPoint *p=&fixture.path.points[n];out[0]=p->pose.x;out[1]=p->pose.y;out[2]=p->pose.yaw;out[3]=p->steering_rad;out[4]=p->velocity_mps;return 0;}
int route_probe_sync(void){return drg_sync(&fixture.owner,&fixture.route);}
int route_probe_meter(uint64_t now,uint64_t *ticks,double *metres){return drg_meter(&fixture.route.core,&fixture.facts,&fixture.vehicle,now,ticks,metres);}
/* Declared tracker correspondence for arithmetic unit cases only. The actual
 * signed search and app bridge separately own tracker/runtime provenance. */
int route_probe_progress_math(uint64_t now){
 struct lp_tracker tracker={.initialized=true,.last_pi_us=fixture.vehicle.pi_sample_us};struct lp_path path={.count=2};
 unsigned i=fixture.route.progress.occurrence_index;if(i>=fixture.plan.occurrences_count)return DRP_INPUT;
 const xenova_drive_RouteOccurrence *o=&fixture.plan.occurrences[i];
 struct drr_view view;int opened=drr_open(&view,&fixture.world,&fixture.windows[0],&fixture.topology,&fixture.plan,i,&lp_selected_config);
 if(opened)return opened;
 struct drg_sample sample={.now_pi_us=now,.calculation_sequence=fixture.route.progress.calculation_sequence+1,
  .remaining_upper_m=fixture.route.progress.remaining_upper_m,.map_pose=fixture.vehicle.odom_pose,.section_index=o->section_index,
  .view=&view,.section=&fixture.section,
  .vehicle=&fixture.vehicle,.facts=&fixture.facts,.path=&path,.tracker=&tracker};
 struct drg_limits limits={2000000,.02f,.02f,.03f};xenova_drive_RouteProgress out;
 int rc=drg_update(&fixture.owner,&fixture.route,&fixture.plan,&fixture.topology,&sample,&limits,&out);
 if(!rc)fixture.route.progress=out;
 return rc;
}
uint64_t route_probe_last_progress(void){return fixture.route.progress.last_progress_pi_us;}
uint64_t route_probe_used_ticks(void){return fixture.route.progress.used_travel_ticks;}
uint64_t route_probe_tracking_sequence(void){return fixture.tracker.completed;}
/* The declared plant uses the compiled profile's bounds, not a second set of
 * fixture braking/tolerance constants. This is observation, not STOP evidence. */
void route_probe_goal_profile(float out[5]){
 out[0]=lp_selected_config.wheelbase;out[1]=lp_selected_config.max_accel;
 out[2]=lp_selected_config.max_brake;out[3]=lp_selected_config.goal_distance;out[4]=lp_selected_config.goal_heading;
}
int route_probe_goal_plan(float x,float y,float yaw){
 uint64_t now;if(real_clock(NULL,&now))return LP_CLOCK;
 unsigned occurrence=fixture.route.progress.occurrence_index;struct drr_view view;
 if(drr_open(&view,&fixture.world,&fixture.windows[0],&fixture.topology,&fixture.plan,occurrence,&lp_selected_config))return LP_INPUT;
 if(!tr_basis_accept(&fixture.basis,&fixture.world))return LP_INPUT;
 struct lp_budget budget={now+10000000,real_clock,cancelled,NULL,NULL};
 int rc=lp_start_task_goal(&fixture.search,&fixture.world,(dw_pose){x,y,yaw},0,fixture.goal.rear_axle_pose,
  &lp_selected_config,&lp_selected_assets,budget,&view.intent,&fixture.basis);
 for(unsigned i=0;rc==LP_SEARCHING&&i<10000;i++)rc=lp_advance(&fixture.search,32,&fixture.path);
 if(rc==LP_PATH_FOUND){
  fixture.path.execution.present=true;fixture.path.execution.mission=fixture.state.mission;
  fixture.path.initial_pi_us=fixture.vehicle.pi_sample_us;memset(&fixture.tracker,0,sizeof(fixture.tracker));
 }
 return rc;
}
int route_probe_goal_track(float x,float y,float yaw,float velocity,float steering,uint64_t pi_us,float out[2]){
 fixture.vehicle.sequence++;fixture.vehicle.source_sample++;fixture.vehicle.source_us=pi_us;fixture.vehicle.pi_sample_us=pi_us;
 fixture.vehicle.odom_pose=(dw_pose){x,y,yaw};
 fixture.vehicle.velocity_mps=velocity;fixture.vehicle.estimated_steering_rad=steering;fixture.vehicle.has_estimated_steering_rad=true;
 uint64_t now;if(real_clock(NULL,&now))return LP_CLOCK;struct lp_budget budget={now+1000000,real_clock,cancelled,NULL,NULL};
 int rc=lp_track(&fixture.tracker,&fixture.path,&fixture.world,(dw_pose){x,y,yaw},&fixture.vehicle,&lp_selected_config,.02f,
  &fixture.command,&fixture.metrics,&budget);out[0]=fixture.command.velocity;out[1]=fixture.command.steer;return rc;
}
int route_probe_terminal(uint64_t now){
 struct drr_view view;int rc=drr_open(&view,&fixture.world,&fixture.windows[0],&fixture.topology,&fixture.plan,
  fixture.route.progress.occurrence_index,&lp_selected_config);if(rc)return rc;
 struct drg_sample sample={.now_pi_us=now,.calculation_sequence=fixture.route.progress.calculation_sequence+1,
  .vehicle=&fixture.vehicle,.facts=&fixture.facts,.path=&fixture.path,.tracker=&fixture.tracker};
 struct drg_limits limits={2000000,.02f,.02f,.03f};uint64_t guest;if(real_clock(NULL,&guest))return DRP_DEADLINE;
 struct lp_budget budget={guest+1000000,real_clock,cancelled,NULL,NULL};
 return drg_terminal(&fixture.route,&fixture.plan,&fixture.goal,&fixture.grant,&view,fixture.vehicle.odom_pose,&sample,
  &fixture.command,&lp_selected_config,&limits,&fixture.metrics,&budget);
}
size_t route_probe_vehicle(uint8_t *out,size_t cap){pb_ostream_t stream=pb_ostream_from_buffer(out,cap);
 return pb_encode(&stream,xenova_drive_VehicleSample_fields,&fixture.vehicle)?stream.bytes_written:0;}
