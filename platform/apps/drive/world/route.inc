/* route structure/original hashes. No socket, current clock, registry or motion authority. */
static enum dw_result rt_semantic(const void *,const pb_msgdesc_t *);
static bool rt_id(const void *p){return !nil_uuid(p);}
static bool rt_sha(const void *p){return !zero32(p);}
static bool rt_pair(const uint8_t *id,uint64_t revision){return nil_uuid(id)==(revision==0);}
static bool rt_convex(const dw_point *p,size_t n){
 if(n<3||n>8||!simple(p,n,true))return false;
 float sign=0;
 for(size_t i=0;i<n;i++){if(!finite_point(p[i]))return false;float turn=cross(p[(i+n-2)%n],p[(i+n-1)%n],p[i]);
  if(fabsf(turn)>1e-9f){if(sign&&sign*turn<0)return false;sign=turn;}}
 return sign!=0;
}
static enum dw_result rt_tree(const void *value,const pb_msgdesc_t *desc,unsigned depth){
 if(!value||depth>8)return DW_ARRAY;
 pb_field_iter_t it;if(!pb_field_iter_begin(&it,desc,(void *)value))return DW_ARRAY;
 do{
  unsigned ht=PB_HTYPE(it.type);
  if(ht==PB_HTYPE_OPTIONAL&&it.pSize&&!*(const bool *)it.pSize)continue;
  if(ht==PB_HTYPE_ONEOF&&(!it.pSize||*(const pb_size_t *)it.pSize!=it.tag))continue;
  size_t count=ht==PB_HTYPE_REPEATED?*(const pb_size_t *)it.pSize:1;
  for(size_t i=0;i<count;i++){
   const uint8_t *item=(const uint8_t *)it.pData+i*it.data_size;
   if(it.submsg_desc){enum dw_result rc=rt_tree(item,it.submsg_desc,depth+1);if(rc)return rc;}
   else{size_t width=route_float_size(desc,it.tag);
    if(width==4){float f;memcpy(&f,item,4);if(!isfinite(f))return DW_NUMERIC;}
    if(width==8){double f;memcpy(&f,item,8);if(!isfinite(f))return DW_NUMERIC;}}
  }
 }while(pb_field_iter_next(&it));
 if(desc==xenova_drive_Pose_fields)return dw_pose_finite(*(const dw_pose *)value)?DW_OK:DW_NUMERIC;
 if(desc==xenova_drive_Point_fields)return finite_point(*(const dw_point *)value)?DW_OK:DW_NUMERIC;
 if(desc==xenova_drive_LaneModel_fields)return lane_validate(value);
 if(desc==xenova_drive_OperationBinding_fields)return dw_operation_binding_validate(value);
 if(desc==xenova_drive_EpisodeSourceContext_fields)return dw_episode_source_validate(value);
 if(desc==xenova_drive_VehicleSample_fields)return vehicle_validate(value);
 if(desc==xenova_drive_StopFacts_fields)return ep_stop(value);
 if(desc==xenova_drive_PiAbsoluteMeter_fields){const xenova_drive_PiAbsoluteMeter *m=value;
  if(!isfinite(m->metres_per_tick)||m->metres_per_tick<0||(m->valid&&(!rt_id(m->meter_epoch.bytes)||!m->vehicle_sequence||m->metres_per_tick<=0)))return DW_SOURCE;}
 if(desc==xenova_drive_DriveControlFacts_fields){const xenova_drive_DriveControlFacts *f=value;
  if(f->major!=1||((f->meter.valid||(f->has_stop&&f->stop.state))&&!positive63(f->valid_until_pi_us)))return DW_SOURCE;}
 return route_cap(desc)?rt_semantic(value,desc):DW_OK;
}
enum dw_result dw_route_validate(const void *value,const pb_msgdesc_t *desc){
 if(!route_cap(desc)||!typed_valid(value,desc,0))return DW_ARRAY;
 size_t n=0;if(!pb_get_encoded_size(&n,desc,value)||n>message_cap(desc))return DW_TOO_LARGE;
 return rt_tree(value,desc,0);
}
struct rt_compare {const uint8_t *raw;size_t size,at;};
static bool rt_compare_write(pb_ostream_t *s,const pb_byte_t *p,size_t n){
 struct rt_compare *c=s->state;if(n>c->size-c->at||memcmp(c->raw+c->at,p,n))return false;c->at+=n;return true;
}
enum dw_result dw_route_decode(const uint8_t *p,size_t n,const pb_msgdesc_t *desc,void *out){
 if(!p||!out||!route_cap(desc)||!wire_valid(p,n,desc,0))return DW_DECODE;
 pb_istream_t in=pb_istream_from_buffer(p,n);if(!pb_decode(&in,desc,out))return DW_DECODE;
 enum dw_result rc=dw_route_validate(out,desc);if(rc)return rc;
 struct rt_compare c={p,n,0};pb_ostream_t stream={.callback=rt_compare_write,.state=&c,.max_size=n,.bytes_written=0};
 return pb_encode(&stream,desc,out)&&c.at==n?DW_OK:DW_DECODE;
}
/* Only the new management subtrees require canonical known-field bytes. Legacy
 * parents retain their existing unknown-optional-field compatibility. */
