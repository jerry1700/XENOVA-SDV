/* SPDX-License-Identifier: Apache-2.0 */
/* Host test/experiment ABI only; firmware never links allocation or this ABI. */
#include "lattice.h"
#include "engine.h"
#include "pb_encode.h"
#include "pb_decode.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>

extern const struct lp_config lp_selected_config;
extern const struct lp_assets lp_selected_assets;
struct probe {struct dw_snapshot snapshot;struct lp_search search;struct lp_path path;
 struct lp_tracker tracker;struct lp_config config;struct lp_metrics tracked;
 struct lp_collision_index collision_index;struct tr_basis basis;uint64_t cancel_after;bool cancelled;enum dw_result decoded;
 struct lp_tracking_geometry tracking_geometry;struct lp_parking_cache parking_cache;bool use_prepared_tracking;
 double parking_trace[24];};
struct behavior_probe {struct db_episode episode;struct db_facts facts;struct db_limits limits;struct dp_round round;};
static const struct dw_trust trust={.vehicle="rc-test-1",.mapper_boot="11111111-1111-4111-8111-111111111111",
 .mapper_instance="22222222-2222-4222-8222-222222222222",.map_epoch="33333333-3333-4333-8333-333333333333",
 .frame_epoch="44444444-4444-4444-8444-444444444444",.odom_epoch="55555555-5555-4555-8555-555555555555",
 .configuration="lattice-lab-1",.calibration="encoder-lab-1",.primitive=TEST_PRIMITIVE_SHA,
 .clock={.epoch="66666666-6666-4666-8666-666666666666",.lower_offset_us=100000,
 .upper_offset_us=101000,.expires_pi_us=3000000,.max_error_us=2000},.max_live_age_us=300000};
