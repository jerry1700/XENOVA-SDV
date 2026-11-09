/* SPDX-License-Identifier: Apache-2.0 */
/* Included by the existing input owner. Immutable data leases and original
 * receive hashes are kept here; no worker/clock/permission is manufactured. */
#include "lattice_route_input.h"

#if defined(CONFIG_XENOVA_DRIVE_ROUTE)
struct dlr_record_slot {xenova_drive_RoadData value;uint8_t raw[DLR_RECORD_WIRE];size_t size;
 uint8_t hash[32];uint32_t kind,generation,leases[4];};
static struct dlr_record_slot route_records[DLR_RECORD_SLOTS];
static uint32_t route_record_generation,route_next_lease;static unsigned route_record_cursor;
static xenova_drive_RouteQuery route_query;static uint8_t route_query_raw[768],route_query_raw_sha[32];
static size_t route_query_raw_size;static bool route_query_available;
static xenova_drive_RouteStatus route_current_status;static bool route_status_available;
static struct {uint32_t generation;uint8_t hash[32];bool valid;} route_world_hash[DW_MAP_SLOTS];
static struct {uint64_t sequence,pi_sample,source_us;char host[37],device[37],odom[37];uint8_t hash[32];bool valid;} route_vehicle_hash[DW_ODOM_SLOTS];
static unsigned route_vehicle_hash_cursor;
static struct {struct dlr_clock_proof value;uint32_t generation;bool valid;} route_clock_pending,route_clock_confirmed;
static xenova_drive_RouteExchange route_out,route_ephemeral;
static uint8_t route_out_hash[32];static bool route_out_pending,route_out_send,route_ephemeral_send;
static xenova_drive_RouteReceipt route_answer;static bool route_answer_available;
static xenova_drive_WindowTransitionReceipt route_transition_answer;static bool route_transition_available;
static uint8_t route_answer_raw[1536],route_answer_raw_sha[32],route_transition_raw[1024],route_transition_raw_sha[32];
static size_t route_answer_raw_size,route_transition_raw_size;
static xenova_drive_RoadData route_plan_out;static bool route_plan_send;
static uint64_t route_last_send;

