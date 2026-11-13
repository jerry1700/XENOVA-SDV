/* SPDX-License-Identifier: Apache-2.0 */
#include "route_exit.h"
#include "parking.h"
#include <math.h>
#include <string.h>

static bool hash_member(const uint8_t hash[32],const xenova_drive_ExitSpace *s){
 for(unsigned i=0;i<s->connection_sha256_count;i++)if(!memcmp(hash,s->connection_sha256[i].bytes,32))return true;
 return false;
}
enum drp_result dre_space(const struct dre_records *r,const struct lp_config *c,struct lp_space *out){
 uint8_t section[32],exit_hash[32],hash[32];
 if(!r||!r->world||!r->space||!r->section||!out||!c||!r->parking_attempt_id||!r->parking_goal_sha256||
    !r->world->has_route_view||!(r->world->required_features&DW_ROUTE_VIEW)||
    !r->world->has_lane_model||r->world->parking_slots_count>8||r->connection_count<1||r->connection_count>2||
    dw_route_validate(r->space,xenova_drive_ExitSpace_fields)!=DW_OK||
    dw_route_validate(r->section,xenova_drive_RoadSection_fields)!=DW_OK||
    !dw_route_record_hash(3,r->section,section)||!dw_route_record_hash(6,r->space,exit_hash)||
    memcmp(section,r->space->road_section_sha256.bytes,32)||
    memcmp(r->space->geometry_scope_sha256.bytes,r->world->route_view.geometry_scope_sha256.bytes,32)||
    memcmp(r->space->geometry_scope_sha256.bytes,r->section->geometry_scope_sha256.bytes,32)||
    memcmp(r->parking_attempt_id,r->space->parking_attempt_id.bytes,16)||
    memcmp(r->parking_goal_sha256,r->space->parking_goal_sha256.bytes,32))return DRP_CONTEXT;
 const xenova_drive_ParkingSlot *slot=dp_slot(r->world,r->space->slot_id);
 if(!slot||slot->geometry_revision!=r->space->slot_revision||slot->geometry_state!=1||!slot->geometry_complete||
    slot->corners_count!=4||slot->approach_lane_id!=r->space->aisle_lane_id)return DRP_CONTEXT;
 for(unsigned i=0;i<4;i++)if(slot->corners[i].x!=r->space->selected_slot_polygon[i].x||
    slot->corners[i].y!=r->space->selected_slot_polygon[i].y)return DRP_CONTEXT;
 const xenova_drive_RoadConnection *connector=NULL;
 if(r->connection_count!=r->space->connection_sha256_count)return DRP_CONTEXT;
 for(unsigned i=0;i<r->connection_count;i++){
  const xenova_drive_RoadConnection *x=r->connections[i];
  if(!x||dw_route_validate(x,xenova_drive_RoadConnection_fields)!=DW_OK||!dw_route_record_hash(4,x,hash)||
     !hash_member(hash,r->space)||memcmp(x->geometry_scope_sha256.bytes,r->space->geometry_scope_sha256.bytes,32))return DRP_CONTEXT;
  if(i&&r->connections[0]&&!memcmp(x->connection_id.bytes,r->connections[0]->connection_id.bytes,16))return DRP_CONTEXT;
  if(x->kind==3){
   if(connector||memcmp(x->from_section_sha256.bytes,section,32)||memcmp(x->to_section_sha256.bytes,section,32)||
      x->from_lane_id!=r->space->aisle_lane_id||x->to_lane_id!=x->from_lane_id||
      x->from_point_index!=x->to_point_index||x->from_point_index>=r->section->reference_count)return DRP_CONTEXT;
   bool observation=false,goal=false;
   for(unsigned j=0;j<x->evidence_roots_sha256_count;j++){
    observation|=!memcmp(x->evidence_roots_sha256[j].bytes,r->space->origin.frame_evidence_sha256.bytes,32);
    goal|=!memcmp(x->evidence_roots_sha256[j].bytes,r->space->parking_goal_sha256.bytes,32);
   }
   if(!observation||!goal)return DRP_CONTEXT;
   connector=x;
  }else if(x->kind!=1||
    (memcmp(x->from_section_sha256.bytes,section,32)&&memcmp(x->to_section_sha256.bytes,section,32)))return DRP_CONTEXT;
 }
 if(!connector)return DRP_CONTEXT;
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 float uncertainty=fmaxf(r->space->position_error_m,slot->position_bound_m)+
  radius*fmaxf(r->space->heading_error_rad,slot->heading_bound_rad);
 for(unsigned i=0;i<connector->overlap_error_m_count;i++)uncertainty=fmaxf(uncertainty,connector->overlap_error_m[i]+radius*connector->heading_error_rad);
 if(!isfinite(uncertainty)||uncertainty<0)return DRP_INPUT;
 memset(out,0,sizeof(*out));out->parking=true;out->exiting=true;out->slot_id=slot->slot_id;out->slot_revision=slot->geometry_revision;
 out->uncertainty=uncertainty;out->connector_count=connector->connector_polygon_count;
 memcpy(out->polygon,r->space->selected_slot_polygon,sizeof(out->polygon));
 memcpy(out->connector,connector->connector_polygon,out->connector_count*sizeof(dw_point));
 memcpy(out->exit_space_sha256,exit_hash,32);memcpy(out->geometry_scope_sha256,r->space->geometry_scope_sha256.bytes,32);
 return DRP_OK;
}
enum drp_result dre_prepare(const struct dre_input *in,const struct lp_config *c,struct lp_space *out){
 uint8_t source[32];
 if(!in||!c||!in->query||!in->witness||!in->records.world||!in->records.space||!in->vehicle||
    !in->world_raw_sha256||!in->vehicle_raw_sha256||!dw_pose_finite(in->current_map_pose)||in->vehicle->quality!=3||
    dw_route_validate(in->query,xenova_drive_RouteQuery_fields)!=DW_OK||
    dw_route_validate(in->witness,xenova_drive_RouteCurrentWitness_fields)!=DW_OK||
    in->now_pi_us<in->query->issued_pi_us||in->now_pi_us>=in->query->valid_until_pi_us||
    in->now_pi_us>=in->witness->valid_until_pi_us||!in->records.world->has_scene||
    in->now_pi_us<in->vehicle->pi_sample_us||in->now_pi_us-in->vehicle->pi_sample_us+in->vehicle->clock_error_us>c->pose_age_us||
    in->vehicle->sequence!=in->witness->vehicle_sequence||
    memcmp(in->world_raw_sha256,in->witness->world_raw_sha256.bytes,32)||
    memcmp(in->vehicle_raw_sha256,in->witness->vehicle_raw_sha256.bytes,32)||
    !dw_preflight_source_matches(&in->witness->source,in->records.world)||
    !dw_route_source_key_hash(&in->witness->source,source)||memcmp(source,in->query->owner.source_key_sha256.bytes,32)||
    memcmp(in->query->owner.geometry_scope_sha256.bytes,in->records.space->geometry_scope_sha256.bytes,32)||
    memcmp(in->query->owner.scene_admission_id.bytes,in->records.world->scene.admission_id.bytes,16)||
    in->query->owner.scene_revision!=in->records.world->scene.admission_revision)return DRP_CONTEXT;
 return dre_space(&in->records,c,out);
}
bool dre_inactive(const xenova_drive_EpisodeSnapshot *copy,const xenova_drive_EpisodeStatus *status,uint64_t now){
 uint8_t hash[32];
 return copy&&status&&dw_episode_snapshot_validate(copy)==DW_OK&&dw_episode_status_validate(status)==DW_OK&&
  copy->core.state==1&&status->state==1&&now<status->valid_until_pi_us&&
  !memcmp(copy->acceptance.registry_epoch.bytes,status->registry_epoch.bytes,16)&&
  copy->acceptance.registry_revision==status->registry_revision&&
  !memcmp(copy->core.episode_id.bytes,status->episode_id.bytes,16)&&copy->core.revision==status->episode_revision&&
  dw_episode_core_hash(&copy->core,hash)&&!memcmp(hash,status->accepted_core_sha256.bytes,32)&&
  !memcmp(hash,copy->acceptance.accepted_core_sha256.bytes,32);
}

