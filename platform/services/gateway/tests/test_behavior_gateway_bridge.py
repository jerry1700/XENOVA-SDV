"""Actual C app/input/codec ↔ Gateway actor/clock/registry integration on Nano.

RTOS scheduling, authenticated pipe transport, placed World and stationary plant
are explicit leaves. No READY/clock confirmation/registration receipt is injected.
The pre-existing operator-world lifecycle is only the initial control premise;
actual shared C lifecycle output is retained as evidence, not replaced by it.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import subprocess
import time
from uuid import UUID,uuid4

import pytest
from xenova_drive_world import (schema,encode,decode,episode_request_body,budget_request_body,
    initial_none_core,initial_none_snapshot,episode_core_sha256)
from xenova_gateway.drive_link import inbound
from xenova_gateway.guest_clock import OwnedPreparedClock,SentClock
from xenova_gateway.frame import DriveFrame
from xenova_gateway.output_owner import OutputOwner
from test_behavior_app_bridge import app_bridge_binary
from test_command3_gateway_flow import SnapshotDevice,receive_original,ingress_diagnostics
from test_engine import modern_world
from test_lattice import obstacle
from test_operator_runtime import world
from b08_fixture import activate,journal


class App:
    WIRE_CAP=64

    def __init__(self,binary,stderr):
        self.process=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=stderr,bufsize=0)
        self.buffer=b'';self.transcript=[];self.buffer_received=None
        self.wire_received={};self.transport_sequence=0;self.last_transport={}

    def ask(self,line):
        # A private late factory prepares only the text envelope immediately
        # before writing; callers keep the original (rc, guest, output) tuple.
        self.transport_sequence+=1;self.wire_received={}
        if callable(line):line=line()
        write_begin=time.monotonic()
        self.process.stdin.write(line.encode()+b'\n');self.process.stdin.flush()
        write_end=time.monotonic()
        output=[];until=time.monotonic()+15
        while True:
            if b'\n' not in self.buffer:
                remaining=until-time.monotonic();assert remaining>0,'actual C bridge response timeout'
                assert select.select([self.process.stdout],[],[],remaining)[0],'actual C bridge stalled'
                value=os.read(self.process.stdout.fileno(),65536)
                received=time.monotonic()
                assert value,'actual C bridge exited before END'
                # Read only while no complete line exists. Thus every newline
                # consumed next was completed by this exact read, including a
                # line whose prefix arrived in an earlier read. Preserve it
                # across END/ask calls rather than timestamping later parsing.
                self.buffer_received=received;self.buffer+=value;continue
            raw,self.buffer=self.buffer.split(b'\n',1);text=raw.decode();output.append(text)
            if text.startswith('WIRE '):
                assert len(self.wire_received)<self.WIRE_CAP,'bounded native WIRE observation queue exhausted'
                self.wire_received[len(output)-1]=self.buffer_received
            if text.startswith('END '):
                _,result,guest=text.split();self.transcript.append((line,output))
                self.last_transport=dict(sequence=self.transport_sequence,command=line.split(' ',1)[0],
                    write_begin_host_s=write_begin,write_end_host_s=write_end,
                    end_read_host_s=self.buffer_received,guest_end_us=int(guest),wire_count=len(self.wire_received))
                return int(result),int(guest),output[:-1]

    def close(self):
        if self.process.poll() is None:self.ask('quit')
        self.process.stdin.close();assert self.process.wait(5)==0


def test_phase_publication_keeps_registered_identity_while_an_unrelated_candidate_is_pending(app_bridge_binary,tmp_path):
    # Typed predicate/cache boundary only. No actual input, READY, clock,
    # registration or command is emitted by this fresh isolated native process.
    current=schema.EpisodeStatus(major=1,state=1,registry_epoch=b'r'*16,registry_revision=1,
        accepted_core_sha256=episode_core_sha256(initial_none_core()),episode_id=bytes(16),episode_revision=0,
        commit_state=1,state_message_id=bytes(16),state_sha256=bytes(32),state_revision=0,plan_revision=0,
        phase_revision=0,calculation_hwm=0,vehicle_hwm=0,valid_until_pi_us=2000000)
    with (tmp_path/'publication-policy.stderr').open('wb') as errors:
        app=App(app_bridge_binary,errors)
        try:
            rc,_,output=app.ask('publication-policy '+encode(initial_none_snapshot(current)).hex())
            assert rc==0,output
            value=json.loads(next(row.split(' ',1)[1] for row in output if row.startswith('PUBLICATION_POLICY ')))
            assert value==dict(baseline=1,unrelated_candidate=1,wrong_core=0,wrong_ref=0,wrong_id=0,
                wrong_receipt_ref=0,wrong_path=0,wrong_plan=0,future_ack=0,failed=0,
                cache_rc=-11,old_cache_retained=0,new_id_preserved=1,
                measured_progress=1,old_progress=0,other_registry=0,other_progress_core=0)
        finally:
            app.close()
            (tmp_path/'publication-policy.transcript.json').write_text(json.dumps(app.transcript,indent=2))


@pytest.mark.parametrize('steering_available,event_boundary',[(True,None),(False,None),(True,'expiry'),(True,'context')],
    ids=['servo-estimate','steering-unavailable','event-clock-expiry','event-clock-context'])
def test_actual_c_initial_none_then_registered_episode_full_state(world,app_bridge_binary,tmp_path,steering_available,event_boundary):
    w=activate(world);provider=w.r.drive_link
    # 관측 유무는 새 plant 표본 생산에만 적용한다. 이미 만든 원 Vehicle을 고치지 않는다.
    w.sink.steering_available=steering_available
    # The software bench installs the actual generated asset identity, not the
    # transport-only ab*32 placeholder from OperatorWorld's old typed fixture.
    manifest=json.loads((Path(__file__).resolve().parents[4]/
        'platform/apps/drive/planner/generated/primitive-manifest.json').read_text())
    provider.trust.primitive=manifest['asset_sha256']
    record=w.o.missions.start(w.call('auto.resume'),1,admitted=True)
    journal(w,dict(current_mission=record));assert w.o.missions.publish()
    host_origin=time.monotonic();pi_origin=w.w.t
    # Both fixture processes use this host's monotonic clock. Keep the original
    # complete-read timestamp, translated by this fixed Pi fixture relation.
    def pi_from_host(host_monotonic):return pi_origin+host_monotonic-host_origin
    def current_pi_clock():return pi_from_host(time.monotonic())
    # During real C/actor I/O, CPU work and OutputOwner waiting also consume E;
    # the pre-existing fixture clock must not freeze between pipe turns.
    w.r._clock=current_pi_clock;w.w.m.clock=current_pi_clock;w.io.clock=current_pi_clock
    calculation=0;seen=[];confirmed=[];first_state=None;first_request=None;admitted_phase=None;selected_drive=None
    negative_observation=None;snapshot_identities={}
    with (tmp_path/'actual-c.stderr').open('wb') as errors:
        app=App(app_bridge_binary,errors)
        queue=[];clock_leaf=[];clock_leaf_total=0
        world_period_us=100000;world_sequence=0;last_world_capture=None;stationary_pose=None
        assert world_period_us<manifest['profile']['live_age_us']
        world_sources=[];world_sources_total=0
        # Declared software mapper clock on this same host. Fix this relation
        # once; a later receive or trust refresh never retimes a captured frame.
        mapper_offset_us=provider.trust.upper_offset_us
        def note_clock(value):
            nonlocal clock_leaf_total
            clock_leaf_total+=1
            if len(clock_leaf)<64:clock_leaf.append(value)

        def clock():
            w.w.t=pi_origin+time.monotonic()-host_origin;w.w.bind()
        def command(line):
            nonlocal selected_drive
            clock()
            rc,guest,output=app.ask(line)
            clock()
            for index,value in enumerate(output):
                if value.startswith('WIRE '):
                    assert len(queue)<App.WIRE_CAP,'bounded native WIRE delivery queue exhausted'
                    received_host=app.wire_received[index]
                    assert received_host is not None
                    _,topic,raw=value.split(' ',2)
                    queue.append((topic,bytes.fromhex(raw),pi_from_host(received_host),received_host,
                        dict(app.last_transport)))
                elif value.startswith('COMMAND '):
                    original=value.split(' ',2)[2];body=json.loads(original);seen.append(('command',body))
                    if admitted_phase is not None and selected_drive is None and body['command_kind']=='DRIVE':
                        selected_drive=(body,original)
                elif value.startswith('LIFECYCLE '):seen.append(('lifecycle',json.loads(value.split(' ',1)[1])))
                elif value.startswith('META '):seen.append(('meta',json.loads(value.split(' ',1)[1])))
                elif value.startswith('EVENT_CLOCK '):seen.append(('event-clock',json.loads(value.split(' ',1)[1])))
            return rc,guest
        def refresh():
            nonlocal world_sequence,last_world_capture,stationary_pose,world_sources_total
            clock()
            capture_host=time.monotonic();w.w.t=pi_from_host(capture_host)
            w.record_meter()  # actual new stationary software encoder ingress
            sample=schema.VehicleSample(major=1,**{**w.view(),'gateway_instance':provider.instance})
            assert sample.HasField('estimated_steering_rad') is steering_available
            assert not sample.HasField('measured_steering_rad')
            pose=(sample.odom_pose.x,sample.odom_pose.y,sample.odom_pose.yaw,sample.left_ticks,sample.right_ticks)
            if stationary_pose is None:stationary_pose=pose
            assert sample.velocity_mps==0 and pose==stationary_pose,'declared stationary scene cannot follow a moving plant'
            capture_pi_us=int(pi_from_host(capture_host)*1e6)
            assert sample.pi_sample_us==capture_pi_us
            command('vehicle '+encode(sample).hex())
            if last_world_capture is None or capture_host-last_world_capture>=world_period_us/1e6:
                gap_us=None if last_world_capture is None else int((capture_host-last_world_capture)*1e6)
                last_world_capture=capture_host
                # New scene observation at the captured plant state, not a
                # received World whose old timestamps/E are being renewed.
                value=modern_world();trust=provider.trust
                assert trust.lower_offset_us<=mapper_offset_us<=trust.upper_offset_us
                for key in ('vehicle','mapper_boot','mapper_instance','map_epoch','frame_epoch','odom_epoch',
                        'configuration','calibration','primitive','clock_epoch'):setattr(value,key,getattr(trust,key))
                assert world_sequence<2**63-1;world_sequence+=1
                value.sequence=value.observation_seq=world_sequence
                # Geometry/count가 같은 stationary plant의 basis revision은 그대로다.
                # 관측/World 순번 증가를 실제 reference append로 가장하지 않는다.
                value.reference_odom_seq=sample.sequence
                value.observed_us=capture_pi_us-mapper_offset_us
                value.localization.observed_mapper_us=value.observed_us
                obstacle(value,14,17,10,14)
                completed_host=time.monotonic()
                value.completed_us=int(pi_from_host(completed_host)*1e6)-mapper_offset_us
                published=[]
                def world_line():
                    published_host=time.monotonic()
                    value.published_us=int(pi_from_host(published_host)*1e6)-mapper_offset_us
                    raw=encode(value);published.append((published_host,raw))
                    return 'world '+raw.hex()

                rc,guest=command(world_line)
                published_host,raw=published[0];world_sources_total+=1
                if len(world_sources)<64:
                    world_sources.append(dict(observation_sequence=value.observation_seq,world_sequence=value.sequence,
                        vehicle_sequence=sample.sequence,capture_host_s=capture_host,capture_pi_us=capture_pi_us,
                        completed_host_s=completed_host,published_host_s=published_host,
                        observed_mapper_us=value.observed_us,completed_mapper_us=value.completed_us,published_mapper_us=value.published_us,
                        mapper_offset_us=mapper_offset_us,reference_revision=value.reference_revision,
                        basis_sha256=sha256(b''.join(point.SerializeToString(deterministic=True) for point in value.reference)).hexdigest(),
                        world_sha256=sha256(raw).hexdigest(),capture_gap_us=gap_us,
                        late_us=0 if gap_us is None else max(0,gap_us-world_period_us),
                        gap_reaches_live_age=gap_us is not None and gap_us>=manifest['profile']['live_age_us'],
                        link_rc=rc,guest_end_us=guest,transport=dict(app.last_transport)))
        def exchange():
            nonlocal first_state,first_request,admitted_phase
            while queue:
                clock()
                topic,raw,received,received_host,transport=queue.pop(0)
                body=decode(raw,schema.DriveLink);kind=body.WhichOneof('payload')
                if kind=='snapshot':
                    if body.snapshot.episode.core.state==2:
                        # The original receipt's progress sample is not the new
                        # actual calculation sample. No raw field is rewritten.
                        assert body.snapshot.episode.progress.vehicle_sequence==body.snapshot.vehicle_sequence
                    identity=body.snapshot.message_id
                    if identity in snapshot_identities:
                        assert snapshot_identities[identity]==raw,'one State ID cannot be reused for different bytes'
                    else:
                        assert len(snapshot_identities)<64,'bounded original State identity evidence'
                        snapshot_identities[identity]=raw
                prepared=(w.io.prepare_snapshot(body) if kind=='snapshot' else
                    episode_request_body(raw) if kind=='episode_request' else budget_request_body(raw) if kind=='budget_request' else None)
                accepted=provider.receive(w.peer[0],body,received,prepared)
                seen.append(('actor',kind,accepted,dict(provider.reasons)))
                if kind=='probe':
                    note_clock(dict(direction='guest-probe',sequence=body.probe.sequence,
                        guest_send_us=body.probe.guest_send_us,read_host_s=received_host,
                        pi_receive_us=int(received*1e6),raw_sha256=sha256(raw).hexdigest(),
                        accepted=accepted,transport=transport))
                if accepted and kind=='probe' and body.probe.HasField('receipt'):
                    receipt=body.probe.receipt
                    lease=w.o.clocks.lease(w.peer,receipt.probe_sequence,w.o.now,w.w.m.generation)
                    original=w.o.clocks.sent.get((*w.peer,receipt.probe_sequence))
                    assert lease is not None and original is not None
                    assert lease.digest==receipt.reply_sha256==sha256(original.raw).digest()
                    confirmed.append((receipt.probe_sequence,receipt.reply_sha256.hex()))
                    seen.append(('clock-confirmed',*confirmed[-1]))
                if kind=='snapshot' and accepted:
                    if body.snapshot.episode.core.phase==0 and first_state is None:first_state=raw
                    elif body.snapshot.episode.core.state==2 and admitted_phase is None:admitted_phase=raw
                if kind=='episode_request' and first_request is None:
                    first_request=raw;request=body.episode_request
                    proof=w.o.clocks.lease(w.peer,request.clock_probe_sequence,w.o.now,w.w.m.generation)
                    birth=w.o.episodes._meter(request.candidate.origin_vehicle_sequence,historical=True)
                    # Preserve the original emitted request and the issuer's
                    # actual proof even if a later State/DRIVE assertion fails.
                    (tmp_path/'actual-original-episode-request.bin').write_bytes(raw)
                    (tmp_path/'actual-original-event-proof.json').write_text(json.dumps(dict(
                        request_sha256=sha256(episode_request_body(raw)).hexdigest(),
                        began_guest_us=request.began_guest_us,phase_guest_us=request.phase_started_guest_us,
                        completed_guest_us=request.completed_guest_us,clock_probe_sequence=request.clock_probe_sequence,
                        clock_reply_sha256=request.clock_reply_sha256.hex(),
                        original_clock=None if proof is None else dict(support_guest_us=proof.support_guest_us,
                            lower=proof.lower,upper=proof.upper,expires_pi_us=proof.expires,digest=proof.digest.hex()),
                        birth_vehicle=None if birth is None else dict(sequence=birth.vehicle_sequence,pi_sample_us=birth.pi_sample_us,
                            clock_error_us=birth.clock_error_us,left_ticks=birth.left_ticks,right_ticks=birth.right_ticks),
                        current_vehicle_sequence=request.vehicle_sequence,
                        began_interval=w.o.clocks.interval(w.peer,request.clock_probe_sequence,request.began_guest_us,w.o.now,w.w.m.generation),
                        phase_interval=w.o.clocks.interval(w.peer,request.clock_probe_sequence,request.phase_started_guest_us,w.o.now,w.w.m.generation),
                        scope='original C bytes and actual Gateway historical clock/meter lookup; no physical APPLIED'),indent=2))
            with w.io.lock:
                outgoing=[(key,w.io.pending.pop(key)) for key in tuple(w.io.pending) if key[0]==inbound(w.peer[0])]
            for key,value in outgoing:
                submitted=[]
                def link_line():
                    # command() already refreshed bindings before entering ask.
                    # stamp -> linear text envelope -> pipe write; no full
                    # codec, actor or cache reinstall may intervene here.
                    wire=value.stamp(w.o.now) if isinstance(value,OwnedPreparedClock) else value
                    submitted.append(wire)
                    return 'link '+wire.hex()

                rc,guest=command(link_line)
                wire=submitted[0]
                if isinstance(value,OwnedPreparedClock):
                    original=decode(wire,schema.DriveLink).clock;parent=original.vehicle
                    assert parent.HasField('estimated_steering_rad') is steering_available
                    assert not parent.HasField('measured_steering_rad')
                    if steering_available:assert parent.steering_error_rad>0
                    sent_clock=SentClock(value.peer,value.generation,wire)
                    note_clock(dict(direction='pi-reply',sequence=original.probe.sequence,
                        guest_send_us=original.probe.guest_send_us,pi_receive_us=original.pi_receive_us,
                        pi_send_us=original.pi_send_us,original_support_end_pi_us=original.guest_clock_valid_until_pi_us,
                        replay=value.replay is not None,raw_sha256=sha256(wire).hexdigest(),
                        link_rc=rc,guest_end_us=guest,transport=dict(app.last_transport)))
                    # Like DriveLinkIo: stamp immediately before the transport,
                    # then notify the actor after the original bytes were sent.
                    # Actor work between P2 and the pipe inflated clock width.
                    clock();w.io.clock_completions.put_nowait(sent_clock);provider.tick()
        try:
            binding=w.r.bindings.current(w.peer[0],w.w.t)
            # The envelope below uses the exact C bootstrap/binding identity.
            # A different boot/app source must not be re-labelled as this producer.
            assert command('boot '+' '.join((*w.peer,w.r.authority.host,binding['boot_id'],provider.instance)))[0]==0
            control=json.dumps(w.w.m.control_payload('b08-software-bench'),separators=(',',':')).encode()
            assert command('control '+control.hex())[0]==0
            end=time.monotonic()+12
            while time.monotonic()<end and selected_drive is None:
                # One real wall clock drives both processes. No source time/E is
                # assigned to skip a failed clock or slow real planner result.
                # One declared coordinator turn, not 32 planner owner loops
                # whose WIRE/P4 would wait behind the same pipe END. This is
                # still sequential software scheduling, not an RTOS/HIL claim.
                refresh();command('step 1');exchange()
                if event_boundary and first_request is not None:
                    request=decode(first_request,schema.DriveLink).episode_request
                    lookup=f'event-clock {request.began_guest_us} {request.phase_started_guest_us}'
                    rc,at=command(lookup);original=seen[-1][1]
                    assert rc==0 and original['rc']==0 and original['sequence']==request.clock_probe_sequence
                    assert original['confirmed_guest_us']<=original['reserved_guest_us']<=request.began_guest_us
                    assert original['support_guest_us']<=request.began_guest_us<=request.phase_started_guest_us<=request.completed_guest_us
                    assert at<original['expires_guest_us']
                    if event_boundary=='context':
                        command('disconnect')
                    else:
                        # Real host time and fresh source/P4 continue. Only the
                        # old event proof expires; its E is never renewed.
                        until=time.monotonic()+2
                        while at<=original['expires_guest_us'] and time.monotonic()<until:
                            refresh();command('io');exchange();time.sleep(.003)
                            _,at=command('io');exchange()
                        assert at>original['expires_guest_us']
                    rc,at=command(lookup);rejected=seen[-1][1]
                    assert rc!=0 and rejected['rc']!=0
                    command('meta');meta=seen[-1][1]
                    if event_boundary=='expiry':assert meta['clock_rc']==0 and meta['clock_acknowledged']
                    else:assert meta['clock_rc']!=0
                    (tmp_path/'actual-event-clock-boundary.json').write_text(json.dumps(dict(
                        scope='actual C/P4 pin; declared transport loss or real original expiry; no DRIVE/HIL acceptance',
                        boundary=event_boundary,original=original,rejected=rejected,current=meta,guest_end_us=at),indent=2))
                    return
                calculation+=1;command('calc '+str(calculation));command('io');exchange()
                if not steering_available and calculation%4==0:
                    command('meta')
                    observed=next(row[1] for row in reversed(seen) if row[0]=='meta')
                    # Record the veto while its original input preconditions coexist.
                    # A later diagnostic can outlive facts; it must not replace this
                    # actual observation or refresh any producer's original E.
                    if (observed['clock_rc']==0 and observed['clock_acknowledged']
                            and observed['clock_probe_sequence']>0 and observed['maps_published']>0
                            and observed['map_current']>=0 and observed['has_grant']
                            and observed['facts_current'] and observed['search_reason_name']=='INPUT_INVALID'):
                        negative_observation=dict(observed)
                        break
                time.sleep(.003)
            command('meta');command('life')
            (tmp_path/'actual-c.transcript.json').write_text(json.dumps(app.transcript,indent=2))
            (tmp_path/'actor-observations.json').write_text(json.dumps(seen,indent=2))
            assert confirmed and all(sequence>0 for sequence,_ in confirmed),seen[-12:]
            if not steering_available:
                assert negative_observation is not None,seen[-12:]
                meta=negative_observation
                (tmp_path/'actual-missing-steering-veto.json').write_text(json.dumps(meta,indent=2))
                assert meta['clock_rc']==0 and meta['clock_acknowledged'] and meta['clock_probe_sequence']>0
                assert meta['maps_published']>0 and meta['map_current']>=0 and meta['has_grant'] and meta['facts_current']
                assert meta['search_reason_name']=='INPUT_INVALID',meta
                assert not meta['prepared'] and meta['path_current']<0
                assert first_state is first_request is admitted_phase is None
                assert w.o.episodes.status(w.peer).commit_state==1
                commands=[row[1] for row in seen if row[0]=='command']
                assert commands and all(row['command_kind']=='STOP' and row['v_mps']==row['steer_rad']==0 for row in commands)
                return
            assert first_state is not None,seen[-12:]
            assert first_request is not None,seen[-12:]
            assert admitted_phase is not None,seen[-12:]
            initial=decode(first_state,schema.DriveLink).snapshot
            request=decode(first_request,schema.DriveLink).episode_request
            active=decode(admitted_phase,schema.DriveLink).snapshot
            reserved=str(UUID(bytes=request.phase_state_message_id))
            first_reserved=decode(snapshot_identities[reserved],schema.DriveLink).snapshot
            assert first_reserved==active,'old completion must not consume the new phase State ID'
            assert first_reserved.episode.core.state==2
            assert not initial.direction and not initial.task_reference.changing
            assert request.action==1 and w.o.episodes.registry['state'] is not None
            assert request.candidate.episode_id==active.episode.core.episode_id
            assert active.episode.acceptance.request_sha256==sha256(episode_request_body(first_request)).digest()
            assert active.calculation_sequence>request.calculation_sequence
            assert w.o.episodes.status(w.peer).commit_state==2
            # The actual C produced these bytes; neither app READY nor command
            # count is used to fabricate the registry/fullState condition.
            assert any(row[0]=='command' for row in seen)
            assert any(row[0]=='command' and int(row[1]['clock_probe_sequence'])>0 for row in seen)
            assert selected_drive is not None,seen[-12:]
            body,original=selected_drive
            (tmp_path/'selected-drive-command3.json').write_text(original)
            (tmp_path/'actual-drive-candidate.json').write_text(json.dumps(dict(command=body,
                bootstrap=dict(vm=w.peer[0],execution=w.peer[1],app_instance=w.peer[2],boot=binding['boot_id']),
                first_accepted_state_calculation=active.calculation_sequence,
                observed_pi_us=w.o.now,generation=w.w.m.generation,mode=w.w.m.mode.value,
                ready=w.w.m._ready(w.peer[0],'autonomous',w.w.t)),indent=2))
            assert int(body['calculation_sequence'])>active.calculation_sequence
            assert body['mission_id']==w.o.missions.current()['id']
            assert int(body['mission_revision'])==active.mission.mission_revision
            assert int(body['grant_revision'])==active.mission.grant_revision
            assert int(body['plan_revision'])==active.path_revision and int(body['phase_revision'])==active.maneuver_revision
            assert int(body['clock_probe_sequence'])>0 and int(body['valid_until_pi_us'])>w.o.now
            assert body['profile']=='drive-command-3' and body['producer_role']=='AUTO' and body['v_mps']>0
            # Same actual command ingress and one OutputOwner as the earlier
            # software boundary. This stationary plant records submissions;
            # no serial receipt or physical APPLIED is invented here.
            device=SnapshotDevice(w.sink);owner=OutputOwner(device,command_timeout_s=.1,clock=current_pi_clock)
            previous=w.r._sink;w.r._sink=owner
            before=len(w.sink.frames)
            try:
                clock();before_ready=w.w.m._ready(w.peer[0],'autonomous',w.w.t)
                message=receive_original(w,w.peer[0],body,1,emit=True)
                diagnostic=ingress_diagnostics(w,w.peer[0],owner)
                diagnostic.update(before_ready=before_ready,actual_c_boot=binding['boot_id'],
                    original_calculation_sequence=body['calculation_sequence'],original_valid_until_pi_us=body['valid_until_pi_us'],
                    first_accepted_state_calculation=active.calculation_sequence,
                    source_scope='actual C bytes; typed stationary device submission only; no physical APPLIED')
                (tmp_path/'actual-drive-ingress.json').write_text(json.dumps(diagnostic,indent=2,default=str))
                key=(w.peer[0],'autonomous',w.peer[1],w.peer[2],body['generation'])
                assert w.o.command_hwm.get(key)==int(body['calculation_sequence']),diagnostic
                frames=[frame for _,frame,_ in list(w.sink.frames)[before:]
                    if isinstance(frame,DriveFrame) and frame.witness]
                assert frames,diagnostic
                frame=frames[-1]
                assert frame.witness['message_id']==message.envelope['message_id']
                assert frame.witness['command']==json.loads(original)
                assert frame.witness['execution_id']==w.peer[1] and frame.witness['agent_instance_id']==w.peer[2]
                assert frame.expires_pi_us<=int(body['valid_until_pi_us'])
                assert all(row.get('device_applied') is not True for row in w.r.submissions)
            finally:owner.close();w.r._sink=previous
        finally:
            try:app.close()
            finally:
                (tmp_path/'actual-c.transcript.json').write_text(json.dumps(app.transcript,indent=2))
                (tmp_path/'actor-observations.json').write_text(json.dumps(seen,indent=2))
                (tmp_path/'actual-c-clock-leaf.json').write_text(json.dumps(dict(
                    scope='declared text-pipe leaf; original complete read and one pre-write stamp',
                    cap=64,total=clock_leaf_total,dropped=clock_leaf_total-len(clock_leaf),events=clock_leaf),indent=2))
                (tmp_path/'actual-c-world-source.json').write_text(json.dumps(dict(
                    scope='new observations of a stationary software scene/encoder; no Jetson/physical/HIL claim',
                    period_us=world_period_us,cap=64,total=world_sources_total,dropped=world_sources_total-len(world_sources),
                    live_age_us=manifest['profile']['live_age_us'],observations=world_sources),indent=2))
                w.io.pending.clear()
