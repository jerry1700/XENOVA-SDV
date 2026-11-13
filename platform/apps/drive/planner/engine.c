/* SPDX-License-Identifier: Apache-2.0 */
#include "engine.h"
#include <string.h>
#include <math.h>

static bool take(struct drive_engine *e){
 if(!atomic_flag_test_and_set_explicit(&e->lock,memory_order_acquire))return true;
 atomic_fetch_add(&e->resource_contention,1);return false;
}
static void give(struct drive_engine *e){atomic_flag_clear_explicit(&e->lock,memory_order_release);}
static enum de_result behavior_prepare(struct drive_engine *,const dw_world *,dw_pose,const dw_vehicle *,uint64_t,float,struct tr_intent *,dw_pose *);
static enum de_result behavior_candidate(struct drive_engine *,struct lp_path *,const dw_world *,uint64_t);
static bool grant_same(const xenova_drive_MissionGrant *,const xenova_drive_MissionGrant *);
static bool grant_key_same(const xenova_drive_MissionGrant *,const xenova_drive_MissionGrant *);
static enum de_result parking_poll(struct drive_engine *,const dw_world *,dw_pose,const dw_vehicle *,uint64_t);
static enum de_result parking_start(struct drive_engine *,struct dw_handle,const struct dw_snapshot *,dw_pose,const dw_vehicle *,uint64_t,struct lp_budget);
static enum de_result parking_slice(struct drive_engine *,unsigned,uint64_t);
static void parking_metadata(struct drive_engine *,struct lp_path *,const xenova_drive_MissionGrant *);
static bool behavior_restore(struct drive_engine *,const struct lp_path *,bool);
static void source_context(const dw_world *,xenova_drive_EpisodeSourceContext *);
static void behavior_uuid(char text[37],const uint8_t bytes[16]);
static enum de_result control_facts_current(struct drive_engine *,const xenova_drive_DriveControlFacts *,uint64_t,struct db_facts *);
static bool readers(const struct de_path_slot *s){for(unsigned i=0;i<DE_READERS;i++)if(s->leases[i])return true;return false;}
void de_init(struct drive_engine *e,const struct lp_config *c,const struct lp_assets *a){
 memset(e,0,sizeof(*e));atomic_flag_clear(&e->lock);dw_pool_init(&e->maps);dw_odom_init(&e->odometry);
 e->config=c;e->assets=a;e->current=-1;e->epoch=1;e->last_reason=DE_WAITING;
}
static enum de_result reserve(struct drive_engine *e,struct de_reference *h,struct lp_path **path){
 if(!take(e))return DE_BUSY;
 enum de_result rc=DE_BUSY;
 if(e->stopping)rc=DE_CANCELLED;
 else if(e->next_generation==UINT32_MAX)rc=DE_LIMIT;
 else for(unsigned i=0;i<DE_PATH_SLOTS;i++)if((int)i!=e->current&&!readers(&e->paths[i])&&!e->paths[i].writing){
  struct de_path_slot *slot=&e->paths[i];slot->writing=true;slot->generation=++e->next_generation;slot->scene_generation=0;
  slot->tracking_prepared=false;
  *h=(struct de_reference){i,slot->generation,0};*path=&slot->path;rc=DE_OK;
  unsigned used=0;
  for(unsigned j=0;j<DE_PATH_SLOTS;j++)if((int)j==e->current||e->paths[j].writing||readers(&e->paths[j]))used++;
  if(used>e->path_peak)e->path_peak=used;
  break;
 }
 if(rc==DE_BUSY)e->path_exhausted++;
 give(e);return rc;
}
/* Only the reserving writer touches this slot until it becomes current. */
static bool prepare_tracking(struct drive_engine *e,struct de_reference h){
 if(h.slot>=DE_PATH_SLOTS)return false;
 struct de_path_slot *slot=&e->paths[h.slot];
 if(slot->generation!=h.generation||!slot->writing)return false;
 if(!slot->tracking_prepared)slot->tracking_prepared=lp_prepare_tracking(&slot->path,e->config,&slot->tracking_geometry);
 return slot->tracking_prepared;
}
static enum lp_result validate_slot(struct de_path_slot *slot,const dw_world *world,const struct lp_config *config,
 struct lp_metrics *metrics,const struct lp_budget *budget){
 if(slot->tracking_prepared)
  return lp_recheck_path(&slot->path,&slot->tracking_geometry,world,config,metrics,budget);
 enum lp_result result=lp_validate_path_prepared(&slot->path,world,config,metrics,budget,&slot->tracking_geometry);
 slot->tracking_prepared=result==LP_PATH_FOUND;
 return result;
}
/* Only the reserving writer can abandon an unpublished slot. Once it clears
 * writing, it must stop accessing the slot so the next reservation can use it. */
static void discard_path(struct drive_engine *e,struct de_reference h){
 if(!h.lease&&h.slot<DE_PATH_SLOTS&&e->paths[h.slot].generation==h.generation)
  atomic_store_explicit(&e->paths[h.slot].writing,false,memory_order_release);
}
static enum de_result publish(struct drive_engine *e,struct de_reference h,bool valid){
 if(valid)valid=prepare_tracking(e,h);
 if(!take(e))return DE_BUSY;
 enum de_result rc=DE_CHANGED;
 if(h.slot<DE_PATH_SLOTS&&e->paths[h.slot].generation==h.generation&&e->paths[h.slot].writing){
  if(valid&&!e->stopping){e->current=(int)h.slot;e->path_invalidated=false;rc=DE_OK;
   if(e->behavior_enabled)atomic_store(&e->snapshot_requested_generation,h.generation);}
  e->paths[h.slot].writing=false;
 }
 give(e);return rc;
}
enum de_result de_path_acquire(struct drive_engine *e,struct de_reference *h,const struct lp_path **out){
 if(!take(e))return DE_BUSY;
 enum de_result rc=DE_WAITING;
 if(e->current>=0&&!e->stopping){
  struct de_path_slot *s=&e->paths[e->current];
  /* Fixed product read owners, each releases before its next acquire. */
  rc=DE_BUSY;
  for(unsigned k=0;k<DE_READERS;k++)if(!s->leases[k]&&e->next_lease<UINT32_MAX){
   s->leases[k]=++e->next_lease;*h=(struct de_reference){(unsigned)e->current,s->generation,e->next_lease};*out=&s->path;rc=DE_OK;break;
  }
 }
 give(e);return rc;
}
enum de_result de_path_release(struct drive_engine *e,struct de_reference h){
 if(h.slot<DE_PATH_SLOTS&&e->paths[h.slot].generation==h.generation&&h.lease){
  for(unsigned k=0;k<DE_READERS;k++){unsigned expected=h.lease;
   if(atomic_compare_exchange_strong(&e->paths[h.slot].leases[k],&expected,0))return DE_OK;
  }
 }
 return DE_CHANGED;
}
enum de_result de_world(struct drive_engine *e,const uint8_t *bytes,size_t size,uint64_t now){
 if(de_receive_drain(e))return DE_BUSY;
 struct dw_handle h;struct dw_snapshot *snapshot;
 enum dw_result rc=dw_reserve(&e->maps,&h,&snapshot);if(rc)return rc==DW_BUSY?DE_BUSY:DE_MAP;
 /* The receive owner also owns trust updates, so the trust is stable during
  * decode. source_revision still guards the final publication. */
 struct dw_trust trust;rc=dw_pool_trust(&e->maps,&trust);
 if(!rc)rc=dw_decode(bytes,size,&trust,now,snapshot);
 if(!rc)rc=dw_scene_time(&e->maps,&snapshot->world,now,NULL,&snapshot->scene_generation);
 e->last_world_result=rc;
 if(rc){
  if(dw_abort(&e->maps,h)==DW_BUSY){e->receive_pending=true;e->receive_handle=h;}
  return rc==DW_BUSY?DE_BUSY:DE_MAP;
 }
 lp_index_build(&e->map_indices[h.slot],&snapshot->world,e->config);
 e->receive_awaiting=true;e->receive_handle=h;e->receive_snapshot=snapshot;
 (void)de_receive_poll(e,now);
 return DE_OK;
}
enum de_result de_receive_drain(struct drive_engine *e){
 if(!e->receive_pending&&!e->receive_awaiting)return DE_OK;
 if(dw_abort(&e->maps,e->receive_handle)==DW_BUSY)return DE_BUSY;
 e->receive_pending=false;e->receive_awaiting=false;return DE_OK;
}
enum de_result de_vehicle(struct drive_engine *e,const dw_vehicle *vehicle){
 enum dw_result rc=dw_odom_add(&e->odometry,vehicle);
 return !rc?DE_OK:rc==DW_BUSY?DE_BUSY:DE_POSE;
}
static bool grant_primitive(const xenova_drive_MissionGrant *grant,const char *expected){
 static const char hex[]="0123456789abcdef";
 if(strlen(expected)!=64)return false;
 for(size_t i=0;i<32;i++)if(expected[2*i]!=hex[grant->primitive.bytes[i]>>4]||expected[2*i+1]!=hex[grant->primitive.bytes[i]&15])return false;
 return true;
}
enum de_result de_mission(struct drive_engine *e,const xenova_drive_MissionGrant *grant,uint64_t now){
 if(!grant){
  if(!e->has_grant)return DE_OK;
  e->parking.publish_source=NULL;
  atomic_store(&e->parking_stop_generation,0);
  e->has_grant=false;bool dependent=false;
  if(e->planning&&e->planning_has_grant){e->search_reason=LP_CANCELLED;dependent=true;}
  if(e->current>=0&&e->paths[e->current].path.execution.present&&
     e->paths[e->current].path.execution.mission.kind){e->path_invalidated=true;dependent=true;}
  if(e->apply_pending&&e->paths[e->apply_path.slot].path.execution.present&&
     e->paths[e->apply_path.slot].path.execution.mission.kind){e->apply_valid=false;dependent=true;}
  if(!dependent)return DE_OK;
  if(e->requested==UINT32_MAX)return DE_LIMIT;
  atomic_fetch_add(&e->requested,1);return DE_OK;
 }
 if(dw_mission_grant_validate(grant)||grant->issued_pi_us>now||strcmp(grant->configuration,e->config->id)||
    strcmp(grant->calibration,e->config->calibration)||strcmp(grant->vehicle,e->config->vehicle)||
    !grant_primitive(grant,e->assets->sha256))return DE_CONFIGURATION;
 bool changed=!e->has_grant||!grant_key_same(&e->grant,grant);
 if(changed&&e->requested==UINT32_MAX)return DE_LIMIT;
 bool new_intent=e->has_grant&&(e->grant.mission_revision!=grant->mission_revision||memcmp(e->grant.mission_id.bytes,grant->mission_id.bytes,16));
 if(new_intent&&e->behavior_enabled){
  atomic_store(&e->parking_stop_generation,0);
  memset(&e->episode,0,sizeof(e->episode));memset(&e->episode_state,0,sizeof(e->episode_state));
  memset(&e->parking,0,sizeof(e->parking));e->episode_transition=e->episode_request_ready=e->episode_receipt_pending=false;
  e->episode_pass_planning=false;
  e->phase_state_message_id[0]=0;
  e->behavior_failed=false;e->parking_boundary_pending=false;
 }
 e->grant=*grant;e->has_grant=true;
 if(grant->kind!=2)e->parking.publish_source=NULL;
 if(changed&&e->current>=0&&e->paths[e->current].path.execution.present&&
    !lp_mission_matches(&e->paths[e->current].path,grant))e->path_invalidated=true;
 if(e->behavior_enabled&&grant->kind==2&&(!e->parking.active||
    memcmp(e->parking.original_grant.mission_id.bytes,grant->mission_id.bytes,16)||e->parking.original_grant.mission_revision!=grant->mission_revision)){
  memset(&e->parking,0,sizeof(e->parking));e->parking.active=true;e->parking.phase=DP_SEARCH;e->parking.phase_revision=1;
  e->parking.original_grant=*grant;
 }
 if(changed)atomic_fetch_add(&e->requested,1);
 return DE_OK;
}
static void mission_metadata(struct lp_path *path,const xenova_drive_MissionGrant *g){
 struct lp_execution *out=&path->execution;pb_size_t geometry_count=out->segments_count;
 xenova_drive_GearSegment geometry[3];memcpy(geometry,out->segments,geometry_count*sizeof(geometry[0]));
 memset(out,0,sizeof(*out));out->present=true;
 out->features=DW_STATE_FEATURES|(path->task.active?DW_TASK_REFERENCE:0)|(path->has_episode?DW_EPISODE:0)|
  (g->has_operation_binding?DW_OPERATION:0);
 xenova_drive_MissionExecution *m=&out->mission;
 m->mission_id.size=m->operator_request_nonce.size=m->scene_admission_id.size=16;
 memcpy(m->mission_id.bytes,g->mission_id.bytes,16);memcpy(m->operator_request_nonce.bytes,g->operator_request_nonce.bytes,16);
 memcpy(m->scene_admission_id.bytes,g->scene_admission_id.bytes,16);
 m->mission_revision=g->mission_revision;m->kind=g->kind;m->phase=g->kind==1&&g->admitted?10:0;
 m->grant_revision=g->grant_revision;m->cancel_fence_revision=g->cancel_fence_revision;m->scene_admission_revision=g->scene_revision;
 m->has_operation_binding=g->has_operation_binding;if(g->has_operation_binding)m->operation_binding=g->operation_binding;
 /* 새 parking phase/goal은 만들지 않는다. 후속 parking 계산 owner가 생성한다. */
 float travel=0;for(size_t i=1;i<path->count;i++)travel+=hypotf(path->points[i].pose.x-path->points[i-1].pose.x,
                                                          path->points[i].pose.y-path->points[i-1].pose.y);
 if(geometry_count){out->segments_count=geometry_count;memcpy(out->segments,geometry,geometry_count*sizeof(geometry[0]));}
 else if(travel>0){out->segments_count=1;out->segments[0]=(xenova_drive_GearSegment){0,(uint32_t)path->count-1,1};}
}
static bool scene_mission(const dw_world *w,const xenova_drive_MissionGrant *g){
 return g&&g->admitted&&g->scene_revision==w->scene.admission_revision&&
  !memcmp(g->scene_admission_id.bytes,w->scene.admission_id.bytes,16);
}
static enum de_result inputs(struct drive_engine *e,const struct dw_snapshot *map,uint64_t now,dw_pose *pose,dw_vehicle *vehicle,uint64_t *deadline){
 if(dw_snapshot_time(map,now,(uint64_t)e->config->live_age_us,deadline))return DE_EXPIRED;
 uint64_t scene_end;enum dw_result scene=dw_scene_time(&e->maps,&map->world,now,&scene_end,NULL);
 if(scene)return scene==DW_BUSY?DE_BUSY:DE_CHANGED;
 if(deadline&&*deadline>scene_end)*deadline=scene_end;
 enum dw_result rc=dw_current_pose(&map->world,&e->odometry,now,(uint64_t)e->config->pose_age_us,pose,vehicle);
 if(rc)return rc==DW_BUSY?DE_BUSY:DE_POSE;
 if(map->world.required_features&DW_LOCALIZATION_BOUNDS){
  const xenova_drive_MapLocalization *loc=&map->world.localization;
  if(!map->world.has_localization||loc->quality!=1||!loc->valid_for_us||map->observed_upper_age_us>map->received_us)return DE_POSE;
  uint64_t start=map->received_us-map->observed_upper_age_us;
  if(loc->observed_mapper_us>=map->world.observed_us){
   uint64_t delta=loc->observed_mapper_us-map->world.observed_us;if(delta>UINT64_MAX-start)return DE_CLOCK;start+=delta;
  }else{uint64_t delta=map->world.observed_us-loc->observed_mapper_us;if(delta>start)return DE_CLOCK;start-=delta;}
  if(loc->valid_for_us>UINT64_MAX-start||now<start||now>=start+loc->valid_for_us)return DE_EXPIRED;
  if(deadline&&*deadline>start+loc->valid_for_us)*deadline=start+loc->valid_for_us;
  float age=(float)((double)(now-start)/1000000.);
  vehicle->position_error_m+=loc->anchor_position_bound_m+loc->geometry_position_bound_m+age*loc->position_drift_bound_mps;
  vehicle->heading_error_rad+=loc->anchor_heading_bound_rad+loc->geometry_heading_bound_rad+age*loc->heading_drift_bound_rps;
 }
 if(!isfinite(vehicle->position_error_m)||!isfinite(vehicle->heading_error_rad)||
    vehicle->position_error_m>e->config->max_position_error||vehicle->heading_error_rad>e->config->max_heading_error)return DE_POSE;
 return DE_OK;
}
static enum de_result input_preflight(struct drive_engine *e,uint64_t now,struct lp_budget budget,struct de_input_check *out,bool geometry){
 if(!e||!out)return DE_INPUT;
 memset(out,0,sizeof(*out));
 if(e->stopping)return DE_CANCELLED;
 struct dw_handle handle;const struct dw_snapshot *map;
 enum dw_result acquired=dw_acquire(&e->maps,&handle,&map);
 if(acquired)return acquired==DW_BUSY?DE_BUSY:DE_MAP;
 uint64_t end=UINT64_MAX;
 enum de_result rc=inputs(e,map,now,&out->pose,&out->vehicle,&end);
 if(!rc){
  enum dw_result scene=dw_scene_time(&e->maps,&map->world,now,NULL,&out->scene_generation);
  if(scene)rc=scene==DW_BUSY?DE_BUSY:DE_CHANGED;
 }
 const struct lp_config *c=e->config;const dw_vehicle *v=&out->vehicle;
 float steering=v->has_measured_steering_rad?v->measured_steering_rad:v->has_estimated_steering_rad?v->estimated_steering_rad:NAN;
 if(!rc&&(!isfinite(v->velocity_mps)||v->velocity_mps< -c->max_reverse_velocity-.01f||v->velocity_mps>c->max_velocity+.01f||
   !v->has_steering_error_rad||!isfinite(v->steering_error_rad)||v->steering_error_rad<0||v->steering_error_rad>c->max_steering_error||
   !isfinite(steering)||fabsf(steering)+v->steering_error_rad>c->max_steer+.0001f))rc=DE_INPUT;
 if(!rc){
  uint64_t pose_age=(uint64_t)c->pose_age_us;
  if(v->pi_sample_us>UINT64_MAX-pose_age||v->clock_error_us>=v->pi_sample_us+pose_age)rc=DE_CLOCK;
  else{uint64_t vehicle_end=v->pi_sample_us+pose_age-v->clock_error_us;if(end>vehicle_end)end=vehicle_end;}
 }
 if(!rc&&geometry){
  float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
  double position=v->position_error_m,heading=v->heading_error_rad;
  if(map->world.has_localization){
   /* The immutable map/Vehicle body witness encloses localization drift only
    * until its original input E. It is not a predicted pose or a new sample. */
   double remaining=end>now?(double)(end-now)/1000000.:0.;
   position+=remaining*map->world.localization.position_drift_bound_mps;
   heading+=remaining*map->world.localization.heading_drift_bound_rps;
  }
  out->geometry_position_bound_m=nextafterf((float)position,INFINITY);
  out->geometry_heading_bound_rad=nextafterf((float)heading,INFINITY);
  float pad=out->geometry_position_bound_m+radius*out->geometry_heading_bound_rad+c->tracking_guard;
  enum lp_result checked=lp_footprint(&map->world,out->pose,pad,c,&out->metrics,&budget);
  if(checked!=LP_PATH_FOUND)rc=checked==LP_CLOCK?DE_CLOCK:checked==LP_CANCELLED?DE_CANCELLED:DE_PATH;
 }
 if(!rc){
  enum dw_result valid=dw_source_valid(&e->maps,handle);
  if(valid)rc=valid==DW_BUSY?DE_BUSY:DE_CHANGED;
  else if(end<=now)rc=DE_EXPIRED;
  else{source_context(&map->world,&out->source);out->valid_until_pi_us=end;out->map_generation=handle.generation;}
 }
 (void)dw_release(&e->maps,handle);
 return rc;
}
enum de_result de_input_preflight(struct drive_engine *e,uint64_t now,struct lp_budget budget,struct de_input_check *out){
 return input_preflight(e,now,budget,out,true);
}
enum de_result de_input_current(struct drive_engine *e,uint64_t now,struct de_input_check *out){
 return input_preflight(e,now,(struct lp_budget){0},out,false);
}
enum de_result de_receive_poll(struct drive_engine *e,uint64_t now){
 if(e->receive_pending)return de_receive_drain(e);
 if(!e->receive_awaiting)return DE_OK;
 dw_pose pose;dw_vehicle vehicle;
 enum de_result rc=inputs(e,e->receive_snapshot,now,&pose,&vehicle,NULL);
 if(rc==DE_EXPIRED||rc==DE_CHANGED||e->stopping){(void)de_receive_drain(e);return rc;}
 if(rc)return rc;
 enum dw_result published=dw_publish(&e->maps,e->receive_handle);
 e->last_world_result=published;
 if(published==DW_BUSY)return DE_BUSY;
 if(published){(void)de_receive_drain(e);return DE_MAP;}
 e->receive_awaiting=false;
 if(atomic_load(&e->requested)==UINT32_MAX)return DE_LIMIT;
 atomic_fetch_add(&e->requested,1);return DE_OK;
}
static void search_memory_publish(struct drive_engine *e){
 unsigned used=e->search.used;
 if(used>atomic_load(&e->search_peak))atomic_store(&e->search_peak,used);
 atomic_store(&e->search_used,used);
}
static enum de_result task_prepare(struct drive_engine *e,const dw_world *w,dw_pose pose,const dw_vehicle *vehicle,
 float clearance,struct lp_budget budget,struct tr_intent *task,struct lp_metrics *prefix,dw_pose *goal,uint64_t now){
 memset(task,0,sizeof(*task));
 if(!(w->required_features&DW_LANE_MODEL))return lp_reference_goal(w,pose,e->config->plan_horizon,clearance,goal)==LP_PATH_FOUND?DE_OK:DE_PATH;
 if(!tr_basis_accept(&e->basis,w))return DE_REFERENCE;
 float radius=hypotf(fmaxf(e->config->front,e->config->rear)+e->config->margin,e->config->half_width+e->config->margin);
 float pad=vehicle->position_error_m+radius*vehicle->heading_error_rad;
 uint32_t actual=tr_unique_lane(w,pose,pad+e->config->tracking_guard,e->config,budget.collision_index);
 *task=e->task;
 if(!task->active){if(!actual)return DE_POSE;*task=(struct tr_intent){true,false,actual,actual,actual,pad};}
 task->vehicle_pad=pad;
 struct tr_view view;if(!tr_open_indexed(w,task,budget.collision_index,&view))return DE_PATH;
 if(e->behavior_enabled)return behavior_prepare(e,w,pose,vehicle,now,clearance,task,goal);
 if(task->changing){
  if(actual==task->selected_lane_id){task->changing=false;task->from_lane_id=actual;}
  else if(!tr_contains(&view,pose,pad+e->config->tracking_guard,e->config))return DE_POSE;
 }else if(actual!=task->selected_lane_id)return DE_POSE;
 if(lp_task_goal(w,task,pose,e->config->plan_horizon,clearance,goal)!=LP_PATH_FOUND)return DE_PATH;
 if(!task->changing){bool blocked=false;
  enum lp_result tested=lp_task_obstructed(w,task,pose,*goal,e->config,budget,prefix,&blocked);
  if(tested!=LP_PATH_FOUND)return tested==LP_CLOCK?DE_CLOCK:DE_PATH;
  if(blocked){
   if(!tr_open(w,task,&view))return DE_PATH;
   uint32_t adjacent=view.selected->adjacent_lane_id;
   task->from_lane_id=task->selected_lane_id;task->selected_lane_id=adjacent;task->changing=true;
   if(!tr_open_indexed(w,task,budget.collision_index,&view)||!tr_contains(&view,pose,pad+e->config->tracking_guard,e->config)||
      lp_task_goal(w,task,pose,e->config->plan_horizon,clearance,goal)!=LP_PATH_FOUND)return DE_PATH;
  }
 }
 return DE_OK;
}