static enum dw_result rt_raw_children(const uint8_t *p,size_t n,const pb_msgdesc_t *desc,const void *value,unsigned depth){
 if(depth>8)return DW_DECODE;
 if(route_cap(desc)){
  enum dw_result rc=dw_route_validate(value,desc);if(rc)return rc;
  struct rt_compare c={p,n,0};pb_ostream_t stream={.callback=rt_compare_write,.state=&c,.max_size=n,.bytes_written=0};
  return pb_encode(&stream,desc,value)&&c.at==n?DW_OK:DW_DECODE;
 }
 if(desc!=xenova_drive_WorldSnapshot_fields&&desc!=xenova_drive_DriveState_fields&&desc!=xenova_drive_DriveLink_fields&&
    desc!=xenova_drive_AcceptedState_fields&&desc!=xenova_drive_ClockReply_fields&&desc!=xenova_drive_MissionExecution_fields&&
    desc!=xenova_drive_MissionGrant_fields&&desc!=xenova_drive_MissionBudgetReply_fields)return DW_OK;
 size_t at=0;pb_field_iter_t it;
 while(at<n){uint64_t tag,size;if(!varint(p,n,&at,&tag))return DW_DECODE;unsigned kind=(unsigned)(tag&7);
  if(!kind){if(!varint(p,n,&at,&size))return DW_DECODE;continue;}
  if(kind==2){if(!varint(p,n,&at,&size))return DW_DECODE;}else if(kind==1)size=8;else if(kind==5)size=4;else return DW_DECODE;
  if(size>n-at)return DW_DECODE;
  if(pb_field_iter_begin(&it,desc,(void *)value)&&pb_field_iter_find(&it,(uint32_t)(tag>>3))&&it.submsg_desc){
   /* All paths to new route subtrees are singular/oneof in existing parents. */
   if(PB_HTYPE(it.type)==PB_HTYPE_REPEATED){at+=(size_t)size;continue;}
   enum dw_result rc=rt_raw_children(p+at,(size_t)size,it.submsg_desc,it.pData,depth+1);if(rc)return rc;
  }
  at+=(size_t)size;
 }
 return DW_OK;
}
bool dw_route_body(const uint8_t *p,size_t n,unsigned field,const uint8_t **body,size_t *size){
 if(!p||!body||!size||n>DW_WIRE_MAX)return false;
 size_t at=0;while(at<n){uint64_t tag,length;if(!varint(p,n,&at,&tag))return false;
  unsigned kind=(unsigned)(tag&7);if(!kind){if(!varint(p,n,&at,&length))return false;continue;}
  if(kind==2){if(!varint(p,n,&at,&length))return false;}else if(kind==1)length=8;else if(kind==5)length=4;else return false;
  if(length>n-at)return false;
  if(tag>>3==field){if(kind!=2)return false;*body=p+at;*size=(size_t)length;return true;}at+=(size_t)length;}
 return false;
}
static bool rt_pb_hash(const void *value,const pb_msgdesc_t *desc,const char *domain,uint8_t out[32]){
 size_t n=0;if(!pb_get_encoded_size(&n,desc,value)||n>DW_WIRE_MAX)return false;
 struct dhash hash;dh_init(&hash,domain,strlen(domain)+1);dh_u(&hash,n,4);
 pb_ostream_t stream={.callback=dh_pb_write,.state=&hash,.max_size=DW_WIRE_MAX,.bytes_written=0};
 return pb_encode(&stream,desc,value)&&dh_done(&hash,out);
}
#define RT_HASH(stem,Type,domain) bool dw_route_##stem##_hash(const xenova_drive_##Type *v,uint8_t out[32]){\
 return dw_route_validate(v,xenova_drive_##Type##_fields)==DW_OK&&rt_pb_hash(v,xenova_drive_##Type##_fields,"xenova.drive.route." domain ".v1",out);}
RT_HASH(query,RouteQuery,"query")
RT_HASH(candidate,RouteCandidate,"candidate")
RT_HASH(core,RouteCore,"core")
RT_HASH(owner,RouteOwnerContext,"owner")
RT_HASH(witness,RouteCurrentWitness,"current-witness")
RT_HASH(receipt,RouteReceipt,"receipt")
RT_HASH(eligibility,RouteEligibility,"eligibility")
RT_HASH(transition_request,WindowTransitionRequest,"transition-request")
RT_HASH(transition_receipt,WindowTransitionReceipt,"transition-receipt")
RT_HASH(data_request,RoadDataRequest,"data-request")
RT_HASH(registration_request,RoadRegistrationRequest,"registration-request")
RT_HASH(world_witness,MapperWorldWitness,"world-witness")
#undef RT_HASH
bool dw_route_permission_hash(const xenova_drive_MissionGrant *v,uint8_t out[32]){
 return dw_mission_grant_validate(v)==DW_OK&&rt_pb_hash(v,xenova_drive_MissionGrant_fields,"xenova.drive.route.current-permission.v1",out);
}
bool dw_route_stop_facts_hash(const xenova_drive_DriveControlFacts *v,uint8_t out[32]){
 return v&&typed_valid(v,xenova_drive_DriveControlFacts_fields,0)&&rt_tree(v,xenova_drive_DriveControlFacts_fields,0)==DW_OK&&
  rt_pb_hash(v,xenova_drive_DriveControlFacts_fields,"xenova.drive.route.stop-facts.v1",out);
}
static const pb_msgdesc_t *rt_record_desc(uint32_t kind){
 switch(kind){case 1:return xenova_drive_StartGoal_fields;case 2:return xenova_drive_RoadTopology_fields;
 case 3:return xenova_drive_RoadSection_fields;case 4:return xenova_drive_RoadConnection_fields;
 case 5:return xenova_drive_RoadWindow_fields;case 6:return xenova_drive_ExitSpace_fields;
 case 7:return xenova_drive_RoutePlan_fields;case 8:return xenova_drive_RoadGeometryScope_fields;default:return NULL;}
}
bool dw_route_record_hash(uint32_t kind,const void *v,uint8_t out[32]){
 static const char *const domains[]={"xenova.drive.route.record.v1/1","xenova.drive.route.record.v1/2","xenova.drive.route.record.v1/3","xenova.drive.route.record.v1/4","xenova.drive.route.record.v1/5","xenova.drive.route.record.v1/6","xenova.drive.route.record.v1/7","xenova.drive.route.record.v1/8"};
 const pb_msgdesc_t *desc=rt_record_desc(kind);
 return desc&&dw_route_validate(v,desc)==DW_OK&&rt_pb_hash(v,desc,domains[kind-1],out);
}
static bool rt_text_length(const char *p,size_t cap,size_t *n){
 const char *end=memchr(p,0,cap);if(!end||end==p||end-p>64)return false;
 for(const char *at=p;at<end;at++){if((unsigned char)*at<33||(unsigned char)*at>126)return false;}
 *n=(size_t)(end-p);return true;
}
bool dw_route_source_key_hash(const xenova_drive_EpisodeSourceContext *s,uint8_t out[32]){
 if(dw_episode_source_validate(s))return false;
 const char *values[]={s->vehicle,s->mapper_boot,s->mapper_instance,s->map_epoch,s->frame_epoch,s->odom_epoch,s->clock_epoch,s->configuration,s->calibration};
 size_t n=4+32,lengths[9];
 for(unsigned i=0;i<9;i++){if(!rt_text_length(values[i],i==0||i>=7?65:37,&lengths[i]))return false;n+=4+lengths[i];}
 struct dhash h;const char *domain="xenova.drive.route.source-key.v1";dh_init(&h,domain,strlen(domain)+1);dh_u(&h,n,4);
 for(unsigned i=0;i<9;i++){dh_u(&h,lengths[i],4);dh_put(&h,values[i],lengths[i]);}dh_u(&h,32,4);dh_put(&h,s->primitive.bytes,32);return dh_done(&h,out);
}
static bool rt_uuid_bytes(const char *text,uint8_t out[16]){
 static const char *hex="0123456789abcdef";if(!source_uuid_text(text))return false;unsigned at=0;
 for(unsigned i=0;i<16;i++){if(i==4||i==6||i==8||i==10)at++;const char *a=strchr(hex,text[at++]),*b=strchr(hex,text[at++]);if(!a||!b)return false;out[i]=(uint8_t)((a-hex)*16+(b-hex));}return true;
}
bool dw_route_local_path_hash(const dw_state *s,const struct dw_route_path_ref *r,uint8_t out[32]){
 if(!s||!r||!out||!rt_sha(r->global_plan_sha256)||!rt_sha(r->geometry_scope_sha256)||!rt_sha(r->basis_window_sha256)||
  (r->task_space_kind!=1&&r->task_space_kind!=2)||((r->task_space_kind==1)!=!rt_sha(r->exit_space_sha256))||
  !positive63(r->basis_revision)||!r->basis_prefix||r->basis_prefix_count<2||r->basis_prefix_count>32||
  s->path_count<2||s->path_count>512||s->segments_count<1||s->segments_count>3||s->direction< -1||s->direction>1||s->maneuver_phase>2||!positive63(s->maneuver_revision))return false;
 if(s->segments[0].first_index||s->segments[s->segments_count-1].last_index!=(uint32_t)s->path_count-1u)return false;
 size_t ln[3];if(!rt_text_length(s->reference_id,sizeof(s->reference_id),&ln[0])||!rt_text_length(s->configuration,sizeof(s->configuration),&ln[1])||!rt_text_length(s->calibration,sizeof(s->calibration),&ln[2]))return false;
 uint8_t ids[3][16],primitive[32];if(!rt_uuid_bytes(s->map_epoch,ids[0])||!rt_uuid_bytes(s->frame_epoch,ids[1])||!rt_uuid_bytes(s->odom_epoch,ids[2])||strlen(s->primitive)!=64)return false;
 static const char *hex="0123456789abcdef";
 for(unsigned i=0;i<32;i++){const char *a=strchr(hex,s->primitive[2*i]),*b=strchr(hex,s->primitive[2*i+1]);if(!a||!b)return false;primitive[i]=(uint8_t)((a-hex)*16+(b-hex));}
 size_t n=276+ln[0]+ln[1]+ln[2]+8*r->basis_prefix_count+20*s->path_count+12*s->segments_count;if(n>16384)return false;
 struct dhash h;const char *domain="xenova.drive.route.local-path.v1";dh_init(&h,domain,strlen(domain)+1);dh_u(&h,n,4);
 dh_u(&h,1,4);dh_put(&h,r->global_plan_sha256,32);dh_put(&h,r->geometry_scope_sha256,32);dh_put(&h,r->basis_window_sha256,32);dh_u(&h,r->task_space_kind,4);dh_put(&h,r->exit_space_sha256,32);
 dh_u(&h,ln[0],4);dh_put(&h,s->reference_id,ln[0]);dh_u(&h,r->basis_revision,8);dh_u(&h,r->basis_prefix_count,4);
 for(size_t i=0;i<r->basis_prefix_count;i++){dh_point(&h,r->basis_prefix[i]);}
 dh_u(&h,s->path_count,4);
 for(size_t i=0;i<s->path_count;i++){dh_pose(&h,s->path[i].pose);dh_f(&h,s->path[i].steering_rad);dh_f(&h,s->path[i].velocity_mps);}dh_u(&h,s->segments_count,4);
 for(size_t i=0;i<s->segments_count;i++){const xenova_drive_GearSegment *g=&s->segments[i];if(g->first_index>=g->last_index||g->last_index>=s->path_count||(g->signed_direction!=1&&g->signed_direction!=-1)||
   (i&&(g->first_index!=s->segments[i-1].last_index+1||g->signed_direction==s->segments[i-1].signed_direction)))return false;
  if(i){const xenova_drive_PathPoint *before=&s->path[s->segments[i-1].last_index],*after=&s->path[g->first_index];
   if(!pose_equal(before->pose,after->pose)||before->velocity_mps||after->velocity_mps)return false;}
  dh_u(&h,g->first_index,4);dh_u(&h,g->last_index,4);dh_u(&h,(uint32_t)g->signed_direction,4);}
 dh_pose(&h,s->merge_goal);dh_u(&h,(uint32_t)s->direction,4);dh_u(&h,s->maneuver_phase,4);dh_u(&h,s->maneuver_revision,8);
 dh_u(&h,ln[1],4);dh_put(&h,s->configuration,ln[1]);dh_u(&h,ln[2],4);dh_put(&h,s->calibration,ln[2]);dh_put(&h,primitive,32);
 for(unsigned i=0;i<3;i++){dh_put(&h,ids[i],16);}
 return dh_done(&h,out);
}

#define RN(c) do{if(!(c))return DW_SOURCE;}while(0)
#define RI(v,k) rt_id((v)->k.bytes)
#define RH(v,k) rt_sha((v)->k.bytes)
#define RP(v,k) positive63((v)->k)
#define RB(T) if(desc==xenova_drive_##T##_fields){const xenova_drive_##T *v=value;
#define RE return DW_OK;}
static bool rt_purpose(uint32_t purpose,const uint8_t goal[32]){return (purpose==1||purpose==2)&&((purpose==1)==!rt_sha(goal));}
static bool rt_geometry(const dw_point *reference,size_t n,const dw_point *left,size_t ln,const dw_point *right,size_t rn,
 const float *errors,size_t en,const xenova_drive_LaneModel *road,float heading){
 if(n<2||n>32||ln!=n||rn!=n||en!=n||!open_basis(reference,n)||!nonnegative(heading)||heading>3.141592654f||road->divider_count!=n)return false;
 dw_point polygon[64];
  for(size_t i=0;i<n;i++){polygon[i]=left[i];polygon[n+i]=right[n-1-i];if(!nonnegative(errors[i]))return false;}
 if(!simple(polygon,2*n,true))return false;
 for(size_t i=0;i<road->lanes_count;i++){if(road->lanes[i].centerline_count!=n)return false;}
 return true;
}
static enum dw_result rt_semantic(const void *value,const pb_msgdesc_t *desc){
 if(route_has_major(desc)){uint32_t major;memcpy(&major,value,4);if(major!=1)return DW_VERSION;}
 RB(RouteBinding) RN(RI(v,route_id)&&RP(v,revision)&&rt_purpose(v->purpose,v->goal_sha256.bytes)&&RH(v,core_sha256)&&RH(v,global_plan_sha256));
 RE
 RB(RoadGeometryScope)
  RN(RI(v,dataset_uuid)&&RI(v,map_epoch)&&RI(v,frame_epoch)&&RI(v,origin_pi_host_boot)&&RI(v,origin_gateway_instance)&&RI(v,origin_pi_session)&&RI(v,origin_device_session)&&RI(v,origin_odom_epoch));
  RN(RH(v,primitive)&&RH(v,alignment_frame_sha256)&&RH(v,geometry_policy_sha256)&&RH(v,origin_storage_context_sha256)&&v->coordinate_policy==1);
 RE
 RB(RoadDurableRef) RN(RI(v,database_uuid)&&RI(v,job_id)&&RP(v,durable_revision)&&RH(v,job_digest)&&RH(v,origin_context_sha256)&&RH(v,record_sha256)&&RH(v,original_receipt_sha256)&&v->status==1);
 RE
 RB(RoadObservationOrigin)
  RN(RI(v,mapper_boot)&&RI(v,mapper_instance)&&RI(v,clock_epoch)&&RP(v,observation_sequence)&&RP(v,frame_source_sequence)&&RP(v,vehicle_sequence));
  RN(v->capture_mapper_lower_us<=v->capture_mapper_upper_us&&v->capture_mapper_upper_us<=v->completed_mapper_us&&RH(v,raw_binding_sha256)&&RH(v,frame_evidence_sha256)&&RH(v,origin_context_sha256)&&RH(v,alignment_raw_sha256)&&RH(v,mapper_clock_reply_sha256));
  RN(!v->has_independent_world_sha256||RH(v,independent_world_sha256));
 RE
 RB(MapperRouteWitness)
  RN(RI(v,mapper_boot)&&RI(v,mapper_instance)&&RI(v,clock_epoch)&&RI(v,pi_host_boot)&&RI(v,pi_client_instance)&&RP(v,clock_probe_sequence)&&RP(v,checked_mapper_us)&&RP(v,valid_until_pi_us)&&RP(v,source_binding_revision)&&RP(v,vehicle_sequence)&&RP(v,observation_sequence));
  RN(RH(v,clock_reply_sha256)&&RH(v,raw_binding_sha256)&&RH(v,frame_evidence_sha256)&&rt_pair(v->scene_admission_id.bytes,v->scene_revision));
 RE
 RB(RouteOwnerContext)
  RN(RI(v,vm)&&RI(v,execution)&&RI(v,app_instance)&&v->control_generation&&rt_pair(v->mission_id.bytes,v->mission_revision)&&((v->grant_revision!=0)==(v->mission_revision!=0))&&rt_pair(v->scene_admission_id.bytes,v->scene_revision)&&RH(v,source_key_sha256)&&RH(v,geometry_scope_sha256));
 RE
 RB(RouteCurrentWitness) RN(RP(v,vehicle_sequence)&&RP(v,checked_guest_us)&&RP(v,valid_until_pi_us)&&RP(v,clock_probe_sequence)&&RH(v,clock_reply_sha256)&&RH(v,world_raw_sha256)&&RH(v,vehicle_raw_sha256));
 RE
 RB(StartGoal)
  RN(RI(v,goal_id)&&RI(v,section_id)&&RP(v,revision)&&v->lane_id&&RP(v,origin_vehicle_sequence)&&RP(v,stop_command_sequence)&&RP(v,stop_report_sequence));
  RN(RH(v,geometry_scope_sha256)&&RH(v,vehicle_raw_sha256)&&RH(v,alignment_raw_sha256)&&RH(v,world_raw_sha256)&&RH(v,terminal_profile_sha256)&&RH(v,stop_facts_sha256));
  RN(nonnegative(v->station_m)&&nonnegative(v->position_error_m)&&nonnegative(v->heading_error_rad)&&v->heading_error_rad<=3.141592654f&&v->origin_vehicle_sequence==v->origin.vehicle_sequence);
  RN(!v->origin.has_independent_world_sha256||!memcmp(v->origin.independent_world_sha256.bytes,v->world_raw_sha256.bytes,32));
 RE
 RB(RoadSectionRef) RN(RI(v,section_id)&&RH(v,section_sha256)&&v->left_lane_id&&v->right_lane_id&&v->left_lane_id!=v->right_lane_id&&v->length_m>0&&v->order<32);
 RE
 RB(RoadEdge)
  RN(v->from_section_index<32&&v->to_section_index<32&&v->from_lane_id&&v->to_lane_id&&(v->kind==1||v->kind==2)&&nonnegative(v->from_station_m)&&nonnegative(v->to_station_m)&&RH(v,connection_sha256));
  RN(v->kind!=2||(v->from_section_index==v->to_section_index&&v->from_lane_id!=v->to_lane_id&&v->from_station_m<v->to_station_m&&!v->closes_circuit));
 RE
 RB(RoadTopology)
  RN(RI(v,topology_id)&&RP(v,revision)&&RH(v,geometry_scope_sha256)&&RH(v,origin_roots_sha256)&&v->sections_count>0&&v->sections_count<=32&&v->edges_count<=96);
  bool closed=false;
  for(size_t i=0;i<v->sections_count;i++)for(size_t j=0;j<i;j++)RN(memcmp(v->sections[i].section_id.bytes,v->sections[j].section_id.bytes,16));
  for(size_t i=0;i<v->edges_count;i++){const xenova_drive_RoadEdge *e=&v->edges[i];RN(e->from_section_index<v->sections_count&&e->to_section_index<v->sections_count);
   const xenova_drive_RoadSectionRef *a=&v->sections[e->from_section_index],*b=&v->sections[e->to_section_index];
   RN((e->from_lane_id==a->left_lane_id||e->from_lane_id==a->right_lane_id)&&(e->to_lane_id==b->left_lane_id||e->to_lane_id==b->right_lane_id));closed|=e->closes_circuit;}
  RN(v->closed_observed==closed);
 RE
 RB(RoadSection)
  RN(RI(v,section_id)&&RP(v,revision)&&RH(v,geometry_scope_sha256)&&RH(v,alignment_raw_sha256)&&RH(v,observation_evidence_sha256));
  RN(rt_geometry(v->reference,v->reference_count,v->left_boundary,v->left_boundary_count,v->right_boundary,v->right_boundary_count,v->point_error_m,v->point_error_m_count,&v->road,v->heading_error_rad));
 RE
 RB(RoadWindow)
  RN(RI(v,window_id)&&RP(v,revision)&&RH(v,geometry_scope_sha256)&&RH(v,topology_sha256)&&v->section_sha256_count>0&&v->section_sha256_count<=2);
  for(size_t i=0;i<v->section_sha256_count;i++)RN(rt_sha(v->section_sha256[i].bytes));
  RN(rt_geometry(v->reference,v->reference_count,v->left_boundary,v->left_boundary_count,v->right_boundary,v->right_boundary_count,v->point_error_m,v->point_error_m_count,&v->road,v->heading_error_rad));
  if(!RI(v,from_window_id))RN(!RH(v,from_window_sha256)&&!RI(v,transition_id)&&!RH(v,overlap_prefix_sha256)&&!v->overlap_point_count);
  else RN(RI(v,transition_id)&&RH(v,from_window_sha256)&&RH(v,overlap_prefix_sha256)&&memcmp(v->from_window_id.bytes,v->window_id.bytes,16)&&v->overlap_point_count>=2&&v->overlap_point_count<=v->reference_count);
 RE
 RB(RoadConnection)
  RN(RI(v,connection_id)&&RP(v,revision)&&v->from_lane_id&&v->to_lane_id&&RH(v,geometry_scope_sha256)&&RH(v,from_section_sha256)&&RH(v,to_section_sha256));
  size_t n=v->overlap_reference_count;RN((v->kind==1||v->kind==2||v->kind==3)&&v->from_point_index<32&&v->to_point_index<32&&(!n||(n>=2&&n<=8))&&v->overlap_error_m_count==n&&rt_convex(v->connector_polygon,v->connector_polygon_count)&&v->evidence_roots_sha256_count>0&&v->evidence_roots_sha256_count<=4&&nonnegative(v->heading_error_rad)&&v->heading_error_rad<=3.141592654f);
  if(v->kind==3)RN(!memcmp(v->from_section_sha256.bytes,v->to_section_sha256.bytes,32)&&v->from_lane_id==v->to_lane_id&&v->from_point_index==v->to_point_index);
  for(size_t i=0;i<n;i++)RN(nonnegative(v->overlap_error_m[i]));
  for(size_t i=0;i<v->evidence_roots_sha256_count;i++)RN(rt_sha(v->evidence_roots_sha256[i].bytes));
 RE
 RB(ExitSpace)
  RN(RI(v,space_id)&&RI(v,parking_attempt_id)&&RP(v,revision)&&v->slot_id&&RP(v,slot_revision)&&v->aisle_lane_id&&RH(v,geometry_scope_sha256)&&RH(v,parking_goal_sha256)&&RH(v,road_section_sha256));
  RN(v->selected_slot_polygon_count==4&&rt_convex(v->selected_slot_polygon,4)&&v->connection_sha256_count>0&&v->connection_sha256_count<=2&&nonnegative(v->position_error_m)&&nonnegative(v->heading_error_rad)&&v->heading_error_rad<=3.141592654f);
  for(size_t i=0;i<v->connection_sha256_count;i++)RN(rt_sha(v->connection_sha256[i].bytes));
 RE
 RB(RouteOccurrence) RN(v->section_index<32&&(v->edge_index<96||v->edge_index==UINT32_MAX)&&v->lane_id&&nonnegative(v->entry_station_m)&&v->entry_station_m<=v->exit_station_m);
 RE
 RB(RoutePlan)
  RN(RI(v,route_id)&&RP(v,revision)&&rt_purpose(v->purpose,v->goal_sha256.bytes)&&RH(v,topology_sha256)&&RH(v,geometry_scope_sha256)&&RH(v,origin_guest_witness_sha256)&&RH(v,terminal_profile_sha256)&&v->max_laps==1&&v->occurrences_count<=96&&(v->purpose!=2||v->occurrences_count));
  for(size_t i=0;i<v->occurrences_count;i++)RN((v->occurrences[i].edge_index==UINT32_MAX)==(i+1==v->occurrences_count));
  if(RH(v,exit_space_sha256))RN(RI(v,merge_section_id)&&v->merge_lane_id&&nonnegative(v->merge_station_lower_m)&&v->merge_station_lower_m<=v->merge_station_upper_m);
  else RN(v->purpose==2&&!RI(v,merge_section_id)&&!v->merge_lane_id&&!v->merge_station_lower_m&&!v->merge_station_upper_m&&!v->merge_seed_pose.x&&!v->merge_seed_pose.y&&!v->merge_seed_pose.yaw);
 RE
 RB(RouteCore)
  RN(RI(v,route_id)&&RP(v,revision)&&RP(v,phase_revision)&&rt_purpose(v->purpose,v->goal_sha256.bytes)&&RH(v,topology_sha256)&&RH(v,global_plan_sha256)&&RH(v,geometry_scope_sha256)&&RH(v,budget_profile_sha256)&&RH(v,local_path_sha256));
  RN(v->phase>=1&&v->phase<=6&&v->max_laps==1&&v->target_lane_id&&v->max_travel_m>0&&(v->progress_mode==1||v->progress_mode==2)&&v->terminal_result<=3&&v->terminal_reason<=24);
  if(v->terminal_result)RN(v->phase==(v->terminal_result==1?5u:6u)&&((v->terminal_reason==0)==(v->terminal_result==1)));
  else RN(v->phase!=5&&!v->terminal_reason);
  RN((!RI(v,parking_attempt_id))==(!RH(v,parking_goal_sha256))&&((v->started_pi_us==0)==(v->deadline_pi_us==0))&&((v->phase_started_pi_us==0)==(v->phase_expires_pi_us==0)));
  if(v->started_pi_us)RN(v->started_pi_us<v->deadline_pi_us&&RI(v,meter_epoch)&&v->metres_per_tick_upper>0);
  else RN(!RI(v,meter_epoch)&&!v->meter_baseline_total_ticks&&!v->meter_gap_revision&&!v->metres_per_tick_upper);
  if(v->phase_started_pi_us)RN(v->started_pi_us<=v->phase_started_pi_us&&v->phase_started_pi_us<v->phase_expires_pi_us&&v->phase_expires_pi_us<=v->deadline_pi_us);
  RN((!v->stop_wait_command_sequence)==(!RH(v,stop_wait_facts_sha256))&&((v->stop_wait_started_pi_us!=0)==(v->stop_wait_command_sequence!=0)));
 RE
 RB(RouteProgress) RN(RP(v,vehicle_sequence)&&RP(v,calculation_sequence)&&RP(v,revision)&&nonnegative(v->station_lower_m)&&v->station_lower_m<=v->station_upper_m&&nonnegative(v->remaining_upper_m)&&v->completed_laps<=1);
 RE
 RB(RouteAcceptanceRef) RN(RI(v,registry_epoch)&&RI(v,acceptance_nonce)&&RI(v,request_nonce)&&RP(v,registry_revision)&&RP(v,request_sequence)&&RH(v,request_sha256)&&RH(v,accepted_core_sha256)&&RH(v,origin_owner_sha256));
 RE
 RB(RouteSnapshot)
  RN((v->state==2||v->state==3)&&(v->task_space_kind==1||v->task_space_kind==2)&&RP(&v->core,started_pi_us)&&RP(&v->core,deadline_pi_us)&&RP(&v->core,phase_started_pi_us)&&RP(&v->core,phase_expires_pi_us));
  uint8_t hash[32];RN(RH(v,current_window_sha256)&&RH(v,basis_window_sha256)&&RH(v,current_world_sha256)&&RP(v,basis_window_revision)&&dw_route_core_hash(&v->core,hash)&&!memcmp(hash,v->acceptance.accepted_core_sha256.bytes,32));
  RN((v->state==3)==(v->core.terminal_result!=0));if(v->task_space_kind==2)RN(RH(v,exit_space_sha256));else RN(!RH(v,exit_space_sha256));
  RN(!v->progress.last_progress_pi_us||(v->core.started_pi_us<=v->progress.last_progress_pi_us&&v->progress.last_progress_pi_us<=v->core.deadline_pi_us));
 RE
 RB(WorldRouteView)
  RN(RI(v,window_id)&&RI(v,first_section_id)&&RI(v,last_section_id)&&RP(v,window_revision)&&RP(v,geometry_revision)&&RH(v,geometry_scope_sha256)&&RH(v,topology_sha256)&&RH(v,window_sha256));
  if(!RI(v,from_window_id))RN(!RH(v,from_window_sha256)&&!RI(v,transition_id)&&!RH(v,overlap_prefix_sha256));else RN(RI(v,transition_id)&&RH(v,from_window_sha256)&&RH(v,overlap_prefix_sha256));
 RE
 RB(RouteExtentProof)
  RN(v->mode>=1&&v->mode<=3&&RH(v,window_sha256)&&RP(v,vehicle_sequence)&&nonnegative(v->available_forward_m)&&nonnegative(v->available_backward_m)&&nonnegative(v->required_forward_m)&&nonnegative(v->required_backward_m)&&nonnegative(v->braking_distance_m)&&nonnegative(v->body_radius_m)&&nonnegative(v->position_bound_m)&&nonnegative(v->heading_bound_rad));
  if(v->mode==1)RN(!RH(v,cusp_sweep_sha256)&&!RH(v,s19_sweep_sha256));
  if(v->mode==2)RN(RH(v,exit_space_sha256)&&RH(v,cusp_sweep_sha256));
  if(v->mode==3)RN(RH(v,s19_sweep_sha256));
 RE
 RB(RouteEligibility)
  RN(RI(v,eligibility_id)&&rt_purpose(v->purpose,v->goal_sha256.bytes)&&(v->task_space_kind==1||v->task_space_kind==2)&&v->reason<=24&&(v->eligible==(v->reason==0))&&RH(v,topology_sha256)&&RH(v,window_sha256));
  uint8_t hash[32];RN(dw_route_source_key_hash(&v->witness.source,hash)&&!memcmp(hash,v->owner.source_key_sha256.bytes,32)&&!memcmp(v->extent.window_sha256.bytes,v->window_sha256.bytes,32)&&v->extent.vehicle_sequence==v->witness.vehicle_sequence);
  RN((v->task_space_kind==2)==RH(v,exit_space_sha256));
 RE
 RB(RouteEligibilityRef) RN(RI(v,eligibility_id)&&RH(v,body_sha256)&&RP(v,ready_revision)&&RP(v,valid_until_pi_us)&&(v->purpose==1||v->purpose==2)&&v->reason<=24&&(v->task_space_kind==1||v->task_space_kind==2)&&(v->eligible==(v->reason==0)));
 RE
 RB(RouteQuery) RN(RI(v,query_id)&&RI(v,operator_request_nonce)&&rt_purpose(v->purpose,v->goal_sha256.bytes)&&RP(v,issued_pi_us)&&RP(v,valid_until_pi_us)&&v->issued_pi_us<v->valid_until_pi_us&&RI(&v->owner.operation_binding,departure_nonce));
 RE
 RB(RouteCandidate)
  RN(RI(v,request_nonce)&&RI(v,query_id)&&RI(v,phase_state_message_id)&&RH(v,query_sha256)&&RP(v,request_sequence)&&RP(v,plan_revision)&&RP(v,calculation_sequence)&&RP(v,basis_window_revision)&&v->action>=1&&v->action<=3&&(v->task_space_kind==1||v->task_space_kind==2)&&RH(v,current_window_sha256)&&RH(v,basis_window_sha256)&&rt_pair(v->expected_registry_epoch.bytes,v->expected_registry_revision));
  RN(v->progress.vehicle_sequence==v->witness.vehicle_sequence&&v->progress.calculation_sequence==v->calculation_sequence);
  uint8_t hash[32];RN(dw_route_source_key_hash(&v->witness.source,hash)&&!memcmp(hash,v->owner.source_key_sha256.bytes,32)&&v->extent.vehicle_sequence==v->witness.vehicle_sequence&&!memcmp(v->extent.window_sha256.bytes,v->current_window_sha256.bytes,32));
  if(v->action==1)RN(RP(v,began_guest_us)&&RP(v,phase_started_guest_us)&&v->began_guest_us<=v->phase_started_guest_us&&v->phase_started_guest_us<=v->witness.checked_guest_us&&!v->core.started_pi_us&&!v->core.phase_started_pi_us&&!v->progress.used_travel_ticks&&!v->progress.last_progress_pi_us);
  else RN(!v->began_guest_us&&v->core.started_pi_us>0);
  if(v->action==3)RN(v->core.terminal_result&&!v->phase_started_guest_us);else RN(!v->core.terminal_result);
  if(v->task_space_kind==2)RN(RH(v,exit_space_sha256)&&v->extent.mode==2);else RN(!RH(v,exit_space_sha256));
 RE
 RB(RouteReceipt)
  RN(RI(v,request_nonce)&&RI(v,query_id)&&RH(v,request_sha256)&&RH(v,query_sha256)&&RP(v,request_sequence)&&v->result>=1&&v->result<=5&&v->reason<=24);
  if(v->result==1){RN(v->has_snapshot&&v->has_initial_target_context);const xenova_drive_RouteAcceptanceRef *a=&v->snapshot.acceptance;
   RN(!memcmp(a->request_nonce.bytes,v->request_nonce.bytes,16)&&!memcmp(a->request_sha256.bytes,v->request_sha256.bytes,32)&&a->request_sequence==v->request_sequence);}
  else RN(!v->has_initial_target_context);
 RE
 RB(RouteStatus)
  RN(RP(v,valid_until_pi_us)&&(v->route_state==0||v->route_state==2||v->route_state==3)&&v->commit_state<=3&&v->reason<=24&&v->terminal_result<=3&&v->eligibility_count<=2);
  if(v->eligibility_count==2)RN(v->eligibility[0].purpose!=v->eligibility[1].purpose);
  if(!v->route_state)RN(!RI(v,registry_epoch)&&!RI(v,route_id)&&!v->registry_revision&&!RH(v,accepted_core_sha256)&&!RH(v,global_plan_sha256)&&!RH(v,local_path_sha256)&&!v->commit_state&&!v->terminal_result);
  else RN(RI(v,registry_epoch)&&RI(v,route_id)&&RP(v,registry_revision)&&RH(v,accepted_core_sha256)&&RH(v,global_plan_sha256)&&RH(v,local_path_sha256)&&RH(v,current_window_sha256)&&((v->route_state==3)==(v->terminal_result!=0)));
  if(v->commit_state>=2)RN(RI(v,state_message_id)&&RH(v,state_sha256)&&RH(v,current_permission_sha256)&&RP(v,state_revision)&&RP(v,plan_revision)&&RP(v,phase_revision)&&RP(v,calculation_hwm)&&RP(v,vehicle_hwm));
 RE
 RB(MapperWorldWitness) RN(RH(v,world_raw_sha256)&&operation_uuid(v->mapper.mapper_boot.bytes,v->source.mapper_boot)&&operation_uuid(v->mapper.mapper_instance.bytes,v->source.mapper_instance)&&operation_uuid(v->mapper.clock_epoch.bytes,v->source.clock_epoch)&&v->mapper.observation_sequence==v->source.observation_sequence);
 RE
 RB(WindowTransitionRequest)
  RN(RI(v,request_nonce)&&RI(v,query_id)&&RI(v,route_id)&&RI(v,expected_registry_epoch)&&RI(v,transition_id)&&RH(v,query_sha256)&&RH(v,from_window_sha256)&&RH(v,to_window_sha256)&&RH(v,from_world_sha256)&&RP(v,expected_registry_revision)&&RP(v,expected_source_binding_revision)&&RP(v,phase_revision)&&v->action>=1&&v->action<=5&&memcmp(v->from_window_sha256.bytes,v->to_window_sha256.bytes,32));
  uint8_t hash[32];RN(dw_route_source_key_hash(&v->witness.source,hash)&&!memcmp(hash,v->owner.source_key_sha256.bytes,32));if(v->action==2)RN(RH(v,candidate_local_path_sha256));
 RE
 RB(WindowTransitionReceipt)
  RN(RI(v,request_nonce)&&RI(v,transition_id)&&RH(v,request_sha256)&&RH(v,from_window_sha256)&&RH(v,to_window_sha256)&&RP(v,source_binding_revision)&&RP(v,transition_generation)&&RP(v,valid_until_pi_us)&&v->state<=5&&v->reason<=24);
  if(v->state==2||v->state==3)RN(RH(v,activated_world_sha256)&&RP(v,activated_world_sequence));else RN(!RH(v,activated_world_sha256)&&!v->activated_world_sequence);
 RE
 RB(RoadDataRequest) RN(RI(v,request_nonce)&&RH(v,record_sha256)&&RP(v,valid_until_pi_us)&&rt_record_desc(v->record_kind));
 RE
 RB(RoadRegistrationRequest)
  RN(RI(v,request_nonce)&&RH(v,world_raw_sha256)&&RP(v,issued_pi_us)&&RP(v,valid_until_pi_us)&&v->issued_pi_us<v->valid_until_pi_us&&((v->expected_goal_revision==0)==!RH(v,expected_goal_sha256)));
  RN(v->facts.meter.vehicle_sequence==v->vehicle.sequence&&v->facts.has_stop&&v->facts.stop.state==3&&v->valid_until_pi_us<=v->facts.valid_until_pi_us&&dw_control_facts_validate(&v->facts,&v->vehicle)==DW_OK);
 RE
 RB(RoadData)
  const void *record=NULL;uint32_t kind=0;
  switch(v->which_payload){case 10:record=&v->payload.start_goal;kind=1;break;case 11:record=&v->payload.road_topology;kind=2;break;
   case 12:record=&v->payload.road_section;kind=3;break;case 13:record=&v->payload.road_connection;kind=4;break;
   case 14:record=&v->payload.road_window;kind=5;break;case 15:record=&v->payload.exit_space;kind=6;break;
   case 16:record=&v->payload.route_plan;kind=7;break;case 17:record=&v->payload.road_geometry_scope;kind=8;break;default:return DW_ARRAY;}
  uint8_t hash[32];RN(dw_route_record_hash(kind,record,hash)&&!memcmp(hash,v->record_sha256.bytes,32));
  if(v->publisher_kind==1)RN(v->has_mapper&&!v->has_guest_owner&&!v->has_guest_witness&&v->has_durable);
  else if(v->publisher_kind==2){RN(!v->has_mapper&&v->has_guest_owner&&v->has_guest_witness&&kind==7);
   RN(dw_route_source_key_hash(&v->guest_witness.source,hash)&&!memcmp(hash,v->guest_owner.source_key_sha256.bytes,32));
   RN(dw_route_witness_hash(&v->guest_witness,hash)&&!memcmp(hash,v->payload.route_plan.origin_guest_witness_sha256.bytes,32));}
  else return DW_VERSION;
  RN(!v->has_durable||!memcmp(v->durable.record_sha256.bytes,v->record_sha256.bytes,32));
 RE
 RB(RouteExchange) RN(v->which_payload>=2&&v->which_payload<=10);
 RE
 return DW_ARRAY;
}
#undef RB
#undef RE
#undef RI
#undef RH
#undef RP
#undef RN


enum dw_result dw_route_world_validate(const dw_world *w){
 if(!w)return DW_ARRAY;
 if(((w->required_features&DW_ROUTE_VIEW)!=0)!=w->has_route_view)return DW_VERSION;
 if(!w->has_route_view)return DW_OK;
 if(w->required_features!=27&&w->required_features!=31)return DW_VERSION;
 return dw_route_validate(&w->route_view,xenova_drive_WorldRouteView_fields);
}
enum dw_result dw_route_mission_validate(const xenova_drive_MissionExecution *m){
 if(!m||m->has_route_binding!=(m->kind==3))return DW_SOURCE;
 if(m->kind!=3)return DW_OK;
 if(!m->has_operation_binding||m->phase<1||m->phase>6)return DW_SOURCE;
 return dw_route_validate(&m->route_binding,xenova_drive_RouteBinding_fields);
}
enum dw_result dw_route_grant_validate(const xenova_drive_MissionGrant *m){
 if(!m||m->has_route_binding!=(m->kind==3))return DW_SOURCE;
 if(m->kind!=3)return DW_OK;
 if(!m->has_operation_binding)return DW_SOURCE;
 return dw_route_validate(&m->route_binding,xenova_drive_RouteBinding_fields);
}
enum dw_result dw_route_state_validate(const dw_state *s){
 if(!s||((s->state_features&DW_ROUTE_STATE)!=0)!=s->has_route)return DW_VERSION;
 if(!s->has_route)return s->has_mission&&s->mission.kind==3?DW_SOURCE:DW_OK;
 enum dw_result rc=dw_route_validate(&s->route,xenova_drive_RouteSnapshot_fields);if(rc)return rc;
 const xenova_drive_RouteSnapshot *r=&s->route;const xenova_drive_RouteCore *c=&r->core;
 if(r->state==3&&s->has_mission&&s->mission.kind!=3)return DW_OK; /* historical proof, not a motion lease */
 if(!s->has_mission||s->mission.kind!=3||!s->mission.has_route_binding||!(s->world_features&DW_ROUTE_VIEW))return DW_SOURCE;
 const xenova_drive_RouteBinding *binding=&s->mission.route_binding;
 if(memcmp(binding->route_id.bytes,c->route_id.bytes,16)||binding->revision!=c->revision||binding->purpose!=c->purpose||
  memcmp(binding->core_sha256.bytes,r->acceptance.accepted_core_sha256.bytes,32)||memcmp(binding->global_plan_sha256.bytes,c->global_plan_sha256.bytes,32)||
  memcmp(binding->goal_sha256.bytes,c->goal_sha256.bytes,32)||s->mission.phase!=c->phase||s->calculation_sequence!=r->progress.calculation_sequence||s->vehicle_sequence!=r->progress.vehicle_sequence)return DW_SOURCE;
 if(r->task_space_kind==1)return s->has_task_reference?DW_OK:DW_SOURCE;
 return !s->has_task_reference&&(!s->has_episode||(s->episode.core.state==1&&s->episode.core.phase==5))?DW_OK:DW_SOURCE;
}
enum dw_result dw_route_clock_validate(const xenova_drive_ClockReply *r){
 if(!r||!r->has_route_status)return DW_OK;
 enum dw_result rc=dw_route_validate(&r->route_status,xenova_drive_RouteStatus_fields);if(rc)return rc;
 const xenova_drive_RouteStatus *s=&r->route_status;
 if(!r->probe.has_supported_state_features||!(r->probe.supported_state_features&DW_ROUTE_STATE)||!r->has_guest_clock_valid_until_pi_us||s->valid_until_pi_us>r->guest_clock_valid_until_pi_us)return DW_SOURCE;
 if(rt_sha(s->current_permission_sha256.bytes)){
  uint8_t hash[32];if(!r->has_current_mission||!dw_route_permission_hash(&r->current_mission,hash)||memcmp(hash,s->current_permission_sha256.bytes,32))return DW_SOURCE;}
 return DW_OK;
}
