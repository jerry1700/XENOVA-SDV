"""Grant/path 없는 실제 C 입력 preflight→Pi 준비→명시 출발 수락의 왕복.

RTOS/pipe/정지 plant와 초기 MANUAL은 typed leaf다. 실제 C의 유효 보고/철회/
미응답과1초 READY·3초 countdown 입력 수명을 관찰한다. AUTO app apply/DRIVE 출력이나
MANUAL worker/WASD까지 검증한 것으로 계산하지 않는다. 원 C bytes/P4를 보존한다.
"""
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

from xenova_drive_world import schema,encode,decode
from xenova_gateway.drive_link import inbound
from xenova_gateway.guest_clock import OwnedPreparedClock,SentClock
from test_behavior_app_bridge import app_bridge_binary
from test_behavior_gateway_bridge import App
from test_engine import modern_world
from test_operation_runtime import operating
from mapper_operation_fixture import install_mapper_fixture


def test_actual_c_grantless_input_preflight_precedes_explicit_departure(operating,app_bridge_binary,tmp_path):
    w=operating;provider=w.r.drive_link
    manifest=json.loads((Path(__file__).resolve().parents[4]/'platform/apps/drive/planner/generated/primitive-manifest.json').read_text())
    provider.trust.primitive=manifest['asset_sha256']
    # The typed installed M source now names the actual compiled C asset.
    # Re-negotiate its original source/clock before C boot; no cap boolean override.
    install_mapper_fixture(w)
    origin=time.monotonic();pi_origin=w.w.t
    def from_host(stamp):return pi_origin+stamp-origin
    def now():return from_host(time.monotonic())
    w.r._clock=now;w.w.m.clock=now;w.io.clock=now
    seen=[];pending=[];reports=[];confirmed=[];probes=[];coverage=[];report_bodies={}
    request=None;response=None;hold_replies=False;held=[];milestones={}
    last_accepted_report_body=None
    world_sequence=0;last_capture=None;world_sources=[];mapper_offset=w.mapper_fixture.offset
    def record(rows,value,cap=2048):
        assert len(rows)<cap,'bounded actual C preflight observation storage exhausted'
        rows.append(value)
    with (tmp_path/'actual-a1-c.stderr').open('wb') as errors:
        app=App(app_bridge_binary,errors)
        def clock():w.w.t=now();w.w.bind()
        def command(line):
            clock();rc,guest,output=app.ask(line);clock()
            for index,item in enumerate(output):
                if item.startswith('WIRE '):
                    assert len(pending)<App.WIRE_CAP
                    _,topic,raw=item.split(' ',2)
                    pending.append((topic,bytes.fromhex(raw),from_host(app.wire_received[index])))
                elif item.startswith('META '):record(seen,('meta',json.loads(item.split(' ',1)[1])))
            return rc,guest
        def deliver(value):
            submitted=[]
            def link_line():
                wire=value.stamp(w.o.now) if isinstance(value,OwnedPreparedClock) else value
                submitted.append(wire);return 'link '+wire.hex()
            rc,guest=command(link_line);wire=submitted[0]
            if isinstance(value,OwnedPreparedClock):
                reply=decode(wire,schema.DriveLink).clock
                record(seen,('clock-delivery',dict(sequence=reply.probe.sequence,rc=rc,guest_end=guest,
                    p1=reply.pi_receive_us,p2=reply.pi_send_us,E=reply.guest_clock_valid_until_pi_us,
                    raw_sha256=sha256(wire).hexdigest(),replay=value.replay is not None,
                    transport=dict(app.last_transport))))
                clock();w.io.clock_completions.put_nowait(SentClock(value.peer,value.generation,wire));provider.tick()
        def exchange():
            nonlocal last_accepted_report_body
            while pending:
                clock();topic,raw,received=pending.pop(0);link=decode(raw,schema.DriveLink);kind=link.WhichOneof('payload')
                assert kind=='probe',('unexpected pre-depart source body',kind)
                probe=link.probe
                accepted=provider.receive(w.peer[0],link,received)
                record(seen,('actor',kind,accepted,dict(provider.reasons)))
                assert accepted
                if probes:assert probe.guest_send_us-probes[-1]['guest_send_us']>=40000
                ready,reason,_,_=w.o.operations.inputs('AUTO')
                record(probes,dict(sequence=probe.sequence,guest_send_us=probe.guest_send_us,
                    p1=int(received*1e6),has_report=probe.HasField('auto_input_preflight'),
                    checked_guest_us=probe.auto_input_preflight.checked_guest_us if probe.HasField('auto_input_preflight') else 0,
                    input_ready=ready,reason=reason),512)
                if probe.HasField('receipt'):
                    lease=w.o.clocks.lease(w.peer,probe.receipt.probe_sequence,w.o.now,w.w.m.generation)
                    original=w.o.clocks.sent.get((*w.peer,probe.receipt.probe_sequence))
                    assert lease is not None and original is not None
                    assert lease.digest==probe.receipt.reply_sha256==sha256(original.raw).digest()
                    record(confirmed,probe.receipt.probe_sequence,512)
                if probe.HasField('auto_input_preflight'):
                    record(reports,raw.hex(),512);report=probe.auto_input_preflight
                    assert report.clock_probe_sequence in confirmed and report.control_generation==w.w.m.generation
                    assert report.vehicle_sequence in provider.vehicle_history
                    assert report.source.vehicle==provider.trust.vehicle
                    assert report.checked_guest_us<=probe.guest_send_us
                    canonical=encode(report);old=report_bodies.get(report.checked_guest_us)
                    assert old is None or old==canonical  # Same original check cannot gain a new E.
                    assert old is not None or len(report_bodies)<512
                    report_bodies[report.checked_guest_us]=canonical
                    stored=w.o.operations.reports.get(w.peer)
                    if stored is not None and stored['raw']==canonical:
                        last_accepted_report_body=canonical
                        record(coverage,dict(start=w.o.now,end=report.valid_until_pi_us,checked=report.checked_guest_us),512)
                else:
                    # An actual omission withdraws the last app report; numeric
                    # clock receipt processing above remains independent.
                    assert w.peer not in w.o.operations.reports
                with w.io.lock:
                    outgoing=[w.io.pending.pop(k) for k in tuple(w.io.pending) if k[0]==inbound(w.peer[0])]
                for value in outgoing:
                    if hold_replies and isinstance(value,OwnedPreparedClock):
                        assert not held,'a new probe replaced an unanswered original before its retry'
                        held.append(value)
                    else:deliver(value)
        def produce_inputs():
            nonlocal world_sequence,last_capture
            clock();capture=time.monotonic();w.w.t=from_host(capture)
            w.sink.measure();provider.tick();w.mapper_fixture.step()
            sample=schema.VehicleSample(major=1,**{**w.view(),'gateway_instance':provider.instance})
            command('vehicle '+encode(sample).hex())
            if last_capture is None or capture-last_capture>=.1:
                last_capture=capture
                world=modern_world();trust=provider.trust
                assert sample.velocity_mps==0 and sample.odom_pose.x==sample.odom_pose.y==sample.odom_pose.yaw==0
                assert trust.lower_offset_us<=mapper_offset<=trust.upper_offset_us
                for key in ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch',
                    'configuration','calibration','primitive','clock_epoch'):setattr(world,key,getattr(trust,key))
                world_sequence+=1;world.sequence=world.observation_seq=world_sequence
                world.reference_odom_seq=sample.sequence
                world.observed_us=int(from_host(capture)*1e6)-mapper_offset
                world.localization.observed_mapper_us=world.observed_us
                world.completed_us=int(now()*1e6)-mapper_offset
                submitted=[]
                def world_line():
                    world.published_us=int(now()*1e6)-mapper_offset
                    raw=encode(world);submitted.append(raw);return 'world '+raw.hex()
                command(world_line)
                record(world_sources,dict(sequence=world.sequence,vehicle_sequence=sample.sequence,
                    observed=world.observed_us,completed=world.completed_us,published=world.published_us,
                    reference_revision=world.reference_revision,raw_sha256=sha256(submitted[0]).hexdigest()),512)
            # One step is a complete planner turn, already internally bounded.
            # Pump original clock/actor traffic between turns, as the independent
            # runtime IO owner does; a 32-turn batch starves this sequential bench.
            command('step 1');command('io');exchange()
        def pump_until(predicate,seconds=3):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                produce_inputs();clock();w.o.tick()
                if predicate():return
                time.sleep(.003)
            assert predicate(),seen[-8:]
        def covered(start,end):
            cursor=start
            for interval in coverage:
                if interval['end']<=cursor:continue
                assert interval['start']<=cursor,('actual accepted report validity gap',cursor,interval)
                cursor=max(cursor,interval['end'])
                if cursor>=end:return
            assert cursor>=end,('actual accepted reports do not cover interval',start,end,cursor)
        try:
            binding=w.r.bindings.current(w.peer[0],w.w.t)
            assert command('boot '+' '.join((*w.peer,w.r.authority.host,binding['boot_id'],provider.instance)))[0]==0
            control=json.dumps(w.w.m.control_payload('a1-grantless-software-bench'),separators=(',',':')).encode()
            assert command('control '+control.hex())[0]==0
            limit=time.monotonic()+12
            while time.monotonic()<limit:
                produce_inputs();clock();view=w.o.operation_status()
                record(seen,('departure_check',w.o.now,view))
                if view['auto_ready']:break
                command('meta')
            assert confirmed and reports and view['auto_ready']
            assert w.o.missions.current() is None and not provider.copies
            # One contended app-metadata turn preserves the already-admitted
            # report and its original check time and deadline.
            assert command('preflight-busy')[0] == 0
            milestones['metadata_contention'] = 'original report retained'

            # Actual invalid new steering observations cause an omission. It
            # withdraws the app report at Pi while its numeric proof survives.
            prior=decode(bytes.fromhex(reports[-1]),schema.DriveLink).probe.auto_input_preflight
            prior_lease=w.o.clocks.lease(w.peer,prior.clock_probe_sequence,w.o.now,w.w.m.generation)
            assert prior_lease is not None
            before_withdraw=len(probes);w.sink.steering_available=False;time.sleep(.021)
            pump_until(lambda:any(not p['has_report'] for p in probes[before_withdraw:]))
            omitted=next(p for p in probes[before_withdraw:] if not p['has_report'])
            assert not omitted['input_ready'] and w.peer not in w.o.operations.reports
            remaining=w.o.clocks.lease(w.peer,prior.clock_probe_sequence,w.o.now,w.w.m.generation)
            assert remaining is not None and (remaining.expires,remaining.digest)==(prior_lease.expires,prior_lease.digest)
            milestones['withdrawal']=dict(probe=omitted,clock_sequence=prior.clock_probe_sequence,
                original_clock_end=prior_lease.expires,original_report_end=prior.valid_until_pi_us)
            w.sink.steering_available=True;time.sleep(.021)
            pump_until(lambda:w.o.operation_status()['auto_ready'])

            # Hold a not-yet-submitted prepared reply, not a stamped/relabelled
            # old wire. New reports must not replace this outstanding probe.
            hold_replies=True;pump_until(lambda:bool(held))
            original=decode(bytes(held[0]),schema.DriveLink).clock
            held_sequence=original.probe.sequence;held_p1=original.pi_receive_us
            held_end=original.guest_clock_valid_until_pi_us
            assert last_accepted_report_body is not None
            original_report_body=last_accepted_report_body
            old_report=decode(original_report_body,schema.AutoInputPreflight)
            count=len(probes)
            held_report=original.probe.HasField('auto_input_preflight')
            if not held_report:
                assert w.peer not in w.o.operations.reports
                assert w.o.operations.inputs('AUTO')[1]=='PREFLIGHT_UNAVAILABLE'
            report_end=old_report.valid_until_pi_us;until=time.monotonic()+.2
            command('meta');before=next(row[1] for row in reversed(seen) if row[0]=='meta')
            # Same original World/Vehicle and confirmed numeric lease remain
            # valid during this short source pause. Only actual C current-input
            # checks may renew the report window; no new ACK is delivered.
            frozen_until=time.monotonic()+.065
            while time.monotonic()<frozen_until:
                command('step 1');command('io');exchange();clock();w.o.tick();time.sleep(.003)
            command('meta');after=next(row[1] for row in reversed(seen) if row[0]=='meta')
            assert after['preflight_attempt_checked']>=before['preflight_attempt_checked']+40000
            assert after['preflight_attempt_e']>before['preflight_attempt_e']
            assert after['preflight_vehicle_sequence']==before['preflight_vehicle_sequence']
            assert after['clock_probe_sequence']==before['clock_probe_sequence']
            assert after['preflight_geometry_checks']==before['preflight_geometry_checks']
            assert before['preflight_geometry_cached']==after['preflight_geometry_cached']==1
            milestones['lead_refresh']=dict(before=before,after=after,source='same unexpired inputs; actual recheck, no ACK')
            while time.monotonic()<until:
                produce_inputs();clock();w.o.tick();time.sleep(.003)
            assert len(probes)==count and probes[-1]['sequence']==held_sequence
            ok,reason,_,_=w.o.operations.inputs('AUTO')
            expected_reason='PREFLIGHT_UNAVAILABLE' if w.peer not in w.o.operations.reports else 'PREFLIGHT_EXPIRED'
            assert w.o.now>=report_end and not ok and reason==expected_reason,(reason,expected_reason)
            assert encode(old_report)==original_report_body  # Historical evidence never becomes current READY.
            milestones['unanswered']=dict(sequence=held_sequence,p1=held_p1,original_clock_end=held_end,
                original_report_end=report_end,original_report_sha256=sha256(original_report_body).hexdigest(),
                held_has_report=held_report,current_reason=reason,observed_expiry_at=w.o.now,new_probes=0)
            hold_replies=False;deliver(held.pop(0))
            delivered=next(row[1] for row in reversed(seen) if row[0]=='clock-delivery')
            assert delivered['sequence']==held_sequence and delivered['p1']==held_p1 and delivered['E']==held_end
            pump_until(lambda:w.o.operation_status()['auto_ready'])

            # Conservative accepted-report intervals must cover the whole
            # second, not only isolated successful READY polls.
            ready_start=w.o.now;ready_until=time.monotonic()+1.
            while time.monotonic()<ready_until:
                produce_inputs();clock();w.o.tick();view=w.o.operation_status()
                if not view['auto_ready']:
                    command('meta')
                assert view['auto_ready'],(view,seen[-6:])
                record(seen,('continuous-ready',w.o.now,view['valid_until_pi_us']))
                time.sleep(.003)
            ready_end=w.o.now;covered(ready_start,ready_end)
            assert ready_end-ready_start>=1000000
            milestones['continuous_ready']=dict(start=ready_start,end=ready_end)
            # No diagnostic pipe round trip or stale fixture clock between
            # current READY and this explicit intent. Its original report E
            # remains unchanged; if it expires the actor must still reject.
            stamp=view['binding'];issued=w.o.now//1000
            call=dict(version=2,action='drive.depart',request_id=f'g1:{w.r.authority.host}:{issued}:{uuid4()}',
                host_boot_id=w.r.authority.host,gateway_instance_id=w.o.instance,expected_generation=w.w.m.generation,
                expected_owner=w.w.m.active_vm,deadline_pi_ms=issued+10000,args=dict(mode='AUTO',
                    expected_operation_revision=stamp['operation_revision'],expected_permission_revision=stamp['permission_revision'],
                    ready_revision=view['ready_revision'],trigger_sha256=None))
            request=call;queue=w.o.submit(call)
            until=time.monotonic()+2
            while queue.empty():
                assert time.monotonic()<until,'actual admission journal did not settle'
                clock();w.o.tick();time.sleep(.001)
                if queue.empty():produce_inputs()
            response=queue.get_nowait();assert response['ok'],response
            assert w.o.operations.current()['phase']=='COUNTDOWN'
            assert not w.w.m.output_permitted and w.o.hold and w.o.missions.current() is None
            operation=w.o.operations.current();countdown_end=operation['countdown_until']
            assert int(w.w.m.timing.resume_countdown_s*1e6)==3000000
            countdown_start=countdown_end-3000000
            original_departure=json.dumps(operation['departure'],sort_keys=True)
            until=time.monotonic()+5
            while w.o.operations.current()['phase']=='COUNTDOWN':
                assert time.monotonic()<until,'original countdown did not complete'
                produce_inputs();clock();operation=w.o.operations.current()
                if operation['phase']=='COUNTDOWN':
                    assert operation['countdown_until']==countdown_end
                    assert json.dumps(operation['departure'],sort_keys=True)==original_departure
                    ok,reason,_,_=w.o.operations.inputs('AUTO')
                    assert ok,reason
                    public=w.o.operation_status()
                    assert not public['auto_ready']  # duplicate Enter is busy, input is ready
                    record(seen,('countdown-input-ready',w.o.now,countdown_end))
                w.o.tick();time.sleep(.003)
            assert w.o.now>=countdown_end and w.o.operations.current()['phase'] in ('APPLYING','ACTIVE')
            covered(countdown_start,countdown_end)
            milestones['countdown']=dict(start=countdown_start,end=countdown_end,
                observed_after=w.o.now,phase=w.o.operations.current()['phase'])
        finally:
            app.close()
            (tmp_path/'actual-a1-c.transcript.json').write_text(json.dumps(app.transcript,separators=(',',':')))
            (tmp_path/'actual-a1-observations.json').write_text(json.dumps(dict(observations=seen,reports=reports,
                confirmed=confirmed,probes=probes,coverage=coverage,world_sources=world_sources,milestones=milestones,
                mapper_clock=list(w.mapper_fixture.trace),mapper_leaf_offset=mapper_offset,
                request=request,response=response,scope='C preflight/withdraw/outstanding/report expiry, continuous READY and original countdown; no AUTO app apply/output'),separators=(',',':')))
