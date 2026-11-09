"""설치 M의 명시 protocol leaf + 실제 MapperClockOwner/_Link 지원 협상.

caller의 원 Pi clock/DriveLink Vehicle bytes를 사용한다. MQTT/child/M 소비 알고리즘·
실제 장치 승인을 대신하지 않으며, 주기/lease/source/E를 실제 제품 owner가 검산한다.
"""
from collections import deque
from hashlib import sha256
import json
from pathlib import Path
import socket
import time
from types import SimpleNamespace
from uuid import uuid4

from xenova_drive_world import schema,encode,decode
from xenova_drive_world.mapper_clock import ClockPolicy,MapperClockError,candidate_body,plan_json
from xenova_gateway.mapper_uplink import (GatewayMapperUplink,UplinkProfile,_Link,
    parse_operation_plan,build_operation_probe)


class InstalledMapperFixture:
    def __init__(self,runner,*,supports_operation,offset=None):
        assert type(supports_operation) is bool
        self.r=runner;self.supports_operation=supports_operation;self.trace=deque(maxlen=256)
        trust=runner.drive_link.trust;assert trust is not None
        self.source={key:getattr(trust,key) for key in ('mapper_boot','mapper_instance','clock_epoch','map_epoch',
            'frame_epoch','odom_epoch','configuration','calibration','primitive')}
        # This is the declared leaf's single J clock definition, not an injected
        # offset estimate or a replacement for the actual two-way bound.
        self.offset=trust.upper_offset_us if offset is None else offset
        maximum=getattr(runner._sink,'_vehicle_maximum_age_us',None)
        if maximum is None:maximum=runner._sink.odometry.config.maximum_sample_age_us
        profile=ClockPolicy(trust.vehicle,'oak-unit',trust.configuration,trust.calibration,trust.primitive,
            2,10,5000,1000000,maximum,1000000)
        self.u=GatewayMapperUplink(UplinkProfile(profile,'127.0.0.1',1883,None,None),runner.authority.host,
            runner.operator.instance,clock=self.now)
        self.u.uplink_instance=str(uuid4());self.u.channel_epoch=str(uuid4())
        identity=(self.u.host_boot,self.u.gateway_instance,self.u.uplink_instance,self.u.channel_epoch)
        left,right=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        self.u.link=_Link(left,identity);self.peer=_Link(right,identity)
        self.u.process=SimpleNamespace(exitcode=None)  # declared child boundary; no actual reap claim
        runner.mapper_uplink=self.u;self.closed=False
        self.events=deque(maxlen=64);self.first_events=[];self.event_sequence=0;self.host_started=time.monotonic()
        self.bootstrap_outcome='PENDING';self.diagnostic_path=Path(runner.authority.directory).parent/'actual-mapper-bootstrap.json'
        # Observation wrappers return/rethrow the actual owner's original result.
        # Neither a successful offer nor a decoded Reply is treated as a lease.
        original_vehicle=self.u.owner.set_vehicle;original_accept=self.u.owner.accept
        def vehicle(raw,now):
            event=dict(kind='owner-set-vehicle',raw=raw.hex(),now=now)
            try:
                result=original_vehicle(raw,now);event['result']='ACCEPTED';return result
            except Exception as error:
                event['error']=type(error).__name__+': '+str(error);raise
            finally:self.note(event)
        def accept(plan,body,now):
            event=dict(kind='owner-accept',plan=plan_json(plan).decode('ascii'),body=body.hex(),now=now)
            try:
                result=original_accept(plan,body,now)
                event['result']='WARMUP' if result is None else 'LEASE'
                if result is not None:event['original_trust']=result.trust_bytes.hex()
                return result
            except Exception as error:
                event['error']=type(error).__name__+': '+str(error);raise
            finally:self.note(event)
        self.u.owner.set_vehicle=vehicle;self.u.owner.accept=accept
        self.note(dict(kind='installed',declared_source=self.source,declared_offset=self.offset))

    def now(self):return int(self.r._clock()*1000000)

    def state(self):
        owner=self.u.owner;cap=self.u._operation_capability;source=self.r.drive_link.trust
        return dict(now=self.now(),host_elapsed_s=time.monotonic()-self.host_started,counts=dict(self.u.counts),
            failure=self.u.failure,vehicle=None if owner.vehicle is None else encode(owner.vehicle).hex(),
            channel=[self.u.host_boot,self.u.gateway_instance,self.u.uplink_instance,self.u.channel_epoch],
            current_control=dict(generation=self.r.machine.generation,mode=self.r.machine.mode.value,
                fence_revision=self.r.operator.fence_revision,hold=self.r.operator.hold),
            current_provider_trust=None if source is None else encode(source).hex(),
            last_plan_us=owner.last_plan_us,sequence=owner.sequence,streak=owner._streak,
            pending=None if owner.pending is None else plan_json(owner.pending).decode('ascii'),
            lease=None if owner._lease is None else owner._lease.trust_bytes.hex(),
            cap=None if cap is None else dict(reply=cap.reply_bytes.hex(),reply_sha256=cap.reply_sha256.hex(),
                source=cap.source,channel=cap.channel,vehicle_sessions=cap.vehicle_sessions,e=cap.valid_until_pi_us))

    def note(self,event):
        self.event_sequence+=1;event=dict(event,event_sequence=self.event_sequence,state=self.state())
        if len(self.first_events)<32:self.first_events.append(event)
        self.events.append(event)

    def save_diagnostic(self):
        value=dict(scope='diagnostic only; actual owner return/exception unchanged; no M/device/READY authority',
            bootstrap_outcome=self.bootstrap_outcome,declared_source=self.source,declared_offset=self.offset,
            clock_model=getattr(self,'clock_model',None),
            first_events=self.first_events,recent_events=list(self.events),exchanges=list(self.trace),final=self.state())
        self.diagnostic_path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf8')

    def step(self):
        assert not self.closed
        provider=self.r.drive_link;provider.tick()
        sample=self.r.device_view().get('vehicle_sample');assert sample is not None
        raw=provider.vehicle_history.get(sample['sequence']);assert raw is not None
        # The exact source bytes also sent to P; no sample is re-timestamped here.
        offered=self.u.offer_vehicle(raw);self.u.tick(provider)
        self.note(dict(kind='offer-and-tick',offered=offered,raw_vehicle=raw.hex()))
        for header,body in self.peer.receive(self.now()):
            self.note(dict(kind='peer-received',header=header,body=body.hex()))
            if header['kind']!='PROBE_PLAN':continue
            plan,gate=parse_operation_plan(body,request_id=header['request_id'],
                issued_us=int(header['issued_pi_us']),expires_us=int(header['expires_pi_us']))
            p1=self.now()
            try:probe_raw=build_operation_probe(plan,gate,p1)
            except MapperClockError:
                self.trace.append(dict(kind='expired-original-plan',request_id=plan.request_id,p1=p1));continue
            probe=decode(probe_raw,schema.MapperClockProbe)
            j2=p1-self.offset;j3=self.now()-self.offset
            reply=schema.MapperClockReply(major=1,pi_host_boot=probe.pi_host_boot,pi_client_instance=probe.pi_client_instance,
                probe_seq=probe.probe_seq,nonce=probe.nonce,pi_send_us=probe.pi_send_us,device=probe.device,
                j2=j2,j3=j3,**self.source)
            if self.supports_operation:reply.supported_mapper_features=1
            reply_raw=encode(reply);p4=self.now()
            self.note(dict(kind='original-probe-reply',probe=probe_raw.hex(),reply=reply_raw.hex(),p1=p1,p4=p4))
            if p4>=plan.expires_us:
                self.trace.append(dict(kind='expired-original-reply',request_id=plan.request_id,p1=p1,p4=p4));continue
            assert self.peer.offer('TRUST_CANDIDATE',candidate_body(probe_raw,reply_raw,p4),p4,plan.expires_us,plan.request_id)
            self.peer.flush(p4);self.u.tick(provider)
            cap=self.u.operation_capability(self.now())
            self.trace.append(dict(kind='actual-clock-exchange',request_id=plan.request_id,sequence=probe.probe_seq,
                p1=p1,j2=j2,j3=j3,p4=p4,probe=probe_raw.hex(),reply=reply_raw.hex(),reply_sha256=sha256(reply_raw).hexdigest(),
                operation_gate=probe.HasField('operation_gate'),cap_e=0 if cap is None else cap.valid_until_pi_us))

    def ready(self):
        result=self.r.mapper_operation_capability(self.now(),self.r.drive_link.trust) is not None
        self.note(dict(kind='ready-check',result=result));return result

    def close(self):
        if self.closed:return
        self.closed=True;self.u.process=None;self.u.close()
        self.u.link.socket.close();self.peer.socket.close()
        if self.r.mapper_uplink is self.u:self.r.mapper_uplink=None