static int clock_us(void *unused,uint64_t *out) {
 (void)unused;struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return -1;
 *out=(uint64_t)t.tv_sec*1000000+(uint64_t)t.tv_nsec/1000;return 0;
}
static bool cancelled(void *context) {
 struct probe *p=context;return p->cancelled||(p->cancel_after&&p->search.metrics.footprints>=p->cancel_after);
}
void *probe_new(const uint8_t *bytes,size_t length) {
 struct probe *p=calloc(1,sizeof(*p));if(!p)return NULL;
 p->decoded=dw_decode(bytes,length,&trust,1200000,&p->snapshot);p->config=lp_selected_config;
 if(!p->decoded)lp_index_build(&p->collision_index,&p->snapshot.world,&p->config);
 return p;
}
void probe_free(void *pointer) { free(pointer); }
static struct dp_limits parking_limits(const struct lp_config *c){
 return (struct dp_limits){c->parking_goal_distance,c->parking_goal_heading,c->parking_seed_inward_m,c->parking_yaw_seed_rad,
  c->min_control_speed,c->parking_clearance_sufficient,c->parking_clearance_band,c->parking_stationary_speed,c->parking_yaw_rate,c->parking_final_window_us};
}
int probe_parking_goal_set(void *pointer,uint32_t slot,unsigned strategy,float *out){
 struct probe *p=pointer;const xenova_drive_ParkingSlot *s=dp_slot(&p->snapshot.world,slot);
 struct lp_goal_set goals;struct lp_space space;struct dp_limits limits=parking_limits(&p->config);
 if(!dp_slot_eligible(&p->snapshot.world,s,strategy)||!dp_goal_seeds(s,strategy,&p->config,&limits,.01f,&goals,&space))return 0;
 for(unsigned k=0;k<goals.count;k++){out[3*k]=goals.poses[k].x;out[3*k+1]=goals.poses[k].y;out[3*k+2]=goals.poses[k].yaw;}
 return (int)goals.count;
}
int probe_parking_terminal(void *pointer,uint32_t slot,float x,float y,float yaw,float error){
 struct probe *p=pointer;const xenova_drive_ParkingSlot *s=dp_slot(&p->snapshot.world,slot);
 struct lp_goal_set goals;struct lp_space space;struct dp_limits limits=parking_limits(&p->config);
 if(!dp_slot_eligible(&p->snapshot.world,s,1)||!dp_goal_seeds(s,1,&p->config,&limits,.01f,&goals,&space))return 0;
 float radius=hypotf(fmaxf(p->config.front,p->config.rear)+p->config.margin,p->config.half_width+p->config.margin);
 return dp_terminal_accept(&space,(dw_pose){x,y,yaw},error+radius*.003f,goals.poses[0],.003f,&p->config);
}
int probe_parking_search_strategy(void *pointer,uint32_t slot,float x,float y,float yaw,unsigned strategy){
 struct probe *p=pointer;const dw_world *w=&p->snapshot.world;const xenova_drive_ParkingSlot *s=dp_slot(w,slot);
 struct lp_goal_set goals;struct lp_space space;struct dp_limits limits=parking_limits(&p->config);
 if(!dp_slot_eligible(w,s,strategy)||!dp_goal_seeds(s,strategy,&p->config,&limits,.01f,&goals,&space)||!tr_basis_accept(&p->basis,w))return LP_INPUT;
 struct tr_intent task={true,false,s->approach_lane_id,s->approach_lane_id,s->approach_lane_id,.01f};
 struct tr_view view;dw_pose root={x,y,yaw};memset(p->parking_trace,0,sizeof(p->parking_trace));
 bool opened=tr_open(w,&task,&view);
 p->parking_trace[0]=opened&&tr_parking_contains(&view,space.polygon,space.uncertainty,root,p->config.tracking_guard+task.vehicle_pad,&p->config);
 p->parking_trace[3]=goals.count;p->parking_trace[11]=opened?2*view.selected->centerline[0].y-w->lane_model.divider[0].y:NAN;
 p->parking_trace[12]=space.polygon[2].y;p->parking_trace[13]=space.uncertainty;
 p->parking_trace[14]=x;p->parking_trace[15]=y;p->parking_trace[16]=yaw;
 p->parking_trace[17]=goals.poses[0].x;p->parking_trace[18]=goals.poses[0].y;p->parking_trace[19]=goals.poses[0].yaw;
 uint64_t now;clock_us(NULL,&now);struct lp_budget b={now+1000000,clock_us,cancelled,p,&p->collision_index};
 enum lp_result rc=lp_start_parking(&p->search,w,(dw_pose){x,y,yaw},0,&goals,goals.final_gear,2,&space,&task,&p->basis,&p->config,&lp_selected_assets,b);
 p->parking_trace[1]=rc;
 while(rc==LP_SEARCHING)rc=lp_advance(&p->search,8,&p->path);
 p->parking_trace[2]=rc;p->parking_trace[4]=p->search.selected_goal;p->parking_trace[5]=p->path.count;
 p->parking_trace[6]=(double)p->search.metrics.expanded;p->parking_trace[7]=(double)p->search.metrics.primitives;
 p->parking_trace[8]=(double)p->search.metrics.footprints;p->parking_trace[9]=(double)p->search.metrics.cells;
 p->parking_trace[10]=(double)p->search.metrics.road_checks;
 if(rc==LP_PATH_FOUND&&p->path.count){dw_pose last=p->path.points[p->path.count-1].pose;
  p->parking_trace[20]=last.x;p->parking_trace[21]=last.y;p->parking_trace[22]=last.yaw;
  p->parking_trace[23]=dp_goal_contains(&space,last,p->config.tracking_guard+task.vehicle_pad,&p->config);}
 return rc;
}
int probe_parking_search(void *pointer,uint32_t slot,float x,float y,float yaw){
 return probe_parking_search_strategy(pointer,slot,x,y,yaw,1);
}
void probe_parking_trace(void *pointer,double out[24]){memcpy(out,((struct probe *)pointer)->parking_trace,24*sizeof(*out));}
void *probe_behavior_new(void *pointer){
 struct probe *p=pointer;struct behavior_probe *b=calloc(1,sizeof(*b));if(!b)return NULL;
 const struct lp_config *c=&p->config;
 b->limits=(struct db_limits){c->avoid_episode_us,c->avoid_phase_us,c->avoid_progress_us,c->avoid_settle_us,
  c->avoid_travel_m,c->avoid_progress_m,c->avoid_pass_margin_m,c->avoid_heading_rad,c->avoid_steering_rad,c->avoid_policy};
 b->facts=(struct db_facts){.valid=true,.meter_valid=true,.expires_pi_us=100000000,.vehicle_sequence=1,.meter_ticks=1000,.metres_per_tick=.01,.sampled_pi_us=1000000};
 memset(b->facts.meter_epoch,0xa1,16);strcpy(b->facts.pi_session,"Pi");strcpy(b->facts.device_session,"Device");strcpy(b->facts.odom_epoch,p->snapshot.world.odom_epoch);
 struct tr_basis basis={0};struct db_blocker blocker={.present=true,.lower={2,1},.upper={2.2f,1.4f},.station_lower=2,.station_upper=2.2f,.observation_sequence=1};
 strcpy(blocker.map,p->snapshot.world.map_epoch);uint8_t id[16];memset(id,0x21,16);
 if(!tr_basis_accept(&basis,&p->snapshot.world)||!db_episode_begin(&b->episode,id,1,2,&basis,&blocker,(dw_pose){4,2,0},1000000,&b->facts,&b->limits)){
  free(b);return NULL;}
 (void)db_episode_phase(&b->episode,DB_CHANGE_OUT,(dw_pose){4,2,0},1000000,&b->limits);return b;
}
int probe_behavior_step(void *pointer,void *behavior,uint64_t now,uint64_t ticks,uint64_t sequence,float x,float y,int wait){
 struct probe *p=pointer;struct behavior_probe *b=behavior;b->facts.meter_ticks=ticks;b->facts.vehicle_sequence=sequence;b->facts.sampled_pi_us=now;
 dw_vehicle v={.sequence=sequence,.quality=3,.position_error_m=.001f,.heading_error_rad=.001f};strcpy(v.odom_epoch,p->snapshot.world.odom_epoch);
 return db_episode_step(&b->episode,&p->snapshot.world,(dw_pose){x,y,0},&v,now,&b->facts,&b->limits,&p->config,wait!=0);
}
uint64_t probe_behavior_deadline(void *behavior,int episode){struct behavior_probe *b=behavior;return episode?b->episode.deadline_pi_us:b->episode.phase_deadline_pi_us;}
int probe_behavior_reenter(void *behavior,uint64_t now){struct behavior_probe *b=behavior;return db_episode_phase(&b->episode,DB_CHANGE_OUT,b->episode.phase_goal,now,&b->limits);}
int probe_episode_station(int split,float bounds[2]){
 struct tr_basis basis={.bound=true,.count=4,.points={{0,0},{.38f,0},{.6f,0},{1,0}}};
 dw_pose pose={.09f,.24f,0};
 if(split){
  basis.points[1]=(dw_point){1,0};basis.points[2]=(dw_point){1,.08f};basis.points[3]=(dw_point){0,.08f};
  pose=(dw_pose){.5f,.04f,0};
 }
 return db_station_bounds(&basis,pose,.0016f,&lp_selected_config,&bounds[0],&bounds[1]);
}
int probe_observed_blocker(void *pointer){
 struct probe *p=pointer;struct db_blocker blocker;struct tr_intent task={true,false,1,1,1,0};
 if(p->decoded||!db_blocker_observe(&p->snapshot.world,&task,(dw_pose){4.9f,1.2f,0},1.5f,&p->config,&blocker))return -1;
 return blocker.present;
}
void probe_behavior_free(void *behavior){free(behavior);}
uint32_t probe_round(void *pointer,void *behavior,int action,uint32_t id){
 struct probe *p=pointer;struct behavior_probe *b=behavior;
 if(action==0){xenova_drive_MissionGrant grant={.kind=2,.admitted=true,.mission_revision=1};memset(grant.mission_id.bytes,1,16);
  if(!dp_round_begin(&b->round,&p->snapshot.world,&grant,(dw_pose){.3f,1.2f,0}))return 0;}
 if(action==1)(void)dp_round_visit(&b->round,id);
 return dp_round_next(&b->round);
}
int probe_decoded(void *pointer) { return ((struct probe *)pointer)->decoded; }
int probe_plan(void *pointer,float x,float y,float yaw,float steer,float gx,float gy,float gyaw,
 unsigned nodes,unsigned collisions,uint64_t cancel_after,unsigned budget_ms,float width) {
 struct probe *p=pointer;if(p->decoded)return LP_INPUT;
 if(nodes)p->config.node_limit=nodes;
 if(collisions)p->config.collision_limit=collisions;
 if(width>0)p->config.half_width=width;
 p->cancel_after=cancel_after;uint64_t now;
 if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget b={now+(uint64_t)budget_ms*1000,clock_us,cancelled,p,&p->collision_index};
 enum lp_result rc=lp_start(&p->search,&p->snapshot.world,(dw_pose){x,y,yaw},steer,(dw_pose){gx,gy,gyaw},
                           &p->config,&lp_selected_assets,b,0);
 while(rc==LP_SEARCHING)rc=lp_advance(&p->search,8,&p->path);
 return rc;
}
unsigned probe_count(void *pointer) { return (unsigned)((struct probe *)pointer)->path.count; }
int probe_direction(void *pointer) { return ((struct probe *)pointer)->path.direction; }
int probe_point(void *pointer,unsigned index,float *out) {
 struct probe *p=pointer;if(index>=p->path.count)return -1;
 const xenova_drive_PathPoint *v=&p->path.points[index];
 out[0]=v->pose.x;out[1]=v->pose.y;out[2]=v->pose.yaw;out[3]=v->steering_rad;out[4]=v->velocity_mps;return 0;
}
int probe_track(void *pointer,float x,float y,float yaw,float velocity,float steer,float dt,float *out) {
 struct probe *p=pointer;dw_vehicle vehicle={.quality=3,.velocity_mps=velocity,.has_estimated_steering_rad=true,
  .estimated_steering_rad=steer,.position_error_m=.005f,.heading_error_rad=.01f,.has_steering_error_rad=true,.steering_error_rad=.001f};
 struct lp_command cmd;struct lp_metrics metrics={0};uint64_t now;
 if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget b={now+1000000,clock_us,NULL,NULL,&p->collision_index};
 enum lp_result rc=lp_track(&p->tracker,&p->path,&p->snapshot.world,(dw_pose){x,y,yaw},&vehicle,
                          &p->config,dt,&cmd,&metrics,&b);
 p->tracked.cells+=metrics.cells;p->tracked.footprints+=metrics.footprints;
 out[0]=cmd.velocity;out[1]=cmd.steer;out[2]=cmd.target_x;out[3]=cmd.target_y;
 out[4]=cmd.geometric_distance;out[5]=cmd.curvature;out[6]=cmd.known_distance;out[7]=cmd.emergency;
 return rc;
}
void probe_metrics(void *pointer,uint64_t *out) {
 struct probe *p=pointer;const struct lp_metrics *m=&p->search.metrics;
 out[0]=m->expanded;out[1]=m->primitives;out[2]=m->footprints;out[3]=m->cells;
 out[4]=m->nodes_high_water;out[5]=m->open_high_water;out[6]=m->index_probes_max;
 out[7]=m->cancel_checks;out[8]=m->clock_checks;out[9]=sizeof(*p);out[10]=p->tracked.cells;
}
int probe_update(void *pointer,const uint8_t *bytes,size_t n) {
 struct probe *p=pointer;struct dw_snapshot *next=malloc(sizeof(*next));if(!next)return -1;
 enum dw_result rc=dw_decode(bytes,n,&trust,1200000,next);
 if(!rc){p->snapshot=*next;lp_index_build(&p->collision_index,&p->snapshot.world,&p->config);}
 free(next);return rc;
}
int probe_valid(void *pointer) {
 struct probe *p=pointer;struct lp_metrics m={0};uint64_t now;
 if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget b={now+1000000,clock_us,NULL,NULL,&p->collision_index};
 return lp_validate_path(&p->path,&p->snapshot.world,&p->config,&m,&b);
}
/* One bounded test owner retains the Vehicle/path/World witness from the same
 * successful calculation. Prepared geometry inspection has no completed calc. */