/* A registered, never-followed local path may outlive its search-time first
 * correspondence while the original receipt is in flight. Re-adoption changes
 * private bootstrap metadata only; initialized progress/permission never resets. */
static bool initial_correspondence_stale(const struct lp_path *p,const dw_vehicle *v,const struct lp_config *c){
 return p->initial_pi_us&&v->pi_sample_us>=p->initial_pi_us&&
  v->pi_sample_us-p->initial_pi_us>(uint64_t)c->pose_age_us;
}
/* Geometry alone does not prove a usable current tracker correspondence.
 * A stale correspondence takes the ordinary new-search/registration path; the
 * published tracker, original episode and its clocks are never reset here. */
static enum de_result correspondence_current(struct drive_engine *e,const struct lp_path *p,uint32_t generation,const dw_vehicle *v){
 uint64_t anchor=p->initial_pi_us;
 if(!take(e))return DE_BUSY;
 if(e->progress_generation==generation&&e->progress_snapshot.initialized)anchor=e->progress_snapshot.last_pi_us;
 give(e);
 if(!anchor)return DE_OK; /* existing explicit new-plan correspondence */
 return v->pi_sample_us>=anchor&&v->pi_sample_us-anchor<=(uint64_t)e->config->pose_age_us?DE_OK:DE_PATH;
}
static enum de_result initial_correspondence_unrun(struct drive_engine *e,const struct lp_path *p,uint32_t generation){
 if(p->origin.retained||p->initial_tracker_index||!p->execution.present||p->execution.mission.kind!=1||
    p->execution.segments_count!=1||p->execution.mission.active_segment||p->execution.segments[0].first_index||
    p->execution.segments[0].signed_direction!=1||(p->execution.has_direction_gate&&p->execution.direction_gate.state))return DE_STATE;
 if(!take(e))return DE_BUSY;
 bool used=e->progress_generation==generation&&(e->progress_snapshot.initialized||e->progress_snapshot.gate_revision||e->progress_snapshot.segment);
 give(e);return used?DE_STATE:DE_OK;
}
/* Tracker math owns a path lease. Commit only in its absence, under the same
 * metadata lock that admits a new lease; no live tracker fields race this read. */
static enum de_result initial_correspondence_publish(struct drive_engine *e,struct de_reference next,uint32_t original){
 if(!prepare_tracking(e,next))return publish(e,next,false);
 if(!take(e))return DE_BUSY;
 for(unsigned i=0;i<DE_PATH_SLOTS;i++)if(readers(&e->paths[i])){give(e);return DE_BUSY;}
 bool valid=e->current>=0&&e->paths[e->current].generation==original&&!e->stopping&&
  e->last_track_path.generation!=original&&
  !(e->tracker.path_token==original&&(e->tracker.initialized||e->tracker.gate_revision||e->tracker.segment||e->tracker.completed))&&
  !(e->progress_generation==original&&e->progress_snapshot.initialized);
 enum de_result rc=DE_CHANGED;
 if(next.slot<DE_PATH_SLOTS&&e->paths[next.slot].generation==next.generation&&e->paths[next.slot].writing){
  if(valid){e->current=(int)next.slot;e->path_invalidated=false;atomic_store(&e->snapshot_requested_generation,next.generation);rc=DE_OK;}
  else rc=DE_STATE;
  e->paths[next.slot].writing=false;
 }
 give(e);return rc;
}
static bool initial_correspondence_pending(const struct drive_engine *e,const struct lp_path *p){
 return e->behavior_enabled&&p->has_episode&&p->episode.core.state==2&&e->phase_state_message_id[0]&&
  e->episode_status.commit_state==1&&p->episode.acceptance.registry_revision==e->episode_status.registry_revision&&
  !memcmp(p->episode.acceptance.registry_epoch.bytes,e->episode_status.registry_epoch.bytes,16)&&
  !memcmp(p->episode.acceptance.accepted_core_sha256.bytes,e->episode_status.accepted_core_sha256.bytes,32);
}
/* A newly searched local path has no receipt yet. A slow bounded search may
 * outlive its start Vehicle; establish its first correspondence on current
 * inputs before creating a registration request, never on a published path. */
static enum lp_result planning_correspondence(struct drive_engine *e,struct de_path_slot *slot,const dw_world *world,
 dw_pose pose,const dw_vehicle *vehicle,uint64_t now,struct lp_tracker *probe,struct lp_command *command,
 struct lp_metrics *metrics,const struct lp_budget *budget){
 struct lp_path *path=&slot->path;
 const struct lp_tracking_geometry *geometry=&slot->tracking_geometry;
 bool stale=initial_correspondence_stale(path,vehicle,e->config);
 if(!stale)return lp_track_prepared(probe,path,geometry,world,pose,vehicle,e->config,.02f,command,metrics,budget,NULL);
 const struct db_episode *episode=&e->episode;const struct db_facts *facts=&e->facts;
 if(!e->behavior_enabled||!e->planning||e->planning_path.slot>=DE_PATH_SLOTS||
    path!=&e->paths[e->planning_path.slot].path||!e->paths[e->planning_path.slot].writing||
    path->has_episode||path->origin.retained||!path->task.active||path->space.parking||
    !episode->active||!e->episode_transition||e->behavior_failed||!e->has_grant||!lp_mission_matches(path,&e->grant)||
    now>=episode->deadline_pi_us||now>=episode->phase_deadline_pi_us||now<episode->phase_entered_pi_us||
    !e->facts_current||!facts->valid||!facts->meter_valid||now>=facts->expires_pi_us||
    memcmp(facts->meter_epoch,episode->meter_epoch,16)||facts->meter_revision!=episode->meter_revision||
    facts->metres_per_tick!=episode->metres_per_tick||facts->vehicle_sequence>vehicle->sequence||
    strcmp(facts->pi_session,vehicle->pi_session)||strcmp(facts->device_session,vehicle->device_session)||
    strcmp(facts->odom_epoch,vehicle->odom_epoch)||facts->meter_ticks<episode->meter_start_ticks||
    facts->meter_ticks-episode->meter_start_ticks<episode->meter_used_ticks)return LP_INPUT;
 double travel=db_travel_upper(facts,episode->meter_start_ticks,now,fmaxf(e->config->max_velocity,e->config->max_reverse_velocity));
 if(!isfinite(travel)||travel>=e->behavior_limits.travel_m)return LP_INPUT;
 float radius=hypotf(fmaxf(e->config->front,e->config->rear)+e->config->margin,e->config->half_width+e->config->margin);
 float enclosure=e->config->max_position_error+vehicle->position_error_m+
  radius*(e->config->max_heading_error+vehicle->heading_error_rad);
 if(hypotf(pose.x-e->search.start.x,pose.y-e->search.start.y)>enclosure||
    fabsf(dw_angle(pose.yaw-e->search.start.yaw))>e->config->max_heading_error+vehicle->heading_error_rad)return LP_INPUT;
 uint64_t original=path->initial_pi_us;size_t original_index=path->initial_tracker_index;
 path->initial_pi_us=0;path->initial_tracker_index=0;
 enum lp_result rc=lp_track_prepared(probe,path,geometry,world,pose,vehicle,e->config,.02f,command,metrics,budget,NULL);
 bool checked=(rc==LP_PATH_FOUND||rc==LP_STOP)&&!probe->index&&!probe->segment&&!probe->gate_revision&&probe->progress<=enclosure;
 if(!checked){path->initial_pi_us=original;path->initial_tracker_index=original_index;return rc==LP_PATH_FOUND||rc==LP_STOP?LP_INPUT:rc;}
 path->initial_pi_us=vehicle->pi_sample_us;path->initial_tracker_index=probe->index;
 memset(probe,0,sizeof(*probe));
 return lp_track_prepared(probe,path,geometry,world,pose,vehicle,e->config,.02f,command,metrics,budget,NULL);
}
/* Recognize the private metadata-only candidate produced below. Imported copies
 * and ordinary grant rebinds keep their existing restore path. */
