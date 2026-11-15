/* SPDX-License-Identifier: Apache-2.0 */
#include "tof_recovery.h"
#include <math.h>
#include <string.h>

static bool stopped_vehicle(const dw_vehicle *v,const struct lp_config *c,bool stopped,float *steer){
 if(!stopped||!v||v->quality!=3||!isfinite(v->velocity_mps)||fabsf(v->velocity_mps)>.01f||
    !isfinite(v->position_error_m)||v->position_error_m<0||v->position_error_m>c->max_position_error||
    !isfinite(v->heading_error_rad)||v->heading_error_rad<0||v->heading_error_rad>c->max_heading_error||
    !v->has_steering_error_rad||!isfinite(v->steering_error_rad)||v->steering_error_rad<0||
    v->steering_error_rad>c->max_steering_error)return false;
 *steer=v->has_measured_steering_rad?v->measured_steering_rad:
        v->has_estimated_steering_rad?v->estimated_steering_rad:NAN;
 return isfinite(*steer)&&fabsf(*steer)+v->steering_error_rad<=c->max_steer+.0001f;
}

static float position_pad(const dw_vehicle *v,const struct lp_config *c){
 return v->position_error_m+hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin)*v->heading_error_rad;
}

static bool local_world(const dw_world *w){
 return w&&w->complete&&!w->truncated&&(w->required_features&DW_LANE_MODEL)&&
        !w->has_route_view&&w->lane_model.lanes_count==2&&w->width&&w->height&&w->width<=64&&w->height<=64&&
        isfinite(w->resolution_m)&&w->resolution_m>0&&w->occupancy.size==w->width*w->height;
}

static bool hit_is_mapped(const dw_world *w,dw_point hit){
 dw_pose cell=dw_compose(dw_inverse(w->origin),(dw_pose){hit.x,hit.y,0});
 float x=cell.x/w->resolution_m,y=cell.y/w->resolution_m;
 if(!isfinite(x)||!isfinite(y)||x<0||y<0||x>=w->width||y>=w->height)return false;
 size_t index=(size_t)y*w->width+(size_t)x;
 return index<w->occupancy.size&&index<w->cell_flags.size&&w->occupancy.bytes[index]>=70&&
        w->occupancy.bytes[index]<=100&&(w->cell_flags.bytes[index]&1)&&!(w->cell_flags.bytes[index]&4);
}

static void path_scope(struct lp_path *p,const dw_world *w){
 p->world_features=w->required_features;
 strcpy(p->frame,w->frame_epoch);strcpy(p->odom,w->odom_epoch);
 strcpy(p->configuration,w->configuration);strcpy(p->reference,w->reference_id);
}

static uint16_t observed_free_subcells(const dw_world *w,size_t cell){
 unsigned low=0,high=w->has_boundary_cell_free4_le?w->boundary_cell_free4_le.size/4:0;
 while(low<high){
  unsigned middle=(low+high)/2;
  const uint8_t *record=w->boundary_cell_free4_le.bytes+4*middle;
  unsigned index=dw_grid_le16(record);
  if(index==cell)return dw_grid_le16(record+2);
  if(index<cell)low=middle+1;else high=middle;
 }
 return 0;
}

static bool sensor_hits(float dx,float dy,float cs,float sn,float radius,float slope,float pad){
 float along=dx*cs+dy*sn,side=-dx*sn+dy*cs;
 return along>=-pad&&dx*dx+dy*dy<=radius*radius&&fabsf(side)<=fmaxf(0,along)*slope+pad;
}

/* An ordinary collision-free path can still hit the proximity stop cone. */
static bool proximity_clear(const struct lt_recovery *r,const dw_world *w,const struct lp_config *c){
 const struct lp_path *path=&r->forward;
 float resolution=w->resolution_m,half_cell=.5f*resolution;
 dw_pose inverse=dw_inverse(w->origin);
 for(size_t i=0;i<path->count;i+=4){
  dw_pose sensor=dw_compose(inverse,dw_compose(path->points[i].pose,(dw_pose){c->front,0,0}));
  float path_pad=c->tracking_guard+path->task.vehicle_pad;
  float pad=.7072f*resolution+path_pad;
  float radius=r->stop_m+pad;
  int left=(int)floorf((sensor.x-radius)/resolution),right=(int)floorf((sensor.x+radius)/resolution);
  int top=(int)floorf((sensor.y-radius)/resolution),bottom=(int)floorf((sensor.y+radius)/resolution);
  float cs=cosf(sensor.yaw),sn=sinf(sensor.yaw),slope=tanf(r->sensor_half_angle_rad);
  for(int y=top;y<=bottom;y++)for(int x=left;x<=right;x++){
   if(x<0||y<0||x>=(int)w->width||y>=(int)w->height)continue;
   size_t cell=(size_t)y*w->width+x;
   if(w->occupancy.bytes[cell]<70||w->occupancy.bytes[cell]>100)continue;
   float dx=x*resolution+half_cell-sensor.x,dy=y*resolution+half_cell-sensor.y;
   if(!sensor_hits(dx,dy,cs,sn,radius,slope,pad))continue;
   uint16_t free=observed_free_subcells(w,cell);
   if(!free)return false;
   float subpad=.1768f*resolution+path_pad,subradius=r->stop_m+subpad;
   for(unsigned bit=0;bit<16;bit++)if(!(free&(1u<<bit))&&
     sensor_hits(dx+((bit%4)-1.5f)*resolution/4,dy+((bit/4)-1.5f)*resolution/4,
      cs,sn,subradius,slope,subpad))return false;
  }
 }
 return true;
}