struct engine_fixture {
 struct drive_engine core;dw_vehicle calculated_vehicle;struct de_witness calculated_world;uint32_t calculated_path;bool calculated;
};
void *engine_new(void){
 struct engine_fixture *unit=calloc(1,sizeof(*unit));if(!unit)return NULL;
 struct drive_engine *e=&unit->core;
 de_init(e,&lp_selected_config,&lp_selected_assets);dw_pool_source(&e->maps,&trust);return e;
}
static void engine_calculated(void *pointer,const dw_vehicle *vehicle){
 struct engine_fixture *unit=pointer;unit->calculated_vehicle=*vehicle;
 unit->calculated_world=unit->core.last_track_world;
 unit->calculated_path=unit->core.last_track_path.generation;unit->calculated=true;
}
void engine_free(void *e){free(e);}
int engine_world(void *e,const uint8_t *bytes,size_t n,uint64_t now){return de_world(e,bytes,n,now);}
int engine_fixture_world_result(void *e){return ((struct drive_engine *)e)->last_world_result;}
/* Actual input-only helper; the typed source is serialized unchanged. The
 * transport/P4 report submission is covered by the persistent app bridge. */
int engine_preflight(void *pointer,uint64_t now,uint8_t *out,size_t capacity,uint64_t facts[8]){
 struct drive_engine *e=pointer;uint64_t guest;
 (void)de_receive_poll(e,now);
 if(clock_us(NULL,&guest))return -1;
 struct de_input_check checked;struct lp_budget budget={guest+100000,clock_us,NULL,NULL,NULL};
 enum de_result rc=de_input_preflight(e,now,budget,&checked);
 facts[0]=checked.vehicle.sequence;facts[1]=checked.valid_until_pi_us;
 facts[2]=checked.metrics.footprints;facts[3]=checked.metrics.cells;
 facts[4]=(uint64_t)(e->current+1);facts[5]=e->has_grant;facts[6]=e->planning;facts[7]=e->completed;
 if(rc)return rc;
 pb_ostream_t stream=pb_ostream_from_buffer(out,capacity);
 if(!pb_encode(&stream,xenova_drive_EpisodeSourceContext_fields,&checked.source))return -2;
 return -(int)stream.bytes_written;
}
int engine_preflight_current(void *pointer,uint64_t now,uint8_t *out,size_t capacity,uint64_t facts[8]){
 struct drive_engine *e=pointer;(void)de_receive_poll(e,now);struct de_input_check checked;
 enum de_result rc=de_input_current(e,now,&checked);
 facts[0]=checked.vehicle.sequence;facts[1]=checked.valid_until_pi_us;facts[2]=checked.metrics.footprints;facts[3]=checked.metrics.cells;
 facts[4]=(uint64_t)(e->current+1);facts[5]=e->has_grant;facts[6]=e->planning;facts[7]=e->completed;
 if(rc)return rc;
 pb_ostream_t stream=pb_ostream_from_buffer(out,capacity);
 return pb_encode(&stream,xenova_drive_EpisodeSourceContext_fields,&checked.source)?-(int)stream.bytes_written:-2;
}
int engine_vehicle(void *e,const uint8_t *bytes,size_t n){
 dw_vehicle vehicle;enum dw_result rc=dw_vehicle_decode(bytes,n,&vehicle);
 return rc?100+(int)rc:(int)de_vehicle(e,&vehicle);
}
int engine_plan(void *pointer,uint64_t now,unsigned slices){
 struct drive_engine *e=pointer;uint64_t clock;if(clock_us(NULL,&clock))return DE_CLOCK;
 enum de_result received=de_receive_poll(e,now);
 if(e->maps.current<0&&received)return received;
 struct lp_budget b={clock+10000000,clock_us,NULL,NULL,NULL};
 enum de_result rc=de_plan_start(e,now,b);
 if(rc)return rc;
 for(unsigned k=0;k<slices&&e->planning;k++)rc=de_plan_slice(e,8,now);
 return rc;
}
int engine_track(void *e,uint64_t now,float *out){
 (void)de_receive_poll(e,now); /* Fixture dispatches the product map owner. */
 uint64_t clock;if(clock_us(NULL,&clock))return DE_CLOCK;
 struct lp_command command;dw_vehicle vehicle;struct lp_budget b={clock+1000000,clock_us,NULL,NULL,NULL};
 enum de_result rc=de_track(e,now,.02f,&command,&vehicle,b);
 if(rc==DE_OK)engine_calculated(e,&vehicle);
 out[0]=command.velocity;out[1]=command.steer;out[2]=command.emergency;out[3]=(float)command.result;
 return rc;
}
int engine_apply(void *e,const uint8_t *bytes,size_t n,uint64_t now){
 (void)de_receive_poll(e,now);
 dw_state *s=malloc(sizeof(*s));if(!s)return -1;enum dw_result rc=dw_state_decode(bytes,n,s);
 if(rc){free(s);return 100+(int)rc;}
 uint64_t clock;if(clock_us(NULL,&clock)){free(s);return DE_CLOCK;}
 struct lp_budget b={clock+10000000,clock_us,NULL,NULL,NULL};
 enum de_result result=de_apply(e,s,now,b);free(s);return result;
}
int engine_grant(void *e,const uint8_t *bytes,size_t n,uint64_t now){
 xenova_drive_DriveLink *link=calloc(1,sizeof(*link));if(!link)return -1;
 enum dw_result rc=dw_link_decode(bytes,n,link);
 int result=rc?100+(int)rc:link->which_payload!=xenova_drive_DriveLink_mission_grant_tag?DE_STATE:
  (int)de_mission(e,&link->payload.mission_grant,now);
 free(link);return result;
}
int engine_finish_apply(void *e,uint64_t now){return de_apply_finish(e,now);}
int engine_origin_mission(void *pointer,uint8_t *out,size_t capacity){
 struct drive_engine *e=pointer;struct de_reference h;const struct lp_path *path;
 if(de_path_acquire(e,&h,&path))return -1;
 pb_ostream_t stream=pb_ostream_from_buffer(out,capacity);
 bool ok=path->origin.retained&&pb_encode(&stream,xenova_drive_MissionExecution_fields,&path->origin.mission);
 (void)de_path_release(e,h);return ok?(int)stream.bytes_written:-1;
}
void engine_behavior_enable_for_budget(void *pointer){
 const uint8_t incarnation[16]={1};de_behavior_enable(pointer,incarnation);
}
void engine_parking_original_budget(void *pointer,uint64_t out[8]){
 struct drive_engine *e=pointer;const xenova_drive_MissionGrant *g=&e->parking.original_grant;
 out[0]=e->parking.active;out[1]=g->mission_deadline_pi_us;out[2]=g->search_deadline_pi_us;out[3]=g->execution_deadline_pi_us;
 out[4]=g->correction_attempts_used;out[5]=g->mission_revision;out[6]=e->parking.phase;out[7]=g->grant_revision;
}
int engine_track_grant(void *e,const uint8_t *bytes,size_t n,uint64_t now,float *out){
 xenova_drive_DriveLink *link=calloc(1,sizeof(*link));if(!link)return -1;
 enum dw_result decoded=dw_link_decode(bytes,n,link);uint64_t clock;
 if(decoded||link->which_payload!=xenova_drive_DriveLink_mission_grant_tag||clock_us(NULL,&clock)){free(link);return DE_STATE;}
 struct lp_command command;dw_vehicle vehicle;struct lp_budget budget={clock+1000000,clock_us,NULL,NULL,NULL};
 enum de_result rc=de_track_mission(e,&link->payload.mission_grant,now,.02f,&command,&vehicle,budget);
 if(rc==DE_OK)engine_calculated(e,&vehicle);
 out[0]=command.velocity;out[1]=command.steer;free(link);return rc;
}
int engine_track_grant_facts(void *e,const uint8_t *bytes,size_t n,const uint8_t *raw,size_t length,uint64_t now,float *out){
 xenova_drive_DriveLink *link=calloc(1,sizeof(*link));if(!link)return -1;
 xenova_drive_DriveControlFacts facts={0};pb_istream_t stream=pb_istream_from_buffer(raw,length);uint64_t clock;
 if(dw_link_decode(bytes,n,link)||link->which_payload!=xenova_drive_DriveLink_mission_grant_tag||
    !pb_decode(&stream,xenova_drive_DriveControlFacts_fields,&facts)||clock_us(NULL,&clock)){free(link);return DE_STATE;}
 struct lp_command command;dw_vehicle vehicle;struct lp_budget budget={clock+1000000,clock_us,NULL,NULL,NULL};
 enum de_result rc=de_track_mission_facts(e,&link->payload.mission_grant,&facts,now,.02f,&command,&vehicle,budget);
 if(rc==DE_OK)engine_calculated(e,&vehicle);
 out[0]=command.velocity;out[1]=command.steer;free(link);return rc;
}
int engine_state(void *pointer,uint8_t *out,size_t cap){
 struct engine_fixture *unit=pointer;struct drive_engine *e=&unit->core;struct de_reference h;const struct lp_path *p;
 if(de_path_acquire(e,&h,&p))return -1;
 bool completed=unit->calculated&&unit->calculated_path==h.generation;
 dw_vehicle source=unit->calculated_vehicle;struct de_witness witness=unit->calculated_world;
 if(!completed){
  struct dw_handle mh;const struct dw_snapshot *map;
  if(dw_vehicle_latest(&e->odometry,&source)||dw_acquire(&e->maps,&mh,&map)){de_path_release(e,h);return -1;}
  witness.reference_revision=map->world.reference_revision;witness.observation_sequence=map->world.observation_seq;
  strcpy(witness.map_epoch,map->world.map_epoch);(void)dw_release(&e->maps,mh);
 }
 dw_state *s=calloc(1,sizeof(*s));if(!s){de_path_release(e,h);return -1;}
 if(!lp_state_export(p,s)){free(s);(void)de_path_release(e,h);return -1;}strcpy(s->source_vm,"77777777-7777-4777-8777-777777777777");
 strcpy(s->source_execution,"88888888-8888-4888-8888-888888888888");strcpy(s->source_app_instance,"99999999-9999-4999-8999-999999999999");
 s->source_generation=1;s->state_revision=1;strcpy(s->frame_epoch,p->frame);strcpy(s->odom_epoch,p->odom);
 strcpy(s->configuration,p->configuration);strcpy(s->primitive,e->assets->sha256);strcpy(s->reference_id,p->reference);
 s->reference_revision=witness.reference_revision;
 s->path_revision=p->revision;s->maneuver_revision=p->maneuver_revision;
 s->has_world_features=true;s->world_features=p->world_features;
 s->direction=p->direction;s->maneuver_phase=p->phase;s->merge_goal=p->merge_goal;
 if(p->merge_passed_reference_revision&&p->merge_passed_reference_revision==s->reference_revision){
  s->has_merge_passed_reference_revision=true;s->merge_passed_reference_revision=p->merge_passed_reference_revision;
 }
 s->vehicle_sequence=source.sequence;s->calculation_sequence=completed?e->completed:0;s->tracker_index=e->tracker.index;s->complete=true;
 if(s->has_mission)s->mission.active_segment=e->tracker.segment;
 strcpy(s->message_id,"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");strcpy(s->calibration,"encoder-lab-1");
 strcpy(s->map_epoch,witness.map_epoch);s->observation_seq=witness.observation_sequence;
 s->path_count=p->count;memcpy(s->path,p->points,p->count*sizeof(*p->points));de_path_release(e,h);
 pb_ostream_t stream=pb_ostream_from_buffer(out,cap);bool ok=pb_encode(&stream,xenova_drive_DriveState_fields,s);
 free(s);return ok?(int)stream.bytes_written:-1;
}

