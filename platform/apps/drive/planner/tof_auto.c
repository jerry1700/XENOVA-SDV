/* SPDX-License-Identifier: Apache-2.0 */
#include "tof_auto.h"
#include <math.h>
#include <string.h>

bool lt_auto_same(const struct lt_auto *a,const xenova_drive_MissionGrant *g){
 return g->admitted&&g->kind==1&&g->control_generation==a->generation&&g->grant_revision==a->grant_revision&&
        g->mission_revision==a->mission_revision&&!memcmp(a->mission,g->mission_id.bytes,sizeof(a->mission));
}

void lt_auto_begin(struct lt_auto *a,const xenova_drive_MissionGrant *g,const struct lp_path *origin,uint64_t now,uint64_t expires){
 a->stage=LT_AUTO_BRAKE;a->generation=g->control_generation;a->grant_revision=g->grant_revision;
 a->mission_revision=g->mission_revision;memcpy(a->mission,g->mission_id.bytes,sizeof(a->mission));
 a->plan_revision=origin->revision;a->phase_revision=origin->maneuver_revision;
 a->began_pi_us=now;a->expires_pi_us=expires;a->stop_sequence=a->gate_revision=0;a->hit_valid=false;
 a->attempted_stop_sequence=0;a->attempted_map_generation=0;
 a->device_session[0]=a->pi_session[0]=a->odom_epoch[0]=0;
 a->plan.retreat_m=0;a->tracker=(struct lp_tracker){0};
}

void lt_auto_observe(struct lt_auto *a,const xenova_drive_DriveControlFacts *facts,const dw_vehicle *sample){
 const xenova_drive_ProximityFacts *p=&facts->proximity;
 if(a->stage==LT_AUTO_BRAKE&&!a->hit_valid&&facts->has_proximity&&p->front_blocked&&p->has_front_range_mm&&
    sample&&sample->has_pi_session&&p->front_vehicle_sequence==sample->sequence&&sample->quality==3&&dw_pose_finite(sample->odom_pose)){
  a->hit_odom=sample->odom_pose;a->front_range_m=.001f*p->front_range_mm;a->hit_valid=true;
  strcpy(a->device_session,sample->device_session);strcpy(a->pi_session,sample->pi_session);strcpy(a->odom_epoch,sample->odom_epoch);
 }
}

static bool motion_scope(const struct lt_auto *a,const xenova_drive_DirectionGateWitness *gate,uint64_t now,
 unsigned segment,int direction){
 return gate&&now<a->expires_pi_us&&now<gate->valid_until_pi_us&&gate->producer_role==1&&
  gate->calculation_sequence&&gate->gate.stop_command_sequence&&gate->gate.gate_revision&&
  gate->control_generation==a->generation&&gate->plan_revision==a->plan_revision&&gate->phase_revision==a->phase_revision&&
  gate->segment_index==segment&&gate->cusp_index==segment&&gate->gate.requested_direction==direction&&
  gate->mission_revision==a->mission_revision&&gate->grant_revision==a->grant_revision&&
  !memcmp(gate->mission_id.bytes,a->mission,sizeof(a->mission));
}

static bool vehicle_scope(const struct lt_auto *a,const dw_vehicle *v){
 return a->hit_valid&&v->has_pi_session&&!strcmp(a->device_session,v->device_session)&&
  !strcmp(a->pi_session,v->pi_session)&&!strcmp(a->odom_epoch,v->odom_epoch);
}

bool lt_auto_stop(const struct lt_auto *a,const xenova_drive_DriveControlFacts *f,
 const xenova_drive_DirectionGateWitness *gate,uint64_t now,unsigned segment,int direction){
 if(!f||now>=f->valid_until_pi_us||!f->has_stop||!motion_scope(a,gate,now,segment,direction))return false;
 const xenova_drive_StopFacts *s=&f->stop;
 return gate->gate.state>=2&&s->state==3&&now<s->valid_until_pi_us&&s->producer_role==1&&s->applied_pi_lower_us>=a->began_pi_us&&
  s->command_sequence==gate->gate.stop_command_sequence&&s->calculation_sequence==gate->calculation_sequence&&
  !strcmp(s->device_session,gate->device_session)&&!strcmp(s->pi_session,gate->pi_session)&&
  s->control_generation==a->generation&&s->plan_revision==a->plan_revision&&s->phase_revision==a->phase_revision&&
  s->mission_revision==a->mission_revision&&s->grant_revision==a->grant_revision&&
  !memcmp(s->mission_id.bytes,a->mission,sizeof(a->mission));
}

