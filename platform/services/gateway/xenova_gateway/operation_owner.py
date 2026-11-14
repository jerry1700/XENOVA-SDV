"""단일 actor의 출발·주차 허용 설정과 비권위 입력 준비 진술.

내구 상태는 기존 Authority의 current_operation 한 개다. clock/preflight·완전 State·
실제 새 계산은 별도로 검산하며 READY나 Enter를 모터 출력 권한으로 바꾸지 않는다.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from xenova_drive_world import schema,encode,decode,operation_binding_equal
from xenova_drive_world.codec import STATE_KNOWN_FEATURES
from xenova_mqtt.drive_binding import app_instance
from .authority_journal import canonical
from .operator_contract import OperatorError
from .machine import Mode


NIL=bytes(16);U63=2**63-1
PHASES={'STOPPING':1,'WAIT':2,'COUNTDOWN':3,'APPLYING':4,'ACTIVE':5}
REASONS={None:0,'UNSUPPORTED_PROFILE':1,'STARTUP':2,'STOP_REQUIRED':3,'INPUT_NOT_NEUTRAL':4,
    'SOURCE_UNAVAILABLE':5,'PREFLIGHT_UNAVAILABLE':6,'PREFLIGHT_EXPIRED':7,'SCENE_UNAVAILABLE':8,
    'BUSY_NOT_ACCEPTED':9,'ROUTE_NOT_READY':10,'CONTEXT_CHANGED':11,'STORAGE_UNCERTAIN':12}


def advance(value):
    if type(value) is not int or not 0<value<U63:raise OperatorError('COUNTER_EXHAUSTED')
    return value+1


def binding_record(value):
    return dict(service_epoch=value.service_epoch.hex(),operation_revision=value.operation_revision,
        departure_nonce=value.departure_nonce.hex(),permission_revision=value.permission_revision,
        parking_enabled=value.parking_enabled)


def binding_message(value):
    if (type(value) is not dict or set(value)!={'service_epoch','operation_revision','departure_nonce','permission_revision','parking_enabled'}
            or type(value['parking_enabled']) is not bool):raise ValueError('exact operation binding record')
    if any(type(value[k]) is not int or not 0<value[k]<=U63 for k in ('operation_revision','permission_revision')):
        raise ValueError('operation binding revision')
    if any(type(value[k]) is not str or len(value[k])!=32 or value[k]!=value[k].lower()
            for k in ('service_epoch','departure_nonce')):raise ValueError('operation binding UUID bytes')
    return schema.OperationBinding(service_epoch=bytes.fromhex(value['service_epoch']),
        operation_revision=value['operation_revision'],departure_nonce=bytes.fromhex(value['departure_nonce']),
        permission_revision=value['permission_revision'],parking_enabled=value['parking_enabled'])


def validate_operation(value):
    if value is None:return
    fields={'version','service','revision','permission_revision','parking_enabled','departure','desired_mode',
        'phase','countdown_until','parking_attempt','follow','result','search_birth'}
    if type(value) is not dict or set(value)!=fields or value['version']!=1:raise ValueError('exact current operation')
    if str(UUID(value['service']))!=value['service']:raise ValueError('operation service')
    if any(type(value[k]) is not int or not 0<value[k]<=U63 for k in ('revision','permission_revision')):
        raise ValueError('operation revision')
    if (type(value['parking_enabled']) is not bool or value['phase'] not in PHASES
            or value['desired_mode'] not in ('NONE','AUTO','MANUAL')
            or type(value['countdown_until']) is not int or not 0<=value['countdown_until']<=U63
            or type(value['result']) is not str or len(value['result'])>64):raise ValueError('operation state')
    if value['departure'] is not None:
        from .ipc_contract import request_key
        if request_key(value['departure']) is None:raise ValueError('operation departure key')
    if value['phase'] in ('COUNTDOWN','APPLYING','ACTIVE') and value['departure'] is None:
        raise ValueError('operation original departure')
    if bool(value['countdown_until'])!=(value['phase']=='COUNTDOWN'):raise ValueError('operation countdown sentinel')
    from .mission_authority import validate_record
    for name in ('parking_attempt','follow'):
        record=value[name];validate_record(record)
        if record is not None and len(canonical(record))>4096:raise ValueError('operation mission partition')
    birth=value['search_birth']
    if birth is not None and (type(birth) is not dict or set(birth)!={'state','sha','calculation','issued','vehicle'}
            or len(bytes.fromhex(birth['sha']))!=32 or any(type(birth[k]) is not int or not 0<birth[k]<=U63
                for k in ('calculation','issued','vehicle'))):raise ValueError('operation original search birth')
    if birth is not None and str(UUID(birth['state']))!=birth['state']:raise ValueError('operation birth State ID')
    if len(canonical(value))>6144:raise ValueError('operation record capacity')


class OperationOwner:
    """actor 전용. peer4/report4·accepted State1·원 trigger1만 RAM에 보관한다."""
    def __init__(self,operator):
        self.o=operator;self.r=operator.runner;self.reports={};self.selected=False
        self.ready_revision=1;self.semantic=None;self.last_state=None;self.fresh_command=None
        self.trigger=None;self.search_birth=None;self.report_hwm={};self.support_fence=None

    @property
    def enabled(self):return bool(getattr(self.r,'operation_support',False))

    def current(self):
        value=self.r.authority.value.get('current_operation')
        return value if value is not None and value['service']==self.o.instance else None

    def startup(self):
        return dict(version=1,service=self.o.instance,revision=1,permission_revision=1,parking_enabled=False,
            departure=None,desired_mode='NONE',phase='STOPPING',countdown_until=0,parking_attempt=None,
            follow=None,result='NONE',search_birth=None)

    def binding(self,value=None):
        value=self.current() if value is None else value
        if value is None:return None
        return schema.OperationBinding(service_epoch=UUID(value['service']).bytes,operation_revision=value['revision'],
            departure_nonce=NIL if value['departure'] is None else UUID(value['departure'].split(':')[-1]).bytes,
            permission_revision=value['permission_revision'],parking_enabled=value['parking_enabled'])

    def active_peer(self):
        m=self.r.machine;row=self.r.bindings.current(m.active_vm,self.r._clock())
        return None if row is None else (m.active_vm,row['execution_id'],app_instance(row,'autonomous'))

    def supported(self,peer):
        features=self.o.episodes.peers.get(peer,0)
        return bool(self.enabled and self.o.episodes.supported(peer) and features&32 and not features&~STATE_KNOWN_FEATURES)

    def observe_probe(self,peer,probe):
        """인증된 원 Probe/P4 뒤 호출한다. 누락/실패는 report를 철회하고 clock은 계속한다."""
        old=self.reports.get(peer);self.reports.pop(peer,None)
        if not self.supported(peer):
            if self.selected and peer==self.active_peer():
                self.last_state=self.fresh_command=None;self.search_birth=None
                value=self.current()
                pending=any(not row['terminal'] and row['request']['action']=='drive.depart'
                    and row['request']['args']['mode']=='AUTO' for row in self.o.pending.values())
                automatic=value is not None and value['desired_mode']=='AUTO'
                if value is not None and (automatic or pending) and self.support_fence!=value['revision']:
                    self.support_fence=value['revision'];self.o._stop('OPERATION_SUPPORT_LOST')
            return False
        self.selected=True
        if not probe.HasField('auto_input_preflight'):return False
        body=probe.auto_input_preflight;m=self.r.machine;now=self.o.now
        lease=self.o.clocks.lease(peer,body.clock_probe_sequence,now,m.generation)
        interval=self.o.clocks.interval(peer,body.clock_probe_sequence,body.checked_guest_us,now,m.generation)
        trust=self.r.drive_link.trust;sample=self.r.drive_link.vehicle_history.get(body.vehicle_sequence)
        if lease is None or interval is None or trust is None or sample is None:return False
        vehicle=decode(sample,schema.VehicleSample);source=body.source
        end=min(lease.expires,interval[0]+100000,trust.expires_pi_us)
        maximum=getattr(self.r._sink,'_vehicle_maximum_age_us',None)
        if maximum is None:
            maximum=getattr(getattr(getattr(self.r._sink,'odometry',None),'config',None),'maximum_sample_age_us',None)
        if maximum is None:return False
        end=min(end,vehicle.pi_sample_us-vehicle.clock_error_us+maximum)
        current=self.r.vehicle_sample()
        if (body.control_generation!=m.generation or lease.digest!=body.clock_reply_sha256
                or body.checked_guest_us<lease.p4 or body.valid_until_pi_us>end
                or not now<body.valid_until_pi_us or current is None or vehicle.quality!=3
                or any(getattr(vehicle,k)!=current.get(k) for k in
                    ('host_boot','pi_session','device_session','odom_epoch','calibration'))
                or vehicle.host_boot!=self.r.authority.host or vehicle.gateway_instance!=self.o.instance
                or source.odom_epoch!=vehicle.odom_epoch or not source.world_sequence or not source.observation_sequence
                or any(getattr(source,k)!=getattr(trust,k) for k in
                    ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','clock_epoch','configuration','calibration'))
                or source.primitive!=bytes.fromhex(trust.primitive)
                or not vehicle.HasField('measured_steering_rad') and not
                    (vehicle.HasField('estimated_steering_rad') and vehicle.HasField('steering_error_rad'))):return False
        raw=encode(body)
        prior=self.report_hwm.get(peer)
        if prior is not None and (body.checked_guest_us<prior[0] or body.checked_guest_us==prior[0]
                and (raw!=prior[1] or old is None)):return False
        if peer not in self.reports and len(self.reports)>=4:self.reports.pop(next(iter(self.reports)))
        copied=schema.AutoInputPreflight();copied.CopyFrom(body)
        self.reports[peer]=dict(body=copied,raw=raw,generation=m.generation)
        if peer not in self.report_hwm and len(self.report_hwm)>=4:self.report_hwm.pop(next(iter(self.report_hwm)))
        self.report_hwm[peer]=(body.checked_guest_us,raw)
        return True

    def neutral(self):
        source=self.o.sources
        return bool(not source.keys and not source.moving and source.valid
            and (source.kind=='NEUTRAL_ONLY' or not source.blocked and source.state=='ARMED_NEUTRAL'
                and (source.kind!='HELD' or source.all_up>=2 and self.o.now<source.expires)))

    def inputs(self,mode='AUTO'):
        """Enter 전 현재 조건. 미래 grant/Prepared/fullState를 요구하지 않는다."""
        now=self.o.now;m=self.r.machine;value=self.current()
        if not self.enabled:return False,'UNSUPPORTED_PROFILE',now,None
        if not self.o.ready or value is None:return False,'STARTUP',now,None
        if self.o.unknown is not None:return False,'STORAGE_UNCERTAIN',now,None
        if not self.o.stationary():return False,'STOP_REQUIRED',now,None
        if not self.neutral():return False,'INPUT_NOT_NEUTRAL',now,None
        peer=self.active_peer()
        if (peer is None or m.mode is Mode.SAFE_STOP or m.failure or not m.transport_ok
                or m.authority.protected(m.active_vm)):return False,'SOURCE_UNAVAILABLE',now,None
        if mode=='MANUAL':return True,None,now+100000,peer
        attempt=value['parking_attempt']
        if attempt is not None and (attempt['result']=='PARKED' or attempt['result']=='CANCELLED' and attempt['execution_deadline']):
            routes=getattr(self.o,'routes',None)
            return (False,'ROUTE_NOT_READY',now,peer) if routes is None else routes.exit_inputs()
        report=self.reports.get(peer)
        if not self.supported(peer) or report is None:return False,'PREFLIGHT_UNAVAILABLE',now,None
        body=report['body']
        if report['generation']!=m.generation or now>=body.valid_until_pi_us:return False,'PREFLIGHT_EXPIRED',now,peer
        trust=self.r.drive_link.trust;current=self.r.vehicle_sample()
        if (trust is None or now>=trust.expires_pi_us or current is None or body.source.odom_epoch!=current.get('odom_epoch')
                or any(getattr(body.source,k)!=getattr(trust,k) for k in
                    ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','clock_epoch','configuration','calibration'))
                or body.source.primitive!=bytes.fromhex(trust.primitive)):
            return False,'SOURCE_UNAVAILABLE',now,peer
        capability=self.r.mapper_operation_capability(now,body.source)
        if capability is None:return False,'SOURCE_UNAVAILABLE',now,peer
        end=min(body.valid_until_pi_us,capability.valid_until_pi_us);runtime=self.r.scene_runtime
        scene=None if runtime is None else runtime.motion_grant(now)
        if value['parking_enabled'] and scene is None:return False,'SCENE_UNAVAILABLE',now,peer
        if scene is not None:end=min(end,scene.valid_until_pi_us)
        return True,None,end,peer

    def refresh_ready(self):
        value=self.current();ok,reason,end,peer=self.inputs()
        if (ok and value['phase']!='WAIT' and not
                (value['phase']=='ACTIVE' and self.r.machine.mode is Mode.MANUAL)):
            ok=False;reason='BUSY_NOT_ACCEPTED'
        source=None if peer not in self.reports else self.reports[peer]['body'].source
        scene=None if self.r.scene_runtime is None else self.r.scene_runtime.motion_grant(self.o.now)
        context=(peer,self.r.machine.generation,None if value is None else value['permission_revision'],
            None if value is None else value['phase'],ok,reason,
            None if source is None else tuple(getattr(source,k) for k in
                ('mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch','clock_epoch','configuration','calibration','reference_id')),
            None if scene is None else (bytes(scene.scene.admission_id),scene.scene.admission_revision))
        routes=getattr(self.o,'routes',None)
        if routes is not None:context+=(routes.readiness_context(),)
        if self.semantic is not None and context!=self.semantic:self.ready_revision=advance(self.ready_revision)
        self.semantic=context
        return ok,reason,end,peer

    def view(self):
        ok,reason,end,_=self.refresh_ready();value=self.current();binding=self.binding()
        return dict(contract='operation-status-1',supported=self.enabled and value is not None,host_boot_id=self.r.authority.host,
            gateway_instance_id=self.o.instance,generation=self.r.machine.generation,pi_now_us=str(self.o.now),
            binding=None if binding is None else dict(service_epoch=str(UUID(bytes=binding.service_epoch)),
                operation_revision=str(binding.operation_revision),departure_nonce=str(UUID(bytes=binding.departure_nonce)),
                permission_revision=str(binding.permission_revision),parking_enabled=binding.parking_enabled),
            vehicle_phase='STOPPING' if value is None else value['phase'],ready_revision=str(self.ready_revision),
            auto_ready=ok,blocked_reason=reason,valid_until_pi_us=str(max(self.o.now,end)),
            active_request_id=None if value is None else value['departure'],
            countdown_until_pi_us=str(0 if value is None else value['countdown_until']),
            desired_mode='NONE' if value is None else value['desired_mode'])

    def clock_status(self,peer,probe,clock_end):
        if not self.supported(peer) or self.current() is None:return None
        ok,reason,end,active=self.refresh_ready();value=self.current()
        # READY is tied to this exact Probe's current report, never another peer's report.
        report=self.reports.get(peer)
        ready=bool(ok and peer==active and report is not None and probe.HasField('auto_input_preflight')
            and report['raw']==encode(probe.auto_input_preflight))
        end=min(clock_end,end) if ready else clock_end
        if self.o.now>=end:return None
        return schema.OperationStatus(major=1,binding=self.binding(),vehicle_phase=PHASES[value['phase']],
            ready_revision=self.ready_revision,auto_ready=ready,blocked_reason=0 if ready else REASONS.get(reason,5) or 5,
            valid_until_pi_us=end,active_request_nonce=NIL if value['departure'] is None else UUID(value['departure'].split(':')[-1]).bytes,
            countdown_until_pi_us=value['countdown_until'],desired_mode={'NONE':0,'AUTO':1,'MANUAL':2}[value['desired_mode']])

    def prepare(self,row):
        call=row['request'];action=call['action'];args=call['args'];value=self.current()
        if not self.enabled or value is None:raise OperatorError('UNSUPPORTED_PROFILE')
        if int(args['expected_permission_revision'])!=value['permission_revision']:raise OperatorError('CONTEXT_CHANGED')
        if action=='parking.permission.set':
            if args['enabled']==value['parking_enabled']:return None
            next_value=deepcopy(value);next_value['permission_revision']=advance(value['permission_revision'])
            next_value['parking_enabled']=args['enabled']
            current=self.o.missions.current();state=None if self.last_state is None else self.last_state[0]
            performing=bool(current is not None and current['kind']==2 and current['phase']!='HOLD'
                and (current['execution_deadline'] or state is not None and state['kind']==2 and state['mission_phase'] not in (0,1)))
            registry=self.o.episodes.registry
            if (not (not args['enabled'] and performing) and registry is not None
                    and registry['request'] is not None and registry['state'] is None):raise OperatorError('BUSY_NOT_ACCEPTED')
            if not args['enabled'] and performing:
                self.o._stop('PARKING_PERMISSION_OFF')
                next_value.update(phase='STOPPING',desired_mode='NONE',countdown_until=0,result='CANCELLED')
            return next_value
        if int(args['expected_operation_revision'])!=value['revision']:raise OperatorError('CONTEXT_CHANGED')
        ok,reason,_,_=self.refresh_ready()
        if value['phase'] not in ('WAIT','ACTIVE') or value['phase']=='ACTIVE' and self.r.machine.mode is not Mode.MANUAL:
            raise OperatorError('BUSY_NOT_ACCEPTED')
        if not ok and args['mode']=='AUTO':raise OperatorError(reason or 'NOT_READY')
        if args['mode']=='MANUAL':
            ok,reason,_,_=self.inputs('MANUAL')
            if not ok:raise OperatorError(reason or 'NOT_READY')
        if int(args['ready_revision'])!=self.ready_revision:raise OperatorError('CONTEXT_CHANGED')
        digest=args['trigger_sha256']
        if digest is not None and (self.trigger is None or self.trigger['key']!=row['key']
                or self.trigger['sha']!=digest or self.o.now>=self.trigger['end'] or not self.trigger['current']()):
            raise OperatorError('UNAUTHENTICATED_TRIGGER')
        next_value=deepcopy(value);next_value.update(revision=advance(value['revision']),departure=row['key'],
            desired_mode=args['mode'],phase='COUNTDOWN' if args['mode']=='AUTO' else 'APPLYING',
            countdown_until=self.o.now+int(self.r.machine.timing.resume_countdown_s*1e6) if args['mode']=='AUTO' else 0,
            parking_attempt=None,follow=None,search_birth=None,result='NONE')
        # This actor turn withdraws manual propulsion before any fsync/countdown.
        self.o.hold=True;self.o.hold_reason='drive.depart';self.r.machine.output_permitted=False
        self.r.machine._authorized=False;self.o._stop_driver()
        self.last_state=self.fresh_command=None
        return next_value

    def safety_off(self,call):
        if call['action']!='parking.permission.set' or call['args']['enabled']:return False
        value=self.current();mission=self.o.missions.current()
        state=None if self.last_state is None else self.last_state[0]
        return bool(self.enabled and value is not None and int(call['args']['expected_permission_revision'])==value['permission_revision']
            and mission is not None and mission['kind']==2 and mission['phase']!='HOLD'
            and (mission['execution_deadline'] or state is not None and state['kind']==2 and state['mission_phase'] not in (0,1)))

    def manual_operation(self,call):
        value=deepcopy(self.current())
        value.update(revision=advance(value['revision']),departure=call['request_id'],desired_mode='MANUAL',
            phase='APPLYING',countdown_until=0,result='NONE')
        return value

    def augment(self,changes):
        """foreground와 원 parking attempt를 같은 journal CAS에 저장한다."""
        if not self.enabled:return changes
        value=deepcopy(changes.get('current_operation',self.current()))
        if value is None:return changes
        mission=changes.get('current_mission')
        previous=self.o.missions.current()
        if (mission is None and previous is not None and previous['admitted'] and value['departure'] is not None
                and previous.get('operation_binding')!=binding_record(self.binding(value))):
            mission=deepcopy(previous);mission['grant_revision']=advance(mission['grant_revision'])
        if mission is not None:
            mission=deepcopy(mission)
            if mission['admitted'] and value['departure'] is not None:
                mission['operation_binding']=binding_record(self.binding(value))
            changes={**changes,'current_mission':mission}
            original_service=mission.get('operation_binding',{}).get('service_epoch',UUID(self.o.instance).hex)
            if mission['kind']==2 and original_service==UUID(self.o.instance).hex:value['parking_attempt']=deepcopy(mission)
            elif mission['admitted']:value['follow']=deepcopy(mission)
            if (mission['phase']=='HOLD' and mission.get('operation_binding') is not None
                    and mission['operation_binding']['departure_nonce']==self.binding(value).departure_nonce.hex()):
                value.update(phase='STOPPING',countdown_until=0,desired_mode='NONE',result=mission['result'])
        if len(canonical(value))>6144:raise OperatorError('HISTORY_FULL')
        validate_operation(value)
        return {**changes,'current_operation':value}

    def _finish(self,row,result,changes=None,*,failed=False):
        row=self.o._terminal(row,result,failed=failed)
        return row,{**self.o._ledger(row),**(changes or {})}

    def _cancel_departure(self,row,reason):
        self.o._stop(reason);value=deepcopy(self.current())
        value.update(phase='STOPPING',desired_mode='NONE',countdown_until=0,result=reason)
        changes={'current_operation':value};mission=self.o.missions.fence('FAILED')
        if mission is not None:changes['current_mission']=mission
        return self._finish(row,reason,changes,failed=True)

    def _parking_usable(self,record):
        now=self.o.now;used=self.o.missions._meter_distance(record)
        return bool(record['result']=='NONE' and record['phase']=='SEARCH' and not record['execution_deadline']
            and now<min(record['deadline'],record['search_deadline']) and used is not None and used<10)

    def continue_request(self,row):
        call=row['request'];effect=self.o._effects[row['key']];m=self.r.machine;value=self.current()
        if value is None:return self._finish(row,'CONTEXT_CHANGED',failed=True)
        if call['action']=='parking.permission.set':
            if self.o.now>=call['deadline_pi_ms']*1000:return self._finish(row,'DEADLINE_EXCEEDED',failed=True)
            changes={};mission=self.o.missions.current();attempt=value['parking_attempt']
            if not call['args']['enabled'] and mission is not None and mission['kind']==2 and mission['phase']!='HOLD':
                state=None if self.last_state is None else self.last_state[0]
                performing=bool(mission['execution_deadline'] or state is not None and state['kind']==2 and state['mission_phase'] not in (0,1))
                if performing:
                    self.o._stop('PARKING_PERMISSION_OFF');changes['current_mission']=self.o.missions.fence()
                    value=deepcopy(value);value.update(phase='STOPPING',desired_mode='NONE',countdown_until=0,result='CANCELLED')
                    changes['current_operation']=value
                elif value['follow'] is not None:
                    follow=self.o.missions.rebind(value['follow'],m.generation,m.active_vm)
                    follow['grant_revision']=advance(max(follow['grant_revision'],mission['grant_revision']))
                    changes['current_mission']=follow
            elif (call['args']['enabled'] and value['phase']=='ACTIVE' and value['desired_mode']=='AUTO'
                    and mission is not None and mission['kind']==1 and attempt is not None and self._parking_usable(attempt)):
                resumed=self.o.missions.rebind(attempt,m.generation,m.active_vm)
                resumed['grant_revision']=advance(max(resumed['grant_revision'],mission['grant_revision']))
                changes['current_mission']=resumed
            return self._finish(row,'APPLIED',changes)
        if self.o.now>=call['deadline_pi_ms']*1000:return self._cancel_departure(row,'DEADLINE_EXCEEDED')
        if value['departure']!=row['key'] or effect['fence']!=self.o.fence_revision:return self._cancel_departure(row,'CONTEXT_CHANGED')
        trigger=self.trigger
        if call['args']['trigger_sha256'] is not None and (trigger is None or trigger['key']!=row['key'] or not trigger['current']()):
            return self._cancel_departure(row,'INPUT_NOT_NEUTRAL')
        if effect['stage']=='APPLYING':
            if m.generation!=effect['committed'] or m.failure:return self._cancel_departure(row,'CONTEXT_CHANGED')
            fresh=self.fresh_command
            applied=m.phase=='APPLIED' and m.output_permitted
            if applied and (call['args']['mode']=='MANUAL' or fresh is not None and fresh[0]==m.generation):
                value=deepcopy(value);value.update(phase='ACTIVE',countdown_until=0)
                return self._finish(row,'APPLIED',{'current_operation':value})
            return None
        if effect['stage']=='COMMITTING':return None
        ok,reason,_,_=self.inputs(call['args']['mode'])
        if not ok:return self._cancel_departure(row,reason or 'NOT_READY')
        if m.generation!=effect['generation']:return self._cancel_departure(row,'CONTEXT_CHANGED')
        if value['phase']=='COUNTDOWN' and self.o.now<value['countdown_until']:return None
        mode=Mode.AUTO if call['args']['mode']=='AUTO' else Mode.MANUAL
        fields=self.r.authority.issue_fields(mode,m.active_vm,'operator_drive.depart',self.r._clock(),
            min(m.apply_timeout_s,max(.001,(call['deadline_pi_ms']*1000-self.o.now)/1e6)),arm=True,
            manual=dict(manual_session=self.o.sources.sid,manual_policy='NEUTRAL_THEN_ACTION') if mode is Mode.MANUAL else None)
        if mode is Mode.AUTO:
            mission=self.o.missions.start(call,1,admitted=True)
            fields['current_mission']=self.o.missions.rebind(mission,fields['generation'],m.active_vm)
        else:
            mission=self.o.missions.fence()
            if mission is not None:fields['current_mission']=mission
        value=deepcopy(value);value.update(phase='APPLYING',countdown_until=0)
        fields.update(current_operation=value,manual_hold=mode is Mode.MANUAL)
        applying=deepcopy(row);applying['phase']='APPLYING';applying['original']['commit_generation']=fields['generation']
        fields.update(self.o._ledger(applying))
        def committed(ok,unknown):
            if not ok:
                effect['stage']='OPERATION';return
            current=self.o.now<call['deadline_pi_ms']*1000 and effect['fence']==self.o.fence_revision
            m._authorized=current
            self.r._emit(m._adopt_control(mode,m.active_vm,'operator_drive.depart',self.r._clock()))
            self.o.pending[row['key']]=applying;effect.update(stage='APPLYING',committed=fields['generation'])
            # New control is still PENDING; actual fullState/apply/fresh calc gate remains below.
            self.o.hold=not current;self.o.hold_reason=None if current else 'CONTEXT_CHANGED'
            if mode is Mode.MANUAL:self.o.sources.enable_pulse()
            self.o.missions.publish()
        if self.o._proposal(row['key'],fields,'operation_depart',call['deadline_pi_ms']*1000,committed):effect['stage']='COMMITTING'
        return None

    def tick(self):
        if not self.enabled:return
        value=self.current()
        if value is None:return
        self.refresh_ready()
        if self.trigger is not None:
            row=self.o._known(self.trigger['key'])
            if row is None or row['terminal']:self.trigger=None
        if self.o.job is not None or self.o.worker.busy or self.o.unknown is not None:return
        if value['phase']=='STOPPING' and self.o.stationary():
            stopped=deepcopy(value);stopped.update(phase='WAIT',departure=None,desired_mode='NONE',countdown_until=0,
                revision=advance(value['revision']))
            changes={'current_operation':stopped};mission=self.o.missions.current()
            if mission is not None and mission['admitted']:changes['current_mission']=self.o.missions.fence()
            self.o._proposal('operation-wait',changes,'operation_wait',self.o.now+10000000,lambda ok,unknown:None)
            return
        if value['phase']=='ACTIVE' and value['desired_mode']=='AUTO' and self.r.machine.mode is not Mode.AUTO:
            self.o._stop('AUTO_INPUT_UNAVAILABLE')
        if value['phase']=='ACTIVE' and self.o.hold:
            stopped=deepcopy(value);stopped.update(phase='STOPPING',countdown_until=0,desired_mode='NONE',result=self.o.hold_reason or 'CANCELLED')
            self.o._proposal('operation-stop',{'current_operation':stopped},'operation_stop',self.o.now+10000000,lambda ok,unknown:None)
            return
        if self.search_birth is not None:
            state,birth,meter=self.search_birth;self.search_birth=None
            if (value['phase']!='ACTIVE' or not value['parking_enabled'] or value['parking_attempt'] is not None
                    or not self.proof_current(state) or self.o.now>=birth['issued']+100000):return
            current=self.o.missions.current();runtime=self.r.scene_runtime
            scene=None if runtime is None else runtime.motion_grant(self.o.now)
            if current is None or current['kind']!=1 or scene is None:return
            attempt=deepcopy(current);attempt.update(kind=2,strategy=1 if getattr(self.r,'operation_parking_strategy','REVERSE')=='REVERSE' else 2,
                grant_revision=advance(current['grant_revision']),issued=birth['issued'],deadline=birth['issued']+120000000,
                search_deadline=birth['issued']+90000000,execution_deadline=0,corrections=0,execution_distance=0.,search_distance=0.,
                phase='SEARCH',result='NONE',goal=None,reservations=[],scene_id=str(UUID(bytes=scene.scene.admission_id)),
                scene_revision=scene.scene.admission_revision,
                search_meter=dict(epoch=meter.meter_epoch,total_ticks=meter.total_ticks,metres_per_tick=meter.metres_per_tick,
                    gap_revision=meter.gap_revision,vehicle_sequence=meter.vehicle_sequence),execution_meter=None)
            next_value=deepcopy(value);next_value.update(follow=deepcopy(current),parking_attempt=attempt,search_birth=birth)
            self.o._proposal('operation-search',{'current_operation':next_value,'current_mission':attempt},'operation_search',
                birth['issued']+100000,lambda ok,unknown:self.o.missions.publish() if ok else None)

    def state_allowed(self,state):
        if not self.enabled:return True
        current=self.current();peer=(state.source_vm,state.source_execution,state.source_app_instance)
        return bool(current is not None and current['departure'] is not None and current['revision']!=self.support_fence
            and current['desired_mode']=='AUTO' and current['phase'] in ('APPLYING','ACTIVE')
            and peer==self.active_peer() and self.supported(peer)
            and state.mission.HasField('operation_binding')
            and operation_binding_equal(state.mission.operation_binding,self.binding()))

    def observe_state(self,state,accepted_wire):
        if self.enabled and self.state_allowed(state):
            # Full bytes remain in the existing copy4 owner. This key/proof adds no fifth full copy.
            self.last_state=(dict(id=state.message_id,sha=sha256(accepted_wire).hexdigest(),
                vm=state.source_vm,peer=(state.source_vm,state.source_execution,state.source_app_instance),
                generation=state.source_generation,calculation=state.calculation_sequence,
                plan=state.path_revision,phase=state.maneuver_revision,kind=state.mission.kind,
                mission_phase=state.mission.phase,binding=binding_record(state.mission.operation_binding)),self.o.now)
            # OFF 수락 뒤 worker가 저장하는 동안 실제 phase가 수행 단계로 넘어온 경우도 즉시 철회한다.
            if state.mission.kind==2 and state.mission.phase not in (0,1):
                target=dict(id=str(UUID(bytes=state.mission.mission_id)),revision=state.mission.mission_revision)
                pending=any(not row['terminal'] and row['request']['action']=='parking.permission.set'
                    and not row['request']['args']['enabled'] and row['target']==target for row in self.o.pending.values())
                if pending and self.o.hold_reason!='PARKING_PERMISSION_OFF':self.o._stop('PARKING_PERMISSION_OFF')

    def proof_current(self,state):
        return bool(state['id'] in self.r.drive_link.copies and self.current() is not None
            and self.current()['revision']!=self.support_fence
            and state['peer']==self.active_peer() and self.supported(state['peer'])
            and state['vm']==self.r.machine.active_vm and state['generation']==self.r.machine.generation
            and state['binding']==binding_record(self.binding()))

    def command_allowed(self,source):
        if not self.enabled:return True
        value=self.current();command=source.get('command',source)
        if value is None or value['phase'] not in ('APPLYING','ACTIVE') or value['desired_mode']!='AUTO' or self.last_state is None:return False
        state=self.last_state[0];m=self.r.machine
        return bool(self.proof_current(state) and int(command.get('plan_revision',0))==state['plan']
            and int(command.get('phase_revision',0))==state['phase']
            and int(command.get('calculation_sequence',0))>state['calculation'])

    def observe_command(self,source):
        if not self.enabled or not self.command_allowed(source):return
        self.fresh_command=(self.r.machine.generation,int(source['command']['calculation_sequence']))
        value=self.current();state=self.last_state[0];command=source['command']
        if (value['parking_enabled'] and value['parking_attempt'] is None and state['kind']==1
                and self.search_birth is None and value['phase']=='ACTIVE'):
            meter=self.o.episodes._meter(int(command['vehicle_sequence']),historical=True)
            if meter is not None:
                self.search_birth=(state,dict(state=state['id'],sha=state['sha'],
                    calculation=int(command['calculation_sequence']),issued=source['issued_pi_us'],vehicle=meter.vehicle_sequence),meter)

    def mapper_gate(self):
        if not self.enabled or self.current() is None:return None
        capability=self.r.mapper_operation_capability(self.o.now,self.r.drive_link.trust)
        if capability is None:return None
        value=self.current();m=self.r.machine;view=self.r.device_view()
        sample=view.get('vehicle_sample');facts=self.r.control_facts(view=view)
        if sample is None or facts is None:return None
        wait=bool(value['phase']=='WAIT' and self.o.hold and self.o.stationary()
            and facts.HasField('stop') and facts.stop.state==3 and facts.stop.control_generation==m.generation)
        manual=bool(m.mode is Mode.MANUAL and m.output_permitted and not self.o.hold and self.o.sources.valid and not self.o.sources.blocked)
        if not wait and not manual:return None
        end=min(facts.valid_until_pi_us,self.o.now+100000,capability.valid_until_pi_us)
        if wait:end=min(end,facts.stop.valid_until_pi_us)
        if end<=self.o.now:return None
        return schema.PiOperationGate(gateway_instance=UUID(self.o.instance).bytes,control_generation=m.generation,
            operation_revision=value['revision'],mode=2 if m.mode is Mode.MANUAL else 1 if m.mode is Mode.AUTO else 0,
            wait_confirmed=wait,manual_local_admitted=manual,vehicle_sequence=sample['sequence'],
            stop_command_sequence=facts.stop.command_sequence if wait else 0,valid_until_pi_us=end)
