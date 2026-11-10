"""1×1 ToF 원 관측과 실제 FD→serial owner→actor→기존 J 전달 회귀.

센서 모델·실측 보정·물리 STOP·FREE 근거를 만들지 않는다. 구 8×8은 운영 거절한다.
"""
from binascii import crc32
import json
import os
from pathlib import Path
import socket
import struct
from uuid import UUID,uuid4

import pytest

from xenova_drive_world import schema,decode,encode
from xenova_gateway.feedback import packet,Framer
from xenova_gateway.feedback3 import COMMAND,REQUIRED_CAPS
from xenova_gateway.frame import StopFrame
from xenova_gateway.mapper_uplink import _Link,unframe
from xenova_gateway.scene_authority import COMMAND3
from xenova_gateway.tof_feedback3 import (TOF_SAMPLE3,TOF_COMMON3,TOF_SAMPLE,TOF_CAPABILITY,
    TofClock,TofInbox,TofReceiver)
from tof_feedback_fixture import selection,sample_frames,WireBench,HOST,GATEWAY,DEVICE


def typed(*,profiles=((0,'c'*64,4),(1,'d'*64,5))):
    context=(uuid4().bytes,UUID(DEVICE).bytes,uuid4().bytes,True)
    return TofReceiver(selection(profiles)),TofInbox(),context,TofClock(context[2],1,100000,100100,1100000,2100000,10)

def submit(inbox,frames,clock,at=1100000):
    for payload in frames:assert inbox.offer(payload,at,clock)

def run(owner,inbox,context,at=1100000):
    loss,items=inbox.drain();return owner.process(loss,items,context,at)

def sample(context,**kwargs):
    stamp=kwargs.pop('stamp',998000+min(1000,kwargs.get('sample',1)-1)*1000)
    return sample_frames(*context[:3],1,stamp=stamp,**kwargs)


def test_independent_front_rear_vectors_have_one_packet_original_crc_and_exact_scalar_shape():
    source=Path(__file__).resolve().parents[4]/'docs/contracts/vectors/gateway-tof-single-1.json'
    values=json.loads(source.read_text(encoding='utf-8'))
    assert (TOF_SAMPLE,TOF_CAPABILITY,TOF_SAMPLE3.size,TOF_COMMON3.size)==(0x0f17,0x0800,145,137)
    for row in values['frames']:
        payload=bytes.fromhex(row['payload_hex']);body=bytes.fromhex(row['source_crc_body_hex'])
        assert len(body)==141 and payload[:-4]==body and crc32(body)&0xffffffff==row['source_crc32']
        assert packet(TOF_SAMPLE,payload)==bytes.fromhex(row['wire_hex'])
        assert Framer().feed(bytes.fromhex(row['wire_hex']))==[(TOF_SAMPLE,payload)]
        assert len(bytes.fromhex(row['wire_hex']))<=152
        fields=TOF_SAMPLE3.unpack(payload);received=fields[3]+100000
        owner=TofReceiver(selection(((fields[7],fields[11].hex(),fields[10]),)))
        inbox=TofInbox();clock=TofClock(fields[5],fields[6],100000,100100,received,received+1000000,0)
        submit(inbox,[payload],clock,received)
        raw,_=run(owner,inbox,(fields[0],fields[1],fields[5],True),received)
        value=decode(raw[0],schema.TofObservation)
        assert (value.rows,value.cols,value.distance_mm_le,value.status,value.quality)==(1,1,struct.pack('<H',row['distance_mm']),b'\1',b'\3')
        assert value.source_us==fields[3] and value.source_sample==fields[2] and value.source_crc32==row['source_crc32']


def test_one_packet_completes_immediately_and_duplicates_never_retime_or_republish():
    owner,inbox,context,clock=typed();frames,body=sample(context)
    assert len(frames)==1
    submit(inbox,frames,clock);raw,_=run(owner,inbox,context);assert len(raw)==1
    value=decode(raw[0],schema.TofObservation)
    assert owner.payload_bytes()==141 and owner.sensors[0]['latest']['body']==body
    assert all('partial' not in state for state in owner.sensors.values())
    original=owner.sensors[0]['latest']['received']
    submit(inbox,frames,clock,1100100);assert run(owner,inbox,context,1100100)[0]==()
    assert owner.sensors[0]['latest']['received']==original and owner.counts['duplicate']==1
    end=value.pi_sample_us+500000-value.clock_error_us-value.sensor_time_error_us
    assert run(owner,inbox,context,end-1)[0]==()
    assert owner.sensors[0]['latest'] is not None
    assert 0 in run(owner,inbox,context,end)[1] and owner.sensors[0]['latest'] is None
    submit(inbox,frames,clock,end);assert run(owner,inbox,context,end)[0]==()


