/* SPDX-License-Identifier: Apache-2.0 */
#include "route_progress.h"
#include <math.h>
#include <float.h>
#include <string.h>
_Static_assert(sizeof(struct drg_owner)<=1024,"route owner is not a full State stack copy");

static bool limits(const struct drg_limits *c){return c&&c->progress_us&&isfinite(c->progress_m)&&c->progress_m>0&&
 isfinite(c->stationary_speed)&&c->stationary_speed>=0&&isfinite(c->yaw_rate)&&c->yaw_rate>=0;}
static bool same_birth(const xenova_drive_RouteCore *a,const xenova_drive_RouteCore *b){
 return !memcmp(a->route_id.bytes,b->route_id.bytes,16)&&a->purpose==b->purpose&&
  !memcmp(a->goal_sha256.bytes,b->goal_sha256.bytes,32)&&!memcmp(a->topology_sha256.bytes,b->topology_sha256.bytes,32)&&
  !memcmp(a->global_plan_sha256.bytes,b->global_plan_sha256.bytes,32)&&!memcmp(a->geometry_scope_sha256.bytes,b->geometry_scope_sha256.bytes,32)&&
  a->started_pi_us==b->started_pi_us&&a->deadline_pi_us==b->deadline_pi_us&&
  !memcmp(a->meter_epoch.bytes,b->meter_epoch.bytes,16)&&a->meter_baseline_total_ticks==b->meter_baseline_total_ticks&&
  a->max_travel_m==b->max_travel_m&&a->meter_gap_revision==b->meter_gap_revision&&a->metres_per_tick_upper==b->metres_per_tick_upper&&
  a->max_laps==b->max_laps&&!memcmp(a->parking_attempt_id.bytes,b->parking_attempt_id.bytes,16)&&
  !memcmp(a->parking_goal_sha256.bytes,b->parking_goal_sha256.bytes,32)&&!memcmp(a->predecessor_episode_sha256.bytes,b->predecessor_episode_sha256.bytes,32)&&
  !memcmp(a->budget_profile_sha256.bytes,b->budget_profile_sha256.bytes,32);
}
enum drp_result drg_sync(struct drg_owner *owner,const xenova_drive_RouteSnapshot *s){
 if(!owner||!s||dw_route_validate(s,xenova_drive_RouteSnapshot_fields)!=DW_OK||s->state!=2||!s->core.started_pi_us)return DRP_CONTEXT;
 const xenova_drive_RouteCore *c=&s->core;
 if(owner->bound&&(!same_birth(&owner->original,c)||c->revision<owner->original.revision||c->phase_revision<owner->phase_revision))return DRP_CONTEXT;
 if(owner->bound&&c->phase_revision==owner->phase_revision){
  if(c->phase_started_pi_us!=owner->phase_started||c->phase_expires_pi_us!=owner->phase_end||c->progress_mode!=owner->original.progress_mode||
     c->phase_goal.x!=owner->original.phase_goal.x||c->phase_goal.y!=owner->original.phase_goal.y||c->phase_goal.yaw!=owner->original.phase_goal.yaw)return DRP_CONTEXT;
  if(memcmp(owner->original.local_path_sha256.bytes,c->local_path_sha256.bytes,32)){owner->path_metric_pending=true;
   owner->original.local_path_sha256=c->local_path_sha256;}
  owner->original.revision=c->revision;return DRP_OK;
 }
 if(owner->bound&&(c->phase!=owner->original.phase+1||c->phase_started_pi_us<owner->phase_started))return DRP_CONTEXT;
 if(!owner->bound){owner->original=*c;owner->bound=true;}
 owner->phase_revision=c->phase_revision;owner->phase_started=c->phase_started_pi_us;owner->phase_end=c->phase_expires_pi_us;
 owner->original.revision=c->revision;owner->original.phase=c->phase;
 owner->original.progress_mode=c->progress_mode;owner->original.phase_goal=c->phase_goal;
 owner->anchor=s->progress.last_progress_pi_us>c->phase_started_pi_us?s->progress.last_progress_pi_us:c->phase_started_pi_us;
 owner->wait_baseline=s->progress.confirmed_stop_wait_us;
 owner->occurrence_anchor=s->progress.occurrence_index;owner->station_anchor=s->progress.station_lower_m;
 owner->remaining_anchor=s->progress.remaining_upper_m;
 owner->path_metric_pending=true;
 return DRP_OK;
}
enum drp_result drg_meter(const xenova_drive_RouteCore *c,const xenova_drive_DriveControlFacts *f,
 const dw_vehicle *v,uint64_t now,uint64_t *used,double *metres){
 if(!c||!f||!v||!used||!metres||dw_route_validate(c,xenova_drive_RouteCore_fields)!=DW_OK||
    dw_control_facts_validate(f,v)!=DW_OK||!f->meter.valid||now>=f->valid_until_pi_us||
    now<c->started_pi_us||now>=c->deadline_pi_us||now<c->phase_started_pi_us||now>=c->phase_expires_pi_us)return DRP_EXPIRED;
 if(memcmp(c->meter_epoch.bytes,f->meter.meter_epoch.bytes,16)||c->meter_gap_revision!=f->meter.gap_revision||
    c->metres_per_tick_upper!=f->meter.metres_per_tick||f->meter.total_ticks<c->meter_baseline_total_ticks)return DRP_CONTEXT;
 uint64_t ticks=f->meter.total_ticks-c->meter_baseline_total_ticks;
 if(ticks>UINT64_C(9007199254740992))return DRP_PROGRESS;
 double upper=(double)ticks*c->metres_per_tick_upper;if(ticks)upper=nextafter(upper,INFINITY);
 if(!isfinite(upper)||upper>c->max_travel_m)return DRP_EXPIRED;
 *used=ticks;*metres=upper;return DRP_OK;
}
static bool matching_stop(const xenova_drive_RouteCore *core,const xenova_drive_DriveControlFacts *f,const dw_vehicle *v,
 uint64_t now,const struct drg_limits *l){
 if(!f->has_stop||dw_control_facts_validate(f,v)!=DW_OK||!f->meter.valid||now>=f->valid_until_pi_us)return false;
 const xenova_drive_StopFacts *s=&f->stop;
 return core->stop_wait_command_sequence&&s->state==3&&s->producer_role==1&&s->command_sequence==core->stop_wait_command_sequence&&
  s->phase_revision==core->phase_revision&&now<s->valid_until_pi_us&&s->left_speed_bound_mps<=l->stationary_speed&&
  s->right_speed_bound_mps<=l->stationary_speed&&s->yaw_rate_bound_rps<=l->yaw_rate;
}
static bool tracker_remaining(const struct lp_path *p,const struct lp_tracker *t,const dw_vehicle *v,float *out){
 unsigned count=p->execution.present&&p->execution.segments_count?p->execution.segments_count:1;
 if(p->count<2||p->count>LP_PATH||count>3||t->segment>=count||!isfinite(t->progress)||t->progress<0)return false;
 double remaining=0;
 for(unsigned part=t->segment;part<count;part++){
  unsigned first=0,last=(unsigned)p->count-1;
  if(p->execution.present&&p->execution.segments_count){first=p->execution.segments[part].first_index;last=p->execution.segments[part].last_index;}
  if(first>=last||last>=p->count)return false;
  double length=0;
  for(unsigned i=first+1;i<=last;i++){
   if(!dw_pose_finite(p->points[i].pose))return false;
   length+=hypot((double)p->points[i].pose.x-p->points[i-1].pose.x,(double)p->points[i].pose.y-p->points[i-1].pose.y);
  }
  if(!isfinite(length)||length<=0||(part==t->segment&&t->progress>length))return false;
  remaining+=part==t->segment?length-t->progress:length;
 }
 remaining+=v->position_error_m;
 if(!isfinite(remaining)||remaining>FLT_MAX)return false;
 *out=nextafterf((float)remaining,INFINITY);return true;
}
enum drp_result drg_update(struct drg_owner *owner,const xenova_drive_RouteSnapshot *s,const xenova_drive_RoutePlan *p,
 const xenova_drive_RoadTopology *t,const struct drg_sample *sample,const struct drg_limits *l,xenova_drive_RouteProgress *out){
 if(!sample||!sample->vehicle||!sample->facts||!out||!limits(l)||drg_sync(owner,s)!=DRP_OK||drp_plan_validate(p,t)!=DRP_OK||
    !sample->calculation_sequence||s->progress.occurrence_index>=p->occurrences_count)return DRP_INPUT;
 uint8_t hash[32];if(!dw_route_record_hash(7,p,hash)||memcmp(hash,s->core.global_plan_sha256.bytes,32))return DRP_CONTEXT;
 uint64_t used=0;double travelled=0;enum drp_result rc=drg_meter(&s->core,sample->facts,sample->vehicle,sample->now_pi_us,&used,&travelled);
 if(rc!=DRP_OK)return rc;
 (void)travelled;
 if(used<s->progress.used_travel_ticks||sample->vehicle->sequence<s->progress.vehicle_sequence||!sample->tracker||!sample->path||
    !sample->tracker->initialized||sample->tracker->last_pi_us!=sample->vehicle->pi_sample_us||sample->tracker->index>=sample->path->count||
    !isfinite(sample->remaining_upper_m)||sample->remaining_upper_m<0)return DRP_PROGRESS;
 unsigned occurrence=s->progress.occurrence_index;const xenova_drive_RouteOccurrence *old=&p->occurrences[occurrence];
 struct drp_location actual={old->section_index,old->lane_id,s->progress.station_lower_m,s->progress.station_upper_m};
 if(s->core.progress_mode==1){
  if(!sample->view||!sample->section)return DRP_CONTEXT;
  rc=drr_project(sample->view,sample->section,t,sample->section_index,sample->map_pose,sample->vehicle->position_error_m,&actual);
  if(rc!=DRP_OK)return rc;
 }
 double increment=(double)(used-s->progress.used_travel_ticks)*s->core.metres_per_tick_upper;
 if(used>s->progress.used_travel_ticks)increment=nextafter(increment,INFINITY);
 bool crossed=actual.section!=old->section_index||actual.lane!=old->lane_id;
 if(crossed){
  if(occurrence+1>=p->occurrences_count||old->edge_index>=t->edges_count||!sample->crossing)return DRP_PROGRESS;
  const xenova_drive_RoadEdge *edge=&t->edges[old->edge_index];const xenova_drive_RouteOccurrence *next=&p->occurrences[occurrence+1];
  if(dw_route_validate(sample->crossing,xenova_drive_RoadConnection_fields)!=DW_OK||
     !dw_route_record_hash(4,sample->crossing,hash)||memcmp(hash,edge->connection_sha256.bytes,32)||
     actual.section!=next->section_index||actual.lane!=next->lane_id||
     actual.lower<next->entry_station_m||
     fmax(0,(double)old->exit_station_m-s->progress.station_upper_m)+
      fmax(0,(double)actual.lower-next->entry_station_m)+(edge->kind==2?(double)edge->to_station_m-edge->from_station_m:0)>increment||
     sample->vehicle->sequence==s->progress.vehicle_sequence)return DRP_PROGRESS;
  occurrence++;
 }else if((double)actual.lower-s->progress.station_upper_m>increment)return DRP_PROGRESS;
 const xenova_drive_RouteOccurrence *current=&p->occurrences[occurrence];
 if(actual.upper>current->exit_station_m+sample->vehicle->position_error_m||actual.upper<current->entry_station_m)return DRP_PROGRESS;
 if(s->progress.revision==UINT64_MAX)return DRP_PROGRESS;
 *out=s->progress;out->used_travel_ticks=used;out->vehicle_sequence=sample->vehicle->sequence;
 out->calculation_sequence=sample->calculation_sequence;out->revision++;out->remaining_upper_m=sample->remaining_upper_m;
 if(s->core.progress_mode==2){
  if(sample->path->goal.x!=s->core.phase_goal.x||sample->path->goal.y!=s->core.phase_goal.y||sample->path->goal.yaw!=s->core.phase_goal.yaw||
     !tracker_remaining(sample->path,sample->tracker,sample->vehicle,&out->remaining_upper_m))return DRP_CONTEXT;
  if(owner->path_metric_pending){owner->remaining_anchor=out->remaining_upper_m;owner->path_metric_pending=false;}
 }
 out->occurrence_index=occurrence;out->station_lower_m=crossed?actual.lower:fmaxf(out->station_lower_m,actual.lower);
 out->station_upper_m=fmaxf(out->station_lower_m,actual.upper);
 if(crossed&&t->edges[old->edge_index].closes_circuit)out->completed_laps++;
 if(out->completed_laps>s->core.max_laps)return DRP_PROGRESS;
 if(matching_stop(&s->core,sample->facts,sample->vehicle,sample->now_pi_us,l)){
  const xenova_drive_StopFacts *stop=&sample->facts->stop;
  uint64_t low=stop->stationary_started_pi_us;
  if(low<s->core.stop_wait_started_pi_us)low=s->core.stop_wait_started_pi_us;
  if(low<out->stop_wait_hwm_pi_us)low=out->stop_wait_hwm_pi_us;
  if(low<owner->anchor)low=owner->anchor;
  uint64_t high=stop->stationary_observed_pi_us;
  if(high>sample->now_pi_us)high=sample->now_pi_us;
  if(high>low){if(high-low>UINT64_MAX-out->confirmed_stop_wait_us)return DRP_PROGRESS;
   out->confirmed_stop_wait_us+=high-low;out->stop_wait_hwm_pi_us=high;}
 }
 uint64_t anchor=out->last_progress_pi_us>s->core.phase_started_pi_us?out->last_progress_pi_us:s->core.phase_started_pi_us;
 if(owner->anchor!=anchor){owner->anchor=anchor;owner->wait_baseline=out->confirmed_stop_wait_us;
  owner->occurrence_anchor=s->progress.occurrence_index;owner->station_anchor=s->progress.station_lower_m;owner->remaining_anchor=s->progress.remaining_upper_m;}
 if(sample->now_pi_us<anchor||out->confirmed_stop_wait_us<owner->wait_baseline)return DRP_PROGRESS;
 uint64_t elapsed=sample->now_pi_us-anchor,credit=out->confirmed_stop_wait_us-owner->wait_baseline;
 if(credit>elapsed||elapsed-credit>=l->progress_us)return DRP_PROGRESS;
 bool progress=sample->vehicle->sequence>s->progress.vehicle_sequence&&(out->occurrence_index>owner->occurrence_anchor||
  (s->core.progress_mode==1?out->station_lower_m-owner->station_anchor>=l->progress_m:
   owner->remaining_anchor-out->remaining_upper_m>=l->progress_m));
 uint64_t progress_stamp=sample->vehicle->pi_sample_us;
 if(sample->vehicle->clock_error_us>=progress_stamp)return DRP_PROGRESS;
 progress_stamp-=sample->vehicle->clock_error_us;
 if(progress&&progress_stamp>owner->anchor){out->last_progress_pi_us=progress_stamp;owner->anchor=progress_stamp;owner->wait_baseline=out->confirmed_stop_wait_us;
  owner->occurrence_anchor=out->occurrence_index;owner->station_anchor=out->station_lower_m;owner->remaining_anchor=out->remaining_upper_m;}
 return DRP_OK;
}
enum drp_result drg_terminal(const xenova_drive_RouteSnapshot *s,const xenova_drive_RoutePlan *p,const xenova_drive_StartGoal *goal,
 const xenova_drive_MissionGrant *g,const struct drr_view *view,dw_pose pose,const struct drg_sample *sample,
 const struct lp_command *command,const struct lp_config *c,const struct drg_limits *l,struct lp_metrics *m,const struct lp_budget *budget){
 uint8_t hash[32];
 if(!s||!p||!goal||!g||!view||!sample||!sample->vehicle||!sample->facts||!command||!c||!m||!limits(l)||
    dw_route_validate(s,xenova_drive_RouteSnapshot_fields)!=DW_OK||dw_route_validate(p,xenova_drive_RoutePlan_fields)!=DW_OK||
    dw_route_grant_validate(g)!=DW_OK||!view->world->has_scene||
    !dw_pose_finite(pose)||s->state!=2||s->core.phase!=DW_ROUTE_FINAL_STOP||!p->occurrences_count||
    s->progress.occurrence_index+1!=p->occurrences_count||g->kind!=3||!g->admitted||!g->has_route_binding||
    dw_route_validate(goal,xenova_drive_StartGoal_fields)!=DW_OK||!dw_route_record_hash(1,goal,hash)||
    memcmp(p->terminal_profile_sha256.bytes,goal->terminal_profile_sha256.bytes,32)||
    memcmp(hash,p->goal_sha256.bytes,32)||memcmp(hash,s->core.goal_sha256.bytes,32)||memcmp(hash,g->route_binding.goal_sha256.bytes,32)||
    !dw_route_record_hash(7,p,hash)||memcmp(hash,s->core.global_plan_sha256.bytes,32)||memcmp(hash,g->route_binding.global_plan_sha256.bytes,32)||
    memcmp(p->route_id.bytes,s->core.route_id.bytes,16)||memcmp(p->route_id.bytes,g->route_binding.route_id.bytes,16)||
    g->route_binding.revision!=s->core.revision||
    s->core.phase_goal.x!=goal->rear_axle_pose.x||s->core.phase_goal.y!=goal->rear_axle_pose.y||s->core.phase_goal.yaw!=goal->rear_axle_pose.yaw||
    !dw_route_core_hash(&s->core,hash)||memcmp(hash,g->route_binding.core_sha256.bytes,32)||
    !sample->path||sample->path->goal.x!=goal->rear_axle_pose.x||sample->path->goal.y!=goal->rear_axle_pose.y||
    sample->path->goal.yaw!=goal->rear_axle_pose.yaw||command->result!=LP_STOP||command->gate_required)return DRP_CONTEXT;
 const dw_vehicle *v=sample->vehicle;
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin),pad=v->position_error_m+radius*v->heading_error_rad;
 if(hypotf(pose.x-goal->rear_axle_pose.x,pose.y-goal->rear_axle_pose.y)+v->position_error_m+goal->position_error_m>c->goal_distance||
    fabsf(dw_angle(pose.yaw-goal->rear_axle_pose.yaw))+v->heading_error_rad+goal->heading_error_rad>c->goal_heading||
    view->intent.selected_lane_id!=goal->lane_id||!tr_contains(&view->road,pose,pad+view->intent.vehicle_pad,c)||
    !matching_stop(&s->core,sample->facts,v,sample->now_pi_us,l))return DRP_STOP_REQUIRED;
 const xenova_drive_StopFacts *stop=&sample->facts->stop;
 if(stop->control_generation!=g->control_generation||memcmp(stop->source_vm.bytes,g->owner_vm.bytes,16)||
    memcmp(stop->source_execution.bytes,g->owner_execution.bytes,16)||memcmp(stop->source_app_instance.bytes,g->app_instance.bytes,16)||
    memcmp(stop->mission_id.bytes,g->mission_id.bytes,16)||stop->mission_revision!=g->mission_revision||stop->grant_revision!=g->grant_revision||
    stop->plan_revision!=sample->path->revision||view->world->scene.admission_revision!=g->scene_revision||
    memcmp(view->world->scene.admission_id.bytes,g->scene_admission_id.bytes,16))return DRP_STOP_REQUIRED;
 uint64_t used=0;double travelled=0;enum drp_result checked=drg_meter(&s->core,sample->facts,v,sample->now_pi_us,&used,&travelled);
 if(checked!=DRP_OK)return checked;
 enum lp_result body=lp_footprint(view->world,pose,pad+view->intent.vehicle_pad,c,m,budget);
 return body==LP_PATH_FOUND?DRP_OK:body==LP_DEADLINE?DRP_DEADLINE:DRP_WINDOW;
}