struct region {const struct tr_view *road;const dw_point *points;unsigned n;double area;};
static dw_point vertex(const struct region *r,unsigned i){
 if(r->points)return r->points[i];
 unsigned n=r->road->count;
 if(i<n){dw_point c=r->road->selected->centerline[i],d=r->road->world->lane_model.divider[i];return (dw_point){2*c.x-d.x,2*c.y-d.y};}
 return r->road->world->lane_model.divider[2*n-1-i];
}
static double cross(dw_point a,dw_point b,dw_point c){return ((double)b.x-a.x)*((double)c.y-a.y)-((double)b.y-a.y)*((double)c.x-a.x);}
/* Boundary is exact: a projection gap is never snapped/epsilon-filled. */
static int inside(const struct region *r,double x,double y,unsigned *edge){
 bool result=false;
 for(unsigned i=0,j=r->n-1;i<r->n;j=i++){
  dw_point a=vertex(r,j),b=vertex(r,i);
  if(((double)b.x-a.x)*(y-a.y)-((double)b.y-a.y)*(x-a.x)==0&&
     x>=fmin(a.x,b.x)&&x<=fmax(a.x,b.x)&&y>=fmin(a.y,b.y)&&y<=fmax(a.y,b.y)){
   if(edge)*edge=j;
   return 2;
  }
  if((a.y>y)!=(b.y>y)&&x<((double)b.x-a.x)*(y-a.y)/((double)b.y-a.y)+a.x)result=!result;
 }
 return result?1:0;
}
struct precise_point {double x,y;};
struct body_box {double x,y,cs,sn,half,width;};
static bool clips(struct precise_point a,struct precise_point b,const struct body_box *box){
 double cs=box->cs,sn=box->sn,dx=a.x-box->x,dy=a.y-box->y;
 double origin[2]={dx*cs+dy*sn,-dx*sn+dy*cs};dx=b.x-a.x;dy=b.y-a.y;
 double delta[2]={dx*cs+dy*sn,-dx*sn+dy*cs},bounds[2]={box->half,box->width},lo=0,hi=1;
 for(unsigned i=0;i<2;i++){
  if(delta[i]==0){if(fabs(origin[i])>=bounds[i])return false;continue;}
  double l=(-bounds[i]-origin[i])/delta[i],h=(bounds[i]-origin[i])/delta[i];
  if(l>h){double t=l;l=h;h=t;}lo=fmax(lo,l);hi=fmin(hi,h);if(lo>=hi)return false;
 }
 return true;
}
static bool exterior_cut(struct region regions[3],unsigned owner,dw_point a,dw_point b,const struct body_box *body){
 if(!clips((struct precise_point){a.x,a.y},(struct precise_point){b.x,b.y},body))return false;
 double cuts[80]={0,1};unsigned count=2;double ax=(double)b.x-a.x,ay=(double)b.y-a.y;
 if(ax==0&&ay==0)return true;
 for(unsigned r=0;r<3;r++)if(r!=owner)for(unsigned i=0;i<regions[r].n;i++){
  dw_point c=vertex(&regions[r],i),d=vertex(&regions[r],(i+1)%regions[r].n);
  double bx=(double)d.x-c.x,by=(double)d.y-c.y,den=ax*by-ay*bx;
  if(den!=0){double u=(((double)c.x-a.x)*by-((double)c.y-a.y)*bx)/den,z=(((double)c.x-a.x)*ay-((double)c.y-a.y)*ax)/den;
   if(u>0&&u<1&&z>=0&&z<=1){if(count==80)return true;cuts[count++]=u;}}
  else if(cross(a,b,c)==0){
   double ends[2]={fabs(ax)>=fabs(ay)?((double)c.x-a.x)/ax:((double)c.y-a.y)/ay,
    fabs(ax)>=fabs(ay)?((double)d.x-a.x)/ax:((double)d.y-a.y)/ay};
   for(unsigned k=0;k<2;k++)if(ends[k]>0&&ends[k]<1){if(count==80)return true;cuts[count++]=ends[k];}
  }
 }
 for(unsigned i=1;i<count;i++){double value=cuts[i];unsigned j=i;while(j&&cuts[j-1]>value){cuts[j]=cuts[j-1];j--;}cuts[j]=value;}
 for(unsigned i=1;i<count;i++)if(cuts[i]>cuts[i-1]){
  double mid=.5*(cuts[i]+cuts[i-1]);struct precise_point p={a.x+ax*mid,a.y+ay*mid};
  bool internal=false;
  for(unsigned r=0;r<3&&!internal;r++)if(r!=owner){unsigned edge=0;int hit=inside(&regions[r],p.x,p.y,&edge);
   if(hit==1)internal=true;
   else if(hit==2){dw_point c=vertex(&regions[r],edge),d=vertex(&regions[r],(edge+1)%regions[r].n);
    double dot=(ax*((double)d.x-c.x)+ay*((double)d.y-c.y))*regions[owner].area*regions[r].area;
    if(cross(a,b,c)==0&&cross(a,b,d)==0&&dot<0)internal=true;}
  }
  if(!internal){struct precise_point l={a.x+ax*cuts[i-1],a.y+ay*cuts[i-1]},h={a.x+ax*cuts[i],a.y+ay*cuts[i]};
   if(clips(l,h,body))return true;}
 }
 return false;
}
bool dre_contains(const struct tr_view *road,const struct lp_space *space,dw_pose pose,float extra,const struct lp_config *c){
 if(!road||!road->world||!road->selected||!space||!space->parking||!space->exiting||!c||
    !isfinite(space->uncertainty)||space->uncertainty<0||road->selected!=road->from||road->count<2||road->count>32||
    space->connector_count<3||space->connector_count>8||!dw_pose_finite(pose)||!isfinite(extra)||extra<0)return false;
 struct region regions[3]={{road,NULL,2*road->count,0},{NULL,space->polygon,4,0},{NULL,space->connector,space->connector_count,0}};
 for(unsigned r=0;r<3;r++){
  for(unsigned i=0;i<regions[r].n;i++){dw_point a=vertex(&regions[r],i),b=vertex(&regions[r],(i+1)%regions[r].n);
   if(!isfinite(a.x)||!isfinite(a.y))return false;
   regions[r].area+=(double)a.x*b.y-(double)a.y*b.x;}
  if(!regions[r].area||!isfinite(regions[r].area))return false;
 }
 const xenova_drive_LaneModel *m=&road->world->lane_model;
 float radius=hypotf(fmaxf(c->front,c->rear)+c->margin,c->half_width+c->margin);
 float error=fmaxf(space->uncertainty,2*road->selected->position_bound_m+m->divider_position_bound_m+
  radius*(2*road->selected->heading_bound_rad+m->divider_heading_bound_rad));
 if(!isfinite(error)||error<0)return false;
 struct body_box body={.x=pose.x,.y=pose.y,.cs=cos(pose.yaw),.sn=sin(pose.yaw),
  .half=.5*(c->front+c->rear)+c->margin+extra+error,.width=c->half_width+c->margin+extra+error};
 body.x+=.5*(c->front-c->rear)*body.cs;body.y+=.5*(c->front-c->rear)*body.sn;
 for(unsigned i=0;i<4;i++){double x=i&1?body.half:-body.half,y=i&2?body.width:-body.width;
  struct precise_point p={body.x+body.cs*x-body.sn*y,body.y+body.sn*x+body.cs*y};
  if(!inside(&regions[0],p.x,p.y,NULL)&&!inside(&regions[1],p.x,p.y,NULL)&&!inside(&regions[2],p.x,p.y,NULL))return false;
 }
 for(unsigned r=0;r<3;r++)for(unsigned i=0;i<regions[r].n;i++)if(exterior_cut(regions,r,vertex(&regions[r],i),vertex(&regions[r],(i+1)%regions[r].n),&body))return false;
 return true;
}
