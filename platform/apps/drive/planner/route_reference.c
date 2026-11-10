/* SPDX-License-Identifier: Apache-2.0 */
#include "route_reference.h"
#include <math.h>
#include <float.h>
#include <string.h>

static bool point(dw_point a,dw_point b){return a.x==b.x&&a.y==b.y;}
static bool geometry(const xenova_drive_RoadWindow *a,const xenova_drive_RoadWindow *b,unsigned first,unsigned n){
 if(!a||!b||!n||first+n>a->reference_count||n>b->reference_count||a->road.lanes_count!=2||b->road.lanes_count!=2)return false;
 if(a->heading_error_rad!=b->heading_error_rad||a->road.divider_position_bound_m!=b->road.divider_position_bound_m||
    a->road.divider_heading_bound_rad!=b->road.divider_heading_bound_rad)return false;
 for(unsigned j=0;j<2;j++){
  const xenova_drive_LaneGeometry *x=&a->road.lanes[j],*y=&b->road.lanes[j];
  if(x->lane_id!=y->lane_id||x->travel_direction!=1||y->travel_direction!=1||x->adjacent_lane_id!=y->adjacent_lane_id||
     x->position_bound_m!=y->position_bound_m||x->heading_bound_rad!=y->heading_bound_rad)return false;
  for(unsigned i=0;i<n;i++)if(!point(x->centerline[first+i],y->centerline[i])||x->point_state.bytes[first+i]!=y->point_state.bytes[i])return false;
 }
 for(unsigned i=0;i<n;i++){
  unsigned j=first+i;
  if(!point(a->reference[j],b->reference[i])||!point(a->left_boundary[j],b->left_boundary[i])||
     !point(a->right_boundary[j],b->right_boundary[i])||a->point_error_m[j]!=b->point_error_m[i]||
     !point(a->road.divider[j],b->road.divider[i])||a->road.divider_state.bytes[j]!=b->road.divider_state.bytes[i])return false;
  if(i+1<n&&a->road.crossing.bytes[j]!=b->road.crossing.bytes[i])return false;
 }
 return true;
}
static bool valid(const xenova_drive_RoadWindow *w){return w&&dw_route_validate(w,xenova_drive_RoadWindow_fields)==DW_OK;}
bool drr_append(const xenova_drive_RoadWindow *a,const xenova_drive_RoadWindow *b){
 if(!valid(a)||!valid(b)||memcmp(a->window_id.bytes,b->window_id.bytes,16)||
    memcmp(a->geometry_scope_sha256.bytes,b->geometry_scope_sha256.bytes,32)||
    memcmp(a->topology_sha256.bytes,b->topology_sha256.bytes,32)||b->reference_count<a->reference_count||
    !geometry(a,b,0,a->reference_count))return false;
 if(b->reference_count==a->reference_count){uint8_t x[32],y[32];
  return a->revision==b->revision&&dw_route_record_hash(5,a,x)&&dw_route_record_hash(5,b,y)&&!memcmp(x,y,32);}
 return b->revision>a->revision;
}
bool drr_seam(const xenova_drive_RoadWindow *a,const xenova_drive_RoadWindow *b){
 uint8_t sha[32],prefix[32];
 if(!valid(a)||!valid(b)||!memcmp(a->window_id.bytes,b->window_id.bytes,16)||
    memcmp(a->window_id.bytes,b->from_window_id.bytes,16)||
    memcmp(a->geometry_scope_sha256.bytes,b->geometry_scope_sha256.bytes,32)||
    memcmp(a->topology_sha256.bytes,b->topology_sha256.bytes,32)||
    !dw_route_record_hash(5,a,sha)||memcmp(sha,b->from_window_sha256.bytes,32)||
    b->overlap_point_count<2||b->overlap_point_count>a->reference_count||
    !geometry(a,b,a->reference_count-b->overlap_point_count,b->overlap_point_count)||
    !dw_basis_prefix_hash(b->reference,b->overlap_point_count,prefix)||memcmp(prefix,b->overlap_prefix_sha256.bytes,32))return false;
 return true;
}
enum drp_result drr_open(struct drr_view *out,const dw_world *w,const xenova_drive_RoadWindow *window,
 const xenova_drive_RoadTopology *topology,const xenova_drive_RoutePlan *plan,unsigned occurrence,const struct lp_config *c){
 uint8_t sha[32];
 if(!out||!w||!c||!valid(window)||drp_plan_validate(plan,topology)!=DRP_OK||occurrence>=plan->occurrences_count||
    !(w->required_features&DW_ROUTE_VIEW)||!w->has_route_view||!w->has_lane_model||
    w->lane_model.divider_count!=w->reference_count||w->reference_count<2||w->reference_count>32||
    w->lane_model.divider_state.size!=w->reference_count||w->lane_model.crossing.size!=w->reference_count-1||
    !dw_route_record_hash(5,window,sha)||memcmp(sha,w->route_view.window_sha256.bytes,32)||
    memcmp(window->window_id.bytes,w->route_view.window_id.bytes,16)||window->revision!=w->route_view.window_revision||
    memcmp(window->geometry_scope_sha256.bytes,w->route_view.geometry_scope_sha256.bytes,32)||
    memcmp(window->topology_sha256.bytes,w->route_view.topology_sha256.bytes,32)||
    memcmp(window->from_window_id.bytes,w->route_view.from_window_id.bytes,16)||
    memcmp(window->from_window_sha256.bytes,w->route_view.from_window_sha256.bytes,32)||
    memcmp(window->transition_id.bytes,w->route_view.transition_id.bytes,16)||
    memcmp(window->overlap_prefix_sha256.bytes,w->route_view.overlap_prefix_sha256.bytes,32)||
    memcmp(window->geometry_scope_sha256.bytes,plan->geometry_scope_sha256.bytes,32)||
    memcmp(window->topology_sha256.bytes,plan->topology_sha256.bytes,32)||w->reference_count!=window->reference_count||
    w->lane_model.divider_position_bound_m!=window->road.divider_position_bound_m||
    w->lane_model.divider_heading_bound_rad!=window->road.divider_heading_bound_rad)return DRP_CONTEXT;
 for(unsigned i=0;i<w->reference_count;i++)if(!point(w->reference[i],window->reference[i])||
    (i+1<w->reference_count&&w->lane_model.crossing.bytes[i]!=window->road.crossing.bytes[i]))return DRP_CONTEXT;
 const xenova_drive_RouteOccurrence *o=&plan->occurrences[occurrence];bool found=false;unsigned sections[2]={32,32};
 for(unsigned i=0;i<window->section_sha256_count;i++){
  for(unsigned j=0;j<topology->sections_count;j++)if(!memcmp(window->section_sha256[i].bytes,topology->sections[j].section_sha256.bytes,32))sections[i]=j;
  if(sections[i]==32)return DRP_CONTEXT;
  if(sections[i]==o->section_index)found=true;
 }
 if(memcmp(w->route_view.first_section_id.bytes,topology->sections[sections[0]].section_id.bytes,16)||
    memcmp(w->route_view.last_section_id.bytes,topology->sections[sections[window->section_sha256_count-1]].section_id.bytes,16))return DRP_CONTEXT;
 if(window->section_sha256_count==2){bool connected=false;
  for(unsigned i=0;i<topology->edges_count;i++)if(topology->edges[i].kind==1&&topology->edges[i].from_section_index==sections[0]&&topology->edges[i].to_section_index==sections[1])connected=true;
  if(!connected)return DRP_CONTEXT;
 }
 if(!found)return DRP_WINDOW;
 float original_error=0;
 for(unsigned i=0;i<window->point_error_m_count;i++)original_error=fmaxf(original_error,window->point_error_m[i]);
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 original_error+=radius*window->heading_error_rad;
 if(!isfinite(original_error))return DRP_INPUT;
 struct tr_intent task={true,false,o->lane_id,o->lane_id,o->lane_id,original_error};struct tr_view road;
 if(!tr_open(w,&task,&road))return DRP_CONTEXT;
 const xenova_drive_LaneGeometry *expected=NULL;
 for(unsigned i=0;i<window->road.lanes_count;i++)if(window->road.lanes[i].lane_id==o->lane_id)expected=&window->road.lanes[i];
 if(!expected||expected->centerline_count!=road.count||expected->position_bound_m!=road.selected->position_bound_m||
    expected->heading_bound_rad!=road.selected->heading_bound_rad)return DRP_CONTEXT;
 for(unsigned i=0;i<road.count;i++)if(!point(expected->centerline[i],road.points[i])||expected->point_state.bytes[i]!=road.selected->point_state.bytes[i]||
    !point(window->road.divider[i],w->lane_model.divider[i])||window->road.divider_state.bytes[i]!=w->lane_model.divider_state.bytes[i])return DRP_CONTEXT;
 *out=(struct drr_view){w,window,road,task,c};return DRP_OK;
}
enum drp_result drr_extent(const struct drr_view *v,const struct lp_path *p,dw_pose pose,const dw_vehicle *vehicle,
 const struct lp_config *c,struct lp_metrics *m,const struct lp_budget *budget,xenova_drive_RouteExtentProof *out){
 if(!v||!p||!vehicle||!c||!m||!out||!vehicle->sequence||!dw_pose_finite(pose)||!isfinite(vehicle->velocity_mps)||
    !isfinite(vehicle->position_error_m)||!isfinite(vehicle->heading_error_rad)||vehicle->position_error_m<0||vehicle->heading_error_rad<0||
    !isfinite(c->max_brake)||c->max_brake<=0||!tr_same_intent(&v->intent,&p->task)||
    p->task.vehicle_pad<v->intent.vehicle_pad||p->space.parking)return DRP_INPUT;
 struct lp_road_state state;
 if(lp_task_project(v->world,&v->intent,pose,&state)!=LP_PATH_FOUND)return DRP_CONTEXT;
 enum lp_result checked=lp_validate_path(p,v->world,c,m,budget);
 if(checked!=LP_PATH_FOUND)return checked==LP_DEADLINE?DRP_DEADLINE:checked==LP_CANCELLED?DRP_CANCELLED:DRP_WINDOW;
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 float error=vehicle->position_error_m+radius*vehicle->heading_error_rad;
 float braking=fabsf(vehicle->velocity_mps)*c->reaction_s+vehicle->velocity_mps*vehicle->velocity_mps/(2*c->max_brake);
 if(!isfinite(error)||!isfinite(braking))return DRP_INPUT;
 float ahead=fmaxf(0,state.length-state.arc-error),behind=fmaxf(0,state.arc-error);
 float need_front=c->front+c->margin+error+braking,need_back=c->rear+c->margin+error;
 if(!isfinite(ahead)||!isfinite(behind)||ahead<need_front||behind<need_back)return DRP_WINDOW;
 memset(out,0,sizeof(*out));out->mode=1;out->window_sha256.size=out->exit_space_sha256.size=out->cusp_sweep_sha256.size=out->s19_sweep_sha256.size=32;
 if(!dw_route_record_hash(5,v->window,out->window_sha256.bytes))return DRP_CONTEXT;
 out->available_forward_m=ahead;out->available_backward_m=behind;out->required_forward_m=need_front;out->required_backward_m=need_back;
 out->braking_distance_m=braking;out->body_radius_m=radius;out->position_bound_m=vehicle->position_error_m;
 out->heading_bound_rad=vehicle->heading_error_rad;out->vehicle_sequence=vehicle->sequence;
 return dw_route_validate(out,xenova_drive_RouteExtentProof_fields)==DW_OK?DRP_OK:DRP_INPUT;
}
enum drp_result drr_project(const struct drr_view *view,const xenova_drive_RoadSection *section,
 const xenova_drive_RoadTopology *topology,unsigned index,dw_pose pose,float pad,struct drp_location *out){
 uint8_t hash[32];
 if(!view||!view->config||!section||!topology||!out||index>=topology->sections_count||!dw_pose_finite(pose)||!isfinite(pad)||pad<0||
    !tr_contains(&view->road,pose,pad+view->intent.vehicle_pad,view->config)||
    dw_route_validate(section,xenova_drive_RoadSection_fields)!=DW_OK||!dw_route_record_hash(3,section,hash)||
    memcmp(hash,topology->sections[index].section_sha256.bytes,32)||
    memcmp(section->geometry_scope_sha256.bytes,view->window->geometry_scope_sha256.bytes,32))return DRP_CONTEXT;
 const xenova_drive_LaneGeometry *lane=NULL;
 for(unsigned i=0;i<section->road.lanes_count;i++)if(section->road.lanes[i].lane_id==view->intent.selected_lane_id)lane=&section->road.lanes[i];
 if(!lane||lane->centerline_count!=section->reference_count)return DRP_CONTEXT;
 double total=0,best=DBL_MAX,station=0,arc=0,point_pad=0,ratio=1;
 for(unsigned i=0;i<section->point_error_m_count;i++)point_pad=fmax(point_pad,section->point_error_m[i]);
 double error=pad+point_pad;
 for(unsigned i=1;i<section->reference_count;i++){
  dw_point a=lane->centerline[i-1],b=lane->centerline[i],c=section->reference[i-1],d=section->reference[i];
  double dx=(double)b.x-a.x,dy=(double)b.y-a.y,len=hypot(dx,dy),ref=hypot((double)d.x-c.x,(double)d.y-c.y);
  if(!isfinite(len)||len<=0||!isfinite(ref)||ref<=0)return DRP_INPUT;
  double u=fmax(0,fmin(1,(((double)pose.x-a.x)*dx+((double)pose.y-a.y)*dy)/(len*len)));
  double distance=hypot(a.x+u*dx-pose.x,a.y+u*dy-pose.y);
  if(distance<best){best=distance;station=arc+u*ref;}
  ratio=fmax(ratio,ref/len);arc+=ref;
 }
 total=arc;double source_length=topology->sections[index].length_m;
 /* The numerical term bounds float32 stored arc summation; geometric source
  * error stays explicit and cannot turn a missing connection into an edge. */
 if(fabs(total-source_length)>2*point_pad*section->reference_count+FLT_EPSILON*total*section->reference_count)return DRP_CONTEXT;
 error*=ratio;arc=0;
 for(unsigned i=1;i<section->reference_count;i++){
  dw_point a=lane->centerline[i-1],b=lane->centerline[i],c=section->reference[i-1],d=section->reference[i];
  double dx=(double)b.x-a.x,dy=(double)b.y-a.y,len=hypot(dx,dy),ref=hypot((double)d.x-c.x,(double)d.y-c.y);
  double u=fmax(0,fmin(1,(((double)pose.x-a.x)*dx+((double)pose.y-a.y)*dy)/(len*len)));
  if(hypot(a.x+u*dx-pose.x,a.y+u*dy-pose.y)<=best+2*error&&fabs(arc+u*ref-station)>2*error)return DRP_PROGRESS;
  arc+=ref;
 }
 float lower=(float)fmax(0,station-error),upper=(float)fmin(source_length,station+error);
 lower=fmaxf(0,nextafterf(lower,-INFINITY));upper=fminf(topology->sections[index].length_m,nextafterf(upper,INFINITY));
 *out=(struct drp_location){index,lane->lane_id,lower,upper};return DRP_OK;
}
bool drr_path_hash(const struct lp_path *path,const dw_world *world,const xenova_drive_RoadWindow *basis,
 const xenova_drive_RoutePlan *plan,dw_state *state,uint8_t out[32]){
 struct dw_route_path_ref ref={0};
 if(!path||!world||!valid(basis)||!plan||!state||!out||path->count<2||path->count>LP_PATH||
    !path->basis.bound||path->basis.count<2||path->basis.count>32||path->basis.count!=basis->reference_count||
    memcmp(path->basis.points,basis->reference,basis->reference_count*sizeof(dw_point))||
    !dw_route_record_hash(7,plan,ref.global_plan_sha256)||!dw_route_record_hash(5,basis,ref.basis_window_sha256)||
    memcmp(plan->geometry_scope_sha256.bytes,basis->geometry_scope_sha256.bytes,32))return false;
 memcpy(ref.geometry_scope_sha256,basis->geometry_scope_sha256.bytes,32);ref.task_space_kind=path->space.exiting?2:1;
 if(path->space.exiting){if(!path->space.parking||memcmp(path->space.geometry_scope_sha256,ref.geometry_scope_sha256,32))return false;
  memcpy(ref.exit_space_sha256,path->space.exit_space_sha256,32);}
 ref.basis_revision=basis->revision;ref.basis_prefix=path->basis.points;ref.basis_prefix_count=path->basis.count;
 memset(state,0,sizeof(*state));strcpy(state->map_epoch,path->basis.map);strcpy(state->frame_epoch,path->frame);
 strcpy(state->odom_epoch,path->odom);strcpy(state->configuration,path->configuration);strcpy(state->calibration,world->calibration);
 strcpy(state->primitive,world->primitive);strcpy(state->reference_id,path->reference);
 state->path_count=path->count;memcpy(state->path,path->points,path->count*sizeof(path->points[0]));
 state->segments_count=path->execution.segments_count;
 if(state->segments_count){if(state->segments_count>3)return false;memcpy(state->segments,path->execution.segments,state->segments_count*sizeof(state->segments[0]));}
 else{state->segments_count=1;state->segments[0]=(xenova_drive_GearSegment){0,(uint32_t)path->count-1,1};}
 state->merge_goal=path->merge_goal;state->direction=path->direction;state->maneuver_phase=path->phase;state->maneuver_revision=path->maneuver_revision;
 return dw_route_local_path_hash(state,&ref,out);
}