def test_single_front_and_rear_samples_keep_independent_source_and_latest_only():
    owner,inbox,context,clock=typed()
    for sensor in (0,1):submit(inbox,sample(context,sensor=sensor)[0],clock)
    values=[decode(raw,schema.TofObservation) for raw in run(owner,inbox,context)[0]]
    assert {value.sensor_id for value in values}=={0,1} and owner.payload_bytes()==282
    submit(inbox,sample(context,sample=2)[0],clock)
    assert decode(run(owner,inbox,context)[0][0],schema.TofObservation).source_sample==2
    submit(inbox,sample(context)[0],clock);assert run(owner,inbox,context)[0]==()
    assert owner.sensors[1]['latest']['sample']==1


@pytest.mark.parametrize('fault',['old-generation','same-generation-instance','same-instance-generation','generation-wrap'])
def test_sensor_reset_lifetime_rejects_replay_and_instance_relabelling(fault):
    owner,inbox,context,clock=typed();submit(inbox,sample(context,generation=2)[0],clock)
    assert run(owner,inbox,context)[0]
    kwargs={'old-generation':dict(generation=1),'same-generation-instance':dict(generation=2,instance=uuid4().bytes),
        'same-instance-generation':dict(generation=3),'generation-wrap':dict(generation=1)}[fault]
    if fault=='generation-wrap':
        submit(inbox,sample(context,sample=2,generation=(1<<32)-1,instance=uuid4().bytes)[0],clock)
        assert run(owner,inbox,context)[0]
    submit(inbox,sample(context,sample=3,**kwargs)[0],clock);assert run(owner,inbox,context)[0]==()


def test_valid_sensor_reset_publishes_one_new_instance_and_leaves_other_sensor_alone():
    owner,inbox,context,clock=typed()
    for sensor in (0,1):submit(inbox,sample(context,sensor=sensor)[0],clock)
    assert len(run(owner,inbox,context)[0])==2
    instance=uuid4().bytes
    submit(inbox,sample(context,generation=2,instance=instance,stamp=999000)[0],clock)
    raw,revoked=run(owner,inbox,context)
    assert 0 in revoked and decode(raw[0],schema.TofObservation).sensor_instance==instance
    assert owner.sensors[1]['identity'][1]==1


@pytest.mark.parametrize('fault',['epoch','expired-clock','capture-age','calibration','invalid-counter','timing','rows'])
def test_original_source_clock_capture_expiry_and_calibration_are_not_replaced_by_arrival(fault):
    owner,inbox,context,clock=typed();frames,_=sample(context)
    if fault=='epoch':clock=TofClock(uuid4().bytes,1,100000,100100,1100000,2100000,10)
    elif fault=='expired-clock':clock=TofClock(clock.epoch,1,100000,100100,1100000,1100000,10)
    elif fault=='capture-age':frames,_=sample(context,stamp=1000)
    elif fault=='calibration':frames,_=sample(context,calibration='e'*64)
    elif fault=='invalid-counter':frames,_=sample(context,sample=1<<63)
    elif fault=='rows':frames,_=sample(context,rows=8,cols=8)
    else:frames,_=sample(context,flags=1)
    submit(inbox,frames,clock);assert run(owner,inbox,context)[0]==() and owner.payload_bytes()==0


@pytest.mark.parametrize('distance,status,quality',[(0,1,0),(65535,1,0),(900,8,3),(900,1,32),(900,5,1)])
def test_crc_valid_but_inconsistent_single_measurement_is_rejected_by_owner_and_common_codec(distance,status,quality):
    owner,inbox,context,clock=typed();frames,_=sample(context,measurement=struct.pack('<HBB',distance,status,quality))
    submit(inbox,frames,clock);assert run(owner,inbox,context)[0]==() and owner.payload_bytes()==0
    owner,inbox,context,clock=typed();submit(inbox,sample(context)[0],clock)
    value=decode(run(owner,inbox,context)[0][0],schema.TofObservation)
    value.distance_mm_le=struct.pack('<H',distance);value.status=bytes([status]);value.quality=bytes([quality])
    with pytest.raises(ValueError):encode(value)


@pytest.mark.parametrize('status',[0,5,7])
def test_invalid_or_no_target_measurement_remains_original_provenance_without_free_claim(status):
    owner,inbox,context,clock=typed()
    submit(inbox,sample(context,measurement=struct.pack('<HBB',0,status,0),flags=2)[0],clock)
    value=decode(run(owner,inbox,context)[0][0],schema.TofObservation)
    assert value.status==bytes([status]) and value.distance_mm_le==b'\0\0' and value.quality==b'\0'