static enum de_result initial_correspondence_candidate(struct drive_engine *e,const struct lp_path *p,uint32_t *generation,bool *same){
 if(!take(e))return DE_BUSY;
 if(e->current<0){*same=false;give(e);return DE_OK;}
 const struct de_path_slot *slot=&e->paths[e->current];const struct lp_path *old=&slot->path;
 *same=!p->origin.retained&&!old->origin.retained&&p->initial_pi_us>old->initial_pi_us&&
  p->count==old->count&&p->count>=2&&p->count<=LP_PATH&&p->revision==old->revision&&p->maneuver_revision==old->maneuver_revision&&
  p->world_features==old->world_features&&p->direction==old->direction&&p->phase==old->phase&&
  !memcmp(p->points,old->points,p->count*sizeof(p->points[0]))&&!memcmp(&p->goal,&old->goal,sizeof(p->goal))&&
  !memcmp(&p->merge_goal,&old->merge_goal,sizeof(p->merge_goal))&&!memcmp(&p->execution,&old->execution,sizeof(p->execution))&&
  !memcmp(&p->basis,&old->basis,sizeof(p->basis))&&!memcmp(&p->episode,&old->episode,sizeof(p->episode))&&
  !memcmp(&p->origin,&old->origin,sizeof(p->origin))&&!memcmp(&p->space,&old->space,sizeof(p->space))&&
  tr_same_intent(&p->task,&old->task)&&!strcmp(p->frame,old->frame)&&!strcmp(p->odom,old->odom)&&
  !strcmp(p->configuration,old->configuration)&&!strcmp(p->reference,old->reference);
 if(*same)*generation=slot->generation;
 give(e);return DE_OK;
}
static enum de_result initial_correspondence_context(struct drive_engine *e,const struct lp_path *candidate,
 uint64_t now,const struct lp_budget *budget){
 const xenova_drive_EpisodeCore *core=&candidate->episode.core;
 if(!e->behavior_enabled||!candidate->has_episode||dw_episode_snapshot_validate(&candidate->episode)||core->state!=2||!e->has_grant||
    !lp_mission_matches(candidate,&e->grant)||e->episode_transition||e->behavior_failed)return DE_STATE;
 uint64_t guest=0;
 if(!budget->clock||budget->clock(budget->context,&guest))return DE_CLOCK;
 if(guest>=budget->deadline_us)return DE_EXPIRED;
 if(now>=core->deadline_pi_us||now>=core->phase_deadline_pi_us||now<core->phase_started_pi_us||
    e->episode_status.commit_state!=1||now>=e->episode_status.valid_until_pi_us||
    candidate->episode.acceptance.registry_revision!=e->episode_status.registry_revision||
    memcmp(candidate->episode.acceptance.registry_epoch.bytes,e->episode_status.registry_epoch.bytes,16)||
    memcmp(candidate->episode.acceptance.accepted_core_sha256.bytes,e->episode_status.accepted_core_sha256.bytes,32))return DE_STATE;
 return DE_OK;
}
static enum de_result initial_correspondence_validate(struct drive_engine *e,struct de_path_slot *slot,uint32_t original_generation,
 const dw_world *world,dw_pose pose,const dw_vehicle *vehicle,uint64_t now,const struct lp_budget *budget){
 struct lp_path *candidate=&slot->path;
 enum de_result context=initial_correspondence_context(e,candidate,now,budget);if(context)return context;
 const xenova_drive_EpisodeCore *core=&candidate->episode.core;
 if(memcmp(core->meter_epoch.bytes,e->facts.meter_epoch,16)||e->facts.meter_revision!=e->episode.meter_revision||
    e->facts.metres_per_tick!=e->episode.metres_per_tick||e->facts.vehicle_sequence>vehicle->sequence||
    strcmp(e->facts.pi_session,vehicle->pi_session)||strcmp(e->facts.device_session,vehicle->device_session)||
    strcmp(e->facts.odom_epoch,vehicle->odom_epoch)||e->facts.meter_ticks<core->meter_baseline_total_ticks||
    e->facts.meter_ticks-core->meter_baseline_total_ticks<e->episode.meter_used_ticks||
    e->facts.meter_ticks-core->meter_baseline_total_ticks>UINT64_C(9007199254740992))return DE_STATE;
 if(!e->facts_current||!e->facts.valid||!e->facts.meter_valid||now>=e->facts.expires_pi_us)return DE_BUSY;
 double travel=db_travel_upper(&e->facts,core->meter_baseline_total_ticks,now,fmaxf(e->config->max_velocity,e->config->max_reverse_velocity));
 if(!isfinite(travel)||travel>=core->max_travel_m)return DE_STATE;
 bool seed=initial_correspondence_stale(candidate,vehicle,e->config);
 if(seed){enum de_result eligible=initial_correspondence_unrun(e,candidate,original_generation);if(eligible)return eligible;}
 float radius=hypotf(fmaxf(e->config->front,e->config->rear)+e->config->margin,e->config->half_width+e->config->margin);
 candidate->task.vehicle_pad=vehicle->position_error_m+radius*vehicle->heading_error_rad;
 struct lp_metrics measured={0};struct lp_tracker probe={0};struct lp_command command;
 enum lp_result valid=validate_slot(slot,world,e->config,&measured,budget);
 if(valid!=LP_PATH_FOUND)return DE_PATH;
 if(seed){
  /* Only a new unpublished candidate uses the explicit first-correspondence
   * search. The original path, registered Core/E/meter and first StateID stay. */
  candidate->initial_pi_us=0;candidate->initial_tracker_index=0;
 }
 valid=lp_track_prepared(&probe,candidate,&slot->tracking_geometry,world,pose,vehicle,e->config,.02f,&command,&measured,budget,NULL);
 if(valid!=LP_PATH_FOUND&&valid!=LP_STOP)return DE_PATH;
 if(seed){
  double bound=travel+e->config->max_position_error+vehicle->position_error_m+
   radius*(e->config->max_heading_error+vehicle->heading_error_rad);
  if(probe.index||probe.segment||probe.gate_revision||probe.progress>bound||
     hypotf(pose.x-candidate->points[0].pose.x,pose.y-candidate->points[0].pose.y)>bound)return DE_STATE;
  candidate->initial_tracker_index=probe.index;candidate->initial_pi_us=vehicle->pi_sample_us;
  memset(&probe,0,sizeof(probe));
  valid=lp_track_prepared(&probe,candidate,&slot->tracking_geometry,world,pose,vehicle,e->config,.02f,&command,&measured,budget,NULL);
  if(valid!=LP_PATH_FOUND&&valid!=LP_STOP)return DE_PATH;
 }
 return DE_OK;
}
enum de_result de_plan_start(struct drive_engine *e,uint64_t now,struct lp_budget budget){
 if(e->planning||e->apply_pending)return DE_BUSY;
 bool stopped=e->stopping;uint32_t request=e->requested;
 if(stopped)return DE_CANCELLED;
 if((!request||request==e->started)&&!e->parking.publish_source)return DE_WAITING;
 struct dw_handle h;const struct dw_snapshot *map;
 enum dw_result dr=dw_acquire(&e->maps,&h,&map);if(dr)return dr==DW_BUSY?DE_BUSY:DE_MAP;
 budget.collision_index=&e->map_indices[h.slot];
 if((map->world.required_features&DW_LANE_MODEL)&&e->has_grant&&(!e->grant.admitted||(e->grant.kind!=1&&e->grant.kind!=2))){
  (void)dw_release(&e->maps,h);return DE_WAITING;
 }
 if(dw_world_retained(&map->world)&&(!e->has_grant||!scene_mission(&map->world,&e->grant))){
  (void)dw_release(&e->maps,h);return DE_STATE;
 }
 uint64_t scene_generation=0;
 enum dw_result scene_gate=dw_scene_time(&e->maps,&map->world,now,NULL,&scene_generation);
 dw_pose pose,goal;dw_vehicle vehicle={0};enum de_result rc=scene_gate?(scene_gate==DW_BUSY?DE_BUSY:DE_CHANGED):inputs(e,map,now,&pose,&vehicle,NULL);
 e->budget=budget;
 if(!rc&&e->behavior_enabled&&e->has_grant&&e->grant.kind==2){
  if(!e->local_owner){(void)dw_release(&e->maps,h);return DE_WAITING;}
  uint32_t before=e->current>=0?e->paths[e->current].generation:0;
  bool publishing=e->parking.publish_source!=NULL;
  rc=parking_poll(e,&map->world,pose,&vehicle,now);
  if(rc){(void)dw_release(&e->maps,h);return rc;}
  if(publishing){(void)dw_release(&e->maps,h);return DE_OK;}
  if(e->parking.phase==DP_EVALUATE){
   if(e->parking.request_ready||e->parking.budget_pending){(void)dw_release(&e->maps,h);return DE_WAITING;}
   return parking_start(e,h,map,pose,&vehicle,now,budget);
  }
  if(e->parking.phase!=DP_SEARCH){
   uint32_t after=e->current>=0?e->paths[e->current].generation:0;
   (void)dw_release(&e->maps,h);return before!=after?DE_OK:DE_WAITING;
  }
 }
 float end_clearance=lp_terminal_clearance(e->config);
 struct tr_intent task={0};struct lp_metrics prefix={0};
 e->budget=budget;
 if(!rc)rc=task_prepare(e,&map->world,pose,&vehicle,end_clearance,budget,&task,&prefix,&goal,now);
 float speed=fmaxf(0,vehicle.velocity_mps);
 float replan_distance=fmaxf(e->config->lookahead_min,fminf(e->config->lookahead_max,speed*e->config->lookahead_time))+
                       speed*e->config->reaction_s+speed*speed/(2*e->config->max_brake)+e->config->goal_distance;
 if(!rc&&e->maneuver_active&&!(map->world.required_features&DW_LANE_MODEL)){
  struct lp_road_state current,merge;float steering=vehicle.has_measured_steering_rad?vehicle.measured_steering_rad:vehicle.estimated_steering_rad;
  bool projected=lp_reference_project(&map->world,pose,&current)==LP_PATH_FOUND&&
                 lp_reference_project(&map->world,e->maneuver_goal,&merge)==LP_PATH_FOUND;
  bool member=projected&&hypotf(merge.pose.x-e->maneuver_goal.x,merge.pose.y-e->maneuver_goal.y)<=e->config->goal_distance;
  if(e->merge_passed_reference_revision!=map->world.reference_revision)e->merge_passed_reference_revision=0;
  if(member&&current.arc-vehicle.position_error_m>=merge.arc)e->merge_passed_reference_revision=map->world.reference_revision;
  bool rejoined=projected&&e->merge_passed_reference_revision&&
                fabsf(current.offset)+vehicle.position_error_m<=e->config->goal_distance&&
                fabsf(dw_angle(pose.yaw-current.pose.yaw))+vehicle.heading_error_rad<=e->config->goal_heading&&
                fabsf(steering-atanf(e->config->wheelbase*current.curvature))+vehicle.steering_error_rad<=e->config->max_steer/(LP_STEERS/2);
  if(strcmp(e->maneuver_frame,map->world.frame_epoch)||strcmp(e->maneuver_odom,map->world.odom_epoch)||
   strcmp(e->maneuver_configuration,map->world.configuration)||strcmp(e->maneuver_reference,map->world.reference_id)||
   rejoined){
   e->maneuver_active=false;e->maneuver_direction=0;e->merge_passed_reference_revision=0;
   if(e->maneuver_revision<UINT64_MAX)e->maneuver_revision++;
  }else if(member&&!e->merge_passed_reference_revision&&merge.arc-current.arc>replan_distance)goal=e->maneuver_goal;
  /* Keep the merge milestone while a fresh local path continues beyond it.
   * Reaching a sampled endpoint is not proof of physical road rejoining. */
 }
 if(!rc){
  struct de_reference previous;const struct lp_path *old;
  if(!de_path_acquire(e,&previous,&old)){
   struct lp_metrics checked={0};
   bool mission_bound=old->execution.present&&old->execution.mission.kind;
   bool same_mission=(map->world.required_features&DW_LANE_MODEL)&&e->has_grant?
    lp_mission_matches(old,&e->grant):!mission_bound;
   if(!same_mission&&e->has_grant&&(e->behavior_enabled||e->grant.has_operation_binding)&&lp_grant_revalidates(old,&e->grant)){
    struct de_reference next;struct lp_path *candidate;
    enum de_result adopted=reserve(e,&next,&candidate);
    if(!adopted){
     *candidate=*old;lp_bind_current_grant(candidate,&e->grant);
     struct lp_tracker probe={0};struct lp_command actual;
     struct de_path_slot *slot=&e->paths[next.slot];
     enum lp_result validated=validate_slot(slot,&map->world,e->config,&checked,&budget);
     if(validated==LP_PATH_FOUND)validated=lp_track_prepared(&probe,candidate,&slot->tracking_geometry,
      &map->world,pose,&vehicle,e->config,.02f,&actual,&checked,&budget,NULL);
     e->paths[next.slot].scene_generation=scene_generation;e->apply_path=next;e->apply_valid=validated==LP_PATH_FOUND||validated==LP_STOP;
     e->apply_pending=true;e->apply_grant=e->grant;e->apply_has_grant=true;
     e->apply_map_generation=h.generation;e->apply_budget=budget;
    }
    (void)de_path_release(e,previous);(void)dw_release(&e->maps,h);return adopted?adopted:DE_BUSY;
   }
   bool same_intent=(!e->behavior_enabled||old->has_episode)&&!e->episode_transition&&same_mission&&tr_same_intent(&old->task,&task)&&
                    ((old->direction!=0)==e->maneuver_active)&&
                    old->merge_passed_reference_revision==e->merge_passed_reference_revision;
   float remaining=lp_path_remaining(old,pose,NULL);
   bool same_goal=hypotf(goal.x-old->goal.x,goal.y-old->goal.y)<e->config->goal_distance||remaining>replan_distance;
   uint64_t previous_scene=e->paths[previous.slot].scene_generation;
   bool same_scene=!scene_generation||previous_scene==scene_generation;
   enum dw_result previous_gate=same_scene?dw_scene_generation(&e->maps,previous_scene,now,NULL):DW_SOURCE;
   if(previous_gate==DW_BUSY){
    (void)de_path_release(e,previous);(void)dw_release(&e->maps,h);return DE_BUSY;
   }
   enum lp_result usable=same_intent&&same_goal&&!previous_gate?
    lp_recheck_path(old,&e->paths[previous.slot].tracking_geometry,&map->world,e->config,&checked,&budget):LP_INPUT;
   if(same_intent&&same_goal&&(usable==LP_COLLISION||usable==LP_INPUT||usable==LP_NUMERIC))e->path_invalidated=true;
   if(usable==LP_PATH_FOUND){
    if(initial_correspondence_pending(e,old)&&initial_correspondence_stale(old,&vehicle,e->config)&&
       initial_correspondence_unrun(e,old,previous.generation)==DE_OK){
     struct de_reference next;struct lp_path *candidate;enum de_result adopted=reserve(e,&next,&candidate);
     if(!adopted){
      *candidate=*old;e->paths[next.slot].scene_generation=previous_scene;
      adopted=initial_correspondence_validate(e,&e->paths[next.slot],previous.generation,&map->world,pose,&vehicle,now,&budget);
      if(!adopted&&dw_source_valid(&e->maps,h))adopted=DE_CHANGED;
      if(!adopted&&dw_scene_generation(&e->maps,previous_scene,now,NULL))adopted=DE_CHANGED;
      /* Retain the reserved slot across publish contention using the existing
       * bounded apply owner. Even rejected candidates are reclaimed there. */
      e->apply_path=next;e->apply_valid=adopted==DE_OK;e->apply_pending=true;
      e->apply_grant=e->grant;e->apply_has_grant=true;e->apply_map_generation=h.generation;e->apply_budget=budget;
     }
     (void)de_path_release(e,previous);(void)dw_release(&e->maps,h);
     if(e->apply_pending)adopted=de_apply_finish(e,now);
     if(!adopted)e->started=request;
     return adopted;
    }
    enum de_result correspondence=correspondence_current(e,old,previous.generation,&vehicle);
    if(correspondence==DE_BUSY){(void)de_path_release(e,previous);(void)dw_release(&e->maps,h);return DE_BUSY;}
    if(correspondence==DE_OK){
     e->path_invalidated=false;e->started=request;(void)de_path_release(e,previous);(void)dw_release(&e->maps,h);return DE_OK;
    }
    e->path_invalidated=true; /* fresh search below, no old anchor mutation */
   }
   (void)de_path_release(e,previous);
  }
 }
 struct lp_path *path=NULL;
 if(!rc)rc=reserve(e,&e->planning_path,&path);
 if(rc){(void)dw_release(&e->maps,h);return e->last_reason=rc;}
 e->planning_map=h;e->planning_snapshot=map;e->planning=true;e->plan_finalized=false;
 e->planning_grant=e->grant;e->planning_has_grant=(map->world.required_features&DW_LANE_MODEL)&&e->has_grant&&e->grant.kind;
 e->paths[e->planning_path.slot].scene_generation=scene_generation;
 e->started=request;e->budget=budget;e->plans++;
 e->planning_vehicle_pi_us=vehicle.pi_sample_us;
 float steering=vehicle.has_measured_steering_rad?vehicle.measured_steering_rad:
                 vehicle.has_estimated_steering_rad?vehicle.estimated_steering_rad:NAN;
 if(task.active&&e->behavior_enabled&&e->episode_state.core.state==1&&!e->episode_state.core.phase&&e->episode_status.commit_state<2)
  e->search_reason=lp_start_task_goal(&e->search,&map->world,pose,steering,goal,e->config,e->assets,budget,&task,&e->basis);
 else if(task.active){
  e->search_reason=LP_NOT_FOUND;
  if((e->episode.active||(!task.changing&&!e->maneuver_active))&&e->has_grant&&e->grant.kind==1){
   struct de_path_slot *slot=&e->paths[e->planning_path.slot];
   e->search_reason=lp_lane_guide(&e->search,path,&map->world,pose,steering,vehicle.velocity_mps,goal,
    e->config,budget,&task,&e->basis,&prefix,&slot->tracking_geometry);
   slot->tracking_prepared=e->search_reason==LP_PATH_FOUND;
  }
  if(e->search_reason==LP_NOT_FOUND)
   e->search_reason=lp_start_task(&e->search,&map->world,pose,steering,goal,e->config,e->assets,budget,&task,&e->basis,&prefix);
  else{e->search.metrics=prefix;e->search.used=0;}
 }else e->search_reason=lp_start_local(&e->search,&map->world,pose,steering,goal,e->config,e->assets,budget,e->maneuver_direction);
 search_memory_publish(e);
 return e->last_reason=DE_OK;
}
enum de_result de_plan_slice(struct drive_engine *e,unsigned expansions,uint64_t now){
 if(!e->planning)return DE_WAITING;
 if(e->parking.active&&e->parking.stage)return parking_slice(e,expansions,now);
 if(e->stopping)e->search_reason=LP_CANCELLED;
 else if(e->search_reason==LP_PATH_FOUND){
  /* A completed candidate may wait for a lock, but keeps its search deadline. */
  uint64_t guest;
  if(e->budget.cancelled&&e->budget.cancelled(e->budget.context))e->search_reason=LP_CANCELLED;
  else if(!e->budget.clock||e->budget.clock(e->budget.context,&guest))e->search_reason=LP_CLOCK;
  else if(guest>=e->budget.deadline_us)e->search_reason=LP_DEADLINE;
 }
 if(e->search_reason==LP_SEARCHING||e->search_reason==LP_PATH_FOUND){
  enum dw_result scene=dw_scene_generation(&e->maps,e->paths[e->planning_path.slot].scene_generation,now,NULL);
  if(scene==DW_BUSY)return DE_BUSY;
  if(scene)e->search_reason=LP_CANCELLED;
 }
 struct lp_path *path=&e->paths[e->planning_path.slot].path;
 if(e->search_reason==LP_SEARCHING)e->search_reason=lp_advance(&e->search,expansions,path);
 search_memory_publish(e);
 if(e->search_reason==LP_SEARCHING)return DE_WAITING;
 /* A high-rate pose-anchored map must not starve a bounded search. Validate
  * the completed path against the latest complete map, keeping the original
  * search deadline. Every later command still projects against its own map. */
 if(!e->plan_finalized){
  bool valid=e->search_reason==LP_PATH_FOUND&&!e->stopping;
  path->initial_pi_us=e->planning_vehicle_pi_us;path->initial_tracker_index=0;
  if(valid&&(path->world_features&DW_LANE_MODEL)&&e->planning_has_grant){
   if(!e->has_grant||!grant_same(&e->grant,&e->planning_grant))valid=false;
   else{mission_metadata(path,&e->planning_grant);if(e->planning_grant.kind==2)parking_metadata(e,path,&e->planning_grant);}
  }
  if(valid){
   struct dw_handle current;const struct dw_snapshot *snapshot;
   enum dw_result acquired=dw_acquire(&e->maps,&current,&snapshot);
   if(acquired==DW_BUSY)return DE_BUSY;
   if(acquired)valid=false;
   else{
    struct lp_budget current_budget=e->budget;current_budget.collision_index=&e->map_indices[current.slot];
    struct de_path_slot *slot=&e->paths[e->planning_path.slot];
    if(current.generation!=e->planning_map.generation||(e->episode.active&&e->search.used)){
     struct lp_metrics checked={0};
     enum lp_result result=validate_slot(slot,&snapshot->world,e->config,&checked,&current_budget);
     if(result!=LP_PATH_FOUND){e->search_reason=result;valid=false;}
    }
    enum de_result input=DE_OK;
    if(valid&&!prepare_tracking(e,e->planning_path)){e->search_reason=LP_INPUT;valid=false;}
    if(valid){
     dw_pose pose;dw_vehicle vehicle;input=inputs(e,snapshot,now,&pose,&vehicle,NULL);
     struct lp_tracker probe={0};struct lp_command command;struct lp_metrics measured={0};
     if(input)valid=false;
     else{
      enum lp_result result=planning_correspondence(e,slot,&snapshot->world,pose,&vehicle,now,&probe,&command,&measured,&current_budget);
      if(result!=LP_PATH_FOUND&&result!=LP_STOP){e->search_reason=result;valid=false;}
     }
    }
    enum dw_result source=valid?dw_source_valid(&e->maps,current):DW_OK;
    (void)dw_release(&e->maps,current);
    if(input==DE_BUSY||source==DW_BUSY)return DE_BUSY;
    if(source)valid=false;
   }
  }
  if(valid){
   if(e->path_revision==UINT64_MAX||e->maneuver_revision==UINT64_MAX)valid=false;
   else{
    path->revision=++e->path_revision;
    if(!e->maneuver_revision)e->maneuver_revision=1;
    if(e->maneuver_active){
     path->merge_goal=e->maneuver_goal;
     path->merge_passed_reference_revision=e->merge_passed_reference_revision;
     if(!path->direction)path->direction=e->maneuver_direction;
    }
    if(path->direction){
     if(e->maneuver_active&&e->maneuver_direction!=path->direction){
      /* The merge milestone and its checked context are unchanged above.
       * New avoidance direction does not undo its observed physical passage. */
      e->maneuver_revision++;
     }
     e->maneuver_active=true;e->maneuver_direction=path->direction;e->maneuver_goal=path->merge_goal;
     strcpy(e->maneuver_frame,path->frame);strcpy(e->maneuver_odom,path->odom);
     strcpy(e->maneuver_configuration,path->configuration);strcpy(e->maneuver_reference,path->reference);
    }
    path->maneuver_revision=e->maneuver_revision;
   }
  }
  if(valid&&e->behavior_enabled){
   enum de_result accepted=behavior_candidate(e,path,e->planning_snapshot?&e->planning_snapshot->world:NULL,now);
   if(accepted!=DE_OK&&accepted!=DE_BUSY)valid=false;
  }
  e->plan_finalized=true;e->plan_valid=valid;
 }
 if(e->episode_request_ready||e->episode_receipt_pending){
  uint64_t guest=0;bool expired=e->budget.clock&&(e->budget.clock(e->budget.context,&guest)||guest>=e->budget.deadline_us);
  expired|=e->episode_receipt_pending&&e->episode_request.valid_until_pi_us&&now>=e->episode_request.valid_until_pi_us;
  if(!expired)return DE_BUSY;
  e->episode_request_ready=e->episode_receipt_pending=false;e->plan_valid=false;e->behavior_failed=true;db_episode_hold(&e->episode,DB_TIME);
 }
 bool valid=e->plan_valid&&!e->stopping&&e->search_reason==LP_PATH_FOUND;
 if(valid){
  enum dw_result scene=dw_scene_generation(&e->maps,e->paths[e->planning_path.slot].scene_generation,now,NULL);
  if(scene==DW_BUSY)return DE_BUSY;
  if(scene)valid=false;
 }
 if(valid&&initial_correspondence_pending(e,path)){
  enum de_result context=initial_correspondence_context(e,path,now,&e->budget);
  if(context)valid=false;
  /* A registered private candidate has no output authority. Missing fresh
   * facts can also make inputs()'s old Vehicle expire; retain that candidate
   * only inside its original job/Core/status lifetimes, without publishing. */
  else if(!e->facts_current||!e->facts.valid||!e->facts.meter_valid||now>=e->facts.expires_pi_us)return DE_BUSY;
 }
 if(valid&&initial_correspondence_pending(e,path)){
  struct dw_handle fresh;const struct dw_snapshot *map;enum dw_result acquired=dw_acquire(&e->maps,&fresh,&map);
  if(acquired==DW_BUSY)return DE_BUSY;
  if(acquired)valid=false;
  else{
   dw_pose pose;dw_vehicle vehicle;enum de_result current=inputs(e,map,now,&pose,&vehicle,NULL);
   struct lp_budget current_budget=e->budget;current_budget.collision_index=&e->map_indices[fresh.slot];
   if(!current)current=initial_correspondence_validate(e,&e->paths[e->planning_path.slot],e->planning_path.generation,
    &map->world,pose,&vehicle,now,&current_budget);
   if(!current){
    enum dw_result source=dw_source_valid(&e->maps,fresh);
    if(source)current=source==DW_BUSY?DE_BUSY:DE_CHANGED;
   }
   enum dw_result released=dw_release(&e->maps,fresh);
   if(current==DE_BUSY||released==DW_BUSY)return DE_BUSY;
   if(current||released)valid=false;
  }
 }
 enum de_result rc=publish(e,e->planning_path,valid);if(rc==DE_BUSY)return rc;
 if(!rc){e->task=path->task;e->basis=path->basis;if(e->parking.active)e->parking.published_phase=e->parking.phase;}
 enum dw_result released=dw_release(&e->maps,e->planning_map);if(released==DW_BUSY)return DE_BUSY;
 e->planning=false;e->planning_snapshot=NULL;
 unsigned outcome=(unsigned)atomic_load(&e->search_reason);
 if(outcome<=LP_CLOCK){
  e->search_outcomes[outcome]++;
  if(outcome==LP_NODE_BUDGET)atomic_fetch_add(&e->search_exhausted,1);
  if(outcome!=LP_PATH_FOUND&&e->first_search_failure==LP_SEARCHING)e->first_search_failure=(enum lp_result)outcome;
 }
 const struct lp_metrics *m=&e->search.metrics;struct lp_metrics *total=&e->planned;
 total->expanded+=m->expanded;total->primitives+=m->primitives;total->footprints+=m->footprints;
 total->cells+=m->cells;total->road_checks+=m->road_checks;total->cancel_checks+=m->cancel_checks;total->clock_checks+=m->clock_checks;
 if(m->nodes_high_water>total->nodes_high_water)total->nodes_high_water=m->nodes_high_water;
 if(m->open_high_water>total->open_high_water)total->open_high_water=m->open_high_water;
 if(m->index_probes_max>total->index_probes_max)total->index_probes_max=m->index_probes_max;
 if(!valid)e->discarded++;
 return e->last_reason=valid?DE_OK:e->search_reason==LP_CANCELLED?DE_CANCELLED:DE_PATH;
}
static bool completed_episode_progress(const struct lp_path *path,uint32_t generation,dw_pose pose,
 const dw_vehicle *vehicle,uint64_t now,const struct db_facts *facts,const struct lp_config *config,
 const struct de_episode_progress *previous,struct de_episode_progress *out){
 memset(out,0,sizeof(*out));
 if(!path->has_episode||path->episode.core.state!=2||path->episode.core.phase>=DB_FINISHED)return false;
 const xenova_drive_EpisodeCore *core=&path->episode.core;
 const xenova_drive_EpisodeAcceptanceRef *ref=&path->episode.acceptance;
 /* This is the exact Pi-owned meter sample used for the actual calculation,
  * never a newer Vehicle number painted onto the receipt's older progress. */
 if(!facts->valid||!facts->meter_valid||now>=facts->expires_pi_us||facts->vehicle_sequence!=vehicle->sequence||
    facts->sampled_pi_us!=vehicle->pi_sample_us||strcmp(facts->pi_session,vehicle->pi_session)||
    strcmp(facts->device_session,vehicle->device_session)||strcmp(facts->odom_epoch,vehicle->odom_epoch)||
    memcmp(facts->meter_epoch,core->meter_epoch.bytes,16)||facts->meter_ticks<core->meter_baseline_total_ticks||
    facts->meter_ticks-core->meter_baseline_total_ticks>UINT64_C(9007199254740992)||
    now<core->phase_started_pi_us||now>=core->phase_deadline_pi_us||now>=core->deadline_pi_us||
    !isfinite(facts->metres_per_tick)||facts->metres_per_tick<=0||!dw_episode_core_hash(core,out->core_sha256))return false;
 out->path_generation=generation;out->vehicle_sequence=vehicle->sequence;out->registry_revision=ref->registry_revision;
 memcpy(out->registry_epoch,ref->registry_epoch.bytes,16);
 out->progress=path->episode.progress;
 if(previous->valid&&previous->registry_revision==out->registry_revision&&
    !memcmp(previous->registry_epoch,out->registry_epoch,16)&&!memcmp(previous->core_sha256,out->core_sha256,32)&&
    previous->progress.revision>=out->progress.revision)out->progress=previous->progress;
 xenova_drive_EpisodeProgress *progress=&out->progress;
 uint64_t used=facts->meter_ticks-core->meter_baseline_total_ticks;
 if(vehicle->sequence<progress->vehicle_sequence||used<progress->used_travel_ticks||progress->revision>=INT64_MAX)return false;
 float radius=hypotf(fmaxf(config->front,config->rear)+config->margin,config->half_width+config->margin);
 float pad=vehicle->position_error_m+radius*vehicle->heading_error_rad,lo,hi;
 if(!db_station_bounds(&path->basis,pose,pad,config,&lo,&hi))return false;
 float remaining=hypotf(pose.x-core->phase_goal.x,pose.y-core->phase_goal.y)+pad;
 if(!isfinite(remaining))return false;
 bool fresh=vehicle->sequence>progress->vehicle_sequence;
 bool advanced=core->progress_mode==1?lo>=progress->station_lower_m+config->avoid_progress_m:
  core->progress_mode==2&&remaining+config->avoid_progress_m<=progress->remaining_upper_m;
 if(fresh&&advanced&&vehicle->pi_sample_us>progress->last_progress_pi_us&&vehicle->pi_sample_us>=core->phase_started_pi_us){
  /* Source sample time is an existing Pi event, not the later guest-now upper
   * estimate. Heartbeat/re-export cannot create movement or renew a budget. */
  progress->station_lower_m=fmaxf(progress->station_lower_m,lo);
  progress->remaining_upper_m=fminf(progress->remaining_upper_m,remaining);
  progress->last_progress_pi_us=vehicle->pi_sample_us;
 }
 progress->vehicle_sequence=vehicle->sequence;progress->used_travel_ticks=used;progress->revision++;
 out->valid=true;(void)hi;return true;
}
static bool episode_transition_blocks_path(const struct drive_engine *e,const struct lp_path *path){
 if(!e->episode_transition)return false;
 /* Before registration, a settled vehicle may still follow the published
  * target-lane path. The caller checks the current map and stopping sweep. */
 return !e->episode_pass_planning||e->path_invalidated||path->execution.mission.kind!=1||
  !path->has_episode||path->episode.core.state!=2||path->episode.core.phase!=DB_CHANGE_OUT||
  !path->task.active||path->task.changing||path->task.selected_lane_id!=path->episode.core.target_lane_id;
}
static enum de_result track(struct drive_engine *e,struct lp_tracker *tracker,uint64_t now,float dt,struct lp_command *command,
 dw_vehicle *vehicle,struct lp_budget budget,struct de_reference *completed,struct de_witness *witness,
 const xenova_drive_MissionGrant *mission,bool enforce_mission_profile,const xenova_drive_DriveControlFacts *current_facts){
 memset(command,0,sizeof(*command));command->emergency=true;command->result=LP_INPUT;
 if(witness)memset(witness,0,sizeof(*witness));
 struct lp_tracker next=*tracker;
 bool live=tracker==&e->tracker,parking_stop=false;
 struct db_facts facts={0};
 struct dw_handle mh;const struct dw_snapshot *map;
 enum dw_result acquired=dw_acquire(&e->maps,&mh,&map);
 if(acquired)return acquired==DW_BUSY?DE_BUSY:DE_MAP;
 budget.collision_index=&e->map_indices[mh.slot];
 dw_pose pose;uint64_t map_end=0;enum de_result rc=inputs(e,map,now,&pose,vehicle,&map_end);
 if(!rc&&enforce_mission_profile&&(map->world.required_features&DW_LANE_MODEL)&&!mission)rc=DE_STATE;
 struct de_reference ph={0};const struct lp_path *path=NULL;
 if(!rc)rc=de_path_acquire(e,&ph,&path);
 if(!rc){
  bool metadata_busy=false,facts_ready=e->facts_current;
  if(e->behavior_enabled){
   if(current_facts)facts_ready=control_facts_current(e,current_facts,now,&facts)==DE_OK;
   else if(!take(e))metadata_busy=true;
   else{facts=e->facts;give(e);}
  }
  struct lp_metrics measured={0};
  enum lp_result tracked=LP_INPUT;bool calculated=false;
  uint64_t map_scene=0,scene_end=UINT64_MAX;
  const xenova_drive_MissionGrant *scene_grant=enforce_mission_profile?mission:e->has_grant?&e->grant:NULL;
  enum dw_result map_gate=dw_scene_time(&e->maps,&map->world,now,NULL,&map_scene);
  enum dw_result path_gate=dw_scene_generation(&e->maps,e->paths[ph.slot].scene_generation,now,&scene_end);
  if(metadata_busy||map_gate==DW_BUSY||path_gate==DW_BUSY)rc=DE_BUSY;
  else if(map_gate||path_gate)rc=DE_CHANGED;
  else if(map_scene&&(e->paths[ph.slot].scene_generation!=map_scene||!scene_mission(&map->world,scene_grant)))rc=DE_STATE;
  else if(enforce_mission_profile&&(map->world.required_features&DW_LANE_MODEL)&&!lp_mission_matches(path,mission))rc=DE_STATE;
  else if(e->behavior_enabled&&(e->behavior_failed||!facts_ready||episode_transition_blocks_path(e,path)))rc=DE_STATE;
  else if(path->has_episode&&path->episode.core.state==2&&path->episode.core.phase<DB_FINISHED&&
    (now>=path->episode.core.deadline_pi_us||now>=path->episode.core.phase_deadline_pi_us||
     !facts.valid||!facts.meter_valid||now>=facts.expires_pi_us||memcmp(facts.meter_epoch,path->episode.core.meter_epoch.bytes,16)||
     facts.meter_ticks<path->episode.core.meter_baseline_total_ticks||
     facts.meter_ticks-path->episode.core.meter_baseline_total_ticks>UINT64_C(9007199254740992)||
     db_travel_upper(&facts,path->episode.core.meter_baseline_total_ticks,now,
      fmaxf(e->config->max_velocity,e->config->max_reverse_velocity))>=path->episode.core.max_travel_m))rc=DE_STATE;
  else{
   calculated=true;
   lp_tracker_begin(&next,ph.generation);
   struct lp_parking_cache *parking=NULL;
   if(live&&path->space.parking&&!path->space.exiting){
    parking=&e->tracking_parking;
    if(e->tracking_parking_map!=mh.generation||e->tracking_parking_path!=ph.generation){
     parking->ready=false;e->tracking_parking_map=mh.generation;e->tracking_parking_path=ph.generation;
    }
   }
   tracked=lp_track_prepared(&next,path,&e->paths[ph.slot].tracking_geometry,&map->world,pose,vehicle,e->config,dt,command,&measured,&budget,parking);
   if(path->has_episode&&path->episode.core.state==1&&!path->episode.core.phase&&e->episode_status.commit_state<2){command->velocity=0;command->steer=0;command->emergency=true;}
   if(live&&e->behavior_enabled&&path->execution.mission.kind==2&&path->execution.mission.phase!=DP_SEARCH){
    unsigned phase=path->execution.mission.phase;
    bool drive=phase==DP_APPROACH||phase==DP_ENTERING;
    parking_stop=drive&&tracked==LP_STOP&&!command->gate_required;
    if(!drive){command->velocity=0;command->steer=0;command->emergency=true;
     if(phase==DP_ALIGN_STOP&&atomic_load(&e->parking_boundary_pending)){command->gate_required=true;
      command->requested_direction=path->execution.segments[0].signed_direction;}}
   }
   rc=tracked==LP_PATH_FOUND||tracked==LP_STOP?DE_OK:DE_PATH;
  }
  if(calculated&&live&&(tracked==LP_COLLISION||tracked==LP_INPUT||tracked==LP_NUMERIC))e->path_invalidated=true;
  if(!rc){
   uint64_t pose_end=vehicle->pi_sample_us+(uint64_t)e->config->pose_age_us-vehicle->clock_error_us;
   command->valid_until_pi_us=map_end<pose_end?map_end:pose_end;
   if(command->valid_until_pi_us>map->valid_until_us)command->valid_until_pi_us=map->valid_until_us;
   if(command->valid_until_pi_us>scene_end)command->valid_until_pi_us=scene_end;
   if(path->has_episode&&path->episode.core.state==2&&path->episode.core.phase<DB_FINISHED&&
      command->valid_until_pi_us>path->episode.core.phase_deadline_pi_us)command->valid_until_pi_us=path->episode.core.phase_deadline_pi_us;
   enum dw_result final_scene=dw_scene_generation(&e->maps,e->paths[ph.slot].scene_generation,now,NULL);
   if(final_scene)rc=final_scene==DW_BUSY?DE_BUSY:DE_CHANGED;
  }
  if(live){e->tracking.cells+=measured.cells;e->tracking.footprints+=measured.footprints;}
  if(!rc&&witness){witness->reference_revision=map->world.reference_revision;witness->observation_sequence=map->world.observation_seq;
   witness->valid_until_pi_us=command->valid_until_pi_us;strcpy(witness->map_epoch,map->world.map_epoch);}
 }
 if(!rc){
  enum dw_result source=dw_source_valid(&e->maps,mh);
  if(source)rc=source==DW_BUSY?DE_BUSY:DE_CHANGED;
 }
 if(dw_release(&e->maps,mh))rc=DE_BUSY;
 /* The next tracker state and parking STOP belong to this accepted sample. */
 bool locked=!rc&&live&&take(e);
 bool publish_progress=locked&&e->current==(int)ph.slot&&!e->stopping;
 struct de_episode_progress episode_progress={0};
 if(publish_progress)
  (void)completed_episode_progress(path,ph.generation,pose,vehicle,now,&facts,e->config,&e->tracked_episode_progress,&episode_progress);
 if(path&&de_path_release(e,ph))rc=DE_BUSY;
 if(!rc){
  *tracker=next;
  if(completed)*completed=ph;
  if(publish_progress){
   e->progress_snapshot=next;e->progress_generation=ph.generation;
   if(episode_progress.valid){
    e->tracked_episode_progress=episode_progress;
    if(witness)witness->episode=episode_progress;
   }
   if(parking_stop)atomic_store(&e->parking_stop_generation,ph.generation);
  }
 }
 if(locked)give(e);
 if(rc){memset(command,0,sizeof(*command));command->emergency=true;command->result=LP_INPUT;
  if(witness)memset(witness,0,sizeof(*witness));}
 return e->last_reason=rc;
}
enum de_result de_track(struct drive_engine *e,uint64_t now,float dt,struct lp_command *command,dw_vehicle *vehicle,struct lp_budget budget){
 enum de_result rc=track(e,&e->tracker,now,dt,command,vehicle,budget,&e->last_track_path,&e->last_track_world,NULL,true,NULL);
 if(!rc)atomic_fetch_add(&e->completed,1);
 return rc;
}
enum de_result de_track_mission(struct drive_engine *e,const xenova_drive_MissionGrant *mission,uint64_t now,float dt,
 struct lp_command *command,dw_vehicle *vehicle,struct lp_budget budget){
 return de_track_mission_facts(e,mission,NULL,now,dt,command,vehicle,budget);
}
enum de_result de_track_mission_facts(struct drive_engine *e,const xenova_drive_MissionGrant *mission,
 const xenova_drive_DriveControlFacts *facts,uint64_t now,float dt,struct lp_command *command,dw_vehicle *vehicle,struct lp_budget budget){
 memset(command,0,sizeof(*command));command->emergency=true;command->result=LP_INPUT;
 if(!mission||dw_mission_grant_validate(mission))return DE_STATE;
 enum de_result rc=track(e,&e->tracker,now,dt,command,vehicle,budget,&e->last_track_path,&e->last_track_world,mission,true,facts);
 if(!rc)atomic_fetch_add(&e->completed,1);
 return rc;
}
enum de_result de_prepare(struct drive_engine *e,uint64_t now,struct de_reference *path,struct de_witness *witness,dw_vehicle *vehicle,struct lp_budget budget){
 struct lp_tracker temporary={0};struct lp_command calculated;
 if(!take(e))return DE_BUSY;
 if(e->progress_generation)temporary=e->progress_snapshot;
 give(e);
 return track(e,&temporary,now,.02f,&calculated,vehicle,budget,path,witness,NULL,false,NULL);
}
enum de_result de_apply(struct drive_engine *e,const dw_state *state,uint64_t now,struct lp_budget budget){
 if(e->planning||e->apply_pending)return DE_BUSY;
 if(dw_state_validate(state))return DE_STATE;
 struct dw_handle mh;const struct dw_snapshot *map;
 if(dw_acquire(&e->maps,&mh,&map))return DE_MAP;
 budget.collision_index=&e->map_indices[mh.slot];
 dw_pose pose;dw_vehicle vehicle;enum de_result rc=inputs(e,map,now,&pose,&vehicle,NULL);
 uint64_t scene_generation=0;
 if(!rc&&dw_scene_time(&e->maps,&map->world,now,NULL,&scene_generation))rc=DE_CHANGED;
 if(!rc&&scene_generation&&(!e->has_grant||!scene_mission(&map->world,&e->grant)||
    !state->has_mission||state->mission.scene_admission_revision!=map->world.scene.admission_revision||
    memcmp(state->mission.scene_admission_id.bytes,map->world.scene.admission_id.bytes,16)))rc=DE_STATE;
 dw_vehicle original_vehicle={0};
 if(!rc){enum dw_result original=dw_vehicle_sequence(&e->odometry,state->vehicle_sequence,&original_vehicle);
  if(original)rc=original==DW_BUSY?DE_BUSY:DE_POSE;
  else if(vehicle.pi_sample_us<original_vehicle.pi_sample_us||
   vehicle.pi_sample_us-original_vehicle.pi_sample_us+vehicle.clock_error_us+original_vehicle.clock_error_us>e->config->pose_age_us)rc=DE_EXPIRED;
 }
 struct de_reference ph;struct lp_path *path=NULL;
 if(!rc&&(strcmp(state->frame_epoch,map->world.frame_epoch)||strcmp(state->odom_epoch,map->world.odom_epoch)))rc=DE_FRAME;
 if(!rc&&(strcmp(state->configuration,map->world.configuration)||strcmp(state->primitive,e->assets->sha256)||strcmp(state->calibration,map->world.calibration)))rc=DE_CONFIGURATION;
 if(!rc&&strcmp(state->reference_id,map->world.reference_id))rc=DE_REFERENCE;
 if(!rc&&state->world_features!=map->world.required_features)rc=DE_CONFIGURATION;
 if(!rc&&(map->world.required_features&DW_LANE_MODEL)&&!state->has_task_reference)rc=DE_STATE;
 if(!rc&&e->behavior_enabled&&(map->world.required_features&DW_LANE_MODEL)&&(!state->has_episode||!e->has_grant))rc=DE_STATE;
 /* legacy AUTO는 기존 forward-only 동작이다. 새 signed 수학 API가 NONE 사본을 통해
  * command1의 방향전환 권한을 우회 생성하지 않는다. 현대 parking owner는 별도다. */
 if(!rc&&!(map->world.required_features&DW_LANE_MODEL)){
  if(state->segments_count>1||(state->segments_count&&state->segments[0].signed_direction!=1))rc=DE_STATE;
 }
 if(!rc&&(!state->complete||state->path_count<2||state->path_count>LP_PATH))rc=DE_STATE;
 if(!rc)rc=reserve(e,&ph,&path);
 if(!rc){
  e->paths[ph.slot].scene_generation=scene_generation;
  memset(path,0,sizeof(*path));path->count=state->path_count;memcpy(path->points,state->path,path->count*sizeof(*path->points));
  path->merge_goal=state->merge_goal;path->goal=path->points[path->count-1].pose;
  path->world_features=state->world_features;
  path->merge_passed_reference_revision=state->has_merge_passed_reference_revision&&
   state->merge_passed_reference_revision==map->world.reference_revision?state->merge_passed_reference_revision:0;
  path->revision=state->path_revision;path->maneuver_revision=state->maneuver_revision;
  path->direction=state->direction;path->phase=state->maneuver_phase;
  lp_state_import(path,state);path->initial_pi_us=original_vehicle.pi_sample_us;
  if(e->has_grant&&(e->behavior_enabled||e->grant.has_operation_binding)&&!lp_mission_matches(path,&e->grant)){
   if(lp_grant_revalidates(path,&e->grant))lp_bind_current_grant(path,&e->grant);
   else rc=DE_STATE;
  }
  if(path->space.parking){const xenova_drive_ParkingSlot *slot=dp_slot(&map->world,path->space.slot_id);
   if(!slot||slot->geometry_revision!=path->space.slot_revision||!dp_slot_eligible(&map->world,slot,e->grant.strategy)||
      memcmp(slot->corners,path->space.polygon,sizeof(path->space.polygon)))rc=DE_STATE;
   else path->space.uncertainty=slot->position_bound_m+hypotf(e->config->front,e->config->half_width)*slot->heading_bound_rad;
  }
  if(path->task.active){
   float radius=hypotf(fmaxf(e->config->front,e->config->rear)+e->config->margin,e->config->half_width+e->config->margin);
   path->task.vehicle_pad=vehicle.position_error_m+radius*vehicle.heading_error_rad;
  }
  strcpy(path->frame,state->frame_epoch);strcpy(path->odom,state->odom_epoch);
  strcpy(path->configuration,state->configuration);strcpy(path->reference,state->reference_id);
  struct lp_metrics metrics={0};struct lp_tracker probe={0};struct lp_command calculated;
  struct de_path_slot *slot=&e->paths[ph.slot];
  enum lp_result valid=rc||(scene_generation&&!lp_mission_matches(path,&e->grant))?LP_INPUT:
   validate_slot(slot,&map->world,e->config,&metrics,&budget);
  if(valid==LP_PATH_FOUND)valid=lp_track_prepared(&probe,path,&slot->tracking_geometry,&map->world,pose,&vehicle,e->config,
   .02f,&calculated,&metrics,&budget,NULL);
  e->last_state_result=valid;
  bool usable=valid==LP_PATH_FOUND||valid==LP_STOP;
  e->apply_path=ph;e->apply_valid=usable;e->apply_pending=true;
  e->apply_has_grant=e->has_grant&&(map->world.required_features&DW_LANE_MODEL);if(e->apply_has_grant)e->apply_grant=e->grant;
  e->apply_map_generation=mh.generation;e->apply_budget=budget;
  /* Scene candidates commit on the next planner tick with a fresh Pi bound. */
  rc=scene_generation?DE_BUSY:de_apply_finish(e,now);
 }
 if(dw_release(&e->maps,mh))rc=DE_BUSY;
 return rc;
}
enum de_result de_apply_finish(struct drive_engine *e,uint64_t now){
 if(!e->apply_pending)return DE_WAITING;
 enum dw_result scene=dw_scene_generation(&e->maps,e->paths[e->apply_path.slot].scene_generation,now,NULL);
 if(scene==DW_BUSY)return DE_BUSY;
 if(e->stopping||scene)e->apply_valid=false;
 if(e->apply_has_grant&&(!e->has_grant||!grant_same(&e->apply_grant,&e->grant)))e->apply_valid=false;
 uint32_t original_generation=0;bool first_adoption=false;
 if(e->apply_valid&&initial_correspondence_candidate(e,&e->paths[e->apply_path.slot].path,&original_generation,&first_adoption)==DE_BUSY)return DE_BUSY;
 if(e->apply_valid){
  struct dw_handle mh;const struct dw_snapshot *map;enum dw_result current=dw_acquire(&e->maps,&mh,&map);
  if(current==DW_BUSY)return DE_BUSY;
  if(current)e->apply_valid=false;
  else{
   struct lp_budget current_budget=e->apply_budget;current_budget.collision_index=&e->map_indices[mh.slot];
   if(mh.generation!=e->apply_map_generation){
    struct lp_metrics measured={0};
    enum lp_result checked=validate_slot(&e->paths[e->apply_path.slot],&map->world,e->config,&measured,&current_budget);
    if(checked!=LP_PATH_FOUND)e->apply_valid=false;
   }
   if(dw_snapshot_time(map,now,(uint64_t)e->config->live_age_us,NULL))e->apply_valid=false;
   if(e->apply_valid&&first_adoption){
    struct lp_path *candidate=&e->paths[e->apply_path.slot].path;dw_pose pose;dw_vehicle vehicle;
    enum de_result valid=initial_correspondence_pending(e,candidate)?initial_correspondence_unrun(e,candidate,original_generation):DE_STATE;
    if(!valid)valid=inputs(e,map,now,&pose,&vehicle,NULL);
    if(!valid)valid=initial_correspondence_validate(e,&e->paths[e->apply_path.slot],original_generation,&map->world,pose,&vehicle,now,&current_budget);
    if(valid==DE_BUSY){(void)dw_release(&e->maps,mh);return DE_BUSY;}
    if(valid||dw_source_valid(&e->maps,mh))e->apply_valid=false;
   }
   (void)dw_release(&e->maps,mh);
  }
 }
 if(e->apply_valid&&e->behavior_enabled&&!first_adoption&&!behavior_restore(e,&e->paths[e->apply_path.slot].path,false))e->apply_valid=false;
 enum de_result rc=e->apply_valid&&first_adoption?initial_correspondence_publish(e,e->apply_path,original_generation):
  publish(e,e->apply_path,e->apply_valid);if(rc==DE_BUSY)return rc;
 e->apply_pending=false;
 if(e->apply_valid&&rc==DE_OK){
  const struct lp_path *path=&e->paths[e->apply_path.slot].path;
  if(e->behavior_enabled&&!first_adoption)(void)behavior_restore(e,path,true);
  e->task=path->task;e->basis=path->basis;
  e->maneuver_active=path->direction!=0;e->maneuver_direction=path->direction;e->maneuver_goal=path->merge_goal;e->maneuver_revision=path->maneuver_revision;
  e->merge_passed_reference_revision=path->merge_passed_reference_revision;
  if(e->path_revision<path->revision)e->path_revision=path->revision;
  strcpy(e->maneuver_frame,path->frame);strcpy(e->maneuver_odom,path->odom);
  strcpy(e->maneuver_configuration,path->configuration);strcpy(e->maneuver_reference,path->reference);
 }
 return e->apply_valid?rc:DE_STATE;
}
void de_stop(struct drive_engine *e){atomic_store(&e->stopping,true);}
enum de_result de_restart(struct drive_engine *e){
 if(!e->stopping||e->planning||e->apply_pending)return DE_BUSY;
 if(!take(e))return DE_BUSY;
 for(unsigned k=0;k<DE_PATH_SLOTS;k++)if(readers(&e->paths[k])||e->paths[k].writing){give(e);return DE_BUSY;}
 if(e->epoch==UINT32_MAX||e->requested==UINT32_MAX){give(e);return DE_LIMIT;}
 e->epoch++;e->current=-1;e->started=0;atomic_fetch_add(&e->requested,1);
 memset(&e->tracker,0,sizeof(e->tracker));e->completed=0;e->stopping=false;
 e->maneuver_active=false;e->maneuver_direction=0;e->maneuver_revision=0;e->merge_passed_reference_revision=0;
 e->has_grant=false;memset(&e->grant,0,sizeof(e->grant));
 if(e->behavior_enabled){
  memset(&e->episode,0,sizeof(e->episode));memset(&e->episode_state,0,sizeof(e->episode_state));memset(&e->parking,0,sizeof(e->parking));
  e->episode_request_ready=e->episode_receipt_pending=e->episode_transition=false;e->local_owner=false;
  e->episode_pass_planning=false;
  e->behavior_failed=e->facts_current=e->parking_boundary_pending=false;e->phase_state_message_id[0]=0;
 }
 memset(&e->task,0,sizeof(e->task));memset(&e->basis,0,sizeof(e->basis));
 memset(&e->progress_snapshot,0,sizeof(e->progress_snapshot));e->progress_generation=0;
 atomic_store(&e->parking_stop_generation,0);
 memset(&e->tracked_episode_progress,0,sizeof(e->tracked_episode_progress));
 give(e);return DE_OK;
}
const char *de_reason(enum de_result rc){
 static const char *const reasons[]={"OK","BUSY","INPUT_INVALID","STATE_PREPARING","MAP_UNAVAILABLE","POSE_UNAVAILABLE",
  "PATH_UNAVAILABLE","OBSERVATION_EXPIRED","CONTEXT_CHANGED","STATE_REJECTED","RESOURCE_LIMIT","CANCELLED","CLOCK_ERROR",
  "STATE_FRAME_CHANGED","STATE_CONFIGURATION_CHANGED","STATE_REFERENCE_CHANGED"};
 return (unsigned)rc<sizeof(reasons)/sizeof(*reasons)?reasons[rc]:"INTERNAL_ERROR";
}

