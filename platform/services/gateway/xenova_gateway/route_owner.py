"""명시 출차/시작점 복귀의 원 Query·예산·등록·실제 State/apply owner.

기존 actor에서만 호출한다. 새 thread/DB/동기 fsync는 없다. B09 운영 병합 전에는
기존 operator/drive_link에 자동 부착하지 않는다. current_owner/stop_auto/terminal_check는
기존 실제 Authority/physical/geometry owner를 연결할 필수 유한 포트이며 wire 입력이 아니다.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID,uuid4

from xenova_drive_world import schema,encode,decode,operation_binding_equal
from xenova_drive_world.codec import STATE_KNOWN_FEATURES
from xenova_drive_world.route import (RouteReason,route_query_sha256,route_candidate_sha256,
    route_core_sha256,route_owner_sha256,route_source_key_sha256,route_eligibility_sha256,
    route_permission_sha256,route_stop_facts_sha256,route_local_path_sha256)
from .route_policy import RouteBudgetPolicy,RouteError,require,advance,NIL,ZERO
from .absolute_meter import upper_distance


def copied(value):
    result=type(value)();result.CopyFrom(value);return result


def peer_key(owner):return tuple(str(UUID(bytes=getattr(owner,name))) for name in ('vm','execution','app_instance'))


@dataclass(frozen=True)
class RouteRegistration:
    """기존 async Authority job에 전달할 준비 결과. 아직 receipt/현재 grant/출력 허가가 아니다."""
    candidate_raw: bytes
    candidate_sha256: bytes
    query_raw: bytes
    snapshot_raw: bytes
    route_binding_raw: bytes


class RouteOwner:
    def __init__(self,operator,policy,data,*,current_owner,installed,mapper_support,stop_auto,terminal_check,progress_check):
        require(type(policy) is RouteBudgetPolicy,RouteReason.UNSUPPORTED_PROFILE)
        self.o=operator;self.r=operator.runner;self.policy=policy;self.data=data
        self.current_owner=current_owner;self.installed=installed;self.mapper_support=mapper_support
        self.stop_auto=stop_auto;self.terminal_check=terminal_check;self.progress_check=progress_check
        self.epoch=NIL;self.revision=0;self.query=None;self.pending=None;self.registry=None
        self.last_reply=None;self.terminal=None;self.eligibility={};self.ready_revision={1:1,2:1}
        self.semantic={};self.request_hwm=OrderedDict();self.reason=RouteReason.READY;self.failed=False

    @property
    def now(self):return self.o.now

    def supported(self,peer):
        features=self.o.episodes.peers.get(tuple(peer),0)
        return bool(self.installed() and self.mapper_support()&2 and features&64
            and not features&~STATE_KNOWN_FEATURES)

    def _current(self,owner,peer):
        actual=self.current_owner()
        require(actual is not None and peer_key(owner)==tuple(peer) and peer_key(actual)==tuple(peer),
            RouteReason.SOURCE_UNAVAILABLE)
        require(route_owner_sha256(owner)==route_owner_sha256(actual),RouteReason.REQUEST_CONTEXT_CHANGED)
        require(self.supported(peer),RouteReason.CAPABILITY_LOST)
        return actual

    def _witness(self,owner,witness,peer):
        self._current(owner,peer);now=self.now
        require(now<witness.valid_until_pi_us,RouteReason.CLOCK_OR_EXPIRY)
        lease=self.o.clocks.lease(tuple(peer),witness.clock_probe_sequence,now,owner.control_generation)
        require(lease is not None and lease.digest==witness.clock_reply_sha256,RouteReason.CLOCK_OR_EXPIRY)
        interval=self.o.clocks.interval(tuple(peer),witness.clock_probe_sequence,witness.checked_guest_us,now,owner.control_generation)
        require(interval is not None and witness.checked_guest_us>=lease.p4,RouteReason.CLOCK_OR_EXPIRY)
        provider=self.r.drive_link;trust=provider.trust;raw=provider.vehicle_history.get(witness.vehicle_sequence)
        require(trust is not None and raw is not None and sha256(raw).digest()==witness.vehicle_raw_sha256,
            RouteReason.SOURCE_UNAVAILABLE)
        vehicle=decode(raw,schema.VehicleSample);current=self.r.device_view().get('vehicle_sample')
        require(current is not None and vehicle.quality==3 and vehicle.host_boot==self.r.authority.host
            and vehicle.gateway_instance==self.o.instance and all(getattr(vehicle,k)==current.get(k)
                for k in ('pi_session','device_session','odom_epoch','calibration')),RouteReason.SOURCE_UNAVAILABLE)
        source=witness.source
        require(owner.source_key_sha256==route_source_key_sha256(source) and source.odom_epoch==vehicle.odom_epoch
            and all(getattr(source,k)==getattr(trust,k) for k in
                ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','clock_epoch','configuration','calibration'))
            and source.primitive==bytes.fromhex(trust.primitive),RouteReason.SOURCE_UNAVAILABLE)
        world=self.data.world(witness.world_raw_sha256,now)
        require(encode(world.source)==encode(source),RouteReason.HASH_OR_BODY_MISMATCH)
        scope=self.data.get(8,owner.geometry_scope_sha256).message()
        require(scope.map_epoch==UUID(source.map_epoch).bytes and scope.frame_epoch==UUID(source.frame_epoch).bytes
            and scope.origin_odom_epoch==UUID(source.odom_epoch).bytes
            and scope.origin_pi_host_boot==UUID(self.r.authority.host).bytes
            and all(getattr(scope,k)==getattr(source,k) for k in ('vehicle','configuration','calibration','primitive')),
            RouteReason.FRAME_OR_DATASET_CHANGED)
        meter=self.o.episodes._meter(witness.vehicle_sequence)
        require(meter is not None,RouteReason.METER_GAP)
        maximum=self.policy.max_unobserved_gap_us
        end=min(interval[0]+100000,interval[2],trust.expires_pi_us,world.mapper.valid_until_pi_us,
            vehicle.pi_sample_us-vehicle.clock_error_us+maximum)
        scene=self.r.scene_runtime.motion_grant(now) if self.r.scene_runtime is not None else None
        if owner.scene_admission_id!=NIL:
            require(scene is not None and scene.scene.admission_id==owner.scene_admission_id
                and scene.scene.admission_revision==owner.scene_revision,RouteReason.SPACE_UNKNOWN)
            end=min(end,scene.valid_until_pi_us)
        require(now<witness.valid_until_pi_us<=end,RouteReason.CLOCK_OR_EXPIRY)
        return interval,meter,world

    def _extent(self,extent,task_space,window,exit_space,vehicle_sequence,*,full):
        require(extent.window_sha256==window and extent.exit_space_sha256==exit_space
            and extent.vehicle_sequence==vehicle_sequence,RouteReason.HASH_OR_BODY_MISMATCH)
        require(extent.available_forward_m>=extent.required_forward_m
            and extent.available_backward_m>=extent.required_backward_m,RouteReason.WINDOW_INSUFFICIENT)
        if full:
            if task_space==2:
                require(extent.mode==2 and extent.cusp_sweep_sha256!=ZERO,RouteReason.SPACE_UNKNOWN)
            else:
                require(extent.mode in (1,3),RouteReason.SPACE_UNKNOWN)
                episode=self.o.episodes.registry
                if episode is not None and episode['snapshot'].core.state==2:
                    require(extent.mode==3 and extent.s19_sweep_sha256!=ZERO,RouteReason.ACTIVE_EPISODE)
        else:require(extent.mode==1,RouteReason.SPACE_UNKNOWN)

    def _original_facts(self,value,peer):
        original=self.o.clocks.sent.get((*peer,value.witness.clock_probe_sequence))
        require(original is not None and sha256(original.raw).digest()==value.witness.clock_reply_sha256,
            RouteReason.CLOCK_OR_EXPIRY)
        reply=decode(original.raw,schema.DriveLink).clock
        require(reply.HasField('control_facts'),RouteReason.STOP_REQUIRED)
        facts=reply.control_facts;current=self.r.control_facts()
        require(value.control_facts_sha256==route_stop_facts_sha256(facts)
            and facts.HasField('stop') and facts.stop.state==3 and current is not None
            and current.HasField('stop') and current.stop.state==3
            and self.now<min(facts.valid_until_pi_us,facts.stop.valid_until_pi_us,
                current.valid_until_pi_us,current.stop.valid_until_pi_us),RouteReason.STOP_REQUIRED)
        require(all(getattr(facts.stop,k)==getattr(current.stop,k) for k in
            ('pi_session','device_session','command_sequence','control_generation','producer_role',
             'source_vm','source_execution','source_app_instance','mission_id','mission_revision','grant_revision',
             'plan_revision','phase_revision')),RouteReason.STOP_REQUIRED)
        return facts

    def observe_eligibility(self,raw,peer):
        value=decode(raw,schema.RouteEligibility)
        self._witness(value.owner,value.witness,peer)
        self._extent(value.extent,value.task_space_kind,value.window_sha256,value.exit_space_sha256,
            value.witness.vehicle_sequence,full=False)
        purpose=value.purpose;require(purpose in (1,2),RouteReason.REQUEST_CONTEXT_CHANGED)
        if value.eligible:
            self.data.get(2,value.topology_sha256);self.data.get(5,value.window_sha256)
            if purpose==2:self.data.get(1,value.goal_sha256)
            if value.task_space_kind==2:self.data.get(6,value.exit_space_sha256)
        # Raw report/normal sequence is deliberately outside the semantic key.
        semantic=(route_owner_sha256(value.owner),value.task_space_kind,value.goal_sha256,
            value.exit_space_sha256,value.topology_sha256,value.eligible,value.reason)
        if purpose in self.semantic and self.semantic[purpose]!=semantic:self.ready_revision[purpose]=advance(self.ready_revision[purpose])
        self.semantic[purpose]=semantic
        self.eligibility[purpose]=(bytes(raw),route_eligibility_sha256(raw))
        return self.eligibility_ref(purpose)

    def eligibility_ref(self,purpose):
        row=self.eligibility.get(purpose)
        if row is None:return None
        value=decode(row[0],schema.RouteEligibility)
        return schema.RouteEligibilityRef(eligibility_id=value.eligibility_id,body_sha256=row[1],purpose=purpose,
            ready_revision=self.ready_revision[purpose],valid_until_pi_us=value.witness.valid_until_pi_us,
            reason=value.reason,task_space_kind=value.task_space_kind,eligible=value.eligible)

    def begin_query(self,raw,operator_row):
        """기존 actor의 durable 출발 수락 뒤만 호출한다. Query로 mode/grant를 발급하지 않는다."""
        query=decode(raw,schema.RouteQuery);call=operator_row['request'];key=operator_row['key']
        require(self.pending is None and (self.query is None or self.failed or self.registry is not None and self.registry['snapshot'].state==3)
            and (self.registry is None or self.failed or self.registry['snapshot'].state==3),
            RouteReason.POOL_BUSY)
        require(self.query is None or self.query['key']!=key,RouteReason.REQUEST_CONTEXT_CHANGED)
        require(self.o._known(key)==operator_row and not operator_row['terminal']
            and any(row['key']==key for row in self.o._rows()),RouteReason.DURABLE_NOT_READY)
        require(call['action'] in ('drive.depart','home.return')
            and query.operator_request_nonce==UUID(key.split(':')[-1]).bytes
            and query.issued_pi_us==int(key.split(':')[2])*1000
            and self.now<query.valid_until_pi_us<=call['deadline_pi_ms']*1000,RouteReason.REQUEST_CONTEXT_CHANGED)
        self._current(query.owner,peer_key(query.owner))
        operation=self.r.authority.value.get('current_operation')
        require(operation is not None and operation['phase']=='COUNTDOWN' and operation['departure']==key,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        binding=query.owner.operation_binding
        require(binding.service_epoch==UUID(self.o.instance).bytes==UUID(operation['service']).bytes
            and binding.departure_nonce==query.operator_request_nonce
            and binding.operation_revision==operation['revision']
            and binding.permission_revision==operation['permission_revision']
            and binding.parking_enabled==operation['parking_enabled'],RouteReason.REQUEST_CONTEXT_CHANGED)
        if query.purpose==2:require(not operation['parking_enabled'] and call['action']=='home.return',RouteReason.REQUEST_CONTEXT_CHANGED)
        report=self.eligibility.get(query.purpose)
        require(report is not None,RouteReason.SPACE_UNKNOWN)
        eligible=decode(report[0],schema.RouteEligibility)
        require(eligible.eligible and eligible.goal_sha256==query.goal_sha256 and self.now<eligible.witness.valid_until_pi_us
            and int(call['args']['ready_revision'])==self.ready_revision[query.purpose],RouteReason.SPACE_UNKNOWN)
        before=copied(eligible.owner);before.operation_binding.CopyFrom(query.owner.operation_binding)
        require(route_owner_sha256(before)==route_owner_sha256(query.owner)
            and eligible.owner.operation_binding.operation_revision==int(call['args']['expected_operation_revision'])
            and query.owner.operation_binding.operation_revision==eligible.owner.operation_binding.operation_revision+1
            and eligible.owner.operation_binding.permission_revision==query.owner.operation_binding.permission_revision,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        if self.registry is not None:
            require(self.query is not None and self.query['key']!=key,RouteReason.REQUEST_CONTEXT_CHANGED)
            self.terminal=deepcopy(self.registry);self.registry=None;self.epoch=NIL;self.revision=0
            self.data.pin((),owner='route')
        self.query=dict(raw=bytes(raw),body=copied(query),sha=route_query_sha256(raw),key=key)
        self.failed=False;self.reason=RouteReason.READY
        return bytes(raw)

    def _reply(self,value,digest,result,reason,snapshot=None,owner=None):
        reply=schema.RouteReceipt(major=1,request_nonce=value.request_nonce,request_sha256=digest,
            result=result,reason=int(reason),request_sequence=value.request_sequence,
            query_id=value.query_id,query_sha256=value.query_sha256)
        if snapshot is not None:reply.snapshot.CopyFrom(snapshot)
        if owner is not None:reply.initial_target_context.CopyFrom(owner)
        return encode(reply)

    def candidate(self,raw,peer):
        """원 retry 조회가 현재 context/기한 검산보다 먼저다. 미커밋 등록은 reply를 합성하지 않는다."""
        value=decode(raw,schema.RouteCandidate);digest=route_candidate_sha256(raw)
        key=(bytes(value.query_id),bytes(value.request_nonce))
        for saved in (self.registry,self.terminal,self.last_reply):
            if saved is not None and saved.get('key')==key:
                require(saved['digest']==digest,RouteReason.HASH_OR_BODY_MISMATCH)
                return saved.get('reply')
        if self.pending is not None:
            require(self.pending['key']==key and self.pending['digest']==digest,RouteReason.POOL_BUSY)
            return None
        try:
            self._prepare(value,bytes(raw),digest,key,tuple(peer))
        except RouteError as exc:
            reply=self._reply(value,digest,3 if exc.reason in (RouteReason.CLOCK_OR_EXPIRY,RouteReason.REQUEST_CONTEXT_CHANGED) else 2,exc.reason)
            self.last_reply=dict(key=key,digest=digest,reply=reply)
            return reply
        return None

    def _prepare(self,value,raw,digest,key,peer):
        require(not self.failed and self.query is not None,RouteReason.REQUEST_CONTEXT_CHANGED)
        query=self.query['body'];require(value.query_id==query.query_id
            and value.query_sha256==self.query['sha'],RouteReason.REQUEST_CONTEXT_CHANGED)
        if value.action==1:
            require(self.now<query.valid_until_pi_us,RouteReason.CLOCK_OR_EXPIRY)
            require(route_owner_sha256(value.owner)==route_owner_sha256(query.owner),RouteReason.REQUEST_CONTEXT_CHANGED)
        interval,meter,_=self._witness(value.owner,value.witness,peer)
        require(value.expected_registry_epoch==self.epoch and value.expected_registry_revision==self.revision,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        require(value.request_sequence>self.request_hwm.get(peer,0),RouteReason.REQUEST_CONTEXT_CHANGED)
        self.request_hwm[peer]=value.request_sequence
        while len(self.request_hwm)>4:self.request_hwm.popitem(last=False)
        core=copied(value.core);old=None if self.registry is None else self.registry['snapshot']
        require(core.purpose==query.purpose and core.goal_sha256==query.goal_sha256
            and core.geometry_scope_sha256==value.owner.geometry_scope_sha256
            and core.budget_profile_sha256==self.policy.registered_sha256
            and core.max_laps==self.policy.max_laps,RouteReason.HASH_OR_BODY_MISMATCH)
        plan_record=self.data.get(7,core.global_plan_sha256);plan=plan_record.message()
        published=self.data.world_snapshot(value.witness.world_raw_sha256,self.now)
        require(published.route_view.window_sha256==value.current_window_sha256
            and published.route_view.geometry_scope_sha256==core.geometry_scope_sha256,
            RouteReason.GEOMETRY_CONFLICT)
        require(plan.route_id==core.route_id and plan.purpose==core.purpose and plan.goal_sha256==core.goal_sha256
            and plan.topology_sha256==core.topology_sha256 and plan.geometry_scope_sha256==core.geometry_scope_sha256
            and len(plan.occurrences)<=self.policy.max_occurrences and plan.max_laps==self.policy.max_laps
            and plan.terminal_profile_sha256==self.policy.terminal_profile_sha256,RouteReason.GEOMETRY_CONFLICT)
        topology=self.data.get(2,core.topology_sha256).message()
        basis=self.data.get(5,value.basis_window_sha256).message();window=self.data.get(5,value.current_window_sha256).message()
        require(topology.geometry_scope_sha256==basis.geometry_scope_sha256==window.geometry_scope_sha256==core.geometry_scope_sha256
            and value.basis_window_revision==basis.revision,RouteReason.GEOMETRY_CONFLICT)
        if core.purpose==2:
            goal=self.data.get(1,core.goal_sha256).message()
            require(goal.geometry_scope_sha256==core.geometry_scope_sha256
                and goal.terminal_profile_sha256==self.policy.terminal_profile_sha256,RouteReason.TARGET_UNAVAILABLE)
        self._extent(value.extent,value.task_space_kind,value.current_window_sha256,value.exit_space_sha256,
            value.witness.vehicle_sequence,full=True)
        episode=self.o.episodes.registry
        if value.task_space_kind==2:
            from .route_episode import inactive_episode_history
            history=inactive_episode_history(self.o,value.witness.source,core.parking_attempt_id,core.parking_goal_sha256)
            require(core.predecessor_episode_sha256==history.accepted_core_sha256,RouteReason.HASH_OR_BODY_MISMATCH)
            space=self.data.get(6,value.exit_space_sha256).message()
            require(space.parking_attempt_id==core.parking_attempt_id and space.parking_goal_sha256==core.parking_goal_sha256,
                RouteReason.GEOMETRY_CONFLICT)
        if value.action==1:
            require(old is None and self.epoch==NIL and self.revision==0,RouteReason.POOL_BUSY)
            require(value.expected_route_revision==0 and core.revision==1 and core.route_id!=NIL
                and core.phase_revision==1 and core.phase==(1 if value.task_space_kind==2 else 3)
                and value.began_guest_us>0 and value.phase_started_guest_us>0,RouteReason.REQUEST_CONTEXT_CHANGED)
            begin=self.o.clocks.interval(peer,value.witness.clock_probe_sequence,value.began_guest_us,self.now,value.owner.control_generation)
            phase=self.o.clocks.interval(peer,value.witness.clock_probe_sequence,value.phase_started_guest_us,self.now,value.owner.control_generation)
            require(begin is not None and phase is not None and begin[0]<=phase[0]<=interval[1],RouteReason.CLOCK_OR_EXPIRY)
            birth=self._birth_meter(begin,meter)
            core.started_pi_us=begin[0];core.deadline_pi_us=begin[0]+self.policy.route_duration_us
            core.phase_started_pi_us=phase[0];core.phase_expires_pi_us=min(core.deadline_pi_us,phase[0]+self.policy.phase_cap(core.phase))
            core.meter_epoch=UUID(birth.meter_epoch).bytes;core.meter_baseline_total_ticks=birth.total_ticks
            core.meter_gap_revision=birth.gap_revision;core.metres_per_tick_upper=birth.metres_per_tick
            core.max_travel_m=self.policy.route_max_travel_m
        else:
            require(old is not None and old.state==2 and self.registry['state'] is not None
                and value.expected_route_revision==old.core.revision and core.revision==advance(old.core.revision),
                RouteReason.REQUEST_CONTEXT_CHANGED)
            immutable=('route_id','purpose','goal_sha256','topology_sha256','global_plan_sha256','geometry_scope_sha256',
                'started_pi_us','deadline_pi_us','meter_epoch','meter_baseline_total_ticks','max_travel_m',
                'parking_attempt_id','parking_goal_sha256','predecessor_episode_sha256','budget_profile_sha256',
                'max_laps','meter_gap_revision','metres_per_tick_upper')
            require(all(getattr(core,k)==getattr(old.core,k) for k in immutable) and value.began_guest_us==0,
                RouteReason.REQUEST_CONTEXT_CHANGED)
            if value.action==3:
                require(core.phase in (5,6) and core.phase_revision==old.core.phase_revision
                    and core.phase_started_pi_us==old.core.phase_started_pi_us
                    and core.phase_expires_pi_us==old.core.phase_expires_pi_us
                    and core.local_path_sha256==old.core.local_path_sha256 and value.phase_started_guest_us==0,
                    RouteReason.REQUEST_CONTEXT_CHANGED)
            elif core.phase_revision==old.core.phase_revision:
                require(core.phase==old.core.phase and core.phase_started_pi_us==old.core.phase_started_pi_us
                    and core.phase_expires_pi_us==old.core.phase_expires_pi_us and value.phase_started_guest_us==0
                    and encode(core.phase_goal)==encode(old.core.phase_goal) and core.progress_mode==old.core.progress_mode,
                    RouteReason.REQUEST_CONTEXT_CHANGED)
            elif value.action!=3:
                allowed={1:(2,),2:(3,) if core.purpose==2 else (),3:(4,),4:()}
                require(core.phase_revision==advance(old.core.phase_revision) and value.phase_started_guest_us>0,
                    RouteReason.REQUEST_CONTEXT_CHANGED)
                require(core.phase in allowed.get(old.core.phase,()),RouteReason.REQUEST_CONTEXT_CHANGED)
                phase=self.o.clocks.interval(peer,value.witness.clock_probe_sequence,value.phase_started_guest_us,self.now,value.owner.control_generation)
                require(phase is not None and old.core.phase_started_pi_us<=phase[0]<=interval[1],RouteReason.CLOCK_OR_EXPIRY)
                core.phase_started_pi_us=phase[0];core.phase_expires_pi_us=min(core.deadline_pi_us,phase[0]+self.policy.phase_cap(core.phase))
                if old.core.phase==2 and core.phase==3:
                    facts=self._original_facts(value,peer)
                    require(self.terminal_check(value,facts,self.now),RouteReason.STOP_REQUIRED)
        progress=copied(value.progress);self._meter_progress(core,progress,meter)
        progress.last_progress_pi_us=0 if old is None or core.phase_revision!=old.core.phase_revision else old.progress.last_progress_pi_us
        progress.confirmed_stop_wait_us=0 if old is None else old.progress.confirmed_stop_wait_us
        progress.stop_wait_hwm_pi_us=0 if old is None else old.progress.stop_wait_hwm_pi_us
        require(self.now<min(core.deadline_pi_us,core.phase_expires_pi_us),RouteReason.ROUTE_DEADLINE)
        state=2
        if value.action==3:
            require(old is not None and value.phase_started_guest_us==0
                and core.phase_started_pi_us==old.core.phase_started_pi_us and core.phase_expires_pi_us==old.core.phase_expires_pi_us,
                RouteReason.REQUEST_CONTEXT_CHANGED)
            require(core.terminal_result in (1,2,3),RouteReason.REQUEST_CONTEXT_CHANGED)
            if core.terminal_result==1:
                facts=self._original_facts(value,peer)
                require(self.terminal_check(value,facts,self.now),RouteReason.STOP_REQUIRED)
            state=3
        else:require(core.terminal_result==0 and core.terminal_reason==0,RouteReason.REQUEST_CONTEXT_CHANGED)
        acceptance=schema.RouteAcceptanceRef(registry_epoch=uuid4().bytes if self.epoch==NIL else self.epoch,
            registry_revision=1 if self.revision==0 else advance(self.revision),
            acceptance_nonce=uuid4().bytes,request_nonce=value.request_nonce,request_sha256=digest,
            accepted_core_sha256=route_core_sha256(core),request_sequence=value.request_sequence,
            origin_owner_sha256=route_owner_sha256(value.owner))
        snapshot=schema.RouteSnapshot(core=core,progress=progress,acceptance=acceptance,
            current_window_sha256=value.current_window_sha256,state=state,task_space_kind=value.task_space_kind,
            exit_space_sha256=value.exit_space_sha256,basis_window_sha256=value.basis_window_sha256,
            basis_window_revision=value.basis_window_revision,current_world_sha256=value.witness.world_raw_sha256)
        wait_baseline=progress.confirmed_stop_wait_us if old is None or core.phase_revision!=old.core.phase_revision else self.registry['wait_baseline']
        wait_source=None
        if core.stop_wait_command_sequence:
            facts=self._original_facts(value,peer)
            require(facts.stop.command_sequence==core.stop_wait_command_sequence
                and route_stop_facts_sha256(facts)==core.stop_wait_facts_sha256
                and facts.stop.stationary_started_pi_us<=core.stop_wait_started_pi_us<=facts.stop.stationary_observed_pi_us
                and self.now<facts.stop.valid_until_pi_us,RouteReason.STOP_REQUIRED)
            wait_source=encode(facts)
        self.pending=dict(key=key,digest=digest,raw=raw,body=copied(value),snapshot=snapshot,plan_raw=plan_record.original_body,
            wait_baseline=wait_baseline,wait_source=wait_source)

    def _birth_meter(self,interval,current):
        """보수 birth lower 이전 실제 누적 표본을 pin한다. 도착 시점 이동으로 환불하지 않는다."""
        value=self.r.device_view().get('absolute_meter')
        require(value is not None,RouteReason.METER_GAP)
        history=value[1]
        before=[item for item in history if item.valid and item.meter_epoch==current.meter_epoch
            and item.gap_revision==current.gap_revision and item.metres_per_tick==current.metres_per_tick
            and item.pi_sample_us+item.clock_error_us<=interval[0]]
        require(bool(before),RouteReason.METER_GAP)
        birth=before[-1]
        actual=self.o.episodes._meter(birth.vehicle_sequence,historical=True)
        require(actual==birth and interval[1]-(birth.pi_sample_us-birth.clock_error_us)<=self.policy.max_unobserved_gap_us,
            RouteReason.METER_GAP)
        return birth

    def _meter_progress(self,core,progress,meter):
        require(meter.valid and UUID(meter.meter_epoch).bytes==core.meter_epoch and meter.gap_revision==core.meter_gap_revision
            and meter.metres_per_tick==core.metres_per_tick_upper and meter.total_ticks>=core.meter_baseline_total_ticks,
            RouteReason.METER_GAP)
        used=meter.total_ticks-core.meter_baseline_total_ticks
        require(upper_distance(used,core.metres_per_tick_upper)<core.max_travel_m,RouteReason.ROUTE_DEADLINE)
        progress.used_travel_ticks=used;progress.vehicle_sequence=meter.vehicle_sequence
        require(progress.last_progress_pi_us<=self.now and progress.completed_laps<=core.max_laps,
            RouteReason.NO_PROGRESS)

    def registration(self):
        if self.pending is None:return None
        value=self.pending;snapshot=value['snapshot'];core=snapshot.core
        binding=schema.RouteBinding(route_id=core.route_id,revision=core.revision,purpose=core.purpose,
            core_sha256=snapshot.acceptance.accepted_core_sha256,global_plan_sha256=core.global_plan_sha256,goal_sha256=core.goal_sha256)
        return RouteRegistration(value['raw'],value['digest'],self.query['raw'],encode(snapshot),encode(binding))

    def commit_grant(self,request_nonce,raw_grant):
        """실제 기존 Authority ACK 뒤만 호출. 같은 원 nonce의 grant 실패를 accepted로 바꾸지 않는다."""
        pending=self.pending;require(pending is not None and pending['body'].request_nonce==request_nonce,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        grant=decode(raw_grant,schema.MissionGrant);actual=self.o.missions.grant();owner=self.current_owner()
        require(actual is not None and encode(actual)==raw_grant and owner is not None and grant.kind==3 and grant.admitted,
            RouteReason.DURABLE_NOT_READY)
        binding=decode(self.registration().route_binding_raw,schema.RouteBinding)
        require(grant.HasField('route_binding') and encode(grant.route_binding)==encode(binding)
            and (grant.owner_vm,grant.owner_execution,grant.app_instance)==(owner.vm,owner.execution,owner.app_instance)
            and grant.control_generation==owner.control_generation and grant.mission_id==owner.mission_id
            and grant.mission_revision==owner.mission_revision and grant.grant_revision==owner.grant_revision
            and grant.cancel_fence_revision==owner.cancel_fence_revision,RouteReason.REQUEST_CONTEXT_CHANGED)
        if pending['body'].action!=1:
            old=decode(self.registry['grant'],schema.MissionGrant)
            require(grant.mission_id==old.mission_id and grant.mission_revision==old.mission_revision
                and grant.grant_revision>old.grant_revision,RouteReason.REQUEST_CONTEXT_CHANGED)
        before=pending['body'].owner
        require(peer_key(before)==peer_key(owner) and operation_binding_equal(before.operation_binding,owner.operation_binding)
            and all(getattr(before,k)==getattr(owner,k) for k in
                ('scene_admission_id','scene_revision','source_key_sha256','geometry_scope_sha256')),
            RouteReason.REQUEST_CONTEXT_CHANGED)
        end=min(pending['snapshot'].core.deadline_pi_us,pending['body'].witness.valid_until_pi_us)
        if pending['body'].action==1:end=min(end,self.query['body'].valid_until_pi_us)
        require(self.now<end,RouteReason.CLOCK_OR_EXPIRY)
        reply=self._reply(pending['body'],pending['digest'],1,RouteReason.READY,pending['snapshot'],owner)
        saved=dict(**pending,reply=reply,grant=bytes(raw_grant),target=copied(owner),state=None,
            applied={},floor=0,first_admitted=False,calculation_hwm={})
        self.registry=saved;self.epoch=pending['snapshot'].acceptance.registry_epoch
        self.revision=pending['snapshot'].acceptance.registry_revision;self.pending=None
        self.last_reply=dict(key=saved['key'],digest=saved['digest'],reply=reply)
        if saved['snapshot'].state==3:self.terminal=deepcopy(saved)
        core=saved['snapshot'].core
        refs=() if saved['snapshot'].state==3 else (core.goal_sha256,core.topology_sha256,core.global_plan_sha256,
            core.geometry_scope_sha256,saved['snapshot'].current_window_sha256,saved['snapshot'].basis_window_sha256,
            saved['snapshot'].exit_space_sha256)
        self.data.pin((v for v in refs if v!=ZERO),owner='route')
        return reply

    def support_changed(self):
        """현재 인증 peer/M capability 교체 사건 직후 같은 actor에서 호출한다. legacy로 강등하지 않는다."""
        if self.registry is None or self.registry['snapshot'].state!=2 or self.failed:return
        owner=self.current_owner()
        if owner is None or not self.supported(peer_key(owner)):self.cancel(RouteReason.CAPABILITY_LOST)

    def original_query(self):
        """원 E가 지난 조회도 원 bytes다. 새 권한·새 nonce·새 출발로 재발급하지 않는다."""
        return None if self.query is None else self.query['raw']

    def _current_grant(self,peer):
        require(self.registry is not None and not self.failed,RouteReason.REQUEST_CONTEXT_CHANGED)
        actual=self.current_owner();require(actual is not None,RouteReason.SOURCE_UNAVAILABLE)
        self._current(actual,peer)
        grant=self.o.missions.grant()
        require(grant is not None and grant.kind==3 and grant.admitted and grant.HasField('route_binding')
            and (grant.owner_vm,grant.owner_execution,grant.app_instance)==(actual.vm,actual.execution,actual.app_instance)
            and route_permission_sha256(grant)==route_permission_sha256(self.registry['grant']),
            RouteReason.REQUEST_CONTEXT_CHANGED)
        target=self.registry['target']
        require(all(getattr(actual,k)==getattr(target,k) for k in ('source_key_sha256','geometry_scope_sha256',
            'scene_admission_id','scene_revision','control_generation','mission_id','mission_revision','grant_revision','cancel_fence_revision'))
            and operation_binding_equal(actual.operation_binding,grant.operation_binding),RouteReason.REQUEST_CONTEXT_CHANGED)
        return grant

    def rebind_grant(self,raw_grant):
        """A/B 재결합은 실제 새 grant만 채택한다. 원 receipt/core/birth/query는 불변이다."""
        require(self.registry is not None and self.registry['snapshot'].state==2 and not self.failed,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        grant=decode(raw_grant,schema.MissionGrant);actual=self.o.missions.grant()
        require(actual is not None and encode(actual)==raw_grant and grant.kind==3 and grant.admitted,
            RouteReason.DURABLE_NOT_READY)
        old=decode(self.registry['grant'],schema.MissionGrant)
        previous=old.operation_binding;current=grant.operation_binding
        require(encode(grant.route_binding)==encode(old.route_binding)
            and (current.service_epoch,current.operation_revision,current.departure_nonce)==
                (previous.service_epoch,previous.operation_revision,previous.departure_nonce)
            and current.permission_revision>=previous.permission_revision
            and (current.permission_revision!=previous.permission_revision or current.parking_enabled==previous.parking_enabled)
            and grant.mission_id==old.mission_id and grant.mission_revision==old.mission_revision
            and grant.grant_revision>old.grant_revision and grant.mission_deadline_pi_us==old.mission_deadline_pi_us,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        owner=self.current_owner();require(owner is not None,RouteReason.SOURCE_UNAVAILABLE)
        self._current(owner,peer_key(owner))
        require(owner.source_key_sha256==self.registry['target'].source_key_sha256
            and owner.geometry_scope_sha256==self.registry['target'].geometry_scope_sha256
            and operation_binding_equal(owner.operation_binding,grant.operation_binding)
            and owner.control_generation==grant.control_generation and owner.mission_id==grant.mission_id
            and (grant.owner_vm,grant.owner_execution,grant.app_instance)==(owner.vm,owner.execution,owner.app_instance)
            and owner.mission_revision==grant.mission_revision and owner.grant_revision==grant.grant_revision,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        self.registry['grant']=bytes(raw_grant);self.registry['state']=None;self.registry['applied'].clear()
        self.registry['target']=copied(owner)

    def prepare_state(self,state,peer,raw):
        """기존 StateCopies 수락 전에 검산한다. 계산/completion 자체를 gate하지 않는다."""
        if not state.HasField('route'):return None if self.registry is not None and self.registry['snapshot'].state==2 else True
        if self.registry is None:return None
        saved=self.registry;expected=saved['snapshot'];candidate=state.route
        if expected.state==3:
            if encode(candidate.core)!=encode(expected.core) or encode(candidate.acceptance)!=encode(expected.acceptance):return None
            grant=self.o.missions.grant()
            if grant is None or grant.kind!=3:return True  # Historical sidecar needs no fresh motion lease.
        try:
            self._current_grant(peer)
            require((state.source_vm,state.source_execution,state.source_app_instance)==tuple(peer)
                and state.source_generation==self.current_owner().control_generation
                and encode(candidate.core)==encode(expected.core) and encode(candidate.acceptance)==encode(expected.acceptance),
                RouteReason.REQUEST_CONTEXT_CHANGED)
            request=saved['body'];first=not saved['first_admitted']
            require(state.path_revision==request.plan_revision and state.maneuver_revision==expected.core.phase_revision,
                RouteReason.HASH_OR_BODY_MISMATCH)
            if first:
                require(UUID(state.message_id).bytes==request.phase_state_message_id
                    and state.calculation_sequence>request.calculation_sequence,RouteReason.ACTIVATION_NOT_READY)
            require(candidate.task_space_kind==expected.task_space_kind and candidate.exit_space_sha256==expected.exit_space_sha256
                and candidate.current_window_sha256==expected.current_window_sha256
                and candidate.basis_window_sha256==expected.basis_window_sha256
                and candidate.basis_window_revision==expected.basis_window_revision,RouteReason.GEOMETRY_CONFLICT)
            world=self.data.world(candidate.current_world_sha256,self.now)
            published=self.data.world_snapshot(candidate.current_world_sha256,self.now)
            require(published.route_view.window_sha256==candidate.current_window_sha256
                and published.route_view.geometry_scope_sha256==expected.core.geometry_scope_sha256,
                RouteReason.GEOMETRY_CONFLICT)
            source=world.source
            require(state.map_epoch==source.map_epoch and state.frame_epoch==source.frame_epoch
                and state.odom_epoch==source.odom_epoch and state.configuration==source.configuration
                and state.calibration==source.calibration and state.primitive==source.primitive.hex()
                and state.reference_id==source.reference_id and state.reference_revision==source.reference_revision
                and state.observation_seq==source.observation_sequence,RouteReason.SOURCE_UNAVAILABLE)
            window=self.data.get(5,candidate.basis_window_sha256).message()
            if candidate.task_space_kind==1:
                require(state.HasField('task_reference') and state.task_reference.basis_revision>0,RouteReason.GEOMETRY_CONFLICT)
                if state.task_reference.changing:
                    episode=self.o.episodes.registry
                    require(saved['body'].extent.mode==3 and episode is not None and episode['snapshot'].core.state==2
                        and state.HasField('episode') and state.episode.acceptance==episode['snapshot'].acceptance,
                        RouteReason.ACTIVE_EPISODE)
                prefix=state.task_reference.basis_prefix;basis=state.task_reference.basis_revision
            else:prefix=window.reference;basis=candidate.basis_window_revision
            local=route_local_path_sha256(state,global_plan_sha256=expected.core.global_plan_sha256,
                geometry_scope_sha256=expected.core.geometry_scope_sha256,basis_window_sha256=candidate.basis_window_sha256,
                task_space_kind=candidate.task_space_kind,exit_space_sha256=candidate.exit_space_sha256,
                basis_revision=basis,basis_prefix=prefix)
            require(local==expected.core.local_path_sha256,RouteReason.HASH_OR_BODY_MISMATCH)
            require(state.mission.HasField('route_binding') and encode(state.mission.route_binding)==encode(self.o.missions.grant().route_binding),
                RouteReason.REQUEST_CONTEXT_CHANGED)
            grant=self.o.missions.grant()
            require(state.mission.mission_id==grant.mission_id and state.mission.mission_revision==grant.mission_revision
                and state.mission.grant_revision==grant.grant_revision
                and state.mission.cancel_fence_revision==grant.cancel_fence_revision
                and operation_binding_equal(state.mission.operation_binding,grant.operation_binding),
                RouteReason.REQUEST_CONTEXT_CHANGED)
            if expected.state==3:
                require(encode(candidate.progress)==encode(expected.progress),RouteReason.REQUEST_CONTEXT_CHANGED)
                return dict(revision=self.revision,peer=tuple(peer),progress=copied(expected.progress),raw=bytes(raw),
                    message_id=state.message_id,state_revision=state.state_revision,calculation=state.calculation_sequence,
                    plan=state.path_revision,phase=state.maneuver_revision,vehicle=state.vehicle_sequence,
                    wait_baseline=saved['wait_baseline'],world=candidate.current_world_sha256)
            meter=self.o.episodes._meter(state.vehicle_sequence);require(meter is not None,RouteReason.METER_GAP)
            progress=copied(candidate.progress);self._meter_progress(expected.core,progress,meter)
            old=expected.progress
            require(candidate.progress.used_travel_ticks==progress.used_travel_ticks
                and candidate.progress.confirmed_stop_wait_us<=old.confirmed_stop_wait_us
                and candidate.progress.stop_wait_hwm_pi_us<=old.stop_wait_hwm_pi_us,
                RouteReason.METER_GAP)
            require(progress.occurrence_index in (old.occurrence_index,old.occurrence_index+1)
                and state.calculation_sequence>saved['calculation_hwm'].get(tuple(peer),0)
                and state.vehicle_sequence>=old.vehicle_sequence,RouteReason.NO_PROGRESS)
            # The actual tracker/Vehicle/seam check is a required internal port,
            # not a report/heartbeat cast into physical progress.
            advanced=bool(progress.used_travel_ticks>old.used_travel_ticks and state.vehicle_sequence>old.vehicle_sequence
                and self.progress_check(expected,state,world,meter))
            if progress.occurrence_index!=old.occurrence_index or progress.station_lower_m>old.station_upper_m:
                require(advanced,RouteReason.NO_PROGRESS)
            progress.last_progress_pi_us=meter.pi_sample_us-meter.clock_error_us if advanced else old.last_progress_pi_us
            progress.confirmed_stop_wait_us=old.confirmed_stop_wait_us;progress.stop_wait_hwm_pi_us=old.stop_wait_hwm_pi_us
            self._stop_wait(expected.core,progress)
            return dict(revision=self.revision,peer=tuple(peer),progress=progress,raw=bytes(raw),
                message_id=state.message_id,state_revision=state.state_revision,calculation=state.calculation_sequence,
                plan=state.path_revision,phase=state.maneuver_revision,vehicle=state.vehicle_sequence,
                wait_baseline=progress.confirmed_stop_wait_us if advanced else saved['wait_baseline'],
                world=candidate.current_world_sha256)
        except (RouteError,ValueError):return None

    def commit_state(self,prepared):
        if prepared is True:return
        require(prepared is not None and self.registry is not None and prepared['revision']==self.revision,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        saved=self.registry;saved['snapshot'].progress.CopyFrom(prepared['progress'])
        saved['snapshot'].current_world_sha256=prepared['world']
        saved['state']=prepared;saved['calculation']=prepared['calculation'];saved['floor']=prepared['calculation']
        saved['wait_baseline']=prepared['wait_baseline'];saved['first_admitted']=True
        saved['calculation_hwm'][prepared['peer']]=prepared['calculation']

    def applied(self,peer,identity):
        """기존 StateCopies의 실제 target application 증거를 소비한다. prepare calc번호는 비교하지 않는다."""
        if self.registry is None or self.registry['state'] is None:return False
        evidence=self.r.machine.states.evidence(peer[0],'autonomous')
        saved=self.r.drive_link.copies.get(identity)
        if evidence is None or evidence['copy_message_id']!=identity or saved is None:return False
        original=decode(saved[0],schema.DriveLink).accepted.state
        if (not original.HasField('route') or original.route.acceptance!=self.registry['snapshot'].acceptance
                or original.route.core.local_path_sha256!=self.registry['snapshot'].core.local_path_sha256):return False
        try:self._current_grant(peer)
        except RouteError:return False
        self.registry['applied'][tuple(peer)]=(identity,self.now)
        return True

    def _stop_wait(self,core,progress):
        if not core.stop_wait_command_sequence:return
        facts=self.r.control_facts()
        if facts is None or not facts.HasField('stop'):return
        stop=facts.stop
        if (stop.state!=3 or stop.command_sequence!=core.stop_wait_command_sequence
                or self.now>=stop.valid_until_pi_us):return
        if self.registry is None or self.registry.get('wait_source') is None:return
        original=decode(self.registry['wait_source'],schema.DriveControlFacts).stop
        if any(getattr(stop,k)!=getattr(original,k) for k in ('pi_session','device_session','command_sequence',
                'control_generation','producer_role','source_vm','source_execution','source_app_instance',
                'mission_id','mission_revision','grant_revision','plan_revision','phase_revision')):return
        begin=max(core.stop_wait_started_pi_us,progress.stop_wait_hwm_pi_us,stop.stationary_started_pi_us)
        end=min(stop.stationary_observed_pi_us,self.now)
        if end>begin:
            progress.confirmed_stop_wait_us+=end-begin;progress.stop_wait_hwm_pi_us=end

    def output_deadline(self):
        if self.registry is None or self.registry['snapshot'].state!=2:return None
        core=self.registry['snapshot'].core
        return min(core.deadline_pi_us,core.phase_expires_pi_us)

    def exhausted(self):
        if self.registry is None:return None
        snapshot=self.registry['snapshot'];core=snapshot.core
        if snapshot.state==3:return None
        if self.now>=core.deadline_pi_us:return RouteReason.ROUTE_DEADLINE
        if self.now>=core.phase_expires_pi_us:return RouteReason.PHASE_DEADLINE
        meter=self.o.episodes._meter()
        try:
            require(meter is not None,RouteReason.METER_GAP)
            progress=copied(snapshot.progress);self._meter_progress(core,progress,meter);self._stop_wait(core,progress)
        except RouteError as exc:return exc.reason
        previous=snapshot.progress
        # Pure confirmed STOP wait can discount elapsed progress time once,
        # while original hard route/phase deadlines continue advancing.
        at=previous.last_progress_pi_us or core.phase_started_pi_us
        elapsed=self.now-at-(progress.confirmed_stop_wait_us-self.registry['wait_baseline'])
        snapshot.progress.confirmed_stop_wait_us=progress.confirmed_stop_wait_us
        snapshot.progress.stop_wait_hwm_pi_us=progress.stop_wait_hwm_pi_us
        if elapsed>=self.policy.progress_timeout_us:return RouteReason.NO_PROGRESS
        return None

    def command_allowed(self,peer,command):
        if command.get('producer_role')=='MANUAL':return True  # Existing authenticated manual registry remains authoritative.
        # A plain safety STOP is never a motion lease. A cusp direction intent
        # still needs the complete current route/source/clock checks below.
        if command.get('command_kind')=='STOP' and not command.get('next_direction',0):return True
        grant=self.o.missions.grant()
        if grant is None or grant.kind!=3:return True  # MANUAL/legacy FOLLOW keep their own guards.
        if self.registry is None or self.failed or self.registry['snapshot'].state!=2:return False
        saved=self.registry;state=saved['state'];application=saved['applied'].get(tuple(peer))
        if state is None or application is None:return False
        try:
            self._current_grant(peer)
            require(not self.o.hold and self.r.machine.active_app=='autonomous',RouteReason.REQUEST_CONTEXT_CHANGED)
            require(int(command['generation'])==self.current_owner().control_generation
                and UUID(command['mission_id']).bytes==grant.mission_id and int(command['mission_revision'])==grant.mission_revision
                and int(command['grant_revision'])==grant.grant_revision
                and int(command['plan_revision'])==state['plan'] and int(command['phase_revision'])==state['phase']
                and int(command['calculation_sequence'])>state['calculation'],RouteReason.ACTIVATION_NOT_READY)
            interval=self.o.clocks.interval(tuple(peer),int(command['clock_probe_sequence']),int(command['completed_guest_us']),
                self.now,self.current_owner().control_generation)
            require(interval is not None and interval[0]>=application[1]
                and self.now<int(command['valid_until_pi_us'])<=min(interval[0]+100000,interval[2]),
                RouteReason.CLOCK_OR_EXPIRY)
            world=self.data.world_for_sequence(int(command['world_sequence']),self.current_owner().source_key_sha256,self.now)
            published=self.data.world_snapshot(world.world_raw_sha256,self.now);trust=self.r.drive_link.trust
            require(trust is not None and self.now<trust.expires_pi_us
                and int(command['valid_until_pi_us'])<=world.mapper.valid_until_pi_us
                and all(getattr(world.source,k)==getattr(trust,k) for k in ('vehicle','mapper_boot','mapper_instance',
                    'map_epoch','frame_epoch','clock_epoch','configuration','calibration'))
                and world.source.primitive==bytes.fromhex(trust.primitive)
                and published.route_view.window_sha256==saved['snapshot'].current_window_sha256
                and published.route_view.geometry_scope_sha256==saved['snapshot'].core.geometry_scope_sha256,
                RouteReason.SOURCE_UNAVAILABLE)
            original=decode(state['raw'],schema.DriveState)
            require(published.reference_id==original.reference_id,RouteReason.GEOMETRY_CONFLICT)
            if original.HasField('task_reference'):
                prefix=original.task_reference.basis_prefix
                require(len(published.reference)>=len(prefix)
                    and all(encode(a)==encode(b) for a,b in zip(prefix,published.reference)),RouteReason.GEOMETRY_CONFLICT)
            raw=self.r.drive_link.vehicle_history.get(int(command['vehicle_sequence']))
            require(raw is not None,RouteReason.SOURCE_UNAVAILABLE)
            vehicle=decode(raw,schema.VehicleSample);current=self.r.device_view().get('vehicle_sample')
            require(current is not None and vehicle.quality==3 and all(getattr(vehicle,k)==current.get(k)
                for k in ('host_boot','device_session','odom_epoch','pi_session','calibration'))
                and self.now<vehicle.pi_sample_us-vehicle.clock_error_us+self.policy.max_unobserved_gap_us,
                RouteReason.SOURCE_UNAVAILABLE)
            require(self.exhausted() is None,RouteReason.ROUTE_DEADLINE)
        except (RouteError,KeyError,ValueError):return False
        return True

    def tick(self):
        self.data.tick(self.now)
        for purpose,(raw,_) in tuple(self.eligibility.items()):
            value=decode(raw,schema.RouteEligibility)
            try:self._witness(value.owner,value.witness,peer_key(value.owner))
            except RouteError:
                del self.eligibility[purpose];self.ready_revision[purpose]=advance(self.ready_revision[purpose])
                self.semantic.pop(purpose,None)
        if self.registry is None or self.registry['snapshot'].state==3 or self.failed:return
        owner=self.current_owner();peer=None if owner is None else peer_key(owner)
        reason=RouteReason.CAPABILITY_LOST if peer is None or not self.supported(peer) else self.exhausted()
        if reason is None:
            try:self._current_grant(peer)
            except RouteError as exc:reason=exc.reason
        if reason is not None:
            # Close the output registry only. This is not a fabricated C FINISH
            # receipt or a source-exported CLOSED fullState.
            self.failed=True;self.reason=reason;self.terminal=deepcopy(self.registry)
            self.data.pin((),owner='route')
            self.stop_auto(reason)

    def cancel(self,reason=RouteReason.CANCELLED):
        """안전 actor가 RAM 철회를 먼저 처리한다. 미수락 후보를 성공 receipt로 바꾸지 않는다."""
        self.failed=True;self.reason=RouteReason(reason)
        if self.registry is not None:self.terminal=deepcopy(self.registry)
        self.data.pin((),owner='route')
        self.stop_auto(self.reason)
        if self.pending is None:return None
        pending=self.pending;reply=self._reply(pending['body'],pending['digest'],2,self.reason)
        self.last_reply=dict(key=pending['key'],digest=pending['digest'],reply=reply);self.pending=None
        return reply

    def status(self,peer,clock_end):
        if not self.supported(peer) or self.now>=clock_end:return None
        saved=self.registry;snapshot=None if saved is None else saved['snapshot'];state=None if saved is None else saved['state']
        known=snapshot is not None and not self.failed
        if not known:state=None
        end=clock_end if not known or snapshot.state==3 else min(clock_end,self.output_deadline())
        if self.now>=end:return None
        grant=self.o.missions.grant()
        result=schema.RouteStatus(major=1,registry_epoch=NIL if not known else self.epoch,registry_revision=0 if not known else self.revision,
            route_id=NIL if not known else snapshot.core.route_id,accepted_core_sha256=ZERO if not known else snapshot.acceptance.accepted_core_sha256,
            global_plan_sha256=ZERO if not known else snapshot.core.global_plan_sha256,local_path_sha256=ZERO if not known else snapshot.core.local_path_sha256,
            route_state=0 if not known else snapshot.state,commit_state=0 if not known else 1 if state is None else 3 if tuple(peer) in saved['applied'] else 2,
            reason=int(self.reason),valid_until_pi_us=end,state_message_id=NIL if state is None else UUID(state['message_id']).bytes,
            state_sha256=ZERO if state is None else sha256(state['raw']).digest(),state_revision=0 if state is None else state['state_revision'],
            plan_revision=0 if state is None else state['plan'],phase_revision=0 if state is None else state['phase'],
            calculation_hwm=0 if state is None else state['calculation'],vehicle_hwm=0 if state is None else state['vehicle'],
            current_permission_sha256=ZERO if grant is None else route_permission_sha256(grant),
            current_window_sha256=ZERO if not known else snapshot.current_window_sha256,
            terminal_result=0 if not known else snapshot.core.terminal_result)
        for purpose in (1,2):
            ref=self.eligibility_ref(purpose)
            if ref is not None:result.eligibility.add().CopyFrom(ref)
        return result
