"""원 Vehicle wire의 내용·출처 변경과 제한된 수명만 검증한다. 정지 판정은 별도다."""
from collections import OrderedDict
from unittest.mock import patch
from uuid import uuid4

from xenova_gateway.feedback3 import Feedback3SerialSink
from xenova_gateway.odometry import VehicleOdometry
from xenova_drive_world import encode,decode,schema
from test_odometry import config


def rig():
    sink=object.__new__(Feedback3SerialSink);sink._vehicle_wire_cache=OrderedDict()
    odom=VehicleOdometry(config(),str(uuid4()));device=str(uuid4());pi=str(uuid4())
    odom.device_session(device)
    odom.observe_mapped(pi,device,1000000,1,0,0,1,0,0,1100000,1100100)
    row=odom.observe_mapped(pi,device,1020000,2,0,0,1,0,0,1120000,1120100)
    return sink,row,str(uuid4())


def test_same_original_row_reuses_only_deterministic_wire():
    sink,row,gateway=rig()
    with patch('xenova_gateway.feedback3.encode',wraps=encode) as encoded:
        original=sink._vehicle_wire(row,gateway)
        assert sink._vehicle_wire(row,gateway) is original
        assert encoded.call_count==1
    assert decode(original,schema.VehicleSample).source_us==row['source_us']


def test_changed_contents_pose_or_gateway_cannot_reuse_old_wire():
    sink,row,gateway=rig()
    original=sink._vehicle_wire(row,gateway)
    row['left_ticks']+=1
    changed=sink._vehicle_wire(row,gateway)
    assert changed!=original
    row['odom_pose']['x']=.1
    moved=sink._vehicle_wire(row,gateway)
    assert moved!=changed
    other=str(uuid4());new_gateway=sink._vehicle_wire(row,other)
    assert decode(new_gateway,schema.VehicleSample).gateway_instance==other
    assert new_gateway!=moved


def test_wire_cache_retains_at_most_original_history_capacity():
    sink,row,gateway=rig()
    for sequence in range(1,258):
        sample={**row,'sequence':sequence,'source_sample':sequence,'odom_pose':dict(row['odom_pose'])}
        sink._vehicle_wire(sample,gateway)
    assert len(sink._vehicle_wire_cache)==128
