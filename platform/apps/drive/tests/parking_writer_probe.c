/* Host fault injection at the parking writer's publication boundary. */
#include <assert.h>
#include "engine.c"

extern const struct lp_config lp_selected_config;
extern const struct lp_assets lp_selected_assets;
static struct drive_engine engine;
static struct dw_snapshot snapshot;
static dw_vehicle feedback;
static bool contend,source_busy;
static uint64_t checked_deadline,guest;

static int fixture_clock(void *unused,uint64_t *out){
 (void)unused;*out=guest;return 0;
}

enum dw_result __wrap_dw_acquire(struct dw_pool *pool,struct dw_handle *handle,const struct dw_snapshot **out){
 assert(pool==&engine.maps);
 *handle=(struct dw_handle){0};*out=&snapshot;return DW_OK;
}
enum dw_result __wrap_dw_source_valid(struct dw_pool *pool,struct dw_handle handle){
 assert(pool==&engine.maps&&handle.slot==0);return source_busy?DW_BUSY:DW_OK;
}
enum dw_result __wrap_dw_release(struct dw_pool *pool,struct dw_handle handle){
 assert(pool==&engine.maps&&handle.slot==0);return DW_OK;
}
enum dw_result __wrap_dw_current_pose(const dw_world *world,struct dw_odometry *odom,uint64_t now,uint64_t age,
 dw_pose *pose,dw_vehicle *vehicle){
 (void)now;(void)age;assert(world==&snapshot.world&&odom==&engine.odometry);
 *pose=feedback.odom_pose;if(vehicle)*vehicle=feedback;return DW_OK;
}
enum lp_result __wrap_lp_validate_path(const struct lp_path *path,const dw_world *world,
 const struct lp_config *config,struct lp_metrics *metrics,const struct lp_budget *budget){
 (void)config;(void)metrics;
 assert(path->count>=2&&world==&snapshot.world);
 checked_deadline=budget->deadline_us;
 if(guest>=checked_deadline)return LP_DEADLINE;
 if(contend)assert(take(&engine));
 return LP_PATH_FOUND;
}

static void writer_contention(void){
 de_init(&engine,&lp_selected_config,&lp_selected_assets);
 struct lp_path *source=&engine.parking.best_maneuver;
 source->count=2;source->points[1].pose.x=.2f;
 engine.budget.deadline_us=200;
 assert(parking_publish(&engine,source,100,true,false)==DE_OK);
 assert(engine.current==0&&engine.path_revision==1);
 engine.parking.phase=DP_ALIGN_STOP;engine.parking.resuming_plan=true;
 contend=true;
 for(unsigned attempt=0;attempt<DE_PATH_SLOTS+1;attempt++){
  enum de_result rc=attempt?parking_publish_pending(&engine,101+attempt):
   parking_publish(&engine,source,101,true,true);
  assert(rc==DE_BUSY);
  give(&engine);
  assert(engine.current==0&&engine.path_revision==1&&engine.parking.publish_source==source);
  assert(engine.parking.resuming_plan&&!engine.parking.request_ready);
  for(unsigned i=0;i<DE_PATH_SLOTS;i++)assert(!engine.paths[i].writing);
 }
 contend=false;engine.budget.deadline_us=999;
 assert(parking_publish_pending(&engine,110)==DE_OK);
 assert(checked_deadline==200&&engine.path_revision==2&&engine.current!=0);
 assert(!engine.parking.publish_source&&!engine.parking.resuming_plan);
 assert(engine.parking.published_phase==DP_ALIGN_STOP&&engine.parking.result==DP_FEASIBLE);
 assert(engine.path_exhausted==0);
 de_stop(&engine);
 assert(de_restart(&engine)==DE_OK);
}