def test_crc_corruption_and_conflicting_duplicate_never_create_new_observation():
    owner,inbox,context,clock=typed();frames,_=sample(context);bad=bytearray(frames[0]);bad[-1]^=1
    submit(inbox,[bytes(bad)],clock);assert run(owner,inbox,context)[0]==()
    submit(inbox,sample(context,sample=2)[0],clock);assert run(owner,inbox,context)[0]
    changed,_=sample(context,sample=2,measurement=struct.pack('<HBB',800,1,3))
    submit(inbox,changed,clock);assert run(owner,inbox,context)[0]==() and owner.sensors[0]['latest'] is None


def test_short_or_legacy_fragment_payload_is_not_adopted_as_a_scalar_sample():
    owner,inbox,context,clock=typed();frames,_=sample(context)
    for bad in (frames[0][:-1],bytes(209)):
        assert not inbox.offer(bad,1100000,clock)
        assert run(owner,inbox,context)[0]==()


@pytest.mark.parametrize('field,value',[('rows',8),('cols',8),('distance_mm_le',bytes(128)),
    ('status',bytes(64)),('quality',bytes(64)),('complete',False),('sensor_generation',0),
    ('source_sample',0),('sensor_id',2),('frame_status',8)])
def test_common_decoder_rejects_old_grid_and_invalid_identity_without_accepting_default_values(field,value):
    owner,inbox,context,clock=typed();submit(inbox,sample(context)[0],clock)
    observation=decode(run(owner,inbox,context)[0][0],schema.TofObservation)
    setattr(observation,field,value)
    with pytest.raises(ValueError):decode(observation.SerializeToString(),schema.TofObservation)


def test_source_loss_and_overflow_retire_original_samples_without_affecting_other_sensor():
    owner,inbox,context,clock=typed()
    submit(inbox,sample(context,sensor=1)[0],clock);assert run(owner,inbox,context)[0]
    frames,_=sample(context)
    for _ in range(8):assert inbox.offer(frames[0],1100000,clock)
    assert not inbox.offer(frames[0],1100000,clock)
    assert run(owner,inbox,context)[0]==() and owner.sensors[1]['latest'] is not None
    submit(inbox,frames,clock);assert run(owner,inbox,context)[0]==()
    submit(inbox,sample(context,sample=2)[0],clock)
    assert run(owner,inbox,(*context[:3],False))[0]==()
    submit(inbox,sample(context,sample=2)[0],clock);assert run(owner,inbox,context)[0]==()


@pytest.mark.skipif(os.name!='posix',reason='actual explicit development FD')
def test_actual_fd_single_packet_mapper_handoff_preserves_original_bytes_and_expiry():
    bench=WireBench().install_clock();left=right=None
    try:
        runner,uplink=bench.actor();frames,_=bench.frame();epoch=bench.sink.odometry.epoch
        bench.feed_frames(frames);runner._mapper_tof_once()
        raw=uplink._tof_inputs[0];value=decode(raw,schema.TofObservation)
        assert (value.rows,value.cols)==(1,1) and value.source_us==TOF_SAMPLE3.unpack(frames[0])[3]
        assert value.pi_session==bench.sink.pi_session and value.clock_epoch==bench.sink.clock_epoch
        assert uplink.offer_tof(raw)
        queued=uplink.pending[('tof',0)];expected=value.pi_sample_us+500000-value.clock_error_us-value.sensor_time_error_us
        assert queued[1]==raw and queued[3]==expected
        left,right=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        link=_Link(left,(HOST,GATEWAY,str(uuid4()),str(uuid4())))
        assert link.offer(*queued);link.flush(int(bench.now*1e6))
        header,preserved=unframe(right.recv(4096));assert header['kind']=='SOURCE_TOF' and preserved==raw
        assert int(header['expires_pi_us'])==expected and not uplink.offer_tof(raw)
        bench.feed_frames(bench.frame(generation=2,instance=uuid4().bytes)[0]);runner._mapper_tof_once()
        assert ('tof',0) not in uplink.pending
        assert uplink.offer_tof(uplink._tof_inputs[0])
        with pytest.raises(ValueError,match='J_TOF_SOURCE_LIFETIME'):uplink.offer_tof(raw)
        assert bench.sink.odometry.epoch==epoch and bench.sink.fault is None
    finally:
        if left is not None:left.close();right.close()
        bench.close()


