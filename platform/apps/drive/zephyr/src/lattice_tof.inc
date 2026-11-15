/* SPDX-License-Identifier: Apache-2.0 */
/* Planner work is admitted only while stopped. The control worker never waits
 * for it, and a busy planning turn preserves the already submitted STOP. */
static const struct lt_limits tof_limits={.max_retreat_m=.43f,.reverse_speed_mps=.08f,
 .forward_speed_mps=.13f,.front_release_m=.18f,.stop_m=.15f,.sensor_half_angle_rad=.21f};

static enum de_result tof_inputs(uint64_t now,struct dw_handle *handle,const struct dw_snapshot **map,
 struct de_input_check *input,struct lp_budget *budget){
 enum de_result rc=de_input_current(&engine,now,input);
 if(rc)return rc;
 enum dw_result result=dw_acquire(&engine.maps,handle,map);
 if(result)return result==DW_BUSY?DE_BUSY:DE_MAP;
 if(handle->generation!=input->map_generation){(void)dw_release(&engine.maps,*handle);return DE_CHANGED;}
 budget->collision_index=&engine.map_indices[handle->slot];
 return DE_OK;
}

static bool tof_command_current(const struct xm_drive_command2 *command){
 if(k_mutex_lock(&tof_lock,K_NO_WAIT))return false;
 bool valid=atomic_load(&tof_active)&&engine.local_owner&&command->generation==tof.generation&&
  command->plan_revision==tof.plan_revision&&command->phase_revision==tof.phase_revision&&
  uuid_same(tof.mission,command->mission_id)&&command->mission_revision==tof.mission_revision&&
  command->grant_revision==tof.grant_revision;
 if(valid&&command->v_mps)valid=command->v_mps<0?tof.stage==LT_AUTO_REVERSE:tof.stage==LT_AUTO_FORWARD;
 if(valid&&!command->v_mps)valid=!strcmp(command->command_kind,"HOLD_STEER")?
  tof.stage==LT_AUTO_BRAKE||tof.stage==LT_AUTO_ALIGN:tof.stage==LT_AUTO_REVERSE||tof.stage==LT_AUTO_FORWARD;
 k_mutex_unlock(&tof_lock);
 return valid;
}

