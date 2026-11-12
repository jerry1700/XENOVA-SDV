"""기존 operator actor의 home 원장과 route 원문 관리 포트.

관리 owner가 실제로 붙기 전에는 명시 NOT_READY다. 같은 actor의 RouteOwner/Goal/Transition과
기존 Authority 원장을 사용하며 transport 수신·등록 요청만으로 성공/출력 권한을 만들지 않는다.
새 thread·동기 D I/O·별도 원장은 없다.
"""
from copy import deepcopy
from uuid import UUID,uuid4

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import (RouteReason,road_data_body,route_exchange_body,
    route_candidate_body,route_source_key_sha256)
from .operator_contract import OperatorError
from .route_goal import RouteGoalOwner
from .route_owner import RouteOwner,peer_key
from .route_policy import RouteBudgetPolicy,RouteError,ZERO,advance
from .route_transition import RouteTransitionOwner
from .route_episode import inactive_episode_history
from .machine import Mode


class RouteOperator:
    def __init__(self,operator):
        self.o=operator;self.r=operator.runner
        self.policy=getattr(self.r,'route_profile',None)
        if self.policy is not None and type(self.policy) is not RouteBudgetPolicy:
            raise ValueError('explicit installed route policy')
        self.route=None;self.goal=None;self.transition=None;self.data=None
        self.submit_management=None;self.continue_departure=None
        self.effect=None
        self.predecessor=None
        self.management=None

    def attach(self,route,goal,transition,*,submit_management):
        """실제 관리/Authority owner를 한 번 연결한다. callback의 성공 기본값은 없다."""
        if (self.route is not None or self.policy is None or type(route) is not RouteOwner
                or route.o is not self.o or route.policy is not self.policy
                or type(goal) is not RouteGoalOwner or goal.route is not route
                or type(transition) is not RouteTransitionOwner or transition.route is not route
                or not callable(submit_management)):
            raise ValueError('one route owner/actual management and Authority ports')
        self.route,self.goal,self.transition,self.data=route,goal,transition,route.data
        self.submit_management=submit_management;self.continue_departure=self._continue_departure

    @property
    def configured(self):
        return bool(self.policy is not None and self.o.operations.enabled and self.route is not None)

    def owns(self,key):return self.effect is not None and self.effect['key']==key and 'prepared' in self.effect

    def exit_required(self):
        value=self.o.operations.current()
        attempt=None if value is None else value['parking_attempt']
        return bool(attempt is not None and (attempt['result']=='PARKED'
            or attempt['result']=='CANCELLED' and attempt['execution_deadline']))

    def exit_inputs(self):
        """현재 정지한 ExitSpace 입력 준비다. 전체 signed path/BEGIN 승인을 대신하지 않는다."""
        now=self.o.now
        if not self.configured:return False,'ROUTE_NOT_READY',now,None
        ref=self.route.eligibility_ref(1)
        if ref is None or not ref.eligible or now>=ref.valid_until_pi_us:return False,'ROUTE_NOT_READY',now,None
        report=decode(self.route.eligibility[1][0],schema.RouteEligibility);peer=peer_key(report.owner)
        try:
            if not self.o.operations.supported(peer):raise RouteError(RouteReason.CAPABILITY_LOST)
            self.route._witness(report.owner,report.witness,peer)
            if report.task_space_kind!=2:raise RouteError(RouteReason.SPACE_UNKNOWN)
            space=self.data.get(6,report.exit_space_sha256).message()
            history=inactive_episode_history(self.o,report.witness.source,space.parking_attempt_id,space.parking_goal_sha256)
            if history.accepted_wire is None:raise RouteError(RouteReason.EPISODE_UNKNOWN)
        except RouteError as exc:return False,exc.reason.name,now,peer
        return True,None,ref.valid_until_pi_us,peer

    def readiness_context(self):
        if not self.configured or not self.exit_required():return None
        ref=self.route.eligibility_ref(1)
        semantic=self.route.semantic.get(1)
        return (None if ref is None else ref.ready_revision,semantic)

    def prepare_exit(self,row):
        call=row['request'];args=call['args'];value=self.o.operations.current()
        if args['mode']!='AUTO' or not self.exit_required():raise OperatorError('CONTEXT_CHANGED')
        if (value is None or int(args['expected_operation_revision'])!=value['revision']
                or int(args['expected_permission_revision'])!=value['permission_revision']):raise OperatorError('CONTEXT_CHANGED')
        if value['phase']!='WAIT' and not (value['phase']=='ACTIVE' and self.r.machine.mode is Mode.MANUAL):
            raise OperatorError('BUSY_NOT_ACCEPTED')
        ok,reason,_,_=self.o.operations.refresh_ready()
        if not ok:raise OperatorError(reason or 'ROUTE_NOT_READY')
        if int(args['ready_revision'])!=self.o.operations.ready_revision:raise OperatorError('CONTEXT_CHANGED')
        trigger=self.o.operations.trigger
        if args['trigger_sha256'] is not None and (trigger is None or trigger['key']!=row['key']
                or trigger['sha']!=args['trigger_sha256'] or self.o.now>=trigger['end'] or not trigger['current']()):
            raise OperatorError('UNAUTHENTICATED_TRIGGER')
        if self.effect is not None:raise OperatorError('BUSY_NOT_ACCEPTED')
        try:handle=self.route.prepare_departure(row,1,int(args['ready_revision']))
        except RouteError as exc:raise OperatorError(exc.reason.name) from exc
        self.effect=dict(key=row['key'],owner=handle.owner_raw,stage='ACCEPTED',request=None,prepared=handle)
        candidate=deepcopy(value)
        candidate.update(revision=advance(value['revision']),departure=row['key'],desired_mode='AUTO',phase='COUNTDOWN',
            countdown_until=self.o.now+int(self.r.machine.timing.resume_countdown_s*1e6),result='NONE')
        self.o.hold=True;self.o.hold_reason='drive.depart';self.r.machine.output_permitted=False
        self.r.machine._authorized=False;self.o._stop_driver()
        self.o.operations.last_state=self.o.operations.fresh_command=None
        return candidate

    def status(self):
        if self.goal is not None:return self.goal.status()
        return dict(supported=False,goal_id=None,goal_revision='0',goal_sha256=ZERO.hex(),
            geometry_scope_sha256=ZERO.hex(),registered=False,current_compatible=False,
            blocked_reason='UNSUPPORTED_PROFILE' if self.policy is None else 'SOURCE_UNAVAILABLE',
            return_ready_revision=None)

    def clock_status(self,peer,clock_end):
        if not self.configured:return None
        cap=self.r.mapper_operation_capability(self.o.now)
        if cap is None:return None
        reply=decode(cap.reply_bytes,schema.MapperClockReply)
        if not reply.HasField('supported_mapper_features') or not reply.supported_mapper_features&2:return None
        return self.route.status(peer,min(clock_end,cap.valid_until_pi_us))

    def pinned_copies(self):
        if self.predecessor is None or self.predecessor.accepted_wire is None:return set()
        return {decode(self.predecessor.source_state_raw,schema.DriveState).message_id}

    def prepare_state(self,state,peer,raw):
        current=self.o.missions.current()
        if not state.HasField('route'):
            return None if current is not None and current['kind']==3 and current['admitted'] else True
        if not self.configured:return None
        return self.route.prepare_state(state,peer,raw)

    def commit_state(self,prepared):
        if prepared is not True and prepared is not None:self.route.commit_state(prepared)

    def applied(self,peer,identity):
        if self.configured:self.route.applied(peer,identity)

    def independent_exit(self):
        if not self.configured or self.predecessor is None or self.predecessor.accepted_wire is None:return False
        saved=self.route.registry
        if saved is None or self.route.failed or saved['snapshot'].task_space_kind!=2:return False
        if saved['snapshot'].core.predecessor_episode_sha256!=self.predecessor.accepted_core_sha256:return False
        current=self.o.episodes.registry
        return current is None or current['snapshot'].core.state!=2

    def episode_genesis_allowed(self,current):
        """실제 새 ROAD_ROUTE admission에만 최초 EP genesis를 허용한다. EXIT/UNKNOWN은 NONE가 아니다."""
        if not self.configured or self.predecessor is None or self.predecessor.accepted_wire is None:return False
        saved=self.route.registry;previous=self.o.episodes.registry
        grant=self.o.missions.grant()
        if (saved is None or self.route.failed or saved['snapshot'].state!=2 or saved['snapshot'].task_space_kind!=1
                or grant is None or encode(grant)!=saved['grant'] or previous is None
                or previous['snapshot'].core.state!=1):return False
        target=saved['target'];history=decode(self.predecessor.snapshot_raw,schema.EpisodeSnapshot)
        return bool((target.mission_id,target.mission_revision)==(UUID(current['id']).bytes,current['revision'])
            and previous['snapshot'].acceptance==history.acceptance
            and previous['snapshot'].acceptance.accepted_core_sha256==self.predecessor.accepted_core_sha256)

    def command_allowed(self,peer,command):
        if command.get('producer_role')!='AUTO':return True
        current=self.o.missions.current()
        if current is None or current['kind']!=3:return True
        return bool(self.configured and self.route.command_allowed(peer,command))

    def output_deadline(self):
        current=self.o.missions.current()
        if current is None or current['kind']!=3:return None
        return 0 if not self.configured else self.route.output_deadline()

    def send_guest(self,peer,field,raw):
        """같은 원 reply/query를 현재 인증 Guest mailbox에 넣는다. 현재 권한으로 재포장하지 않는다."""
        types={'road_data':schema.RoadData,'route_query':schema.RouteQuery,'route_receipt':schema.RouteReceipt,
            'window_transition_receipt':schema.WindowTransitionReceipt}
        if field not in types:raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
        vm,execution,instance=peer
        binding=self.r.bindings.current(vm,self.r._clock())
        from xenova_mqtt.drive_binding import app_instance
        if binding is None or binding['execution_id']!=execution or app_instance(binding,'autonomous')!=instance:return False
        value=decode(raw,types[field]);link=schema.DriveLink(major=2)
        if field=='road_data':link.road_data.CopyFrom(value)
        else:
            link.route_exchange.major=1;getattr(link.route_exchange,field).CopyFrom(value)
        from .drive_link import inbound
        return self.r.drive_link.io.offer_route(inbound(vm),encode(link))

    def _forward_management(self,wire,message,peer):
        """원 관리 body/E를 단일 슬롯으로 전달한다. accepted 시각은 capture/birth 시각이 아니다."""
        now=self.o.now
        end=message.witness.valid_until_pi_us if message.DESCRIPTOR.name=='WindowTransitionRequest' else message.valid_until_pi_us
        if not now<end:raise RouteError(RouteReason.CLOCK_OR_EXPIRY)
        if self.management is not None:
            if self.management['wire']!=wire or self.management['peer']!=tuple(peer):raise RouteError(RouteReason.POOL_BUSY)
            return
        self.management=dict(wire=bytes(wire),peer=tuple(peer),accepted=now,end=end,
            key='route:'+message.request_nonce.hex())

    def _management_tick(self):
        item=self.management
        if item is None:return
        if self.o.now>=item['end']:
            self.management=None
            return
        value=decode(item['wire'],schema.DriveLink).route_exchange
        request=getattr(value,value.WhichOneof('payload'))
        try:self.route._current(request.owner,item['peer'])
        except RouteError:
            self.management=None;return
        if self.submit_management(item['wire'],item['accepted'],item['end'],item['key']):self.management=None

    def _lookup_reply(self):
        raw=self.data.lookup
        if raw is None:return False
        request=decode(raw,schema.RoadDataRequest)
        if self.o.now>=request.valid_until_pi_us:return False
        try:self.route._current(request.owner,peer_key(request.owner))
        except RouteError:
            self.data.finish_lookup(request.request_nonce);return False
        try:record=self.data.get(request.record_kind,request.record_sha256)
        except RouteError:return False
        if not self.send_guest(peer_key(request.owner),'road_data',record.delivery_envelope):return False
        self.data.finish_lookup(request.request_nonce)
        return True

    def _operation(self,call):
        if not self.configured:raise OperatorError('UNSUPPORTED_PROFILE' if self.policy is None else 'ROUTE_NOT_READY')
        value=self.o.operations.current()
        args=call['args']
        if (value is None or value['phase']!='WAIT' or not self.o.stationary()
                or int(args['expected_operation_revision'])!=value['revision']
                or int(args['expected_permission_revision'])!=value['permission_revision']):
            raise OperatorError('CONTEXT_CHANGED')
        current=self.route.current_owner()
        if (current is None or not self.route.supported(peer_key(current))
                or not self.o.operations.supported(peer_key(current))):raise OperatorError('SOURCE_UNAVAILABLE')
        return value,current

    def prepare(self,row):
        """원 사용자 CAS를 admission 전에 확인한다. 여기서는 M 요청/Query를 보내지 않는다."""
        call=row['request'];value,current=self._operation(call)
        args=call['args'];status=self.status()
        if (int(args['expected_goal_revision'])!=int(status['goal_revision'])
                or args['expected_goal_sha256']!=status['goal_sha256']):raise OperatorError('CONTEXT_CHANGED')
        if self.effect is not None:raise OperatorError('BUSY_NOT_ACCEPTED')
        if call['action']=='home.return':
            try:self.goal.return_ready(expected_goal_revision=int(args['expected_goal_revision']),
                expected_goal_sha256=bytes.fromhex(args['expected_goal_sha256']),ready_revision=int(args['ready_revision']))
            except RouteError as exc:raise OperatorError(exc.reason.name) from exc
            # 출발 writer가 준비되지 않은 설치를 요청 예약으로 바꾸지 않는다.
            if self.continue_departure is None:raise OperatorError('ROUTE_NOT_READY')
            if not self.o.operations.neutral():raise OperatorError('INPUT_NOT_NEUTRAL')
        self.effect=dict(key=row['key'],owner=encode(current),stage='ACCEPTED',request=None)
        if call['action']=='home.return':
            try:handle=self.route.prepare_departure(row,2,int(args['ready_revision']))
            except RouteError as exc:
                self.effect=None;raise OperatorError(exc.reason.name) from exc
            self.effect['prepared']=handle
            candidate=deepcopy(value)
            candidate.update(revision=advance(value['revision']),departure=row['key'],desired_mode='AUTO',
                phase='COUNTDOWN',countdown_until=self.o.now+int(self.r.machine.timing.resume_countdown_s*1e6),result='NONE')
            # 원 parking attempt/budget/follow 이력은 유지하고 수동 추진을 RAM에서 먼저 철회한다.
            self.o.hold=True;self.o.hold_reason='home.return';self.r.machine.output_permitted=False
            self.r.machine._authorized=False;self.o._stop_driver()
            self.o.operations.last_state=self.o.operations.fresh_command=None
            return candidate
        return None

    def ensure_query(self,row):
        """원 durable COUNTDOWN 뒤만 Query를 만든다. 아직 AUTO control/grant는 발급하지 않는다."""
        effect=self.effect
        if effect is None or effect['key']!=row['key']:raise OperatorError('CONTEXT_CHANGED')
        if self.route.query is not None and self.route.query['key']==row['key']:
            return self.route.query['raw']
        handle=effect.get('prepared')
        if handle is None:raise OperatorError('CONTEXT_CHANGED')
        current=self.route.current_owner()
        if current is None:return None
        ref=self.route.eligibility_ref(handle.purpose)
        if ref is None or not ref.eligible or self.o.now>=ref.valid_until_pi_us:return None
        query=schema.RouteQuery(major=1,query_id=uuid4().bytes,purpose=handle.purpose,goal_sha256=handle.goal_sha256,
            owner=current,issued_pi_us=int(row['key'].split(':')[2])*1000,
            valid_until_pi_us=row['request']['deadline_pi_ms']*1000,
            operator_request_nonce=UUID(row['key'].split(':')[-1]).bytes)
        raw=encode(query)
        try:self.route.begin_query(raw,row,prepared=handle)
        except RouteError as exc:raise OperatorError(exc.reason.name) from exc
        effect.update(stage='QUERY',request=raw)
        self.send_guest(peer_key(current),'route_query',raw)
        return raw

    def _finish(self,row,result,*,failed=False):
        terminal=self.o._terminal(row,result,failed=failed)
        self.effect=None
        return terminal,self.o._ledger(terminal)

    def _cancel_departure(self,row,reason):
        result=self.o.operations._cancel_departure(row,reason)
        self.effect=None
        return result

    def _continue_departure(self,row):
        effect=self.effect;machine=self.r.machine;value=self.o.operations.current()
        global_effect=self.o._effects.get(row['key'])
        if (value is None or global_effect is None or value['departure']!=row['key']
                or global_effect['fence']!=self.o.fence_revision):return self._cancel_departure(row,'CONTEXT_CHANGED')
        if effect.get('fault'):return self._cancel_departure(row,effect['fault'])
        if row['request']['action']=='drive.depart' and row['request']['args']['trigger_sha256'] is not None:
            trigger=self.o.operations.trigger
            if trigger is None or trigger['key']!=row['key'] or not trigger['current']():
                return self._cancel_departure(row,'INPUT_NOT_NEUTRAL')
        if effect['stage']=='APPLYING':
            if machine.generation!=effect['committed'] or machine.failure:
                return self._cancel_departure(row,'CONTEXT_CHANGED')
            fresh=self.o.operations.fresh_command
            saved=self.route.registry
            peer=self.o.operations.active_peer()
            admitted=(saved is not None and saved['state'] is not None and peer in saved['applied'])
            if (admitted and machine.phase=='APPLIED' and machine.output_permitted
                    and fresh is not None and fresh[0]==machine.generation):
                active=deepcopy(value);active.update(phase='ACTIVE',countdown_until=0)
                terminal=self.o._terminal(row,'APPLIED');self.effect=None
                return terminal,{**self.o._ledger(terminal),'current_operation':active}
            return None
        if effect['stage']=='COMMITTING':return None
        if machine.generation!=global_effect['generation'] or not self.o.stationary() or not self.o.operations.neutral():
            return self._cancel_departure(row,'CONTEXT_CHANGED')
        if self.ensure_query(row) is None:return None
        if self.o.now<value['countdown_until']:return None
        registration=self.route.registration()
        if registration is None:
            if self.route.failed:return self._cancel_departure(row,self.route.reason.name)
            return None
        candidate=decode(registration.candidate_raw,schema.RouteCandidate)
        snapshot=decode(registration.snapshot_raw,schema.RouteSnapshot)
        original_end=min(candidate.witness.valid_until_pi_us,snapshot.core.deadline_pi_us,
            row['request']['deadline_pi_ms']*1000)
        if self.o.now>=original_end:return self._cancel_departure(row,'CLOCK_OR_EXPIRY')
        fields=self.r.authority.issue_fields(Mode.AUTO,machine.active_vm,'operator_route_departure',self.r._clock(),
            min(machine.apply_timeout_s,max(.001,(row['request']['deadline_pi_ms']*1000-self.o.now)/1e6)),arm=True)
        fields['current_mission']=self.o.missions.route_record(row['request'],registration,fields['generation'],machine.active_vm)
        applying=deepcopy(value);applying.update(phase='APPLYING',countdown_until=0)
        fields.update(current_operation=applying,manual_hold=False)
        applying_row=deepcopy(row);applying_row['phase']='APPLYING'
        applying_row['original']['commit_generation']=fields['generation']
        fields.update(self.o._ledger(applying_row))
        def committed(ok,unknown):
            if not ok:
                effect['stage']='QUERY'
                return
            current=(self.o.now<original_end and global_effect['fence']==self.o.fence_revision
                and self.route.registration()==registration)
            machine._authorized=current
            self.r._emit(machine._adopt_control(Mode.AUTO,machine.active_vm,'operator_route_departure',self.r._clock()))
            self.o.pending[row['key']]=applying_row
            effect.update(stage='APPLYING',committed=fields['generation'])
            self.o.hold=not current;self.o.hold_reason=None if current else 'CONTEXT_CHANGED'
            if not current:
                effect['fault']='CONTEXT_CHANGED';self.o._stop('CONTEXT_CHANGED');return
            try:reply=self.route.commit_grant(candidate.request_nonce,encode(self.o.missions.grant()))
            except RouteError as exc:
                effect['fault']=exc.reason.name;self.o._stop(exc.reason.name);return
            self.o.missions.publish()
            self.send_guest(peer_key(candidate.owner),'route_receipt',reply)
        if self.o._proposal(row['key'],fields,'route_departure',row['request']['deadline_pi_ms']*1000,committed):
            effect['stage']='COMMITTING'
        return None

    def _registration(self,row):
        current=self.route.current_owner()
        if current is None or encode(current)!=self.effect['owner']:raise OperatorError('CONTEXT_CHANGED')
        facts=self.r.control_facts()
        if facts is None or not facts.HasField('stop') or facts.stop.state!=3:raise OperatorError('STOP_REQUIRED')
        raw=self.r.drive_link.vehicle_history.get(facts.meter.vehicle_sequence)
        if raw is None:raise OperatorError('SOURCE_UNAVAILABLE')
        vehicle=decode(raw,schema.VehicleSample)
        matches=[]
        for digest,saved in self.data.worlds.items():
            if self.o.now>=saved[2]:continue
            witness=decode(saved[0],schema.MapperWorldWitness)
            if route_source_key_sha256(witness.source)==current.source_key_sha256:matches.append((digest,witness))
        if not matches:raise OperatorError('SOURCE_UNAVAILABLE')
        world_sha,_=matches[-1];call=row['request'];args=call['args']
        request=schema.RoadRegistrationRequest(major=1,request_nonce=UUID(row['key'].split(':')[-1]).bytes,
            owner=current,expected_goal_revision=int(args['expected_goal_revision']),
            expected_goal_sha256=bytes.fromhex(args['expected_goal_sha256']),world_raw_sha256=world_sha,
            vehicle=vehicle,facts=facts,issued_pi_us=int(row['key'].split(':')[2])*1000,
            valid_until_pi_us=call['deadline_pi_ms']*1000)
        original=encode(request)
        self.goal.request_registration(original,row)
        link=schema.DriveLink(major=2);link.route_exchange.major=1
        link.route_exchange.road_registration_request.CopyFrom(request)
        return original,encode(link),request.issued_pi_us,request.valid_until_pi_us

    def continue_request(self,row):
        effect=self.effect
        if effect is None or effect['key']!=row['key']:raise OperatorError('CONTEXT_CHANGED')
        if effect.get('cancelled'):
            if 'prepared' in effect:return self._cancel_departure(row,'CANCELLED')
            return self._finish(row,'CANCELLED',failed=True)
        if self.o.now>=row['request']['deadline_pi_ms']*1000:
            if 'prepared' in effect:return self._cancel_departure(row,'DEADLINE_EXCEEDED')
            if self.goal is not None and self.goal.pending is not None:
                self.goal.pending=None;self.data.registration=None
            return self._finish(row,'DEADLINE_EXCEEDED',failed=True)
        if 'prepared' in effect:
            # 실제 출발 writer는 원 Query/Candidate와 같은 actor에서만 진행한다.
            return self.continue_departure(row)
        if effect['stage']=='ACCEPTED':
            try:original,wire,issued,end=self._registration(row)
            except RouteError as exc:raise OperatorError(exc.reason.name) from exc
            effect.update(stage='SUBMIT',request=original,wire=wire,issued=issued,end=end)
        if effect['stage']=='SUBMIT':
            if not self.submit_management(effect['wire'],effect['issued'],effect['end'],row['key']):return None
            effect['stage']='WAIT_M_D'
        if self.goal.last_result is not None and self.goal.last_result['key']==row['key']:
            return self._finish(row,'APPLIED')
        if effect['stage']=='WAIT_M_D' and self.goal.pending is None:
            return self._finish(row,'DEADLINE_EXCEEDED',failed=True)
        return None

    def receive_mapper(self,raw,peer):
        """관리 transport의 원 major2 union. 실제 publication/D 검산은 각 원 owner가 한다."""
        if not self.configured:raise RouteError(RouteReason.UNSUPPORTED_PROFILE)
        value=decode(raw,schema.DriveLink);kind=value.WhichOneof('payload')
        if kind=='road_data':
            body=road_data_body(raw)
            if self.goal.pending is not None and value.road_data.WhichOneof('payload')=='start_goal':
                return self.goal.complete_registration(body,peer)
            result=self.data.receive(body,peer,self.o.now);self._lookup_reply();return result
        if kind!='route_exchange':raise RouteError(RouteReason.UNSUPPORTED_PROFILE)
        original=route_exchange_body(raw)
        if original!=encode(value.route_exchange):raise RouteError(RouteReason.HASH_OR_BODY_MISMATCH)
        exchange=value.route_exchange;name=exchange.WhichOneof('payload')
        if name=='mapper_world_witness':return self.data.receive_world(encode(exchange.mapper_world_witness),peer,self.o.now)
        if name=='window_transition_receipt':
            result=self.transition.receive_receipt(encode(exchange.window_transition_receipt),peer)
            if result is not None:
                request=decode(self.transition.current['request'],schema.WindowTransitionRequest)
                self.send_guest(peer_key(request.owner),'window_transition_receipt',result)
            return result
        raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)

    def receive_guest(self,raw,peer):
        """인증 DriveLink peer가 전달한 원 bytes만 사용한다. guest는 M/D 결과를 생산하지 않는다."""
        if not self.configured:raise RouteError(RouteReason.UNSUPPORTED_PROFILE)
        value=decode(raw,schema.DriveLink);kind=value.WhichOneof('payload')
        if kind=='road_data':
            body=value.road_data
            if body.publisher_kind!=2 or peer_key(body.guest_owner)!=tuple(peer):
                raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
            return self.data.receive(road_data_body(raw),peer,self.o.now)
        if kind!='route_exchange':raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
        original=route_exchange_body(raw)
        if original!=encode(value.route_exchange):raise RouteError(RouteReason.HASH_OR_BODY_MISMATCH)
        body=value.route_exchange;name=body.WhichOneof('payload')
        if name not in ('route_eligibility','route_candidate','window_transition_request','road_data_request'):
            raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
        message=getattr(body,name)
        if peer_key(message.owner)!=tuple(peer):raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
        if name=='route_eligibility':return self.route.observe_eligibility(encode(body.route_eligibility),peer)
        if name=='route_candidate':
            candidate=body.route_candidate
            if candidate.action==1 and self.route.registry is None and self.route.pending is None:
                operation=self.o.operations.current()
                if (operation is None or operation['phase']!='COUNTDOWN'
                        or self.o.now<operation['countdown_until']):raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)
            if candidate.action==1:
                history=inactive_episode_history(self.o,candidate.witness.source,
                    candidate.core.parking_attempt_id if candidate.task_space_kind==2 else None,
                    candidate.core.parking_goal_sha256 if candidate.task_space_kind==2 else None)
                if (history.accepted_wire is None or candidate.task_space_kind==2
                        and history.accepted_core_sha256!=candidate.core.predecessor_episode_sha256):
                    raise RouteError(RouteReason.EPISODE_UNKNOWN)
                self.predecessor=history
            reply=self.route.candidate(route_candidate_body(raw),peer)
            if reply is not None:self.send_guest(peer,'route_receipt',reply)
            return reply
        if name=='window_transition_request':
            result=self.transition.request(encode(message),peer)
            if result is not None:
                receipt=self.transition.last.get('reply')
                if receipt is not None:self.send_guest(peer,'window_transition_receipt',receipt)
                else:self._forward_management(raw,message,peer)
            return result
        if name=='road_data_request':
            self.route._current(message.owner,peer)
            result=self.data.request_lookup(encode(message),self.o.now)
            if not self._lookup_reply():self._forward_management(raw,message,peer)
            return result
        raise RouteError(RouteReason.REQUEST_CONTEXT_CHANGED)

    def tick(self):
        if not self.configured:return
        if self.effect is not None:
            row=self.o._known(self.effect['key'])
            if row is None or row['terminal']:
                if self.route.prepared is not None:self.route.cancel(RouteReason.CANCELLED,stop=False)
                self.effect=None
        try:
            self.data.tick(self.o.now);self.goal.tick()
            receipt=self.transition.tick()
            if receipt is not None:
                request=decode(self.transition.current['request'],schema.WindowTransitionRequest)
                self.send_guest(peer_key(request.owner),'window_transition_receipt',receipt)
            self._management_tick();self._lookup_reply()
            self._adopt_current_grant()
            if self.o.missions.route_rebind_source() is None:self.route.tick()
            # 원 route/progress/meter 검사는 route_rebind_source가 수행한다. 실제 새 durable
            # ACK 전에는 후보를 보존할 뿐 새 대상의 출력·firstState를 승인하지 않는다.
            self._commit_phase_registration()
            self._terminal_wait()
        except RouteError as exc:self.cancel(exc.reason)

    def _adopt_current_grant(self):
        saved=self.route.registry
        if saved is None or self.route.failed or saved['snapshot'].state!=2:return
        journal=self.o.missions.route_journal
        if journal is not None and journal['explicit'] and journal['operation']=='mission_binding':return
        grant=self.o.missions.grant()
        if grant is None:
            if self.o.missions.route_rebind_source() is not None:return
            self.o.missions.revoke_route();self.o._stop('ROUTE_AUTHORITY_UNAVAILABLE');return
        raw=encode(grant)
        if raw==saved['grant']:return
        # 새 owner/gen/op의 durable ACK만 이 경로에 도달한다. 원 receipt는 보존한다.
        self.route.rebind_grant(raw)

    def _commit_phase_registration(self):
        pending=self.route.pending
        if (pending is None or pending['body'].action==1 or self.o.job is not None or self.o.worker.busy
                or not self.o.worker._results.empty() or self.o.unknown is not None
                or self.o.missions.route_rebind_source() is not None):return
        registration=self.route.registration()
        if registration is None:return
        try:
            raw=self.o.missions.commit_route_registration(registration)
            reply=self.route.commit_grant(pending['body'].request_nonce,raw)
        except (OperatorError,RouteError) as exc:
            # issuer 채택 뒤 실패도 과거 grant로 되돌리지 않는다. STOP은 fsync를 기다리지 않는다.
            self.o.missions.revoke_route();self.o._stop(getattr(exc,'code',str(exc)))
            return
        self.o.missions.publish()
        self.send_guest(peer_key(pending['body'].owner),'route_receipt',reply)

    def _terminal_wait(self):
        saved=self.route.registry
        if saved is None or self.route.failed or saved['snapshot'].state!=3 or saved['state'] is None:return
        peer=self.o.operations.active_peer()
        if peer not in saved['applied'] or not self.o.stationary():return
        result=saved['snapshot'].core.terminal_result
        self.o._stop('ROUTE_COMPLETED' if result==1 else 'ROUTE_CANCELLED' if result==2 else 'ROUTE_FAILED')

    def cancel(self,reason,*,stop=True):
        if not isinstance(reason,RouteReason):reason=RouteReason.__members__.get(str(reason),RouteReason.CANCELLED)
        if self.route is not None and not self.route.failed:self.route.cancel(reason,stop=stop)
        if self.goal is not None and self.goal.pending is not None:
            self.goal.pending=None;self.data.registration=None
        if self.effect is not None:self.effect['cancelled']=True
        self.management=None