static void parking_fixture(void){
 de_init(&engine,&lp_selected_config,&lp_selected_assets);
 de_behavior_enable(&engine,(const uint8_t[16]){1});
 engine.facts_current=true;
 engine.grant=(xenova_drive_MissionGrant){.kind=2,.strategy=1,.mission_deadline_pi_us=5000000,
  .search_deadline_pi_us=4000000,.execution_deadline_pi_us=4000000};
 engine.parking.active=true;engine.parking.phase=DP_ENTERING;engine.parking.phase_revision=1;
 engine.budget=(struct lp_budget){.deadline_us=100,.clock=fixture_clock};
 guest=1;
 snapshot.received_us=990;snapshot.valid_until_us=2000000;
 snapshot.world.width=64;snapshot.world.height=64;snapshot.world.resolution_m=.05f;
 memset(snapshot.world.cell_flags.bytes,1,sizeof(snapshot.world.cell_flags.bytes));
 feedback=(dw_vehicle){.pi_sample_us=999,.sequence=1,.quality=3,.has_measured_steering_rad=true,.has_steering_error_rad=true};
 struct lp_path *path=&engine.parking.best_maneuver;
 path->count=4;path->space.parking=true;
 path->execution.segments_count=2;
 path->execution.segments[0]=(xenova_drive_GearSegment){0,1,1};
 path->execution.segments[1]=(xenova_drive_GearSegment){2,3,-1};
 path->points[0].pose=(dw_pose){1.2f,1.2f,0};
 path->points[1].pose=(dw_pose){1.204f,1.2f,0};
 path->points[2].pose=path->points[1].pose;
 path->points[3].pose=(dw_pose){1.201f,1.2f,0};
 path->goal=path->points[3].pose;
 path->space.polygon[0]=(dw_point){.7f,.7f};
 path->space.polygon[1]=(dw_point){1.7f,.7f};
 path->space.polygon[2]=(dw_point){1.7f,1.7f};
 path->space.polygon[3]=(dw_point){.7f,1.7f};
 engine.parking.goal=path->goal;engine.parking.space=path->space;
 assert(parking_publish(&engine,path,1000,true,false)==DE_OK);
}

static void parking_handoff(void){
 parking_fixture();
 struct lp_path *path=&engine.paths[engine.current].path;
 struct lp_command command;dw_vehicle vehicle;
 uint32_t generation=engine.paths[engine.current].generation;
 feedback.odom_pose=path->points[1].pose;
 assert(de_track(&engine,1000,.02f,&command,&vehicle,engine.budget)==DE_OK);
 assert(command.result==LP_STOP&&command.gate_required&&!engine.parking_stop_generation);
 struct lp_direction_fact gate={.path_revision=path->revision,.maneuver_revision=path->maneuver_revision,
  .calculation_sequence=1,.serial_stop_sequence=1,.gate_revision=1,.expires_pi_us=2000,
  .next_segment=1,.direction=-1,.current=true,.steering_settled=true};
 assert(!lp_tracker_next_segment(&engine.tracker,path,1000,1,&gate));
 gate.stationary=true;
 assert(lp_tracker_next_segment(&engine.tracker,path,1000,1,&gate));

 feedback.odom_pose=path->goal;feedback.sequence++;
 source_busy=true;
 assert(de_track(&engine,1001,.02f,&command,&vehicle,engine.budget)==DE_BUSY);
 assert(engine.completed==1&&!engine.parking_stop_generation);
 assert(engine.progress_snapshot.segment==0&&engine.tracker.index==2);
 source_busy=false;
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,1001)==DE_OK);
 assert(engine.parking.phase==DP_ENTERING);
 atomic_store(&engine.parking_stop_generation,generation+1);
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,1001)==DE_OK);
 assert(engine.parking.phase==DP_ENTERING);

 assert(de_track(&engine,1001,.02f,&command,&vehicle,engine.budget)==DE_OK);
 assert(command.result==LP_STOP&&!command.gate_required&&engine.parking_stop_generation==generation);
 size_t accepted_index=engine.progress_snapshot.index;
 assert(accepted_index>=2&&accepted_index<=3);
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,1002)==DE_OK);
 assert(engine.parking.phase==DP_FINAL_VERIFY&&!engine.parking_stop_generation);
 path=&engine.paths[engine.current].path;
 assert(path->execution.mission.active_segment==1&&path->initial_tracker_index==accepted_index&&path->initial_pi_us==999);
 assert(de_track(&engine,1002,.02f,&command,&vehicle,engine.budget)==DE_OK);
 assert(command.result==LP_STOP&&!command.gate_required&&command.segment_index==1&&!engine.path_invalidated);

 /* A stationary confirmation for this exact published plan closes the maneuver. */
 engine.facts=(struct db_facts){.valid=true,.expires_pi_us=1000000,.stop_applied=true,.stationary=true,
  .plan_revision=path->revision,.phase_revision=path->maneuver_revision};
 engine.control_facts.stop=(xenova_drive_StopFacts){.valid_until_pi_us=1000000,.producer_role=1,
  .calculation_sequence=1,.stationary_started_pi_us=1000,
  .stationary_observed_pi_us=1000+engine.parking_limits.final_window_us};
 feedback.sequence++;
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,
  engine.control_facts.stop.stationary_observed_pi_us)==DE_OK);
 assert(engine.parking.phase==DP_HOLD&&engine.parking.terminal&&engine.parking.result==DP_FEASIBLE);
}