static bool tof_automatic(const xenova_drive_MissionGrant *mission,const xenova_drive_DriveControlFacts *facts,
 const struct dl_clock *clock,uint64_t now,uint64_t deadline,struct drive_sample *out){
 bool front=facts&&facts->has_proximity&&facts->proximity.front_blocked;
 if(!front&&!atomic_load(&tof_active))return false;
 if(mission->kind!=1)return false;
 if(k_mutex_lock(&tof_lock,K_NO_WAIT)){out->tof_busy=atomic_load(&tof_active);return true;}
 if(tof.stage!=LT_AUTO_IDLE&&!lt_auto_same(&tof,mission)){
  tof.stage=LT_AUTO_IDLE;atomic_store(&tof_active,false);
 }
 if(tof.stage==LT_AUTO_IDLE){
  if(!front){k_mutex_unlock(&tof_lock);return false;}
  struct de_reference handle;const struct lp_path *path;
  if(de_path_acquire(&engine,&handle,&path)){out->reason="TOF_PATH_UNAVAILABLE";k_mutex_unlock(&tof_lock);return true;}
  if(lp_startup_straight(path,engine.config)){
   /* The startup tracker holds stopped on this original ToF report. */
   (void)de_path_release(&engine,handle);k_mutex_unlock(&tof_lock);return false;
  }
  /* The interrupted committed path supplies the existing mission/phase
   * identity for both original direction gates. */
  bool eligible=engine.local_owner&&engine.episode_handoff==DE_EPISODE_IDLE&&
   lp_mission_matches(path,mission)&&path->revision&&path->maneuver_revision&&
   !path->has_route&&(!path->has_episode||path->episode.core.state==1||path->episode.core.phase==DB_FINISHED);
  if(eligible){lt_auto_begin(&tof,mission,path,now,now+lp_selected_config.avoid_episode_us);atomic_store(&tof_active,true);}
  (void)de_path_release(&engine,handle);
  if(!eligible){out->reason="TOF_STOPPED";k_mutex_unlock(&tof_lock);return true;}
 }
 if(!engine.local_owner){out->reason="TOF_OWNER_CHANGED";k_mutex_unlock(&tof_lock);return true;}
 if(now>=tof.expires_pi_us)tof.stage=LT_AUTO_HELD;
 struct de_input_check input;struct dw_handle handle;const struct dw_snapshot *map;
 struct lp_budget budget={deadline,clock_us,tracker_cancelled,NULL,NULL};
 enum de_result rc=tof_inputs(now,&handle,&map,&input,&budget);
 if(rc){out->reason=de_reason(rc);k_mutex_unlock(&tof_lock);return true;}
 if(tof.stage==LT_AUTO_BRAKE&&facts&&facts->has_proximity&&facts->proximity.has_front_range_mm&&!tof.hit_valid){
  dw_vehicle original;
  if(!dw_vehicle_sequence(&engine.odometry,facts->proximity.front_vehicle_sequence,&original))lt_auto_observe(&tof,facts,&original);
 }
 xenova_drive_DirectionGateWitness gate;bool have_gate=!dl_direction_get(false,&gate,now);
 if(tof.stage==LT_AUTO_BRAKE&&facts&&facts->has_proximity&&!facts->proximity.rear_blocked&&
    facts->proximity.has_rear_range_mm&&now<facts->proximity.rear_valid_until_pi_us&&
    lt_auto_ready(&tof,have_gate?&gate:NULL,now,LT_STOP_REVERSE,-1))tof.stage=LT_AUTO_REVERSE;
 if(tof.stage==LT_AUTO_ALIGN&&facts&&facts->has_proximity&&!facts->proximity.front_blocked&&
    lt_auto_ready(&tof,have_gate?&gate:NULL,now,LT_DRIVE_REVERSE,1))tof.stage=LT_AUTO_FORWARD;
 struct lp_command target={0};
 if(tof.stage==LT_AUTO_REVERSE||tof.stage==LT_AUTO_FORWARD){
  enum lp_result tracked=lt_auto_track(&tof,&map->world,input.pose,&input.vehicle,facts,now,&target,&budget);
  if(tracked!=LP_PATH_FOUND&&tracked!=LP_STOP){tof.stage=LT_AUTO_HELD;out->reason=lp_reason(tracked);}
  /* A zero speed inside a checked segment can be the tracker braking before
   * goal capture. Hold the present steering with a zero DRIVE at that instant
   * so it can finish without replacing the original direction STOP pin. */
  if(tracked==LP_PATH_FOUND&&!target.velocity&&(tof.stage==LT_AUTO_REVERSE||tof.stage==LT_AUTO_FORWARD))
   out->tof_coast=true;
 }
 uint64_t bound=input.valid_until_pi_us;
 if(facts&&bound>facts->valid_until_pi_us)bound=facts->valid_until_pi_us;
 if(tof.stage==LT_AUTO_REVERSE&&facts&&facts->has_proximity&&facts->proximity.has_rear_range_mm&&
    bound>facts->proximity.rear_valid_until_pi_us)bound=facts->proximity.rear_valid_until_pi_us;
 if(bound>tof.expires_pi_us)bound=tof.expires_pi_us;
 if(dw_source_valid(&engine.maps,handle))out->reason="TOF_MAP_CHANGED";
 else if(!command_lease(out,clock,&input.vehicle,now,bound))out->reason="TOF_INPUT_EXPIRED";
 (void)dw_release(&engine.maps,handle);
 struct xm_drive_command2 *body=&out->command.command;
 uuid_text(body->mission_id,mission->mission_id.bytes);body->mission_revision=mission->mission_revision;body->grant_revision=mission->grant_revision;
 body->plan_revision=tof.plan_revision;body->phase_revision=tof.phase_revision;body->world_sequence=input.source.observation_sequence;
 body->segment_index=body->cusp_index=tof.stage<=LT_AUTO_BRAKE?LT_STOP_REVERSE:
  tof.stage<=LT_AUTO_ALIGN?LT_DRIVE_REVERSE:LT_DRIVE_FORWARD;
 out->input_sequence=(int64_t)input.vehicle.sequence;
 out->input_received_ms=((int64_t)input.vehicle.pi_sample_us-input.vehicle.clock_error_us-clock->upper)/1000;
 out->tof_recovery=true;out->v_mps=target.velocity;out->steer_rad=target.velocity||out->tof_coast?target.steer:0;
 if(!out->reason&&(tof.stage==LT_AUTO_BRAKE||tof.stage==LT_AUTO_ALIGN)){
  bool reverse=tof.stage==LT_AUTO_BRAKE;
  out->gate_required=true;out->requested_direction=reverse?-1:1;
  body->stop_reason=3;
  if(tof.stop_sequence&&have_gate&&lt_auto_stop(&tof,facts,&gate,now,reverse?LT_STOP_REVERSE:LT_DRIVE_REVERSE,reverse?-1:1)&&
     gate.gate.stop_command_sequence==tof.stop_sequence&&gate.gate.gate_revision==tof.gate_revision){
   out->hold_steer=true;
   out->steer_rad=(reverse?tof.plan.reverse.points[0]:tof.plan.forward.points[0]).steering_rad;
  }
 }
 k_mutex_unlock(&tof_lock);
 return true;
}

