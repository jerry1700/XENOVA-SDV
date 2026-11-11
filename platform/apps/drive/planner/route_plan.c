/* SPDX-License-Identifier: Apache-2.0 */
#include "route_plan.h"
#include <float.h>
#include <math.h>
#include <string.h>

#define SET_BYTES(field,source,n) do{(field).size=(n);memcpy((field).bytes,(source),(n));}while(0)
_Static_assert(sizeof(struct drp_workspace)<=8192,"global route workspace must stay bounded");
static bool lane_at(const xenova_drive_RoadTopology *t,uint32_t section,uint32_t lane){
 return section<t->sections_count&&(lane==t->sections[section].left_lane_id||lane==t->sections[section].right_lane_id);
}
static enum drp_result guard(const struct lp_budget *b,uint64_t *work,uint64_t cap){
 if(!b||!b->clock||!b->deadline_us||!cap)return DRP_INPUT;
 if(*work>=cap)return DRP_WORK;
 ++*work;if(b->cancelled&&b->cancelled(b->context))return DRP_CANCELLED;
 uint64_t now=0;if(b->clock(b->context,&now)||!now||now>=b->deadline_us)return DRP_DEADLINE;
 return DRP_OK;
}
static bool location(const xenova_drive_RoadTopology *t,struct drp_location p){
 return lane_at(t,p.section,p.lane)&&isfinite(p.lower)&&isfinite(p.upper)&&p.lower>=0&&p.lower<=p.upper&&
  p.upper<=t->sections[p.section].length_m;
}
static enum drp_result topology(const xenova_drive_RoadTopology *t){
 if(!t||dw_route_validate(t,xenova_drive_RoadTopology_fields)!=DW_OK)return DRP_INPUT;
 for(unsigned i=0;i<t->edges_count;i++){
  const xenova_drive_RoadEdge *e=&t->edges[i];
  if(e->kind>2||e->from_station_m>t->sections[e->from_section_index].length_m||
     e->to_station_m>t->sections[e->to_section_index].length_m)return DRP_INPUT;
  if(e->from_section_index==e->to_section_index&&e->from_lane_id==e->to_lane_id&&
     e->to_station_m<=e->from_station_m&&!e->closes_circuit)return DRP_INPUT;
 }
 return DRP_OK;
}
static struct drp_location node_location(const struct drp_request *r,unsigned n){
 if(!n)return r->start;
 const xenova_drive_RoadEdge *e=&r->topology->edges[(n-1)%r->topology->edges_count];
 return (struct drp_location){e->to_section_index,e->to_lane_id,e->to_station_m,e->to_station_m};
}
enum drp_result drp_plan_validate(const xenova_drive_RoutePlan *p,const xenova_drive_RoadTopology *t){
 uint8_t sha[32];
 if(!p||topology(t)!=DRP_OK||dw_route_validate(p,xenova_drive_RoutePlan_fields)!=DW_OK||
    !dw_route_record_hash(2,t,sha)||memcmp(sha,p->topology_sha256.bytes,32)||
    memcmp(t->geometry_scope_sha256.bytes,p->geometry_scope_sha256.bytes,32))return DRP_INPUT;
 unsigned laps=0;
 for(unsigned i=0;i<p->occurrences_count;i++){
  const xenova_drive_RouteOccurrence *o=&p->occurrences[i];
  if(!lane_at(t,o->section_index,o->lane_id)||o->exit_station_m>t->sections[o->section_index].length_m)return DRP_INPUT;
  for(unsigned j=0;j<i;j++)if(p->occurrences[j].section_index==o->section_index&&p->occurrences[j].lane_id==o->lane_id&&
      o->entry_station_m<p->occurrences[j].exit_station_m){bool observed_wrap=false;
   for(unsigned k=j;k<i;k++)if(p->occurrences[k].edge_index<t->edges_count&&t->edges[p->occurrences[k].edge_index].closes_circuit)observed_wrap=true;
   if(!observed_wrap)return DRP_INPUT;
  }
  if(i+1==p->occurrences_count){if(o->edge_index!=DRP_NO_EDGE)return DRP_INPUT;break;}
  if(o->edge_index>=t->edges_count)return DRP_INPUT;
  const xenova_drive_RoadEdge *e=&t->edges[o->edge_index];const xenova_drive_RouteOccurrence *next=&p->occurrences[i+1];
  if(e->from_section_index!=o->section_index||e->from_lane_id!=o->lane_id||e->from_station_m!=o->exit_station_m||
     e->to_section_index!=next->section_index||e->to_lane_id!=next->lane_id||e->to_station_m!=next->entry_station_m)return DRP_INPUT;
  if(e->closes_circuit&&(!t->closed_observed||++laps>p->max_laps))return DRP_INPUT;
 }
 return DRP_OK;
}
enum drp_result drp_build(struct drp_workspace *s,const struct drp_request *r,const struct lp_budget *b,
 uint64_t cap,xenova_drive_RoutePlan *out){
 if(!s||!r||!out||!r->revision||!r->witness||!dw_pose_finite(r->merge_pose)||
    topology(r->topology)!=DRP_OK||!location(r->topology,r->start)||
    (r->purpose!=DW_ROUTE_EXIT_FOLLOW&&r->purpose!=DW_ROUTE_RETURN_START))return DRP_INPUT;
 const xenova_drive_RoadTopology *t=r->topology;struct drp_location target=r->start;
 uint8_t goal_sha[32]={0},exit_sha[32]={0},witness_sha[32],topology_sha[32];
 if(!dw_route_witness_hash(r->witness,witness_sha)||!dw_route_record_hash(2,t,topology_sha))return DRP_CONTEXT;
 if(r->purpose==DW_ROUTE_RETURN_START){
  if(!r->goal||dw_route_validate(r->goal,xenova_drive_StartGoal_fields)!=DW_OK||
     memcmp(r->goal->geometry_scope_sha256.bytes,t->geometry_scope_sha256.bytes,32)||
     !dw_route_record_hash(1,r->goal,goal_sha))return DRP_CONTEXT;
  target.section=t->sections_count;target.lane=r->goal->lane_id;target.lower=target.upper=r->goal->station_m;
  for(unsigned i=0;i<t->sections_count;i++)if(!memcmp(t->sections[i].section_id.bytes,r->goal->section_id.bytes,16))target.section=i;
  if(!location(t,target))return DRP_CONTEXT;
 }
 if(r->exit_space){
  if(dw_route_validate(r->exit_space,xenova_drive_ExitSpace_fields)!=DW_OK||
     memcmp(r->exit_space->geometry_scope_sha256.bytes,t->geometry_scope_sha256.bytes,32)||
     memcmp(r->exit_space->road_section_sha256.bytes,t->sections[r->start.section].section_sha256.bytes,32)||
     r->exit_space->aisle_lane_id!=r->start.lane||!dw_route_record_hash(6,r->exit_space,exit_sha))return DRP_CONTEXT;
 }else if(r->purpose==DW_ROUTE_EXIT_FOLLOW)return DRP_INPUT;
 memset(s,0,sizeof(*s));unsigned count=1+2*t->edges_count;
 for(unsigned i=0;i<count;i++){s->nodes[i].cost=DBL_MAX;s->nodes[i].parent=-1;s->nodes[i].edge=DRP_NO_EDGE;}
 s->nodes[0].cost=0;unsigned best=DRP_STATES;double best_cost=DBL_MAX;
 for(unsigned round=0;round<count;round++){
  enum drp_result rc=guard(b,&s->work,cap);if(rc!=DRP_OK)return rc;
  unsigned at=count;double cost=DBL_MAX;
  for(unsigned i=0;i<count;i++)if(!s->nodes[i].closed&&s->nodes[i].cost<cost){cost=s->nodes[i].cost;at=i;}
  if(at==count||cost>best_cost)break;
  struct drp_node *n=&s->nodes[at];n->closed=1;struct drp_location here=node_location(r,at);
  double finish=cost+fmax(0,(double)target.lower-here.upper);
  if(here.section==target.section&&here.lane==target.lane&&(target.lower>=here.upper||(r->purpose==DW_ROUTE_EXIT_FOLLOW&&!at))&&
     finish<best_cost){best=at;best_cost=finish;}
  for(unsigned i=0;i<t->edges_count;i++){
   rc=guard(b,&s->work,cap);if(rc!=DRP_OK)return rc;
   const xenova_drive_RoadEdge *e=&t->edges[i];
   if(e->from_section_index!=here.section||e->from_lane_id!=here.lane||e->from_station_m<here.upper)continue;
   unsigned laps=n->laps+(e->closes_circuit?1:0);if(laps>1)continue;
   unsigned next=1+i+laps*t->edges_count;double candidate=cost+(double)e->from_station_m-here.upper;
   if(e->kind==2)candidate+=(double)e->to_station_m-e->from_station_m;
   if(!isfinite(candidate))return DRP_INPUT;
   if(candidate<s->nodes[next].cost){s->nodes[next].cost=candidate;s->nodes[next].parent=(int16_t)at;s->nodes[next].edge=i;s->nodes[next].laps=(uint8_t)laps;}
  }
 }
 if(best==DRP_STATES)return DRP_UNREACHABLE;
 unsigned length=0,at=best;
 for(;;){if(length>=96)return DRP_WORK;s->chain[length++]=(uint16_t)at;if(!at)break;
  if(s->nodes[at].parent<0)return DRP_INPUT;
  at=(unsigned)s->nodes[at].parent;}
 memset(out,0,sizeof(*out));out->major=1;out->revision=r->revision;out->purpose=r->purpose;out->max_laps=1;
 SET_BYTES(out->route_id,r->route_id,16);SET_BYTES(out->goal_sha256,goal_sha,32);SET_BYTES(out->exit_space_sha256,exit_sha,32);
 SET_BYTES(out->topology_sha256,topology_sha,32);SET_BYTES(out->geometry_scope_sha256,t->geometry_scope_sha256.bytes,32);
 SET_BYTES(out->origin_guest_witness_sha256,witness_sha,32);SET_BYTES(out->terminal_profile_sha256,r->terminal_profile_sha256,32);
 if(r->exit_space){SET_BYTES(out->merge_section_id,t->sections[r->start.section].section_id.bytes,16);out->merge_lane_id=r->start.lane;
  out->merge_station_lower_m=r->start.lower;out->merge_station_upper_m=r->start.upper;out->merge_seed_pose=r->merge_pose;}
 else out->merge_section_id.size=16;
 out->occurrences_count=length;
 for(unsigned i=0;i<length;i++){
  unsigned node=s->chain[length-1-i];struct drp_location p=node_location(r,node);xenova_drive_RouteOccurrence *o=&out->occurrences[i];
  o->section_index=p.section;o->lane_id=p.lane;o->entry_station_m=p.lower;
  o->edge_index=i+1<length?s->nodes[s->chain[length-2-i]].edge:DRP_NO_EDGE;
  o->exit_station_m=i+1<length?t->edges[o->edge_index].from_station_m:target.upper;
 }
 return drp_plan_validate(out,t);
}
const char *drp_reason(enum drp_result r){
 static const char *const names[]={"OK","INPUT_INVALID","DIRECTED_UNREACHABLE","WORK_BUDGET","DEADLINE","CANCELLED",
  "WINDOW_INSUFFICIENT","CONTEXT_CHANGED","PROGRESS_INVALID","ORIGINAL_EXPIRED","ACTUAL_STOP_REQUIRED"};
 return (unsigned)r<sizeof(names)/sizeof(*names)?names[r]:"INTERNAL_ERROR";
}