static enum lp_result forward_end(struct lt_recovery *r,const dw_world *w,const struct lp_config *c,struct lp_budget b){
 struct lp_path *path=&r->forward;
 struct tr_intent task=path->task;
 path->task.changing=false;path->task.from_lane_id=task.selected_lane_id;
 struct tr_view lane;
 enum lp_result rc=LP_NOT_FOUND;
 if(proximity_clear(r,w,c)&&tr_open_indexed(w,&path->task,b.collision_index,&lane)&&
    tr_contains_live(&lane,path->goal,task.vehicle_pad+c->tracking_guard,c))
  rc=lp_continue_lane(path,w,c,&r->metrics,&b,false);
 path->task=task;
 return rc;
}

static enum lp_result forward_plan(struct lt_recovery *r,struct lp_search *search,const dw_world *w,
 dw_pose pose,float steer,uint32_t from,uint32_t to,float pad,const struct lp_config *c,struct lp_budget b){
 struct tr_intent task={.active=true,.changing=true,.from_lane_id=from,.selected_lane_id=to,.home_lane_id=from,.vehicle_pad=pad};
 struct tr_basis basis={0};
 if(!tr_basis_accept(&basis,w))return LP_INPUT;
 dw_pose goal;
 /* Leave room in this same window to check continuation after the merge. */
 float horizon=fminf(c->plan_horizon,4*c->wheelbase);
 enum lp_result rc=lp_task_goal(w,&task,pose,horizon,lp_terminal_clearance(c),&goal);
 if(rc!=LP_PATH_FOUND)return rc==LP_INPUT?LP_NOT_FOUND:rc;
 struct lp_lane_guide_options guide={.curve_blocker=true,.compact_merge=true,.inset_scale=1};
 /* Bounded recovery-only candidates tuned with the R45 simulation. */
 const float responses[]={1,1.23f,1.45f};
 for(unsigned k=0;k<sizeof(responses)/sizeof(responses[0]);k++){
  r->forward_config=*c;r->forward_config.lookahead_min=c->lookahead_min*responses[k];
  r->forward_config.narrow_lane_speed_mps=r->forward_speed_mps;
  r->forward_config.goal_distance=fminf(c->goal_distance,1.1f*w->resolution_m);
  rc=lp_lane_guide(search,&r->forward,w,pose,steer,0,goal,&r->forward_config,b,&task,&basis,&r->metrics,&r->forward_geometry,guide);
  if(rc==LP_PATH_FOUND)rc=forward_end(r,w,c,b);
  if(rc!=LP_NOT_FOUND&&rc!=LP_COLLISION)return rc;
 }
 return rc;
}

