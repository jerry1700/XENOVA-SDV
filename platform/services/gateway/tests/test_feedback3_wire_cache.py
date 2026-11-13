"""원 Vehicle wire의 내용·출처 변경과 제한된 수명만 검증한다. 정지 판정은 별도다."""
from collections import OrderedDict
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from xenova_gateway.feedback3 import Feedback3SerialSink
from xenova_gateway.odometry import VehicleOdometry
from xenova_drive_world import encode,decode,schema
from test_odometry import config
from xenova_gateway.scene_authority import StationaryPolicy,check_stationary_window,SceneAuthorityError
import pytest


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


def test_cached_vehicle_decode_does_not_reuse_stationary_age_or_origin_acceptance():
    odom=VehicleOdometry(replace(config(),heading_error_rad=.000001),str(uuid4()))
    pi,device,gateway=str(uuid4()),str(uuid4()),str(uuid4());odom.device_session(device)
    for sample in range(1,5):
        stamp=1000000+80000*sample
        odom.observe_mapped(pi,device,stamp,sample,0,0,1,0,0,stamp+100000,stamp+100100)
    rows=tuple(encode(schema.VehicleSample(major=1,**{**row,'gateway_instance':gateway})) for row in odom.samples)
    last=decode(rows[-1],schema.VehicleSample)
    source={k:getattr(last,k) for k in ('vehicle','calibration','odom_epoch')}
    policy=StationaryPolicy(60000,100000,200000,.000001,.03,.1,.001,.001)
    args=(rows,source,odom.host_boot,gateway,pi,device,policy)
    assert check_stationary_window(*args,last.pi_sample_us+1000,last.source_sample,last.source_us)
    with pytest.raises(SceneAuthorityError,match='fresh matched sensor window'):
        check_stationary_window(*args,last.pi_sample_us+200001,last.source_sample,last.source_us)
    with pytest.raises(SceneAuthorityError,match='source/session'):
        check_stationary_window(rows,source,odom.host_boot,str(uuid4()),pi,device,policy,
            last.pi_sample_us+1000,last.source_sample,last.source_us)
