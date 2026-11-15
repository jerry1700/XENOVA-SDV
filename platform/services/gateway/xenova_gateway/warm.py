"""warm-drive-1 consumer. Qualification may select a target, never permit output.

The existing Authority issue/apply/rearm boundary remains authoritative. A new
owner must actually calculate in the newly issued context before output resumes.
"""
from xenova_mqtt.lifecycle import parse
from xenova_mqtt.drive_binding import app_instance
from .verified import VerifiedDriveMachine
from .machine import DRIVE_APPS,ForwardStop,Mode
from .authority import AuthorityError,AUTO_RECOVERY_IDENTITY

# 결과의 부적합과 VM/앱 실행 고장을 구분한다. 아래 이유 자체는 새 READY나 명령을 만들지 않는다.
_CALCULATION_REJECTIONS=frozenset(('CALCULATION_FAILED','DEADLINE'))
_TRANSIENT_REJECTIONS=_CALCULATION_REJECTIONS|frozenset(('INPUT_UNAVAILABLE','INPUT_STALE',
    'MAP_UNAVAILABLE','WORLD_UNAVAILABLE','POSE_UNAVAILABLE','OBSERVATION_EXPIRED',
    'CLOCK_ERROR','STEERING_UNAVAILABLE'))
_REJECTION_LIMIT=5  # 초기 운영값: 서로 다른 실패 완료 5개 또는 원 300ms 창 중 먼저 도달한 때
_STATE_INVALIDATIONS=frozenset(('STATE_REJECTED','STATE_FRAME_CHANGED',
    'STATE_CONFIGURATION_CHANGED','STATE_REFERENCE_CHANGED'))
_PLANNER_WAITS=frozenset(('PATH_UNAVAILABLE','PATH_INVALIDATED','SEARCH_NOT_FOUND','NODE_BUDGET',
    'WORK_BUDGET','BUSY','COLLISION','PATH_LIMIT','CANCELLED','STATE_PREPARING','CONTEXT_CHANGED'))|_STATE_INVALIDATIONS