void de_behavior_enable(struct drive_engine *e,const uint8_t incarnation[16]){
 if(!e||!incarnation)return;
 if(!e->behavior_enabled){memset(&e->task,0,sizeof(e->task));e->path_invalidated=true;
  if(e->requested<UINT32_MAX)atomic_fetch_add(&e->requested,1);}
 memcpy(e->behavior_incarnation,incarnation,16);e->behavior_enabled=true;
 const struct lp_config *c=e->config;
 e->behavior_limits=(struct db_limits){c->avoid_episode_us,c->avoid_phase_us,c->avoid_progress_us,c->avoid_settle_us,
  c->avoid_travel_m,c->avoid_progress_m,c->avoid_pass_margin_m,c->avoid_heading_rad,c->avoid_steering_rad,c->avoid_policy};
 e->parking_limits=(struct dp_limits){c->parking_goal_distance,c->parking_goal_heading,c->parking_seed_inward_m,
  c->parking_yaw_seed_rad,c->min_control_speed,c->parking_clearance_sufficient,c->parking_clearance_band,
  c->parking_stationary_speed,c->parking_yaw_rate,c->parking_final_window_us};
}
static enum de_result control_facts_current(struct drive_engine *e,const xenova_drive_DriveControlFacts *f,uint64_t now,struct db_facts *out){
 if(!e||!f||f->major!=1||now>=f->valid_until_pi_us||!f->meter.valid)return DE_STATE;
 dw_vehicle vehicle;if(dw_vehicle_sequence(&e->odometry,f->meter.vehicle_sequence,&vehicle))return DE_POSE;
 if(dw_control_facts_validate(f,&vehicle))return DE_STATE;
 if(!vehicle.has_pi_session||now<vehicle.pi_sample_us||now-vehicle.pi_sample_us+vehicle.clock_error_us>e->config->pose_age_us)return DE_EXPIRED;
 struct db_facts facts={.valid=true,.meter_valid=f->meter.valid,.expires_pi_us=f->valid_until_pi_us,
  .vehicle_sequence=f->meter.vehicle_sequence,.meter_revision=f->meter.gap_revision,.meter_ticks=f->meter.total_ticks,
  .metres_per_tick=f->meter.metres_per_tick,.sampled_pi_us=vehicle.pi_sample_us,.clock_error_us=vehicle.clock_error_us};
 memcpy(facts.meter_epoch,f->meter.meter_epoch.bytes,16);
 strcpy(facts.pi_session,vehicle.pi_session);strcpy(facts.device_session,vehicle.device_session);strcpy(facts.odom_epoch,vehicle.odom_epoch);
 if(f->has_stop){const xenova_drive_StopFacts *s=&f->stop;
  facts.stop_applied=s->state>=2&&now<s->valid_until_pi_us&&s->command_sequence&&
   !strcmp(s->pi_session,vehicle.pi_session)&&!strcmp(s->device_session,vehicle.device_session);
  facts.stationary=facts.stop_applied&&s->state==3&&s->stationary_last_sequence>=s->stationary_first_sequence&&
   s->stationary_observed_pi_us>=s->stationary_started_pi_us&&s->left_speed_bound_mps<=e->parking_limits.stationary_speed&&
   s->right_speed_bound_mps<=e->parking_limits.stationary_speed&&s->yaw_rate_bound_rps<=e->parking_limits.yaw_rate;
  facts.stop_calculation=s->calculation_sequence;facts.stop_serial=s->command_sequence;facts.control_generation=s->control_generation;
  facts.plan_revision=s->plan_revision;facts.phase_revision=s->phase_revision;
 }
 *out=facts;return DE_OK;
}
enum de_result de_control_facts(struct drive_engine *e,const xenova_drive_DriveControlFacts *f,uint64_t now){
 struct db_facts facts;enum de_result valid=control_facts_current(e,f,now,&facts);if(valid)return valid;
 if(!take(e))return DE_BUSY;
 bool changed=e->facts.vehicle_sequence!=facts.vehicle_sequence||e->facts.stop_serial!=facts.stop_serial||
  e->facts.stationary!=facts.stationary||e->facts.meter_valid!=facts.meter_valid;
 e->control_facts=*f;e->facts=facts;e->facts_current=true;
 if(changed&&e->requested<UINT32_MAX)atomic_fetch_add(&e->requested,1);
 give(e);return DE_OK;
}
enum de_result de_episode_status(struct drive_engine *e,const xenova_drive_EpisodeStatus *status,uint64_t now){
 if(!e||!status||dw_episode_status_validate(status)||now>=status->valid_until_pi_us)return DE_STATE;
 e->episode_status=*status;
 if(!e->episode_state.core.state&&status->state==1){
  if(dw_episode_initial_none(&e->episode_state,status))return DE_STATE;
  if(e->requested<UINT32_MAX)atomic_fetch_add(&e->requested,1);
 }
 return DE_OK;
}
bool de_episode_request(struct drive_engine *e,xenova_drive_EpisodeRequest *out){
 if(!e||!out||!e->episode_request_ready||e->episode_receipt_pending)return false;
 *out=e->episode_request;return true;
}
bool de_registration_state(struct drive_engine *e,dw_state *out){
 if(!e||!out)return false;
 struct de_reference ph={0};struct dw_handle mh={0};const struct lp_path *p;const dw_world *w;const struct dw_snapshot *map;
 bool owned=!(e->planning&&e->plan_finalized&&e->plan_valid&&e->planning_snapshot);
 if(owned){
  if(de_path_acquire(e,&ph,&p))return false;
  if(dw_acquire(&e->maps,&mh,&map)){(void)de_path_release(e,ph);return false;}w=&map->world;
 }else{p=&e->paths[e->planning_path.slot].path;w=&e->planning_snapshot->world;}
 memset(out,0,sizeof(*out));if(!lp_state_export(p,out)){
  if(owned){(void)de_path_release(e,ph);(void)dw_release(&e->maps,mh);}return false;
 }
 strcpy(out->map_epoch,w->map_epoch);strcpy(out->frame_epoch,p->frame);strcpy(out->odom_epoch,p->odom);strcpy(out->configuration,p->configuration);
 strcpy(out->calibration,w->calibration);strcpy(out->primitive,w->primitive);strcpy(out->reference_id,p->reference);
 out->reference_revision=w->reference_revision;out->path_revision=p->revision;out->maneuver_revision=p->maneuver_revision;
 out->has_world_features=true;out->world_features=p->world_features;out->path_count=p->count;memcpy(out->path,p->points,p->count*sizeof(p->points[0]));
 if(owned){(void)de_path_release(e,ph);(void)dw_release(&e->maps,mh);}return true;
}
void de_episode_submitted(struct drive_engine *e,const xenova_drive_EpisodeRequest *request){
 if(!e||!request||!e->episode_request_ready)return;
 e->episode_request=*request;e->episode_request_ready=false;e->episode_receipt_pending=true;
}
enum de_result de_episode_receipt(struct drive_engine *e,const xenova_drive_EpisodeReceipt *receipt){
 if(!e||!receipt||dw_episode_receipt_validate(receipt)||!e->episode_receipt_pending)return DE_STATE;
 uint8_t hash[32],core[32];
 if(!dw_episode_request_hash(&e->episode_request,hash)||receipt->request_sequence!=e->episode_request.request_sequence||
    memcmp(receipt->request_nonce.bytes,e->episode_request.request_nonce.bytes,16)||memcmp(hash,receipt->request_sha256.bytes,32))return DE_STATE;
 e->episode_receipt_pending=false;
 /* The Pi fixes original guest intervals to their conservative Pi lower bound.
  * Candidate clock estimates are not approval. Everything else stays exact. */
 xenova_drive_EpisodeCore approved=e->episode_candidate.core;bool bounded=true;
 if(receipt->has_snapshot){const xenova_drive_EpisodeCore *actual=&receipt->snapshot.core;
  if(e->episode_request.action==1){approved.started_pi_us=actual->started_pi_us;approved.deadline_pi_us=actual->deadline_pi_us;
   approved.origin_vehicle_sequence=actual->origin_vehicle_sequence;approved.left_baseline_ticks=actual->left_baseline_ticks;
   approved.right_baseline_ticks=actual->right_baseline_ticks;approved.meter_epoch=actual->meter_epoch;
   approved.meter_baseline_total_ticks=actual->meter_baseline_total_ticks;
   if(actual->max_travel_m>approved.max_travel_m||actual->max_travel_m>e->behavior_limits.travel_m)bounded=false;
   approved.max_travel_m=actual->max_travel_m;
  }
  if(e->episode_request.action==1||e->episode_state.core.phase!=approved.phase){
   approved.phase_started_pi_us=actual->phase_started_pi_us;approved.phase_deadline_pi_us=actual->phase_deadline_pi_us;
  }
 }
 if(!bounded||receipt->result!=1||!receipt->has_snapshot||!dw_episode_core_hash(&approved,core)||
    memcmp(core,receipt->snapshot.acceptance.accepted_core_sha256.bytes,32)){
  db_episode_hold(&e->episode,DB_CONTEXT);e->behavior_failed=true;e->plan_valid=false;e->path_invalidated=true;return DE_STATE;
 }
 e->episode_state=receipt->snapshot;
 /* Only the exact accepted request reserves the next fullState ID. Preparing
  * another candidate must not relabel the current registered path/completion. */
 behavior_uuid(e->phase_state_message_id,e->episode_request.phase_state_message_id.bytes);
 e->episode.started_pi_us=receipt->snapshot.core.started_pi_us;e->episode.deadline_pi_us=receipt->snapshot.core.deadline_pi_us;
 e->episode.phase_entered_pi_us=receipt->snapshot.core.phase_started_pi_us;e->episode.phase_deadline_pi_us=receipt->snapshot.core.phase_deadline_pi_us;
 e->episode.meter_start_ticks=receipt->snapshot.core.meter_baseline_total_ticks;
 e->episode.meter_used_ticks=receipt->snapshot.progress.used_travel_ticks;
 e->episode.last_progress_pi_us=receipt->snapshot.progress.last_progress_pi_us;
 if(e->planning&&e->plan_finalized){struct lp_path *p=&e->paths[e->planning_path.slot].path;p->has_episode=true;p->episode=e->episode_state;}
 e->episode_transition=false;return DE_OK;
}
static bool same_stop(const struct drive_engine *e,const struct lp_path *p,uint64_t now){
 const xenova_drive_StopFacts *s=&e->control_facts.stop;const xenova_drive_MissionGrant *g=&e->grant;
 return e->facts.valid&&now<e->facts.expires_pi_us&&e->facts.stop_applied&&e->facts.stationary&&now<s->valid_until_pi_us&&
  e->facts.control_generation==g->control_generation&&e->facts.plan_revision==p->revision&&e->facts.phase_revision==p->maneuver_revision&&
  s->producer_role==1&&s->calculation_sequence&&s->mission_revision==g->mission_revision&&s->grant_revision==g->grant_revision&&
  !memcmp(s->mission_id.bytes,g->mission_id.bytes,16)&&!memcmp(s->source_vm.bytes,g->owner_vm.bytes,16)&&
  !memcmp(s->source_execution.bytes,g->owner_execution.bytes,16)&&!memcmp(s->source_app_instance.bytes,g->app_instance.bytes,16);
}
static bool behavior_restore(struct drive_engine *e,const struct lp_path *p,bool commit){
 if(!(p->world_features&DW_LANE_MODEL))return true;
 if(!p->has_episode||dw_episode_snapshot_validate(&p->episode))return false;
 memset(&e->restore_episode,0,sizeof(e->restore_episode));
 if(p->episode.core.state==1&&p->episode.core.phase==DB_FINISHED){
  /* Closed history is provenance, not a present motion lease or meter epoch. */
  e->restore_episode.active=e->restore_episode.terminal=true;e->restore_episode.phase=DB_FINISHED;
  e->restore_episode.home_lane_id=p->episode.core.home_lane_id;e->restore_episode.target_lane_id=p->episode.core.target_lane_id;
 }else if(p->episode.core.state==2){
  const xenova_drive_EpisodeCore *c=&p->episode.core;const xenova_drive_EpisodeProgress *g=&p->episode.progress;
  struct db_episode *out=&e->restore_episode;
  if(!e->facts.valid||!e->facts.meter_valid||memcmp(c->meter_epoch.bytes,e->facts.meter_epoch,16)||
     e->facts.meter_ticks<c->meter_baseline_total_ticks||g->used_travel_ticks>e->facts.meter_ticks-c->meter_baseline_total_ticks)return false;
  out->active=true;out->terminal=c->phase==DB_FINISHED;out->phase=(enum db_phase)c->phase;out->policy=c->policy;
  memcpy(out->id,c->episode_id.bytes,16);out->revision=c->revision;out->phase_revision=c->phase_revision;
  out->home_lane_id=c->home_lane_id;out->target_lane_id=c->target_lane_id;out->phase_goal=c->phase_goal;
  out->started_pi_us=c->started_pi_us;out->deadline_pi_us=c->deadline_pi_us;out->phase_entered_pi_us=c->phase_started_pi_us;
  out->phase_deadline_pi_us=c->phase_deadline_pi_us;out->last_progress_pi_us=g->last_progress_pi_us;
  out->progress_lower=g->station_lower_m;out->remaining_upper=g->remaining_upper_m;out->last_vehicle_sequence=g->vehicle_sequence;
  out->meter_start_ticks=c->meter_baseline_total_ticks;out->meter_used_ticks=g->used_travel_ticks;out->metres_per_tick=e->facts.metres_per_tick;
  out->meter_revision=e->facts.meter_revision;memcpy(out->meter_epoch,c->meter_epoch.bytes,16);
  strcpy(out->pi_session,e->facts.pi_session);strcpy(out->device_session,e->facts.device_session);strcpy(out->odom_epoch,e->facts.odom_epoch);
  out->basis=p->basis;if(c->basis_prefix_count>out->basis.count)return false;out->basis.count=c->basis_prefix_count;out->basis.revision=c->basis_revision;
  uint8_t hash[32];if(!dw_basis_prefix_hash(out->basis.points,out->basis.count,hash)||memcmp(hash,c->basis_prefix_sha256.bytes,32))return false;
  out->blocker.present=true;out->blocker.observation_sequence=c->blocker_observation_sequence;strcpy(out->blocker.map,p->basis.map);
  out->blocker.lower=(dw_point){INFINITY,INFINITY};out->blocker.upper=(dw_point){-INFINITY,-INFINITY};
  for(unsigned k=0;k<c->blocker_count;k++){
   out->blocker.lower.x=fminf(out->blocker.lower.x,c->blocker[k].x);out->blocker.lower.y=fminf(out->blocker.lower.y,c->blocker[k].y);
   out->blocker.upper.x=fmaxf(out->blocker.upper.x,c->blocker[k].x);out->blocker.upper.y=fmaxf(out->blocker.upper.y,c->blocker[k].y);
  }
  if(!db_blocker_locate(&out->basis,&out->blocker,c->blocker_position_bound_m))return false;
 }
 if(!commit)return true;
 e->episode=e->restore_episode;e->episode_state=p->episode;
 if(p->execution.mission.kind==2){
  const xenova_drive_MissionExecution *m=&p->execution.mission;struct dp_state *parking=&e->parking;
  memset(parking,0,sizeof(*parking));parking->active=true;parking->original_grant=e->grant;
  parking->phase=(enum dp_phase)m->phase;parking->phase_revision=p->maneuver_revision;parking->published_phase=m->phase;
  parking->locked=e->grant.execution_deadline_pi_us!=0;parking->selected_slot=m->has_slot_id?m->slot_id:0;
  parking->selected_slot_revision=m->slot_revision;parking->goal=m->goal;parking->goal_revision=m->goal_revision;parking->space=p->space;
  if(m->selected_slot_polygon_count==4){parking->space.parking=true;parking->space.slot_id=m->slot_id;parking->space.slot_revision=m->slot_revision;
   memcpy(parking->space.polygon,m->selected_slot_polygon,sizeof(parking->space.polygon));}
  parking->goals=(struct lp_goal_set){.count=1,.poses={m->goal},.distance=e->parking_limits.goal_distance,
   .heading=e->parking_limits.goal_heading,.final_gear=m->strategy==1?-1:1};
  if(m->phase==DP_APPROACH)parking->best_approach=*p;else if(m->phase>=DP_ALIGN_STOP&&m->phase<=DP_FINAL_VERIFY)parking->best_maneuver=*p;
  if(e->grant.has_locked_goal_sha256)memcpy(parking->selected_goal_sha256,e->grant.locked_goal_sha256.bytes,32);
  parking->terminal=m->phase==DP_HOLD;parking->result=m->result==1?DP_FEASIBLE:DP_PENDING;
 }
 e->episode_transition=e->episode_pass_planning=false;e->behavior_failed=false;return true;
}
static bool behavior_phase(struct drive_engine *e,enum db_phase phase,dw_pose goal,uint64_t now){
 bool changed=e->episode.phase!=phase;
 if(!db_episode_phase(&e->episode,phase,goal,now,&e->behavior_limits))return false;
 if(changed)e->episode_pass_planning=false;
 if(changed&&(!e->budget.clock||e->budget.clock(e->budget.context,&e->phase_began_guest_us))){e->behavior_failed=true;return false;}
 return true;
}
static bool lane_settled(struct drive_engine *e,const dw_world *w,const struct tr_intent *task,dw_pose pose,const dw_vehicle *v,
 uint32_t lane,bool contained,uint64_t now){
 struct tr_intent only=*task;only.changing=false;only.from_lane_id=only.selected_lane_id=lane;
 struct lp_road_state road;float steer=v->has_measured_steering_rad?v->measured_steering_rad:v->estimated_steering_rad;
 bool fit=contained&&
  lp_task_project(w,&only,pose,&road)==LP_PATH_FOUND&&
  fabsf(dw_angle(pose.yaw-road.pose.yaw))+v->heading_error_rad<=e->behavior_limits.heading_rad&&
  fabsf(steer-atanf(e->config->wheelbase*road.curvature))+v->steering_error_rad<=e->behavior_limits.steering_rad;
 if(!fit){e->episode.settle_since_pi_us=0;return false;}
 if(!e->episode.settle_since_pi_us)e->episode.settle_since_pi_us=v->pi_sample_us;
 return v->pi_sample_us>=e->episode.settle_since_pi_us&&v->pi_sample_us-e->episode.settle_since_pi_us>=e->behavior_limits.settle_us&&now>=v->pi_sample_us;
}
static enum de_result behavior_prepare(struct drive_engine *e,const dw_world *w,dw_pose pose,const dw_vehicle *v,
 uint64_t now,float clearance,struct tr_intent *task,dw_pose *goal){
 if(!db_limits_valid(&e->behavior_limits)||e->behavior_failed||!e->facts_current||!e->facts.valid||now>=e->facts.expires_pi_us||!e->episode_state.core.state)return DE_STATE;
 if(e->episode_state.core.state==1&&!e->episode_state.core.phase&&e->episode_status.commit_state<2){
  float horizon=fmaxf(e->config->goal_distance*2,e->config->primitive_speed*e->config->primitive_duration);
  return lp_task_goal(w,task,pose,horizon,e->config->front+e->config->margin,goal)==LP_PATH_FOUND?DE_OK:DE_PATH;
 }
 /* The registered phase milestone and the swept local terminal have separate lifetimes. */
 const float phase_clearance=e->config->front+e->config->margin;
 if(e->episode.active&&e->episode.terminal&&e->episode.phase==DB_FINISHED){
  struct db_blocker next;if(!db_blocker_observe(w,task,pose,e->config->plan_horizon,e->config,&next))return DE_PATH;
  if(next.present&&!next.unknown)e->episode.active=false;
 }
 if(e->episode.active){
  if(!e->episode.terminal){
   struct tr_intent home=*task;home.changing=false;home.from_lane_id=home.selected_lane_id=e->episode.home_lane_id;
   struct db_blocker observed;
   if(!db_blocker_observe(w,&home,pose,e->config->plan_horizon,e->config,&observed))return DE_PATH;
   if(observed.present){
    struct db_blocker *original=&e->episode.blocker;
    original->lower.x=fminf(original->lower.x,observed.lower.x);original->lower.y=fminf(original->lower.y,observed.lower.y);
    original->upper.x=fmaxf(original->upper.x,observed.upper.x);original->upper.y=fmaxf(original->upper.y,observed.upper.y);
    if(!db_blocker_locate(&e->episode.basis,original,w->localization.geometry_position_bound_m)){
     original->station_upper=INFINITY;return DE_PATH;
    }
    /* New raster extents preserve the registered phase. Tracking checks the
     * current grid and stopping sweep before every command. */
   }
  }
  enum db_reason reason=db_episode_step(&e->episode,w,pose,v,now,&e->facts,&e->behavior_limits,e->config,false);
  if(reason){e->behavior_failed=true;e->path_invalidated=true;return DE_STATE;}
  uint32_t arrival=e->episode.phase==DB_CHANGE_OUT?e->episode.target_lane_id:
                   e->episode.phase==DB_REJOIN?e->episode.home_lane_id:0;
  bool contained=arrival&&tr_unique_lane(w,pose,task->vehicle_pad+e->config->tracking_guard,e->config,e->budget.collision_index)==arrival;
  if(e->episode.phase==DB_CHANGE_OUT&&lane_settled(e,w,task,pose,v,e->episode.target_lane_id,contained,now)){
   task->changing=false;task->from_lane_id=task->selected_lane_id=e->episode.target_lane_id;
   dw_pose phase_goal;
   if(lp_task_goal(w,task,pose,e->config->plan_horizon,phase_clearance,&phase_goal)!=LP_PATH_FOUND)return DE_PATH;
   if(!behavior_phase(e,DB_PASS,phase_goal,now))return DE_STATE;
   e->episode_pass_planning=true;
   e->episode_transition=true;
  }else if(e->episode.phase==DB_PASS){
   float radius=hypotf(e->config->front,e->config->half_width),lo,hi,pad=v->position_error_m+radius*v->heading_error_rad;
   if(!db_station_bounds(&e->episode.basis,pose,pad,e->config,&lo,&hi))return DE_STATE;
   if(lo>e->episode.blocker.station_upper+e->behavior_limits.pass_margin_m){
    if(e->episode.policy==DB_KEEP_TARGET){
     if(!behavior_phase(e,DB_FINISHED,e->episode.phase_goal,now))return DE_STATE;
     e->episode.terminal=true;
     task->home_lane_id=task->from_lane_id=task->selected_lane_id=e->episode.target_lane_id;task->changing=false;
    }else{
     task->from_lane_id=e->episode.target_lane_id;task->selected_lane_id=e->episode.home_lane_id;task->changing=true;
     dw_pose phase_goal;
     if(lp_task_goal(w,task,pose,e->config->plan_horizon,phase_clearance,&phase_goal)!=LP_PATH_FOUND)return DE_PATH;
     if(!behavior_phase(e,DB_REJOIN,phase_goal,now))return DE_STATE;
    }
    e->episode_transition=true;
   }
  }else if(e->episode.phase==DB_REJOIN&&lane_settled(e,w,task,pose,v,e->episode.home_lane_id,contained,now)){
   if(!behavior_phase(e,DB_FINISHED,e->episode.phase_goal,now))return DE_STATE;
   task->changing=false;task->from_lane_id=task->selected_lane_id=e->episode.home_lane_id;e->episode.terminal=true;e->episode_transition=true;
  }
  /* Once the body is in the arrival lane, follow that lane while settling.
   * The episode phase still advances only after the existing dwell proof. */
  if(e->episode.phase==DB_CHANGE_OUT){task->selected_lane_id=e->episode.target_lane_id;
   task->from_lane_id=contained?task->selected_lane_id:e->episode.home_lane_id;task->changing=!contained;}
  else if(e->episode.phase==DB_PASS){task->from_lane_id=task->selected_lane_id=e->episode.target_lane_id;task->changing=false;}
  else if(e->episode.phase==DB_REJOIN){task->selected_lane_id=e->episode.home_lane_id;
   task->from_lane_id=contained?task->selected_lane_id:e->episode.target_lane_id;task->changing=!contained;}
 }
 if(!e->episode.active){
  struct db_blocker blocker;if(!db_blocker_observe(w,task,pose,e->config->plan_horizon,e->config,&blocker))return DE_PATH;
  if(blocker.present){
   if(!e->local_owner)return DE_WAITING;
   dw_vehicle birth_vehicle;enum dw_result birth_sample=dw_vehicle_sequence(&e->odometry,e->facts.vehicle_sequence,&birth_vehicle);
   if(birth_sample)return birth_sample==DW_BUSY?DE_BUSY:DE_POSE;
   /* UNKNOWN is never an occupied blocker or free permission. The selected
    * complete path and stopping sweep still independently reject unknown. */
   blocker.unknown=false;
   if(!db_blocker_locate(&e->basis,&blocker,w->localization.geometry_position_bound_m))return DE_PATH;
   struct tr_view view;if(!tr_open_indexed(w,task,e->budget.collision_index,&view)||e->episode_counter==UINT64_MAX)return DE_STATE;
   uint8_t id[16];memcpy(id,e->behavior_incarnation,16);uint64_t n=++e->episode_counter;
   for(unsigned k=0;k<8;k++)id[15-k]^=(uint8_t)(n>>(k*8));
   uint32_t target=view.selected->adjacent_lane_id;task->from_lane_id=task->selected_lane_id;task->selected_lane_id=target;task->changing=true;
   dw_pose phase_goal;
   if(lp_task_goal(w,task,pose,e->config->plan_horizon,phase_clearance,&phase_goal)!=LP_PATH_FOUND||
      !db_episode_begin(&e->episode,id,task->home_lane_id,target,&e->basis,&blocker,phase_goal,now,&e->facts,&e->behavior_limits)||
      !db_episode_phase(&e->episode,DB_CHANGE_OUT,phase_goal,now,&e->behavior_limits))return DE_STATE;
   /* Search completion may arrive after several newer Vehicle samples. Keep
    * the actual birth witness; those newer samples are current progress, not
    * permission to move the original birth/meter baseline forward. */
   e->episode_candidate.core.origin_vehicle_sequence=birth_vehicle.sequence;
   e->episode_candidate.core.left_baseline_ticks=birth_vehicle.left_ticks;
   e->episode_candidate.core.right_baseline_ticks=birth_vehicle.right_ticks;
   float lo,hi,radius=hypotf(e->config->front,e->config->half_width),pad=v->position_error_m+radius*v->heading_error_rad;
   if(!db_station_bounds(&e->episode.basis,pose,pad,e->config,&lo,&hi))return DE_STATE;
   e->episode.progress_lower=lo;e->episode.remaining_upper=hypotf(pose.x-phase_goal.x,pose.y-phase_goal.y)+pad;
   e->episode.last_vehicle_sequence=v->sequence;
   if(e->budget.clock)e->budget.clock(e->budget.context,&e->episode_began_guest_us);
   e->phase_began_guest_us=e->episode_began_guest_us;
   e->episode_transition=true;
  }
 }
 return lp_task_goal(w,task,pose,e->config->plan_horizon,clearance,goal)==LP_PATH_FOUND?DE_OK:DE_PATH;
}
static void source_context(const dw_world *w,xenova_drive_EpisodeSourceContext *s){
 memset(s,0,sizeof(*s));strcpy(s->vehicle,w->vehicle);strcpy(s->mapper_boot,w->mapper_boot);strcpy(s->mapper_instance,w->mapper_instance);
 strcpy(s->map_epoch,w->map_epoch);strcpy(s->frame_epoch,w->frame_epoch);strcpy(s->odom_epoch,w->odom_epoch);strcpy(s->clock_epoch,w->clock_epoch);
 strcpy(s->configuration,w->configuration);strcpy(s->calibration,w->calibration);s->primitive.size=32;
 for(unsigned k=0;k<32;k++){unsigned value=0;for(unsigned j=0;j<2;j++){char ch=w->primitive[2*k+j];value=value*16+(ch<='9'?ch-'0':ch-'a'+10);}s->primitive.bytes[k]=(uint8_t)value;}
 strcpy(s->reference_id,w->reference_id);s->reference_revision=w->reference_revision;s->world_sequence=w->sequence;s->observation_sequence=w->observation_seq;
}
static void behavior_uuid(char text[37],const uint8_t bytes[16]){
 static const char hex[]="0123456789abcdef";unsigned at=0;
 for(unsigned k=0;k<16;k++){if(k==4||k==6||k==8||k==10)text[at++]='-';text[at++]=hex[bytes[k]>>4];text[at++]=hex[bytes[k]&15];}text[at]=0;
}
static bool grant_key_same(const xenova_drive_MissionGrant *a,const xenova_drive_MissionGrant *b){
 return a->admitted==b->admitted&&a->control_generation==b->control_generation&&a->grant_revision==b->grant_revision&&
  a->mission_revision==b->mission_revision&&a->cancel_fence_revision==b->cancel_fence_revision&&a->scene_revision==b->scene_revision&&
  a->kind==b->kind&&a->strategy==b->strategy&&a->mission_deadline_pi_us==b->mission_deadline_pi_us&&
  a->execution_deadline_pi_us==b->execution_deadline_pi_us&&a->search_deadline_pi_us==b->search_deadline_pi_us&&
  a->correction_attempts_used==b->correction_attempts_used&&a->execution_travel_m==b->execution_travel_m&&
  a->search_travel_m==b->search_travel_m&&a->max_execution_travel_m==b->max_execution_travel_m&&a->max_search_travel_m==b->max_search_travel_m&&
  a->has_locked_goal_sha256==b->has_locked_goal_sha256&&(!a->has_locked_goal_sha256||!memcmp(a->locked_goal_sha256.bytes,b->locked_goal_sha256.bytes,32))&&
  a->has_operation_binding==b->has_operation_binding&&(!a->has_operation_binding||dw_operation_binding_equal(&a->operation_binding,&b->operation_binding))&&
  !memcmp(a->mission_id.bytes,b->mission_id.bytes,16)&&!memcmp(a->scene_admission_id.bytes,b->scene_admission_id.bytes,16)&&
  !memcmp(a->operator_request_nonce.bytes,b->operator_request_nonce.bytes,16)&&
  !memcmp(a->owner_vm.bytes,b->owner_vm.bytes,16)&&!memcmp(a->owner_execution.bytes,b->owner_execution.bytes,16)&&!memcmp(a->app_instance.bytes,b->app_instance.bytes,16);
}
static bool grant_same(const xenova_drive_MissionGrant *a,const xenova_drive_MissionGrant *b){
 return a->admitted&&b->admitted&&grant_key_same(a,b);
}
static enum de_result behavior_candidate(struct drive_engine *e,struct lp_path *path,const dw_world *w,uint64_t now){
 if(!w||!(w->required_features&DW_LANE_MODEL))return DE_OK;
 if(!e->episode_state.core.state)return DE_STATE;
 path->has_episode=true;path->episode=e->episode_state;
 if(path->execution.present)path->execution.features|=DW_EPISODE;
 if(!e->episode.active||(!e->episode_transition&&e->episode.phase==DB_FINISHED)){
  if(e->episode_state.core.state==1&&!e->episode_state.core.phase&&e->episode_status.commit_state<2&&(path->direction||path->task.changing))return DE_STATE;
  return DE_OK;
 }
 if(!e->has_grant||!grant_same(&e->grant,&e->planning_grant)||e->episode_request_sequence==INT64_MAX)return DE_STATE;
 xenova_drive_EpisodeCore core=e->episode_state.core;
 bool birth=core.state!=2||memcmp(core.episode_id.bytes,e->episode.id,16);
 if(birth){
  memset(&core,0,sizeof(core));core.state=2;core.episode_id.size=core.birth_snapshot_id.size=core.meter_epoch.size=16;
  core.basis_prefix_sha256.size=core.source_geometry_sha256.size=32;memcpy(core.episode_id.bytes,e->episode.id,16);
  memcpy(core.birth_snapshot_id.bytes,e->episode_status.state_message_id.bytes,16);memcpy(core.meter_epoch.bytes,e->episode.meter_epoch,16);
  core.home_lane_id=e->episode.home_lane_id;core.target_lane_id=e->episode.target_lane_id;core.policy=e->episode.policy;
  core.blocker_count=4;dw_point low=e->episode.blocker.lower,high=e->episode.blocker.upper;
  core.blocker[0]=low;core.blocker[1]=(dw_point){high.x,low.y};core.blocker[2]=high;core.blocker[3]=(dw_point){low.x,high.y};
  core.blocker_position_bound_m=w->localization.geometry_position_bound_m;core.blocker_world_sequence=w->sequence;
  core.blocker_observation_sequence=e->episode.blocker.observation_sequence;core.basis_revision=e->episode.basis.revision;
  core.basis_prefix_count=e->episode.basis.count;
  xenova_drive_EpisodeSourceContext source;source_context(w,&source);
  if(!dw_basis_prefix_hash(e->episode.basis.points,e->episode.basis.count,core.basis_prefix_sha256.bytes)||
     !dw_episode_source_hash(&source,core.source_geometry_sha256.bytes))return DE_STATE;
  if(!e->episode_candidate.core.origin_vehicle_sequence)return DE_POSE;
  core.origin_vehicle_sequence=e->episode_candidate.core.origin_vehicle_sequence;
  core.left_baseline_ticks=e->episode_candidate.core.left_baseline_ticks;core.right_baseline_ticks=e->episode_candidate.core.right_baseline_ticks;
  core.started_pi_us=e->episode.started_pi_us;core.deadline_pi_us=e->episode.deadline_pi_us;
  core.meter_baseline_total_ticks=e->episode.meter_start_ticks;core.max_travel_m=e->behavior_limits.travel_m;
 }
 if(core.revision==INT64_MAX)return DE_LIMIT;
 /* Registered birth blocker/source remains immutable across every replan. */
 core.revision++;core.phase=e->episode.phase;core.phase_revision=e->episode.phase_revision;
 if(core.phase==DB_FINISHED)core.state=1;
 core.phase_started_pi_us=e->episode.phase_entered_pi_us;core.phase_deadline_pi_us=e->episode.phase_deadline_pi_us;
 core.phase_goal=e->episode.phase_goal;core.progress_mode=core.phase==DB_PASS?1:2;
 e->episode_candidate=e->episode_state;e->episode_candidate.core=core;
 e->episode_candidate.progress=(xenova_drive_EpisodeProgress){.used_travel_ticks=e->episode.meter_used_ticks,
  .last_progress_pi_us=e->episode.last_progress_pi_us,.vehicle_sequence=e->episode.last_vehicle_sequence,
  .station_lower_m=e->episode.progress_lower,.remaining_upper_m=e->episode.remaining_upper,.revision=e->episode_state.progress.revision+1};
 path->maneuver_revision=core.phase_revision;path->episode=e->episode_candidate;
 path->phase=core.phase==DB_REJOIN?2:core.phase<DB_FINISHED?1:0;
 if(core.phase==DB_FINISHED){path->direction=0;e->maneuver_active=false;e->maneuver_direction=0;}
 xenova_drive_EpisodeRequest *r=&e->episode_request;memset(r,0,sizeof(*r));r->major=1;r->request_sequence=++e->episode_request_sequence;
 r->request_nonce.size=r->mission_id.size=r->scene_admission_id.size=r->expected_registry_epoch.size=r->expected_episode_id.size=r->phase_state_message_id.size=16;
 r->clock_reply_sha256.size=r->plan_sha256.size=32;
 memcpy(r->request_nonce.bytes,e->behavior_incarnation,16);for(unsigned k=0;k<8;k++)r->request_nonce.bytes[15-k]^=(uint8_t)(r->request_sequence>>(k*8));
 memcpy(r->phase_state_message_id.bytes,r->request_nonce.bytes,16);r->phase_state_message_id.bytes[0]^=0x50;
 r->action=birth?1:core.phase==DB_FINISHED?3:2;r->candidate=core;r->progress=e->episode_candidate.progress;
 r->mission_revision=e->grant.mission_revision;r->expected_grant_revision=e->grant.grant_revision;r->cancel_fence_revision=e->grant.cancel_fence_revision;
 r->control_generation=e->grant.control_generation;memcpy(r->mission_id.bytes,e->grant.mission_id.bytes,16);
 memcpy(r->scene_admission_id.bytes,e->grant.scene_admission_id.bytes,16);r->scene_revision=e->grant.scene_revision;
 source_context(w,&r->source);r->began_guest_us=birth?e->episode_began_guest_us:0;r->phase_started_guest_us=e->phase_began_guest_us;
 memcpy(r->expected_registry_epoch.bytes,e->episode_state.acceptance.registry_epoch.bytes,16);
 r->expected_registry_revision=e->episode_state.acceptance.registry_revision;
 memcpy(r->expected_episode_id.bytes,e->episode_state.core.episode_id.bytes,16);r->expected_episode_revision=e->episode_state.core.revision;
 r->plan_revision=path->revision;r->phase_revision=path->maneuver_revision;r->calculation_sequence=e->last_actual_calculation;
 r->vehicle_sequence=e->facts.vehicle_sequence;e->episode_pass_planning=false;
 e->episode_request_ready=true;e->episode_transition=true;
 (void)now;return DE_BUSY;
}
static void parking_metadata(struct drive_engine *e,struct lp_path *p,const xenova_drive_MissionGrant *g){
 xenova_drive_MissionExecution *m=&p->execution.mission;
 m->phase=e->parking.phase;m->strategy=g->strategy;m->mission_deadline_pi_us=g->mission_deadline_pi_us;
 m->search_deadline_pi_us=g->search_deadline_pi_us;m->execution_deadline_pi_us=g->execution_deadline_pi_us;
 m->execution_travel_m=g->execution_travel_m;m->search_travel_m=g->search_travel_m;m->correction_attempts_used=g->correction_attempts_used;
 if(e->parking.selected_slot){
  m->has_slot_id=m->has_slot_revision=m->has_goal=m->has_goal_revision=true;
  m->has_goal_position_bound_m=m->has_goal_heading_bound_rad=true;m->slot_id=e->parking.selected_slot;
  m->slot_revision=e->parking.selected_slot_revision;m->goal=e->parking.goal;m->goal_revision=e->parking.goal_revision;
  m->goal_position_bound_m=e->parking_limits.goal_distance;m->goal_heading_bound_rad=e->parking_limits.goal_heading;
  m->selected_slot_polygon_count=4;memcpy(m->selected_slot_polygon,e->parking.space.polygon,sizeof(e->parking.space.polygon));
 }
 if(e->parking.terminal)m->result=e->parking.result==DP_FEASIBLE?1:3;
}
static bool parking_expired(const struct drive_engine *e,uint64_t now){
 return now>=e->grant.mission_deadline_pi_us||
  now>=(e->parking.locked?e->grant.execution_deadline_pi_us:e->grant.search_deadline_pi_us);
}
static enum de_result parking_publish_pending(struct drive_engine *e,uint64_t now){
 struct dp_state *parking=&e->parking;
 const struct lp_path *source=parking->publish_source;
 if(parking->publish_source_generation&&(e->current<0||
    e->paths[e->current].generation!=parking->publish_source_generation)){
  parking->publish_source=NULL;return DE_CHANGED;
 }
 if(parking->publish_new_revision&&e->path_revision==UINT64_MAX){parking->publish_source=NULL;return DE_LIMIT;}
 size_t index=source->initial_tracker_index;
 unsigned segment=source->execution.mission.active_segment;
 uint64_t sample=source->initial_pi_us;
 if(parking->publish_source_generation){
  if(!take(e))return DE_BUSY;
  if(e->progress_generation==parking->publish_source_generation&&e->progress_snapshot.initialized){
   index=e->progress_snapshot.index;
   segment=e->progress_snapshot.segment;
   sample=e->progress_snapshot.last_pi_us;
  }
  give(e);
 }else if(parking->publish_new_revision){
  index=0;segment=0;sample=0;
 }
 struct de_reference h;struct lp_path *p;enum de_result rc=reserve(e,&h,&p);
 if(rc){if(rc!=DE_BUSY)parking->publish_source=NULL;return rc;}
 *p=*source;
 if(parking->publish_new_revision)p->revision=e->path_revision+1;
 p->maneuver_revision=e->parking.phase_revision;
 mission_metadata(p,&e->grant);parking_metadata(e,p,&e->grant);
 p->initial_tracker_index=index;p->initial_pi_us=sample;
 p->execution.mission.active_segment=segment;
 if(e->behavior_enabled){p->has_episode=true;p->episode=e->episode_state;p->execution.features|=DW_EPISODE;}
 struct dw_handle mh;const struct dw_snapshot *map;
 enum dw_result world=dw_acquire(&e->maps,&mh,&map);
 if(world)rc=world==DW_BUSY?DE_BUSY:DE_MAP;
 else{
  uint64_t scene=0;world=dw_scene_time(&e->maps,&map->world,now,NULL,&scene);
  if(!world){
   struct lp_metrics metrics={0};struct lp_budget b=parking->publish_budget;
   b.collision_index=&e->map_indices[mh.slot];
   enum lp_result checked=validate_slot(&e->paths[h.slot],&map->world,e->config,&metrics,&b);
   if(checked!=LP_PATH_FOUND)
    rc=checked==LP_DEADLINE?DE_EXPIRED:checked==LP_CANCELLED?DE_CANCELLED:DE_STATE;
   if(!rc&&!parking->publish_source_generation){
    /* A newly selected path starts at index zero with a real sample anchor. */
    dw_pose pose;dw_vehicle vehicle;
    if(dw_current_pose(&map->world,&e->odometry,now,e->config->pose_age_us,&pose,&vehicle))rc=DE_BUSY;
    else p->initial_pi_us=vehicle.pi_sample_us;
   }
   if(!rc)world=dw_source_valid(&e->maps,mh);
  }
  if(world)rc=world==DW_BUSY?DE_BUSY:DE_CHANGED;
  e->paths[h.slot].scene_generation=scene;
  if(!rc)rc=publish(e,h,true);
  (void)dw_release(&e->maps,mh);
 }
 if(rc)discard_path(e,h);
 if(rc==DE_EXPIRED&&!parking_expired(e,now)){
  if(!parking->publish_selection){
   /* A phase transition still needs this exact path. Revalidate it on the next
    * planner turn with a fresh validation budget, within the original grant. */
   parking->publish_revalidate=true;
   return DE_WAITING;
  }
  /* An unpublished candidate can be replaced. Keep the current stopped phase
   * on the published path and restart selection from fresh inputs. */
  parking->publish_source=NULL;
  parking->phase=DP_EVALUATE;parking->result=DP_UNKNOWN_BUDGET;
  parking->has_best=false;parking->round.initialized=false;parking->round_deadline_us=0;
  if(e->requested<UINT32_MAX)atomic_fetch_add(&e->requested,1);
  return DE_WAITING;
 }
 if(rc!=DE_BUSY)parking->publish_source=NULL;
 if(rc==DE_CHANGED||rc==DE_EXPIRED||rc==DE_STATE){
  parking->phase=DP_HOLD;parking->terminal=true;
  parking->result=rc==DE_EXPIRED?DP_UNKNOWN_BUDGET:DP_INPUT_INVALID;
  e->behavior_failed=true;e->path_invalidated=true;
 }
 if(!rc){
  if(parking->publish_new_revision)e->path_revision=p->revision;
  e->task=p->task;e->basis=p->basis;e->parking.published_phase=e->parking.phase;
  atomic_store(&e->parking_boundary_pending,e->parking.phase==DP_ALIGN_STOP&&!e->parking.boundary_ready&&p->space.parking&&p->count>=2);
  if(source==&e->parking.best_approach)e->parking.best_approach=*p;
  if(source==&e->parking.best_maneuver)e->parking.best_maneuver=*p;
  if(parking->publish_selection){
   parking->request_ready=!parking->resuming_plan;parking->result=DP_FEASIBLE;parking->resuming_plan=false;
  }
 }
 return rc;
}
static enum de_result parking_publish(struct drive_engine *e,const struct lp_path *source,uint64_t now,bool new_revision,bool selection){
 struct dp_state *p=&e->parking;
 p->publish_source=source;p->publish_new_revision=new_revision;p->publish_selection=selection;
 p->publish_budget=e->budget;p->publish_budget.collision_index=NULL;p->publish_revalidate=false;
 p->publish_source_generation=source==&p->best_approach||source==&p->best_maneuver||e->current<0?
  0:e->paths[e->current].generation;
 return parking_publish_pending(e,now);
}
static enum de_result parking_poll(struct drive_engine *e,const dw_world *w,dw_pose pose,const dw_vehicle *v,uint64_t now){
 struct dp_state *p=&e->parking;
 if(!p->active||!dp_limits_valid(&e->parking_limits))return DE_STATE;
 if(parking_expired(e,now)){p->publish_source=NULL;p->phase=DP_HOLD;p->terminal=true;p->result=DP_UNKNOWN_BUDGET;e->behavior_failed=true;return DE_STATE;}
 if(p->locked){
  const xenova_drive_ParkingSlot *s=dp_slot(w,p->selected_slot);
  if(!s||s->geometry_revision!=p->selected_slot_revision||!dp_slot_eligible(w,s,e->grant.strategy)){
   p->publish_source=NULL;p->phase=DP_HOLD;p->terminal=true;p->result=DP_INPUT_INVALID;e->behavior_failed=true;return DE_STATE;
  }
 }
 if(p->publish_source){
  if(p->publish_revalidate){
   p->publish_budget=e->budget;p->publish_budget.collision_index=NULL;p->publish_revalidate=false;
  }
  return parking_publish_pending(e,now);
 }
 struct de_reference h;const struct lp_path *current=NULL;
 if(de_path_acquire(e,&h,&current))return DE_OK;
 if(p->phase==DP_SEARCH&&(!e->episode.active||e->episode.phase==DB_FINISHED)){
  bool eligible=false;for(unsigned k=0;k<w->parking_slots_count;k++)eligible|=dp_slot_eligible(w,&w->parking_slots[k],e->grant.strategy);
  if(eligible){p->phase=DP_EVAL_STOP;p->phase_revision++;p->phase_entered_pi_us=now;}
 }
 if(p->phase==DP_EVAL_STOP&&current->execution.mission.phase==DP_EVAL_STOP&&same_stop(e,current,now)){
  p->phase=DP_EVALUATE;p->phase_revision++;p->phase_entered_pi_us=now;
  if(!dp_round_begin(&p->round,w,&e->grant,pose)){p->phase=DP_HOLD;p->result=DP_INPUT_INVALID;}
 }
 unsigned stop_generation=h.generation;
 if(p->phase==DP_APPROACH&&current->execution.mission.phase==DP_APPROACH&&
    atomic_compare_exchange_strong(&e->parking_stop_generation,&stop_generation,0)){
  p->phase=DP_ALIGN_STOP;p->phase_revision++;p->boundary_ready=false;
  if(p->best_maneuver.count>=2){(void)de_path_release(e,h);return parking_publish(e,&p->best_maneuver,now,true,false);}
 }
 if(p->phase==DP_ALIGN_STOP&&current->execution.mission.phase==DP_ALIGN_STOP&&
    p->best_maneuver.count<2&&same_stop(e,current,now)){
  p->resuming_plan=true;p->phase=DP_EVALUATE;p->round_deadline_us=0;p->has_best=false;
 }
 if(p->phase==DP_ALIGN_STOP&&p->boundary_ready){p->phase=DP_ENTERING;}
 if(p->phase==DP_TARGET_LOCKED&&e->episode_status.commit_state==2&&e->episode_status.plan_revision==current->revision&&
    e->episode_status.phase_revision==current->maneuver_revision){p->phase=DP_APPROACH;p->phase_revision++;}
 stop_generation=h.generation;
 if(p->phase==DP_ENTERING&&current->execution.mission.phase==DP_ENTERING&&
    atomic_compare_exchange_strong(&e->parking_stop_generation,&stop_generation,0)){
  p->phase=DP_FINAL_VERIFY;p->phase_revision++;p->verify_since_pi_us=0;
 }
 if(p->phase==DP_FINAL_VERIFY&&!p->request_ready&&!p->budget_pending&&same_stop(e,current,now)&&v->sequence>p->last_vehicle_sequence){
  float radius=hypotf(e->config->front,e->config->half_width),pad=v->position_error_m+radius*v->heading_error_rad;
  bool fit=dp_terminal_accept(&p->space,pose,pad,p->goal,v->heading_error_rad,e->config);
  if(fit){
   if(!p->verify_since_pi_us)p->verify_since_pi_us=e->control_facts.stop.stationary_started_pi_us;
   if(e->control_facts.stop.stationary_observed_pi_us>=p->verify_since_pi_us&&
      e->control_facts.stop.stationary_observed_pi_us-p->verify_since_pi_us>=e->parking_limits.final_window_us){p->phase=DP_HOLD;p->terminal=true;p->result=DP_FEASIBLE;}
  }else{
   p->verify_since_pi_us=0;
   if(e->grant.correction_attempts_used<2){p->correction_pending=true;p->stage=0;p->phase=DP_EVALUATE;p->has_best=false;p->round_deadline_us=0;}
   else{p->phase=DP_HOLD;p->terminal=true;p->result=DP_INPUT_INVALID;}
  }
 }
 p->last_vehicle_sequence=v->sequence;
 bool republish=p->phase!=DP_EVALUATE&&p->phase!=p->published_phase;
 enum de_result rc=DE_OK;
 if(republish)rc=parking_publish(e,current,now,false,false);
 (void)de_path_release(e,h);return rc;
}
bool de_parking_boundary(struct drive_engine *e){return e&&atomic_load(&e->parking_boundary_pending);}
void de_parking_boundary_ready(struct drive_engine *e){if(e&&atomic_exchange(&e->parking_boundary_pending,false)){
 e->parking.boundary_ready=true;if(e->requested<UINT32_MAX)atomic_fetch_add(&e->requested,1);}}