static bool tof_planner(uint64_t now,uint64_t guest){
 if(!atomic_load(&tof_active))return false;
 if(k_mutex_lock(&tof_lock,K_NO_WAIT))return true;
 if(!engine.has_grant||!lt_auto_same(&tof,&engine.grant)||!engine.local_owner){
  tof.stage=LT_AUTO_IDLE;atomic_store(&tof_active,false);k_mutex_unlock(&tof_lock);return false;
 }
 if(now>=tof.expires_pi_us)tof.stage=LT_AUTO_HELD;
 if(tof.stage==LT_AUTO_RETURN){
  struct de_reference handle;const struct lp_path *path;
  if(!de_path_acquire(&engine,&handle,&path)){
   if(path->task.active&&!path->task.changing&&path->task.selected_lane_id==tof.plan.forward.task.selected_lane_id&&
      path->revision!=tof.plan_revision&&!k_mutex_lock(&app_metadata,K_NO_WAIT)){
    if(prepared_valid&&prepared.generation==handle.generation){tof.stage=LT_AUTO_IDLE;atomic_store(&tof_active,false);}
    k_mutex_unlock(&app_metadata);
   }
   (void)de_path_release(&engine,handle);
  }
  k_mutex_unlock(&tof_lock);return false;
 }
 if(tof.stage==LT_AUTO_HANDOFF&&!engine.planning&&!engine.apply_pending){
  uint32_t lane=tof.plan.forward.task.selected_lane_id;
  engine.task=(struct tr_intent){true,false,lane,lane,lane,0};
  engine.path_invalidated=true;
  if(engine.requested<UINT32_MAX)atomic_fetch_add(&engine.requested,1);
  tof.stage=LT_AUTO_RETURN;k_mutex_unlock(&tof_lock);return false;
 }
 if(engine.planning){
  /* Drain the interrupted search before reusing its scratch. */
  (void)de_plan_slice(&engine,4,now);
  k_mutex_unlock(&tof_lock);return true;
 }
 if(engine.apply_pending){(void)de_apply_finish(&engine,now);k_mutex_unlock(&tof_lock);return true;}
 if((tof.stage==LT_AUTO_BRAKE||tof.stage==LT_AUTO_ALIGN)&&!tof.stop_sequence&&
    !dl_control_facts(&planner_facts,now)){
  xenova_drive_DirectionGateWitness gate;
  bool reverse=tof.stage==LT_AUTO_BRAKE;
  if(!dl_direction_get(false,&gate,now)&&lt_auto_stop(&tof,&planner_facts,&gate,now,
     reverse?LT_STOP_REVERSE:LT_DRIVE_REVERSE,reverse?-1:1)){
   struct lp_budget budget={guest+lp_selected_config.planner_budget_us,clock_us,cancelled,NULL,NULL};
   struct de_input_check input;struct dw_handle handle;const struct dw_snapshot *map;
   if(!tof_inputs(now,&handle,&map,&input,&budget)){
    if(tof.attempted_map_generation!=handle.generation||tof.attempted_stop_sequence!=gate.gate.stop_command_sequence){
     enum lp_result planned=lt_auto_plan(&tof,&engine.search,&map->world,input.pose,&input.vehicle,&planner_facts,&gate,now,
      &lp_selected_config,&tof_limits,budget);
     /* Missing input can arrive before the next World. A budget miss needs
      * a new map or STOP before spending another full search budget. */
     if(planned!=LP_SEARCHING){tof.attempted_map_generation=handle.generation;
      tof.attempted_stop_sequence=gate.gate.stop_command_sequence;}
     if(planned==LP_COLLISION||planned==LP_NOT_FOUND)tof.stage=LT_AUTO_HELD;
     if(dw_source_valid(&engine.maps,handle))tof.stop_sequence=tof.gate_revision=0;
    }
    (void)dw_release(&engine.maps,handle);
   }
  }
 }
 planner_completed();
 k_mutex_unlock(&tof_lock);
 return true;
}
