"""home.set/status/return의 유한 actor 포트. M 검증+D 원 COMMITTED 뒤만 목표를 채택한다.

기존 operator.v2 parser/CLI와 async 관리 transport 연결은 A1 정상 병합 뒤 수행한다.
현재 STOP/원 Vehicle를 요청에 결박하며 새 지도 pose나 D 성공 응답을 발명하지 않는다.
"""
from hashlib import sha256
from uuid import UUID

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.route import RouteReason,route_owner_sha256,route_stop_facts_sha256,route_source_key_sha256
from .route_policy import RouteError,require,ZERO


class RouteGoalOwner:
    def __init__(self,route):
        self.route=route;self.o=route.o;self.data=route.data
        self.goal=None;self.pending=None;self.last_result=None

    def _waiting(self):
        value=self.o.runner.authority.value.get('current_operation')
        return value is not None and value['phase']=='WAIT' and self.o.stationary()

    def request_registration(self,raw,operator_row):
        request=decode(raw,schema.RoadRegistrationRequest);now=self.o.now
        if self.pending is not None:
            require(self.pending['raw']==raw,RouteReason.POOL_BUSY);return self.pending['raw']
        require(self.route.installed() and self.route.mapper_support()&2,RouteReason.UNSUPPORTED_PROFILE)
        require(self._waiting(),RouteReason.STOP_REQUIRED)
        call=operator_row['request'];key=operator_row['key']
        require(call['action']=='home.set' and self.o._known(key)==operator_row and not operator_row['terminal']
            and any(row['key']==key for row in self.o._rows()),RouteReason.DURABLE_NOT_READY)
        require(request.request_nonce==UUID(key.split(':')[-1]).bytes
            and request.issued_pi_us==int(key.split(':')[2])*1000
            and request.issued_pi_us<=now<request.valid_until_pi_us<=call['deadline_pi_ms']*1000,
            RouteReason.CLOCK_OR_EXPIRY)
        current=self.route.current_owner()
        require(current is not None and route_owner_sha256(current)==route_owner_sha256(request.owner),
            RouteReason.REQUEST_CONTEXT_CHANGED)
        expected_revision=0 if self.goal is None else self.goal.message().revision
        expected_sha=ZERO if self.goal is None else self.goal.digest
        require(request.expected_goal_revision==expected_revision and request.expected_goal_sha256==expected_sha,
            RouteReason.REQUEST_CONTEXT_CHANGED)
        world=self.data.world(request.world_raw_sha256,now)
        require(request.owner.source_key_sha256==route_source_key_sha256(world.source),RouteReason.SOURCE_UNAVAILABLE)
        raw_vehicle=self.o.runner.drive_link.vehicle_history.get(request.vehicle.sequence)
        facts=self.o.runner.control_facts()
        require(raw_vehicle is not None and raw_vehicle==encode(request.vehicle) and facts is not None
            and encode(facts)==encode(request.facts) and facts.HasField('stop') and facts.stop.state==3
            and now<min(facts.valid_until_pi_us,facts.stop.valid_until_pi_us)
            and facts.meter.vehicle_sequence==request.vehicle.sequence,RouteReason.STOP_REQUIRED)
        self.pending=dict(raw=bytes(raw),key=key,request=request)
        self.data.registration=bytes(raw)
        return bytes(raw)

    def complete_registration(self,raw_data,peer):
        """관리 transport에서 받은 원 RoadData. 보고만으로 기존 Authority 원장을 완료하지 않는다."""
        body=decode(raw_data,schema.RoadData)
        if self.last_result is not None and self.last_result['raw']==raw_data:return self.last_result['record']
        require(self.pending is not None,RouteReason.REQUEST_CONTEXT_CHANGED)
        request=self.pending['request'];now=self.o.now;current=self.route.current_owner()
        require(self._waiting() and now<request.valid_until_pi_us and current is not None
            and route_owner_sha256(current)==route_owner_sha256(request.owner),RouteReason.REQUEST_CONTEXT_CHANGED)
        require(body.WhichOneof('payload')=='start_goal' and body.publisher_kind==1 and body.HasField('durable'),
            RouteReason.DURABLE_NOT_READY)
        goal=body.start_goal
        require(goal.revision==request.expected_goal_revision+1
            and goal.geometry_scope_sha256==request.owner.geometry_scope_sha256
            and goal.vehicle_raw_sha256==sha256(encode(request.vehicle)).digest()
            and goal.world_raw_sha256==request.world_raw_sha256
            and goal.origin_vehicle_sequence==request.vehicle.sequence
            and goal.stop_command_sequence==request.facts.stop.command_sequence
            and goal.stop_report_sequence==request.facts.stop.applied_report_sequence
            and goal.stop_facts_sha256==route_stop_facts_sha256(request.facts)
            and request.issued_pi_us<=goal.registered_pi_us<=now
            and goal.terminal_profile_sha256==self.route.policy.terminal_profile_sha256,
            RouteReason.HASH_OR_BODY_MISMATCH)
        previous_pins=self.data.pins_for('goal');old=ZERO if self.goal is None else self.goal.digest
        # One actor turn: make the old goal replaceable only after exact request
        # checks. receive() verifies original current M/D facts before eviction.
        self.data.pin((value for value in previous_pins if value!=old),owner='goal')
        try:record=self.data.receive(raw_data,peer,now)
        except BaseException:
            self.data.pin(previous_pins,owner='goal');raise
        self.data.pin((*self.data.pins_for('goal'),record.digest),owner='goal');self.goal=record
        self.last_result=dict(raw=bytes(raw_data),record=record,key=self.pending['key'])
        self.pending=None;self.data.registration=None
        return record

    def status(self):
        current=self.route.current_owner();goal=None if self.goal is None else self.goal.message()
        compatible=goal is not None and current is not None and goal.geometry_scope_sha256==current.geometry_scope_sha256
        installed=bool(self.route.installed());supported=bool(installed and self.route.mapper_support()&2)
        revision=None
        reason=RouteReason.UNSUPPORTED_PROFILE if not installed else RouteReason.CAPABILITY_LOST if not supported else RouteReason.TARGET_UNAVAILABLE
        if supported and compatible:
            ref=self.route.eligibility_ref(2)
            if ref is None:reason=RouteReason.SPACE_UNKNOWN
            else:
                try:self.return_ready(expected_goal_revision=goal.revision,expected_goal_sha256=self.goal.digest,
                    ready_revision=ref.ready_revision)
                except RouteError as exc:reason=exc.reason
                else:revision=str(ref.ready_revision);reason=None
        return dict(supported=supported,
            goal_id=None if goal is None else str(UUID(bytes=goal.goal_id)),goal_revision='0' if goal is None else str(goal.revision),
            goal_sha256=ZERO.hex() if goal is None else self.goal.digest.hex(),
            geometry_scope_sha256=ZERO.hex() if goal is None else goal.geometry_scope_sha256.hex(),
            registered=goal is not None,current_compatible=compatible,
            blocked_reason=None if reason is None else reason.name,return_ready_revision=revision)

    def return_ready(self,*,expected_goal_revision,expected_goal_sha256,ready_revision):
        """현재 준비 조회만 한다. parking OFF·READY만으로 출발/Query를 만들지 않는다."""
        require(self._waiting(),RouteReason.STOP_REQUIRED)
        operation=self.o.runner.authority.value['current_operation']
        require(not operation['parking_enabled'],RouteReason.REQUEST_CONTEXT_CHANGED)
        require(self.goal is not None and self.goal.message().revision==expected_goal_revision
            and self.goal.digest==expected_goal_sha256,RouteReason.TARGET_UNAVAILABLE)
        ref=self.route.eligibility_ref(2)
        require(ref is not None and ref.eligible and ref.ready_revision==ready_revision,RouteReason.SPACE_UNKNOWN)
        require(self.o.now<ref.valid_until_pi_us,RouteReason.CLOCK_OR_EXPIRY)
        report=decode(self.route.eligibility[2][0],schema.RouteEligibility)
        require(report.goal_sha256==self.goal.digest,RouteReason.TARGET_UNAVAILABLE)
        self.route._witness(report.owner,report.witness,tuple(str(UUID(bytes=getattr(report.owner,k)))
            for k in ('vm','execution','app_instance')))
        return ref

    def tick(self):
        if self.pending is not None and self.o.now>=self.pending['request'].valid_until_pi_us:
            self.pending=None;self.data.registration=None