bool lt_auto_ready(const struct lt_auto *a,const xenova_drive_DirectionGateWitness *gate,uint64_t now,
 unsigned segment,int direction){
 return motion_scope(a,gate,now,segment,direction)&&gate->gate.state==4&&a->stop_sequence&&
  gate->gate.stop_command_sequence==a->stop_sequence&&gate->gate.gate_revision==a->gate_revision;
}

/* Caller supplies the actual stop pose/odom pair in the newest validated World. */
enum lp_result lt_auto_plan(struct lt_auto *a,struct lp_search *search,const dw_world *world,dw_pose pose,const dw_vehicle *vehicle,
 const xenova_drive_DriveControlFacts *facts,const xenova_drive_DirectionGateWitness *gate,uint64_t now,
 const struct lp_config *config,const struct lt_limits *limits,struct lp_budget budget){
 bool reverse=a->stage==LT_AUTO_BRAKE;
 if(!reverse&&a->stage!=LT_AUTO_ALIGN)return LP_STOP;
 if(!lt_auto_stop(a,facts,gate,now,reverse?LT_STOP_REVERSE:LT_DRIVE_REVERSE,reverse?-1:1)||!facts->has_proximity)
  return LP_SEARCHING;
 const xenova_drive_ProximityFacts *p=&facts->proximity;
 enum lp_result result;
 if(reverse){
  if(p->rear_blocked){a->stage=LT_AUTO_HELD;return LP_STOP;}
  if(!vehicle_scope(a,vehicle)||!p->has_rear_range_mm||now>=p->rear_valid_until_pi_us)return LP_SEARCHING;
  dw_pose birth=dw_compose(pose,dw_compose(dw_inverse(vehicle->odom_pose),a->hit_odom));
  dw_pose hit=dw_compose(birth,(dw_pose){config->front+a->front_range_m,0,0});
  float rear=p->rear_range_mm==65535?INFINITY:.001f*p->rear_range_mm;
  result=lt_plan(&a->plan,search,world,pose,vehicle,(dw_point){hit.x,hit.y},true,true,rear,config,limits,budget);
 }else{
  if(!vehicle_scope(a,vehicle)||p->front_blocked)return LP_SEARCHING;
  result=lt_resume(&a->plan,search,world,pose,vehicle,true,true,config,budget);
 }
 if(result==LP_PATH_FOUND){
  a->stop_sequence=gate->gate.stop_command_sequence;a->gate_revision=gate->gate.gate_revision;
  a->tracker=(struct lp_tracker){0};
 }
 return result;
}

enum lp_result lt_auto_track(struct lt_auto *a,const dw_world *w,dw_pose pose,const dw_vehicle *vehicle,
 const xenova_drive_DriveControlFacts *facts,uint64_t now,struct lp_command *out,const struct lp_budget *budget){
 *out=(struct lp_command){.result=LP_STOP};
 bool forward=a->stage==LT_AUTO_FORWARD;
 if(!forward&&a->stage!=LT_AUTO_REVERSE)return LP_STOP;
 if(now>=a->expires_pi_us||!vehicle_scope(a,vehicle)||!facts||!facts->has_proximity||now>=facts->valid_until_pi_us){
  a->stage=LT_AUTO_HELD;return LP_STOP;
 }
 const xenova_drive_ProximityFacts *p=&facts->proximity;
 bool clear=forward?!p->front_blocked:!p->rear_blocked&&p->has_rear_range_mm&&now<p->rear_valid_until_pi_us;
 struct lp_metrics metrics={0};
 enum lp_result rc=lt_track(&a->plan,&a->tracker,w,pose,vehicle,forward,clear,.02f,out,&metrics,budget);
 if(!clear)a->stage=LT_AUTO_HELD;
 else if(rc==LP_STOP){a->stage=forward?LT_AUTO_HANDOFF:LT_AUTO_ALIGN;a->stop_sequence=a->gate_revision=0;}
 return rc;
}
