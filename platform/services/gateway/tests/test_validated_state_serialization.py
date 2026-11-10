"""Typed State callers keep full validation and current admission checks.

These small paths are codec fixtures, not C planner/physical success evidence.
The real actor sees unchanged prepared bytes; no permission token is supplied.
"""
from hashlib import sha256
from uuid import uuid4

import pytest

from xenova_drive_world import codec,encode,initial_none_snapshot,schema
from xenova_gateway import drive_link
from test_drive_link import snapshot
from test_operator_runtime import world
from b08_fixture import activate


def state_case(w):
    value=snapshot(w.w,w.r.drive_link.trust)
    value.snapshot.state_features|=16
    value.snapshot.episode.CopyFrom(initial_none_snapshot(w.o.episodes.status(w.peer)))
    return value


def observe_episode(monkeypatch,w):
    seen=[];original=w.o.episodes.prepare_state
    def prepare(state,peer,raw):
        seen.append(raw)
        return original(state,peer,raw)
    monkeypatch.setattr(w.o.episodes,'prepare_state',prepare)
    return seen


def test_validated_subtree_has_exact_original_bytes_and_no_second_full_encode(world,monkeypatch):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    expected=encode(value.snapshot);prepared=w.io.prepare_snapshot(value)
    seen=observe_episode(monkeypatch,w);validated=[];encoded=[]
    validate=drive_link.validate;encode_original=drive_link.encode
    def checked(message):
        validated.append(message.DESCRIPTOR.full_name)
        return validate(message)
    def encoding(message):
        encoded.append(message.DESCRIPTOR.name)
        return encode_original(message)
    monkeypatch.setattr(drive_link,'validate',checked)
    monkeypatch.setattr(drive_link,'encode',encoding)
    assert provider.receive(w.peer[0],value,w.w.t,prepared)
    assert validated==[schema.DriveLink.DESCRIPTOR.full_name]
    assert 'DriveState' not in encoded
    assert seen==[expected] and value.snapshot.SerializeToString(deterministic=True)==expected
    assert w.o.episodes.registry['state']['sha256']==sha256(expected).digest()
    assert provider.copies[value.snapshot.message_id][0]==prepared


def test_io_owned_snapshot_validates_once_and_keeps_exact_accepted_bytes(world,monkeypatch):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    raw=encode(value);prepared=w.io.prepare_snapshot(value);calls=[]
    original=codec.validate
    def checked(message):
        calls.append(message.DESCRIPTOR.name)
        return original(message)
    monkeypatch.setattr(codec,'validate',checked)
    monkeypatch.setattr(drive_link,'validate',checked)
    incoming=w.io._decode_incoming(w.peer[0],raw,w.w.t)
    # 외부 protobuf 변경은 private decode 사본에 도달하지 않는다.
    value.snapshot.ClearField('major')
    w.io.incoming.put_nowait(incoming)
    provider.tick()
    assert calls.count('DriveLink')==1 and 'DriveState' not in calls
    assert provider.copies[incoming.arguments[1].snapshot.message_id][0]==prepared


def test_queued_validated_snapshot_still_requires_current_authority(world):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    w.io.incoming.put_nowait(w.io._decode_incoming(w.peer[0],encode(value),w.w.t))
    w.w.m.output_permitted=False
    provider.tick()
    assert provider.reasons['STATE_SOURCE_AUTHORITY']==1
    assert not provider.copies


def test_public_take_does_not_bypass_mutable_protobuf_validation(world):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    w.io.incoming.put_nowait(w.io._decode_incoming(w.peer[0],encode(value),w.w.t))
    item=w.io.take()
    item[1].snapshot.ClearField('major')
    assert not provider.receive(*item)
    assert provider.reasons['LINK_WIRE_INVALID']==1


def test_private_wrapper_still_checks_total_wire_size(world,monkeypatch):
    w=activate(world);raw=encode(state_case(w))
    monkeypatch.setattr(drive_link,'MAX_BYTES',len(raw)+1)
    with pytest.raises(ValueError,match='wire size cap'):
        w.io._decode_incoming(w.peer[0],raw,w.w.t)


@pytest.mark.parametrize('invalid',['required','nan','oversize','path-cap','task-crossing'])
def test_direct_invalid_state_never_reaches_episode_or_accepts_old_prepared_bytes(world,monkeypatch,invalid):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    prepared=w.io.prepare_snapshot(value);seen=observe_episode(monkeypatch,w)
    if invalid=='required':value.snapshot.ClearField('major')
    elif invalid=='nan':value.snapshot.path[0].pose.x=float('nan')
    elif invalid=='oversize':value.snapshot.reference_id='x'*(drive_link.MAX_BYTES+1)
    elif invalid=='path-cap':
        for i in range(513-len(value.snapshot.path)):
            value.snapshot.path.add(pose=schema.Pose(x=float(i+1),y=1.6,yaw=0),steering_rad=0,velocity_mps=.2)
    else:
        trust=provider.trust;value.snapshot.state_features|=8
        value.snapshot.task_reference.CopyFrom(schema.TaskReferenceState(home_lane_id=1,from_lane_id=1,
            selected_lane_id=1,changing=False,basis_revision=1,vehicle=trust.vehicle,
            mapper_boot=trust.mapper_boot,mapper_instance=trust.mapper_instance,clock_epoch=trust.clock_epoch))
        for x,y in ((0.,0.),(2.,2.),(0.,2.),(2.,0.)):
            value.snapshot.task_reference.basis_prefix.add(x=x,y=y)
    # A direct caller can mutate a protobuf after earlier preparation. The
    # actual actor still rejects it before the raw State fingerprint is used.
    assert not provider.receive(w.peer[0],value,w.w.t,prepared)
    assert provider.reasons['LINK_WIRE_INVALID']==1 and seen==[] and not provider.copies


@pytest.mark.parametrize('invalid',['source','generation','configuration','authority'])
def test_validated_state_still_uses_current_source_and_authority(world,monkeypatch,invalid):
    w=activate(world);provider=w.r.drive_link;value=state_case(w)
    prepared=w.io.prepare_snapshot(value);seen=observe_episode(monkeypatch,w)
    reason='STATE_SOURCE_AUTHORITY'
    if invalid=='source':value.snapshot.source_execution=str(uuid4());reason='LINK_EXECUTION'
    elif invalid=='generation':value.snapshot.source_generation+=1
    elif invalid=='configuration':value.snapshot.configuration='different';reason='STATE_CONFIGURATION'
    else:w.w.m.output_permitted=False
    assert not provider.receive(w.peer[0],value,w.w.t,prepared)
    assert provider.reasons[reason]==1 and seen==[] and not provider.copies
