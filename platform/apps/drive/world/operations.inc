/* Bounded operation/preflight structure; no current clock/authority issuance. */
static bool operation_uuid(const uint8_t *bytes,const char *text){
 static const char hex[]="0123456789abcdef";unsigned at=0;
 if(!source_uuid_text(text))return false;
 for(unsigned i=0;i<16;i++){if(i==4||i==6||i==8||i==10)at++;
  if(text[at++]!=hex[bytes[i]>>4]||text[at++]!=hex[bytes[i]&15])return false;}
 return true;
}
enum dw_result dw_operation_binding_validate(const xenova_drive_OperationBinding *b){
 if(!b||!typed_valid(b,xenova_drive_OperationBinding_fields,0))return DW_ARRAY;
 return !nil_uuid(b->service_epoch.bytes)&&positive63(b->operation_revision)&&positive63(b->permission_revision)?DW_OK:DW_SOURCE;
}
bool dw_operation_binding_equal(const xenova_drive_OperationBinding *a,const xenova_drive_OperationBinding *b){
 return a&&b&&!memcmp(a->service_epoch.bytes,b->service_epoch.bytes,16)&&a->operation_revision==b->operation_revision&&
  !memcmp(a->departure_nonce.bytes,b->departure_nonce.bytes,16)&&a->permission_revision==b->permission_revision&&a->parking_enabled==b->parking_enabled;
}
enum dw_result dw_operation_status_validate(const xenova_drive_OperationStatus *s){
 if(!s||!typed_valid(s,xenova_drive_OperationStatus_fields,0))return DW_ARRAY;
 enum dw_result rc=dw_operation_binding_validate(&s->binding);if(rc)return rc;
 if(s->major!=1||s->vehicle_phase<1||s->vehicle_phase>5||s->desired_mode>2||s->blocked_reason>31)return DW_VERSION;
 if(!positive63(s->ready_revision)||!positive63(s->valid_until_pi_us)||(s->auto_ready&&s->blocked_reason)||
    (s->vehicle_phase>=3&&nil_uuid(s->binding.departure_nonce.bytes)))return DW_SOURCE;
 if(s->vehicle_phase==3)return positive63(s->countdown_until_pi_us)&&!nil_uuid(s->active_request_nonce.bytes)&&s->desired_mode==1?DW_OK:DW_SOURCE;
 return s->countdown_until_pi_us?DW_SOURCE:DW_OK;
}
enum dw_result dw_auto_preflight_validate(const xenova_drive_AutoInputPreflight *p){
 if(!p||!typed_valid(p,xenova_drive_AutoInputPreflight_fields,0))return DW_ARRAY;
 if(p->major!=1)return DW_VERSION;
 enum dw_result rc=dw_episode_source_validate(&p->source);if(rc)return rc;
 return positive63(p->vehicle_sequence)&&p->control_generation&&positive63(p->checked_guest_us)&&positive63(p->valid_until_pi_us)&&
  positive63(p->clock_probe_sequence)&&!zero32(p->clock_reply_sha256.bytes)?DW_OK:DW_SOURCE;
}
enum dw_result dw_pi_operation_gate_validate(const xenova_drive_PiOperationGate *g){
 if(!g||!typed_valid(g,xenova_drive_PiOperationGate_fields,0))return DW_ARRAY;
 if(g->mode>2)return DW_VERSION;
 return !nil_uuid(g->gateway_instance.bytes)&&g->control_generation&&positive63(g->operation_revision)&&positive63(g->vehicle_sequence)&&
  positive63(g->valid_until_pi_us)&&(!g->wait_confirmed||positive63(g->stop_command_sequence))&&
  (!g->manual_local_admitted||g->mode==2)?DW_OK:DW_SOURCE;
}
enum dw_result dw_operation_probe_validate(const xenova_drive_ClockProbe *p){
 if(p->has_supported_state_features&&(p->supported_state_features&~DW_STATE_KNOWN_FEATURES))return DW_VERSION;
 if(!p->has_auto_input_preflight)return DW_OK;
 enum dw_result rc=dw_auto_preflight_validate(&p->auto_input_preflight);if(rc)return rc;
 return p->has_supported_state_features&&(p->supported_state_features&DW_OPERATION)&&
  p->auto_input_preflight.clock_probe_sequence<p->sequence&&p->auto_input_preflight.checked_guest_us<=p->guest_send_us?DW_OK:DW_SOURCE;
}
enum dw_result dw_operation_clock_validate(const xenova_drive_ClockReply *r){
 enum dw_result rc=dw_operation_probe_validate(&r->probe);if(rc||!r->has_operation_status)return rc;
 rc=dw_operation_status_validate(&r->operation_status);if(rc)return rc;
 const xenova_drive_OperationStatus *s=&r->operation_status;
 if(!r->probe.has_supported_state_features||!(r->probe.supported_state_features&DW_OPERATION)||
    !operation_uuid(s->binding.service_epoch.bytes,r->gateway_instance)||!r->has_guest_clock_valid_until_pi_us||
    s->valid_until_pi_us>r->guest_clock_valid_until_pi_us)return DW_SOURCE;
 if(s->auto_ready&&(!r->probe.has_auto_input_preflight||r->probe.auto_input_preflight.valid_until_pi_us<=r->pi_send_us||
    s->valid_until_pi_us>r->probe.auto_input_preflight.valid_until_pi_us))return DW_TIME;
 return r->has_current_mission&&r->current_mission.admitted&&r->current_mission.has_operation_binding&&
  !dw_operation_binding_equal(&s->binding,&r->current_mission.operation_binding)?DW_SOURCE:DW_OK;
}
enum dw_result dw_mapper_operation_probe_validate(const xenova_drive_MapperClockProbe *p){
 if(!p||!typed_valid(p,xenova_drive_MapperClockProbe_fields,0))return DW_ARRAY;
 if(!p->has_operation_gate)return DW_OK;
 enum dw_result rc=dw_pi_operation_gate_validate(&p->operation_gate);if(rc)return rc;
 return p->operation_gate.valid_until_pi_us>p->pi_send_us?DW_OK:DW_TIME;
}
enum dw_result dw_mapper_operation_reply_validate(const xenova_drive_MapperClockReply *r){
 if(!r||!typed_valid(r,xenova_drive_MapperClockReply_fields,0))return DW_ARRAY;
 return r->has_supported_mapper_features&&(r->supported_mapper_features&~1u)?DW_VERSION:DW_OK;
}
bool dw_preflight_source_matches(const xenova_drive_EpisodeSourceContext *s,const dw_world *w){
 static const char hex[]="0123456789abcdef";
 if(!s||!w||s->primitive.size!=32||strlen(w->primitive)!=64)return false;
 for(unsigned i=0;i<32;i++)if(w->primitive[2*i]!=hex[s->primitive.bytes[i]>>4]||w->primitive[2*i+1]!=hex[s->primitive.bytes[i]&15])return false;
 return !strcmp(s->vehicle,w->vehicle)&&!strcmp(s->mapper_boot,w->mapper_boot)&&!strcmp(s->mapper_instance,w->mapper_instance)&&
  !strcmp(s->map_epoch,w->map_epoch)&&!strcmp(s->frame_epoch,w->frame_epoch)&&!strcmp(s->odom_epoch,w->odom_epoch)&&
  !strcmp(s->clock_epoch,w->clock_epoch)&&!strcmp(s->configuration,w->configuration)&&!strcmp(s->calibration,w->calibration)&&
  !strcmp(s->reference_id,w->reference_id)&&s->reference_revision==w->reference_revision&&
  s->world_sequence==w->sequence&&s->observation_sequence==w->observation_seq;
}