enum lp_result lt_plan(struct lt_recovery *r,struct lp_search *search,const dw_world *w,dw_pose pose,
 const dw_vehicle *v,dw_point hit,bool stopped,bool rear_clear,float rear_range_m,const struct lp_config *c,
 const struct lt_limits *limit,struct lp_budget b){
 if(!r||!search||!c||!limit)return LP_INPUT;
 r->reverse.count=r->forward.count=0;r->retreat_m=0;r->forward_ready=false;r->metrics=(struct lp_metrics){0};
 if(!stopped||!rear_clear)return LP_STOP;
 float steer;
 if(!local_world(w)||!dw_pose_finite(pose)||!stopped_vehicle(v,c,stopped,&steer)||
    !isfinite(hit.x)||!isfinite(hit.y)||!isfinite(limit->max_retreat_m)||limit->max_retreat_m<=0||
    !isfinite(limit->front_release_m)||limit->front_release_m<=limit->stop_m||!isfinite(limit->stop_m)||limit->stop_m<=0||
    !isfinite(limit->sensor_half_angle_rad)||limit->sensor_half_angle_rad<=0||limit->sensor_half_angle_rad>=1||
    !isfinite(limit->forward_speed_mps)||limit->forward_speed_mps<c->min_control_speed||limit->forward_speed_mps>c->max_velocity||
    !isfinite(limit->reverse_speed_mps)||
    limit->reverse_speed_mps<c->min_control_speed||limit->reverse_speed_mps>c->max_reverse_velocity)return LP_INPUT;
 if(isnan(rear_range_m)||rear_range_m<=limit->stop_m||!hit_is_mapped(w,hit))return LP_STOP;
 r->stop_m=limit->stop_m;r->sensor_half_angle_rad=limit->sensor_half_angle_rad;
 r->forward_speed_mps=limit->forward_speed_mps;
 if(!b.collision_index){lp_index_build(&search->collision_index,w,c);b.collision_index=&search->collision_index;}
 float pad=position_pad(v,c);
 uint32_t lane=tr_live_lane(w,pose,pad+c->tracking_guard,c,b.collision_index);
 if(!lane)return LP_COLLISION;
 unsigned target=w->lane_model.lanes[0].lane_id==lane?1:0;
 uint32_t adjacent=w->lane_model.lanes[target].lane_id;
 struct lp_path *back=&r->reverse;
 memset(back,0,sizeof(*back));path_scope(back,w);
 back->task=(struct tr_intent){.active=true,.selected_lane_id=lane,.from_lane_id=lane,.home_lane_id=lane,.vehicle_pad=pad};
 if(!tr_basis_accept(&back->basis,w))return LP_INPUT;
 float max_step=c->primitive_speed*c->primitive_duration/(LP_PRIMITIVE_SAMPLES-1);
 float candidate_step=w->resolution_m/2;
 if(!isfinite(max_step)||max_step<=0||!isfinite(candidate_step)||candidate_step<=0||
    candidate_step/max_step>=LP_PATH||limit->max_retreat_m>max_step*(LP_PATH-1))return LP_INPUT;
 const unsigned samples=(unsigned)ceilf(candidate_step/max_step);
 /* Constant-steering reverse arc. Sample no farther apart than a lattice edge. */
 float ds=-candidate_step/samples,half_yaw=.5f*ds*tanf(steer)/c->wheelbase;
 float chord=ds*(fabsf(half_yaw)<1e-4f?1.f:sinf(half_yaw)/half_yaw);
 dw_pose step={chord*cosf(half_yaw),chord*sinf(half_yaw),2*half_yaw};
 r->reverse_config=*c;
 r->reverse_config.max_reverse_velocity=limit->reverse_speed_mps;
 /* The tracker reserves the first reaction interval even from zero speed.
  * Include that reserve in the capture radius so restart cannot stall short. */
 float stationary_reserve=.5f*c->max_accel*c->reaction_s*c->reaction_s;
 r->reverse_config.goal_distance=fminf(c->goal_distance,stationary_reserve+2*pad+c->tracking_guard);
 r->reverse_config.lookahead_min=fminf(c->lookahead_min,c->wheelbase*.5f);
 float capture=r->reverse_config.goal_distance;
 unsigned capture_samples=(unsigned)ceilf(capture/-ds);
 float rear_motion_scale=1+c->rear*fabsf(tanf(steer)/c->wheelbase);
 float reverse_limit=fminf(limit->max_retreat_m,(rear_range_m-limit->stop_m-pad)/rear_motion_scale);
 back->points[0]=(xenova_drive_PathPoint){.pose=pose,.steering_rad=steer};
 for(unsigned k=1; k*candidate_step<=reverse_limit; k++){
  unsigned last=k*samples;
  if(last>=LP_PATH)break;
  float retreat=k*candidate_step;
  for(unsigned j=(k-1)*samples+1;j<=last;j++){
   dw_pose next=dw_compose(back->points[j-1].pose,step);
   back->points[j]=(xenova_drive_PathPoint){.pose=next,.steering_rad=steer,.velocity_mps=-limit->reverse_speed_mps};
  }
  dw_pose end=back->points[last].pose;
  back->count=last+1;back->goal=back->merge_goal=end;
  back->execution.segments_count=1;
  back->execution.segments[0]=(xenova_drive_GearSegment){0,last,-1};
  enum lp_result rc=lp_validate_path_prepared(back,w,&r->reverse_config,&r->metrics,&b,&r->reverse_geometry);
  if(rc!=LP_PATH_FOUND){back->count=0;return rc;}
  /* Check the near edge of the stop region, where the tracker can first
   * legitimately finish. The actual stopped pose is checked again on resume. */
  if(last<=capture_samples)continue;
  dw_pose early=back->points[last-capture_samples].pose;
  dw_pose front=dw_compose(early,(dw_pose){c->front,0,0});
  if(hypotf(hit.x-front.x,hit.y-front.y)<limit->front_release_m+pad)continue;
  rc=forward_plan(r,search,w,early,steer,lane,adjacent,pad,c,b);
  if(rc==LP_PATH_FOUND){
   back->points[last].velocity_mps=0;r->retreat_m=retreat;r->front_hit=hit;return LP_PATH_FOUND;
  }
  r->forward.count=0;
  if(rc!=LP_NOT_FOUND&&rc!=LP_COLLISION){back->count=0;return rc;}
 }
 back->count=0;
 return LP_NOT_FOUND;
}

