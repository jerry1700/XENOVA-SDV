"""Pure numeric semantic-walk shortcut; no input/authority result is cached.

Typed geometry validates bytes and invariants only. It proves no live source,
planner, clock, permission, or physical readiness.
"""
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest
from google.protobuf import descriptor_pb2,descriptor_pool,message_factory
from xenova_drive_world import codec,decode,encode,schema
from test_drive_link import A,snapshot,trust


def numeric(kind):
    if kind=='Point':return schema.Point(x=.25,y=-.5)
    if kind=='Pose':return schema.Pose(x=.25,y=-.5,yaw=1.)
    return schema.PathPoint(pose=schema.Pose(x=.25,y=-.5,yaw=1.),steering_rad=.125,velocity_mps=.25)


def path_state():
    w=SimpleNamespace(t=1.,m=SimpleNamespace(generation=1),executions={A:str(uuid4())},
                      ids={A:{'autonomous':str(uuid4())}})
    value=snapshot(w,trust(w));path=value.snapshot.path
    for i in range(2,512):
        path.add(pose=schema.Pose(x=.3+.02*i,y=1.6,yaw=0),steering_rad=0,velocity_mps=.2)
    value.snapshot.segments[0].last_index=len(path)-1
    return value


def varint(value):
    raw=bytearray()
    while value>127:raw.append((value&127)|128);value>>=7
    raw.append(value);return bytes(raw)


def test_exact_numeric_shapes_keep_original_wire_and_parent_semantics(monkeypatch):
    assert codec._NUMERIC_TREE_TYPES==(schema.Point,schema.Pose,schema.PathPoint)
    point=b'\x0d'+struct.pack('<f',.25)+b'\x15'+struct.pack('<f',-.5)
    pose=point+b'\x1d'+struct.pack('<f',1.)
    path=b'\x0a'+bytes([len(pose)])+pose+b'\x15'+struct.pack('<f',.125)+b'\x1d'+struct.pack('<f',.25)
    for kind,expected in (('Point',point),('Pose',pose),('PathPoint',path)):
        value=numeric(kind)
        assert encode(value)==expected and decode(expected,type(value))==value
    calls=[];original=codec._validate_one
    def checked(message):
        calls.append(type(message))
        return original(message)
    monkeypatch.setattr(codec,'_validate_one',checked)
    value=path_state();raw=encode(value)
    assert decode(raw,schema.DriveLink)==value
    assert all(t not in calls for t in (schema.Point,schema.Pose,schema.PathPoint))
    assert calls.count(schema.DriveState)==2 and calls.count(schema.DriveLink)==2
    assert calls.count(schema.GearSegment)==2 and calls.count(schema.MissionExecution)==2


@pytest.mark.parametrize('kind',['Point','Pose','PathPoint'])
@pytest.mark.parametrize('invalid',['required','nan','inf','byte-cap'])
def test_numeric_fastpath_never_skips_fresh_required_finite_or_byte_checks(kind,invalid):
    value=numeric(kind);encode(value)
    if invalid=='required':value.ClearField('pose' if kind=='PathPoint' else 'x')
    elif invalid in ('nan','inf'):setattr(value,'steering_rad' if kind=='PathPoint' else 'x',float(invalid))
    else:
        # Unknown optional bytes still contribute to the original message cap.
        # This is malformed test input, not a new product field/contract.
        value.MergeFromString(b'\xa2\x06'+varint(codec.MAX_BYTES)+b'x'*codec.MAX_BYTES)
    with pytest.raises(ValueError):encode(value)


@pytest.mark.parametrize('invalid',['velocity-sign','duplicate-pose','segment-range','path-cap','metadata-cap'])
def test_parent_path_semantics_and_repeated_cap_run_after_every_numeric_walk(invalid):
    value=path_state();encode(value);state=value.snapshot
    if invalid=='velocity-sign':state.path[0].velocity_mps=-.2
    elif invalid=='duplicate-pose':state.path[1].pose.CopyFrom(state.path[0].pose)
    elif invalid=='segment-range':state.segments[0].last_index=0
    elif invalid=='path-cap':
        state.path.add(pose=schema.Pose(x=20,y=1.6,yaw=0),steering_rad=0,velocity_mps=.2)
    else:state.MergeFromString(b'\xa2\x06'+varint(4096)+b'x'*4096)
    with pytest.raises(ValueError):encode(value)


@pytest.mark.parametrize('invalid',['pose','speed'])
def test_stationary_signed_cusp_is_still_checked_by_parent(invalid):
    value=path_state();state=value.snapshot;state.ClearField('path');state.ClearField('segments')
    for x,velocity in ((.3,.2),(.32,0.),(.32,0.),(.31,-.2)):
        state.path.add(pose=schema.Pose(x=x,y=1.6,yaw=0),steering_rad=0,velocity_mps=velocity)
    state.segments.add(first_index=0,last_index=1,signed_direction=1)
    state.segments.add(first_index=2,last_index=3,signed_direction=-1)
    encode(value)
    if invalid=='pose':state.path[2].pose.y+=.01
    else:state.path[2].velocity_mps=-.01
    with pytest.raises(ValueError,match='stationary cusp pair'):encode(value)


def test_same_name_future_numeric_type_with_semantic_child_uses_full_walk():
    file=descriptor_pb2.FileDescriptorProto(name='numeric-future-test.proto',package='numeric_future',syntax='proto2')
    schema.GearSegment.DESCRIPTOR.CopyToProto(file.message_type.add())
    pose=file.message_type.add();schema.Pose.DESCRIPTOR.CopyToProto(pose)
    pose.field.add(name='segment',number=100,label=1,type=11,type_name='.numeric_future.GearSegment')
    pool=descriptor_pool.DescriptorPool();pool.AddSerializedFile(file.SerializeToString())
    descriptor=pool.FindMessageTypeByName('numeric_future.Pose')
    make=getattr(message_factory,'GetMessageClass',None)
    future_type=make(descriptor) if make else message_factory.MessageFactory(pool).GetPrototype(descriptor)
    value=future_type(x=.25,y=-.5,yaw=1.)
    value.segment.first_index=0;value.segment.last_index=0;value.segment.signed_direction=0
    assert value.IsInitialized() and type(value) not in codec._NUMERIC_TREE_TYPES
    with pytest.raises(ValueError,match='gear segment range/direction'):encode(value)