static void parking_deadlines(void){
 parking_fixture();
 struct dp_state *parking=&engine.parking;
 uint32_t revision=engine.paths[engine.current].generation;
 /* Model candidate selection while the published path is issuing a stop. */
 engine.paths[engine.current].path.execution.mission.phase=DP_EVAL_STOP;
 parking->phase=DP_EVALUATE;parking->has_best=true;parking->round.initialized=true;
 contend=true;guest=99;
 assert(parking_publish(&engine,&parking->best_maneuver,1000,true,true)==DE_BUSY);
 give(&engine);contend=false;guest=100;
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,1001)==DE_WAITING);
 assert(parking->phase==DP_EVALUATE&&!parking->terminal&&!engine.behavior_failed&&!engine.path_invalidated);
 assert(!parking->publish_source&&!parking->has_best&&!parking->round.initialized&&!parking->round_deadline_us);
 assert(engine.paths[engine.current].generation==revision);

 /* The replacement is a new candidate and owns a new calculation deadline. */
 engine.budget.deadline_us=200;
 engine.grant.admitted=true;
 snapshot.world.required_features=DW_PARKING_MODEL;
 snapshot.world.parking_slots_count=1;
 snapshot.world.parking_slots[0].slot_id=2;
 /* The next search sees and evaluates a fresh candidate even when its
  * occupancy is not yet known. */
 assert(parking_start(&engine,(struct dw_handle){0},&snapshot,feedback.odom_pose,&feedback,
  1002,engine.budget)==DE_WAITING);
 assert(parking->round.initialized&&parking->round.done&&parking->round.ids[0]==2);
 snapshot.world.required_features=0;
 assert(parking_publish(&engine,&parking->best_maneuver,1002,true,true)==DE_OK);
 assert(parking->request_ready&&parking->result==DP_FEASIBLE);
 assert(engine.grant.search_deadline_pi_us==4000000);

 /* An already selected path is preserved across a fresh validation job. */
 struct lp_path *current=&engine.paths[engine.current].path;
 parking->phase=DP_ALIGN_STOP;guest=200;
 assert(parking_publish(&engine,current,1003,false,false)==DE_WAITING);
 assert(parking->publish_source==current&&parking->publish_revalidate);
 engine.budget.deadline_us=300;
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,1004)==DE_OK);
 assert(checked_deadline==300&&parking->published_phase==DP_ALIGN_STOP&&!parking->publish_source);
 assert(parking_poll(&engine,&snapshot.world,feedback.odom_pose,&feedback,4000000)==DE_STATE);
 assert(parking->phase==DP_HOLD&&parking->terminal&&engine.behavior_failed);
}

int main(int argc,char **argv){
 assert(argc==2);
 if(!strcmp(argv[1],"writer"))writer_contention();
 else if(!strcmp(argv[1],"handoff"))parking_handoff();
 else if(!strcmp(argv[1],"deadlines"))parking_deadlines();
 else assert(false);
}