int engine_memory(void *e,unsigned region,uint64_t *out){
 struct dw_memory m;enum de_result rc=de_memory_read(e,region,&m);
 if(!rc){out[0]=m.reserved;out[1]=m.capacity;out[2]=m.used;out[3]=m.peak;out[4]=m.exhausted;out[5]=m.contention;}
 return rc;
}
int engine_force_node_limit(void *pointer,uint64_t now){
 struct drive_engine *e=pointer;
 struct lp_config limited=*e->config;limited.node_limit=1;
 const struct lp_config *original=e->config;e->config=&limited;
 int rc=engine_plan(e,now,4096);e->config=original;return rc;
}
/* 새 내부 차로 소비 경계의 actual C 시험 ABI다. 운영 wire나 임무 발급이 아니다. */
uint32_t probe_task_lane(void *pointer,float x,float y,float yaw,float error){
 struct probe *p=pointer;return tr_unique_lane(&p->snapshot.world,(dw_pose){x,y,yaw},error,&p->config);
}
void probe_task_tape(void *pointer,float half_width){
 ((struct probe *)pointer)->config.lane_tape_half_width_m=half_width;
}
int probe_lane_guide(void *pointer,uint32_t from,uint32_t to,float y,float goal_y){
 struct probe *p=pointer;const dw_world *w=&p->snapshot.world;
 if(p->decoded||!tr_basis_accept(&p->basis,w))return LP_INPUT;
 struct tr_intent task={true,true,to,from,from,0};
 uint64_t now;if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget budget={now+1000000,clock_us,NULL,NULL,NULL};
 struct lp_metrics metrics={0};
 return lp_lane_guide(&p->search,&p->path,w,(dw_pose){.6f,y,0},0,0,(dw_pose){2.f,goal_y,0},
  &p->config,budget,&task,&p->basis,&metrics);
}
int probe_task_contains(void *pointer,uint32_t from,uint32_t to,int change,float x,float y,float yaw,float error){
 struct probe *p=pointer;struct tr_intent task={true,change!=0,to,from,from,0};struct tr_view view;
 return tr_open(&p->snapshot.world,&task,&view)&&tr_contains(&view,(dw_pose){x,y,yaw},error,&p->config);
}
int probe_lane_change_terminal_path(void *pointer,float y){
 struct probe *p=pointer;
 const dw_world *w=&p->snapshot.world;
 if(p->decoded||!tr_basis_accept(&p->basis,w))return LP_INPUT;
 struct lp_path *path=&p->path;
 memset(path,0,sizeof(*path));
 path->count=6;path->task=(struct tr_intent){true,true,2,1,1,0};
 path->basis=p->basis;path->world_features=w->required_features;
 strcpy(path->frame,w->frame_epoch);strcpy(path->odom,w->odom_epoch);
 strcpy(path->configuration,w->configuration);strcpy(path->reference,w->reference_id);
 /* The goal is close enough for capture and the remaining path permits forward motion. */
 for(unsigned k=0;k<path->count;k++){
  float distance=.018f*k;
  path->points[k]=(xenova_drive_PathPoint){.pose={2+distance*cosf(.2f),y+distance*sinf(.2f),.2f},
   .velocity_mps=p->config.primitive_speed};
 }
 path->goal=path->points[path->count-1].pose;
 uint64_t now;if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget budget={now+1000000,clock_us,NULL,NULL,&p->collision_index};
 struct lp_metrics metrics={0};
 return lp_validate_path(path,w,&p->config,&metrics,&budget);
}
int probe_task_goal(void *pointer,uint32_t id,float x,float y,float horizon,float *out){
 struct probe *p=pointer;struct tr_intent task={true,false,id,id,id,0};dw_pose goal;
 enum lp_result rc=lp_task_goal(&p->snapshot.world,&task,(dw_pose){x,y,0},horizon,.4f,&goal);
 if(rc==LP_PATH_FOUND){out[0]=goal.x;out[1]=goal.y;out[2]=goal.yaw;}return rc;
}
int probe_basis_accept(void *pointer){struct probe *p=pointer;return tr_basis_accept(&p->basis,&p->snapshot.world);}
unsigned probe_basis_count(void *pointer){return ((struct probe *)pointer)->basis.count;}
int probe_basis_fault(void *pointer,unsigned variant){
 /* 형식 거절의 내부 값 주입이다. wire가 수락한 geometry라는 뜻이 아니다. */
 struct probe *p=pointer;dw_world *w=&p->snapshot.world;pb_size_t count=w->reference_count;
 dw_point last=w->reference[count-1];char calibration=w->calibration[0];
 if(variant==1)w->reference[count-1]=w->reference[0];else if(variant==2)w->reference_count=65;else w->calibration[0]='z';
 int accepted=tr_basis_accept(&p->basis,w);w->reference_count=count;w->reference[count-1]=last;w->calibration[0]=calibration;return accepted;
}
int probe_parking_approach(void *pointer,uint32_t lane,uint32_t slot){
 struct probe *p=pointer;struct tr_intent task={true,false,lane,lane,lane,0};struct tr_view view;
 return tr_open(&p->snapshot.world,&task,&view)&&tr_parking_approach(&view,slot);
}
int engine_task_state(void *pointer,uint32_t *out){
 struct drive_engine *e=pointer;struct de_reference h;const struct lp_path *p;
 if(de_path_acquire(e,&h,&p))return -1;
 out[0]=p->task.home_lane_id;out[1]=p->task.from_lane_id;out[2]=p->task.selected_lane_id;
 out[3]=p->task.changing;out[4]=p->basis.count;
 (void)de_path_release(e,h);return 0;
}