bool dl_route_installed(void){return drive_lattice_route_supported();}
static bool route_record_readers(const struct dlr_record_slot *slot){
 for(unsigned i=0;i<4;i++)if(slot->leases[i])return true;return false;
}
static const void *route_record_body(const xenova_drive_RoadData *d,uint32_t *kind){
 *kind=d->which_payload>=10&&d->which_payload<=17?d->which_payload-9:0;
 switch(d->which_payload){
 case xenova_drive_RoadData_start_goal_tag:return &d->payload.start_goal;
 case xenova_drive_RoadData_road_topology_tag:return &d->payload.road_topology;
 case xenova_drive_RoadData_road_section_tag:return &d->payload.road_section;
 case xenova_drive_RoadData_road_connection_tag:return &d->payload.road_connection;
 case xenova_drive_RoadData_road_window_tag:return &d->payload.road_window;
 case xenova_drive_RoadData_exit_space_tag:return &d->payload.exit_space;
 case xenova_drive_RoadData_route_plan_tag:return &d->payload.route_plan;
 case xenova_drive_RoadData_road_geometry_scope_tag:return &d->payload.road_geometry_scope;
 default:return NULL;
 }
}
static bool route_owner_local(const xenova_drive_RouteOwnerContext *owner,const struct xt_bootstrap *boot,bool generation){
 char vm[37],execution[37],instance[37];uuid_text(vm,owner->vm.bytes);uuid_text(execution,owner->execution.bytes);uuid_text(instance,owner->app_instance.bytes);
 return context_available&&!strcmp(vm,boot->identity.vm_id)&&!strcmp(execution,boot->identity.execution_id)&&
  !strcmp(instance,boot->auto_instance_id)&&(!generation||owner->control_generation==control_generation);
}
static int route_record_receive(const xenova_drive_RoadData *value,const uint8_t *raw,size_t n){
 uint32_t kind;uint8_t hash[32];const void *body=route_record_body(value,&kind);
 if(!body||!raw||n>DLR_RECORD_WIRE||dw_route_validate(value,xenova_drive_RoadData_fields)||
    !dw_route_record_hash(kind,body,hash)||memcmp(hash,value->record_sha256.bytes,32))return -EPROTO;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 if(!context_available){k_mutex_unlock(&input_context_lock);return -EXDEV;}
 for(unsigned i=0;i<DLR_RECORD_SLOTS;i++)if(route_records[i].generation&&route_records[i].kind==kind&&
     !memcmp(route_records[i].hash,hash,32)){
  /* A matching immutable record is already retained. A new transport wrapper
   * does not replace the original bytes beneath a planner's active lease. */
  k_mutex_unlock(&input_context_lock);return 0;
 }
 if(route_record_generation==UINT32_MAX){k_mutex_unlock(&input_context_lock);return -EOVERFLOW;}
 for(unsigned j=0;j<DLR_RECORD_SLOTS;j++){
  unsigned i=(route_record_cursor+j)%DLR_RECORD_SLOTS;struct dlr_record_slot *slot=&route_records[i];
  if(route_record_readers(slot))continue;
  slot->value=*value;memcpy(slot->raw,raw,n);slot->size=n;memcpy(slot->hash,hash,32);
  slot->kind=kind;slot->generation=++route_record_generation;route_record_cursor=(i+1)%DLR_RECORD_SLOTS;
  k_mutex_unlock(&input_context_lock);return 0;
 }
 k_mutex_unlock(&input_context_lock);return -ENOSPC;
}
int dl_route_record_acquire(uint32_t kind,const uint8_t hash[32],struct dlr_lease *lease,
 const xenova_drive_RoadData **value,const uint8_t **raw,size_t *n){
 if(!hash||!lease||!value||!raw||!n)return -EINVAL;
 if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 for(unsigned i=0;i<DLR_RECORD_SLOTS;i++){struct dlr_record_slot *slot=&route_records[i];
  if(slot->generation&&slot->kind==kind&&!memcmp(slot->hash,hash,32)){
   if(route_next_lease==UINT32_MAX){k_mutex_unlock(&input_context_lock);return -EOVERFLOW;}
   for(unsigned k=0;k<4;k++)if(!slot->leases[k]){
    slot->leases[k]=++route_next_lease;*lease=(struct dlr_lease){i,slot->generation,slot->leases[k]};
    *value=&slot->value;*raw=slot->raw;*n=slot->size;k_mutex_unlock(&input_context_lock);return 0;
   }
   k_mutex_unlock(&input_context_lock);return -EBUSY;
  }
 }
 k_mutex_unlock(&input_context_lock);return -ENOENT;
}
int dl_route_record_release(struct dlr_lease lease){
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=-ESTALE;
 if(lease.slot<DLR_RECORD_SLOTS&&route_records[lease.slot].generation==lease.generation&&lease.token){
  for(unsigned i=0;i<4;i++)if(route_records[lease.slot].leases[i]==lease.token){route_records[lease.slot].leases[i]=0;rc=0;break;}
 }
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_record_next(uint32_t kind,uint32_t after,struct dlr_lease *lease,
 const xenova_drive_RoadData **value,const uint8_t **raw,size_t *n){
 if(!lease||!value||!raw||!n)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 unsigned selected=DLR_RECORD_SLOTS;uint32_t next=UINT32_MAX;
 for(unsigned i=0;i<DLR_RECORD_SLOTS;i++)if(route_records[i].kind==kind&&route_records[i].generation>after&&route_records[i].generation<next){
  next=route_records[i].generation;selected=i;
 }
 int rc=-ENOENT;
 if(selected<DLR_RECORD_SLOTS){struct dlr_record_slot *slot=&route_records[selected];rc=-EBUSY;
  for(unsigned i=0;i<4;i++)if(!slot->leases[i]&&route_next_lease<UINT32_MAX){
   slot->leases[i]=++route_next_lease;*lease=(struct dlr_lease){selected,slot->generation,slot->leases[i]};
   *value=&slot->value;*raw=slot->raw;*n=slot->size;rc=0;break;
  }
 }
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_query_get(xenova_drive_RouteQuery *out,uint8_t raw_sha[32]){
 if(!out||!raw_sha)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 struct xt_bootstrap boot;if(xd_context_get(&boot))return -EAGAIN;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=route_query_available&&route_owner_local(&route_query.owner,&boot,false)?0:-ENODATA;
 if(!rc){*out=route_query;memcpy(raw_sha,route_query_raw_sha,32);}
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_status(xenova_drive_RouteStatus *out,uint64_t now){
 if(!out)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=!context_available||!route_status_available?-ENODATA:now>=route_current_status.valid_until_pi_us?-ESTALE:0;
 if(!rc)*out=route_current_status;k_mutex_unlock(&input_context_lock);return rc;
}
static void route_confirm_pending(const struct guest_clock_slot *slot){
 if(route_clock_pending.valid&&route_clock_pending.value.clock.probe_sequence==slot->confirmed.probe_sequence&&
    !memcmp(route_clock_pending.value.clock.reply_sha256,slot->confirmed.reply_sha256,32)){
  route_clock_confirmed=route_clock_pending;route_clock_confirmed.value.clock=slot->confirmed;
 }else route_clock_confirmed.valid=false;
}
static void route_observe_numeric(const struct guest_clock_slot *slot,const xenova_drive_ClockReply *reply){
 route_clock_pending.valid=slot->pending_valid&&reply->has_control_facts;
 if(route_clock_pending.valid){route_clock_pending.value.clock=slot->pending;
  route_clock_pending.value.facts=reply->control_facts;route_clock_pending.value.vehicle=reply->vehicle;
  route_clock_pending.generation=slot->sent_generation;}
 route_status_available=reply->has_route_status;
 if(route_status_available)route_current_status=reply->route_status;
}
int dl_route_clock_proof(struct dlr_clock_proof *out){
 if(!out)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 struct dl_clock current;uint64_t now;int rc=dl_clock_get(&current,&now);if(rc)return rc;
 if(!current.acknowledged)return -EAGAIN;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 rc=!route_clock_confirmed.valid||route_clock_confirmed.generation!=control_generation||
  current.probe_sequence!=route_clock_confirmed.value.clock.probe_sequence||
  memcmp(current.reply_sha256,route_clock_confirmed.value.clock.reply_sha256,32)||
  now>=route_clock_confirmed.value.facts.valid_until_pi_us?-ESTALE:0;
 if(!rc){*out=route_clock_confirmed.value;out->clock=current;out->now_pi_us=now;}
 k_mutex_unlock(&input_context_lock);return rc;
}
static void route_world_observe(const uint8_t *raw,size_t n){
 if(!engine||engine->receive_handle.slot>=DW_MAP_SLOTS)return;
 uint8_t hash[32];if(!xdr_sha256(raw,n,hash)||k_mutex_lock(&input_context_lock,K_NO_WAIT))return;
 unsigned slot=engine->receive_handle.slot;route_world_hash[slot].generation=engine->receive_handle.generation;
 memcpy(route_world_hash[slot].hash,hash,32);route_world_hash[slot].valid=true;k_mutex_unlock(&input_context_lock);
}
static void route_vehicle_observe(const uint8_t *raw,size_t n,const dw_vehicle *v){
 uint8_t hash[32];if(!xdr_sha256(raw,n,hash)||k_mutex_lock(&input_context_lock,K_NO_WAIT))return;
 unsigned i=route_vehicle_hash_cursor++%DW_ODOM_SLOTS;
 for(unsigned j=0;j<DW_ODOM_SLOTS;j++)if(route_vehicle_hash[j].valid&&route_vehicle_hash[j].sequence==v->sequence&&
    !strcmp(route_vehicle_hash[j].host,v->host_boot)&&!strcmp(route_vehicle_hash[j].device,v->device_session)&&
    !strcmp(route_vehicle_hash[j].odom,v->odom_epoch)){i=j;break;}
 bool same=route_vehicle_hash[i].valid&&route_vehicle_hash[i].sequence==v->sequence&&
  !strcmp(route_vehicle_hash[i].host,v->host_boot)&&!strcmp(route_vehicle_hash[i].device,v->device_session)&&!strcmp(route_vehicle_hash[i].odom,v->odom_epoch);
 if(same&&memcmp(route_vehicle_hash[i].hash,hash,32)){route_vehicle_hash[i].valid=false;k_mutex_unlock(&input_context_lock);return;}
 route_vehicle_hash[i].sequence=v->sequence;route_vehicle_hash[i].pi_sample=v->pi_sample_us;route_vehicle_hash[i].source_us=v->source_us;
 strcpy(route_vehicle_hash[i].host,v->host_boot);strcpy(route_vehicle_hash[i].device,v->device_session);strcpy(route_vehicle_hash[i].odom,v->odom_epoch);
 memcpy(route_vehicle_hash[i].hash,hash,32);route_vehicle_hash[i].valid=true;k_mutex_unlock(&input_context_lock);
}
int dl_route_input_hashes(const struct de_input_check *check,uint8_t world[32],uint8_t vehicle[32]){
 if(!check||!world||!vehicle)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 bool found_world=false,found_vehicle=false;
 for(unsigned i=0;i<DW_MAP_SLOTS;i++)if(route_world_hash[i].valid&&route_world_hash[i].generation==check->map_generation){memcpy(world,route_world_hash[i].hash,32);found_world=true;break;}
 const dw_vehicle *v=&check->vehicle;
 for(unsigned i=0;i<DW_ODOM_SLOTS;i++)if(route_vehicle_hash[i].valid&&route_vehicle_hash[i].sequence==v->sequence&&
    route_vehicle_hash[i].pi_sample==v->pi_sample_us&&route_vehicle_hash[i].source_us==v->source_us&&
    !strcmp(route_vehicle_hash[i].host,v->host_boot)&&!strcmp(route_vehicle_hash[i].device,v->device_session)&&!strcmp(route_vehicle_hash[i].odom,v->odom_epoch)){
  memcpy(vehicle,route_vehicle_hash[i].hash,32);found_vehicle=true;break;
 }
 k_mutex_unlock(&input_context_lock);return found_world&&found_vehicle?0:-ENODATA;
}
static int route_exchange_receive(const xenova_drive_RouteExchange *value,const uint8_t *raw,size_t n,const struct xt_bootstrap *boot){
 if(dw_route_validate(value,xenova_drive_RouteExchange_fields))return -EPROTO;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=-EPROTO;
 if(!context_available){k_mutex_unlock(&input_context_lock);return -EXDEV;}
 if(value->which_payload==xenova_drive_RouteExchange_route_query_tag){
  const xenova_drive_RouteQuery *q=&value->payload.route_query;const uint8_t *body;size_t size;uint8_t hash[32];
  if(route_owner_local(&q->owner,boot,true)&&dw_route_body(raw,n,2,&body,&size)&&size<=sizeof(route_query_raw)&&xdr_sha256(body,size,hash)){
   bool same=route_query_available&&!memcmp(q->query_id.bytes,route_query.query_id.bytes,16);
   if(same)rc=size==route_query_raw_size&&!memcmp(hash,route_query_raw_sha,32)?0:-EPROTO;
   else if(route_out_pending)rc=-EBUSY;
   else{route_query=*q;memcpy(route_query_raw,body,size);route_query_raw_size=size;
    memcpy(route_query_raw_sha,hash,32);route_query_available=true;rc=0;}
  }
 }else if(value->which_payload==xenova_drive_RouteExchange_route_receipt_tag){
  const xenova_drive_RouteReceipt *a=&value->payload.route_receipt;
  if(route_out_pending&&route_out.which_payload==xenova_drive_RouteExchange_route_candidate_tag){
   const xenova_drive_RouteCandidate *q=&route_out.payload.route_candidate;
   bool match=a->request_sequence==q->request_sequence&&!memcmp(a->request_nonce.bytes,q->request_nonce.bytes,16)&&
    !memcmp(a->request_sha256.bytes,route_out_hash,32)&&!memcmp(a->query_id.bytes,q->query_id.bytes,16)&&!memcmp(a->query_sha256.bytes,q->query_sha256.bytes,32);
   if(match&&a->result==1)match=a->has_initial_target_context&&route_owner_local(&a->initial_target_context,boot,true)&&
    mission_available&&current_mission.kind==3&&current_mission.admitted&&
    !memcmp(a->initial_target_context.mission_id.bytes,current_mission.mission_id.bytes,16)&&
    a->initial_target_context.mission_revision==current_mission.mission_revision&&
    a->initial_target_context.grant_revision==current_mission.grant_revision&&
    a->initial_target_context.cancel_fence_revision==current_mission.cancel_fence_revision&&
    current_mission.has_operation_binding&&dw_operation_binding_equal(&a->initial_target_context.operation_binding,&current_mission.operation_binding);
   const uint8_t *body;size_t size;
   if(match&&dw_route_body(raw,n,5,&body,&size)&&size<=sizeof(route_answer_raw)&&xdr_sha256(body,size,route_answer_raw_sha)){
    route_answer=*a;memcpy(route_answer_raw,body,size);route_answer_raw_size=size;
    route_answer_available=true;route_out_send=false;rc=0;
   }
  }
 }else if(value->which_payload==xenova_drive_RouteExchange_window_transition_receipt_tag){
  const xenova_drive_WindowTransitionReceipt *a=&value->payload.window_transition_receipt;
  if(route_out_pending&&route_out.which_payload==xenova_drive_RouteExchange_window_transition_request_tag&&
     !memcmp(a->request_nonce.bytes,route_out.payload.window_transition_request.request_nonce.bytes,16)&&
     !memcmp(a->request_sha256.bytes,route_out_hash,32)){
   const uint8_t *body;size_t size;
   if(dw_route_body(raw,n,7,&body,&size)&&size<=sizeof(route_transition_raw)&&xdr_sha256(body,size,route_transition_raw_sha)){
    route_transition_answer=*a;memcpy(route_transition_raw,body,size);route_transition_raw_size=size;
    route_transition_available=true;route_out_send=false;rc=0;
   }
  }
 }
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_receipt_take(xenova_drive_RouteReceipt *out,uint8_t hash[32]){
 if(!out||!hash)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=route_answer_available?0:-EAGAIN;
 if(!rc){*out=route_answer;memcpy(hash,route_answer_raw_sha,32);route_answer_available=false;route_out_pending=route_out_send=false;}
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_transition_take(xenova_drive_WindowTransitionReceipt *out,uint8_t hash[32]){
 if(!out||!hash)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=route_transition_available?0:-EAGAIN;
 if(!rc){*out=route_transition_answer;memcpy(hash,route_transition_raw_sha,32);route_transition_available=false;route_out_pending=route_out_send=false;}
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_exchange_submit(const xenova_drive_RouteExchange *value){
 if(!value)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 if(dw_route_validate(value,xenova_drive_RouteExchange_fields))return -EPROTO;
 struct xt_bootstrap boot;struct dl_clock clock;uint64_t now;
 if(xd_context_get(&boot)||dl_clock_get(&clock,&now)||!clock.acknowledged)return -EAGAIN;
 const xenova_drive_RouteOwnerContext *owner=NULL;uint64_t end=0;uint8_t hash[32];bool ephemeral=false;
 switch(value->which_payload){
 case xenova_drive_RouteExchange_route_eligibility_tag:
  owner=&value->payload.route_eligibility.owner;end=value->payload.route_eligibility.witness.valid_until_pi_us;
  ephemeral=true;if(!dw_route_eligibility_hash(&value->payload.route_eligibility,hash))return -EINVAL;break;
 case xenova_drive_RouteExchange_route_candidate_tag:
  owner=&value->payload.route_candidate.owner;end=value->payload.route_candidate.witness.valid_until_pi_us;
  if(!dw_route_candidate_hash(&value->payload.route_candidate,hash))return -EINVAL;break;
 case xenova_drive_RouteExchange_window_transition_request_tag:
  owner=&value->payload.window_transition_request.owner;end=value->payload.window_transition_request.witness.valid_until_pi_us;
  if(!dw_route_transition_request_hash(&value->payload.window_transition_request,hash))return -EINVAL;break;
 case xenova_drive_RouteExchange_road_data_request_tag:
  owner=&value->payload.road_data_request.owner;end=value->payload.road_data_request.valid_until_pi_us;
  ephemeral=true;if(!dw_route_data_request_hash(&value->payload.road_data_request,hash))return -EINVAL;break;
 default:return -EPROTO;
 }
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=0;
 if(!route_owner_local(owner,&boot,true)||end<=now)rc=-ESTALE;
 else if(ephemeral){route_ephemeral=*value;route_ephemeral_send=true;}
 else if(route_out_pending){
  if(value->which_payload!=route_out.which_payload||memcmp(hash,route_out_hash,32))rc=-EBUSY;
  else if(!route_answer_available&&!route_transition_available)route_out_send=true;
 }else{
  route_out=*value;memcpy(route_out_hash,hash,32);route_out_pending=route_out_send=true;
  route_answer_available=route_transition_available=false;
 }
 k_mutex_unlock(&input_context_lock);return rc;
}
int dl_route_plan_submit(const xenova_drive_RoadData *value){
 if(!value)return -EINVAL;if(!dl_route_installed())return -ENOTSUP;
 uint32_t kind;uint8_t hash[32];const void *body=route_record_body(value,&kind);
 if(kind!=7||value->publisher_kind!=2||!value->has_guest_owner||!value->has_guest_witness||
    dw_route_validate(value,xenova_drive_RoadData_fields)||!dw_route_record_hash(kind,body,hash)||memcmp(hash,value->record_sha256.bytes,32))return -EPROTO;
 struct xt_bootstrap boot;struct dl_clock clock;uint64_t now;
 if(xd_context_get(&boot)||dl_clock_get(&clock,&now)||!clock.acknowledged)return -EAGAIN;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return -EBUSY;
 int rc=!route_owner_local(&value->guest_owner,&boot,true)||now>=value->guest_witness.valid_until_pi_us?-ESTALE:
  route_plan_send&&memcmp(route_plan_out.record_sha256.bytes,hash,32)?-EBUSY:0;
 if(!rc){route_plan_out=*value;route_plan_send=true;}
 k_mutex_unlock(&input_context_lock);return rc;
}
static void route_context_change(bool hard,bool auto_target){
 route_clock_pending.valid=route_clock_confirmed.valid=false;route_status_available=false;route_ephemeral_send=false;
 bool preserve=!hard&&auto_target&&route_out_pending&&route_out.which_payload==xenova_drive_RouteExchange_route_candidate_tag&&
  route_out.payload.route_candidate.action==DW_ROUTE_BEGIN;
 if(!preserve){route_query_available=false;route_out_pending=route_out_send=false;route_answer_available=route_transition_available=false;route_plan_send=false;}
 /* Immutable geometry remains a candidate. Its current scope/source must be
  * checked again by the planner; it is never a retained permission. */
 if(hard){memset(route_world_hash,0,sizeof(route_world_hash));memset(route_vehicle_hash,0,sizeof(route_vehicle_hash));}
}
static void route_publish_step(void){
 if(!dl_route_installed()||!link_client.connected)return;
 struct dl_clock clock;uint64_t now;if(dl_clock_get(&clock,&now)||!clock.acknowledged)return;
 if(k_mutex_lock(&input_context_lock,K_NO_WAIT))return;
 if(!xpf_elapsed(clock_us(),route_last_send)){k_mutex_unlock(&input_context_lock);return;}
 unsigned kind=0;
 memset(&outgoing,0,sizeof(outgoing));outgoing.major=2;
 if(route_plan_send){outgoing.which_payload=xenova_drive_DriveLink_road_data_tag;outgoing.payload.road_data=route_plan_out;route_plan_send=false;kind=1;}
 else if(route_out_pending&&route_out_send&&!route_answer_available&&!route_transition_available){
  /* A retry is the original value, including its original owner and E. An
   * accepted BEGIN may be retrieving its exact receipt after new-gen ACK. */
  outgoing.which_payload=xenova_drive_DriveLink_route_exchange_tag;outgoing.payload.route_exchange=route_out;route_out_send=false;kind=2;
 }else if(route_ephemeral_send){
  uint64_t end=route_ephemeral.which_payload==xenova_drive_RouteExchange_route_eligibility_tag?
   route_ephemeral.payload.route_eligibility.witness.valid_until_pi_us:route_ephemeral.payload.road_data_request.valid_until_pi_us;
  if(now<end){outgoing.which_payload=xenova_drive_DriveLink_route_exchange_tag;outgoing.payload.route_exchange=route_ephemeral;kind=3;}
  route_ephemeral_send=false;
 }
 if(kind)route_last_send=clock_us();k_mutex_unlock(&input_context_lock);
 if(kind&&binary_publish(&link_client,out_topic,&outgoing)&&!k_mutex_lock(&input_context_lock,K_NO_WAIT)){
  if(kind==1)route_plan_send=true;else if(kind==2)route_out_send=true;else route_ephemeral_send=true;
  k_mutex_unlock(&input_context_lock);
 }
}
#endif