static void metric_add(struct lp_metrics *to,const struct lp_metrics *from){
 to->expanded+=from->expanded;to->primitives+=from->primitives;to->footprints+=from->footprints;to->cells+=from->cells;
 to->road_checks+=from->road_checks;to->cancel_checks+=from->cancel_checks;to->clock_checks+=from->clock_checks;
 if(to->nodes_high_water<from->nodes_high_water)to->nodes_high_water=from->nodes_high_water;
 if(to->open_high_water<from->open_high_water)to->open_high_water=from->open_high_water;
}
static bool parking_work(struct drive_engine *e){
 if(e->parking.spent.expanded>=4096||e->parking.spent.cells>=1500000)return false;
 e->parking_work_config=*e->config;
 e->parking_work_config.expansion_limit=(unsigned)(4096-e->parking.spent.expanded);
 e->parking_work_config.collision_limit=(unsigned)(1500000-e->parking.spent.cells);return true;
}
static enum de_result parking_start(struct drive_engine *e,struct dw_handle mh,const struct dw_snapshot *map,dw_pose pose,const dw_vehicle *v,uint64_t now,struct lp_budget budget){
 struct dp_state *p=&e->parking;uint64_t guest=0;
 if(!budget.clock||budget.clock(budget.context,&guest)){(void)dw_release(&e->maps,mh);return DE_CLOCK;}
 if(!p->correction_pending&&!p->resuming_plan&&!p->round.initialized&&
    !dp_round_begin(&p->round,&map->world,&e->grant,pose)){
  p->round.initialized=false;
  (void)dw_release(&e->maps,mh);return DE_WAITING;
 }
 if(!p->round_deadline_us||guest>=p->round_deadline_us||p->evaluated_in_round>=3){
  if(p->has_best){
   p->goal=p->best_maneuver.goal;p->space=p->best_maneuver.space;p->goal_revision=p->goal_revision?p->goal_revision:1;
   p->selected_slot=p->best_rank.slot_id;p->selected_slot_revision=p->space.slot_revision;
   bool resume=p->resuming_plan;if(resume)p->phase=DP_ALIGN_STOP;else if(p->correction_pending)p->phase=DP_FINAL_VERIFY;
   (void)dw_release(&e->maps,mh);
   return parking_publish(e,p->correction_pending||resume?&p->best_maneuver:&p->best_approach,now,true,true);
  }
  p->round_deadline_us=guest+1000000;p->evaluated_in_round=0;memset(&p->spent,0,sizeof(p->spent));
 }
 uint32_t id=p->correction_pending||p->resuming_plan?p->selected_slot:dp_round_next(&p->round);
 if(!id){p->phase=DP_HOLD;p->result=DP_UNKNOWN_FAMILY;(void)dw_release(&e->maps,mh);return DE_STATE;}
 const xenova_drive_ParkingSlot *slot=dp_slot(&map->world,id);
 if(!dp_slot_eligible(&map->world,slot,e->grant.strategy)){
  (void)dp_round_visit(&p->round,id);p->evaluated_in_round++;(void)dw_release(&e->maps,mh);return DE_WAITING;
 }
 struct tr_intent task=e->task;task.changing=false;task.from_lane_id=task.selected_lane_id=slot->approach_lane_id;
 if(!task.active){task=(struct tr_intent){true,false,slot->approach_lane_id,slot->approach_lane_id,slot->approach_lane_id,0};}
 if(!tr_basis_accept(&e->basis,&map->world)){(void)dw_release(&e->maps,mh);return DE_REFERENCE;}
 float radius=hypotf(e->config->front,e->config->half_width),pad=v->position_error_m+radius*v->heading_error_rad;
 if(!p->correction_pending&&!p->resuming_plan&&!dp_goal_seeds(slot,e->grant.strategy,e->config,&e->parking_limits,pad,&p->goals,&p->space)){
  (void)dp_round_visit(&p->round,id);p->evaluated_in_round++;(void)dw_release(&e->maps,mh);return DE_WAITING;
 }
 if(p->correction_pending||p->resuming_plan){p->goals.count=1;p->goals.poses[0]=p->goal;}
 if(!parking_work(e)){(void)dw_release(&e->maps,mh);return DE_LIMIT;}
 struct lp_path *out;enum de_result rc=reserve(e,&e->planning_path,&out);
 if(rc){(void)dw_release(&e->maps,mh);return rc;}
 p->selected_slot=id;p->selected_slot_revision=slot->geometry_revision;
 p->candidate_deadline_us=guest+300000;
 if(p->candidate_deadline_us>p->round_deadline_us-100000)p->candidate_deadline_us=p->round_deadline_us-100000;
 if(budget.deadline_us>p->candidate_deadline_us)budget.deadline_us=p->candidate_deadline_us;
 e->budget=budget;e->planning=true;e->planning_map=mh;e->planning_snapshot=map;e->planning_grant=e->grant;e->planning_has_grant=true;
 e->paths[e->planning_path.slot].scene_generation=map->scene_generation;
 float steer=v->has_measured_steering_rad?v->measured_steering_rad:v->estimated_steering_rad;
 if(p->correction_pending||p->resuming_plan){
  p->stage=2;e->search_reason=lp_start_parking(&e->search,&map->world,pose,steer,&p->goals,p->goals.final_gear,2,&p->space,&task,&e->basis,&e->parking_work_config,e->assets,budget);
 }else if(dp_approach(&map->world,&task,slot,e->config,&p->approach_goal)){
  p->stage=1;e->search_reason=lp_start_task_goal(&e->search,&map->world,pose,steer,p->approach_goal,&e->parking_work_config,e->assets,budget,&task,&e->basis);
 }else{p->stage=1;e->search_reason=LP_INPUT;}
 return DE_OK;
}
bool de_budget_request(struct drive_engine *e,xenova_drive_MissionBudgetRequest *out){
 if(!e||!out||!e->parking.request_ready||e->parking.budget_pending||e->episode_status.commit_state!=2)return false;
 struct de_reference h;const struct lp_path *p;if(de_path_acquire(e,&h,&p))return false;
 bool accepted=e->episode_status.plan_revision==p->revision&&e->episode_status.phase_revision==p->maneuver_revision&&e->episode_status.calculation_hwm;
 if(!accepted){(void)de_path_release(e,h);return false;}
 xenova_drive_MissionBudgetRequest *r=&e->parking.request;
 if(!r->major){
  memset(r,0,sizeof(*r));r->major=1;r->request_nonce.size=r->mission_id.size=r->scene_admission_id.size=16;
  r->plan_sha256.size=r->selected_goal_sha256.size=32;memcpy(r->request_nonce.bytes,e->behavior_incarnation,16);
  uint64_t n=++e->episode_request_sequence;for(unsigned k=0;k<8;k++)r->request_nonce.bytes[15-k]^=(uint8_t)(n>>(k*8));r->request_nonce.bytes[0]^=0xb0;
  memcpy(r->mission_id.bytes,e->grant.mission_id.bytes,16);r->mission_revision=e->grant.mission_revision;
  r->control_generation=e->grant.control_generation;r->expected_grant_revision=e->grant.grant_revision;
  r->action=e->parking.correction_pending?2:1;r->slot_id=e->parking.selected_slot;r->slot_revision=e->parking.selected_slot_revision;
  r->goal_revision=e->parking.goal_revision;r->plan_revision=p->revision;
  memcpy(r->scene_admission_id.bytes,e->grant.scene_admission_id.bytes,16);r->scene_revision=e->grant.scene_revision;
 }
 *out=*r;(void)de_path_release(e,h);return true;
}
void de_budget_submitted(struct drive_engine *e,const xenova_drive_MissionBudgetRequest *r,const uint8_t hash[32]){
 if(!e||!r||!hash)return;
 e->parking.request=*r;memcpy(e->parking.request_sha256,hash,32);e->parking.budget_pending=true;e->parking.request_ready=false;
}
enum de_result de_budget_reply(struct drive_engine *e,const xenova_drive_MissionBudgetReply *reply,uint64_t now){
 if(!e||!reply||!e->parking.budget_pending)return DE_STATE;
 struct dp_state *p=&e->parking;
 if(memcmp(reply->request_nonce.bytes,p->request.request_nonce.bytes,16)||memcmp(reply->request_sha256.bytes,p->request_sha256,32))return DE_STATE;
 p->budget_pending=false;
 bool valid=e->has_grant&&now<p->request.valid_until_pi_us&&reply->result==1&&reply->has_grant&&grant_same(&e->grant,&reply->grant)&&
  e->grant.has_locked_goal_sha256&&!memcmp(e->grant.locked_goal_sha256.bytes,p->request.selected_goal_sha256.bytes,32)&&
  e->grant.grant_revision==p->request.expected_grant_revision+1&&
  e->grant.mission_deadline_pi_us==p->original_grant.mission_deadline_pi_us&&e->grant.search_deadline_pi_us==p->original_grant.search_deadline_pi_us&&
  e->grant.execution_deadline_pi_us>now&&e->grant.execution_deadline_pi_us<=e->grant.mission_deadline_pi_us&&
  (!p->locked||e->grant.execution_deadline_pi_us==p->original_grant.execution_deadline_pi_us)&&
  e->grant.correction_attempts_used==(p->request.action==2?p->original_grant.correction_attempts_used+1:0)&&e->grant.correction_attempts_used<=2;
 if(!valid){p->phase=DP_HOLD;p->terminal=true;p->result=DP_INPUT_INVALID;e->behavior_failed=true;return DE_STATE;}
 p->original_grant.execution_deadline_pi_us=e->grant.execution_deadline_pi_us;
 p->original_grant.correction_attempts_used=e->grant.correction_attempts_used;
 p->locked=true;memcpy(p->selected_goal_sha256,p->request.selected_goal_sha256.bytes,32);
 p->phase=p->correction_pending?DP_ALIGN_STOP:DP_TARGET_LOCKED;p->phase_revision++;p->boundary_ready=false;
 p->correction_pending=false;p->request.major=0;
 const struct lp_path *source=p->request.action==2?&p->best_maneuver:&p->best_approach;
 return parking_publish(e,source,now,false,false);
}
static enum de_result parking_slice(struct drive_engine *e,unsigned expansions,uint64_t now){
 struct dp_state *p=&e->parking;struct lp_path *out=&e->paths[e->planning_path.slot].path;
 if(e->search_reason==LP_SEARCHING)e->search_reason=lp_advance(&e->search,expansions,out);
 if(e->search_reason==LP_SEARCHING)return DE_WAITING;
 metric_add(&p->spent,&e->search.metrics);
 if(e->search_reason==LP_PATH_FOUND&&p->stage==1&&parking_work(e)){
  p->temporary_approach=*out;dw_pose start=out->points[out->count-1].pose;float steer=out->points[out->count-1].steering_rad;
  p->stage=2;e->search_reason=lp_start_parking(&e->search,&e->planning_snapshot->world,start,steer,&p->goals,p->goals.final_gear,2,
   &p->space,&out->task,&out->basis,&e->parking_work_config,e->assets,e->budget);
  return DE_WAITING;
 }
 if(e->search_reason==LP_PATH_FOUND){
  float clearance=0;struct lp_metrics measure={0};enum lp_result valid=lp_clearance_lower(out,&e->planning_snapshot->world,&e->parking_work_config,
   e->parking_limits.clearance_sufficient,&measure,&e->budget,&clearance);metric_add(&p->spent,&measure);
  if(valid==LP_PATH_FOUND&&grant_same(&e->grant,&e->planning_grant)){
   struct dp_rank rank=dp_path_rank(out,clearance,p->selected_slot,e->search.selected_goal,&e->parking_limits);
   if(!p->has_best||dp_rank_better(&rank,&p->best_rank)){
    p->best_rank=rank;p->best_maneuver=*out;p->best_approach=p->temporary_approach;p->has_best=true;p->goal=out->goal;
   }
  }
 }
 discard_path(e,e->planning_path);(void)dw_release(&e->maps,e->planning_map);e->planning=false;e->planning_snapshot=NULL;
 p->stage=0;p->evaluated_in_round++;if(!p->correction_pending&&!p->resuming_plan)(void)dp_round_visit(&p->round,p->selected_slot);
 if(p->has_best&&(p->correction_pending||p->resuming_plan||p->evaluated_in_round>=3||p->round.done)){
  p->space=p->best_maneuver.space;p->selected_slot=p->best_rank.slot_id;p->selected_slot_revision=p->space.slot_revision;
  p->goal_revision=p->goal_revision?p->goal_revision:1;
  bool resume=p->resuming_plan;
  if(resume)p->phase=DP_ALIGN_STOP;else if(p->correction_pending)p->phase=DP_FINAL_VERIFY;
  return parking_publish(e,p->correction_pending||resume?&p->best_maneuver:&p->best_approach,now,true,true);
 }
 return DE_OK;
}

