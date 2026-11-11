/* 공통 C codec/hash의 시험 전용 입구. 실제 clock/권한/actuator를 만들지 않는다. */
#include "route.h"
#include "pb_decode.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static uint8_t wire[DW_WIRE_MAX+1];
static union {
 xenova_drive_StartGoal goal;xenova_drive_RoadTopology topology;xenova_drive_RoadSection section;
 xenova_drive_RoadConnection connection;xenova_drive_RoadWindow window;xenova_drive_ExitSpace exit_space;
 xenova_drive_RoutePlan plan;xenova_drive_RoadGeometryScope scope;xenova_drive_RouteQuery query;
 xenova_drive_RouteCandidate candidate;xenova_drive_RouteCore core;xenova_drive_RouteOwnerContext owner;
 xenova_drive_RouteCurrentWitness witness;xenova_drive_RouteReceipt receipt;xenova_drive_RouteEligibility eligibility;
 xenova_drive_RouteSnapshot snapshot;xenova_drive_RouteStatus status;xenova_drive_WorldRouteView view;
 xenova_drive_RoadData data;xenova_drive_RouteExchange exchange;xenova_drive_WindowTransitionRequest transition_request;
 xenova_drive_WindowTransitionReceipt transition_receipt;xenova_drive_RoadDataRequest data_request;
 xenova_drive_RoadRegistrationRequest registration_request;xenova_drive_MapperWorldWitness world_witness;
 dw_state state;xenova_drive_DriveLink link;xenova_drive_MissionGrant permission;
 xenova_drive_DriveControlFacts facts;xenova_drive_EpisodeSourceContext source;
} workspace;
int main(int argc,char **argv){
 if(argc!=3)return 2;
 FILE *file=fopen(argv[2],"rb");if(!file)return 2;size_t n=fread(wire,1,sizeof(wire),file);fclose(file);
 if(n>DW_WIRE_MAX)return 2;
 const pb_msgdesc_t *desc=NULL;void *value=NULL;uint8_t hash[32]={0};bool hashed=false;unsigned record=0;
 size_t generated=0,layout=0;enum dw_result result=DW_DECODE;
#define PICK(name,Type,member) if(!strcmp(argv[1],name)){desc=xenova_drive_##Type##_fields;value=&workspace.member;generated=xenova_drive_##Type##_size;layout=sizeof(workspace.member);}
 PICK("record-1",StartGoal,goal) PICK("record-2",RoadTopology,topology) PICK("record-3",RoadSection,section)
 PICK("record-4",RoadConnection,connection) PICK("record-5",RoadWindow,window) PICK("record-6",ExitSpace,exit_space)
 PICK("record-7",RoutePlan,plan) PICK("record-8",RoadGeometryScope,scope)
 PICK("query",RouteQuery,query) PICK("candidate",RouteCandidate,candidate) PICK("core",RouteCore,core)
 PICK("owner",RouteOwnerContext,owner) PICK("witness",RouteCurrentWitness,witness) PICK("receipt",RouteReceipt,receipt)
 PICK("eligibility",RouteEligibility,eligibility) PICK("snapshot",RouteSnapshot,snapshot) PICK("status",RouteStatus,status)
 PICK("view",WorldRouteView,view) PICK("data",RoadData,data) PICK("exchange",RouteExchange,exchange)
 PICK("transition_request",WindowTransitionRequest,transition_request) PICK("transition_receipt",WindowTransitionReceipt,transition_receipt)
 PICK("data_request",RoadDataRequest,data_request) PICK("registration_request",RoadRegistrationRequest,registration_request)
 PICK("world_witness",MapperWorldWitness,world_witness)
 PICK("permission",MissionGrant,permission) PICK("facts",DriveControlFacts,facts) PICK("source",EpisodeSourceContext,source)
#undef PICK
 if(desc){
  if(!strcmp(argv[1],"permission")||!strcmp(argv[1],"facts")||!strcmp(argv[1],"source")){
   pb_istream_t stream=pb_istream_from_buffer(wire,n);
   if(pb_decode(&stream,desc,value)){
    if(!strcmp(argv[1],"permission"))hashed=dw_route_permission_hash(value,hash);
    if(!strcmp(argv[1],"facts"))hashed=dw_route_stop_facts_hash(value,hash);
    if(!strcmp(argv[1],"source"))hashed=dw_route_source_key_hash(value,hash);
    result=hashed?DW_OK:DW_SOURCE;
   }
  }else result=dw_route_decode(wire,n,desc,value);
  if(result==DW_OK){
   if(!strncmp(argv[1],"record-",7)){record=(unsigned)strtoul(argv[1]+7,NULL,10);hashed=dw_route_record_hash(record,value,hash);}
#define HASH(name,member) if(!strcmp(argv[1],#name))hashed=dw_route_##name##_hash(&workspace.member,hash);
   HASH(query,query) HASH(candidate,candidate) HASH(core,core) HASH(owner,owner) HASH(witness,witness)
   HASH(receipt,receipt) HASH(eligibility,eligibility) HASH(transition_request,transition_request)
   HASH(transition_receipt,transition_receipt) HASH(data_request,data_request) HASH(registration_request,registration_request)
   HASH(world_witness,world_witness)
#undef HASH
  }
 }else if(!strcmp(argv[1],"local")||!strcmp(argv[1],"state")){
  result=dw_state_decode(wire,n,&workspace.state);layout=sizeof(workspace.state);generated=xenova_drive_DriveState_size;
  if(result==DW_OK&&!strcmp(argv[1],"local")){
   const dw_state *s=&workspace.state;struct dw_route_path_ref ref={0};
   memcpy(ref.global_plan_sha256,s->route.core.global_plan_sha256.bytes,32);
   memcpy(ref.geometry_scope_sha256,s->route.core.geometry_scope_sha256.bytes,32);
   memcpy(ref.basis_window_sha256,s->route.basis_window_sha256.bytes,32);memcpy(ref.exit_space_sha256,s->route.exit_space_sha256.bytes,32);
   ref.task_space_kind=s->route.task_space_kind;ref.basis_revision=s->route.basis_window_revision;
   ref.basis_prefix=s->task_reference.basis_prefix;ref.basis_prefix_count=s->task_reference.basis_prefix_count;
   hashed=dw_route_local_path_hash(s,&ref,hash);
  }
 }else if(!strcmp(argv[1],"link")){result=dw_link_decode(wire,n,&workspace.link);layout=sizeof(workspace.link);generated=xenova_drive_DriveLink_size;}
 else return 2;
 printf("{\"result\":%d,\"bytes\":%zu,\"generated_max\":%zu,\"sizeof\":%zu,\"hashed\":%s,\"sha256\":\"",result,n,generated,layout,hashed?"true":"false");
 if(hashed){for(unsigned i=0;i<32;i++)printf("%02x",hash[i]);}
 puts("\"}");return 0;
}