def install_mapper_fixture(world):
    """WAIT 중 명시 설치/새 source bootstrap. 원 clock domain은 교체 뒤에도 같다."""
    previous=getattr(world,'mapper_fixture',None);offset=None if previous is None else previous.offset
    if previous is not None:previous.close()
    if not hasattr(world,'_mapper_clock_anchor'):
        world._mapper_clock_anchor=world.w.t,time.monotonic()
    world.mapper_fixture=InstalledMapperFixture(world.r,supports_operation=True,offset=offset)
    anchor_pi,anchor_host=world._mapper_clock_anchor
    world.mapper_fixture.clock_model=dict(pi_anchor_us=int(anchor_pi*1000000),host_anchor_s=anchor_host,
        rule='next Pi = max(previous Pi + original 2ms step, anchored elapsed host monotonic); new sensor production follows')
    if not hasattr(world,'_mapper_original_tick'):
        world._mapper_original_tick=world.tick
        def tick():
            # The existing actor turn can take much longer than 2ms on Nano.
            # Advance this declared test clock before producing NEW plant
            # samples. Never renew timestamps/E on already captured messages.
            # Preserve explicit test-time advances and the original 2ms step.
            pi,host=world._mapper_clock_anchor
            world.w.t=max(world.w.t,pi+time.monotonic()-host-.002)
            world._mapper_original_tick();world.mapper_fixture.step()
        world.tick=tick
    try:
        world.mapper_fixture.step()
        world.pump(world.mapper_fixture.ready)
        world.mapper_fixture.bootstrap_outcome='READY'
    except BaseException as error:
        world.mapper_fixture.bootstrap_outcome=type(error).__name__+': '+str(error)
        raise
    finally:world.mapper_fixture.save_diagnostic()
    return world.mapper_fixture
