"""Route owner 의미시험의 명시적 software leaf와 실제 clock/meter.

원 P4 protocol·cache·codec·absolute encoder 누적·RouteOwner는 실제 제품이다.
현재 A1 actor 미병합 구간의 Authority ACK/M publication/D membership/apply/geometry
tracker는 아래 typed leaf다. native C·실물 수락·내구 journal 증거로 세지 않는다.
"""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID,uuid4

from xenova_drive_world import schema,encode,decode
from xenova_gateway.absolute_meter import AbsoluteWheelMeter
from xenova_gateway.episode import EpisodeOwner
from xenova_gateway.guest_clock import GuestClockOwner,GuestClockPolicy,OwnedPreparedClock,SentClock
from xenova_gateway.route_owner import RouteOwner,peer_key
from xenova_gateway.route_policy import NIL,ZERO
from route_fixtures import route_case,accepted_route
from test_route_data_runtime import data_owner,delivery,records,mapper,policy,HASH,IDS


class RouteRig:
    def __init__(self):
        self.now=1010000;self.base=route_case();self.vehicle=deepcopy(self.base['vehicle'])
        self.owner_context=deepcopy(self.base['owner'])
        self.owner_context.operation_binding.departure_nonce=IDS(72)
        self.peer=peer_key(self.owner_context);self.grant=None;self.stop_calls=[];self.app_evidence={}
        self.authority=SimpleNamespace(host=self.vehicle.host_boot,value={})
        self.machine=SimpleNamespace(active_app='autonomous',states=SimpleNamespace(evidence=self.evidence))
        self.provider=SimpleNamespace(vehicle_history={},copies={},trust=None)
        self.meter=AbsoluteWheelMeter(SimpleNamespace(calibration=self.vehicle.calibration,encoder_bits=32,
            metres_per_tick=.001,metres_per_tick_upper=.001,maximum_sample_age_us=200000),self.vehicle.host_boot)
        old=deepcopy(self.vehicle);old.sequence-=1;old.source_sample-=1;old.source_us-=20000;old.pi_sample_us-=20000
        old.left_ticks-=1;old.right_ticks-=1;self.record_meter(old);self.record_meter(self.vehicle)
        self.operator=SimpleNamespace(now=self.now,instance=self.vehicle.gateway_instance,hold=False,
            runner=SimpleNamespace(authority=self.authority,drive_link=self.provider,machine=self.machine,
                device_view=self.device_view,scene_runtime=None,control_facts=lambda:None),
            missions=SimpleNamespace(grant=lambda:self.grant),clocks=GuestClockOwner(
                GuestClockPolicy(5,10,10000,20000,1000000),self.vehicle.host_boot,self.vehicle.gateway_instance))
        self.operator.episodes=SimpleNamespace(o=self.operator,r=self.operator.runner,
            peers={self.peer:127},registry=None)
        self.operator.episodes._meter=lambda sequence=None,**kwargs:EpisodeOwner._meter(
            self.operator.episodes,sequence,**kwargs)
        self.operator._known=lambda key:self.row if getattr(self,'row',{}).get('key')==key else None
        self.operator._rows=lambda:[self.row] if hasattr(self,'row') else []
        self.clock_sequence=0;self.operator.clocks.register_peers((self.peer,));self.confirm_clock()
        self.data=data_owner();self.published_worlds={}
        self.data.world_current=lambda value,peer,now:self.published_worlds.get(value.world_raw_sha256)
        self.case=self.current_case()
        self.publish_world(self.case)
        for record in records(self.case):
            if record.DESCRIPTOR.name!='RoutePlan':self.data.receive(delivery(self.case,record),('typed-M',),self.now)
        self.route=RouteOwner(self.operator,policy(self.case),self.data,current_owner=lambda:self.owner_context,
            installed=lambda:True,mapper_support=lambda:2,stop_auto=self.stop_calls.append,
            terminal_check=lambda candidate,facts,now:False,progress_check=self.physical_progress)

    def record_meter(self,value):
        self.meter.record(pi_session=value.pi_session,device_session=value.device_session,odom_epoch=value.odom_epoch,
            sequence=value.sequence,source_sample=value.source_sample,source_us=value.source_us,left=value.left_ticks,
            right=value.right_ticks,pi_sample_us=value.pi_sample_us,clock_error_us=value.clock_error_us)
        self.provider.vehicle_history[value.sequence]=encode(value)
        while len(self.provider.vehicle_history)>128:del self.provider.vehicle_history[next(iter(self.provider.vehicle_history))]

    def device_view(self):
        value={key:getattr(self.vehicle,key) for key in ('host_boot','gateway_instance','pi_session','device_session',
            'odom_epoch','calibration','sequence','source_sample','source_us','pi_sample_us','clock_error_us')}
        return dict(vehicle_sample=value,absolute_meter=self.meter.snapshot)

    def confirm_clock(self):
        """실제 4시각 교환과 원 Reply SHA. offset/confirmed/E를 직접 주입하지 않는다."""
        clocks=self.operator.clocks;self.clock_sequence+=2;sequence=self.clock_sequence-1
        first=schema.ClockProbe(vm=self.peer[0],execution=self.peer[1],app_instance=self.peer[2],
            sequence=sequence,guest_send_us=self.now-5000,supported_state_features=127)
        assert clocks.accept_probe(first,self.peer,self.owner_context.control_generation,self.now-4000)==(None,None)
        link=schema.DriveLink(major=2);reply=link.clock;reply.probe.CopyFrom(first)
        reply.host_boot=self.vehicle.host_boot;reply.gateway_instance=self.vehicle.gateway_instance
        reply.pi_receive_us=reply.pi_send_us=self.now-4000;reply.vehicle.CopyFrom(self.vehicle)
        reply.guest_clock_valid_until_pi_us=self.now-4000+1000000
        prepared=OwnedPreparedClock(link,self.peer,self.owner_context.control_generation)
        self.clock_raw=prepared.stamp(self.now-3000)
        clocks.sent_reply(SentClock(prepared.peer,prepared.generation,self.clock_raw))
        second=schema.ClockProbe(vm=self.peer[0],execution=self.peer[1],app_instance=self.peer[2],
            sequence=self.clock_sequence,guest_send_us=self.now,supported_state_features=127)
        second.receipt.CopyFrom(schema.GuestClockReceipt(probe_sequence=sequence,reply_sha256=sha256(self.clock_raw).digest(),
            guest_receive_us=self.now-1000))
        receipt,replay=clocks.accept_probe(second,self.peer,self.owner_context.control_generation,self.now)
        assert receipt==second.receipt and replay is None
        self.clock_ref=sequence

    def current_case(self):
        world=deepcopy(self.base['world']);world.sequence=self.vehicle.sequence;world.observation_seq=self.vehicle.sequence
        # This typed M leaf produces a new observation. Preserve its original
        # stage intervals; advancing capture alone leaves publication in the past.
        for name in ('observed_us','completed_us','published_us'):
            setattr(world,name,getattr(world,name)+self.now-1010000)
        source=deepcopy(self.base['witness'].source);source.world_sequence=world.sequence;source.observation_sequence=world.observation_seq
        witness=schema.RouteCurrentWitness(source=source,vehicle_sequence=self.vehicle.sequence,checked_guest_us=self.now,
            valid_until_pi_us=self.now+50000,clock_probe_sequence=self.clock_ref,clock_reply_sha256=sha256(self.clock_raw).digest(),
            world_raw_sha256=sha256(encode(world)).digest(),vehicle_raw_sha256=sha256(encode(self.vehicle)).digest())
        self.provider.trust=SimpleNamespace(**{key:getattr(source,key) for key in ('vehicle','mapper_boot','mapper_instance',
            'map_epoch','frame_epoch','clock_epoch','configuration','calibration')},primitive=source.primitive.hex(),expires_pi_us=self.now+1000000)
        return route_case(owner=self.owner_context,witness=witness,world=world,vehicle=self.vehicle,records=self.base['records'])

    def publish_world(self,case):
        """M publication/currentWorld is a declared typed leaf; original paired bytes remain exact."""
        raw=encode(case['world']);self.published_worlds[sha256(raw).digest()]=raw
        while len(self.published_worlds)>8:del self.published_worlds[next(iter(self.published_worlds))]
        value=schema.MapperWorldWitness(major=1,mapper=mapper(case),source=case['witness'].source,
            world_raw_sha256=case['witness'].world_raw_sha256)
        self.data.receive_world(encode(value),('typed-M',),self.now)

    def begin(self):
        before=deepcopy(self.owner_context);before.operation_binding.operation_revision-=1
        before.operation_binding.departure_nonce=NIL;self.owner_context=before
        eligibility=schema.RouteEligibility(major=1,eligibility_id=IDS(80),purpose=2,owner=before,witness=self.case['witness'],
            task_space_kind=1,eligible=True,reason=0,goal_sha256=self.case['query'].goal_sha256,exit_space_sha256=ZERO,
            topology_sha256=self.case['candidate'].core.topology_sha256,window_sha256=self.case['candidate'].current_window_sha256,
            global_plan_sha256=ZERO,extent=self.case['extent'])
        ref=self.route.observe_eligibility(encode(eligibility),self.peer)
        self.owner_context=deepcopy(self.case['owner']);query=self.case['query']
        key='g1:'+self.vehicle.host_boot+':1000:'+str(UUID(bytes=query.operator_request_nonce))
        self.row=dict(key=key,terminal=False,request=dict(version=2,action='home.return',deadline_pi_ms=11000,
            args=dict(expected_operation_revision='1',expected_permission_revision='1',ready_revision=str(ref.ready_revision))))
        self.authority.value['current_operation']=dict(service=self.vehicle.gateway_instance,phase='COUNTDOWN',departure=key,
            revision=2,permission_revision=1,parking_enabled=False)
        self.query_raw=encode(query);assert self.route.begin_query(self.query_raw,self.row)==self.query_raw
        self.data.receive(delivery(self.case,self.case['plan']),self.peer,self.now)

    def register(self,candidate=None):
        value=self.case['candidate'] if candidate is None else candidate
        raw=encode(value);assert self.route.candidate(raw,self.peer) is None
        registration=self.route.registration();assert registration is not None
        snapshot=decode(registration.snapshot_raw,schema.RouteSnapshot)
        accepted=accepted_route(self.case,registry_epoch=snapshot.acceptance.registry_epoch,
            registry_revision=snapshot.acceptance.registry_revision,acceptance_nonce=snapshot.acceptance.acceptance_nonce,
            started_pi_us=snapshot.core.started_pi_us,meter_epoch=snapshot.core.meter_epoch,
            meter_baseline_total_ticks=snapshot.core.meter_baseline_total_ticks,metres_per_tick_upper=snapshot.core.metres_per_tick_upper)
        grant=accepted['grant'];grant.route_binding.CopyFrom(decode(registration.route_binding_raw,schema.RouteBinding))
        self.owner_context=deepcopy(accepted['target']);self.grant=grant
        # This leaf is the acknowledged Authority result. It is not a journal/fsync assertion.
        reply=self.route.commit_grant(value.request_nonce,encode(grant))
        accepted['state'].route.CopyFrom(snapshot)
        accepted['state'].route.progress.calculation_sequence=accepted['state'].calculation_sequence
        accepted['state'].mission.route_binding.CopyFrom(grant.route_binding)
        self.state=accepted['state'];return decode(reply,schema.RouteReceipt)

    def admit(self,state=None):
        value=self.state if state is None else state;raw=encode(value)
        prepared=self.route.prepare_state(value,self.peer,raw)
        if prepared is not None:self.route.commit_state(prepared)
        return prepared

    def evidence(self,vm,app):return self.app_evidence.get((vm,app))

    def apply_leaf(self):
        """Actual StateCopies transport is postmerge work; this is an explicit typed acceptance leaf."""
        identity=str(uuid4());link=schema.DriveLink(major=2)
        link.accepted.CopyFrom(schema.AcceptedState(host_boot=self.vehicle.host_boot,
            gateway_instance=self.vehicle.gateway_instance,state=self.state))
        self.provider.copies[identity]=(encode(link),self.now)
        self.app_evidence[(self.peer[0],'autonomous')]=dict(copy_message_id=identity)
        assert self.route.applied(self.peer,identity);return identity

    def physical_progress(self,snapshot,state,world,meter):
        """Declared tracker leaf: only source station forward interval plus actual positive encoder travel."""
        return state.route.progress.station_lower_m>snapshot.progress.station_lower_m

    def advance(self,microseconds=100000,*,left=20,right=20):
        self.now+=microseconds;self.operator.now=self.now;v=self.vehicle
        v.sequence+=1;v.source_sample+=1;v.source_us+=microseconds;v.pi_sample_us+=microseconds
        v.left_ticks+=left;v.right_ticks+=right;v.odom_pose.x+=(left+right)*.0005;self.record_meter(v)
        if self.now+100000>=self.operator.clocks.lease(self.peer,self.clock_ref,self.now,self.owner_context.control_generation).expires:
            self.confirm_clock()
        self.case_now=self.current_case();self.publish_world(self.case_now)

    def progress(self):
        self.state.message_id=str(uuid4());self.state.state_revision+=1;self.state.calculation_sequence+=1
        self.state.vehicle_sequence=self.vehicle.sequence;self.state.route.CopyFrom(self.route.registry['snapshot'])
        self.state.observation_seq=self.case_now['world'].observation_seq
        self.state.route.current_world_sha256=self.case_now['witness'].world_raw_sha256
        p=self.state.route.progress;p.calculation_sequence=self.state.calculation_sequence;p.vehicle_sequence=self.vehicle.sequence
        p.used_travel_ticks=self.meter.snapshot[0].total_ticks-self.state.route.core.meter_baseline_total_ticks
        p.revision+=1;p.station_lower_m+=.02;p.station_upper_m=p.station_lower_m+.01;p.remaining_upper_m=max(0.,p.remaining_upper_m-.02)
        assert self.admit() is not None

    def update(self):
        value=deepcopy(self.case['candidate']);saved=self.route.registry['snapshot']
        value.owner.CopyFrom(self.owner_context);value.witness.CopyFrom(self.case_now['witness'])
        value.expected_registry_epoch=self.route.epoch;value.expected_registry_revision=self.route.revision
        value.expected_route_revision=saved.core.revision;value.request_sequence+=1;value.request_nonce=uuid4().bytes
        value.action=2;value.core.CopyFrom(saved.core);value.core.revision+=1;value.progress.CopyFrom(saved.progress)
        value.progress.calculation_sequence=value.calculation_sequence=self.state.calculation_sequence
        value.progress.vehicle_sequence=self.vehicle.sequence;value.phase_state_message_id=uuid4().bytes
        value.began_guest_us=value.phase_started_guest_us=0;value.extent.vehicle_sequence=self.vehicle.sequence
        return value