class WarmDriveMachine(VerifiedDriveMachine):
    readiness_profile='warm-drive-1'
    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.checks={};self.qualification={};self.execution_phases={}
        self.evidence_not_before=0.
        from .planning import PlannedTransition
        self.planning=PlannedTransition(self)
        from .state_transfer import StateCopies
        self.states=StateCopies(self)
        from xenova_mqtt.manual_session import SessionEvidence
        self.manual_input=SessionEvidence();self.movement=None
        self._deferred_abandon=None
        self.function_rejections={};self.completed_work={};self.last_ready_work={};self.deadline_misses={}

    def on_manual_input(self,payload,received):
        from xenova_mqtt.manual_session import parse
        try:sample=parse(payload)
        except (ValueError,TypeError):return False
        if not 0<=self.clock()-received<=.2:return False
        if not self.manual_input.observe(sample,received):return False
        if sample.valid and self.manual_input.current(self.clock()) is None:return False
        if self.movement:self.movement.observe(sample)
        return True

    def block(self,reason,now):
        self._abandon(reason)
        self.qualification.clear();self.evidence_not_before=now
        self.function_rejections.clear();self.completed_work.clear();self.last_ready_work.clear()
        self.deadline_misses.clear()
        self.execution_phases.clear()
        return super().block(reason,now)

    def _abandon(self,reason):
        if not self.durable_write_available():
            if self._deferred_abandon is None:self._deferred_abandon=self.generation,reason
            return True
        try:self.authority.abandon_preparations(reason)
        except AuthorityError as exc:
            self.output_permitted=False;self._authorized=False
            self.failure=self.authority.error or str(exc);return False
        return True

    def _ready(self,vm,app,now,held=False):
        binding=self.bindings.current(vm,self.clock())
        if (binding and binding.get('app_state_models',{}).get(app)=='stateful' and
                self.states.evidence(vm,app) is None):return False
        pending=self.authority.value.get('pending') or {}
        if vm==self.active_vm and app=='manual' and self.movement:
            sample=self.manual_input.current(now)
            if (sample is None or sample.session!=self.movement.session or not self._manual_context(sample) or self.movement.blocked) and not self._manual_zero_prepared():
                return False
        if pending.get('target') and vm==self.active_vm and app==self.active_app:
            from .planning import context
            row=self.bindings.current(vm,now)
            if row is None or context(row,app)!=pending['target']:return False
        return super()._ready(vm,app,now,held)

    def _change(self,mode,vm,reason,now,*,arm=False,distrust=None):
        if not self.durable_write_available():
            return super()._change(mode,vm,reason,now,arm=arm,distrust=distrust)
        if not self._abandon('CONTROL_CONTEXT_CHANGED'):return [ForwardStop(self.generation)]
        manual=None
        if self.authority.value.get('manual_input_profile') in ('manual-session-1','manual-input-2','manual-input-3') and mode in (Mode.MANUAL,Mode.AUTO_READY):
            sample=self.manual_input.current(now)
            if sample is None or not self._manual_context(sample):
                mode=Mode.SAFE_STOP;reason='driver_session_unavailable';arm=False;self._authorized=False
            else:manual=dict(manual_session=sample.session,manual_policy=(
                'CONTINUE_SESSION' if self.active_app=='manual' else 'NEUTRAL_THEN_ACTION'))
            previous=self.authority.value.get('pending') or {}
            if manual and reason=='resume_requested' and self.authority.value['rearm_required'] and previous.get('manual_policy')=='NEUTRAL_THEN_ACTION':
                manual['manual_policy']='NEUTRAL_THEN_ACTION'
        recovery=None
        lost=self.active_vm
        if (reason in ('autonomous_unavailable','no_fresh_standby') and self.active_app=='autonomous' and
            (vm!=lost or mode is Mode.SAFE_STOP) and
            (self._auto_exit_confirmed(lost,now) or
             (self.output_permitted and self._deadline_recovery_due(lost,now)))):
            fault=self._auto_recovery_identity(lost,now)
            if fault:recovery=(lost,fault)
        return super()._change(mode,vm,reason,now,arm=arm,manual=manual,distrust=distrust,auto_recovery=recovery)

    def _auto_recovery_identity(self,vm,now):
        row=self.bindings.current(vm,now)
        if row and self.sessions.get((vm,'autonomous'))==(row['execution_id'],row['boot_id'],app_instance(row,'autonomous')):
            return {**{key:row[key] for key in AUTO_RECOVERY_IDENTITY},'instance':app_instance(row,'autonomous'),
                    'generation':self.generation,'host':self.authority.host}
        return None

    def _auto_exit_confirmed(self,vm,now):
        key=vm,'autonomous'
        check=self.checks.get(key)
        return (self.execution_phases.get(key)=='STOPPED' and self.prepared[key].generation==self.generation
                and check is not None and 0<=now-check[1]<=self.readiness_lease_s
                and self._bound(vm,now,'autonomous'))

    def _deadline_recovery_due(self,vm,now):
        key=(vm,'autonomous')
        misses=self.deadline_misses.get(key)
        if misses is None or misses[0]!=self.generation or not self._bound(vm,now,'autonomous'):
            return False
        _,_,count,last=misses
        return count>=_REJECTION_LIMIT or now-last>=self.readiness_lease_s

    def _bound(self,vm,at,app=None):
        # Old AUTO must not become eligible again during the receipt/Store update.
        # This fence is durable and scoped to the old instance, preserving MANUAL.
        fault=self.authority.value.get('auto_recovery',{}).get(vm)
        row=self.bindings.current(vm,self.clock()) if fault and app in (None,'autonomous') else None
        if row and row['execution_id']==fault['execution_id'] and app_instance(row,'autonomous')==fault['instance']:
            return False
        return super()._bound(vm,self.clock(),app)

    def _adopt_control(self,*args,**kwargs):
        actions=super()._adopt_control(*args,**kwargs)
        pending=self.authority.value.get('pending') or {}
        if 'manual_policy' in pending:
            from xenova_mqtt.manual_session import MovementGate
            self.movement=MovementGate(self.generation,pending['manual_session'],pending['manual_policy'])
            registry=getattr(self,'manual_registry',None);sample=self.manual_input.current(self.clock())
            if (registry is not None and sample is not None and pending['manual_policy']=='CONTINUE_SESSION'
                    and sample.profile!='manual-session-1' and sample.session==registry.sid
                    and sample.input_epoch==(registry.epoch if registry.kind=='HELD' else None)
                    and sample.grant_revision==(registry.grant if registry.kind=='HELD' else 0)
                    and sample.action==registry.action and sample.keys==frozenset(registry.keys)
                    and not registry.blocked and registry.valid):
                # Reuse the actual current immutable input, including its old E.
                # This is not a new sample, user action or neutral/STOP proof.
                self.movement.observe(sample)
        else:self.movement=None
        return actions

    def _manual_context(self,sample):
        if sample.generation==self.generation:return True
        pending=self.authority.value.get('pending') or {}
        return bool(sample.profile!='manual-session-1' and sample.generation<self.generation
            and pending.get('manual_policy')=='CONTINUE_SESSION' and pending.get('manual_session')==sample.session
            and self.active_app=='manual')

    def _manual_zero_prepared(self):
        registry=getattr(self,'manual_registry',None)
        return bool(registry is not None and registry.zero_prepared(self.manual_input.value))

    def on_command(self,app,vm,payload,t,reference=None):
        if self.execution_phases.get((vm,app))=='STOPPED':return False
        if not 0<=self.clock()-t<=self.timing.command_timeout_s:return False
        if app=='manual' and vm==self.active_vm and self.movement:
            if not isinstance(payload,dict):return False
            if self._manual_zero_prepared() and payload.get('v_mps')==0 and payload.get('steer_rad')==0:
                return super().on_command(app,vm,payload,t,reference)
            sample=self.manual_input.current(self.clock())
            if (sample is None or sample.session!=self.movement.session or
                not self._manual_context(sample) or self.movement.blocked):return False
            if not self.movement.allowed and (payload.get('v_mps')!=0 or payload.get('steer_rad')!=0):return False
        return super().on_command(app,vm,payload,t,reference)

    def _output(self,now):
        if self.active_app=='manual' and self._manual_zero_prepared():
            for tracks in self.nodes.values():
                for item in tracks.values():item.pending=None
            if self._last_stop_t is None or now-self._last_stop_t>=self.timing.stop_resend_s:
                self._last_stop_t=now;return [ForwardStop(self.generation)]
            return []
        if self.active_app=='manual' and self.movement and self.output_permitted:
            sample=self.manual_input.current(now)
            if sample is None or sample.session!=self.movement.session or not self._manual_context(sample) or self.movement.blocked:
                self._fail('MANUAL_INPUT_UNAVAILABLE',now)
        return super()._output(now)

    def accept_session(self,vm,boot,instance,now=None,*,app=None):
        row=self.bindings.current(vm,self.clock() if now is None else now)
        if not row or row.get('profile')!='warm-drive-1' or app not in DRIVE_APPS:return False
        before=self.sessions.get((vm,app))
        if not super().accept_session(vm,boot,instance,now,app=app):return False
        if before!=self.sessions.get((vm,app)):
            self.checks.pop((vm,app),None);self.qualification.pop((vm,app),None)
            self.execution_phases.pop((vm,app),None)
            self.states.invalidate(vm,app)
            self.function_rejections.pop((vm,app),None)
            self.deadline_misses.pop((vm,app),None)
            self.completed_work.pop((vm,app),None);self.last_ready_work.pop((vm,app),None)
        return True

    def on_readiness(self,*args):return False  # no legacy READY fallback

    def _invalidate_function(self,vm,app,reason):
        super()._invalidate_function(vm,app,reason)
        self.qualification.pop((vm,app),None)
        self.states.invalidate(vm,app)

    def _input_unavailable(self,vm,now):
        from .verified import INPUT_FAILURES
        p=self.prepared[vm,'autonomous'];check=self.checks.get((vm,'autonomous'))
        return (self.execution_phases.get((vm,'autonomous')) not in ('STOPPING','STOPPED','RESIDUAL')
                and p.generation==self.generation and p.state!='READY' and p.reason in INPUT_FAILURES and
                check is not None and 0<=now-check[1]<=self.readiness_lease_s)

    def on_lifecycle(self,app,vm,payload,t):
        now=self.clock()
        if (t<self.evidence_not_before or not 0<=now-t<=self.readiness_lease_s or vm not in self.nodes or
            app not in DRIVE_APPS or not self._bound(vm,now,app)):return False
        try:value=parse(payload)
        except (ValueError,TypeError):return False
        key=vm,app;old=self.checks.get(key);p=self.prepared[key]
        if (value.generation!=self.generation or (old and value.check<=old[0]) or value.progress<p.progress):return False
        if self.execution_phases.get(key)=='STOPPED' and value.phase!='STOPPED':return False
        fresh_ready=value.readiness=='READY' and value.progress>p.progress
        if value.readiness=='READY' and p.state!='READY' and not fresh_ready:return False
        if value.phase=='STOPPED':
            self.nodes[vm][app].pending=None
            self.references.pop(key,None)
            self.states.invalidate(vm,app)
        recovering=self._retrying_function(key,p.reason,self.execution_phases.get(key),t)
        self.checks[key]=(value.check,t)
        self.execution_phases[key]=value.phase
        if value.phase=='EXECUTING' and value.progress>0 and value.progress>p.progress:
            self.completed_work[key]=(self.generation,t)
            if value.readiness=='READY':self.last_ready_work[key]=(self.generation,t)
        if app=='autonomous' and value.readiness=='NOT_READY' and value.reason in _TRANSIENT_REJECTIONS:
            previous_failure=self.function_rejections.get(key)
            count=int(value.phase=='EXECUTING' and value.progress>p.progress)
            if previous_failure is not None and previous_failure[0]==self.generation:
                self.function_rejections[key]=(self.generation,previous_failure[1],min(_REJECTION_LIMIT,previous_failure[2]+count))
            else:self.function_rejections[key]=(self.generation,t,count)
        else:self.function_rejections.pop(key,None)
        if app=='autonomous':
            misses=self.deadline_misses.get(key)
            if value.phase=='EXECUTING' and value.reason=='DEADLINE' and value.progress>p.progress and value.progress>0:
                # A skipped completion provides no evidence of consecutiveness.
                consecutive=misses and misses[:2]==(value.generation,value.progress-1)
                count=min(_REJECTION_LIMIT,misses[2]+1) if consecutive else 1
                self.deadline_misses[key]=(value.generation,value.progress,count,t)
            elif not (value.phase=='EXECUTING' and value.reason in _CALCULATION_REJECTIONS and
                      misses and misses[:2]==(value.generation,value.progress)):
                # An aging report for the same completion can become
                # CALCULATION_FAILED without any new work replacing the miss.
                self.deadline_misses.pop(key,None)
        retrying=(value.readiness=='NOT_READY' and self._retrying_function(key,value.reason,value.phase,t))
        valid=value.readiness=='QUALIFIED' or (value.readiness=='READY' and
            (value.progress>p.progress or (p.observed is not None and 0<=t-p.observed<=self.readiness_lease_s)))
        previous=self.qualification.get(key)
        continuous=bool(valid and previous and previous[0]==self.generation and 0<=t-previous[1]<=self.readiness_lease_s)
        if valid:self.qualification[key]=(self.generation,t,previous[2] if continuous else t)
        elif not (retrying and previous and previous[0]==self.generation and
                0<=t-previous[1]<=self.readiness_lease_s):self.qualification[key]=None
        # Renew calculation evidence only after new completed work. Supervision
        # can revoke or qualify waiting; it cannot make a stalled calculation live.
        if value.readiness=='READY' and value.progress>p.progress:
            continuous_ready=(p.state=='READY' or recovering) and p.generation==value.generation and p.observed is not None and 0<=t-p.observed<=self.readiness_lease_s
            p.since=p.since if continuous_ready else t;p.observed=t
        elif value.readiness!='READY' and not retrying:p.observed=None;p.since=None
        p.progress=value.progress;p.generation=value.generation
        p.state='READY' if value.readiness=='READY' else 'NOT_READY';p.reason=value.reason
        # 준비/출력 제한은 즉시 적용하되, 유효 path/state 사본은 실제 불일치 때만 철회한다.
        if value.readiness=='NOT_READY' and value.reason in _STATE_INVALIDATIONS:self.states.invalidate(vm,app)
        return True

    def _retrying_function(self,key,reason,phase,now):
        """직전 양성 근거의 잔여 수명만 보존한다. READY·출력·새 TTL은 만들지 않는다."""
        failure=self.function_rejections.get(key)
        return bool(key[1]=='autonomous' and reason in _TRANSIENT_REJECTIONS and
            phase not in ('STOPPING','RESIDUAL') and failure and failure[0]==self.generation and
            failure[2]<_REJECTION_LIMIT and 0<=now-failure[1]<self.readiness_lease_s)

    def _function_failure_confirmed(self,vm,app,now):
        if app!='autonomous':return True
        if not self._bound(vm,now,app):return True
        key=vm,app;p=self.prepared[key];check=self.checks.get(key)
        if self.execution_phases.get(key) in ('STOPPING','RESIDUAL'):return True
        if key in self.deadline_misses and self.deadline_misses[key][0]==self.generation:
            return self._deadline_recovery_due(vm,now)
        if not check or not 0<=now-check[1]<=self.readiness_lease_s:return True
        if p.state=='READY' and self._ready(vm,app,now):
            # 계산은 정상이어도 명령 전달만 잠시 비었을 수 있다. READY 보고나
            # 거절된 명령 수신은 마지막 유효 출력에서 시작한 관찰 창을 갱신하지 않는다.
            anchors=[t for t in (self._last_forwarded_t,p.since) if t is not None]
            since=max(anchors) if anchors else None
            return since is None or now-since>=self.readiness_lease_s
        if p.state=='NOT_READY' and p.reason in _PLANNER_WAITS:
            work=self.completed_work.get(key)
            return not (work and work[0]==self.generation and 0<=now-work[1]<=self.readiness_lease_s)
        if p.state!='NOT_READY' or p.reason not in _TRANSIENT_REJECTIONS:return True
        rejected=self.function_rejections.get(key)
        if rejected is None or rejected[0]!=self.generation:return True
        ready=self.last_ready_work.get(key)
        since=min(rejected[1],ready[1]) if ready and ready[0]==self.generation else rejected[1]
        return rejected[2]>=_REJECTION_LIMIT or now-since>=self.readiness_lease_s

    def _waiting_function_output(self,now):
        p=self.prepared[self.active_vm,self.active_app]
        if p.state=='NOT_READY' and p.reason in _PLANNER_WAITS:
            if self._last_stop_t is None or now-self._last_stop_t>=self.timing.stop_resend_s:
                self._last_stop_t=now
                return [ForwardStop(self.generation)]
            return []
        return super()._waiting_function_output(now)

    def _qualified(self,vm,app,now,held=False):
        if vm in self.untrusted or self.authority.protected(vm):return False
        return self._qualification_ready(vm,app,now,held)

    def _qualification_ready(self,vm,app,now,held=False):
        # 보존한 hold 이력은 복귀 비용만 줄인다. 현재 거절 중인 B를 승계하거나
        # 현재 거절 중인 AUTO를 새로 활성화할 근거로 쓰지 않는다.
        observed=self.prepared.get((vm,app))
        if app=='autonomous' and observed is not None and observed.reason in _TRANSIENT_REJECTIONS:return False
        binding=self.bindings.current(vm,self.clock())
        if (binding and binding.get('app_state_models',{}).get(app)=='stateful' and
                self.states.evidence(vm,app) is None):return False
        if app=='manual' and self.authority.value.get('manual_input_profile') in ('manual-session-1','manual-input-2','manual-input-3'):
            sample=self.manual_input.current(now)
            if (sample is None or not self._manual_context(sample)) and not self._manual_zero_prepared():return False
        q=self.qualification.get((vm,app))
        return bool(q and self._bound(vm,now,app) and q[0]==self.generation and
            0<=now-q[1]<=self.readiness_lease_s and (not held or now-q[2]>=self.timing.ready_hold_s))

    def rejoin_ready(self,vm,app,now):
        return vm in self.untrusted and self.authority.protected(vm) and self._qualification_ready(vm,app,now,True)

    def _applied(self,vm,app,now):
        return self._qualified(vm,app,now)

    def step(self,now):
        if self.durable_write_available() and self._deferred_abandon is not None:
            generation,reason=self._deferred_abandon;self._deferred_abandon=None
            if generation==self.generation:self._abandon(reason)
        # Input/session loss is an output protection event, not a VM failure.
        # Clear the selected control so a new driver session can prepare without
        # first satisfying the old session's movement gate. No automatic rearm.
        if self.active_app=='manual' and self.movement and self.output_permitted and not self._manual_zero_prepared():
            sample=self.manual_input.current(now)
            if sample is None or sample.session!=self.movement.session or not self._manual_context(sample) or self.movement.blocked:
                self._authorized=False
                return self._change(Mode.SAFE_STOP,self.active_vm,'driver_session_unavailable',now)+self._output(now)
        if (not self.authority.error and self.active_app=='autonomous' and not self.output_permitted and
            self.phase in ('APPLIED','DISARMED','FAILED') and self._auto_exit_confirmed(self.active_vm,now)):
            self._authorized=False
            return self._change(Mode.SAFE_STOP,self.active_vm,'no_fresh_standby',now)+self._output(now)
        actions=super().step(now)
        if not self._record_standby_auto_recovery(now):return [ForwardStop(self.generation)]
        return actions

    def _record_standby_auto_recovery(self,now):
        """Persist a late exit or settle a recovery once its replacement is ready."""
        if self.authority.error or not self.durable_write_available() or self.phase not in ('APPLIED','DISARMED'):
            return True
        # A report may arrive after the lost command already triggered a handoff.
        # Persist the local exit without issuing a new control generation.
        prior=self.authority.value.get('auto_recovery',{})
        recoveries=dict(prior)
        for vm in self.vm_ids:
            active_auto=vm==self.active_vm and self.active_app=='autonomous'
            previous=prior.get(vm,{})
            exited=not active_auto and self._auto_exit_confirmed(vm,now)
            if not exited and not previous:continue
            fault=self._auto_recovery_identity(vm,now)
            if not fault:continue
            same_context=all(previous.get(key)==fault[key] for key in (*AUTO_RECOVERY_IDENTITY,'host'))
            if exited and not same_context:
                # A newly replaced AUTO that exits during preparation belongs
                # to the original attempt's VM escalation, not another rotation.
                recoveries[vm]={**fault,'trigger':'app-exit'}
            elif (same_context and previous['instance']!=fault['instance']
                  and (self.output_permitted and self._ready(vm,'autonomous',now) if active_auto
                       else self._qualified(vm,'autonomous',now,held=True))):
                del recoveries[vm]
        if recoveries!=prior:
            try:self.authority.update(auto_recovery=recoveries)
            except AuthorityError:
                self._fail('AUTHORITY_STORAGE_UNCERTAIN',now)
                return False
        return True