/* signed 알고리즘/추적기 시험 입구. 외부 Gate fact는 명시 software fixture다. */
int probe_signed_plan(void *pointer,float x,float y,float yaw,float gx,float gy,float gyaw,int first,int last,unsigned cusps){
 struct probe *p=pointer;uint64_t now;if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget budget={now+10000000,clock_us,cancelled,p,&p->collision_index};
 enum lp_result rc=lp_start_signed(&p->search,&p->snapshot.world,(dw_pose){x,y,yaw},0,(dw_pose){gx,gy,gyaw},
  first,last,cusps,&p->config,&lp_selected_assets,budget);
 while(rc==LP_SEARCHING)rc=lp_advance(&p->search,16,&p->path);
 lp_tracker_begin(&p->tracker,1);return rc;
}
unsigned probe_segments(void *pointer,int *out){
 struct lp_path *path=&((struct probe *)pointer)->path;
 for(unsigned i=0;i<path->execution.segments_count;i++){out[3*i]=path->execution.segments[i].first_index;out[3*i+1]=path->execution.segments[i].last_index;out[3*i+2]=path->execution.segments[i].signed_direction;}
 return path->execution.segments_count;
}
static dw_pose fixture_step(dw_pose p,float speed,float delta,float dt,float wheelbase){
 float turn=speed/wheelbase*tanf(delta)*dt,ds=speed*dt;
 if(fabsf(turn)<1e-10f){p.x+=ds*cosf(p.yaw);p.y+=ds*sinf(p.yaw);}
 else{float radius=ds/turn;p.x+=radius*(sinf(p.yaw+turn)-sinf(p.yaw));p.y-=radius*(cosf(p.yaw+turn)-cosf(p.yaw));}
 p.yaw=dw_angle(p.yaw+turn);return p;
}
void probe_signed_fixture(void *pointer,unsigned variant){
 struct probe *p=pointer;struct lp_path *path=&p->path;memset(path,0,sizeof(*path));
 path->world_features=p->snapshot.world.required_features;strcpy(path->frame,p->snapshot.world.frame_epoch);strcpy(path->odom,p->snapshot.world.odom_epoch);
 strcpy(path->configuration,p->snapshot.world.configuration);strcpy(path->reference,p->snapshot.world.reference_id);
 path->revision=19;path->maneuver_revision=23;path->execution.segments_count=variant>=2?2:1;
 int gear=variant==1||variant==3?-1:1;float delta=gear<0?.15f:0;
 path->execution.segments[0]=(xenova_drive_GearSegment){0,32,gear};
 dw_pose pose={2,1.6f,0};
 for(unsigned i=0;i<=32;i++){path->points[path->count++]=(xenova_drive_PathPoint){.pose=pose,.steering_rad=delta,.velocity_mps=(i==32?0:gear*.2f)};pose=fixture_step(pose,gear*.4f,delta,.05f,p->config.wheelbase);}
 if(variant>=2){pose=path->points[32].pose;gear=-gear;delta=gear<0?.15f:0;path->execution.segments[1]=(xenova_drive_GearSegment){33,65,gear};
  for(unsigned i=0;i<=32;i++){path->points[path->count++]=(xenova_drive_PathPoint){.pose=pose,.steering_rad=delta,.velocity_mps=(i==0||i==32?0:gear*.2f)};pose=fixture_step(pose,gear*.4f,delta,.05f,p->config.wheelbase);}}
 path->goal=path->merge_goal=path->points[path->count-1].pose;lp_tracker_begin(&p->tracker,1);
}
int probe_parking_cusp_fixture(void *pointer,unsigned strategy){
 struct probe *p=pointer;struct lp_path *path=&p->path;const dw_world *w=&p->snapshot.world;
 const xenova_drive_ParkingSlot *slot=dp_slot(w,1);
 struct dp_limits limits=parking_limits(&p->config);struct lp_goal_set goals;
 memset(path,0,sizeof(*path));
 if(!dp_goal_seeds(slot,strategy,&p->config,&limits,.01f,&goals,&path->space)||!tr_basis_accept(&path->basis,w))return -1;
 path->world_features=w->required_features;
 path->task=(struct tr_intent){true,false,slot->approach_lane_id,slot->approach_lane_id,slot->approach_lane_id,0};
 strcpy(path->frame,w->frame_epoch);strcpy(path->odom,w->odom_epoch);
 strcpy(path->configuration,w->configuration);strcpy(path->reference,w->reference_id);
 int entry_gear=strategy==2?1:-1;
 float yaw=-entry_gear*1.57079632679f;
 const unsigned before=8,after=strategy==2?39:43;
 for(unsigned i=0;i<=before;i++){
  dw_pose pose={1.5f,1.2f-.015f*(before-i),yaw};
  path->points[i]=(xenova_drive_PathPoint){.pose=pose,.velocity_mps=i==before?0:-entry_gear*.1f};
 }
 for(unsigned i=0;i<=after;i++){
  dw_pose pose={1.5f,1.2f-.0185f*i,yaw};
  path->points[before+1+i]=(xenova_drive_PathPoint){.pose=pose,.velocity_mps=i&&i<after?entry_gear*.1f:0};
 }
 path->count=before+after+2;path->goal=path->points[path->count-1].pose;
 path->revision=19;path->maneuver_revision=23;path->execution.segments_count=2;
 path->execution.segments[0]=(xenova_drive_GearSegment){0,before,-entry_gear};
 path->execution.segments[1]=(xenova_drive_GearSegment){before+1,path->count-1,entry_gear};
 path->initial_tracker_index=before;lp_tracker_begin(&p->tracker,1);
 if(dp_goal_contains(&path->space,path->points[before].pose,0,&p->config)||
    !dp_goal_contains(&path->space,path->goal,0,&p->config))return -1;
 return before;
}
bool probe_prepare_tracking(void *pointer){
 struct probe *p=pointer;
 p->parking_cache.ready=false;
 p->use_prepared_tracking=lp_prepare_tracking(&p->path,&p->config,&p->tracking_geometry);
 return p->use_prepared_tracking;
}
int probe_track_sample(void *pointer,unsigned index,uint64_t stamp,float velocity,float *out){
 struct probe *p=pointer;if(index>=p->path.count)return LP_INPUT;
 dw_vehicle vehicle={.quality=3,.pi_sample_us=stamp,.velocity_mps=velocity,.has_measured_steering_rad=true,
  .measured_steering_rad=p->path.points[index].steering_rad,.position_error_m=.003f,.heading_error_rad=.003f,
  .has_steering_error_rad=true,.steering_error_rad=.001f};
 struct lp_command command;struct lp_metrics metrics={0};uint64_t now;if(clock_us(NULL,&now))return LP_CLOCK;
 struct lp_budget budget={now+1000000,clock_us,NULL,NULL,&p->collision_index};
 const struct lp_tracking_geometry *geometry=p->use_prepared_tracking?&p->tracking_geometry:NULL;
 enum lp_result rc=lp_track_prepared(&p->tracker,&p->path,geometry,&p->snapshot.world,p->path.points[index].pose,&vehicle,
  &p->config,.02f,&command,&metrics,&budget,geometry?&p->parking_cache:NULL);
 out[0]=command.velocity;out[1]=command.steer;out[2]=command.gate_required;out[3]=command.segment_index;out[4]=command.requested_direction;out[5]=p->tracker.progress;
 return rc;
}
void probe_seed_cusp(void *pointer){struct probe *p=pointer;p->path.initial_tracker_index=p->path.execution.segments[0].last_index;lp_tracker_begin(&p->tracker,2);}
int probe_gate(void *pointer,unsigned variant){
 struct probe *p=pointer;struct lp_direction_fact fact={.path_revision=19,.maneuver_revision=23,.calculation_sequence=41,
  .serial_stop_sequence=9001,.gate_revision=7,.expires_pi_us=2000000,.next_segment=1,.direction=p->path.execution.segments[1].signed_direction,.current=true,.stationary=true,.steering_settled=true};
 if(variant==1)fact.calculation_sequence=9001;else if(variant==2)fact.serial_stop_sequence=0;else if(variant==3)fact.next_segment=2;
 else if(variant==4)fact.direction=-fact.direction;else if(variant==5)fact.steering_settled=false;else if(variant==6)fact.expires_pi_us=1000000;
 else if(variant==7)fact.maneuver_revision++;else if(variant==8)fact.current=false;
 return lp_tracker_next_segment(&p->tracker,&p->path,1000000,41,&fact);
}