enum lp_result lt_resume(struct lt_recovery *r,struct lp_search *search,const dw_world *w,dw_pose pose,
 const dw_vehicle *v,bool stopped,bool front_clear,const struct lp_config *c,struct lp_budget b){
 if(!r||!search||!c)return LP_INPUT;
 uint32_t from=r->reverse.task.from_lane_id,to=r->forward.task.selected_lane_id;
 r->forward_ready=false;r->metrics=(struct lp_metrics){0};
 if(!stopped||!front_clear)return LP_STOP;
 float steer;
 if(!local_world(w)||!dw_pose_finite(pose)||!r->retreat_m||!r->reverse.count||!from||!to||
    !hit_is_mapped(w,r->front_hit)||!tr_basis_compatible(&r->reverse.basis,w)||
    !stopped_vehicle(v,c,stopped,&steer))return LP_INPUT;
 const dw_pose end=r->reverse.goal;
 if(hypotf(pose.x-end.x,pose.y-end.y)+v->position_error_m>r->reverse_config.goal_distance||
    fabsf(dw_angle(pose.yaw-end.yaw))+v->heading_error_rad>r->reverse_config.goal_heading)return LP_STOP;
 if(!b.collision_index){lp_index_build(&search->collision_index,w,c);b.collision_index=&search->collision_index;}
 /* Reuse a checked continuation only when its original start still matches
  * the live pose. Fresh occupancy validates the entire original path again. */
 const xenova_drive_PathPoint *start=&r->forward.points[0];
 const float start_tolerance_m=.015f;
 if(r->forward.count&&hypotf(pose.x-start->pose.x,pose.y-start->pose.y)+v->position_error_m<start_tolerance_m&&
    fabsf(dw_angle(pose.yaw-start->pose.yaw))+v->heading_error_rad<c->avoid_steering_rad){
  enum lp_result valid=lp_recheck_path(&r->forward,&r->forward_geometry,w,c,&r->metrics,&b);
  if(valid==LP_PATH_FOUND)valid=forward_end(r,w,c,b);
  if(valid==LP_PATH_FOUND){r->forward_ready=true;return valid;}
  if(valid!=LP_COLLISION&&valid!=LP_NOT_FOUND&&valid!=LP_INPUT){r->forward.count=0;return valid;}
 }
 r->forward.count=0;
 enum lp_result rc=forward_plan(r,search,w,pose,steer,from,to,position_pad(v,c),c,b);
 if(rc!=LP_PATH_FOUND)r->forward.count=0;
 r->forward_ready=rc==LP_PATH_FOUND;
 return rc;
}

enum lp_result lt_track(const struct lt_recovery *r,struct lp_tracker *tracker,const dw_world *w,dw_pose pose,
 const dw_vehicle *v,bool forward,bool direction_clear,float dt,struct lp_command *out,struct lp_metrics *metrics,
 const struct lp_budget *budget){
 if(!out)return LP_INPUT;
 *out=(struct lp_command){.result=LP_INPUT};
 if(!r||!tracker||!v||!metrics||!budget)return LP_INPUT;
 if(!direction_clear){
  *out=(struct lp_command){.emergency=true,.result=LP_STOP};
  return LP_STOP;
 }
 if(!local_world(w)||!r->retreat_m||forward!=r->forward_ready||(forward&&!hit_is_mapped(w,r->front_hit)))return LP_INPUT;
 const struct lp_path *path=forward?&r->forward:&r->reverse;
 if(path->count<2)return LP_INPUT;
 return lp_track_prepared(tracker,path,forward?&r->forward_geometry:&r->reverse_geometry,w,pose,v,
  forward?&r->forward_config:&r->reverse_config,dt,out,metrics,budget,NULL);
}
