/* Native integration fixture: actual planner/app/input/lifecycle/serializers.
 * Kernel scheduling, authenticated transport, bootstrap and plant are explicit
 * leaves. It never assigns confirmed clock fields or manufactures READY. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "pb_decode.h"
#include "lattice_app.c"
#include "lifecycle.h"
#include "input_bridge.h"

static struct xt_bootstrap bridge_boot;
static struct xd_inputs bridge_inputs;
static struct drive_controller bridge_controller;
static struct drive_completion bridge_completion;
static struct drive_task_status bridge_status={.state=DRIVE_TASK_WAITING,.lifetime=1,.started=true};
static uint64_t clock_origin,clock_base=1000000,context_revision=1,check_sequence;
static uint8_t bridge_wire[DW_WIRE_MAX];
static char bridge_line[DW_WIRE_MAX*2+256];
/* Native sink for the existing product diagnostics. One process-lifetime cap;
 * no new getter, success state or permission is manufactured by observation. */
void b08_diagnostic_log(const char *format,...){
 static unsigned count;if(count>=64)return;count++;
 fputs("B08_DIAG ",stderr);va_list arguments;va_start(arguments,format);vfprintf(stderr,format,arguments);va_end(arguments);fputc('\n',stderr);
}
static uint64_t wall_us(void){struct timespec t;assert(!clock_gettime(CLOCK_MONOTONIC,&t));return (uint64_t)t.tv_sec*1000000+(uint64_t)t.tv_nsec/1000;}
int xdiag_clock_us(uint64_t *out){*out=clock_base+wall_us()-clock_origin;return 0;}
int64_t k_uptime_get(void){uint64_t now;xdiag_clock_us(&now);return (int64_t)(now/1000);}
int xd_context_get(struct xt_bootstrap *out){*out=bridge_boot;return 0;}
bool xd_stopping(void){return false;}
bool drive_task_stopping(enum drive_task task){(void)task;return false;}
int k_mutex_lock(struct k_mutex *m,int wait){(void)m;(void)wait;return 0;}
void k_mutex_unlock(struct k_mutex *m){(void)m;}
void k_sleep(int64_t delay){(void)delay;}
void k_yield(void){}
void sys_rand_get(void *out,size_t size){
 static uint64_t sequence;uint8_t *p=out;sequence++;
 for(size_t k=0;k<size;k++)p[k]=(uint8_t)(bridge_boot.identity.vm_id[k%36]^(unsigned)(sequence+13*k));
}
struct k_thread *k_thread_create(struct k_thread *t,void *s,size_t n,void (*f)(void *,void *,void *),void *a,void *b,void *c,int p,int flags,int d){
 (void)t;(void)s;(void)n;(void)f;(void)a;(void)b;(void)c;(void)p;(void)flags;(void)d;return NULL;
}
int k_thread_join(struct k_thread *t,int64_t deadline){(void)t;(void)deadline;return 0;}
void k_thread_name_set(struct k_thread *t,const char *name){(void)t;(void)name;}
int xd_io_binary(const struct xm_zbinary *binary){(void)binary;return 0;}
int xm_zclient_init(struct xm_zclient *c,const char *vm,const char *events,const char *app,const struct xm_zconfig *config){
 (void)vm;(void)events;(void)app;(void)config;memset(c,0,sizeof(*c));c->socket_open=c->connected=true;return 0;
}
void xm_zclient_close(struct xm_zclient *c){c->socket_open=c->connected=false;}
int xm_zclient_subscribe(struct xm_zclient *c,const char *topic,int qos){(void)c;(void)topic;(void)qos;return 0;}
int xm_zclient_poll(struct xm_zclient *c,int timeout){(void)c;(void)timeout;return 0;}
static void print_hex(const void *data,size_t count){
 static const char digits[]="0123456789abcdef";const uint8_t *p=data;char chunk[512];size_t used=0;
 for(size_t k=0;k<count;k++){
  chunk[used++]=digits[p[k]>>4];chunk[used++]=digits[p[k]&15];
  if(used==sizeof(chunk)){if(fwrite(chunk,1,used,stdout)!=used)abort();used=0;}
 }
 if(used&&fwrite(chunk,1,used,stdout)!=used)abort();
}
int mqtt_publish(struct mqtt_client *c,const struct mqtt_publish_param *p){
 (void)c;printf("WIRE %.*s ",(int)p->message.topic.topic.size,p->message.topic.topic.utf8);
 print_hex(p->message.payload.data,p->message.payload.len);putchar('\n');return 0;
}
static int unhex(const char *text,uint8_t *out,size_t cap){
 size_t n=strcspn(text,"\r\n");if(n%2||n/2>cap)return -EINVAL;
 /* The fixture's text envelope is not part of the product clock exchange.
  * sscanf on every suffix can scan the remaining string repeatedly; keep this
  * declared transport leaf linear without changing original clock bounds/E. */
 for(size_t k=0;k<n/2;k++){
  unsigned value=0;
  for(unsigned j=0;j<2;j++){
   unsigned char c=(unsigned char)text[2*k+j];unsigned digit;
   if(c>='0'&&c<='9')digit=c-'0';
   else if(c>='a'&&c<='f')digit=c-'a'+10;
   else if(c>='A'&&c<='F')digit=c-'A'+10;
   else return -EINVAL;
   value=value*16+digit;
  }
  out[k]=(uint8_t)value;
 }
 return (int)(n/2);
}
/* Isolated typed predicate checks: no clock, READY, State or command admission.
 * This command is unavailable once the actual owner has received live inputs. */