/* Unit-only registration/clock issuer leaf. Search, receipt hash comparison,
 * current inputs, re-adoption and tracker execute the product engine. This is
 * not a Gateway/P4/STOP/APPLIED or physical-device acceptance claim. */
void engine_fixture_auto_owner(void *pointer){((struct drive_engine *)pointer)->local_owner=true;}
int engine_fixture_phase_id(void *pointer,uint8_t out[37]){
 const struct drive_engine *e=pointer;memcpy(out,e->phase_state_message_id,37);return 0;
}
int engine_fixture_track_progress(void *pointer,uint8_t *out,size_t capacity,uint64_t view[4],uint8_t hash[32]){
 const struct drive_engine *e=pointer;const struct de_episode_progress *p=&e->last_track_world.episode;
 view[0]=p->valid;view[1]=p->path_generation;view[2]=p->vehicle_sequence;view[3]=p->registry_revision;
 memcpy(hash,p->core_sha256,32);
 if(!p->valid)return 0;
 pb_ostream_t stream=pb_ostream_from_buffer(out,capacity);
 return pb_encode(&stream,xenova_drive_EpisodeProgress_fields,&p->progress)?(int)stream.bytes_written:-1;
}
uint64_t engine_fixture_planner_facts_sequence(void *pointer){return ((struct drive_engine *)pointer)->facts.vehicle_sequence;}