@pytest.mark.skipif(os.name!='posix',reason='actual explicit development FD')
def test_split_outer_frame_uses_completing_read_and_crc_loss_drops_only_tof():
    bench=WireBench().install_clock()
    try:
        runner,uplink=bench.actor();frames,_=bench.frame();wire=packet(TOF_SAMPLE,frames[0])
        bench.b.sendall(wire[:20]);bench.now+=.001;bench.sink.poll(bench.now)
        assert bench.sink.tof_inbox.inputs.empty()
        bench.b.sendall(wire[20:]);bench.now+=.001;bench.sink.poll(bench.now);runner._mapper_tof_once()
        assert runner._tof_receiver.sensors[0]['latest']['received']==int(bench.now*1e6)
        before=(bench.sink.odometry.epoch,bench.sink.clock_sequence,bench.sink.snapshot['vehicle_sample'])
        bad=bytearray(packet(TOF_SAMPLE,frames[0]));bad[-2]^=1
        bench.b.sendall(bytes(bad));bench.now+=.001;bench.sink.poll(bench.now);runner._mapper_tof_once()
        assert not uplink._tof_inputs and bench.sink.fault is None and bench.sink.snapshot['clock_ready']
        assert (bench.sink.odometry.epoch,bench.sink.clock_sequence,bench.sink.snapshot['vehicle_sample'])==before
    finally:bench.close()


@pytest.mark.skipif(os.name!='posix',reason='actual explicit development FD')
def test_outer_crc_loss_retires_same_read_batch_before_first_actor_adoption():
    bench=WireBench().install_clock()
    try:
        runner,uplink=bench.actor();frames,_=bench.frame()
        bad=bytearray(packet(TOF_SAMPLE,frames[0]));bad[-2]^=1
        bench.b.sendall(bytes(bad)+packet(TOF_SAMPLE,frames[0]));bench.now+=.001;bench.sink.poll(bench.now)
        runner._mapper_tof_once();assert not uplink._tof_inputs
        bench.feed_frames(frames);runner._mapper_tof_once();assert not uplink._tof_inputs
        bench.feed_frames(bench.frame(sample=2)[0]);runner._mapper_tof_once()
        assert decode(uplink._tof_inputs[0],schema.TofObservation).source_sample==2 and bench.sink.fault is None
    finally:bench.close()


@pytest.mark.parametrize('capabilities',[REQUIRED_CAPS,REQUIRED_CAPS|0x200,REQUIRED_CAPS|TOF_CAPABILITY])
@pytest.mark.skipif(os.name!='posix',reason='actual explicit development FD')
def test_absent_old_capability_legacy_opcode_and_queue_pressure_do_not_block_stop_or_encoder(capabilities):
    bench=WireBench(capabilities=capabilities)
    try:
        bench.install_clock();baseline=(bench.sink.odometry.epoch,bench.sink.clock_sequence,bench.sink.fault)
        bench.send(0x0f16,bytes(209))
        for number in range(1,10):bench.feed_frames(bench.frame(sample=number)[0])
        if capabilities&TOF_CAPABILITY:assert bench.sink.tof_inbox.counts['overflow']>0
        else:assert bench.sink.tof_inbox.inputs.empty()
        assert (bench.sink.odometry.epoch,bench.sink.clock_sequence,bench.sink.fault)==baseline
        bench.sink.write(StopFrame(7,0,1),bench.now)
        commands=[COMMAND3.unpack(raw) for kind,raw in bench.outgoing() if kind==COMMAND]
        assert commands and commands[-1][10]==2 and bench.sink.fault is None
    finally:bench.close()


@pytest.mark.skipif(os.name!='posix',reason='actual explicit development FD')
def test_empty_selection_or_missing_ack_is_optional_and_cannot_backfill_after_recovery():
    empty=WireBench(profiles=())
    try:
        empty.feed_frames(empty.frame()[0]);loss,items,context=empty.sink.tof_inputs()
        assert TofReceiver(selection(())).process(loss,items,context,int(empty.now*1e6))[0]==()
        assert empty.sink.ready and empty.sink.fault is None
    finally:empty.close()
    bench=WireBench()
    try:
        old,_=bench.frame();bench.feed_frames(old)
        assert bench.sink.tof_inbox.inputs.empty() and bench.sink.tof_inbox.counts['invalid']>0
        bench.install_clock();runner,uplink=bench.actor();runner._mapper_tof_once()
        bench.feed_frames(old);runner._mapper_tof_once();assert not uplink._tof_inputs
        bench.feed_frames(bench.frame(sample=2)[0]);runner._mapper_tof_once()
        assert decode(uplink._tof_inputs[0],schema.TofObservation).source_sample==2
        assert bench.sink.fault is None and bench.sink.snapshot['clock_ready']
    finally:bench.close()