static int publication_policy(const char *argument){
 static struct lp_path path;static dw_state ignored;
 if(engine.current>=0||engine.behavior_enabled)return -EACCES;
 int n=argument?unhex(argument,bridge_wire,sizeof(bridge_wire)):-EINVAL;
 if(n<0)return n;
 pb_istream_t wire=pb_istream_from_buffer(bridge_wire,(size_t)n);
 if(!pb_decode(&wire,xenova_drive_EpisodeSnapshot_fields,&path.episode)||dw_episode_snapshot_validate(&path.episode))return -EINVAL;
 path.has_episode=true;engine.episode_state=path.episode;
 const xenova_drive_EpisodeAcceptanceRef *ref=&path.episode.acceptance;
 engine.episode_status.commit_state=1;engine.episode_status.registry_epoch.size=ref->registry_epoch.size;
 memcpy(engine.episode_status.registry_epoch.bytes,ref->registry_epoch.bytes,16);
 engine.episode_status.registry_revision=ref->registry_revision;
 engine.episode_status.accepted_core_sha256.size=ref->accepted_core_sha256.size;
 memcpy(engine.episode_status.accepted_core_sha256.bytes,ref->accepted_core_sha256.bytes,32);
 engine.episode_status.state_message_id.size=16;
 memset(engine.episode_status.state_message_id.bytes,0,16); /* pending registry, no future State ACK */
 strcpy(engine.phase_state_message_id,"31313131-3131-4131-8131-313131313131");
 path.revision=9;path.maneuver_revision=1;
 phase_registration.valid=true;strcpy(phase_registration.message_id,engine.phase_state_message_id);
 phase_registration.snapshot=path.episode;phase_registration.plan_revision=path.revision;
 phase_registration.phase_revision=path.maneuver_revision;phase_registration.original_path_generation=2;
 memset(phase_registration.plan_sha256,0x5a,32);
 bool baseline=phase_snapshot_registered(&path);
 engine.planning=engine.apply_pending=engine.episode_transition=engine.episode_request_ready=engine.episode_receipt_pending=true;
 bool unrelated=phase_snapshot_registered(&path);
 engine.episode_status.accepted_core_sha256.bytes[0]^=1;
 bool wrong_core=phase_snapshot_registered(&path);
 engine.episode_status.accepted_core_sha256.bytes[0]^=1;
 engine.episode_state.acceptance.request_sha256.bytes[0]^=1;
 bool wrong_ref=phase_snapshot_registered(&path);
 engine.episode_state.acceptance.request_sha256.bytes[0]^=1;
 engine.phase_state_message_id[0]^=1;
 bool wrong_id=phase_snapshot_registered(&path);
 engine.phase_state_message_id[0]^=1;
 phase_registration.snapshot.acceptance.origin_grant_revision++;
 bool wrong_receipt_ref=phase_snapshot_registered(&path);phase_registration.snapshot.acceptance.origin_grant_revision--;
 phase_registration.plan_revision++;
 bool wrong_path=phase_snapshot_registered(&path);phase_registration.plan_revision--;
 uint8_t changed_plan[32];memcpy(changed_plan,phase_registration.plan_sha256,32);changed_plan[0]^=1;
 bool wrong_plan=phase_plan_registered(engine.phase_state_message_id,changed_plan);
 engine.episode_status.state_message_id.bytes[0]=1;
 bool future_ack=phase_snapshot_registered(&path);engine.episode_status.state_message_id.bytes[0]=0;
 engine.behavior_failed=true;bool failed=phase_snapshot_registered(&path);engine.behavior_failed=false;
 /* A cache belongs to its original fixed ID, even after the next receipt has
  * installed another one. Stop the fixture before any real export can occur. */
 memset(&phase_snapshot,0,sizeof(phase_snapshot));strcpy(phase_snapshot.message_id,"33333333-3333-4333-8333-333333333333");
 phase_snapshot_valid=true;engine.stopping=true;
 char accepted_id[37];strcpy(accepted_id,engine.phase_state_message_id);
 int cache_rc=drive_lattice_snapshot(&ignored);
 printf("PUBLICATION_POLICY {\"baseline\":%d,\"unrelated_candidate\":%d,\"wrong_core\":%d,\"wrong_ref\":%d,\"wrong_id\":%d,\"wrong_receipt_ref\":%d,\"wrong_path\":%d,\"wrong_plan\":%d,\"future_ack\":%d,\"failed\":%d,\"cache_rc\":%d,\"old_cache_retained\":%d,\"new_id_preserved\":%d}\n",
  baseline,unrelated,wrong_core,wrong_ref,wrong_id,wrong_receipt_ref,wrong_path,wrong_plan,future_ack,failed,cache_rc,phase_snapshot_valid,!strcmp(accepted_id,engine.phase_state_message_id));
 engine.planning=engine.apply_pending=engine.episode_transition=engine.episode_request_ready=engine.episode_receipt_pending=false;
 return 0;
}
static void observe_step(void){
 static unsigned count;if((!engine.episode.active&&engine.episode_state.core.state!=2)||count>=8)return;count++;
 const struct lp_path *p=engine.current>=0?&engine.paths[engine.current].path:NULL;
 fprintf(stderr,"B09_STEP n=%u cap=8 planning=%d finalized=%d valid=%d pending=%d transition=%d failed=%d current=%d currentgen=%u planninggen=%u planning_anchor_pi=%llu path_episode=%u phase=%u facts_current=%d factsE=%llu completion=%llu completiongen=%u exported=%llu fixed_id=%s cache_valid=%d cache_id=%s\n",
  count,engine.planning,engine.plan_finalized,engine.plan_valid,engine.episode_receipt_pending,engine.episode_transition,engine.behavior_failed,
  engine.current,engine.current>=0?engine.paths[engine.current].generation:0,engine.planning_path.generation,(unsigned long long)engine.planning_vehicle_pi_us,
  p&&p->has_episode?p->episode.core.state:0,engine.episode.phase,(int)atomic_load(&engine.facts_current),
  (unsigned long long)engine.facts.expires_pi_us,(unsigned long long)completion.sequence,completion.path_generation,
  (unsigned long long)exported_completion,engine.phase_state_message_id,phase_snapshot_valid,phase_snapshot_valid?phase_snapshot.message_id:"");
}
static void lifecycle(void){
 struct drive_result selected;struct xm_span control={bridge_inputs.control.payload,(uint16_t)bridge_inputs.control.length};
 drive_controller_select(&bridge_controller,bridge_boot.identity.vm_id,control,&selected);
 uint64_t now;xdiag_clock_us(&now);bool chosen=selected.locally_active&&selected.mode==DRIVE_AUTO;
 struct drive_warm_view view=drive_warm_classify(&bridge_controller.automatic,&selected,&bridge_status,&bridge_completion,chosen,
  (struct xm_span){NULL,0},0,context_revision,1,(int64_t)(now/1000),now,true);
 char body[256];int n=xm_app_lifecycle_json(body,sizeof(body),view.generation,++check_sequence,bridge_completion.progress,view.phase,view.state,view.reason);
 if(n>0)printf("LIFECYCLE %s\n",body);
}
static int calculate(uint64_t sequence){
 struct drive_result selected;struct xm_span control={bridge_inputs.control.payload,(uint16_t)bridge_inputs.control.length};
 drive_controller_select(&bridge_controller,bridge_boot.identity.vm_id,control,&selected);
 if(!selected.control_valid||!selected.locally_active||selected.mode!=DRIVE_AUTO)return -EACCES;
 memset(&bridge_completion,0,sizeof(bridge_completion));struct drive_completion *w=&bridge_completion;
 xdiag_clock_us(&w->begin_us);w->source.lifetime=1;w->source.context_revision=w->context_revision=context_revision;w->source.connection_revision=1;
 w->source.sequence=w->sequence=sequence;strcpy(w->source.vm_id,bridge_boot.identity.vm_id);strcpy(w->source.app_instance,bridge_boot.auto_instance_id);
 w->source.control_length=control.len;memcpy(w->source.control,control.p,control.len);
 w->source.release_us=w->begin_us;w->source.deadline_us=w->begin_us+20000;w->performed=true;
 w->rc=drive_adapter_run_until(&bridge_controller.automatic,control,(struct xm_span){NULL,0},0,NULL,NULL,w->source.deadline_us,&w->sample);
 xdiag_clock_us(&w->end_us);w->finished_ms=(int64_t)(w->end_us/1000);w->progress=bridge_controller.automatic.progress;
 static unsigned observed,previous_generation,previous_episode,previous_phase;static int previous_engine_reason;
 static char previous_reason[64];static bool previous_invalidated;
 const char *actual_reason=w->sample.reason?w->sample.reason:"OK";
 unsigned actual_generation=engine.current>=0?engine.paths[engine.current].generation:0;
 unsigned actual_episode=engine.current>=0&&engine.paths[engine.current].path.has_episode?engine.paths[engine.current].path.episode.core.state:0;
 int actual_engine_reason=atomic_load(&engine.last_reason);
 bool changed=!observed||strcmp(previous_reason,actual_reason)||previous_generation!=actual_generation||
  previous_episode!=actual_episode||previous_phase!=(unsigned)engine.episode.phase||previous_engine_reason!=actual_engine_reason||previous_invalidated!=engine.path_invalidated;
 if(changed&&observed<12){
  snprintf(previous_reason,sizeof(previous_reason),"%s",actual_reason);previous_generation=actual_generation;previous_episode=actual_episode;
  previous_phase=engine.episode.phase;previous_engine_reason=actual_engine_reason;previous_invalidated=engine.path_invalidated;
  const struct lp_path *p=engine.current>=0?&engine.paths[engine.current].path:NULL;
  fprintf(stderr,"B09_CALC n=%u cap=12 sequence=%llu end_guest=%llu rc=%d reason=%s engine_reason=%s search=%s currentgen=%u samplegen=%u sampleVehicle=%lld path_episode=%u initial_pi=%llu tracker_last_pi=%llu tracker_initialized=%d invalidated=%d facts_current=%d factsE=%llu\n",
   ++observed,(unsigned long long)w->sequence,(unsigned long long)w->end_us,w->rc,actual_reason,
   de_reason(atomic_load(&engine.last_reason)),lp_reason(atomic_load(&engine.search_reason)),
   engine.current>=0?engine.paths[engine.current].generation:0,w->sample.path_generation,(long long)w->sample.input_sequence,
   p&&p->has_episode?p->episode.core.state:0,(unsigned long long)(p?p->initial_pi_us:0),
   (unsigned long long)engine.tracker.last_pi_us,engine.tracker.initialized,engine.path_invalidated,(int)atomic_load(&engine.facts_current),
   (unsigned long long)engine.facts.expires_pi_us);
 }
 drive_lattice_completed(w);struct xm_drive_command3 command;uint64_t until=0;
 int rc=drive_lattice_command(w,&command,&until);if(rc)return rc;
 char body[XM_COMMAND2_BYTES+1];int n=xm_drive_command3_json(body,sizeof(body),&command);if(n<0)return n;
 drive_lattice_command_attempt(w,&command);printf("COMMAND %llu %s\n",(unsigned long long)until,body);return 0;
}
int main(void){
 clock_origin=wall_us();setvbuf(stdout,NULL,_IOLBF,0);
 de_init(&engine,&lp_selected_config,&lp_selected_assets);drive_controller_init(&bridge_controller);
 drive_lattice_adapter(&bridge_controller.automatic,false);drive_lattice_adapter(&bridge_controller.manual,true);
 assert(!b08_input_bind(&engine));
 while(fgets(bridge_line,sizeof(bridge_line),stdin)){
  char *argument=strchr(bridge_line,' ');if(argument)*argument++=0;else bridge_line[strcspn(bridge_line,"\r\n")]=0;
  int rc=0;
  if(!strcmp(bridge_line,"boot")){
   assert(argument&&sscanf(argument,"%36s %36s %36s %36s %36s %36s",bridge_boot.identity.vm_id,bridge_boot.identity.execution_id,
    bridge_boot.auto_instance_id,bridge_boot.identity.host_boot_id,bridge_boot.identity.boot_id,bridge_inputs.control.instance)==6);
   strcpy(bridge_boot.world_profile,lp_selected_config.id);strcpy(bridge_boot.world_broker_ipv4,"127.0.0.1");strcpy(bridge_boot.broker_ipv4,"127.0.0.1");
   bridge_boot.port=bridge_boot.world_port=1883;strcpy(bridge_inputs.vm_id,bridge_boot.identity.vm_id);strcpy(bridge_inputs.app_instances[0],bridge_boot.auto_instance_id);
   strcpy(bridge_inputs.control.sender,"dom0-gateway");bridge_inputs.connected=true;sys_rand_get(behavior_boot_nonce,sizeof(behavior_boot_nonce));
  }else if(!strcmp(bridge_line,"control")){
   rc=argument?unhex(argument,bridge_wire,sizeof(bridge_wire)):-EINVAL;
   if(rc>0&&(size_t)rc<=XM_MAX_DRIVING_BYTES){bridge_inputs.control.length=(size_t)rc;memcpy(bridge_inputs.control.payload,bridge_wire,(size_t)rc);
    bridge_inputs.control.payload[rc]=0;context_revision++;drive_lattice_context(&bridge_inputs);rc=0;}
  }else if(!strcmp(bridge_line,"link")||!strcmp(bridge_line,"world")||!strcmp(bridge_line,"vehicle")){
   int n=argument?unhex(argument,bridge_wire,sizeof(bridge_wire)):-EINVAL;
   rc=n<0?n:!strcmp(bridge_line,"link")?b08_input_link(bridge_wire,(size_t)n):!strcmp(bridge_line,"world")?
    b08_input_world(bridge_wire,(size_t)n):b08_input_vehicle(bridge_wire,(size_t)n);
  }else if(!strcmp(bridge_line,"step")){
   unsigned count=argument?(unsigned)strtoul(argument,NULL,10):1;assert(count&&count<=2048);
   for(unsigned k=0;k<count;k++){b08_input_map_step();(void)planner_step();b08_input_step();}
   observe_step();
  }else if(!strcmp(bridge_line,"io")){b08_input_map_step();b08_input_step();}
  else if(!strcmp(bridge_line,"event-clock")){
   char *second=argument?strchr(argument,' '):NULL;struct dl_clock original={0};
   if(!second)rc=-EINVAL;
   else rc=dl_episode_event_clock(strtoull(argument,NULL,10),strtoull(second+1,NULL,10),&original);
   printf("EVENT_CLOCK {\"rc\":%d,\"sequence\":%llu,\"support_guest_us\":%llu,\"confirmed_guest_us\":%llu,\"reserved_guest_us\":%llu,\"expires_guest_us\":%llu,\"expires_pi_us\":%llu}\n",
    rc,(unsigned long long)original.probe_sequence,(unsigned long long)original.support_guest_us,
    (unsigned long long)original.confirmed_guest_us,(unsigned long long)original.event_reserved_guest_us,
    (unsigned long long)original.expires_guest,(unsigned long long)original.expires_pi);
  }else if(!strcmp(bridge_line,"disconnect")){
   /* Declared transport loss drives the actual product context fence. */
   bridge_inputs.connected=false;drive_lattice_context(&bridge_inputs);
  }
  else if(!strcmp(bridge_line,"publication-policy"))rc=publication_policy(argument);
  else if(!strcmp(bridge_line,"calc"))rc=argument?calculate(strtoull(argument,NULL,10)):-EINVAL;
  else if(!strcmp(bridge_line,"life"))lifecycle();
  else if(!strcmp(bridge_line,"meta")){
   struct dl_clock mapping={0};uint64_t pi_now=0;int mapped=dl_clock_get(&mapping,&pi_now);
   printf("META {\"behavior\":%d,\"planning\":%d,\"prepared\":%d,\"phase\":%u,\"parking\":%u,\"request\":%d,\"reason\":%d,\"engine_bytes\":%u,\"state_bytes\":%u,\"clock_reply_bytes\":%u,"
    "\"clock_rc\":%d,\"clock_acknowledged\":%d,\"clock_probe_sequence\":%llu,\"clock_expires_guest\":%llu,\"pi_now\":%llu,"
    "\"map_current\":%d,\"maps_published\":%llu,\"receive_awaiting\":%d,\"last_world_result\":%d,\"requested\":%u,\"started\":%u,\"path_current\":%d,"
    "\"last_reason\":%d,\"last_reason_name\":\"%s\",\"search_reason\":%d,\"search_reason_name\":\"%s\",\"has_grant\":%d,\"facts_current\":%d,\"episode_state\":%u,"
    "\"preflight_last_rc\":%d,\"preflight_attempt_checked\":%llu,\"preflight_attempt_e\":%llu,\"preflight_vehicle_sequence\":%llu,\"operation_status_available\":%d,"
    "\"preflight_geometry_checks\":%llu,\"preflight_geometry_cached\":%d,\"path_invalidated\":%d,\"prepared_operation_present\":%d}\n",
    engine.behavior_enabled,engine.planning,(int)atomic_load(&prepared_valid),engine.episode.phase,engine.parking.phase,engine.episode_request_ready||engine.episode_receipt_pending,
    engine.episode.reason,(unsigned)sizeof(engine),(unsigned)sizeof(dw_state),(unsigned)sizeof(xenova_drive_ClockReply),
    mapped,mapping.acknowledged,(unsigned long long)mapping.probe_sequence,(unsigned long long)mapping.expires_guest,(unsigned long long)pi_now,
    engine.maps.current,(unsigned long long)engine.maps.published,engine.receive_awaiting,atomic_load(&engine.last_world_result),atomic_load(&engine.requested),
    engine.started,engine.current,atomic_load(&engine.last_reason),de_reason(atomic_load(&engine.last_reason)),atomic_load(&engine.search_reason),
    lp_reason(atomic_load(&engine.search_reason)),engine.has_grant,(int)atomic_load(&engine.facts_current),engine.episode_state.core.state,
    preflight_last_result,(unsigned long long)preflight_report.checked_guest_us,(unsigned long long)preflight_report.valid_until_pi_us,
    (unsigned long long)preflight_report.vehicle_sequence,(int)atomic_load(&planner_operation_available),
    (unsigned long long)preflight_geometry_checks,(int)atomic_load(&preflight_geometry_valid),(int)atomic_load(&engine.path_invalidated),
    prepared_mission_present&&prepared_mission.has_operation_binding);
  }else if(!strcmp(bridge_line,"quit")){atomic_set(&stop,1);atomic_set(&planner_stop,1);de_stop(&engine);
   while(engine.planning)(void)de_plan_slice(&engine,1,0);
   while(engine.apply_pending)(void)de_apply_finish(&engine,0);
   if(planner_turn.slot>=0)dl_state_release(planner_turn.slot);
   printf("END 0 0\n");break;
  }else rc=-EINVAL;
  uint64_t now;xdiag_clock_us(&now);printf("END %d %llu\n",rc,(unsigned long long)now);
 }
 return 0;
}
