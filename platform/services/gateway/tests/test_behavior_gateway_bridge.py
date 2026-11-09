"""Actual C app/input/codec ↔ Gateway actor/clock/registry integration on Nano.

RTOS scheduling, authenticated pipe transport, placed World and stationary plant
are explicit leaves. No READY/clock confirmation/registration receipt is injected.
The typed operator-world producer is only the initial control premise. A fresh
manager-observed app identity belongs to the native producer from its bootstrap;
only its original C lifecycle can qualify that source at Gateway ingress.
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
from xenova_mqtt import Identity,topics
from xenova_mqtt.client import Message
from xenova_mqtt.envelope import build
from test_behavior_app_bridge import app_bridge_binary
from test_command3_gateway_flow import SnapshotDevice,receive_original,ingress_diagnostics
from test_engine import modern_world
from test_lattice import obstacle
from test_operator_runtime import world
from b08_fixture import activate,journal


class AdmissionTrace:
    """First three actual ACTIVE admissions only; preserve every original call.

    No copied policy/extra validation or input updates. Actual clock reads and
    the actual device_view returned to _meter are observed, then scalars are
    recorded after the original meter call has returned. Restore at teardown.
    """
    def __init__(self,w):
        import xenova_drive_world as fingerprints
        self.w=w;self.module=fingerprints;self.records=[];self.current=None;self.meter_current=None;self.pipeline=None
        self.original_clock=w.r._clock;self.original_view=w.r.device_view
        self.original_prepare=w.o.episodes.prepare_state;self.original_meter=w.o.episodes._meter
        self.original_core=fingerprints.episode_core_sha256;self.original_plan=fingerprints.plan_sha256
        w.r._clock=self.clock;w.r.device_view=self.view
        w.o.episodes.prepare_state=self.prepare;w.o.episodes._meter=self.meter
        fingerprints.episode_core_sha256=self.core;fingerprints.plan_sha256=self.plan

    def clock(self):
        value=self.original_clock()
        if self.pipeline is not None and len(self.pipeline['actual_actor_clock_reads'])<16:
            self.pipeline['actual_actor_clock_reads'].append(int(value*1e6))
        if self.meter_current is not None:
            reads=self.meter_current['actual_pi_now_reads']
            if len(reads)<16:reads.append(int(value*1e6))
            else:self.meter_current['clock_reads_lost']+=1
        return value

    def view(self):
        value=self.original_view()
        if self.meter_current is not None:self.meter_current['_actual_view']=value
        return value

    @staticmethod
    def anchor(value):
        if value is None:return None
        return {key:getattr(value,key,None) for key in ('meter_epoch','gap_revision','valid','total_ticks',
            'metres_per_tick','host_boot','pi_session','device_session','odom_epoch','calibration',
            'vehicle_sequence','source_sample','source_us','pi_sample_us','clock_error_us')}

    def meter(self,sequence=None,*,historical=False):
        if self.current is None or len(self.current['meters'])>=3:
            return self.original_meter(sequence,historical=historical)
        record=dict(sequence=sequence,historical=historical,actual_pi_now_reads=[],clock_reads_lost=0)
        self.meter_current=record
        try:
            value=self.original_meter(sequence,historical=historical)
        finally:self.meter_current=None
        actual=record.pop('_actual_view',{})
        current,history=actual.get('absolute_meter') or (None,())
        anchor=current if sequence is None else next((x for x in history if x.vehicle_sequence==sequence),None)
        record.update(current=self.anchor(current),selected=self.anchor(anchor),returned=self.anchor(value),
            vehicle_sample={k:(actual.get('vehicle_sample') or {}).get(k) for k in
                ('host_boot','pi_session','device_session','odom_epoch','calibration','sequence','pi_sample_us','clock_error_us')})
        self.current['meters'].append(record)
        return value

    def core(self,value):return self.hash_call('core',self.original_core,value)
    def plan(self,value):return self.hash_call('plan',self.original_plan,value)
    def hash_call(self,name,original,value):
        if self.current is None:return original(value)
        began=self.original_clock();result=original(value);ended=self.original_clock()
        if len(self.current['hash_calls'])<4:
            self.current['hash_calls'].append(dict(kind=name,sha256=result.hex(),
                begin_pi_us=int(began*1e6),end_pi_us=int(ended*1e6)))
        return result

    @staticmethod
    def progress(value):
        return {key:getattr(value,key) for key in ('used_travel_ticks','last_progress_pi_us',
            'vehicle_sequence','station_lower_m','remaining_upper_m','revision')}

    def prepare(self,state,peer,raw):
        if not state.HasField('episode') or state.episode.core.state!=2 or len(self.records)>=3:
            return self.original_prepare(state,peer,raw)
        owner=self.w.o.episodes;registry=owner.registry;expected=None if registry is None else registry['snapshot']
        request=None if registry is None else registry['request']
        record=dict(begin_pi_us=int(self.original_clock()*1e6),peer=peer,state_id=state.message_id,
            vehicle=state.vehicle_sequence,calculation=state.calculation_sequence,plan=state.path_revision,
            phase=state.maneuver_revision,progress=self.progress(state.episode.progress),
            core_phase=state.episode.core.phase,core_meter_epoch=state.episode.core.meter_epoch.hex(),
            core_meter_baseline=state.episode.core.meter_baseline_total_ticks,
            peer_features=owner.peers.get(peer),installed_episode_support=getattr(self.w.r,'episode_support',None),
            failed_before=owner.failed,registry_present=registry is not None,
            expected_core_phase=None if expected is None else expected.core.phase,
            direction=state.direction,maneuver_phase=state.maneuver_phase,
            task_changing=state.task_reference.changing if state.HasField('task_reference') else None,
            expected_core_sha256=None if expected is None else expected.acceptance.accepted_core_sha256.hex(),
            acceptance_equal=None if expected is None else state.episode.acceptance==expected.acceptance,
            previous_progress=None if expected is None else self.progress(expected.progress),
            progress_anchor=None if registry is None else registry['progress_anchor'],
            admitted_state_present=registry is not None and registry['state'] is not None,
            request=None if request is None else dict(peer=(request.vm,request.execution,request.app_instance),
                reserved_id=str(UUID(bytes=request.phase_state_message_id)),calculation=request.calculation_sequence,
                plan=request.plan_revision,phase=request.phase_revision,plan_sha256=request.plan_sha256.hex()),
            minimum_progress=(getattr(self.w.r,'planner_parameters',{}) or {}).get('avoid_progress_m'),
            meters=[],hash_calls=[])
        self.current=record
        try:
            result=self.original_prepare(state,peer,raw)
            record['accepted']=result is not None
            return result
        finally:
            record['end_pi_us']=int(self.original_clock()*1e6);record['failed_after']=owner.failed
            self.current=None;self.records.append(record)

    def close(self):
        self.w.r._clock=self.original_clock;self.w.r.device_view=self.original_view
        self.w.o.episodes.prepare_state=self.original_prepare;self.w.o.episodes._meter=self.original_meter
        self.module.episode_core_sha256=self.original_core;self.module.plan_sha256=self.original_plan


class App:
    WIRE_CAP=64

    def __init__(self,binary,stderr):
        self.process=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=stderr,bufsize=0)
        self.buffer=b'';self.transcript=[];self.buffer_received=None
        self.wire_received={};self.lifecycle_received={};self.transport_sequence=0;self.last_transport={}

    def ask(self,line):
        # A private late factory prepares only the text envelope immediately
        # before writing; callers keep the original (rc, guest, output) tuple.
        self.transport_sequence+=1;self.wire_received={};self.lifecycle_received={}
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
            if text.startswith('LIFECYCLE '):
                assert len(self.lifecycle_received)<self.WIRE_CAP,'bounded native lifecycle observation queue exhausted'
                self.lifecycle_received[len(output)-1]=self.buffer_received
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


@pytest.mark.parametrize('steering_available,event_boundary,scenario',
    [(True,None,'avoidance'),(False,None,'avoidance'),(True,'expiry','avoidance'),(True,'context','avoidance'),
     (True,None,'clear-lane-follow')],
    ids=['servo-estimate','steering-unavailable','event-clock-expiry','event-clock-context','clear-lane-follow'])
def test_actual_c_initial_none_then_registered_episode_full_state(world,app_bridge_binary,tmp_path,steering_available,event_boundary,scenario):
    w=activate(world);provider=w.r.drive_link
    admission_trace=None
    # activate's typed operator producer and this new process are distinct
    # owners. Observe its real app instance before issuing its mission/boot;
    # actual ingress accept_session, never a fixture counter reset, adopts it.
    previous_peer=w.peer
    w.w.ids[w.peer[0]]['autonomous']=str(uuid4());w.w.bind()
    w.peer=(w.peer[0],w.peer[1],w.w.ids[w.peer[0]]['autonomous'])
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
    calculation=0;seen=[];confirmed=[];first_state=None;first_request=None;admitted_state=None;selected_drive=None
    initial_value=None;admitted_value=None;request_value=None;admitted_order=None;actual_applications=[]
    negative_observation=None;snapshot_identities={};admission_pipeline=[]
    event_sequence=0;lifecycle_leaf=[];lifecycle_total=0;last_lifecycle=None
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
            nonlocal selected_drive,event_sequence,lifecycle_total,last_lifecycle
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
                    applied=admitted_value is not None and any(row['order']>admitted_order and
                        row['plan']==admitted_value.path_revision and row['phase']==admitted_value.maneuver_revision
                        for row in actual_applications)
                    if admitted_state is not None and applied and selected_drive is None and body['command_kind']=='DRIVE':
                        selected_drive=(body,original)
                elif value.startswith('LIFECYCLE '):
                    original=value.split(' ',1)[1];body=json.loads(original);seen.append(('lifecycle',body))
                    received_host=app.lifecycle_received[index];assert received_host is not None
                    received=pi_from_host(received_host)
                    event_sequence+=1
                    envelope=build(native_identity,'app.lifecycle',body,event_sequence)
                    message=Message(topics.vm_app_events(w.peer[0],'autonomous'),envelope,received,False)
                    old_session=w.w.m.sessions.get((w.peer[0],'autonomous'))
                    w.r._enqueue(message);w.r._drain()
                    key=w.peer[0],'autonomous';check=w.w.m.checks.get(key);prepared=w.w.m.prepared[key]
                    accepted=bool(check and check[0]==int(body['check_seq']) and check[1]==received
                        and prepared.generation==body['applied_generation']
                        and prepared.progress==int(body['progress_seq']))
                    last_lifecycle=dict(original=original,sha256=sha256(original.encode()).hexdigest(),
                        payload=body,envelope=envelope,original_read_pi_us=int(received*1e6),
                        original_read_host_s=received_host,actor_finished_pi_us=int(current_pi_clock()*1e6),
                        accepted=accepted,old_session=old_session,current_session=w.w.m.sessions.get(key),
                        actor_check=check,actor_state=prepared.state,actor_reason=prepared.reason)
                    lifecycle_total+=1
                    if len(lifecycle_leaf)<64:lifecycle_leaf.append(last_lifecycle)
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
                if scenario=='avoidance':obstacle(value,14,17,10,14)
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
            nonlocal first_state,first_request,admitted_state,initial_value,admitted_value,request_value,admitted_order
            while queue:
                clock()
                topic,raw,received,received_host,transport=queue.pop(0)
                decode_begin_pi_us=int(current_pi_clock()*1e6)
                body=decode(raw,schema.DriveLink);kind=body.WhichOneof('payload')
                stage=None
                if kind=='snapshot':
                    if len(admission_pipeline)<4:
                        stage=dict(state_id=body.snapshot.message_id,vehicle=body.snapshot.vehicle_sequence,
                            calculation=body.snapshot.calculation_sequence,core_state=body.snapshot.episode.core.state,
                            original_wire_read_pi_us=int(received*1e6),decode_begin_pi_us=decode_begin_pi_us,
                            decode_end_pi_us=int(current_pi_clock()*1e6),actual_actor_clock_reads=[])
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
                if stage is not None:
                    stage['prepared_encode_end_pi_us']=int(current_pi_clock()*1e6)
                    admission_trace.pipeline=stage
                try:accepted=provider.receive(w.peer[0],body,received,prepared)
                finally:
                    admission_trace.pipeline=None
                    if stage is not None:
                        stage['provider_return_pi_us']=int(current_pi_clock()*1e6);admission_pipeline.append(stage)
                seen.append(('actor',kind,accepted,dict(provider.reasons)))
                if accepted and kind=='applied' and body.applied.applied:
                    if len(actual_applications)==64:actual_applications.pop(0)
                    actual_applications.append(dict(order=len(seen)-1,plan=body.applied.path_revision,
                        phase=body.applied.maneuver_revision,vehicle=body.applied.vehicle_sequence,
                        application_sequence=body.applied.application_sequence,
                        preparation_sequence=body.applied.calculation_sequence,rebuilt=body.applied.rebuilt))
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
                    if body.snapshot.episode.core.phase==0 and first_state is None:
                        first_state=raw;initial_value=body.snapshot
                        if scenario=='clear-lane-follow':
                            admitted_state=raw;admitted_value=body.snapshot;admitted_order=len(seen)-1
                    elif body.snapshot.episode.core.state==2 and admitted_state is None:
                        admitted_state=raw;admitted_value=body.snapshot;admitted_order=len(seen)-1
                if kind=='episode_request' and first_request is None:
                    first_request=raw;request=body.episode_request;request_value=request
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
            admission_trace=AdmissionTrace(w)
            binding=w.r.bindings.current(w.peer[0],w.w.t)
            native_identity=Identity(vm_id=w.peer[0],app_id='autonomous',boot_id=binding['boot_id'],
                agent_instance_id=w.peer[2])
            # The envelope below uses the exact C bootstrap/binding identity.
            # A different boot/app source must not be re-labelled as this producer.
            assert command('boot '+' '.join((*w.peer,w.r.authority.host,binding['boot_id'],provider.instance)))[0]==0
            control=json.dumps(w.w.m.control_payload('b08-software-bench'),separators=(',',':')).encode()
            assert command('control '+control.hex())[0]==0
            command('life')
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
                calculation+=1;command('calc '+str(calculation))
                # Production warm_report classifies completed work separately
                # from command transport. Preserve its actual C report/receipt.
                command('life')
                if selected_drive is not None:break  # original command goes to ingress before optional logging/next I/O
                command('io');exchange()
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
            if selected_drive is None:command('meta');command('life')
            assert confirmed and all(sequence>0 for sequence,_ in confirmed),seen[-12:]
            if not steering_available:
                assert negative_observation is not None,seen[-12:]
                meta=negative_observation
                (tmp_path/'actual-missing-steering-veto.json').write_text(json.dumps(meta,indent=2))
                assert meta['clock_rc']==0 and meta['clock_acknowledged'] and meta['clock_probe_sequence']>0
                assert meta['maps_published']>0 and meta['map_current']>=0 and meta['has_grant'] and meta['facts_current']
                assert meta['search_reason_name']=='INPUT_INVALID',meta
                assert not meta['prepared'] and meta['path_current']<0
                assert first_state is first_request is admitted_state is None
                assert w.o.episodes.status(w.peer).commit_state==1
                commands=[row[1] for row in seen if row[0]=='command']
                assert commands and all(row['command_kind']=='STOP' and row['v_mps']==row['steer_rad']==0 for row in commands)
                return
            assert first_state is not None,seen[-12:]
            assert admitted_state is not None,seen[-12:]
            # Already decoded and admitted by the real provider; preserve its
            # original observer objects/bytes instead of re-decoding under a
            # selected command's short output lease. No actor validation skips.
            initial=initial_value;active=admitted_value
            assert not initial.direction and not initial.task_reference.changing
            if scenario=='clear-lane-follow':
                assert first_request is None and active==initial
                assert active.episode.core.state==1 and active.episode.core.phase==0
                assert not active.direction and not active.task_reference.changing
            else:
                assert first_request is not None,seen[-12:]
                request=request_value
                reserved=str(UUID(bytes=request.phase_state_message_id))
                assert snapshot_identities[reserved]==admitted_state,'old completion must not consume the new phase State ID'
                assert active.episode.core.state==2
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
            applications=[row for row in actual_applications if row['order']>admitted_order and
                row['plan']==active.path_revision and row['phase']==active.maneuver_revision]
            assert applications,'actual C APPLIED for the admitted path/phase must precede the selected fresh command'
            body,original=selected_drive
            assert int(body['calculation_sequence'])>active.calculation_sequence
            assert body['mission_id']==w.o.missions.current()['id']
            assert int(body['mission_revision'])==active.mission.mission_revision
            assert int(body['grant_revision'])==active.mission.grant_revision
            assert int(body['plan_revision'])==active.path_revision and int(body['phase_revision'])==active.maneuver_revision
            assert int(body['clock_probe_sequence'])>0 and int(body['valid_until_pi_us'])>w.o.now
            assert body['profile']=='drive-command-3' and body['producer_role']=='AUTO' and body['v_mps']>0
            assert last_lifecycle is not None and last_lifecycle['accepted']
            assert last_lifecycle['payload']['readiness']=='READY'
            assert last_lifecycle['payload']['applied_generation']==body['generation']
            selected_lifecycle=last_lifecycle
            # Same actual command ingress and one OutputOwner as the earlier
            # software boundary. This stationary plant records submissions;
            # no serial receipt or physical APPLIED is invented here.
            device=SnapshotDevice(w.sink);owner=OutputOwner(device,command_timeout_s=.1,clock=current_pi_clock)
            previous=w.r._sink;w.r._sink=owner
            before=len(w.sink.frames)
            try:
                clock();before_ready=w.w.m._ready(w.peer[0],'autonomous',w.w.t)
                event_sequence+=1
                message=receive_original(w,w.peer[0],body,event_sequence,emit=True)
                diagnostic=ingress_diagnostics(w,w.peer[0],owner)
                # Submission has consumed the untouched command. Evidence I/O
                # and lifecycle observation must not be its artificial delay.
                if scenario=='clear-lane-follow':(tmp_path/'actual-clear-follow-state.bin').write_bytes(admitted_state)
                (tmp_path/'selected-drive-command3.json').write_text(original)
                (tmp_path/'actual-drive-candidate.json').write_text(json.dumps(dict(command=body,scenario=scenario,
                    bootstrap=dict(vm=w.peer[0],execution=w.peer[1],app_instance=w.peer[2],boot=binding['boot_id']),
                    first_accepted_state_calculation=active.calculation_sequence,
                    observed_pi_us=w.o.now,generation=w.w.m.generation,mode=w.w.m.mode.value,
                    ready=w.w.m._ready(w.peer[0],'autonomous',w.w.t)),indent=2))
                command('meta');command('life')
                diagnostic.update(before_ready=before_ready,actual_c_boot=binding['boot_id'],scenario=scenario,
                    actual_c_lifecycle=selected_lifecycle,
                    actual_post_state_applications=applications,
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
            if admission_trace is not None:admission_trace.close()
            try:app.close()
            finally:
                (tmp_path/'actual-state-admission.json').write_text(json.dumps(dict(
                    scope='bounded read-only wrappers; original prepare/meter/hash calls and returns unchanged',
                    attempt_cap=3,meter_cap_per_attempt=3,clock_read_cap=16,scenario=scenario,pipeline=admission_pipeline,
                    records=[] if admission_trace is None else admission_trace.records),indent=2))
                (tmp_path/'actual-c.transcript.json').write_text(json.dumps(app.transcript,indent=2))
                (tmp_path/'actor-observations.json').write_text(json.dumps(seen,indent=2))
                (tmp_path/'actual-c-clock-leaf.json').write_text(json.dumps(dict(
                    scope='declared text-pipe leaf; original complete read and one pre-write stamp',
                    cap=64,total=clock_leaf_total,dropped=clock_leaf_total-len(clock_leaf),events=clock_leaf),indent=2))
                (tmp_path/'actual-c-world-source.json').write_text(json.dumps(dict(
                    scope='new observations of a stationary software scene/encoder; no Jetson/physical/HIL claim',
                    period_us=world_period_us,cap=64,total=world_sources_total,dropped=world_sources_total-len(world_sources),
                    live_age_us=manifest['profile']['live_age_us'],observations=world_sources),indent=2))
                (tmp_path/'actual-c-lifecycle-leaf.json').write_text(json.dumps(dict(
                    scope='Original C lifecycle through real enqueue/drain; manager binding and text pipe are declared leaves',
                    typed_setup_peer=previous_peer,native_peer=w.peer,cap=64,total=lifecycle_total,
                    dropped=lifecycle_total-len(lifecycle_leaf),events=lifecycle_leaf),indent=2))
                w.io.pending.clear()