int engine_fixture_facts(void *pointer,const uint8_t *raw,size_t n,uint64_t now){
 struct drive_engine *e=pointer;xenova_drive_DriveControlFacts facts={0};pb_istream_t in=pb_istream_from_buffer(raw,n);
 if(!pb_decode(&in,xenova_drive_DriveControlFacts_fields,&facts))return -1;
 enum de_result rc=de_control_facts(e,&facts,now);atomic_store(&e->facts_current,rc==DE_OK);return rc;
}
int engine_fixture_status(void *pointer,const uint8_t *raw,size_t n,uint64_t now){
 xenova_drive_EpisodeStatus *status=calloc(1,sizeof(*status));if(!status)return -1;
 pb_istream_t in=pb_istream_from_buffer(raw,n);int rc=!pb_decode(&in,xenova_drive_EpisodeStatus_fields,status)?-1:
  (int)de_episode_status(pointer,status,now);free(status);return rc;
}
static void fixture_uuid(char out[37],const uint8_t raw[16]){
 static const char hex[]="0123456789abcdef";unsigned at=0;
 for(unsigned i=0;i<16;i++){if(i==4||i==6||i==8||i==10)out[at++]='-';out[at++]=hex[raw[i]>>4];out[at++]=hex[raw[i]&15];}out[at]=0;
}
int engine_fixture_registration(void *pointer,uint64_t now,uint8_t *raw,size_t capacity){
 struct drive_engine *e=pointer;xenova_drive_EpisodeRequest *r=calloc(1,sizeof(*r));dw_state *state=calloc(1,sizeof(*state));
 if(!r||!state){free(r);free(state);return -1;}
 /* The prior actual C tracker ran; model only its existing app-owner completion
  * callback, never a timer or fabricated successful calculation. */
 if(!e->completed||!de_episode_request(e,r)||!de_registration_state(e,state)){free(r);free(state);return -1;}
 e->last_actual_calculation=e->completed;r->calculation_sequence=e->completed;
 fixture_uuid(r->vm,e->grant.owner_vm.bytes);fixture_uuid(r->execution,e->grant.owner_execution.bytes);fixture_uuid(r->app_instance,e->grant.app_instance.bytes);
 if(clock_us(NULL,&r->completed_guest_us)){free(r);free(state);return -1;}
 r->valid_until_pi_us=now+250000;r->clock_probe_sequence=1;memset(r->clock_reply_sha256.bytes,9,32);
 if(!dw_plan_hash(state,r->plan_sha256.bytes)){free(r);free(state);return -1;}
 pb_ostream_t out=pb_ostream_from_buffer(raw,capacity);bool encoded=pb_encode(&out,xenova_drive_EpisodeRequest_fields,r);
 if(encoded)de_episode_submitted(e,r);
 free(r);free(state);return encoded?(int)out.bytes_written:-1;
}
int engine_fixture_receipt(void *pointer,const uint8_t *raw,size_t n){
 xenova_drive_EpisodeReceipt *receipt=calloc(1,sizeof(*receipt));if(!receipt)return -1;
 pb_istream_t in=pb_istream_from_buffer(raw,n);int rc=!pb_decode(&in,xenova_drive_EpisodeReceipt_fields,receipt)?-1:
  (int)de_episode_receipt(pointer,receipt);free(receipt);return rc;
}
int engine_fixture_begin(void *pointer,uint64_t now){
 struct drive_engine *e=pointer;uint64_t guest;if(clock_us(NULL,&guest))return DE_CLOCK;
 (void)de_receive_poll(e,now);
 struct lp_budget budget={guest+lp_selected_config.planner_budget_us,clock_us,NULL,NULL,NULL};
 return de_plan_start(e,now,budget);
}
int engine_fixture_continue(void *pointer,uint64_t now,unsigned slices){
 struct drive_engine *e=pointer;(void)de_receive_poll(e,now);enum de_result rc=DE_WAITING;
 for(unsigned k=0;k<slices&&e->planning;k++)rc=de_plan_slice(e,8,now);
 return rc;
}
int engine_fixture_adoption_view(void *pointer,uint64_t out[16],uint8_t hashes[64]){
 struct drive_engine *e=pointer;struct de_reference h;const struct lp_path *p;
 if(de_path_acquire(e,&h,&p))return -1;
 out[0]=h.generation;out[1]=p->initial_pi_us;out[2]=p->initial_tracker_index;out[3]=e->tracker.path_token;
 out[4]=e->tracker.initialized;out[5]=e->tracker.last_pi_us;out[6]=p->revision;out[7]=p->maneuver_revision;
 out[8]=p->has_episode?p->episode.core.started_pi_us:0;out[9]=p->has_episode?p->episode.core.deadline_pi_us:0;
 out[10]=p->has_episode?p->episode.core.phase_deadline_pi_us:0;out[11]=p->has_episode?p->episode.core.meter_baseline_total_ticks:0;
 out[12]=e->episode.meter_used_ticks;out[13]=e->episode.waited_us;out[14]=e->planning;out[15]=e->completed;
 dw_state *state=calloc(1,sizeof(*state));if(!state){de_path_release(e,h);return -1;}
 /* Inspect this acquired current path, never a different pending candidate. */
 struct dw_handle mh;const struct dw_snapshot *map;bool ok=false;
 if(!dw_acquire(&e->maps,&mh,&map)){
  ok=lp_state_export(p,state);
  if(ok){
   strcpy(state->map_epoch,map->world.map_epoch);strcpy(state->frame_epoch,p->frame);strcpy(state->odom_epoch,p->odom);
   strcpy(state->configuration,p->configuration);strcpy(state->calibration,map->world.calibration);
   strcpy(state->primitive,map->world.primitive);strcpy(state->reference_id,p->reference);
   state->reference_revision=map->world.reference_revision;state->path_revision=p->revision;state->maneuver_revision=p->maneuver_revision;
   state->has_world_features=true;state->world_features=p->world_features;
   state->path_count=p->count;memcpy(state->path,p->points,p->count*sizeof(p->points[0]));
   ok=dw_plan_hash(state,hashes);
  }
  (void)dw_release(&e->maps,mh);
 }
 memset(hashes+32,0,32);if(ok&&p->has_episode)ok=dw_episode_core_hash(&p->episode.core,hashes+32);
 free(state);de_path_release(e,h);return ok?0:-1;
}