enum de_result de_memory_read(struct drive_engine *e,unsigned region,struct dw_memory *out){
 if(!out||region>3)return DE_INPUT;
 if(region==0)return dw_memory_read(&e->maps,out)==DW_OK?DE_OK:DE_BUSY;
 if(region==3)return dw_odom_memory_read(&e->odometry,out)==DW_OK?DE_OK:DE_BUSY;
 if(region==2){
  /* Allocated nodes at the last completed slice, not transient stack/heap use.
   * The index/heap/heuristic workspace is included only in reserved bytes. */
  unsigned used=atomic_load(&e->search_used);
  unsigned peak=atomic_load(&e->search_peak);
  *out=(struct dw_memory){.reserved=sizeof(e->search),.capacity=(uint64_t)e->config->node_limit*sizeof(e->search.nodes[0]),
   .used=(uint64_t)used*sizeof(e->search.nodes[0]),
   .peak=(uint64_t)peak*sizeof(e->search.nodes[0]),
   .exhausted=atomic_load(&e->search_exhausted),.contention=0};
  return DE_OK;
 }
 if(atomic_flag_test_and_set_explicit(&e->lock,memory_order_acquire))return DE_BUSY;
 unsigned used=0;
 for(unsigned i=0;i<DE_PATH_SLOTS;i++)if((int)i==e->current||e->paths[i].writing||readers(&e->paths[i]))used++;
 size_t path_bytes=sizeof(e->paths[0].path)+sizeof(e->paths[0].tracking_geometry);
 *out=(struct dw_memory){.reserved=sizeof(e->paths),.capacity=DE_PATH_SLOTS*path_bytes,
  .used=used*path_bytes,.peak=e->path_peak*path_bytes,
  .exhausted=e->path_exhausted,.contention=atomic_load(&e->resource_contention)};
 give(e);return DE_OK;
}
